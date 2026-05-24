
#include "uwp/update.hpp"

#include <algorithm>
#include <cmath>
#include <mutex>
#include <unordered_map>

#include "uwp/parallel_for.hpp"

namespace uwp {

// Approximate kilometres-per-degree of latitude (constant) and longitude
// (latitude-dependent: shrinks by cos(latitude)). Good enough for an
// inland-distance cap that is intentionally conservative.
namespace {

constexpr double KM_PER_DEG_LAT = 111.0;

// Build a "cap box" from a water polygon's envelope, expanded by max_km
// kilometres on every side. Longitude expansion uses cos(latitude_midpoint)
// so the cap stays roughly metric even at high latitudes.
//
// `cos(lat)` is clamped to 0.01 (≈89.4°) to avoid the singularity at the
// poles and keep the cap finite. Latitude is clamped to [-90, 90]; longitude
// is wrapped to [-180, 180] only loosely — Boost.Geometry's intersection
// handles oversized boxes gracefully because the input polygons stay within
// [-180, 180].
auto build_cap_box(const Box &envelope, double max_km) -> Box {
  const double min_lon = bg::get<bg::min_corner, 0>(envelope);
  const double min_lat = bg::get<bg::min_corner, 1>(envelope);
  const double max_lon = bg::get<bg::max_corner, 0>(envelope);
  const double max_lat = bg::get<bg::max_corner, 1>(envelope);

  const double lat_mid = (min_lat + max_lat) * 0.5;
  const double cos_lat = std::max(0.01, std::cos(lat_mid * M_PI / 180.0));

  const double dlat = max_km / KM_PER_DEG_LAT;
  const double dlon = max_km / (KM_PER_DEG_LAT * cos_lat);

  return Box{Point{min_lon - dlon, std::max(-90.0, min_lat - dlat)},
             Point{max_lon + dlon, std::min(90.0, max_lat + dlat)}};
}

// Apply the cap box to an area polygon and emit each resulting piece into
// `out`. If max_inland_km <= 0 (cap disabled) or the area polygon fits
// entirely inside the cap, the original polygon is emitted unchanged — no
// intersection cost is paid.
//
// Intersection of a polygon and a box can produce zero, one, or many polygon
// pieces (e.g. when a long river crosses the cap in two narrow places).
// Multiple pieces share the same water_idx, so the downstream `groupby` will
// merge them into the same union group.
void emit_clipped_pieces(const Polygon &area_polygon, const Box &water_env,
                         double max_inland_km, size_t water_idx,
                         std::vector<std::pair<size_t, Polygon>> &out) {
  if (max_inland_km <= 0.0) {
    out.emplace_back(water_idx, area_polygon);
    return;
  }

  const Box cap = build_cap_box(water_env, max_inland_km);
  const Box area_env = bg::return_envelope<Box>(area_polygon);

  // Cheap envelope check: if the area polygon is already inside the cap,
  // skip the intersection entirely (covers the common case of small estuary
  // polygons that don't extend far inland).
  if (bg::covered_by(area_env, cap)) {
    out.emplace_back(water_idx, area_polygon);
    return;
  }

  std::vector<Polygon> clipped;
  bg::intersection(area_polygon, cap, clipped);
  for (auto &&piece : clipped) {
    out.emplace_back(water_idx, std::move(piece));
  }
}

// Orphan-flavoured variant of `emit_clipped_pieces`: same clipping logic
// (clip to anchor envelope + max_inland_km buffer) but no water_idx
// pairing — orphans live as standalone polygons in the output.
void emit_clipped_orphan(const Polygon &area_polygon, const Box &anchor_env,
                         double max_inland_km, std::vector<Polygon> &out) {
  if (max_inland_km <= 0.0) {
    out.emplace_back(area_polygon);
    return;
  }
  const Box cap = build_cap_box(anchor_env, max_inland_km);
  const Box area_env = bg::return_envelope<Box>(area_polygon);
  if (bg::covered_by(area_env, cap)) {
    out.emplace_back(area_polygon);
    return;
  }
  std::vector<Polygon> clipped;
  bg::intersection(area_polygon, cap, clipped);
  for (auto &&piece : clipped) {
    out.emplace_back(std::move(piece));
  }
}

}  // namespace

// Inverted-loop helper: for each area polygon in [i0, i1), query the water
// R-tree for candidate water polygons and record the first valid match.
//
// "First valid match" means: a water polygon `w` such that
//   bg::intersects(w, area)  AND  NOT bg::within(area, w)
// (i.e. they overlap, and the area is not entirely contained in `w` — in the
// latter case the area is already covered and merging is unnecessary).
//
// Assigning each area polygon to at most one water polygon preserves the
// invariant the merge phase relies on: each water index appears in the
// returned grouping at most once (after the post-loop groupby), so the merge
// can mutate water polygons without cross-thread synchronization.
// Worker output: matched pairs + orphan polygons accumulated in one chunk.
struct LocalSelection {
  std::vector<std::pair<size_t, Polygon>> matched;
  std::vector<Polygon> orphans;
};

inline auto select_overlap_local(const Shapefile &water_shp,
                                 const Shapefile::PolygonList &area,
                                 const size_t i0, const size_t i1,
                                 double max_inland_km) -> LocalSelection {
  auto out = LocalSelection{};
  out.matched.reserve(i1 - i0);

  const auto &water_polygons = *water_shp.polygons();
  const auto &water_rtree = *water_shp.rtree();

  std::vector<Shapefile::PolygonIndex> water_candidates;
  for (size_t ix = i0; ix < i1; ++ix) {
    const auto &area_polygon = *area[ix];
    const auto envelope = bg::return_envelope<Box>(area_polygon);

    // Phase 1: try to match against a coast polygon that the area
    // polygon actually intersects (and is not fully inside).
    water_candidates.clear();
    water_rtree.query(bg::index::intersects(envelope),
                      std::back_inserter(water_candidates));

    bool matched_any = false;
    for (const auto &candidate : water_candidates) {
      const auto water_idx = candidate.second;
      const auto &water_polygon = *water_polygons[water_idx];
      if (!bg::intersects(water_polygon, area_polygon) ||
          bg::within(area_polygon, water_polygon)) {
        continue;
      }
      // Exclusive assignment: this area polygon (or its clipped pieces)
      // belongs to `water_idx` only.
      emit_clipped_pieces(area_polygon, candidate.first, max_inland_km,
                          water_idx, out.matched);
      matched_any = true;
      break;
    }

    // Phase 2: if no intersection found and max_inland_km > 0, look for
    // a coast polygon truly within max_inland_km of the area polygon
    // (using actual polygon-to-polygon distance, not just bbox overlap).
    // If found, the area polygon is an "orphan coastal feature": include
    // it standalone (no union with any coast), but clip it to the
    // nearest coast's envelope + max_inland_km buffer so a long river
    // polygon doesn't drag inland.
    if (!matched_any && max_inland_km > 0.0) {
      const Box neighbourhood = build_cap_box(envelope, max_inland_km);
      water_candidates.clear();
      water_rtree.query(bg::index::intersects(neighbourhood),
                        std::back_inserter(water_candidates));

      // Tight distance check: compute actual polygon-to-polygon distance
      // and keep only candidates within the threshold. The bbox-based
      // rtree query above is a coarse prefilter — its hits include
      // false positives where the bbox is near but the actual geometry
      // is far away.
      // Cheap latitude-aware degree → km conversion at the area's
      // centroid latitude (orphans are usually small enough that
      // varying lat across the polygon doesn't matter).
      const double lat_mid = (bg::get<bg::min_corner, 1>(envelope) +
                              bg::get<bg::max_corner, 1>(envelope)) *
                             0.5;
      const double cos_lat = std::max(0.01, std::cos(lat_mid * M_PI / 180.0));
      const double threshold_deg = max_inland_km / KM_PER_DEG_LAT;
      // For longitude-direction distance, the threshold scales by
      // cos(lat). We use the smaller of the two as a conservative
      // bound (distance > min(threshold_lat, threshold_lon) is the
      // tightest exclusion). In practice envelope-distance in degrees
      // mixes both axes — use the lat threshold (smaller) as a
      // conservative filter.
      const double threshold_env_deg = threshold_deg * cos_lat;

      const Box *nearest_env = nullptr;
      double nearest_dist = std::numeric_limits<double>::max();
      for (const auto &c : water_candidates) {
        // Cheap envelope-distance lower bound first — skip the
        // expensive polygon distance when the envelopes are far apart.
        const double env_dist = bg::distance(envelope, c.first);
        if (env_dist > threshold_env_deg) {
          continue;
        }
        // Actual polygon-to-polygon distance (0 if they touch).
        const double poly_dist =
            bg::distance(area_polygon, *water_polygons[c.second]);
        if (poly_dist > threshold_env_deg) {
          continue;
        }
        if (poly_dist < nearest_dist) {
          nearest_dist = poly_dist;
          nearest_env = &c.first;
        }
      }

      if (nearest_env != nullptr) {
        // Clip the orphan to anchor envelope + max_inland_km buffer
        // (same logic as matched polygons), so a long river polygon
        // gets trimmed to its seaward portion.
        emit_clipped_orphan(area_polygon, *nearest_env, max_inland_km,
                            out.orphans);
      }
      // else: no coast within max_inland_km → real orphan, dropped.
    }
  }
  return out;
}

auto select_overlap(const Shapefile &water_shp,
                    const Shapefile::PolygonList &area, double max_inland_km)
    -> SelectOverlapResult {
  auto pairs = std::vector<std::pair<size_t, Polygon>>();
  auto orphans = std::vector<Polygon>();
  auto mutex = std::mutex();

  auto worker = [&water_shp, &area, &pairs, &orphans, &mutex, max_inland_km](
                    const size_t i0, const size_t i1) {
    auto local = select_overlap_local(water_shp, area, i0, i1, max_inland_km);
    if (local.matched.empty() && local.orphans.empty()) {
      return;
    }
    std::lock_guard<std::mutex> lock(mutex);
    pairs.insert(pairs.end(), std::make_move_iterator(local.matched.begin()),
                 std::make_move_iterator(local.matched.end()));
    orphans.insert(orphans.end(),
                   std::make_move_iterator(local.orphans.begin()),
                   std::make_move_iterator(local.orphans.end()));
  };
  // num_threads = 0 → use std::thread::hardware_concurrency(); chunk size is
  // chosen dynamically inside parallel_for to balance load across threads.
  parallel_for(worker, area.size(), 0);

  // Group matches by water polygon index. Each water index appears at most
  // once in the result so the merge phase can mutate water polygons without
  // synchronization. (A single area polygon clipped into multiple pieces all
  // map to the same water_idx and end up in the same group.)
  std::unordered_map<size_t, std::vector<Polygon>> grouped;
  grouped.reserve(pairs.size());
  for (auto &&p : pairs) {
    grouped[p.first].emplace_back(std::move(p.second));
  }

  auto result = SelectOverlapResult{};
  result.matched.reserve(grouped.size());
  for (auto &&kv : grouped) {
    std::cout << "#" << kv.first << " " << kv.second.size() << std::endl;
    result.matched.emplace_back(kv.first, std::move(kv.second));
  }
  result.orphans = std::move(orphans);
  if (!result.orphans.empty()) {
    std::cout << "Orphans (no coast intersection, within " << max_inland_km
              << " km of coast): " << result.orphans.size() << std::endl;
  }
  return result;
}

// A helper function that computes the union of two polygons
inline auto multi_polygon_union(const MultiPolygon &mpoly1,
                                const MultiPolygon &mpoly2) -> MultiPolygon {
  MultiPolygon output;
  bg::union_(mpoly1, mpoly2, output);
  return output;
}

// Comparator based on area to prioritize merging smaller multipolygons first
struct CompareArea {
  auto operator()(const MultiPolygon &a, const MultiPolygon &b) const -> bool {
    return bg::area(a) > bg::area(b);
  }
};

auto cascade_union(const std::vector<Polygon> &polygons)
    -> std::vector<Polygon> {
  if (polygons.empty()) {
    return {};
  }
  std::priority_queue<MultiPolygon, std::vector<MultiPolygon>, CompareArea>
      polygon_queue;

  for (const auto &item : polygons) {
    polygon_queue.push(MultiPolygon{item});
  }

  while (polygon_queue.size() > 1) {
    // Extract two smallest multipolygons
    auto mpoly1 = polygon_queue.top();
    polygon_queue.pop();

    auto mpoly2 = polygon_queue.top();
    polygon_queue.pop();

    // Compute the union of the two multipolygons and push it back to the queue
    polygon_queue.push(multi_polygon_union(mpoly1, mpoly2));
  }

  // Return resulting multipolygon
  auto final_result = polygon_queue.top();
  return {final_result.begin(), final_result.end()};
}

auto merge_overlapping(
    const std::vector<std::pair<size_t, std::vector<Polygon>>> &overlap,
    const size_t i0, const size_t i1)
    -> std::vector<std::pair<size_t, std::vector<Polygon>>> {
  auto result = std::vector<std::pair<size_t, std::vector<Polygon>>>();
  for (size_t ix = i0; ix < i1; ++ix) {
    const auto &item = overlap[ix];
    auto unioned = cascade_union(item.second);
    if (unioned.empty()) {
      continue;
    }
    result.emplace_back(item.first, std::move(unioned));
  }
  return result;
}

auto merge_overlapping(
    Shapefile &water_shp,
    const std::vector<std::pair<size_t, std::vector<Polygon>>> &overlap,
    Shapefile *patches_out) -> void {
  auto &water_polygons = *(water_shp.polygons());
  auto extra_polygons = std::vector<Polygon>();
  auto patches = std::vector<Polygon>();  // accumulated only if requested

  auto mutex = std::mutex();
  const bool collect_patches = (patches_out != nullptr);

  // Note: each `item.first` (water polygon index) is unique across `overlap`
  // (select_overlap produces at most one entry per index). Different threads
  // therefore operate on disjoint water polygons, so the per-polygon union and
  // assignment require no synchronization. Only the append into the shared
  // `extra_polygons` and `patches` vectors needs to be protected. We
  // accumulate into thread-local vectors and merge once at the end to keep
  // contention near zero.
  auto worker = [&extra_polygons, &patches, collect_patches, &mutex, &overlap,
                 &water_polygons](const size_t i0, const size_t i1) {
    auto unioned_polygons = merge_overlapping(overlap, i0, i1);
    auto local_extra = std::vector<Polygon>();
    auto local_patches = std::vector<Polygon>();
    for (auto &&item : unioned_polygons) {
      // Capture every piece of the cascade-unioned area polygons as a
      // patch — this is the ground-truth "what we added" representation.
      // Done BEFORE the in-place union with the water polygon, while the
      // pieces are still distinct geometric entities.
      if (collect_patches) {
        for (const auto &piece : item.second) {
          local_patches.emplace_back(piece);
        }
      }

      auto &water_polygon = water_polygons[item.first];
      // Merge the target polygon with the unioned polygons
      std::vector<Polygon> unioned;
      bg::union_(*water_polygon, item.second.front(), unioned);
      if (!unioned.empty()) {
        *water_polygon = std::move(unioned.front());
        for (auto it = unioned.begin() + 1; it != unioned.end(); ++it) {
          local_extra.emplace_back(std::move(*it));
        }
      }
      for (auto it = item.second.begin() + 1; it != item.second.end(); ++it) {
        local_extra.emplace_back(std::move(*it));
      }
    }
    if (!local_extra.empty() || !local_patches.empty()) {
      std::lock_guard<std::mutex> lock(mutex);
      if (!local_extra.empty()) {
        extra_polygons.insert(extra_polygons.end(),
                              std::make_move_iterator(local_extra.begin()),
                              std::make_move_iterator(local_extra.end()));
      }
      if (!local_patches.empty()) {
        patches.insert(patches.end(),
                       std::make_move_iterator(local_patches.begin()),
                       std::make_move_iterator(local_patches.end()));
      }
    }
  };

  parallel_for(worker, overlap.size(), 0);
  for (auto &&polygon : extra_polygons) {
    water_shp.append(std::move(polygon));
  }
  if (collect_patches) {
    for (auto &&polygon : patches) {
      patches_out->append(std::move(polygon));
    }
  }
}

}  // namespace uwp

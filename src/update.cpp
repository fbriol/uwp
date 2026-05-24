
#include "uwp/update.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <mutex>
#include <numeric>
#include <unordered_map>
#include <unordered_set>

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

// Union-Find with path compression and rank — used to identify
// connected components of area polygons in the cascading orphan pass.
class UnionFind {
 public:
  explicit UnionFind(size_t n) : parent_(n), rank_(n, 0) {
    std::iota(parent_.begin(), parent_.end(), size_t{0});
  }

  auto find(size_t x) -> size_t {
    while (parent_[x] != x) {
      parent_[x] = parent_[parent_[x]];  // path compression
      x = parent_[x];
    }
    return x;
  }

  void unite(size_t a, size_t b) {
    a = find(a);
    b = find(b);
    if (a == b) return;
    if (rank_[a] < rank_[b]) std::swap(a, b);
    parent_[b] = a;
    if (rank_[a] == rank_[b]) {
      ++rank_[a];
    }
  }

 private:
  std::vector<size_t> parent_;
  std::vector<size_t> rank_;
};

// Find non-matched area polygons that belong to the same connected
// component as a matched one — i.e. coastal polygons that don't touch
// the OSM coastline directly but ARE part of the coastal water network
// via a chain of intersecting OSM water polygons. Typical use case:
// deltaic river channels that connect to other channels which
// eventually touch the open sea. Inland lakes have no neighbours and
// stay in their own (non-coastal) component → excluded.
//
// Returns area-polygon indices to be included as orphans.
auto compute_cascading_orphans(const Shapefile::PolygonList &area,
                               const std::vector<char> &matched_flags)
    -> std::vector<size_t> {
  const size_t n = area.size();

  // Build an R-tree on the area polygons' envelopes so we only test
  // intersection between polygons whose envelopes overlap.
  std::vector<Shapefile::PolygonIndex> indexed;
  indexed.reserve(n);
  for (size_t i = 0; i < n; ++i) {
    indexed.emplace_back(bg::return_envelope<Box>(*area[i]), i);
  }
  auto area_rtree = Shapefile::RTree(indexed);

  // Union-Find over all area polygons. Linked pairs = polygons whose
  // geometries actually intersect (envelope is only a cheap prefilter).
  UnionFind uf(n);
  std::vector<Shapefile::PolygonIndex> candidates;
  for (size_t i = 0; i < n; ++i) {
    const auto envelope = bg::return_envelope<Box>(*area[i]);
    candidates.clear();
    area_rtree.query(bg::index::intersects(envelope),
                     std::back_inserter(candidates));
    for (const auto &c : candidates) {
      const size_t j = c.second;
      if (j <= i) continue;                    // pair handled once
      if (uf.find(i) == uf.find(j)) continue;  // already linked
      if (bg::intersects(*area[i], *area[j])) {
        uf.unite(i, j);
      }
    }
  }

  // Identify roots of components that contain at least one matched
  // polygon — those are the "coastal" components.
  std::unordered_set<size_t> coastal_roots;
  for (size_t i = 0; i < n; ++i) {
    if (matched_flags[i] != 0) {
      coastal_roots.insert(uf.find(i));
    }
  }

  // Non-matched polygons whose component is coastal get included as
  // orphans. Matched polygons are already in the matched list.
  std::vector<size_t> orphan_indices;
  for (size_t i = 0; i < n; ++i) {
    if (matched_flags[i] != 0) continue;
    if (coastal_roots.count(uf.find(i)) > 0) {
      orphan_indices.push_back(i);
    }
  }
  return orphan_indices;
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
// Worker output: matched (water_idx, polygon) pairs for one chunk, plus
// the area indices that found a match (used for the cascading orphan
// pass after all workers join).
struct LocalSelection {
  std::vector<std::pair<size_t, Polygon>> matched;
  std::vector<size_t> matched_indices;  // area indices that got matched
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

    water_candidates.clear();
    water_rtree.query(bg::index::intersects(envelope),
                      std::back_inserter(water_candidates));

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
      out.matched_indices.push_back(ix);
      break;
    }
  }
  return out;
}

// Real geometric distance (in kilometres) from an area polygon to the
// nearest coast polygon, computed via bg::distance on the actual ring
// geometry — NOT envelope-to-envelope. Returns +inf if no coast polygon
// lies within the search neighbourhood (envelope expanded by
// `max_inland_km`), in which case the orphan is far inland and should
// be dropped.
//
// Coordinates are Cartesian lon/lat degrees; we convert the degree
// distance to kilometres using the latitude midpoint of the area
// polygon's envelope (same approximation as build_cap_box, accurate to
// a few % even at high latitudes which is plenty for the inland cap).
auto distance_to_coast_km(const Shapefile &water_shp,
                          const Polygon &area_polygon, double max_inland_km)
    -> double {
  static thread_local std::vector<Shapefile::PolygonIndex> candidates;
  const Box envelope = bg::return_envelope<Box>(area_polygon);
  const Box neighbourhood = build_cap_box(envelope, max_inland_km);
  candidates.clear();
  water_shp.rtree()->query(bg::index::intersects(neighbourhood),
                           std::back_inserter(candidates));
  if (candidates.empty()) {
    return std::numeric_limits<double>::infinity();
  }

  const double lat_mid = 0.5 * (bg::get<bg::min_corner, 1>(envelope) +
                                bg::get<bg::max_corner, 1>(envelope));
  const double cos_lat = std::max(0.01, std::cos(lat_mid * M_PI / 180.0));
  // 1 degree ≈ this many km along a great-circle bearing that mixes
  // lat (111 km/°) and lon (111·cos(lat) km/°). The minimum is the
  // conservative bound for an arbitrary direction.
  const double km_per_deg = KM_PER_DEG_LAT * std::min(1.0, cos_lat);

  const auto &water_polygons = *water_shp.polygons();
  double best_km = std::numeric_limits<double>::infinity();
  for (const auto &c : candidates) {
    const auto &coast = *water_polygons[c.second];
    // Cheap early-out: envelope distance lower-bounds true distance.
    const double env_deg = bg::distance(envelope, c.first);
    if (env_deg * km_per_deg >= best_km) continue;
    const double d_deg = bg::distance(area_polygon, coast);
    const double d_km = d_deg * km_per_deg;
    if (d_km < best_km) {
      best_km = d_km;
      if (best_km == 0.0) break;
    }
  }
  return best_km;
}

auto select_overlap(const Shapefile &water_shp,
                    const Shapefile::PolygonList &area, double max_inland_km)
    -> SelectOverlapResult {
  // -------------------------------------------------------------------
  // Phase 1 (parallel): match each area polygon against the coast.
  // -------------------------------------------------------------------
  auto pairs = std::vector<std::pair<size_t, Polygon>>();
  auto matched_flags = std::vector<char>(area.size(), 0);
  auto mutex = std::mutex();

  auto worker = [&](const size_t i0, const size_t i1) {
    auto local = select_overlap_local(water_shp, area, i0, i1, max_inland_km);
    // matched_indices entries are unique to this chunk's [i0, i1) range,
    // so we can write the flags lock-free (disjoint memory).
    for (size_t ix : local.matched_indices) {
      matched_flags[ix] = 1;
    }
    if (local.matched.empty()) {
      return;
    }
    std::lock_guard<std::mutex> lock(mutex);
    pairs.insert(pairs.end(), std::make_move_iterator(local.matched.begin()),
                 std::make_move_iterator(local.matched.end()));
  };
  parallel_for(worker, area.size(), 0);

  // Group matches by water polygon index. Each water index appears at most
  // once so merge_overlapping can mutate water polygons without locking.
  std::unordered_map<size_t, std::vector<Polygon>> grouped;
  grouped.reserve(pairs.size());
  for (auto &&p : pairs) {
    grouped[p.first].emplace_back(std::move(p.second));
  }

  auto result = SelectOverlapResult{};
  result.matched.reserve(grouped.size());
  for (auto &&kv : grouped) {
    result.matched.emplace_back(kv.first, std::move(kv.second));
  }

  // -------------------------------------------------------------------
  // Phase 2 (single-threaded): cascading orphan inclusion.
  //
  // Only runs when max_inland_km > 0. We identify "coastal" components
  // of the area-polygon graph (intersection-linked), then include any
  // non-matched polygon that lives in such a component AND whose real
  // geometric distance (not envelope distance) to the nearest coast
  // polygon is within max_inland_km. Two filters in series:
  //
  //   * cascading filter → kills inland lakes (no chain to coast);
  //   * distance filter  → kills far-upstream river polygons (the
  //                        chain exists but extends 100s of km inland).
  //
  // We deliberately do NOT clip the kept orphan: river channels are
  // already short individual features and clipping would slice them
  // mid-water at an arbitrary box edge. Either the whole feature is
  // within max_inland_km of the coast or it's dropped.
  // -------------------------------------------------------------------
  if (max_inland_km > 0.0) {
    const auto orphan_idx = compute_cascading_orphans(area, matched_flags);
    size_t included = 0;
    size_t dropped_far = 0;
    for (size_t ix : orphan_idx) {
      const Polygon &area_polygon = *area[ix];
      const double d_km =
          distance_to_coast_km(water_shp, area_polygon, max_inland_km);
      if (d_km > max_inland_km) {
        ++dropped_far;
        continue;
      }
      result.orphans.emplace_back(area_polygon);
      ++included;
    }
    if (included > 0 || dropped_far > 0) {
      std::cout << "Cascading orphans: " << included << " kept (" << dropped_far
                << " dropped, > " << max_inland_km << " km from coast)"
                << std::endl;
    }
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


#include "uwp/update.hpp"

#include <mutex>
#include <unordered_map>

#include "uwp/parallel_for.hpp"

namespace uwp {

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
// returned grouping at most once, so the merge can mutate water polygons
// without cross-thread synchronization.
inline auto select_overlap_local(const Shapefile &water_shp,
                                 const Shapefile::PolygonList &area,
                                 const size_t i0, const size_t i1)
    -> std::vector<std::pair<size_t, Polygon>> {
  auto result = std::vector<std::pair<size_t, Polygon>>();
  result.reserve(i1 - i0);

  const auto &water_polygons = *water_shp.polygons();
  const auto &water_rtree = *water_shp.rtree();

  std::vector<Shapefile::PolygonIndex> water_candidates;
  for (size_t ix = i0; ix < i1; ++ix) {
    const auto &area_polygon = *area[ix];

    // Envelope of the area polygon.
    auto envelope = bg::return_envelope<Box>(area_polygon);

    // Query the water R-tree for candidate water polygons.
    water_candidates.clear();
    water_rtree.query(bg::index::intersects(envelope),
                      std::back_inserter(water_candidates));
    if (water_candidates.empty()) {
      continue;
    }

    for (const auto &candidate : water_candidates) {
      const auto water_idx = candidate.second;
      const auto &water_polygon = *water_polygons[water_idx];

      if (!bg::intersects(water_polygon, area_polygon) ||
          bg::within(area_polygon, water_polygon)) {
        continue;
      }

      // Exclusive assignment: this area polygon belongs to `water_idx` only.
      result.emplace_back(water_idx, area_polygon);
      break;
    }
  }
  return result;
}

auto select_overlap(const Shapefile &water_shp,
                    const Shapefile::PolygonList &area)
    -> std::vector<std::pair<size_t, std::vector<Polygon>>> {
  auto pairs = std::vector<std::pair<size_t, Polygon>>();
  auto mutex = std::mutex();

  auto worker = [&water_shp, &area, &pairs, &mutex](const size_t i0,
                                                    const size_t i1) {
    auto local = select_overlap_local(water_shp, area, i0, i1);
    if (local.empty()) {
      return;
    }
    std::lock_guard<std::mutex> lock(mutex);
    pairs.insert(pairs.end(), std::make_move_iterator(local.begin()),
                 std::make_move_iterator(local.end()));
  };
  parallel_for(worker, area.size(), 128);

  // Group matches by water polygon index. Each water index appears at most
  // once in the result so the merge phase can mutate water polygons without
  // synchronization.
  std::unordered_map<size_t, std::vector<Polygon>> grouped;
  grouped.reserve(pairs.size());
  for (auto &&p : pairs) {
    grouped[p.first].emplace_back(std::move(p.second));
  }

  auto result = std::vector<std::pair<size_t, std::vector<Polygon>>>();
  result.reserve(grouped.size());
  for (auto &&kv : grouped) {
    std::cout << "#" << kv.first << " " << kv.second.size() << std::endl;
    result.emplace_back(kv.first, std::move(kv.second));
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
    const std::vector<std::pair<size_t, std::vector<Polygon>>> &overlap)
    -> void {
  auto &water_polygons = *(water_shp.polygons());
  auto extra_polygons = std::vector<Polygon>();

  auto mutex = std::mutex();

  // Note: each `item.first` (water polygon index) is unique across `overlap`
  // (select_overlap produces at most one entry per index). Different threads
  // therefore operate on disjoint water polygons, so the per-polygon union and
  // assignment require no synchronization. Only the append into the shared
  // `extra_polygons` vector needs to be protected. We accumulate into a
  // thread-local vector and merge once at the end to keep contention near zero.
  auto worker = [&extra_polygons, &mutex, &overlap, &water_polygons](
                    const size_t i0, const size_t i1) {
    auto unioned_polygons = merge_overlapping(overlap, i0, i1);
    auto local_extra = std::vector<Polygon>();
    for (auto &&item : unioned_polygons) {
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
    if (!local_extra.empty()) {
      std::lock_guard<std::mutex> lock(mutex);
      extra_polygons.insert(extra_polygons.end(),
                            std::make_move_iterator(local_extra.begin()),
                            std::make_move_iterator(local_extra.end()));
    }
  };

  parallel_for(worker, overlap.size(), 0);
  for (auto &&polygon : extra_polygons) {
    water_shp.append(std::move(polygon));
  }
}

}  // namespace uwp

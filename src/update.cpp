
#include "uwp/update.hpp"

#include <mutex>

#include "uwp/mutex_protected_set.hpp"
#include "uwp/parallel_for.hpp"

namespace uwp {

inline auto select_overlap(
    Shapefile::PolygonList &water, const Shapefile &area_shp, const size_t i0,
    const size_t i1, MutexProtectedSet<const Polygon *> &filtered_polygons)
    -> std::vector<std::pair<size_t, std::vector<Polygon>>> {
  auto result = std::vector<std::pair<size_t, std::vector<Polygon>>>();
  result.reserve(i1 - i0);

  for (size_t ix = i0; ix < i1; ++ix) {
    const auto &water_polygon = *water[ix];

    // Compute the envelope of the water polygon.
    auto envelope = bg::return_envelope<Box>(water_polygon);

    // Query the RTree index for the area polygons that intersect the envelope.
    std::vector<Shapefile::PolygonIndex> area_polygons;
    area_shp.rtree()->query(bg::index::intersects(envelope),
                            std::back_inserter(area_polygons));
    // If there are no area polygons that intersect the envelope, skip to the
    // next water polygon.
    if (area_polygons.empty()) {
      continue;
    }

    auto matching_areas = std::vector<Polygon>();
    for (auto &&item : area_polygons) {
      // Ensure the current area polygon isn't already included in the selection
      // list.
      if (filtered_polygons.contains(item.second)) {
        continue;
      }

      if (!bg::intersects(water_polygon, *item.second) ||
          bg::within(*item.second, water_polygon)) {
        continue;
      }

      if (filtered_polygons.insert(item.second).second) {
        // Add the area polygon to the selection list.
        matching_areas.emplace_back(*item.second);
      }
    }
    if (!matching_areas.empty()) {
      std::cout << "#" << ix << " " << matching_areas.size() << std::endl;
      result.emplace_back(std::make_pair(ix, std::move(matching_areas)));
    }
  }
  return result;
}

auto select_overlap(Shapefile::PolygonList &water, const Shapefile &area_shp)
    -> std::vector<std::pair<size_t, std::vector<Polygon>>> {
  auto filtered_polygons = MutexProtectedSet<const Polygon *>();
  auto result = std::vector<std::pair<size_t, std::vector<Polygon>>>();
  auto mutex = std::mutex();
  auto worker = [&area_shp, &filtered_polygons, &result, &mutex, &water](
                    const size_t i0, const size_t i1) {
    auto selected_overlapping_polygons =
        select_overlap(water, area_shp, i0, i1, filtered_polygons);
    {
      std::lock_guard<std::mutex> lock(mutex);
      result.insert(result.end(), selected_overlapping_polygons.begin(),
                    selected_overlapping_polygons.end());
    }
  };
  parallel_for(worker, water.size(), 128);
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
  bool operator()(const MultiPolygon &a, const MultiPolygon &b) const {
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
    result.emplace_back(std::make_pair(item.first, std::move(unioned)));
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

  auto worker = [&extra_polygons, &mutex, &overlap, &water_polygons](
                    const size_t i0, const size_t i1) {
    auto unioned_polygons = merge_overlapping(overlap, i0, i1);
    {
      std::lock_guard<std::mutex> lock(mutex);
      for (auto &&item : unioned_polygons) {
        auto &water_polygon = water_polygons[item.first];
        // Merge the target polygon with the unioned polygons
        std::vector<Polygon> unioned;
        bg::union_(*water_polygon, item.second.front(), unioned);
        if (!unioned.empty()) {
          *water_polygon = std::move(unioned.front());
          for (auto it = unioned.begin() + 1; it != unioned.end(); ++it) {
            extra_polygons.emplace_back(std::move(*it));
          }
        }
        for (auto it = item.second.begin() + 1; it != item.second.end(); ++it) {
          extra_polygons.emplace_back(std::move(*it));
        }
      }
    }
  };

  parallel_for(worker, overlap.size(), 0);
  for (auto &&polygon : extra_polygons) {
    water_shp.append(std::move(polygon));
  }
}

}  // namespace uwp

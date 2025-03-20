
#pragma once

#include <utility>
#include <vector>

#include "uwp/shapefile.hpp"

namespace uwp {

auto cascade_union(const std::vector<Polygon> &polygons)
    -> std::vector<Polygon>;

auto select_overlap(Shapefile::PolygonList &water, const Shapefile &area_shp)
    -> std::vector<std::pair<size_t, std::vector<Polygon>>>;

auto merge_overlapping(
    Shapefile &water_shp,
    const std::vector<std::pair<size_t, std::vector<Polygon>>> &overlap)
    -> void;

}  // namespace uwp

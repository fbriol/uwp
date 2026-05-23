
#pragma once

#include <utility>
#include <vector>

#include "uwp/shapefile.hpp"

namespace uwp {

auto cascade_union(const std::vector<Polygon> &polygons)
    -> std::vector<Polygon>;

/// @brief Selects the area polygons that overlap a water polygon.
///
/// Iterates over the (smaller) area-polygon set and queries the water
/// shapefile's R-tree to find candidate water polygons for each area polygon.
/// Each area polygon is assigned to at most one water polygon (the first
/// matching candidate) so the merge phase can mutate each water polygon
/// without synchronization.
///
/// @param[in] water_shp The water shapefile (must have its R-tree built).
/// @param[in] area     The list of area polygons to test against the water
///                     shapefile.
/// @param[in] max_inland_km If > 0, each matched area polygon is clipped to
///   the matched water polygon's envelope expanded by this many kilometres
///   (degree approximation via cos(latitude)). Stops river polygons that
///   extend hundreds of km inland from dragging the coastline along. 0
///   disables the clip — preserves the historical behaviour.
/// @return A list of `(water_index, area_polygons)` pairs — each water index
///         appears at most once.
auto select_overlap(const Shapefile &water_shp,
                    const Shapefile::PolygonList &area,
                    double max_inland_km = 0.0)
    -> std::vector<std::pair<size_t, std::vector<Polygon>>>;

auto merge_overlapping(
    Shapefile &water_shp,
    const std::vector<std::pair<size_t, std::vector<Polygon>>> &overlap)
    -> void;

}  // namespace uwp

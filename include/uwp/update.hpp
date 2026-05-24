
#pragma once

#include <utility>
#include <vector>

#include "uwp/shapefile.hpp"

namespace uwp {

auto cascade_union(const std::vector<Polygon> &polygons)
    -> std::vector<Polygon>;

/// @brief Selects the area polygons that overlap the water shapefile,
///        grouped by matched coast-polygon index.
///
/// Iterates over the (smaller) area-polygon set and queries the water
/// shapefile's R-tree. An area polygon is matched to a coast polygon
/// when they intersect AND the area polygon is not entirely contained
/// in the coast polygon. Each area polygon is assigned to at most one
/// coast polygon, so each water index appears at most once in the
/// returned vector.
///
/// @param[in] water_shp The water shapefile (must have its R-tree built).
/// @param[in] area     The list of area polygons to test against the water
///                     shapefile.
/// @param[in] max_inland_km Optional cap (km): each matched area polygon
///   is clipped to the matched coast polygon's envelope expanded by
///   this distance, preventing oversized river polygons from dragging
///   the coastline far inland. 0 disables clipping.
auto select_overlap(const Shapefile &water_shp,
                    const Shapefile::PolygonList &area,
                    double max_inland_km = 0.0)
    -> std::vector<std::pair<size_t, std::vector<Polygon>>>;

/// @brief Merge the selected area polygons into the matching water polygons.
///
/// For each `(water_idx, area_polygons)` entry in `overlap`:
///   1. Cascade-union the area polygons.
///   2. Union the result with `water_shp.polygons()[water_idx]` in place.
///   3. Any extra disconnected pieces are appended at the end of
///      `water_shp` (as new polygons).
///
/// @param[in,out] water_shp Coastal shapefile to mutate.
/// @param[in] overlap Output of `select_overlap`.
/// @param[in,out] patches_out Optional. If non-null, every polygon piece
///   that contributes to the merge — both the one fused into the water
///   polygon and the disconnected extras — is appended to this shapefile.
///   This gives the caller an exhaustive ground-truth record of what was
///   added, ready to be saved as a stand-alone shapefile for QA. Pass
///   nullptr to preserve the historical behaviour (no patches tracked).
auto merge_overlapping(
    Shapefile &water_shp,
    const std::vector<std::pair<size_t, std::vector<Polygon>>> &overlap,
    Shapefile *patches_out = nullptr) -> void;

}  // namespace uwp

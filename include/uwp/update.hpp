
#pragma once

#include <utility>
#include <vector>

#include "uwp/shapefile.hpp"

namespace uwp {

auto cascade_union(const std::vector<Polygon> &polygons)
    -> std::vector<Polygon>;

/// @brief Result of `select_overlap`: matched + orphan area polygons.
struct SelectOverlapResult {
    /// `(water_index, area_polygons)` pairs to be merged into the water
    /// polygons by `merge_overlapping`. Each water index appears at
    /// most once.
    std::vector<std::pair<size_t, std::vector<Polygon>>> matched;
    /// Standalone area polygons that don't intersect any coast polygon
    /// but are close enough (per `max_inland_km`) to be included as
    /// new patches alongside the coastline. Added as-is, no union.
    /// Empty when `max_inland_km <= 0`.
    std::vector<Polygon> orphans;
};

/// @brief Selects the area polygons that overlap or are close to the
///        water shapefile, splitting them into matched and orphan sets.
///
/// Iterates over the (smaller) area-polygon set and queries the water
/// shapefile's R-tree. For each area polygon:
///
///   * If it intersects a coast polygon (and isn't fully within it) →
///     matched: the polygon (clipped to the coast bbox + max_inland_km
///     buffer) is queued for union with that coast polygon.
///   * Else if `max_inland_km > 0` and at least one coast polygon is
///     within `max_inland_km` km of the area polygon → orphan: included
///     as a standalone patch (no union with any coast polygon).
///     Captures rivers, lagoons and delta channels that OSM's coastline
///     base doesn't connect to (e.g. inside the Lena delta) but that
///     are obviously coastal water.
///   * Else → ignored.
///
/// @param[in] water_shp The water shapefile (must have its R-tree built).
/// @param[in] area     The list of area polygons to test against the water
///                     shapefile.
/// @param[in] max_inland_km Distance threshold in kilometres:
///   * matched polygons are clipped to a buffer of this size around the
///     matched coast polygon's envelope;
///   * orphan polygons are included only if a coast polygon sits within
///     this distance.
///   0 disables both behaviours (no clipping, no orphan inclusion).
auto select_overlap(const Shapefile &water_shp,
                    const Shapefile::PolygonList &area,
                    double max_inland_km = 0.0)
    -> SelectOverlapResult;

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

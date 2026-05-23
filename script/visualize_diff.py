#!/usr/bin/env python3
"""Generate an HD image atlas of every polygon the uwp pipeline added to
the reference OSM coastline.

For SWOT calibration we need to QA the additions visually: for each
"delta" region (estuary, lagoon, missing piece), we render a high-DPI
PNG showing
  - the original OSM water polygons (blue outline),
  - the revised polygons after uwp (red outline),
  - the added area filled in yellow / orange,
  - optionally an OSM basemap underneath for geographic context.

Deltas are clustered geographically so an estuary split across several
base polygons becomes a single image (instead of N tiny ones).

Usage:

    python script/visualize_diff.py \\
        --reference data/water-polygons-split-4326/water_polygons.shp \\
        --revised data/corrected-water-polygons.shp \\
        --output data/diff-report \\
        --min-delta-km2 0.5 \\
        --basemap

The script writes:
  - <output>/png/cluster_NNNN_<lat>_<lon>.png  — one image per cluster
  - <output>/index.html                         — sortable HTML atlas
  - <output>/clusters.csv                       — cluster metadata
"""

from __future__ import annotations

import argparse
import contextlib
import html
import logging
import math
import pathlib
import sys
import warnings

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box
from shapely.ops import unary_union

try:
    import contextily as cx

    _HAVE_CONTEXTILY = True
except ImportError:
    _HAVE_CONTEXTILY = False

LOGGER = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).parent.parent
DATA_DIR = ROOT / 'data'

DEFAULT_REFERENCE = (
    DATA_DIR / 'water-polygons-split-4326' / 'water_polygons.shp'
)
DEFAULT_REVISED = DATA_DIR / 'corrected-water-polygons.shp'
DEFAULT_OUTPUT = DATA_DIR / 'diff-report'

# Approximate equirectangular conversion. Acceptable for area filtering and
# bbox padding — we never use it for storing geometry.
KM_PER_DEG_LAT = 111.0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _area_km2(geom, lat_mid: float) -> float:
    """Approximate area of a geometry in km² given its centroid latitude.

    Avoids the cost of projecting millions of polygons to an equal-area
    CRS just for filtering. Latitude-dependent cos(lat) factor on the
    longitude axis.
    """
    if geom.is_empty:
        return 0.0
    deg2_to_km2 = (KM_PER_DEG_LAT**2) * abs(math.cos(math.radians(lat_mid)))
    return geom.area * deg2_to_km2


def _km_to_deg(km: float, lat_mid: float) -> float:
    """Convert km to degrees at a given latitude (longitude-direction)."""
    cos_lat = max(0.01, abs(math.cos(math.radians(lat_mid))))
    return km / (KM_PER_DEG_LAT * cos_lat)


def _prefilter_by_area(
    deltas: gpd.GeoDataFrame, min_km2: float
) -> gpd.GeoDataFrame:
    """Drop delta pieces whose individual area is below `min_km2`.

    Run *before* clustering: buffer + unary_union scale with N², so
    pruning the long tail of micro-pieces (sub-km² artefacts from union
    rounding) before clustering is the single biggest perf lever.
    Pieces just below the per-cluster threshold can still cluster
    together if there are enough of them — that's why this prefilter
    threshold should be much smaller than --min-delta-km2.
    """
    if min_km2 <= 0 or deltas.empty:
        return deltas
    with _suppress_geographic_crs_warning():
        # Per-piece area approximation via shapely (cheap) + lat-aware
        # cos correction at the piece centroid latitude.
        centroids_y = deltas.geometry.centroid.y.values
    areas_deg2 = deltas.geometry.area.values
    km2 = (
        areas_deg2
        * (KM_PER_DEG_LAT**2)
        * np.abs(np.cos(np.radians(centroids_y)))
    )
    keep = km2 >= min_km2
    kept = deltas.loc[keep].reset_index(drop=True)
    LOGGER.info(
        'Pre-filter ≥ %g km²: kept %d / %d delta pieces',
        min_km2,
        len(kept),
        len(deltas),
    )
    return kept


@contextlib.contextmanager
def _suppress_geographic_crs_warning():
    """Silence geopandas' "Geometry is in a geographic CRS" warning.

    We intentionally do `centroid` / `buffer` on EPSG:4326 geometries for
    cheap clustering. The geometric error this introduces is irrelevant
    for grouping pieces by proximity — we only need the partition to be
    stable, not metrically exact. Wrap the relevant calls in this CM so
    the warning doesn't drown the script's actual log lines.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            'ignore',
            message='Geometry is in a geographic CRS',
            category=UserWarning,
        )
        yield


# ---------------------------------------------------------------------------
# Delta detection
# ---------------------------------------------------------------------------


def find_deltas(
    reference: gpd.GeoDataFrame, revised: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Return a GeoDataFrame of every polygon piece added by uwp.

    Pairing strategy: uwp mutates polygons in place, so for index `i`
    common to both files the addition is `revised[i] - reference[i]`.
    New polygons appended at the end of the revised file (the
    `extra_polygons` from `merge_overlapping`) have no counterpart and are
    treated as fully new.
    """
    n_ref = len(reference)
    n_rev = len(revised)
    LOGGER.info(
        'Reference: %d polygons | Revised: %d polygons '
        '(=> %d common, %d appended)',
        n_ref,
        n_rev,
        min(n_ref, n_rev),
        max(0, n_rev - n_ref),
    )

    ref_geoms = reference.geometry.values
    rev_geoms = revised.geometry.values
    common = min(n_ref, n_rev)

    # Step 1: cheap envelope test, vectorised in numpy. Catches the
    # overwhelming majority of records since the diff only touches a few
    # thousand polygons out of ~50-100k.
    ref_bounds = reference.bounds.values
    rev_bounds = revised.bounds.values
    changed_mask = np.any(ref_bounds[:common] != rev_bounds[:common], axis=1)
    changed_fids = np.flatnonzero(changed_mask)
    LOGGER.info(
        'Envelope-changed candidates: %d / %d', len(changed_fids), common
    )

    # Step 2: batch-compute the geometric differences with shapely 2.x's
    # vectorised API. One call into GEOS for the lot rather than ~40k
    # per-polygon Python round-trips. Drops this step from minutes to
    # seconds on large diffs.
    if len(changed_fids) > 0:
        deltas_arr = shapely.difference(
            rev_geoms[changed_fids], ref_geoms[changed_fids]
        )
        non_empty = ~shapely.is_empty(deltas_arr)
        kept_fids = changed_fids[non_empty]
        kept_geoms = deltas_arr[non_empty]
    else:
        kept_fids = np.empty(0, dtype=int)
        kept_geoms = np.empty(0, dtype=object)
    LOGGER.info('Modified deltas with non-empty geometry: %d', len(kept_fids))

    rows: list[dict] = [
        {'fid': int(fid), 'kind': 'modified', 'geometry': geom}
        for fid, geom in zip(kept_fids, kept_geoms, strict=True)
    ]

    # Polygons appended at the end of the revised file.
    rows.extend(
        {'fid': fid, 'kind': 'appended', 'geometry': rev_geoms[fid]}
        for fid in range(common, n_rev)
    )

    if not rows:
        return gpd.GeoDataFrame(
            columns=['fid', 'kind', 'geometry'], crs=revised.crs
        )
    return gpd.GeoDataFrame(rows, crs=revised.crs)


# ---------------------------------------------------------------------------
# Geographic clustering
# ---------------------------------------------------------------------------


def cluster_deltas(
    deltas: gpd.GeoDataFrame, buffer_km: float
) -> gpd.GeoDataFrame:
    """Assign a cluster id so adjacent delta pieces (within `buffer_km` of
    each other) end up in the same image.

    Implementation: buffer everything by `buffer_km/2` so neighbours
    overlap, union the lot, then take the connected components. Each
    delta's cluster id is the index of the component it belongs to.
    """
    if deltas.empty:
        deltas = deltas.copy()
        deltas['cluster_id'] = []
        return deltas

    # All the centroid/buffer calls below run on EPSG:4326 geometries on
    # purpose — see `_suppress_geographic_crs_warning` for the rationale.
    with _suppress_geographic_crs_warning():
        # Use the global centroid latitude for the buffer-radius conversion.
        # Good enough — we only need clusters, not metric exactness.
        centroid_lats = deltas.geometry.centroid.y
        lat_mid = float(centroid_lats.mean())
        buffer_deg = _km_to_deg(buffer_km / 2, lat_mid)

        buffered = deltas.geometry.buffer(buffer_deg)
        dissolved = unary_union(buffered.values)
        components = (
            [dissolved]
            if dissolved.geom_type == 'Polygon'
            else list(dissolved.geoms)
        )

        cluster_gdf = gpd.GeoDataFrame(
            {'cluster_id': range(len(components))},
            geometry=components,
            crs=deltas.crs,
        )

        # Spatial join: each delta picks up the cluster_id of the component
        # that contains its centroid.
        centroids = gpd.GeoDataFrame(
            deltas.drop(columns='geometry'),
            geometry=deltas.geometry.centroid,
            crs=deltas.crs,
        )
        joined = gpd.sjoin(
            centroids, cluster_gdf, how='left', predicate='within'
        )
    deltas = deltas.copy()
    deltas['cluster_id'] = joined['cluster_id'].astype('Int64').values
    return deltas


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_cluster(
    cluster_id: int,
    cluster_deltas_gdf: gpd.GeoDataFrame,
    reference: gpd.GeoDataFrame,
    revised: gpd.GeoDataFrame,
    output_path: pathlib.Path,
    dpi: int,
    with_basemap: bool,
) -> dict:
    """Render one cluster as a PNG and return metadata about it."""
    bounds = cluster_deltas_gdf.total_bounds
    minx, miny, maxx, maxy = bounds
    pad_x = max(0.005, (maxx - minx) * 0.25)
    pad_y = max(0.005, (maxy - miny) * 0.25)

    extent_minx = minx - pad_x
    extent_miny = max(-90.0, miny - pad_y)
    extent_maxx = maxx + pad_x
    extent_maxy = min(90.0, maxy + pad_y)

    # Clip context layers to the extent (uses geopandas' spatial index).
    ref_local = reference.cx[extent_minx:extent_maxx, extent_miny:extent_maxy]
    rev_local = revised.cx[extent_minx:extent_maxx, extent_miny:extent_maxy]

    fig, ax = plt.subplots(figsize=(10, 10), dpi=dpi)

    if with_basemap and _HAVE_CONTEXTILY:
        target_crs = 3857  # Web Mercator — required by contextily
        ref_3857 = ref_local.to_crs(target_crs)
        rev_3857 = rev_local.to_crs(target_crs)
        delta_3857 = cluster_deltas_gdf.to_crs(target_crs)

        ref_3857.boundary.plot(
            ax=ax, edgecolor='#1f77b4', linewidth=0.6, alpha=0.8
        )
        rev_3857.boundary.plot(
            ax=ax, edgecolor='#d62728', linewidth=0.9, alpha=0.9
        )
        delta_3857.plot(
            ax=ax,
            color='#ffdd44',
            alpha=0.55,
            edgecolor='#ff8c00',
        )

        extent_box_3857 = (
            gpd.GeoSeries(
                [
                    box(
                        extent_minx,
                        extent_miny,
                        extent_maxx,
                        extent_maxy,
                    )
                ],
                crs=4326,
            )
            .to_crs(target_crs)
            .total_bounds
        )
        ax.set_xlim(extent_box_3857[0], extent_box_3857[2])
        ax.set_ylim(extent_box_3857[1], extent_box_3857[3])
        ax.set_aspect('equal')
        try:
            cx.add_basemap(
                ax,
                source=cx.providers.OpenStreetMap.Mapnik,
                attribution_size=5,
            )
        except Exception as exc:  # network / tile errors are not fatal
            LOGGER.warning(
                'Basemap fetch failed for cluster %d: %s', cluster_id, exc
            )
        ax.set_xticks([])
        ax.set_yticks([])
    else:
        ref_local.boundary.plot(
            ax=ax,
            edgecolor='#1f77b4',
            linewidth=0.6,
            label='reference',
            alpha=0.8,
        )
        rev_local.boundary.plot(
            ax=ax,
            edgecolor='#d62728',
            linewidth=0.9,
            label='revised',
            alpha=0.9,
        )
        cluster_deltas_gdf.plot(
            ax=ax,
            color='#ffdd44',
            edgecolor='#ff8c00',
            alpha=0.55,
            label='added',
        )
        ax.set_xlim(extent_minx, extent_maxx)
        ax.set_ylim(extent_miny, extent_maxy)
        ax.set_aspect('equal')
        ax.legend(loc='best', framealpha=0.85)
        ax.set_xlabel('Longitude')
        ax.set_ylabel('Latitude')
        ax.grid(True, linestyle=':', alpha=0.4)

    center_lon = (minx + maxx) / 2
    center_lat = (miny + maxy) / 2
    union_geom = unary_union(cluster_deltas_gdf.geometry.values)
    total_km2 = _area_km2(union_geom, center_lat)

    fig.suptitle(
        f'Cluster #{cluster_id} — {center_lat:+.4f}°, {center_lon:+.4f}° '
        f'— added: {total_km2:.3f} km² '
        f'({len(cluster_deltas_gdf)} piece'
        f'{"s" if len(cluster_deltas_gdf) > 1 else ""})',
        fontsize=12,
    )

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches='tight')
    plt.close(fig)

    return {
        'cluster_id': cluster_id,
        'center_lat': center_lat,
        'center_lon': center_lon,
        'added_km2': total_km2,
        'num_pieces': len(cluster_deltas_gdf),
        'fids': ','.join(str(int(f)) for f in cluster_deltas_gdf['fid']),
        'image': output_path.name,
    }


# ---------------------------------------------------------------------------
# Index file
# ---------------------------------------------------------------------------


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>UWP diff atlas</title>
<style>
  body  {{ font-family: system-ui, sans-serif; margin: 2rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
  th {{ background: #f4f4f4; cursor: pointer; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  img {{ max-height: 60px; }}
  a {{ color: #0645ad; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>UWP diff atlas</h1>
<p>{summary}</p>
<table id="atlas">
<thead><tr>
  <th>#</th><th>Preview</th><th>Center</th>
  <th>Added (km²)</th><th>Pieces</th><th>FIDs</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""


def _write_index(
    output_dir: pathlib.Path, records: list[dict], summary: str
) -> None:
    records_sorted = sorted(records, key=lambda r: -r['added_km2'])

    # CSV (machine-readable)
    csv_path = output_dir / 'clusters.csv'
    pd.DataFrame(records_sorted).to_csv(csv_path, index=False)
    LOGGER.info('Wrote %s', csv_path)

    # HTML
    rows_html = []
    for rec in records_sorted:
        img_rel = f'png/{rec["image"]}'
        rows_html.append(
            '<tr>'
            f'<td>{rec["cluster_id"]}</td>'
            f'<td><a href="{html.escape(img_rel)}">'
            f'<img src="{html.escape(img_rel)}" loading="lazy"></a></td>'
            f'<td>{rec["center_lat"]:+.4f}, {rec["center_lon"]:+.4f}</td>'
            f'<td>{rec["added_km2"]:.3f}</td>'
            f'<td>{rec["num_pieces"]}</td>'
            f'<td><code>{html.escape(rec["fids"])}</code></td>'
            '</tr>'
        )
    html_path = output_dir / 'index.html'
    html_path.write_text(
        _HTML_TEMPLATE.format(summary=summary, rows='\n'.join(rows_html))
    )
    LOGGER.info('Wrote %s', html_path)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--reference',
        type=pathlib.Path,
        default=DEFAULT_REFERENCE,
        help='Reference shapefile (OSM water polygons, pre-uwp).',
    )
    parser.add_argument(
        '--revised',
        type=pathlib.Path,
        default=DEFAULT_REVISED,
        help='Revised shapefile (uwp output).',
    )
    parser.add_argument(
        '--output',
        type=pathlib.Path,
        default=DEFAULT_OUTPUT,
        help='Output directory for PNGs and index.',
    )
    parser.add_argument(
        '--min-delta-km2',
        type=float,
        default=1.0,
        help='Skip clusters whose total added area is below this threshold. '
        'Filters out micro-additions that would clutter the atlas. The '
        'previous default (0.1) was too noisy on global datasets.',
    )
    parser.add_argument(
        '--cluster-buffer-km',
        type=float,
        default=5.0,
        help='Maximum distance between delta pieces for them to be grouped '
        'into the same image. Larger = fewer, bigger images.',
    )
    parser.add_argument(
        '--prefilter-km2',
        type=float,
        default=None,
        help='Drop individual delta pieces smaller than this BEFORE the '
        'expensive clustering step. Massive speed-up on global diffs '
        '(40k+ pieces → a few hundred). Default: --min-delta-km2 / 20, '
        'so 20 sub-threshold pieces could still cluster together. Set to '
        '0 to disable.',
    )
    parser.add_argument(
        '--dpi',
        type=int,
        default=200,
        help='Output PNG resolution (DPI). 200 = ~1600x1600 px crisp.',
    )
    parser.add_argument(
        '--basemap',
        action='store_true',
        help='Add an OSM basemap underneath (requires contextily and a '
        'network connection to tile.openstreetmap.org).',
    )
    parser.add_argument(
        '--max-images',
        type=int,
        default=200,
        help='Cap the number of images rendered (0 = unlimited). The default '
        'gives a manageable atlas; raise it if you need exhaustive QA. '
        'Clusters are ranked by area so the most impactful ones are kept.',
    )
    parser.add_argument(
        '--bbox',
        type=float,
        nargs=4,
        metavar=('MINLON', 'MINLAT', 'MAXLON', 'MAXLAT'),
        default=None,
        help='Restrict the diff to this geographic window. Speeds up '
        'inspection of a single region.',
    )
    return parser.parse_args()


def _load_clipped(path: pathlib.Path, bbox) -> gpd.GeoDataFrame:
    LOGGER.info('Reading %s', path)
    if bbox is not None:
        return gpd.read_file(path, bbox=tuple(bbox))
    return gpd.read_file(path)


def _validate_args(args: argparse.Namespace) -> None:
    """Sanity-check CLI args; abort the process on hard errors."""
    if args.basemap and not _HAVE_CONTEXTILY:
        LOGGER.error(
            '--basemap requested but contextily is not installed. '
            'Either drop --basemap or `mamba install contextily`.'
        )
        sys.exit(1)
    for label, path in (
        ('Reference', args.reference),
        ('Revised', args.revised),
    ):
        if not path.exists():
            LOGGER.error('%s not found: %s', label, path)
            sys.exit(1)


def _load_inputs(
    args: argparse.Namespace,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Load both shapefiles, applying the optional --bbox clip and
    defaulting the CRS to EPSG:4326 when absent."""
    reference = _load_clipped(args.reference, args.bbox)
    revised = _load_clipped(args.revised, args.bbox)

    if reference.crs is None or revised.crs is None:
        LOGGER.warning(
            'Missing CRS — assuming EPSG:4326. Set a .prj file to silence '
            'this warning.'
        )
        reference = reference.set_crs(4326, allow_override=True)
        revised = revised.set_crs(4326, allow_override=True)
    return reference, revised


def _rank_clusters(
    clustered: gpd.GeoDataFrame, min_delta_km2: float
) -> list[tuple[int, float, gpd.GeoDataFrame, object]]:
    """Compute (cluster_id, area_km2, pieces, union) for every cluster,
    drop those below the threshold, and return them sorted by area
    descending. Doing this *before* the render loop lets us cap the work
    at --max-images by area rather than by encounter order — biggest
    additions get rendered first, the rest are skipped."""
    cluster_count = int(clustered['cluster_id'].max()) + 1
    candidates: list[tuple[int, float, gpd.GeoDataFrame, object]] = []
    for cluster_id in range(cluster_count):
        pieces = clustered[clustered['cluster_id'] == cluster_id]
        if pieces.empty:
            continue
        with _suppress_geographic_crs_warning():
            lat_mid = float(pieces.geometry.centroid.y.mean())
        union = unary_union(pieces.geometry.values)
        area = _area_km2(union, lat_mid)
        if area < min_delta_km2:
            continue
        candidates.append((cluster_id, area, pieces, union))
    candidates.sort(key=lambda x: -x[1])
    return candidates


def _render_all_clusters(
    clustered: gpd.GeoDataFrame,
    reference: gpd.GeoDataFrame,
    revised: gpd.GeoDataFrame,
    args: argparse.Namespace,
) -> list[dict]:
    """Rank clusters by area, render the top-N as PNGs, and return the
    metadata records for the index file."""
    rendered: list[dict] = []
    png_dir = args.output / 'png'

    candidates = _rank_clusters(clustered, args.min_delta_km2)
    LOGGER.info(
        '%d cluster(s) above %g km² threshold (of %d total)',
        len(candidates),
        args.min_delta_km2,
        int(clustered['cluster_id'].max()) + 1,
    )
    if args.max_images and len(candidates) > args.max_images:
        LOGGER.info(
            'Rendering top %d by area (--max-images=%d). %d smaller '
            'clusters skipped — raise --max-images or lower '
            '--min-delta-km2 to see them.',
            args.max_images,
            args.max_images,
            len(candidates) - args.max_images,
        )
        candidates = candidates[: args.max_images]

    for cluster_id, _area, pieces, union in candidates:
        # File name encodes location so it sorts geographically.
        centroid = union.centroid
        name = (
            f'cluster_{cluster_id:05d}_'
            f'{centroid.y:+09.4f}_{centroid.x:+010.4f}.png'
        )

        try:
            rec = _render_cluster(
                cluster_id,
                pieces,
                reference,
                revised,
                png_dir / name,
                dpi=args.dpi,
                with_basemap=args.basemap,
            )
            rendered.append(rec)
            if len(rendered) % 25 == 0:
                LOGGER.info(
                    'Rendered %d / %d clusters…',
                    len(rendered),
                    len(candidates),
                )
        except Exception:
            LOGGER.exception(
                'Failed rendering cluster %d, skipping', cluster_id
            )
    return rendered


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
    )
    args = _parse_args()
    _validate_args(args)

    reference, revised = _load_inputs(args)

    LOGGER.info('Computing deltas…')
    deltas = find_deltas(reference, revised)
    if deltas.empty:
        LOGGER.info('No deltas detected — nothing to render.')
        return

    # Pre-filter tiny pieces before the (expensive) clustering pass. Pieces
    # 20x smaller than the cluster threshold still leave room to aggregate
    # into a visible group, but the micro-artefacts (sub-km2 numerical
    # leftovers from union) are dropped on the spot.
    prefilter_km2 = (
        args.prefilter_km2
        if args.prefilter_km2 is not None
        else args.min_delta_km2 / 20.0
    )
    deltas = _prefilter_by_area(deltas, prefilter_km2)
    if deltas.empty:
        LOGGER.info('No deltas survived the pre-filter — nothing to render.')
        return

    LOGGER.info('Clustering %d delta pieces…', len(deltas))
    clustered = cluster_deltas(deltas, args.cluster_buffer_km)

    rendered = _render_all_clusters(clustered, reference, revised, args)
    if not rendered:
        LOGGER.warning(
            'No cluster met the --min-delta-km2=%g threshold.',
            args.min_delta_km2,
        )
        return

    total_km2 = sum(r['added_km2'] for r in rendered)
    summary = (
        f'{len(rendered)} cluster(s) rendered '
        f'(total added: {total_km2:.2f} km²). '
        f'Source: <code>{html.escape(str(args.revised))}</code> vs '
        f'<code>{html.escape(str(args.reference))}</code>.'
    )
    _write_index(args.output, rendered, summary)
    LOGGER.info('Done. %d image(s) written to %s', len(rendered), args.output)


if __name__ == '__main__':
    main()

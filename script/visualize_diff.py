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
import concurrent.futures
import contextlib
import html
import logging
import math
import multiprocessing
import os
import pathlib
import sys
import warnings

import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box
from shapely.ops import unary_union

# Workers render PNGs without an X server. Force the non-interactive backend
# at import time so this works both in `main()` (single-process) and in
# spawned multiprocessing workers.
matplotlib.use('Agg')

try:
    import contextily as cx

    _HAVE_CONTEXTILY = True
except ImportError:
    _HAVE_CONTEXTILY = False

# Default basemap provider. We intentionally do NOT default to
# `OpenStreetMap.Mapnik`: the OSM tile server explicitly forbids scripted /
# automated requests and rate-limits aggressively (HTTP 429 after a handful
# of concurrent fetches). CartoDB Positron is built on the same data, has
# the same coverage, looks comparable (cleaner even, neutral palette better
# suited as a backdrop for overlays), and the CartoDB usage policy permits
# automated use as long as it stays reasonable. Override with
# `--basemap-provider` if you need a different look.
_DEFAULT_BASEMAP_PROVIDER = 'CartoDB.Positron'

# Catalogue of providers reachable via dotted names (e.g. "CartoDB.Positron"
# → cx.providers.CartoDB.Positron). Resolved at call-time so a missing
# provider in older contextily versions fails with a clear error rather
# than at import.
_BASEMAP_CHOICES = (
    'CartoDB.Positron',
    'CartoDB.Voyager',
    'CartoDB.DarkMatter',
    'OpenStreetMap.Mapnik',
    'OpenStreetMap.HOT',
    'Esri.WorldImagery',
    'Esri.WorldTopoMap',
)

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

# Geohash base32 alphabet (no a, i, l, o). Each 1-char geohash cell encodes
# 3 longitude bits (8 cells, 45° wide) and 2 latitude bits (4 cells, 45°
# tall) → 32 cells covering the globe. Used to shard the diff computation
# spatially so workers can process disjoint regions in parallel and only
# load the polygons intersecting their cell (cheap with pyogrio's bbox
# filter).
_GEOHASH_BASE32 = '0123456789bcdefghjkmnpqrstuvwxyz'


def _geohash1_bbox(char: str) -> tuple[float, float, float, float]:
    """Return (minlon, minlat, maxlon, maxlat) for a 1-char geohash cell.

    Bit interleaving: the 5 bits of a 1-char geohash are split lon, lat,
    lon, lat, lon (most significant first). Decoding gives 3 lon bits
    (8 cells) and 2 lat bits (4 cells).
    """
    idx = _GEOHASH_BASE32.index(char.lower())
    bits = [(idx >> i) & 1 for i in range(4, -1, -1)]
    lon_idx = (bits[0] << 2) | (bits[2] << 1) | bits[4]
    lat_idx = (bits[1] << 1) | bits[3]
    lon_step = 360.0 / 8
    lat_step = 180.0 / 4
    minlon = -180.0 + lon_idx * lon_step
    minlat = -90.0 + lat_idx * lat_step
    return (minlon, minlat, minlon + lon_step, minlat + lat_step)


def _intersect_bboxes(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    """Intersection of two (minlon, minlat, maxlon, maxlat) tuples, or
    None if disjoint."""
    minlon = max(a[0], b[0])
    minlat = max(a[1], b[1])
    maxlon = min(a[2], b[2])
    maxlat = min(a[3], b[3])
    if minlon >= maxlon or minlat >= maxlat:
        return None
    return (minlon, minlat, maxlon, maxlat)


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


def _resolve_basemap_provider(name: str):
    """Resolve a dotted provider name (e.g. 'CartoDB.Positron') to the
    contextily provider object. Raises ValueError on unknown name so the
    user gets a clear error instead of a cryptic AttributeError mid-render.
    """
    if not _HAVE_CONTEXTILY:
        raise RuntimeError('contextily is not installed.')
    obj = cx.providers
    for part in name.split('.'):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise ValueError(
                f'Unknown basemap provider {name!r}. '
                f'Try one of: {", ".join(_BASEMAP_CHOICES)}'
            ) from exc
    return obj


def _configure_basemap_cache(cache_dir: pathlib.Path) -> None:
    """Point contextily at a shared on-disk cache so workers running in
    parallel don't re-download the same tiles. With 32 workers hitting the
    same provider, sharing a cache is what keeps us under any reasonable
    rate limit on subsequent runs."""
    if not _HAVE_CONTEXTILY:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    # contextily exposes `set_cache_dir` since 1.3. Older versions read
    # the CONTEXTILY_CACHE env var.
    if hasattr(cx, 'set_cache_dir'):
        cx.set_cache_dir(str(cache_dir))
    else:
        os.environ['CONTEXTILY_CACHE'] = str(cache_dir)


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


def _render_with_basemap(
    ax,
    ref_local: gpd.GeoDataFrame,
    rev_local: gpd.GeoDataFrame,
    cluster_deltas_gdf: gpd.GeoDataFrame,
    extent_4326: tuple[float, float, float, float],
    cluster_id: int,
    basemap_provider: str,
) -> None:
    """Plot the reference/revised/delta layers in Web Mercator and overlay
    a tile basemap. Basemap fetch is best-effort: tile servers occasionally
    rate-limit (HTTP 429) or drop connections under concurrent load. We
    don't want one failed cluster to abort the atlas — log and continue
    with a blank background, the overlay is still meaningful on its own.
    """
    target_crs = 3857  # Web Mercator — required by contextily
    ref_3857 = ref_local.to_crs(target_crs)
    rev_3857 = rev_local.to_crs(target_crs)
    delta_3857 = cluster_deltas_gdf.to_crs(target_crs)

    ref_3857.boundary.plot(ax=ax, edgecolor='#1f77b4', linewidth=0.6, alpha=0.8)
    rev_3857.boundary.plot(ax=ax, edgecolor='#d62728', linewidth=0.9, alpha=0.9)
    delta_3857.plot(ax=ax, color='#ffdd44', alpha=0.55, edgecolor='#ff8c00')

    minx, miny, maxx, maxy = extent_4326
    extent_box_3857 = (
        gpd.GeoSeries([box(minx, miny, maxx, maxy)], crs=4326)
        .to_crs(target_crs)
        .total_bounds
    )
    ax.set_xlim(extent_box_3857[0], extent_box_3857[2])
    ax.set_ylim(extent_box_3857[1], extent_box_3857[3])
    ax.set_aspect('equal')
    try:
        provider = _resolve_basemap_provider(basemap_provider)
        cx.add_basemap(ax, source=provider, attribution_size=5)
    except Exception as exc:
        LOGGER.warning(
            'Basemap fetch failed for cluster %d (provider=%s): %s',
            cluster_id,
            basemap_provider,
            exc,
        )
    ax.set_xticks([])
    ax.set_yticks([])


def _render_cluster(
    cluster_id: int,
    cluster_deltas_gdf: gpd.GeoDataFrame,
    reference: gpd.GeoDataFrame,
    revised: gpd.GeoDataFrame,
    output_path: pathlib.Path,
    dpi: int,
    with_basemap: bool,
    basemap_provider: str = _DEFAULT_BASEMAP_PROVIDER,
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
        _render_with_basemap(
            ax,
            ref_local,
            rev_local,
            cluster_deltas_gdf,
            (extent_minx, extent_miny, extent_maxx, extent_maxy),
            cluster_id,
            basemap_provider,
        )
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
  <th>#</th><th>Cell</th><th>Preview</th><th>Center</th>
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
            f'<td><code>{html.escape(rec.get("cell", "?"))}</code></td>'
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
        help='Add a basemap underneath each PNG (requires contextily and '
        'network access to the tile provider).',
    )
    parser.add_argument(
        '--basemap-provider',
        default=_DEFAULT_BASEMAP_PROVIDER,
        choices=_BASEMAP_CHOICES,
        help='Tile provider for --basemap. Default is CartoDB.Positron '
        '(scripted-use friendly, neutral palette). Avoid '
        'OpenStreetMap.Mapnik for parallel runs: tile.openstreetmap.org '
        'rate-limits aggressively and the OSM usage policy forbids '
        'scripted bulk fetches.',
    )
    parser.add_argument(
        '--basemap-cache-dir',
        type=pathlib.Path,
        default=pathlib.Path.home() / '.cache' / 'uwp-basemap-tiles',
        help='Directory shared across workers to cache fetched basemap '
        'tiles. Sharing a single cache avoids each worker independently '
        're-downloading the same tiles — essential to stay under any tile '
        'provider rate limit.',
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
    parser.add_argument(
        '--parallel-cells',
        type=int,
        default=0,
        help='Number of geohash-1 cells to process in parallel via '
        'ProcessPoolExecutor. 0 = auto (cpu_count // 2). 1 = sequential '
        '(useful for debugging). Each worker only loads the shapefile '
        'slice intersecting its cell, so RAM usage stays bounded.',
    )
    return parser.parse_args()


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


def _process_cell(
    cell_id: str,
    cell_bbox: tuple[float, float, float, float],
    reference_path: pathlib.Path,
    revised_path: pathlib.Path,
    output_dir: pathlib.Path,
    min_delta_km2: float,
    cluster_buffer_km: float,
    prefilter_km2: float,
    dpi: int,
    with_basemap: bool,
    max_images: int,
    basemap_provider: str = _DEFAULT_BASEMAP_PROVIDER,
    basemap_cache_dir: pathlib.Path | None = None,
) -> list[dict]:
    """Worker: process one geohash-1 cell end-to-end.

    Top-level (not a closure) so ProcessPoolExecutor can pickle it. Reads
    only the polygons intersecting `cell_bbox` thanks to pyogrio's bbox
    filter — each worker loads a small slice of the global shapefile
    instead of the whole thing. Returns the list of rendered records
    (each tagged with `cell`).
    """
    # ProcessPoolExecutor workers don't inherit the parent's logging setup
    # when the start method is 'spawn' (macOS / Windows / 3.14+). Configure
    # locally so log lines appear with their cell prefix.
    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s - [cell {cell_id}] %(levelname)s - %(message)s',
        force=True,
    )

    # Point contextily at the shared on-disk tile cache. Critical when
    # running many workers in parallel: without it each worker would
    # download the same tiles independently, triggering rate limits.
    if with_basemap and basemap_cache_dir is not None:
        _configure_basemap_cache(basemap_cache_dir)

    reference = gpd.read_file(reference_path, bbox=cell_bbox)
    revised = gpd.read_file(revised_path, bbox=cell_bbox)
    if reference.empty and revised.empty:
        return []
    if reference.crs is None:
        reference = reference.set_crs(4326, allow_override=True)
    if revised.crs is None:
        revised = revised.set_crs(4326, allow_override=True)

    deltas = find_deltas(reference, revised)
    if deltas.empty:
        return []

    deltas = _prefilter_by_area(deltas, prefilter_km2)
    if deltas.empty:
        return []

    clustered = cluster_deltas(deltas, cluster_buffer_km)
    candidates = _rank_clusters(clustered, min_delta_km2)
    if max_images and len(candidates) > max_images:
        candidates = candidates[:max_images]

    rendered: list[dict] = []
    png_dir = output_dir / 'png'
    for cluster_id, _area, pieces, union in candidates:
        centroid = union.centroid
        # Cell prefix on the filename so we can spot the spatial origin at
        # a glance and avoid name collisions across cells.
        name = (
            f'cell_{cell_id}_cluster_{cluster_id:05d}_'
            f'{centroid.y:+09.4f}_{centroid.x:+010.4f}.png'
        )
        try:
            rec = _render_cluster(
                cluster_id,
                pieces,
                reference,
                revised,
                png_dir / name,
                dpi=dpi,
                with_basemap=with_basemap,
                basemap_provider=basemap_provider,
            )
            rec['cell'] = cell_id
            rendered.append(rec)
        except Exception:
            LOGGER.exception(
                'Failed rendering cell %s cluster %d', cell_id, cluster_id
            )
    LOGGER.info('Cell %s done: %d image(s)', cell_id, len(rendered))
    return rendered


def _select_cells(
    user_bbox: tuple[float, float, float, float] | None,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Return the list of (cell_id, cell_bbox) to dispatch work to,
    restricted to the cells intersecting --bbox when given."""
    cells: list[tuple[str, tuple[float, float, float, float]]] = []
    for ch in _GEOHASH_BASE32:
        bbox = _geohash1_bbox(ch)
        if user_bbox is not None:
            bbox = _intersect_bboxes(bbox, user_bbox)
            if bbox is None:
                continue
        cells.append((ch, bbox))
    return cells


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        force=True,
    )
    args = _parse_args()
    _validate_args(args)

    # Output directory shared by all workers.
    (args.output / 'png').mkdir(parents=True, exist_ok=True)

    prefilter_km2 = (
        args.prefilter_km2
        if args.prefilter_km2 is not None
        else args.min_delta_km2 / 20.0
    )

    user_bbox = tuple(args.bbox) if args.bbox else None
    cells = _select_cells(user_bbox)

    # Auto-pick a parallelism level: half the cores by default. Each worker
    # spawns matplotlib + reads shapefiles, so over-subscribing hurts more
    # than it helps. Capped at len(cells) — no point spawning idle workers.
    parallel = args.parallel_cells
    if parallel <= 0:
        parallel = max(1, (os.cpu_count() or 2) // 2)
    parallel = min(parallel, len(cells))

    LOGGER.info(
        'Processing %d geohash-1 cell(s) with %d worker(s) '
        '(prefilter=%g km², min_delta=%g km², max_images/cell=%d)',
        len(cells),
        parallel,
        prefilter_km2,
        args.min_delta_km2,
        args.max_images,
    )

    # Make sure the basemap cache dir exists *before* spawning workers, so
    # they don't race on `mkdir`. Touched by every worker via
    # `_configure_basemap_cache`.
    if args.basemap:
        args.basemap_cache_dir.mkdir(parents=True, exist_ok=True)
        # Configure for the parent process too, in case --parallel-cells=1.
        _configure_basemap_cache(args.basemap_cache_dir)

    worker_kwargs = {
        'reference_path': args.reference,
        'revised_path': args.revised,
        'output_dir': args.output,
        'min_delta_km2': args.min_delta_km2,
        'cluster_buffer_km': args.cluster_buffer_km,
        'prefilter_km2': prefilter_km2,
        'dpi': args.dpi,
        'with_basemap': args.basemap,
        'max_images': args.max_images,
        'basemap_provider': args.basemap_provider,
        'basemap_cache_dir': args.basemap_cache_dir,
    }

    all_records: list[dict] = []
    if parallel == 1:
        # Sequential fallback (useful for debugging / profiling).
        for cell_id, cell_bbox in cells:
            try:
                all_records.extend(
                    _process_cell(cell_id, cell_bbox, **worker_kwargs)
                )
            except Exception:
                LOGGER.exception('Cell %s failed', cell_id)
    else:
        # 'spawn' start method avoids the pitfalls of forking after
        # matplotlib / GEOS have initialised threads in the parent.
        ctx = multiprocessing.get_context('spawn')
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=parallel, mp_context=ctx
        ) as pool:
            futures = {
                pool.submit(
                    _process_cell, cell_id, cell_bbox, **worker_kwargs
                ): cell_id
                for cell_id, cell_bbox in cells
            }
            for fut in concurrent.futures.as_completed(futures):
                cell_id = futures[fut]
                try:
                    all_records.extend(fut.result())
                except Exception:
                    LOGGER.exception('Cell %s failed', cell_id)

    if not all_records:
        LOGGER.warning(
            'No cluster met the --min-delta-km2=%g threshold across any cell.',
            args.min_delta_km2,
        )
        return

    total_km2 = sum(r['added_km2'] for r in all_records)
    summary = (
        f'{len(all_records)} cluster(s) rendered across '
        f'{len(cells)} geohash-1 cell(s) '
        f'(total added: {total_km2:.2f} km²). '
        f'Source: <code>{html.escape(str(args.revised))}</code> vs '
        f'<code>{html.escape(str(args.reference))}</code>.'
    )
    _write_index(args.output, all_records, summary)
    LOGGER.info(
        'Done. %d image(s) written to %s', len(all_records), args.output
    )


if __name__ == '__main__':
    main()

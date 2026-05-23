"""Shared helpers for the UWP visualisation scripts.

Two scripts use this module:

- `visualize_patches.py` (recommended): reads the patches shapefile written
  by the uwp binary's --patches-output flag. Fast, ground-truth.
- `visualize_diff.py`: full geometric diff between reference and revised
  shapefiles. Slow, kept for regression / audit use cases.

Both share the same downstream pipeline: pre-filter by area → spatial
cluster by geohash-1 → render HD PNGs in parallel → write an HTML / CSV
atlas. Those steps live here so the two scripts only differ in how they
acquire the initial set of "what was added" polygons.
"""

from __future__ import annotations

import argparse  # noqa: TC003  — used in public signatures, kept at runtime
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
from shapely.geometry import box
from shapely.ops import unary_union

# Force the non-interactive backend at import so workers spawned by
# ProcessPoolExecutor don't try to open an X display.
matplotlib.use('Agg')

try:
    import contextily as cx

    _HAVE_CONTEXTILY = True
except ImportError:
    _HAVE_CONTEXTILY = False


LOGGER = logging.getLogger(__name__)

# Approximate equirectangular conversion. Acceptable for area filtering
# and bbox padding — we never use it for storing geometry.
KM_PER_DEG_LAT = 111.0

_DEFAULT_BASEMAP_PROVIDER = 'CartoDB.Positron'

_BASEMAP_CHOICES = (
    'CartoDB.Positron',
    'CartoDB.Voyager',
    'CartoDB.DarkMatter',
    'OpenStreetMap.Mapnik',
    'OpenStreetMap.HOT',
    'Esri.WorldImagery',
    'Esri.WorldTopoMap',
)

# Geohash base32 alphabet (no a, i, l, o). 1-char geohash → 32 cells of
# 45deg by 45deg covering the globe. See `_geohash1_bbox`.
_GEOHASH_BASE32 = '0123456789bcdefghjkmnpqrstuvwxyz'


# ---------------------------------------------------------------------------
# Geohash helpers
# ---------------------------------------------------------------------------


def geohash1_bbox(char: str) -> tuple[float, float, float, float]:
    """(minlon, minlat, maxlon, maxlat) for a 1-char geohash cell.

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


def intersect_bboxes(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    minlon = max(a[0], b[0])
    minlat = max(a[1], b[1])
    maxlon = min(a[2], b[2])
    maxlat = min(a[3], b[3])
    if minlon >= maxlon or minlat >= maxlat:
        return None
    return (minlon, minlat, maxlon, maxlat)


def select_cells(
    user_bbox: tuple[float, float, float, float] | None,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Return the (cell_id, cell_bbox) list, restricted to cells
    intersecting `user_bbox` when given."""
    cells: list[tuple[str, tuple[float, float, float, float]]] = []
    for ch in _GEOHASH_BASE32:
        bbox = geohash1_bbox(ch)
        if user_bbox is not None:
            bbox = intersect_bboxes(bbox, user_bbox)
            if bbox is None:
                continue
        cells.append((ch, bbox))
    return cells


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def area_km2(geom, lat_mid: float) -> float:
    """Approximate area of a geometry in km² given its centroid latitude.

    Avoids the cost of projecting millions of polygons to an equal-area
    CRS just for filtering. Latitude-dependent cos(lat) factor on the
    longitude axis.
    """
    if geom.is_empty:
        return 0.0
    deg2_to_km2 = (KM_PER_DEG_LAT**2) * abs(math.cos(math.radians(lat_mid)))
    return geom.area * deg2_to_km2


def km_to_deg(km: float, lat_mid: float) -> float:
    """km → degrees at given latitude (longitude direction)."""
    cos_lat = max(0.01, abs(math.cos(math.radians(lat_mid))))
    return km / (KM_PER_DEG_LAT * cos_lat)


@contextlib.contextmanager
def suppress_geographic_crs_warning():
    """Silence geopandas' "Geometry is in a geographic CRS" warning.

    We intentionally do `centroid` / `buffer` on EPSG:4326 geometries for
    cheap clustering. The geometric error is irrelevant for grouping
    pieces by proximity — we only need a stable partition, not metric
    exactness.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            'ignore',
            message='Geometry is in a geographic CRS',
            category=UserWarning,
        )
        yield


def prefilter_by_area(
    pieces: gpd.GeoDataFrame, min_km2: float
) -> gpd.GeoDataFrame:
    """Drop polygon pieces whose individual area is below `min_km2`.

    Run BEFORE clustering: buffer + unary_union scale with N², so pruning
    the long tail of micro-pieces is the single biggest perf lever.
    """
    if min_km2 <= 0 or pieces.empty:
        return pieces
    with suppress_geographic_crs_warning():
        centroids_y = pieces.geometry.centroid.y.values
    areas_deg2 = pieces.geometry.area.values
    km2 = (
        areas_deg2
        * (KM_PER_DEG_LAT**2)
        * np.abs(np.cos(np.radians(centroids_y)))
    )
    keep = km2 >= min_km2
    kept = pieces.loc[keep].reset_index(drop=True)
    LOGGER.info(
        'Pre-filter ≥ %g km²: kept %d / %d pieces',
        min_km2,
        len(kept),
        len(pieces),
    )
    return kept


def cluster_pieces(
    pieces: gpd.GeoDataFrame, buffer_km: float
) -> gpd.GeoDataFrame:
    """Buffer + union + connected components to group adjacent pieces.

    Returns a copy of `pieces` with a `cluster_id` column. Pieces within
    `buffer_km` of each other end up in the same cluster — so an estuary
    split across several base polygons renders as a single image.
    """
    if pieces.empty:
        out = pieces.copy()
        out['cluster_id'] = []
        return out

    with suppress_geographic_crs_warning():
        centroid_lats = pieces.geometry.centroid.y
        lat_mid = float(centroid_lats.mean())
        buffer_deg = km_to_deg(buffer_km / 2, lat_mid)

        buffered = pieces.geometry.buffer(buffer_deg)
        dissolved = unary_union(buffered.values)
        components = (
            [dissolved]
            if dissolved.geom_type == 'Polygon'
            else list(dissolved.geoms)
        )
        cluster_gdf = gpd.GeoDataFrame(
            {'cluster_id': range(len(components))},
            geometry=components,
            crs=pieces.crs,
        )
        centroids = gpd.GeoDataFrame(
            pieces.drop(columns='geometry'),
            geometry=pieces.geometry.centroid,
            crs=pieces.crs,
        )
        joined = gpd.sjoin(
            centroids, cluster_gdf, how='left', predicate='within'
        )
    out = pieces.copy()
    out['cluster_id'] = joined['cluster_id'].astype('Int64').values
    return out


def rank_clusters(
    clustered: gpd.GeoDataFrame, min_cluster_km2: float
) -> list[tuple[int, float, gpd.GeoDataFrame, object]]:
    """Return (cluster_id, area_km2, pieces, union) sorted by area desc,
    dropping clusters below `min_cluster_km2`."""
    cluster_count = int(clustered['cluster_id'].max()) + 1
    candidates: list[tuple[int, float, gpd.GeoDataFrame, object]] = []
    for cluster_id in range(cluster_count):
        pieces = clustered[clustered['cluster_id'] == cluster_id]
        if pieces.empty:
            continue
        with suppress_geographic_crs_warning():
            lat_mid = float(pieces.geometry.centroid.y.mean())
        union = unary_union(pieces.geometry.values)
        area = area_km2(union, lat_mid)
        if area < min_cluster_km2:
            continue
        candidates.append((cluster_id, area, pieces, union))
    candidates.sort(key=lambda x: -x[1])
    return candidates


# ---------------------------------------------------------------------------
# Basemap
# ---------------------------------------------------------------------------


def resolve_basemap_provider(name: str):
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


def configure_basemap_cache(cache_dir: pathlib.Path) -> None:
    """Shared on-disk tile cache so parallel workers don't re-download
    the same tiles."""
    if not _HAVE_CONTEXTILY:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(cx, 'set_cache_dir'):
        cx.set_cache_dir(str(cache_dir))
    else:
        os.environ['CONTEXTILY_CACHE'] = str(cache_dir)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_with_basemap(
    ax,
    pieces_3857: gpd.GeoDataFrame,
    extent_4326: tuple[float, float, float, float],
    cluster_id: int,
    basemap_provider: str,
    reference_3857: gpd.GeoDataFrame | None = None,
    revised_3857: gpd.GeoDataFrame | None = None,
) -> None:
    """Overlay the patch pieces (and optionally reference/revised outlines)
    on a tile basemap in Web Mercator. Tile fetch is best-effort: on 429
    or connection issues we just continue with a blank background."""
    if reference_3857 is not None:
        reference_3857.boundary.plot(
            ax=ax, edgecolor='#1f77b4', linewidth=0.6, alpha=0.8
        )
    if revised_3857 is not None:
        revised_3857.boundary.plot(
            ax=ax, edgecolor='#d62728', linewidth=0.9, alpha=0.9
        )
    pieces_3857.plot(ax=ax, color='#ffdd44', alpha=0.55, edgecolor='#ff8c00')

    minx, miny, maxx, maxy = extent_4326
    extent_box_3857 = (
        gpd.GeoSeries([box(minx, miny, maxx, maxy)], crs=4326)
        .to_crs(3857)
        .total_bounds
    )
    ax.set_xlim(extent_box_3857[0], extent_box_3857[2])
    ax.set_ylim(extent_box_3857[1], extent_box_3857[3])
    ax.set_aspect('equal')
    try:
        provider = resolve_basemap_provider(basemap_provider)
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


def render_cluster(
    cluster_id: int,
    cluster_pieces: gpd.GeoDataFrame,
    output_path: pathlib.Path,
    dpi: int,
    with_basemap: bool,
    basemap_provider: str = _DEFAULT_BASEMAP_PROVIDER,
    reference: gpd.GeoDataFrame | None = None,
    revised: gpd.GeoDataFrame | None = None,
) -> dict:
    """Render one cluster as a PNG. Returns metadata dict for the atlas."""
    bounds = cluster_pieces.total_bounds
    minx, miny, maxx, maxy = bounds
    pad_x = max(0.005, (maxx - minx) * 0.25)
    pad_y = max(0.005, (maxy - miny) * 0.25)
    extent_minx = minx - pad_x
    extent_miny = max(-90.0, miny - pad_y)
    extent_maxx = maxx + pad_x
    extent_maxy = min(90.0, maxy + pad_y)

    # Optionally clip context layers (reference / revised) to the extent.
    ref_local = None
    rev_local = None
    if reference is not None:
        ref_local = reference.cx[
            extent_minx:extent_maxx, extent_miny:extent_maxy
        ]
    if revised is not None:
        rev_local = revised.cx[
            extent_minx:extent_maxx,
            extent_miny:extent_maxy,
        ]

    fig, ax = plt.subplots(figsize=(10, 10), dpi=dpi)

    if with_basemap and _HAVE_CONTEXTILY:
        pieces_3857 = cluster_pieces.to_crs(3857)
        ref_3857 = ref_local.to_crs(3857) if ref_local is not None else None
        rev_3857 = rev_local.to_crs(3857) if rev_local is not None else None
        _render_with_basemap(
            ax,
            pieces_3857,
            (extent_minx, extent_miny, extent_maxx, extent_maxy),
            cluster_id,
            basemap_provider,
            reference_3857=ref_3857,
            revised_3857=rev_3857,
        )
    else:
        if ref_local is not None:
            ref_local.boundary.plot(
                ax=ax,
                edgecolor='#1f77b4',
                linewidth=0.6,
                label='reference',
                alpha=0.8,
            )
        if rev_local is not None:
            rev_local.boundary.plot(
                ax=ax,
                edgecolor='#d62728',
                linewidth=0.9,
                label='revised',
                alpha=0.9,
            )
        cluster_pieces.plot(
            ax=ax,
            color='#ffdd44',
            edgecolor='#ff8c00',
            alpha=0.55,
            label='added',
        )
        ax.set_xlim(extent_minx, extent_maxx)
        ax.set_ylim(extent_miny, extent_maxy)
        ax.set_aspect('equal')
        if ref_local is not None or rev_local is not None:
            ax.legend(loc='best', framealpha=0.85)
        ax.set_xlabel('Longitude')
        ax.set_ylabel('Latitude')
        ax.grid(True, linestyle=':', alpha=0.4)

    center_lon = (minx + maxx) / 2
    center_lat = (miny + maxy) / 2
    union_geom = unary_union(cluster_pieces.geometry.values)
    total_km2 = area_km2(union_geom, center_lat)

    fig.suptitle(
        f'Cluster #{cluster_id} — '
        f'{center_lat:+.4f}°, {center_lon:+.4f}° '
        f'— added: {total_km2:.3f} km² '
        f'({len(cluster_pieces)} piece'
        f'{"s" if len(cluster_pieces) > 1 else ""})',
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
        'num_pieces': len(cluster_pieces),
        'image': output_path.name,
    }


# ---------------------------------------------------------------------------
# Atlas writer
# ---------------------------------------------------------------------------


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>UWP atlas</title>
<style>
  body  {{ font-family: system-ui, sans-serif; margin: 2rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
  th {{ background: #f4f4f4; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  img {{ max-height: 60px; }}
  a {{ color: #0645ad; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>UWP atlas</h1>
<p>{summary}</p>
<table id="atlas">
<thead><tr>
  <th>#</th><th>Cell</th><th>Preview</th><th>Center</th>
  <th>Added (km²)</th><th>Pieces</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""


def write_atlas(
    output_dir: pathlib.Path, records: list[dict], summary: str
) -> None:
    records_sorted = sorted(records, key=lambda r: -r['added_km2'])

    csv_path = output_dir / 'clusters.csv'
    pd.DataFrame(records_sorted).to_csv(csv_path, index=False)
    LOGGER.info('Wrote %s', csv_path)

    rows_html = [
        '<tr>'
        f'<td>{rec["cluster_id"]}</td>'
        f'<td><code>{html.escape(rec.get("cell", "?"))}</code></td>'
        f'<td><a href="png/{html.escape(rec["image"])}">'
        f'<img src="png/{html.escape(rec["image"])}" loading="lazy">'
        '</a></td>'
        f'<td>{rec["center_lat"]:+.4f}, {rec["center_lon"]:+.4f}</td>'
        f'<td>{rec["added_km2"]:.3f}</td>'
        f'<td>{rec["num_pieces"]}</td>'
        '</tr>'
        for rec in records_sorted
    ]
    html_path = output_dir / 'index.html'
    html_path.write_text(
        _HTML_TEMPLATE.format(summary=summary, rows='\n'.join(rows_html))
    )
    LOGGER.info('Wrote %s', html_path)


# ---------------------------------------------------------------------------
# Common CLI bits
# ---------------------------------------------------------------------------


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Inject the shared CLI flags (rendering, clustering, parallelism)."""
    parser.add_argument(
        '--output',
        type=pathlib.Path,
        required=True,
        help='Output directory for PNGs and index.',
    )
    parser.add_argument(
        '--min-cluster-km2',
        type=float,
        default=1.0,
        help='Skip clusters whose total area is below this threshold.',
    )
    parser.add_argument(
        '--cluster-buffer-km',
        type=float,
        default=5.0,
        help='Maximum distance between pieces for them to share a cluster.',
    )
    parser.add_argument(
        '--prefilter-km2',
        type=float,
        default=None,
        help='Drop individual pieces smaller than this BEFORE clustering. '
        'Default: --min-cluster-km2 / 20.',
    )
    parser.add_argument(
        '--dpi',
        type=int,
        default=200,
        help='Output PNG resolution.',
    )
    parser.add_argument(
        '--basemap',
        action='store_true',
        help='Add a basemap underneath (network required).',
    )
    parser.add_argument(
        '--basemap-provider',
        default=_DEFAULT_BASEMAP_PROVIDER,
        choices=_BASEMAP_CHOICES,
        help='Tile provider for --basemap.',
    )
    parser.add_argument(
        '--basemap-cache-dir',
        type=pathlib.Path,
        default=pathlib.Path.home() / '.cache' / 'uwp-basemap-tiles',
        help='Shared cache for basemap tiles.',
    )
    parser.add_argument(
        '--max-images',
        type=int,
        default=200,
        help='Cap on number of rendered images (per cell when sharded). '
        '0 = unlimited.',
    )
    parser.add_argument(
        '--bbox',
        type=float,
        nargs=4,
        metavar=('MINLON', 'MINLAT', 'MAXLON', 'MAXLAT'),
        default=None,
        help='Restrict the diff/atlas to this window.',
    )
    parser.add_argument(
        '--parallel-cells',
        type=int,
        default=0,
        help='Workers for parallel cell processing. 0 = cpu_count // 2.',
    )


def validate_basemap_arg(args: argparse.Namespace) -> None:
    if args.basemap and not _HAVE_CONTEXTILY:
        LOGGER.error(
            '--basemap requested but contextily is not installed. '
            'Either drop --basemap or `mamba install contextily`.'
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Parallel cell dispatch
# ---------------------------------------------------------------------------


def auto_parallelism(parallel: int, n_cells: int) -> int:
    if parallel <= 0:
        parallel = max(1, (os.cpu_count() or 2) // 2)
    return min(parallel, max(1, n_cells))


def run_cells_parallel(
    cells: list[tuple[str, tuple[float, float, float, float]]],
    worker_fn,
    worker_kwargs: dict,
    parallel: int,
) -> list[dict]:
    """Dispatch `worker_fn(cell_id, cell_bbox, **worker_kwargs)` across
    cells. Falls back to sequential when parallel == 1."""
    all_records: list[dict] = []
    if parallel == 1:
        for cell_id, cell_bbox in cells:
            try:
                all_records.extend(
                    worker_fn(cell_id, cell_bbox, **worker_kwargs)
                )
            except Exception:
                LOGGER.exception('Cell %s failed', cell_id)
        return all_records

    ctx = multiprocessing.get_context('spawn')
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=parallel, mp_context=ctx
    ) as pool:
        futures = {
            pool.submit(worker_fn, cid, cbb, **worker_kwargs): cid
            for cid, cbb in cells
        }
        for fut in concurrent.futures.as_completed(futures):
            cid = futures[fut]
            try:
                all_records.extend(fut.result())
            except Exception:
                LOGGER.exception('Cell %s failed', cid)
    return all_records

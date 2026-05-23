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


def geohash_bbox(geohash: str) -> tuple[float, float, float, float]:
    """(minlon, minlat, maxlon, maxlat) for an arbitrary-length geohash.

    Each character subdivides the cell along 5 bits, alternating lon
    (first bit) and lat. Standard geohash decoding.

    Precision examples:
      length 1 → 32 cells of 45 by 45 deg
      length 2 → 1024 cells of ~5.6 by 5.6 deg
      length 3 → 32768 cells of ~0.7 by 0.7 deg
    """
    minlon, maxlon = -180.0, 180.0
    minlat, maxlat = -90.0, 90.0
    is_lon = True  # bit 0 of bit 0 of char 0 picks longitude half
    for char in geohash.lower():
        idx = _GEOHASH_BASE32.index(char)
        for shift in range(4, -1, -1):
            bit = (idx >> shift) & 1
            if is_lon:
                mid = (minlon + maxlon) / 2
                if bit:
                    minlon = mid
                else:
                    maxlon = mid
            else:
                mid = (minlat + maxlat) / 2
                if bit:
                    minlat = mid
                else:
                    maxlat = mid
            is_lon = not is_lon
    return (minlon, minlat, maxlon, maxlat)


def geohash1_bbox(char: str) -> tuple[float, float, float, float]:
    """Compatibility shim: bbox for a 1-character geohash cell."""
    return geohash_bbox(char)


def encode_geohash(lon: float, lat: float, precision: int) -> str:
    """Encode a (lon, lat) point to a geohash of the given precision.

    Iteratively refines lon / lat ranges, alternating bits. Returns a
    string of `precision` base32 characters.
    """
    minlon, maxlon = -180.0, 180.0
    minlat, maxlat = -90.0, 90.0
    bits: list[int] = []
    chars: list[str] = []
    is_lon = True
    while len(chars) < precision:
        if is_lon:
            mid = (minlon + maxlon) / 2
            if lon >= mid:
                bits.append(1)
                minlon = mid
            else:
                bits.append(0)
                maxlon = mid
        else:
            mid = (minlat + maxlat) / 2
            if lat >= mid:
                bits.append(1)
                minlat = mid
            else:
                bits.append(0)
                maxlat = mid
        is_lon = not is_lon
        if len(bits) == 5:
            idx = 0
            for b in bits:
                idx = (idx << 1) | b
            chars.append(_GEOHASH_BASE32[idx])
            bits = []
    return ''.join(chars)


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


def assign_geohash(
    pieces: gpd.GeoDataFrame, precision: int
) -> gpd.GeoDataFrame:
    """Add a `geohash` column to `pieces` by encoding each polygon's
    centroid at the given precision.

    The cell grid is deterministic (no proximity clustering), so the
    number of produced cells is bounded above by `32 ** precision` and
    the partition is reproducible across runs. Border polygons get
    assigned to whichever cell contains their centroid — for QA viz
    that's good enough.
    """
    if pieces.empty:
        out = pieces.copy()
        out['geohash'] = []
        return out
    with suppress_geographic_crs_warning():
        cent_x = pieces.geometry.centroid.x.values
        cent_y = pieces.geometry.centroid.y.values
    geohashes = [
        encode_geohash(float(lon), float(lat), precision)
        for lon, lat in zip(cent_x, cent_y, strict=True)
    ]
    out = pieces.copy()
    out['geohash'] = geohashes
    return out


def group_by_geohash(
    pieces: gpd.GeoDataFrame, min_cell_km2: float
) -> list[tuple[str, float, gpd.GeoDataFrame]]:
    """Group polygons by their `geohash` column and return cells sorted by
    total area descending. Cells whose summed added area is below
    `min_cell_km2` are dropped.

    Returns a list of `(geohash, area_km2, pieces_in_cell)`.
    """
    if pieces.empty or 'geohash' not in pieces.columns:
        return []
    cells: list[tuple[str, float, gpd.GeoDataFrame]] = []
    for gh, sub in pieces.groupby('geohash', sort=False):
        if sub.empty:
            continue
        # Bbox center latitude is a good-enough reference for the local
        # cos(lat) area correction — the cell is small.
        bbox = geohash_bbox(gh)
        lat_mid = (bbox[1] + bbox[3]) / 2
        total = area_km2(unary_union(sub.geometry.values), lat_mid)
        if total < min_cell_km2:
            continue
        cells.append((gh, total, sub))
    cells.sort(key=lambda x: -x[1])
    return cells


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


def _polygon_colors(n: int) -> list:
    """Build a list of `n` distinct fill colours by cycling through
    qualitative colormaps.

    `tab20` gives 20 well-separated colours. For more we wrap and accept
    repeats — alphabetical neighbours rarely get the same colour, which
    keeps the overlay readable in practice.
    """
    cmap = matplotlib.colormaps.get_cmap('tab20')
    return [cmap(i % cmap.N) for i in range(n)]


def _add_basemap_safely(
    ax,
    extent_3857: tuple[float, float, float, float],
    basemap_provider: str,
    label: str,
) -> None:
    """Set axis extent in Web Mercator and lay a basemap tile underneath.
    Tile fetch is best-effort: on 429 or connection issues we just
    continue with a blank background — the overlay is still readable."""
    ax.set_xlim(extent_3857[0], extent_3857[2])
    ax.set_ylim(extent_3857[1], extent_3857[3])
    ax.set_aspect('equal')
    try:
        provider = resolve_basemap_provider(basemap_provider)
        cx.add_basemap(ax, source=provider, attribution_size=5)
    except Exception as exc:
        LOGGER.warning(
            'Basemap fetch failed for %s (provider=%s): %s',
            label,
            basemap_provider,
            exc,
        )
    ax.set_xticks([])
    ax.set_yticks([])


def render_geohash_cell(
    geohash: str,
    pieces: gpd.GeoDataFrame,
    output_path: pathlib.Path,
    dpi: int,
    with_basemap: bool,
    basemap_provider: str = _DEFAULT_BASEMAP_PROVIDER,
    reference: gpd.GeoDataFrame | None = None,
    revised: gpd.GeoDataFrame | None = None,
) -> dict:
    """Render every polygon in a geohash cell, each in a distinct colour.

    The extent of the figure is the geohash cell itself (not the pieces'
    bbox) so adjacent cells line up perfectly in the atlas and the user
    can mentally tile them. A thin grey outline around the cell bbox
    makes the boundary explicit.

    Optional `reference` / `revised` layers (used by visualize_diff) are
    clipped to the cell extent and drawn as boundary outlines (blue /
    red) underneath the coloured patches.

    Returns the metadata record for the atlas index.
    """
    minlon, minlat, maxlon, maxlat = geohash_bbox(geohash)
    # No padding — the extent IS the cell. Atlas tiles align then.
    ref_local = None
    rev_local = None
    if reference is not None:
        ref_local = reference.cx[minlon:maxlon, minlat:maxlat]
    if revised is not None:
        rev_local = revised.cx[minlon:maxlon, minlat:maxlat]

    fig, ax = plt.subplots(figsize=(10, 10), dpi=dpi)
    colors = _polygon_colors(len(pieces))

    if with_basemap and _HAVE_CONTEXTILY:
        pieces_3857 = pieces.to_crs(3857)
        if ref_local is not None and not ref_local.empty:
            ref_local.to_crs(3857).boundary.plot(
                ax=ax, edgecolor='#1f77b4', linewidth=0.5, alpha=0.7
            )
        if rev_local is not None and not rev_local.empty:
            rev_local.to_crs(3857).boundary.plot(
                ax=ax, edgecolor='#d62728', linewidth=0.7, alpha=0.85
            )
        pieces_3857.plot(
            ax=ax,
            color=colors,
            edgecolor='black',
            linewidth=0.3,
            alpha=0.75,
        )
        extent_3857 = (
            gpd.GeoSeries([box(minlon, minlat, maxlon, maxlat)], crs=4326)
            .to_crs(3857)
            .total_bounds
        )
        _add_basemap_safely(
            ax, extent_3857, basemap_provider, f'cell {geohash}'
        )
    else:
        if ref_local is not None and not ref_local.empty:
            ref_local.boundary.plot(
                ax=ax,
                edgecolor='#1f77b4',
                linewidth=0.5,
                label='reference',
                alpha=0.7,
            )
        if rev_local is not None and not rev_local.empty:
            rev_local.boundary.plot(
                ax=ax,
                edgecolor='#d62728',
                linewidth=0.7,
                label='revised',
                alpha=0.85,
            )
        pieces.plot(
            ax=ax,
            color=colors,
            edgecolor='black',
            linewidth=0.3,
            alpha=0.75,
        )
        ax.set_xlim(minlon, maxlon)
        ax.set_ylim(minlat, maxlat)
        ax.set_aspect('equal')
        if (ref_local is not None and not ref_local.empty) or (
            rev_local is not None and not rev_local.empty
        ):
            ax.legend(loc='best', framealpha=0.85)
        ax.set_xlabel('Longitude')
        ax.set_ylabel('Latitude')
        ax.grid(True, linestyle=':', alpha=0.4)

    center_lon = (minlon + maxlon) / 2
    center_lat = (minlat + maxlat) / 2
    union_geom = unary_union(pieces.geometry.values)
    total_km2 = area_km2(union_geom, center_lat)

    fig.suptitle(
        f'Geohash {geohash} — '
        f'{center_lat:+.4f}°, {center_lon:+.4f}° '
        f'— added: {total_km2:.3f} km² '
        f'({len(pieces)} polygon'
        f'{"s" if len(pieces) > 1 else ""})',
        fontsize=12,
    )
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches='tight')
    plt.close(fig)

    return {
        'geohash': geohash,
        'center_lat': center_lat,
        'center_lon': center_lon,
        'added_km2': total_km2,
        'num_pieces': len(pieces),
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
  <th>Geohash</th><th>Preview</th><th>Center</th>
  <th>Added (km²)</th><th>Polygons</th>
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

    csv_path = output_dir / 'cells.csv'
    pd.DataFrame(records_sorted).to_csv(csv_path, index=False)
    LOGGER.info('Wrote %s', csv_path)

    rows_html = [
        '<tr>'
        f'<td><code>{html.escape(rec["geohash"])}</code></td>'
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
    """Inject the shared CLI flags (rendering, geohash, parallelism)."""
    parser.add_argument(
        '--output',
        type=pathlib.Path,
        required=True,
        help='Output directory for PNGs and index.',
    )
    parser.add_argument(
        '--geohash-precision',
        type=int,
        default=2,
        help='Geohash length used to grid the globe. Each character '
        'subdivides cells along 5 bits → image count is bounded above by '
        '32^precision. Recommended: 1 (very coarse, ~45deg cells, up to '
        '32 images), 2 (~5.6deg, up to 1024), 3 (~0.7deg, up to 32768). '
        'Only non-empty cells produce an image.',
    )
    parser.add_argument(
        '--min-cell-km2',
        type=float,
        default=1.0,
        help='Skip cells whose total added area is below this threshold.',
    )
    parser.add_argument(
        '--prefilter-km2',
        type=float,
        default=None,
        help='Drop individual pieces smaller than this BEFORE grouping. '
        'Default: --min-cell-km2 / 20.',
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
        default=0,
        help='Cap on number of rendered images per shard. 0 = unlimited '
        '(default — geohash precision already bounds the total).',
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

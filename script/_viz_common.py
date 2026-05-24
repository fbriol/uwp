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

# Default basemap tile cache. We deliberately avoid ~/.cache because HOME
# is small-quota on most HPC clusters (the CNES one fills up fast). The
# repo's `data/` is the right place: `init_workspace.sh` symlinks it to
# scratch space when UWP_DATA_DIR is set. Overridable via
# `--basemap-cache-dir` or the UWP_TILE_CACHE env var.
DEFAULT_TILE_CACHE = (
    pathlib.Path(os.environ['UWP_TILE_CACHE'])
    if os.environ.get('UWP_TILE_CACHE')
    else pathlib.Path(__file__).parent.parent / 'data' / '_tile_cache'
)


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
            # New binding to keep mypy happy (intersect_bboxes returns
            # an Optional, can't reassign to the non-Optional `bbox`).
            clipped = intersect_bboxes(bbox, user_bbox)
            if clipped is None:
                continue
            bbox = clipped
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


def cell_size(precision: int) -> tuple[float, float]:
    """(lon_step, lat_step) in degrees for any geohash cell at `precision`.

    All cells at the same precision have identical dimensions; we read
    them off a sample cell to avoid hard-coding the lon/lat-bit ratios
    (which alternate with precision parity).
    """
    sample = encode_geohash(0.0, 0.0, precision)
    bb = geohash_bbox(sample)
    return (bb[2] - bb[0], bb[3] - bb[1])


def enumerate_geohashes_in_bbox(
    bbox: tuple[float, float, float, float], precision: int
) -> list[str]:
    """All geohashes at `precision` whose cell intersects `bbox`.

    Returns the list in row-major (top-to-bottom, west-to-east) order so
    it can be consumed directly by the planisphere HTML grid.
    """
    minlon, minlat, maxlon, maxlat = bbox
    lon_step, lat_step = cell_size(precision)

    # Snap bbox to the cell grid. The world origin is (-180, -90); cells
    # tile from there outward.
    col_min = math.floor((minlon - (-180.0)) / lon_step)
    col_max = math.floor((maxlon - (-180.0)) / lon_step)
    if (maxlon - (-180.0)) / lon_step == col_max and maxlon > minlon:
        col_max -= 1  # don't include a cell that the bbox only touches
    row_min = math.floor((minlat - (-90.0)) / lat_step)
    row_max = math.floor((maxlat - (-90.0)) / lat_step)
    if (maxlat - (-90.0)) / lat_step == row_max and maxlat > minlat:
        row_max -= 1

    # Row-major output goes north-to-south, west-to-east — matches the way
    # we lay out the planisphere HTML grid.
    geohashes: list[str] = []
    for row in range(row_max, row_min - 1, -1):
        lat_centre = -90.0 + (row + 0.5) * lat_step
        for col in range(col_min, col_max + 1):
            lon_centre = -180.0 + (col + 0.5) * lon_step
            geohashes.append(encode_geohash(lon_centre, lat_centre, precision))
    return geohashes


def geohash_grid_position(
    geohash: str, lon_step: float, lat_step: float
) -> tuple[int, int]:
    """(col, row) of `geohash` in the planisphere grid.

    Col 0 is the westmost cell (minlon = -180). Row 0 is the *northmost*
    cell (matches screen / CSS-grid orientation, top-to-bottom).
    """
    bb = geohash_bbox(geohash)
    col = round((bb[0] - (-180.0)) / lon_step)
    # row 0 = topmost = highest lat. The cell with maxlat = +90 is row 0,
    # so the cell with maxlat = bb[3] is at row (90 - bb[3]) / lat_step.
    row = round((90.0 - bb[3]) / lat_step)
    return col, row


def select_intersecting(
    pieces: gpd.GeoDataFrame, bbox: tuple[float, float, float, float]
) -> gpd.GeoDataFrame:
    """Return the subset of `pieces` whose geometry intersects `bbox`.

    Uses geopandas' coordinate indexer (which itself uses the underlying
    R-tree). Replaces the old centroid-based assignment: a polygon
    straddling a cell boundary now appears in BOTH cells, which is what
    the planisphere visualisation wants.
    """
    if pieces.empty:
        return pieces
    minlon, minlat, maxlon, maxlat = bbox
    # geopandas' CoordinateIndexer accepts float slices, but its type
    # stubs only declare int/SupportsIndex — silence the mypy false
    # positive (runtime behaviour is correct).
    return pieces.cx[minlon:maxlon, minlat:maxlat]  # type: ignore[misc]


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
    coastline: gpd.GeoDataFrame | None = None,
) -> dict:
    """Render the polygons intersecting a geohash cell, each in a
    distinct colour.

    The figure extent is the geohash cell itself; matplotlib clips
    outside, so polygons that extend past the cell border display as
    truncated. Adjacent cells line up perfectly in the planisphere
    atlas.

    `coastline` (optional, used by visualize_diff) is drawn as a thin
    dark outline UNDERNEATH the coloured polygons so the user sees
    where each addition lands relative to the modified coast.
    """
    minlon, minlat, maxlon, maxlat = geohash_bbox(geohash)
    coast_local = None
    if coastline is not None:
        # Same float-slice / stub limitation as in `select_intersecting`.
        coast_local = select_intersecting(
            coastline, (minlon, minlat, maxlon, maxlat)
        )

    fig, ax = plt.subplots(figsize=(10, 10), dpi=dpi)
    colors = _polygon_colors(len(pieces))

    if with_basemap and _HAVE_CONTEXTILY:
        if coast_local is not None and not coast_local.empty:
            coast_local.to_crs(3857).boundary.plot(
                ax=ax, edgecolor='#444', linewidth=0.6, alpha=0.9
            )
        pieces.to_crs(3857).plot(
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
        if coast_local is not None and not coast_local.empty:
            coast_local.boundary.plot(
                ax=ax,
                edgecolor='#444',
                linewidth=0.6,
                alpha=0.9,
                label='coastline (revised)',
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
        if coast_local is not None and not coast_local.empty:
            ax.legend(loc='best', framealpha=0.85)
        ax.set_xlabel('Longitude')
        ax.set_ylabel('Latitude')
        ax.grid(True, linestyle=':', alpha=0.4)

    center_lon = (minlon + maxlon) / 2
    center_lat = (minlat + maxlat) / 2
    union_geom = unary_union(pieces.geometry.values)
    total_km2 = area_km2(union_geom, center_lat)

    # `ax.set_title` instead of `fig.suptitle`: the suptitle is anchored
    # to the figure (always leaves a large gap when the axis is shrunk
    # by set_aspect('equal')), whereas a per-axis title sits flush
    # above the plot area regardless of how matplotlib resized it.
    ax.set_title(
        f'Geohash {geohash} — '
        f'{center_lat:+.4f}°, {center_lon:+.4f}° '
        f'— added: {total_km2:.3f} km² '
        f'({len(pieces)} polygon'
        f'{"s" if len(pieces) > 1 else ""})',
        fontsize=12,
        pad=6,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Tight crop on save: bbox_inches='tight' removes leftover figure
    # margins, pad_inches keeps a small breathing room around the axis.
    fig.savefig(output_path, bbox_inches='tight', pad_inches=0.1)
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
  body {{
    font-family: system-ui, sans-serif;
    margin: 1.5rem;
    background: #fafafa;
  }}
  h1 {{ margin-bottom: 0.25rem; }}
  p.summary {{ color: #555; margin-top: 0; }}
  .planisphere {{
    display: grid;
    grid-template-columns: repeat({cols}, {tile}px);
    gap: 1px;
    background: #ddd;
    border: 1px solid #aaa;
    width: max-content;
    margin: 1rem auto;
  }}
  .cell {{
    width: {tile}px;
    height: {tile}px;
    background: #fff;
    position: relative;
    overflow: hidden;
  }}
  .cell.empty {{
    background: #f0f0f0;
    background-image:
      linear-gradient(45deg, #e6e6e6 25%, transparent 25%),
      linear-gradient(-45deg, #e6e6e6 25%, transparent 25%);
    background-size: 8px 8px;
  }}
  .cell a {{ display: block; width: 100%; height: 100%; }}
  .cell img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }}
  .cell .label {{
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    background: rgba(0, 0, 0, 0.55);
    color: white;
    font-size: 9px;
    text-align: center;
    padding: 1px 2px;
    font-family: monospace;
  }}
</style>
</head>
<body>
<h1>UWP atlas</h1>
<p class="summary">{summary}</p>
<p style="text-align:center; color:#555;">
  Planisphere — geohash precision {precision}, grid {cols}x{rows}.
  Click any tile to enlarge. Striped cells = no modifications.
</p>
<div class="planisphere">
{tiles}
</div>
</body>
</html>
"""


def _planisphere_bounds(
    rendered: list[dict],
    enumerated_geohashes: list[str],
    lon_step: float,
    lat_step: float,
) -> tuple[int, int, int, int]:
    """(col_min, col_max, row_min, row_max) over all cells we know about
    (both rendered and empty). The grid is built within that span."""
    if not enumerated_geohashes:
        return (0, 0, 0, 0)
    cols, rows = [], []
    for gh in enumerated_geohashes:
        c, r = geohash_grid_position(gh, lon_step, lat_step)
        cols.append(c)
        rows.append(r)
    return (min(cols), max(cols), min(rows), max(rows))


def write_atlas(
    output_dir: pathlib.Path,
    records: list[dict],
    enumerated_geohashes: list[str],
    precision: int,
    summary: str,
    tile_px: int = 96,
) -> None:
    """Write `index.html` (planisphere grid) + `cells.csv` (machine-
    readable list of rendered cells).

    `enumerated_geohashes` is the full list of cells the atlas covers
    (in row-major order). Records whose geohash isn't in that list are
    ignored (shouldn't happen in practice).
    """

    # CSV — sorted by area for human consumption. `list.sort` is used
    # instead of `sorted` because pandas-stubs overrides `sorted`'s
    # signature with pandas-specific overloads that interfere with our
    # plain dict records here.
    def _neg_area(rec: dict) -> float:
        return -float(rec['added_km2'])

    sorted_records = list(records)
    sorted_records.sort(key=_neg_area)

    csv_path = output_dir / 'cells.csv'
    pd.DataFrame(sorted_records).to_csv(csv_path, index=False)
    LOGGER.info('Wrote %s', csv_path)

    # Planisphere layout: lay out every enumerated cell on the grid,
    # whether or not it has a rendered image.
    lon_step, lat_step = cell_size(precision)
    col_min, col_max, row_min, row_max = _planisphere_bounds(
        records, enumerated_geohashes, lon_step, lat_step
    )
    n_cols = col_max - col_min + 1
    n_rows = row_max - row_min + 1

    by_geohash: dict[str, dict] = {r['geohash']: r for r in records}
    # Place cells in row-major (top-to-bottom, west-to-east) order.
    # CSS grid fills cells in declaration order along rows.
    tiles_by_pos: dict[tuple[int, int], str] = {}
    for gh in enumerated_geohashes:
        col, row = geohash_grid_position(gh, lon_step, lat_step)
        local_col = col - col_min
        local_row = row - row_min
        rec = by_geohash.get(gh)
        if rec is not None:
            img = html.escape(f'png/{rec["image"]}')
            title = (
                f'{gh}: {rec["added_km2"]:.2f} km² '
                f'({rec["num_pieces"]} polygons)'
            )
            tile = (
                f'<div class="cell" title="{html.escape(title)}">'
                f'<a href="{img}"><img src="{img}" loading="lazy" '
                f'alt="{html.escape(gh)}"></a>'
                f'<div class="label">{html.escape(gh)}</div>'
                '</div>'
            )
        else:
            tile = (
                f'<div class="cell empty" '
                f'title="{html.escape(gh)} — no modifications"></div>'
            )
        tiles_by_pos[(local_row, local_col)] = tile

    # Emit tiles in row-major order; missing positions get a placeholder
    # (shouldn't happen if enumerate_geohashes_in_bbox covers the bbox).
    parts: list[str] = [
        tiles_by_pos.get((row, col), '<div class="cell empty"></div>')
        for row in range(n_rows)
        for col in range(n_cols)
    ]
    html_path = output_dir / 'index.html'
    html_path.write_text(
        _HTML_TEMPLATE.format(
            summary=summary,
            cols=n_cols,
            rows=n_rows,
            tile=tile_px,
            precision=precision,
            tiles='\n'.join(parts),
        )
    )
    LOGGER.info(
        'Wrote %s (planisphere grid %d cols x %d rows, %d rendered, '
        '%d placeholders)',
        html_path,
        n_cols,
        n_rows,
        len(by_geohash),
        len(enumerated_geohashes) - len(by_geohash),
    )


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
        default=DEFAULT_TILE_CACHE,
        help='Shared cache for basemap tiles. Defaults to '
        '<repo>/data/_tile_cache (on the CNES cluster this lives on '
        'scratch via the symlink set up by init_workspace.sh) — not in '
        '~/.cache, which has tight quota. Override with '
        'UWP_TILE_CACHE env var or this flag.',
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

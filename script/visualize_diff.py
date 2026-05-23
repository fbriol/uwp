#!/usr/bin/env python3
"""Generate an HD image atlas of every polygon piece uwp added to the
reference OSM coastline — computed as a full geometric diff between the
reference and the revised shapefiles.

This is the *audit* path. Compared to `visualize_patches.py` (which reads
the patches shapefile written by uwp itself), this script:

  - loads the millions-of-polygons reference & revised shapefiles,
  - runs `shapely.difference(revised, reference)` per FID,
  - is much slower (~10x depending on hardware).

Use it when:

  - you want a ground-truth check that uwp's `--patches-output` is
    consistent with the actual geometric difference (regression test),
  - you don't have the patches shapefile (e.g. comparing the output of
    two different uwp versions),
  - you want the reference/revised coastlines drawn on the PNGs as
    context layers (visualize_patches doesn't load them by default).

Each cluster's PNG shows
  - blue outline: reference polygons in the area,
  - red outline:  revised polygons in the same area,
  - yellow fill:  the added zone(s),
  - optional OSM-style basemap underneath.

Usage:
    python script/visualize_diff.py \\
        --reference data/water-polygons-split-4326/water_polygons.shp \\
        --revised  data/corrected-water-polygons.shp \\
        --output   data/diff-report \\
        --basemap
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

import geopandas as gpd
import numpy as np
import shapely

import _viz_common as common

LOGGER = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).parent.parent
DATA_DIR = ROOT / 'data'

DEFAULT_REFERENCE = (
    DATA_DIR / 'water-polygons-split-4326' / 'water_polygons.shp'
)
DEFAULT_REVISED = DATA_DIR / 'corrected-water-polygons.shp'


# ---------------------------------------------------------------------------
# Diff-specific logic — the *only* thing this script does that
# visualize_patches.py doesn't.
# ---------------------------------------------------------------------------


def find_deltas(
    reference: gpd.GeoDataFrame, revised: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Return a GeoDataFrame of every polygon piece added by uwp.

    Pairing strategy: uwp mutates polygons in place, so for index `i`
    common to both files the addition is `revised[i] - reference[i]`.
    Polygons appended at the end of the revised file (the
    `extra_polygons` from `merge_overlapping`) have no counterpart and
    are treated as fully new.
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
    common_n = min(n_ref, n_rev)

    # Step 1: cheap envelope test, vectorised in numpy. Catches the
    # overwhelming majority of records since the diff only touches a few
    # thousand polygons out of ~50-100k.
    ref_bounds = reference.bounds.values
    rev_bounds = revised.bounds.values
    changed_mask = np.any(
        ref_bounds[:common_n] != rev_bounds[:common_n], axis=1
    )
    changed_fids = np.flatnonzero(changed_mask)
    LOGGER.info(
        'Envelope-changed candidates: %d / %d', len(changed_fids), common_n
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
        for fid in range(common_n, n_rev)
    )

    if not rows:
        return gpd.GeoDataFrame(
            columns=['fid', 'kind', 'geometry'], crs=revised.crs
        )
    return gpd.GeoDataFrame(rows, crs=revised.crs)


# ---------------------------------------------------------------------------
# Worker (per geohash-1 cell)
# ---------------------------------------------------------------------------


def _process_cell(
    shard_id: str,
    shard_bbox: tuple[float, float, float, float],
    reference_path: pathlib.Path,
    revised_path: pathlib.Path,
    output_dir: pathlib.Path,
    geohash_precision: int,
    min_cell_km2: float,
    prefilter_km2: float,
    dpi: int,
    with_basemap: bool,
    max_images: int,
    basemap_provider: str = common._DEFAULT_BASEMAP_PROVIDER,
    basemap_cache_dir: pathlib.Path | None = None,
) -> list[dict]:
    """Top-level worker for ProcessPoolExecutor: load both shapefiles
    clipped to `shard_bbox` (a level-1 geohash), run the diff, then
    group by geohash at the requested precision and render each
    non-empty cell via `_viz_common`. Reference and revised geometries
    are passed to the renderer as context layers."""
    logging.basicConfig(
        level=logging.INFO,
        format=(
            f'%(asctime)s - [shard {shard_id}] %(levelname)s - %(message)s'
        ),
        force=True,
    )
    if with_basemap and basemap_cache_dir is not None:
        common.configure_basemap_cache(basemap_cache_dir)

    reference = gpd.read_file(reference_path, bbox=shard_bbox)
    revised = gpd.read_file(revised_path, bbox=shard_bbox)
    if reference.empty and revised.empty:
        return []
    if reference.crs is None:
        reference = reference.set_crs(4326, allow_override=True)
    if revised.crs is None:
        revised = revised.set_crs(4326, allow_override=True)

    deltas = find_deltas(reference, revised)
    if deltas.empty:
        return []

    deltas = common.prefilter_by_area(deltas, prefilter_km2)
    if deltas.empty:
        return []

    deltas = common.assign_geohash(deltas, geohash_precision)
    cells = common.group_by_geohash(deltas, min_cell_km2)
    if max_images and len(cells) > max_images:
        cells = cells[:max_images]

    rendered: list[dict] = []
    png_dir = output_dir / 'png'
    for geohash, _area, pieces_in_cell in cells:
        name = f'gh_{geohash}.png'
        try:
            rec = common.render_geohash_cell(
                geohash,
                pieces_in_cell,
                png_dir / name,
                dpi=dpi,
                with_basemap=with_basemap,
                basemap_provider=basemap_provider,
                # Diff-specific: layer reference (blue) and revised (red)
                # outlines underneath the coloured delta polygons.
                reference=reference,
                revised=revised,
            )
            rendered.append(rec)
        except Exception:
            LOGGER.exception('Failed rendering geohash %s', geohash)
    LOGGER.info('Shard %s done: %d image(s)', shard_id, len(rendered))
    return rendered


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
    common.add_common_args(parser)
    return parser.parse_args()


def _validate_inputs(args: argparse.Namespace) -> None:
    for label, path in (
        ('Reference', args.reference),
        ('Revised', args.revised),
    ):
        if not path.exists():
            LOGGER.error('%s not found: %s', label, path)
            sys.exit(1)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        force=True,
    )
    args = _parse_args()
    common.validate_basemap_arg(args)
    _validate_inputs(args)

    (args.output / 'png').mkdir(parents=True, exist_ok=True)
    if args.basemap:
        common.configure_basemap_cache(args.basemap_cache_dir)

    prefilter_km2 = (
        args.prefilter_km2
        if args.prefilter_km2 is not None
        else args.min_cell_km2 / 20.0
    )

    user_bbox = tuple(args.bbox) if args.bbox else None
    shards = common.select_cells(user_bbox)  # level-1 cells for I/O
    parallel = common.auto_parallelism(args.parallel_cells, len(shards))

    LOGGER.info(
        'Ref: %s | Rev: %s | shards=%d | workers=%d | '
        'geohash precision=%d | prefilter=%g km² | min_cell=%g km²',
        args.reference,
        args.revised,
        len(shards),
        parallel,
        args.geohash_precision,
        prefilter_km2,
        args.min_cell_km2,
    )

    worker_kwargs = {
        'reference_path': args.reference,
        'revised_path': args.revised,
        'output_dir': args.output,
        'geohash_precision': args.geohash_precision,
        'min_cell_km2': args.min_cell_km2,
        'prefilter_km2': prefilter_km2,
        'dpi': args.dpi,
        'with_basemap': args.basemap,
        'max_images': args.max_images,
        'basemap_provider': args.basemap_provider,
        'basemap_cache_dir': args.basemap_cache_dir,
    }

    records = common.run_cells_parallel(
        shards, _process_cell, worker_kwargs, parallel
    )

    if not records:
        LOGGER.warning(
            'No geohash cell met the --min-cell-km2=%g threshold.',
            args.min_cell_km2,
        )
        return

    total_km2 = sum(r['added_km2'] for r in records)
    summary = (
        f'{len(records)} geohash-{args.geohash_precision} cell(s) rendered '
        f'(total added: {total_km2:.2f} km²). '
        f'Source: <code>{args.revised}</code> vs '
        f'<code>{args.reference}</code>.'
    )
    common.write_atlas(args.output, records, summary)
    LOGGER.info('Done. %d image(s) written to %s', len(records), args.output)


if __name__ == '__main__':
    main()

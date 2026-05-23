#!/usr/bin/env python3
"""Generate an HD image atlas of every polygon piece uwp added to the
reference OSM coastline.

Reads the **patches shapefile** written by the uwp binary's
`--patches-output` flag (one feature per polygon piece, ground-truth from
the C++ merge code). For each cluster of nearby patches we render a
high-DPI PNG showing the patches in yellow/orange, optionally on an OSM
basemap.

This is the fast path for QA. Compared to `visualize_diff.py` (full
geometric diff between reference and revised shapefiles) it skips:
  - loading the millions-of-polygons reference & revised shapefiles,
  - computing 40k+ shapely.difference() calls,
  - the long tail of numerical-artefact "deltas".

Usage:
    python script/visualize_patches.py --patches data/patches.shp \\
        --output data/patches-report --basemap
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

import geopandas as gpd

import _viz_common as common

LOGGER = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).parent.parent
DATA_DIR = ROOT / 'data'
DEFAULT_PATCHES = DATA_DIR / 'patches.shp'


# ---------------------------------------------------------------------------
# Worker (per geohash-1 cell)
# ---------------------------------------------------------------------------


def _process_cell(
    shard_id: str,
    shard_bbox: tuple[float, float, float, float],
    patches_path: pathlib.Path,
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
    """Top-level worker for ProcessPoolExecutor. Reads the patches slice
    intersecting `shard_bbox` (a level-1 geohash cell), groups by
    geohash at the user-requested precision, and renders one PNG per
    non-empty cell."""
    logging.basicConfig(
        level=logging.INFO,
        format=(
            f'%(asctime)s - [shard {shard_id}] %(levelname)s - %(message)s'
        ),
        force=True,
    )
    if with_basemap and basemap_cache_dir is not None:
        common.configure_basemap_cache(basemap_cache_dir)

    patches = gpd.read_file(patches_path, bbox=shard_bbox)
    if patches.empty:
        return []
    if patches.crs is None:
        patches = patches.set_crs(4326, allow_override=True)

    patches = common.prefilter_by_area(patches, prefilter_km2)
    if patches.empty:
        return []

    patches = common.assign_geohash(patches, geohash_precision)
    cells = common.group_by_geohash(patches, min_cell_km2)
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
        '--patches',
        type=pathlib.Path,
        default=DEFAULT_PATCHES,
        help='Patches shapefile written by uwp --patches-output.',
    )
    common.add_common_args(parser)
    args = parser.parse_args()
    # `--output` is required by add_common_args; set a sensible default
    # only after the fact so the user can still override it.
    return args


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        force=True,
    )
    args = _parse_args()
    common.validate_basemap_arg(args)

    if not args.patches.exists():
        LOGGER.error(
            'Patches shapefile not found: %s. Did you run uwp with '
            '--patches-output (default)?',
            args.patches,
        )
        sys.exit(1)

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
        'Patches: %s | shards=%d | workers=%d | '
        'geohash precision=%d | prefilter=%g km² | min_cell=%g km²',
        args.patches,
        len(shards),
        parallel,
        args.geohash_precision,
        prefilter_km2,
        args.min_cell_km2,
    )

    worker_kwargs = {
        'patches_path': args.patches,
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
        f'(total added: {total_km2:.2f} km²) '
        f'from <code>{args.patches}</code>.'
    )
    common.write_atlas(args.output, records, summary)
    LOGGER.info('Done. %d image(s) written to %s', len(records), args.output)


if __name__ == '__main__':
    main()

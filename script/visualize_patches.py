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
    cell_id: str,
    cell_bbox: tuple[float, float, float, float],
    patches_path: pathlib.Path,
    output_dir: pathlib.Path,
    min_cluster_km2: float,
    cluster_buffer_km: float,
    prefilter_km2: float,
    dpi: int,
    with_basemap: bool,
    max_images: int,
    basemap_provider: str = common._DEFAULT_BASEMAP_PROVIDER,
    basemap_cache_dir: pathlib.Path | None = None,
) -> list[dict]:
    """Top-level worker for ProcessPoolExecutor. Reads the patches slice
    intersecting `cell_bbox`, filters/clusters/renders, returns the
    per-cluster metadata."""
    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s - [cell {cell_id}] %(levelname)s - %(message)s',
        force=True,
    )
    if with_basemap and basemap_cache_dir is not None:
        common.configure_basemap_cache(basemap_cache_dir)

    patches = gpd.read_file(patches_path, bbox=cell_bbox)
    if patches.empty:
        return []
    if patches.crs is None:
        patches = patches.set_crs(4326, allow_override=True)

    patches = common.prefilter_by_area(patches, prefilter_km2)
    if patches.empty:
        return []

    clustered = common.cluster_pieces(patches, cluster_buffer_km)
    candidates = common.rank_clusters(clustered, min_cluster_km2)
    if max_images and len(candidates) > max_images:
        candidates = candidates[:max_images]

    rendered: list[dict] = []
    png_dir = output_dir / 'png'
    for cluster_id, _area, pieces, union in candidates:
        centroid = union.centroid
        name = (
            f'cell_{cell_id}_cluster_{cluster_id:05d}_'
            f'{centroid.y:+09.4f}_{centroid.x:+010.4f}.png'
        )
        try:
            rec = common.render_cluster(
                cluster_id,
                pieces,
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
        else args.min_cluster_km2 / 20.0
    )

    user_bbox = tuple(args.bbox) if args.bbox else None
    cells = common.select_cells(user_bbox)
    parallel = common.auto_parallelism(args.parallel_cells, len(cells))

    LOGGER.info(
        'Patches: %s | cells=%d | workers=%d | '
        'prefilter=%g km² | min_cluster=%g km² | max_images/cell=%d',
        args.patches,
        len(cells),
        parallel,
        prefilter_km2,
        args.min_cluster_km2,
        args.max_images,
    )

    worker_kwargs = {
        'patches_path': args.patches,
        'output_dir': args.output,
        'min_cluster_km2': args.min_cluster_km2,
        'cluster_buffer_km': args.cluster_buffer_km,
        'prefilter_km2': prefilter_km2,
        'dpi': args.dpi,
        'with_basemap': args.basemap,
        'max_images': args.max_images,
        'basemap_provider': args.basemap_provider,
        'basemap_cache_dir': args.basemap_cache_dir,
    }

    records = common.run_cells_parallel(
        cells, _process_cell, worker_kwargs, parallel
    )

    if not records:
        LOGGER.warning(
            'No cluster met the --min-cluster-km2=%g threshold.',
            args.min_cluster_km2,
        )
        return

    total_km2 = sum(r['added_km2'] for r in records)
    summary = (
        f'{len(records)} cluster(s) rendered across {len(cells)} '
        f'geohash-1 cell(s) (total added: {total_km2:.2f} km²) '
        f'from <code>{args.patches}</code>.'
    )
    common.write_atlas(args.output, records, summary)
    LOGGER.info('Done. %d image(s) written to %s', len(records), args.output)


if __name__ == '__main__':
    main()

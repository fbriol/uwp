#!/usr/bin/env python3

import argparse
import concurrent.futures
import json
import os
import pathlib
import subprocess
import shutil
import threading
import urllib.error
import urllib.request
import zipfile
import sys
import logging
import tempfile

#: The logger for this module
LOGGER = logging.getLogger(__name__)

#: OpenStreetMap Geofabrik sub-regions
AREAS = {
    'albania': 'europe',
    'azores': 'europe',
    'belgium': 'europe',
    # 'bosnia-herzegovina': 'europe',
    'bulgaria': 'europe',
    'croatia': 'europe',
    'cyprus': 'europe',
    'denmark': 'europe',
    'estonia': 'europe',
    'faroe-islands': 'europe',
    'finland': 'europe',
    'france': 'europe',
    'germany': 'europe',
    'greece': 'europe',
    # 'guernsey-jersey': 'europe',
    'iceland': 'europe',
    'ireland-and-northern-ireland': 'europe',
    'isle-of-man': 'europe',
    'italy': 'europe',
    'latvia': 'europe',
    'lithuania': 'europe',
    'malta': 'europe',
    # 'monaco': 'europe',
    'montenegro': 'europe',
    'netherlands': 'europe',
    'norway': 'europe',
    'poland': 'europe',
    'portugal': 'europe',
    'romania': 'europe',
    'russia': '',
    'slovenia': 'europe',
    'spain': 'europe',
    'sweden': 'europe',
    'turkey': 'europe',
    'ukraine': 'europe',
    'united-kingdom': 'europe',
    # 'alberta': 'north-america/canada',
    'british-columbia': 'north-america/canada',
    'manitoba': 'north-america/canada',
    'new-brunswick': 'north-america/canada',
    'newfoundland-and-labrador': 'north-america/canada',
    'northwest-territories': 'north-america/canada',
    'nova-scotia': 'north-america/canada',
    'nunavut': 'north-america/canada',
    'ontario': 'north-america/canada',
    'prince-edward-island': 'north-america/canada',
    'quebec': 'north-america/canada',
    # 'saskatchewan': 'north-america/canada',
    'yukon': 'north-america/canada',
    'alabama': 'north-america/us',
    'alaska': 'north-america/us',
    # 'arizona': 'north-america/us',
    # 'arkansas': 'north-america/us',
    'california': 'north-america/us',
    # 'colorado': 'north-america/us',
    'connecticut': 'north-america/us',
    'delaware': 'north-america/us',
    # 'district-of-columbia': 'north-america/us',
    'florida': 'north-america/us',
    'georgia': 'north-america/us',
    'hawaii': 'north-america/us',
    # 'idaho': 'north-america/us',
    # 'illinois': 'north-america/us',
    # 'indiana': 'north-america/us',
    # 'iowa': 'north-america/us',
    # 'kansas': 'north-america/us',
    # 'kentucky': 'north-america/us',
    'louisiana': 'north-america/us',
    'maine': 'north-america/us',
    'maryland': 'north-america/us',
    'massachusetts': 'north-america/us',
    # 'michigan': 'north-america/us',
    # 'minnesota': 'north-america/us',
    'mississippi': 'north-america/us',
    # 'missouri': 'north-america/us',
    # 'montana': 'north-america/us',
    # 'nebraska': 'north-america/us',
    # 'nevada': 'north-america/us',
    'new-hampshire': 'north-america/us',
    'new-jersey': 'north-america/us',
    # 'new-mexico': 'north-america/us',
    'new-york': 'north-america/us',
    'north-carolina': 'north-america/us',
    # 'north-dakota': 'north-america/us',
    # 'ohio': 'north-america/us',
    # 'oklahoma': 'north-america/us',
    'oregon': 'north-america/us',
    'pennsylvania': 'north-america/us',
    'puerto-rico': 'north-america/us',
    'rhode-island': 'north-america/us',
    'south-carolina': 'north-america/us',
    # 'south-dakota': 'north-america/us',
    # 'tennessee': 'north-america/us',
    'texas': 'north-america/us',
    # 'us-virgin-islands': 'north-america/us',
    # 'utah': 'north-america/us',
    # 'vermont': 'north-america/us',
    'virginia': 'north-america/us',
    'washington': 'north-america/us',
    # 'west-virginia': 'north-america/us',
    # 'wisconsin': 'north-america/us',
    # 'wyoming': 'north-america/us',
    'greenland': 'north-america',
    'mexico': 'north-america',
    'africa': '',
    'antarctica': '',
    'asia': '',
    'australia-oceania': '',
    'central-america': '',
    'south-america': '',
}

#: The base URL for downloading OpenStreetMap data
GEOFABRIK_URL = 'https://download.geofabrik.de'

#: The base URL for OpenStreetMap data
OSM_URL = 'https://osmdata.openstreetmap.de'

#: The root directory of the project
ROOT = pathlib.Path(__file__).parent.parent

#: This is the directory where the data are handled
DATA_DIR = ROOT / 'data'

#: Where the OSM data are downloaded
OSM_DATA_DIR = DATA_DIR / 'osm-pbf'

#: Where the water polygons are stored
WATER_POLYGON_DIR = DATA_DIR / 'shapefiles'

#: Path to the JSON manifest tracking upstream Last-Modified / ETag for each
#: region. Drives incremental updates: a region is only re-downloaded and
#: re-extracted when its upstream metadata differs from what's stored here.
MANIFEST_PATH = DATA_DIR / 'manifest.json'

#: User-Agent for HEAD/GET requests against Geofabrik & osmdata. Geofabrik
#: ask scripts to identify themselves with a contact URL or email.
USER_AGENT = 'uwp-cnes/1.0 (+https://github.com/CNES; contact via project)'

#: Sentinel value in the manifest for "never seen before". Avoids a separate
#: `None` check in the comparison logic.
_MANIFEST_MISSING = {'last_modified': None, 'etag': None}

#: Lock protecting concurrent updates to the in-memory manifest from worker
#: threads. The manifest itself is a plain dict, so we serialise writes.
_MANIFEST_LOCK = threading.Lock()


def _install_default_opener() -> None:
    """Register a global URL opener that sends our identifying User-Agent
    with every request. Geofabrik asks scripts to identify themselves; sending
    a generic urllib/Python UA risks being throttled or blocked."""
    opener = urllib.request.build_opener()
    opener.addheaders = [('User-Agent', USER_AGENT)]
    urllib.request.install_opener(opener)


def download_file(url: str, output_file: str) -> None:
    """Download file with progress reporting"""
    LOGGER.info('Downloading %s to %s', url, output_file)

    last_percent_reported = -1

    def report_progress(block_count, block_size, total_size):
        downloaded = block_count * block_size
        percent = int(downloaded * 100 / total_size) if total_size > 0 else 0
        percent = min(percent, 100)
        nonlocal last_percent_reported
        # Log only if the percentage increased by at least 10% or reached 100%
        if percent - last_percent_reported >= 10 or percent == 100:
            last_percent_reported = percent
            LOGGER.info('%s: %d%% complete', output_file, percent)

    try:
        urllib.request.urlretrieve(
            url,
            output_file,
            reporthook=report_progress,
        )
    except OSError:
        # Try to remove the partially downloaded file then raise the error
        pathlib.Path(output_file).unlink(missing_ok=True)
        raise

    LOGGER.info('Download complete: %s', output_file)


# ---------------------------------------------------------------------------
# Manifest-based incremental update
# ---------------------------------------------------------------------------
#
# The manifest is a JSON object keyed by a stable region identifier
# (sub_region/region or the bare region name when sub_region is empty),
# storing the upstream HTTP `Last-Modified` and `ETag` we observed last time
# we successfully downloaded that PBF. A region is considered fresh if either
# value still matches the upstream HEAD response. If neither matches, or if
# we have no record at all, the region's cached PBF and extracted shapefile
# are deleted and the work is redone.
#
# The manifest is written atomically (tmp file + rename) only once at the end
# of a successful run. If a download fails midway, the manifest is left
# untouched so the next run retries the failed regions.


def remote_metadata(url: str, timeout: float = 30.0) -> dict:
    """HEAD-request the URL and return its caching metadata.

    Returns a dict with `last_modified` and `etag` keys (either may be None
    if the server didn't send the header).
    """
    req = urllib.request.Request(url, method='HEAD')
    req.add_header('User-Agent', USER_AGENT)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return {
            'last_modified': resp.headers.get('Last-Modified'),
            'etag': resp.headers.get('ETag'),
        }


def load_manifest() -> dict:
    """Load the manifest from disk, or return an empty one."""
    if not MANIFEST_PATH.exists():
        return {}
    try:
        with MANIFEST_PATH.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning(
            'Manifest at %s is unreadable (%s) — treating as empty',
            MANIFEST_PATH,
            exc,
        )
        return {}


def save_manifest(manifest: dict) -> None:
    """Persist the manifest to disk atomically."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix('.json.tmp')
    with tmp.open('w') as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    tmp.replace(MANIFEST_PATH)


def _manifest_key(region: str, sub_region: str) -> str:
    return f'{sub_region}/{region}' if sub_region else region


def _delete_shp_set(shp: pathlib.Path) -> None:
    """Delete a shapefile and its sidecars (.dbf, .shx, .prj, .cpg)."""
    for ext in ('.shp', '.dbf', '.shx', '.prj', '.cpg'):
        sidecar = shp.with_suffix(ext)
        if sidecar.exists():
            sidecar.unlink()


def _delete_region_cache(region: str, sub_region: str) -> None:
    """Remove cached PBF + extracted intermediate PBF + extracted shapefile
    for a region. Called when upstream changed or when the user passes
    --force."""
    pbf = osm_pbf_sub_region(region, sub_region)
    intermediate_pbf = pbf.parent / f'{region}-water.osm.pbf'
    for p in (pbf, intermediate_pbf):
        if p.exists():
            p.unlink()
    _delete_shp_set(shp_sub_region(region, sub_region))


def _metadata_matches(remote: dict, cached: dict) -> bool:
    """A region is considered fresh if EITHER the Last-Modified OR the ETag
    matches the cached value. We accept "either" because Geofabrik
    occasionally rebuilds files without bumping Last-Modified (or vice versa),
    and forcing both to match would cause spurious re-downloads."""
    if remote.get('etag') and remote['etag'] == cached.get('etag'):
        return True
    if remote.get('last_modified') and remote['last_modified'] == cached.get(
        'last_modified'
    ):
        return True
    return False


def check_region_freshness(
    region: str, sub_region: str, manifest: dict, force: bool = False
) -> tuple[bool, dict]:
    """Decide whether `region` needs (re-)downloading.

    Returns `(needs_refresh, remote_meta)`. The remote metadata is returned
    so the caller can record it in the manifest once the refresh succeeds.

    - If `force` is set, always refresh.
    - If upstream is unreachable, fall back to the cache: the run continues
      with whatever local data we have, and no manifest update is performed
      for that region.
    """
    url = region_url(region, sub_region)
    key = _manifest_key(region, sub_region)

    if force:
        LOGGER.info('%s: --force → refresh', key)
        _delete_region_cache(region, sub_region)
        try:
            return True, remote_metadata(url)
        except (urllib.error.URLError, TimeoutError):
            return True, {}

    try:
        remote = remote_metadata(url)
    except (urllib.error.URLError, TimeoutError) as exc:
        LOGGER.warning(
            '%s: cannot reach upstream (%s) — keeping cached version',
            key,
            exc,
        )
        return False, {}

    cached = manifest.get(key, _MANIFEST_MISSING)
    pbf = osm_pbf_sub_region(region, sub_region)
    shp = shp_sub_region(region, sub_region)
    have_local = pbf.exists() or shp.exists()

    if have_local and _metadata_matches(remote, cached):
        LOGGER.info(
            '%s: up to date (Last-Modified=%s)',
            key,
            remote.get('last_modified'),
        )
        return False, remote

    if cached is _MANIFEST_MISSING or not have_local:
        LOGGER.info('%s: first download', key)
    else:
        LOGGER.info(
            '%s: upstream changed (Last-Modified %s → %s) — refreshing',
            key,
            cached.get('last_modified'),
            remote.get('last_modified'),
        )

    _delete_region_cache(region, sub_region)
    return True, remote


def record_region_metadata(
    manifest: dict, region: str, sub_region: str, remote_meta: dict
) -> None:
    """Thread-safely record a successful refresh in the in-memory manifest."""
    if not remote_meta:
        return
    key = _manifest_key(region, sub_region)
    with _MANIFEST_LOCK:
        manifest[key] = {
            'last_modified': remote_meta.get('last_modified'),
            'etag': remote_meta.get('etag'),
            'url': region_url(region, sub_region),
        }


def region_url(region: str, sub_region: str) -> str:
    """Build the Geofabrik URL for a region's `-latest.osm.pbf`."""
    if sub_region:
        return f'{GEOFABRIK_URL}/{sub_region}/{region}-latest.osm.pbf'
    return f'{GEOFABRIK_URL}/{region}-latest.osm.pbf'


def osm_pbf_sub_region(region: str, sub_region: str) -> pathlib.Path:
    """Get the path to the DBF file for the specified region"""
    if sub_region:
        return OSM_DATA_DIR / sub_region / f'{region}.osm.pbf'
    return OSM_DATA_DIR / f'{region}.osm.pbf'


def shp_sub_region(region: str, sub_region: str) -> pathlib.Path:
    """Get the path to the SHP file for the specified region"""
    if sub_region:
        return WATER_POLYGON_DIR / sub_region / region / 'water.shp'
    return WATER_POLYGON_DIR / region / 'water.shp'


def water_polygon_path() -> pathlib.Path:
    """Get the path to the water polygons directory"""
    return DATA_DIR / 'water-polygons-split-4326'


def water_polygon_shp() -> pathlib.Path:
    """Get the path to the water polygons directory"""
    return water_polygon_path() / 'water_polygons.shp'


def download_sub_region(region: str, sub_region: str) -> None:
    """Download the specified sub-region from Geofabrik.

    Caching is handled upstream by `check_region_freshness`, which deletes
    stale files before this function is called. So if the PBF already
    exists when we get here, it is by definition still valid (e.g. another
    region in the same run already touched it, or a previous run downloaded
    it and the upstream hasn't changed). We can safely short-circuit.
    """
    output_file = osm_pbf_sub_region(region, sub_region)
    shp = shp_sub_region(region, sub_region)
    if output_file.exists() or shp.exists():
        LOGGER.info('%s already present, skipping download', output_file)
        return
    output_file.parent.mkdir(parents=True, exist_ok=True)
    download_file(region_url(region, sub_region), str(output_file))


def corrected_water_polygon_path() -> pathlib.Path:
    """Get the path to the corrected water polygons directory"""
    return DATA_DIR / 'corrected-water-polygons.shp'


def tmp_polygon_path() -> pathlib.Path:
    """Get the path to the corrected water polygons directory"""
    return DATA_DIR / 'tmp-water-polygons.shp'


def copy_shp(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Copy the SHP file and its associated files"""
    shutil.copy(src, dst)
    dbf = src.with_suffix('.dbf')
    shutil.copy(dbf, dst.with_suffix('.dbf'))
    prj = src.with_suffix('.prj')
    shutil.copy(prj, dst.with_suffix('.prj'))
    shx = src.with_suffix('.shx')
    shutil.copy(shx, dst.with_suffix('.shx'))


def move_shp(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Move the SHP file and its associated files"""
    shutil.move(src, dst)
    dbf = src.with_suffix('.dbf')
    shutil.move(dbf, dst.with_suffix('.dbf'))
    prj = src.with_suffix('.prj')
    if prj.exists():
        shutil.move(prj, dst.with_suffix('.prj'))
    shx = src.with_suffix('.shx')
    if shx.exists():
        shutil.move(shx, dst.with_suffix('.shx'))


def initialize_working_directory() -> pathlib.Path:
    """Initialize the working directory"""
    corrected_shp = corrected_water_polygon_path()
    if corrected_shp.exists():
        corrected_shp.unlink()
    water_polygon = water_polygon_shp()
    copy_shp(water_polygon, corrected_shp)

    return corrected_shp


def convert_to_shp(region: str, sub_region: str) -> None:
    """Convert the OSM PBF file to SHP format"""
    water_shp = shp_sub_region(region, sub_region)
    if water_shp.exists():
        LOGGER.info(
            'Shapefile for %s already exists, skipping conversion',
            region,
        )
        return
    water_shp.parent.mkdir(parents=True, exist_ok=True)
    osm_pbf = osm_pbf_sub_region(region, sub_region)
    water_pbf = osm_pbf.parent / f'{region}-water.osm.pbf'

    ogr_env = os.environ.copy()
    ogr_env['OGR_GEOMETRY_ACCEPT_UNCLOSED_RING'] = 'NO'

    subprocess.run(
        [
            'osmium',
            'tags-filter',
            str(osm_pbf),
            'w',
            'natural=water',
            'r',
            'natural=waterway',
            '-o',
            str(water_pbf),
            '--overwrite',
        ],
        check=True,
    )
    subprocess.run(
        [
            'ogr2ogr',
            '-f',
            'ESRI Shapefile',
            f'{water_shp}',
            str(water_pbf),
            'multipolygons',
        ],
        env=ogr_env,
        check=True,
    )


#: Manifest key for the global water polygons archive.
_BASE_WATER_KEY = 'base/water-polygons-split-4326.zip'


def download_water_polygons(manifest: dict, force: bool = False) -> None:
    """Download and extract the global water polygons archive.

    Uses the same manifest-based freshness check as the regional PBFs: if
    the upstream Last-Modified/ETag still matches, the existing extracted
    files are kept as-is.
    """
    url = f'{OSM_URL}/download/water-polygons-split-4326.zip'
    shp = water_polygon_shp()

    cached = manifest.get(_BASE_WATER_KEY, _MANIFEST_MISSING)
    try:
        remote = remote_metadata(url)
    except (urllib.error.URLError, TimeoutError) as exc:
        if shp.exists() and not force:
            LOGGER.warning(
                'Cannot reach %s (%s) — using cached water polygons', url, exc
            )
            return
        raise

    if not force and shp.exists() and _metadata_matches(remote, cached):
        LOGGER.info(
            'Base water polygons up to date (Last-Modified=%s)',
            remote.get('last_modified'),
        )
        return

    reason = (
        'first download' if cached is _MANIFEST_MISSING else 'upstream changed'
    )
    LOGGER.info('Base water polygons: %s', reason)

    # Remove the previous extraction so we don't leave half-stale files
    # mixed with new ones.
    extract_dir = water_polygon_path()
    if extract_dir.exists():
        shutil.rmtree(extract_dir)

    output_file = pathlib.Path(tempfile.gettempdir())
    output_file /= 'water-polygons-split-4326.zip'
    download_file(url, str(output_file))
    folder = water_polygon_path().parent
    with zipfile.ZipFile(output_file, 'r') as zip_ref:
        zip_ref.extractall(folder)
    LOGGER.info('Extracted water polygons to %s', folder)
    os.remove(output_file)

    with _MANIFEST_LOCK:
        manifest[_BASE_WATER_KEY] = {
            'last_modified': remote.get('last_modified'),
            'etag': remote.get('etag'),
            'url': url,
        }


def usage() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Update water polygons from OpenStreetMap to include '
        'estuaries and missing polygons.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    choices = tuple(AREAS)
    parser.add_argument(
        '--areas',
        choices=choices,
        default=choices,
        nargs='+',
        help='Areas to process. Defaults to all areas.',
    )
    parser.add_argument(
        '--uwp',
        type=str,
        default=str(ROOT / 'build' / 'uwp'),
        help='Path to the UWP executable.',
    )
    parser.add_argument(
        '--jobs',
        type=int,
        default=4,
        help='Number of concurrent downloads. Download is network-bound and '
        'parallelises well. Use 1 to restore the original sequential '
        'behaviour.',
    )
    parser.add_argument(
        '--extract-jobs',
        type=int,
        default=2,
        help='Number of concurrent osmium/ogr2ogr extractions. osmium is '
        'already multi-threaded internally, so running too many extractions '
        'in parallel oversubscribes the CPU. 2 is a safe default; raise it '
        'if the machine is mostly idle during extractions.',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Ignore the manifest and force re-download + re-extraction of '
        'every selected region. Use after dependency upgrades or to recover '
        'from a corrupted cache.',
    )
    parser.add_argument(
        '--check-only',
        action='store_true',
        help='Print which regions would be refreshed and exit, without '
        'downloading anything or running the C++ binary. Useful to estimate '
        'the size of an incremental update before launching it.',
    )
    return parser.parse_args()


def _check_runtime_dependencies(uwp_path: str) -> None:
    """Verify the external tools the script depends on are reachable."""
    for tool, hint in (
        ('osmium', 'Please install osmium.'),
        ('ogr2ogr', 'Please install GDAL.'),
    ):
        if not shutil.which(tool):
            LOGGER.error('%s is not installed. %s', tool, hint)
            sys.exit(1)
    if not shutil.which(uwp_path):
        LOGGER.error('%s does not exist. Please check the path.', uwp_path)


def _plan_refresh(
    selected_areas: tuple, manifest: dict, force: bool
) -> tuple[list[tuple[str, str]], dict[str, dict]]:
    """HEAD-poll Geofabrik for each selected region and return the list of
    regions that need refreshing together with their pending manifest
    metadata. Stale on-disk caches are deleted as a side effect."""
    candidates = [
        (region, sub_region)
        for region, sub_region in AREAS.items()
        if region in selected_areas
    ]
    LOGGER.info(
        'Checking upstream freshness for %d region(s)…',
        len(candidates),
    )

    pending_metadata: dict[str, dict] = {}
    regions_to_process: list[tuple[str, str]] = []
    for region, sub_region in candidates:
        needs_refresh, remote = check_region_freshness(
            region, sub_region, manifest, force=force
        )
        if needs_refresh:
            regions_to_process.append((region, sub_region))
            if remote:
                pending_metadata[_manifest_key(region, sub_region)] = remote

    LOGGER.info(
        '%d region(s) need refreshing; %d up to date',
        len(regions_to_process),
        len(candidates) - len(regions_to_process),
    )
    return regions_to_process, pending_metadata


def _refresh_regions_parallel(
    regions_to_process: list[tuple[str, str]],
    manifest: dict,
    pending_metadata: dict[str, dict],
    download_jobs: int,
    extract_jobs: int,
) -> None:
    """Two-stage producer/consumer pipeline.

    Stage 1 (download_pool, network-bound): several downloads run in
    parallel; they don't compete for CPU.
    Stage 2 (extract_pool, CPU-bound): osmium tags-filter + ogr2ogr. osmium
    is internally multi-threaded so this pool is kept small to avoid
    oversubscription.
    Stages overlap: as soon as a download finishes, its extraction is
    submitted to stage 2 and the download thread returns to the next URL.
    The manifest is only committed when both the download and extraction
    of a region succeed.
    """

    def _commit(region: str, sub_region: str) -> None:
        meta = pending_metadata.get(_manifest_key(region, sub_region))
        if meta:
            record_region_metadata(manifest, region, sub_region, meta)

    # Threads (not processes): work is dominated by subprocess.run and
    # urllib I/O, which both release the GIL.
    with (
        concurrent.futures.ThreadPoolExecutor(
            max_workers=download_jobs,
            thread_name_prefix='download',
        ) as download_pool,
        concurrent.futures.ThreadPoolExecutor(
            max_workers=extract_jobs,
            thread_name_prefix='extract',
        ) as extract_pool,
    ):

        def download_then_submit_extract(
            region: str, sub_region: str
        ) -> concurrent.futures.Future:
            download_sub_region(region, sub_region)
            ex_future = extract_pool.submit(convert_to_shp, region, sub_region)

            def _on_extract_done(fut: concurrent.futures.Future) -> None:
                if fut.exception() is None:
                    _commit(region, sub_region)

            ex_future.add_done_callback(_on_extract_done)
            return ex_future

        download_futures = {
            download_pool.submit(
                download_then_submit_extract, region, sub_region
            ): region
            for region, sub_region in regions_to_process
        }

        extract_futures: list[concurrent.futures.Future] = []
        try:
            # Collect extract futures as downloads complete so extractions
            # can start without waiting for every download to finish.
            for dl_future in concurrent.futures.as_completed(download_futures):
                region = download_futures[dl_future]
                try:
                    extract_futures.append(dl_future.result())
                except Exception:
                    LOGGER.exception('Download failed for region %s', region)
                    raise

            for ex_future in concurrent.futures.as_completed(extract_futures):
                ex_future.result()
        except Exception:
            # Cancel anything not yet started. In-flight subprocesses cannot
            # be interrupted; their results are discarded at pool shutdown.
            for pending in download_futures:
                pending.cancel()
            for pending in extract_futures:
                pending.cancel()
            raise


def _refresh_regions(
    regions_to_process: list[tuple[str, str]],
    manifest: dict,
    pending_metadata: dict[str, dict],
    download_jobs: int,
    extract_jobs: int,
) -> None:
    """Download + extract every region in `regions_to_process`. Picks the
    sequential or the parallel path based on the requested job counts and
    the size of the work list."""
    if not regions_to_process:
        LOGGER.info('Nothing to refresh in the regional shapefiles.')
        return

    sequential = (download_jobs == 1 and extract_jobs == 1) or len(
        regions_to_process
    ) <= 1
    if sequential:
        for region, sub_region in regions_to_process:
            download_sub_region(region, sub_region)
            convert_to_shp(region, sub_region)
            meta = pending_metadata.get(_manifest_key(region, sub_region))
            if meta:
                record_region_metadata(manifest, region, sub_region, meta)
        return

    _refresh_regions_parallel(
        regions_to_process,
        manifest,
        pending_metadata,
        download_jobs,
        extract_jobs,
    )


def _run_uwp(uwp_path: str, selected_areas: tuple) -> None:
    """Initialise the working copy of the base water shapefile and invoke
    the C++ uwp binary on the full set of regional shapefiles that exist
    on disk for the selected areas. The merge phase always runs on the full
    set because `bg::union_` is not invertible."""
    target = initialize_working_directory()
    water_shapefiles = [
        str(shp_sub_region(region, sub_region))
        for region, sub_region in AREAS.items()
        if region in selected_areas
        and shp_sub_region(region, sub_region).exists()
    ]
    LOGGER.info(
        'Running %s on %d regional shapefile(s)',
        uwp_path,
        len(water_shapefiles),
    )
    subprocess.run(
        [uwp_path, str(target), '-o', str(target), *water_shapefiles],
        check=True,
    )
    LOGGER.info('Done. Output: %s', target)


def main():
    """Main function to download and convert water polygons"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
    )
    args = usage()
    _check_runtime_dependencies(args.uwp)

    LOGGER.info('Starting water polygon update')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Install a global URL opener carrying our identifying User-Agent so
    # Geofabrik can attribute traffic correctly. Used by both urlopen (HEAD)
    # and urlretrieve (downloads).
    _install_default_opener()

    manifest = load_manifest()
    regions_to_process, pending_metadata = _plan_refresh(
        args.areas, manifest, force=args.force
    )

    if args.check_only:
        for region, sub_region in regions_to_process:
            print(_manifest_key(region, sub_region))
        return

    try:
        _refresh_regions(
            regions_to_process,
            manifest,
            pending_metadata,
            download_jobs=max(1, args.jobs),
            extract_jobs=max(1, args.extract_jobs),
        )
        download_water_polygons(manifest, force=args.force)
    finally:
        # Persist whatever progress we made, even if a later step fails:
        # the next run picks up where we left off without re-downloading
        # the regions that already succeeded.
        save_manifest(manifest)

    _run_uwp(args.uwp, args.areas)


if __name__ == '__main__':
    main()

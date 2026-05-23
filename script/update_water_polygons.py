#!/usr/bin/env python3

import argparse
import concurrent.futures
import http.client
import json
import os
import pathlib
import random
import socket
import subprocess
import shutil
import threading
import time
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

#: Custom osmconf.ini for ogr2ogr. GDAL's stock config does not expose the
#: `water=*` tag as a queryable column on the multipolygons layer, so the
#: -where filter below would error with "water not recognised as an
#: available field". Our version adds it.
OSM_CONFIG_FILE = pathlib.Path(__file__).parent / 'osmconf.ini'

#: Path to the JSON manifest tracking upstream Last-Modified / ETag for each
#: region. Drives incremental updates: a region is only re-downloaded and
#: re-extracted when its upstream metadata differs from what's stored here.
MANIFEST_PATH = DATA_DIR / 'manifest.json'

#: User-Agent for HEAD/GET requests against Geofabrik & osmdata. Geofabrik
#: ask scripts to identify themselves with a contact URL or email.
USER_AGENT = 'uwp-cnes/1.0 (+https://github.com/CNES; contact via project)'

#: Default maximum number of retry attempts for a single download or HEAD
#: request before giving up. Each attempt waits longer than the previous
#: one (see `_BACKOFF_*`).
DEFAULT_MAX_RETRIES = 5

#: Effective retry budget actually used at runtime. Rebound from `main()`
#: based on the `--max-retries` CLI flag so we don't have to thread the
#: value through five layers of orchestration functions.
_MAX_RETRIES = DEFAULT_MAX_RETRIES

#: Exponential backoff bounds (seconds). Successive sleeps are computed as
#: min(_BACKOFF_INITIAL * 2**attempt, _BACKOFF_MAX) with a small random jitter
#: added on top so concurrent workers don't synchronously retry against the
#: same server (thundering herd).
_BACKOFF_INITIAL = 5.0
_BACKOFF_MAX = 120.0

#: Network exception classes that warrant a retry. These cover the common
#: transient failures seen against Geofabrik:
#:   * ContentTooShortError: connection dropped mid-transfer (subclass of
#:     URLError, which is itself an OSError).
#:   * IncompleteRead: chunked response cut short.
#:   * URLError: DNS/TCP/SSL failures, including TimeoutError on connect.
#:   * socket.timeout: read timeout once the response started.
#:   * ConnectionError: TCP reset / pipe broken.
#: HTTP 4xx and 5xx errors are NOT retried automatically — they almost always
#: signal a real problem (URL typo, region renamed upstream, etc.) and a retry
#: just wastes time.
_RETRYABLE_EXCEPTIONS = (
    urllib.error.ContentTooShortError,
    http.client.IncompleteRead,
    urllib.error.URLError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
)

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


def _is_retryable(exc: BaseException) -> bool:
    """Decide whether a network exception is worth retrying.

    HTTP 4xx responses are *not* retried: they signal a real upstream
    problem (renamed region, typo, removed dataset, …) and retrying just
    wastes time. HTTP 5xx, on the other hand, often clear up on their own
    and are retried like any other transient failure.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return 500 <= exc.code < 600
    return isinstance(exc, _RETRYABLE_EXCEPTIONS)


def _retry(
    func,
    *,
    what: str,
    max_attempts: int = DEFAULT_MAX_RETRIES,
    on_retry=None,
):
    """Call `func()` with exponential backoff + jitter.

    Retries up to `max_attempts` times when `_is_retryable` returns True.
    `on_retry`, if provided, is invoked between attempts with the failed
    attempt number — useful to clean up partial state (e.g. delete a
    half-written download).
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as exc:
            if attempt >= max_attempts or not _is_retryable(exc):
                raise
            delay = min(_BACKOFF_INITIAL * (2 ** (attempt - 1)), _BACKOFF_MAX)
            # Jitter ±20% so concurrent workers don't all retry in lockstep.
            delay *= 1.0 + random.uniform(-0.2, 0.2)
            LOGGER.warning(
                '%s failed (attempt %d/%d): %s. Retrying in %.1fs…',
                what,
                attempt,
                max_attempts,
                exc,
                delay,
            )
            if on_retry is not None:
                try:
                    on_retry(attempt)
                except Exception:
                    LOGGER.exception(
                        'on_retry cleanup raised; continuing with retry'
                    )
            time.sleep(delay)


def download_file(
    url: str,
    output_file: str,
    max_attempts: int | None = None,
) -> None:
    """Download `url` to `output_file` with progress reporting and retries.

    Transient network failures (connection reset, partial content, read
    timeout, 5xx, …) trigger an exponential backoff and retry. Any partial
    download is removed before each retry so urlretrieve starts fresh —
    Geofabrik's HTTP server does not advertise reliable Range support, so
    a clean re-download is more predictable than trying to resume.
    """
    LOGGER.info('Downloading %s to %s', url, output_file)
    if max_attempts is None:
        max_attempts = _MAX_RETRIES
    output_path = pathlib.Path(output_file)

    def _cleanup(_attempt: int) -> None:
        output_path.unlink(missing_ok=True)

    def _do_download() -> None:
        last_percent_reported = -1

        def report_progress(block_count, block_size, total_size):
            downloaded = block_count * block_size
            percent = (
                int(downloaded * 100 / total_size) if total_size > 0 else 0
            )
            percent = min(percent, 100)
            nonlocal last_percent_reported
            if percent - last_percent_reported >= 10 or percent == 100:
                last_percent_reported = percent
                LOGGER.info('%s: %d%% complete', output_file, percent)

        try:
            urllib.request.urlretrieve(
                url, output_file, reporthook=report_progress
            )
        except BaseException:
            # Always clear partial state on failure — even non-retryable
            # ones — so we never leave a truncated file behind.
            output_path.unlink(missing_ok=True)
            raise

    _retry(
        _do_download,
        what=f'Download {url}',
        max_attempts=max_attempts,
        on_retry=_cleanup,
    )
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


def remote_metadata(
    url: str,
    timeout: float = 30.0,
    max_attempts: int | None = None,
) -> dict:
    """HEAD-request the URL and return its caching metadata.

    Returns a dict with `last_modified` and `etag` keys (either may be None
    if the server didn't send the header).

    Wrapped in the same retry/backoff machinery as `download_file`: HEAD
    requests are short-lived but can still fail transiently when the
    upstream is busy or when a corporate proxy intermittently drops
    connections (typical on the CNES cluster).
    """
    if max_attempts is None:
        max_attempts = _MAX_RETRIES

    def _do_head() -> dict:
        req = urllib.request.Request(url, method='HEAD')
        req.add_header('User-Agent', USER_AGENT)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                'last_modified': resp.headers.get('Last-Modified'),
                'etag': resp.headers.get('ETag'),
            }

    return _retry(_do_head, what=f'HEAD {url}', max_attempts=max_attempts)


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
    have_shp = shp.exists()
    have_pbf = pbf.exists()
    have_local = have_pbf or have_shp

    if have_shp and _metadata_matches(remote, cached):
        # Nominal case: both the shp and the manifest entry agree with
        # upstream. Nothing to do.
        LOGGER.info(
            '%s: up to date (Last-Modified=%s)',
            key,
            remote.get('last_modified'),
        )
        return False, remote

    if have_pbf and _metadata_matches(remote, cached) and not have_shp:
        # Manifest agrees with upstream and the source PBF is still on
        # disk, but the shapefile was deleted. Re-extract from the cached
        # PBF — no need to re-download. The PBF is left in place: the
        # downstream `download_sub_region` will short-circuit on it, and
        # `convert_to_shp` will rebuild the shp from it.
        LOGGER.info(
            '%s: shapefile missing — re-extracting from cached PBF',
            key,
        )
        return True, remote

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
    # Accept rings whose last vertex doesn't repeat the first. GDAL closes
    # them automatically (appends the start vertex at the end). The
    # alternative — setting this to NO — has GDAL print
    #   ERROR 1: Non closed ring detected.
    # for every offending feature and *drop* it. OSM multipolygon relations
    # often have a tiny ring-closure bug at tile boundaries; rejecting them
    # makes us lose the very estuary / lagoon we want to merge into the
    # coastline. Auto-closing produces a valid polygon that bg::union_
    # downstream handles without issue.
    ogr_env['OGR_GEOMETRY_ACCEPT_UNCLOSED_RING'] = 'YES'
    # Point GDAL's OSM driver at our custom config that exposes the
    # `water` tag. Without it the -where filter below fails because GDAL's
    # default config doesn't surface `water=*` as a column.
    if OSM_CONFIG_FILE.exists():
        ogr_env['OSM_CONFIG_FILE'] = str(OSM_CONFIG_FILE)
    else:
        LOGGER.warning(
            'osmconf.ini not found at %s — `water` tag may be unavailable',
            OSM_CONFIG_FILE,
        )

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
    # ogr2ogr `-where` filter: drop the water sub-types that are too narrow
    # or too purely inland to contribute usefully to the coastline. Streams,
    # canals, ditches and drains are typically modelled as long thin
    # polygons that, if absorbed into the coast, drag it deep into the
    # mainland (the "river going to Paris" pathology). Polygons without a
    # `water=*` tag — most lakes, bays, lagoons — are kept (the
    # `water IS NULL` clause).
    excluded_water = ('stream', 'canal', 'ditch', 'drain')
    where_clause = (
        'water IS NULL OR water NOT IN ('
        + ', '.join(f"'{w}'" for w in excluded_water)
        + ')'
    )
    subprocess.run(
        [
            'ogr2ogr',
            '-f',
            'ESRI Shapefile',
            f'{water_shp}',
            str(water_pbf),
            'multipolygons',
            '-where',
            where_clause,
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
    parser.add_argument(
        '--max-retries',
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help='Maximum number of retry attempts for a single download or '
        'HEAD request before giving up. Exponential backoff with jitter '
        'is applied between attempts.',
    )
    parser.add_argument(
        '--max-inland-km',
        type=float,
        default=0.0,
        help='Maximum distance (in kilometres) a regional water polygon may '
        'extend beyond the bounding box of the coastal water polygon it is '
        'merged into. Anything farther is clipped — useful to stop river '
        'polygons modelled as one giant feature from dragging the coastline '
        'hundreds of km inland. 0 disables the cap. Typical values: 100-500.',
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


def _run_uwp(
    uwp_path: str, selected_areas: tuple, max_inland_km: float = 0.0
) -> None:
    """Initialise the working copy of the base water shapefile and invoke
    the C++ uwp binary on the full set of regional shapefiles that exist
    on disk for the selected areas. The merge phase always runs on the full
    set because `bg::union_` is not invertible.

    `max_inland_km`: optional inland-distance cap forwarded to the binary
    (0 disables it)."""
    target = initialize_working_directory()
    water_shapefiles = [
        str(shp_sub_region(region, sub_region))
        for region, sub_region in AREAS.items()
        if region in selected_areas
        and shp_sub_region(region, sub_region).exists()
    ]
    LOGGER.info(
        'Running %s on %d regional shapefile(s) (max_inland_km=%g)',
        uwp_path,
        len(water_shapefiles),
        max_inland_km,
    )
    cmd = [uwp_path, str(target), '-o', str(target)]
    if max_inland_km > 0:
        cmd += ['--max-inland-km', str(max_inland_km)]
    cmd += water_shapefiles
    subprocess.run(cmd, check=True)
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

    # Apply CLI retry budget to the module-level default used by every
    # download_file / remote_metadata call. Using `global` here is the
    # whole point: a CLI override has to mutate the runtime default.
    global _MAX_RETRIES  # noqa: PLW0603
    _MAX_RETRIES = max(1, args.max_retries)

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

    _run_uwp(args.uwp, args.areas, max_inland_km=args.max_inland_km)


if __name__ == '__main__':
    main()

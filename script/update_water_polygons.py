#!/usr/bin/env python3

import argparse
import os
import pathlib
import subprocess
import shutil
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
    'bosnia-herzegovina': 'europe',
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
    'guernsey-jersey': 'europe',
    'iceland': 'europe',
    'ireland-and-northern-ireland': 'europe',
    'isle-of-man': 'europe',
    'italy': 'europe',
    'latvia': 'europe',
    'lithuania': 'europe',
    'malta': 'europe',
    'monaco': 'europe',
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
    'alberta': 'north-america/canada',
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
    'saskatchewan': 'north-america/canada',
    'yukon': 'north-america/canada',
    'alabama': 'north-america/us',
    'alaska': 'north-america/us',
    'arizona': 'north-america/us',
    'arkansas': 'north-america/us',
    'california': 'north-america/us',
    'colorado': 'north-america/us',
    'connecticut': 'north-america/us',
    'delaware': 'north-america/us',
    'district-of-columbia': 'north-america/us',
    'florida': 'north-america/us',
    'georgia': 'north-america/us',
    'hawaii': 'north-america/us',
    'idaho': 'north-america/us',
    'illinois': 'north-america/us',
    'indiana': 'north-america/us',
    'iowa': 'north-america/us',
    'kansas': 'north-america/us',
    'kentucky': 'north-america/us',
    'louisiana': 'north-america/us',
    'maine': 'north-america/us',
    'maryland': 'north-america/us',
    'massachusetts': 'north-america/us',
    'michigan': 'north-america/us',
    'minnesota': 'north-america/us',
    'mississippi': 'north-america/us',
    'missouri': 'north-america/us',
    'montana': 'north-america/us',
    'nebraska': 'north-america/us',
    'nevada': 'north-america/us',
    'new-hampshire': 'north-america/us',
    'new-jersey': 'north-america/us',
    'new-mexico': 'north-america/us',
    'new-york': 'north-america/us',
    'north-carolina': 'north-america/us',
    'north-dakota': 'north-america/us',
    'ohio': 'north-america/us',
    'oklahoma': 'north-america/us',
    'oregon': 'north-america/us',
    'pennsylvania': 'north-america/us',
    'puerto-rico': 'north-america/us',
    'rhode-island': 'north-america/us',
    'south-carolina': 'north-america/us',
    'south-dakota': 'north-america/us',
    'tennessee': 'north-america/us',
    'texas': 'north-america/us',
    'us-virgin-islands': 'north-america/us',
    'utah': 'north-america/us',
    'vermont': 'north-america/us',
    'virginia': 'north-america/us',
    'washington': 'north-america/us',
    'west-virginia': 'north-america/us',
    'wisconsin': 'north-america/us',
    'wyoming': 'north-america/us',
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


def osm_pbf_sub_region(region: str) -> pathlib.Path:
    """Get the path to the DBF file for the specified region"""
    return DATA_DIR / f'{region}.osm.pbf'


def shp_sub_region(region: str) -> pathlib.Path:
    """Get the path to the SHP file for the specified region"""
    return DATA_DIR / region / 'natural_water.shp'


def water_polygon_path() -> pathlib.Path:
    """Get the path to the water polygons directory"""
    return DATA_DIR / 'water-polygons-split-4326'


def water_polygon_shp() -> pathlib.Path:
    """Get the path to the water polygons directory"""
    return water_polygon_path() / 'water_polygons.shp'


def download_sub_region(region: str, sub_region: str) -> None:
    """Download the specified sub-region"""
    if sub_region:
        # https://download.geofabrik.de/asia/afghanistan-latest.osm.pbf
        url = f'{GEOFABRIK_URL}/{sub_region}/{region}-latest.osm.pbf'
    else:
        # https://download.geofabrik.de/asia-latest.osm.pbf
        url = f'{GEOFABRIK_URL}/{region}-latest.osm.pbf'
    output_file = osm_pbf_sub_region(region)
    if output_file.exists() or shp_sub_region(region).exists():
        LOGGER.info('%s already exists, skipping download', output_file)
        return
    download_file(url, str(output_file))


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


def convert_to_shp(region: str) -> None:
    """Convert the OSM PBF file to SHP format"""
    water_shp = shp_sub_region(region)
    if water_shp.exists():
        LOGGER.info(
            'Shapefile for %s already exists, skipping conversion',
            region,
        )
        return
    water_shp.parent.mkdir(parents=True, exist_ok=True)
    osm_pbf = osm_pbf_sub_region(region)
    water_pbf = osm_pbf.parent / f'{region}-water.osm.pbf'

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
        env={'OGR_GEOMETRY_ACCEPT_UNCLOSED_RING': 'NO'},
        check=True,
    )


def download_water_polygons() -> None:
    """Download and convert water polygons for all sub-regions"""
    shp = water_polygon_shp()
    if shp.exists():
        LOGGER.info('%s already exists, skipping download', shp)
        return
    output_file = pathlib.Path(tempfile.gettempdir())
    output_file /= 'water-polygons-split-4326.zip'
    download_file(
        f'{OSM_URL}/download/water-polygons-split-4326.zip',
        str(output_file),
    )
    with zipfile.ZipFile(output_file, 'r') as zip_ref:
        zip_ref.extractall(water_polygon_path())
    LOGGER.info('Extracted water polygons to %s', water_polygon_path())
    # Clean up the zip file
    os.remove(output_file)
    # Remove the temporary directory
    shutil.rmtree(water_polygon_path(), ignore_errors=True)


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
    return parser.parse_args()


def main():
    """Main function to download and convert water polygons"""
    args = usage()

    if not shutil.which('ogr2ogr'):
        LOGGER.error('ogr2ogr is not installed. Please install GDAL.')
        sys.exit(1)

    if not shutil.which(args.uwp):
        LOGGER.error('%s does not exist. Please check the path.', args.uwp)

    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
    )
    LOGGER.info('Starting water polygon update')
    # Create the data directory if it doesn't exist
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Download and convert water polygons for all sub-regions
    for region, sub_region in args.areas.items():
        download_sub_region(region, sub_region)
        convert_to_shp(region)
    # Download the water polygons
    download_water_polygons()

    target = initialize_working_directory()

    water_shapefiles = [
        str(shp_sub_region(region))
        for region in args.areas
        if shp_sub_region(region).exists()
    ]
    subprocess.run(
        [args.uwp, str(target), '-o', str(target), *water_shapefiles],
        check=True,
    )


if __name__ == '__main__':
    main()

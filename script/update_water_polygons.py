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
AREAS = (
    "africa",
    "antarctica",
    "asia",
    "australia-oceania",
    "europe",
    "north-america",
    "south-america",
)

#: The base URL for downloading OpenStreetMap data
GEOFABRIK_URL = "https://download.geofabrik.de"

#: The base URL for OpenStreetMap data
OSM_URL = "https://osmdata.openstreetmap.de"

#: The root directory of the project
ROOT = pathlib.Path(__file__).parent.parent

#: This is the directory where the data are handled
DATA_DIR = ROOT / "data"


def download_file(url: str, output_file: str) -> None:
    """Download file with progress reporting"""
    LOGGER.info(f"Downloading {url} to {output_file}")

    last_percent_reported = -1

    def report_progress(block_count, block_size, total_size):
        downloaded = block_count * block_size
        percent = int(downloaded * 100 / total_size) if total_size > 0 else 0
        percent = min(percent, 100)
        nonlocal last_percent_reported
        # Log only if the percentage increased by at least 10% or reached 100%
        if percent - last_percent_reported >= 10 or percent == 100:
            last_percent_reported = percent
            LOGGER.info(f"{output_file}: {percent}% complete")

    try:
        urllib.request.urlretrieve(
            url,
            output_file,
            reporthook=report_progress,
        )
    except:  # noqa: E722
        # Try to remove the partially downloaded file then raise the error
        pathlib.Path(output_file).unlink(missing_ok=True)
        raise

    LOGGER.info(f"Download complete: {output_file}")


def osm_dbf_sub_region(region: str) -> pathlib.Path:
    """Get the path to the DBF file for the specified region"""
    return DATA_DIR / f"{region}.osm.pbf"


def shp_sub_region(region: str) -> pathlib.Path:
    """Get the path to the SHP file for the specified region"""
    return DATA_DIR / region / "natural_water.shp"


def water_polygon_path() -> pathlib.Path:
    """Get the path to the water polygons directory"""
    return DATA_DIR / "water-polygons-split-4326"


def water_polygon_shp() -> pathlib.Path:
    """Get the path to the water polygons directory"""
    return water_polygon_path() / "water_polygons.shp"


def download_sub_region(region: str) -> None:
    """Download the specified sub-region"""
    url = f"{GEOFABRIK_URL}/{region}-latest.osm.pbf"
    output_file = osm_dbf_sub_region(region)
    if output_file.exists() or shp_sub_region(region).exists():
        LOGGER.info(f"{output_file} already exists, skipping download")
        return
    download_file(url, str(output_file))


def corrected_water_polygon_path() -> pathlib.Path:
    """Get the path to the corrected water polygons directory"""
    return DATA_DIR / "corrected-water-polygons.shp"


def tmp_polygon_path() -> pathlib.Path:
    """Get the path to the corrected water polygons directory"""
    return DATA_DIR / "tmp-water-polygons.shp"


def copy_shp(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Copy the SHP file and its associated files"""
    shutil.copy(src, dst)
    dbf = src.with_suffix(".dbf")
    shutil.copy(dbf, dst.with_suffix(".dbf"))
    prj = src.with_suffix(".prj")
    shutil.copy(prj, dst.with_suffix(".prj"))
    shx = src.with_suffix(".shx")
    shutil.copy(shx, dst.with_suffix(".shx"))


def move_shp(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Move the SHP file and its associated files"""
    shutil.move(src, dst)
    dbf = src.with_suffix(".dbf")
    shutil.move(dbf, dst.with_suffix(".dbf"))
    prj = src.with_suffix(".prj")
    if prj.exists():
        shutil.move(prj, dst.with_suffix(".prj"))
    shx = src.with_suffix(".shx")
    if shx.exists():
        shutil.move(shx, dst.with_suffix(".shx"))


def iniailize_working_directory() -> tuple[pathlib.Path, pathlib.Path]:
    """Initialize the working directory"""
    corrected_shp = corrected_water_polygon_path()
    if corrected_shp.exists():
        corrected_shp.unlink()
    tmpfile = tmp_polygon_path()
    water_polygon = water_polygon_shp()
    copy_shp(water_polygon, corrected_shp)

    return corrected_shp, tmpfile


def convert_to_shp(region: str) -> None:
    """Convert the OSM PBF file to SHP format"""
    output_file = shp_sub_region(region)
    if output_file.exists():
        LOGGER.info(f"{output_file} already exists, skipping conversion")
        return
    output_file.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ogr2ogr",
            "-f",
            "ESRI Shapefile",
            f"{output_file}",
            str(osm_dbf_sub_region(region)),
            "multipolygons",
            "-where",
            "natural='water'",
        ],
        env={"OGR_GEOMETRY_ACCEPT_UNCLOSED_RING": "NO"},
        check=True,
    )


def download_water_polygons() -> None:
    """Download and convert water polygons for all sub-regions"""
    shp = water_polygon_shp()
    if shp.exists():
        LOGGER.info(f"{shp} already exists, skipping download")
        return
    output_file = pathlib.Path(tempfile.gettempdir())
    output_file /= "water-polygons-split-4326.zip"
    download_file(
        f"{OSM_URL}/download/water-polygons-split-4326.zip",
        str(output_file),
    )
    with zipfile.ZipFile(output_file, "r") as zip_ref:
        zip_ref.extractall(water_polygon_path())
    LOGGER.info(f"Extracted water polygons to {water_polygon_path()}")
    # Clean up the zip file
    os.remove(output_file)
    # Remove the temporary directory
    shutil.rmtree(water_polygon_path(), ignore_errors=True)


def usage() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update water polygons from OpenStreetMap to include "
        "estuaries and missing polygons.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--areas",
        choices=AREAS,
        default=AREAS,
        nargs="+",
        help="Areas to process. Defaults to all areas.",
    )
    parser.add_argument(
        "--uwp",
        type=str,
        default=str(ROOT / "build" / "uwp"),
        help="Path to the UWP executable.",
    )
    return parser.parse_args()


def main():
    """Main function to download and convert water polygons"""
    args = usage()

    if not shutil.which("ogr2ogr"):
        LOGGER.error("ogr2ogr is not installed. Please install GDAL.")
        sys.exit(1)

    if not shutil.which(args.uwp):
        LOGGER.error(args.uwp + " does not exist. Please check the path.")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    LOGGER.info("Starting water polygon update")
    # Create the data directory if it doesn't exist
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Download and convert water polygons for all sub-regions
    for area in args.areas:
        download_sub_region(area)
        convert_to_shp(area)
    # Download the water polygons
    download_water_polygons()

    target, tmpfile = iniailize_working_directory()

    for item in args.areas:
        LOGGER.info(f"Processing {item}")
        subprocess.run(
            [
                args.uwp,
                str(target),
                str(shp_sub_region(item)),
                str(tmpfile),
            ],
            check=True,
        )
        move_shp(tmpfile, target)


if __name__ == "__main__":
    main()

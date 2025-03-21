# Purpose
This project updates [OpenStreetMap water
polygons](https://osmdata.openstreetmap.de/data/water-polygons.html) by
incorporating river deltas and coastal areas that are missing from the standard
water polygons dataset.

# Main Components
1. [Python Script](script/update_water_polygons.py):
    * Downloads water polygon data from OpenStreetMap
    * Downloads regional OSM data from Geofabrik
    * Extracts water features (`natural=water`) using osmium
    * Converts to shapefiles using `ogr2ogr`
    * Orchestrates the overall update process
2. C++ Program [uwp](src/main.cpp):
    * Takes a water polygon shapefile and regional water polygon files
    * Identifies overlapping areas between them
    * Merges the polygons where they intersect
    * Outputs updated water polygons

# Workflow

1. The script downloads the base water polygons from OSM and regional data for
   specified areas
2. It extracts water features from the regional data and converts them to
   shapefiles
3. It prepares a working copy of the water polygons
4. The C++ program processes the overlapping polygons:
    * Loads the main water shapefile
    * For each regional shapefile:
        * Builds spatial indices
        * Finds overlapping polygons using [select_overlap](src/update.cpp)
        * Merges the overlapping polygons using
          [merge_overlapping](src/update.cpp)
5. The result is an updated water polygon file with better coverage of deltas
   and coastal areas

# Usage

You can run the update script with specific areas to process:

```bash
python script/update_water_polygons.py --areas south-america antarctica
```

The script will handle downloading the necessary data and running the C++
program to perform the actual polygon merging.
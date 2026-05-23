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
    * Tracks upstream change with a JSON manifest so reruns only re-download
      regions whose OSM extract actually changed (incremental update)
2. C++ Program [uwp](src/main.cpp):
    * Takes a water polygon shapefile and regional water polygon files
    * Identifies overlapping areas between them
    * Merges the polygons where they intersect
    * Outputs updated water polygons
3. Bootstrap script [`script/init_workspace.sh`](script/init_workspace.sh):
    * Creates / updates the conda environment from
      [`environment.yml`](environment.yml)
    * Builds the C++ binary
    * Prepares the data directory layout

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

# Quick start

## 1. Bootstrap the workspace

The `script/init_workspace.sh` script handles everything: it locates `conda` or
`mamba`, creates (or updates) the `uwp` environment defined in
`environment.yml`, then configures and builds the C++ binary.

```bash
./script/init_workspace.sh
```

After it finishes, activate the environment as instructed at the end of the
script's output (either `conda activate uwp` or `mamba activate uwp` depending
on what was detected).

### Configuration via environment variables

All paths are overridable. Useful on shared / HPC machines where the repo
sits in `$HOME` but the data has to live on scratch space:

| Variable | Default | Purpose |
|---|---|---|
| `UWP_ENV_NAME` | `uwp` | Name of the conda env to create/update |
| `UWP_ENV_FILE` | `<repo>/environment.yml` | Alternate env file |
| `UWP_BUILD_DIR` | `<repo>/build` | CMake build directory |
| `UWP_BUILD_TYPE` | `Release` | CMake build type |
| `UWP_JOBS` | `nproc` | Parallel build jobs |
| `UWP_DATA_DIR` | `<repo>/data` | Data root. If set elsewhere, the script creates a `data/` symlink so the Python script still finds it |
| `UWP_DISABLE_SSL_VERIFY` | unset | Set to `1` to disable conda SSL verification (auto-enabled on `*.cnes.fr` hosts where the corporate TLS proxy breaks conda-forge requests) |

Example for the CNES TREX cluster:

```bash
UWP_DATA_DIR=/work/scratch/$USER/uwp-data ./script/init_workspace.sh
```

The script detects CNES hosts automatically from the FQDN and exports
`CONDA_SSL_VERIFY=false` for you — no extra flag needed.

## 2. Run an update

```bash
conda activate uwp        # or: mamba activate uwp
python script/update_water_polygons.py --areas france italy spain
```

Omit `--areas` to process every region declared in the `AREAS` table at the
top of [`update_water_polygons.py`](script/update_water_polygons.py).

# Update script reference

The script handles downloads, extraction, and invocation of the C++ binary,
with incremental caching and a producer/consumer pipeline so that downloads
and extractions overlap.

```
python script/update_water_polygons.py [--areas REGION ...]
                                       [--uwp PATH_TO_BINARY]
                                       [--jobs N]
                                       [--extract-jobs N]
                                       [--force]
                                       [--check-only]
```

| Option | Default | Description |
|---|---|---|
| `--areas` | all regions | Subset of regions to process. Use the region names listed in `AREAS`. |
| `--uwp` | `<repo>/build/uwp` | Path to the C++ binary. |
| `--jobs` | `4` | Number of concurrent **downloads**. Network-bound, parallelises well. Use `1` to fully serialise. |
| `--extract-jobs` | `2` | Number of concurrent **extractions** (osmium + ogr2ogr). osmium is already multi-threaded internally, so the default is intentionally small to avoid CPU oversubscription. Increase on idle machines. |
| `--force` | off | Ignore the manifest and re-download / re-extract every selected region. Use after dependency upgrades or to recover from a corrupted cache. |
| `--check-only` | off | Print the list of regions that would be refreshed and exit, without downloading or running the binary. Handy to estimate the size of an update. |
| `--max-retries` | `5` | Retry budget for transient download / HEAD failures (exponential backoff with jitter). |
| `--max-inland-km` | `0` (off) | Clip regional water polygons that extend more than this many km beyond the matched coastal polygon's envelope. Stops giant river polygons from dragging the coastline deep inland. Typical values: 100-500. |

## What gets extracted from OSM

The osmium + ogr2ogr extraction step takes every `natural=water` polygon
from a region's PBF, then drops the narrow / linear sub-types via an
`ogr2ogr -where` clause:

```sql
water IS NULL OR water NOT IN ('stream', 'canal', 'ditch', 'drain')
```

This keeps lakes, lagoons, bays, reservoirs, and rivers (whose `water=*`
tag is `lake`, `lagoon`, `bay`, `river`, …, or absent), and drops streams,
canals, ditches, and drains — which are typically modelled as long thin
polygons that, if absorbed into the coastline, pull it far inland. The
remaining `water=river` polygons are still kept; pair this with
`--max-inland-km` to truncate them at a sensible distance from the coast.

## Two-stage pipeline

For each run, the script runs two thread pools concurrently:

```
   ┌─────────────────┐     download finishes     ┌─────────────────┐
   │ download pool   │ ────────────────────────▶ │ extract pool    │
   │ (--jobs)        │       hand off            │ (--extract-jobs)│
   │ network I/O     │                           │ osmium + ogr2ogr│
   └─────────────────┘                           └─────────────────┘
```

A download thread fetches a PBF, immediately submits the extraction to the
extract pool, then returns to grab the next region — so extractions start as
soon as their input is ready instead of waiting for every download to finish.

## Incremental update via manifest

A JSON manifest at `<DATA_DIR>/manifest.json` records the upstream
`Last-Modified` and `ETag` headers observed the last time each region was
successfully downloaded. On every run, before any heavy work:

1. The script `HEAD`s the Geofabrik URL for every selected region (fast, ~1s
   per region).
2. If the upstream headers still match the manifest **and** the cached files
   exist on disk, the region is skipped entirely — no download, no extraction.
3. Otherwise the cached PBF and shapefile are deleted, the region is queued
   for refresh, and the manifest is updated *after* a successful refresh.
4. The base water polygons archive (OSM's
   `water-polygons-split-4326.zip`) is tracked the same way.

The manifest is written atomically and only after work completes (`finally`
block), so a partial / failed run leaves the cache in a consistent state and
the next run resumes from where it stopped.

If Geofabrik is unreachable, the script falls back to whatever cache is on
disk (with a warning) rather than failing the run.

**The C++ merge phase always runs on the full set of regional shapefiles**,
not just the refreshed ones, because `bg::union_` is not invertible — there
is no way to "subtract" a region's contribution from the previous output.
The incremental gain is concentrated on the download and extraction phases,
which dominate the total wall-clock time.

## Quick examples

```bash
# See what would change without doing anything
python script/update_water_polygons.py --check-only

# Incremental refresh of Europe + North America
python script/update_water_polygons.py \
    --areas france germany spain italy united-kingdom \
            quebec ontario new-york california

# Saturate a beefy machine
python script/update_water_polygons.py --jobs 8 --extract-jobs 4

# Recover from a corrupted cache
python script/update_water_polygons.py --force --areas france

# Truncate river polygons that extend more than 300 km inland
python script/update_water_polygons.py --max-inland-km 300
```

# QA: visual diff atlas

For SWOT coastline calibration, every addition the pipeline made to the
reference shapefile should be inspected. The companion script
[`script/visualize_diff.py`](script/visualize_diff.py) renders a high-DPI
PNG per modified region with an HTML atlas to navigate them.

```bash
python script/visualize_diff.py \
    --reference data/water-polygons-split-4326/water_polygons.shp \
    --revised  data/corrected-water-polygons.shp \
    --output   data/diff-report \
    --min-delta-km2 0.5 \
    --basemap
```

The script:

1. Pairs polygons by FID across the two shapefiles, computes
   `revised[i] − reference[i]` for the modified ones, and picks up the
   new polygons appended at the end of the revised file (the
   `extra_polygons` produced by the C++ merge).
2. **Clusters adjacent deltas geographically** (`--cluster-buffer-km`,
   default 5 km) so a single estuary split across several base polygons
   renders as one image rather than a dozen useless tiny ones.
3. For each cluster, draws on a single PNG:
   - blue outline: reference polygons in the area,
   - red outline: revised polygons in the same area,
   - yellow fill with orange border: the added zone(s),
   - optional OSM basemap underneath when `--basemap` is set
     (requires network).
4. Writes `index.html` (sortable atlas of thumbnails) and
   `clusters.csv` (machine-readable list with center coords, area added,
   FIDs) under `--output`.

| Option | Default | Description |
|---|---|---|
| `--reference` | `data/water-polygons-split-4326/water_polygons.shp` | Reference shapefile (pre-uwp). |
| `--revised` | `data/corrected-water-polygons.shp` | Revised shapefile (uwp output). |
| `--output` | `data/diff-report` | Output directory. |
| `--min-delta-km2` | `0.1` | Skip clusters smaller than this — filters noise. |
| `--cluster-buffer-km` | `5.0` | Merge adjacent deltas within this distance. |
| `--dpi` | `200` | Output PNG resolution. 200 ≈ 1600×1600 px. |
| `--basemap` | off | Add OSM tiles under the polygons (network required). |
| `--max-images` | `0` (unlimited) | Cap the render count — for a quick sanity check. |
| `--bbox MINLON MINLAT MAXLON MAXLAT` | none | Restrict the diff to a window — much faster when inspecting one region. |

### Typical workflow

```bash
# After a full pipeline run, generate the QA atlas with basemap:
python script/visualize_diff.py --basemap --min-delta-km2 0.5

# Quick look at one area only:
python script/visualize_diff.py --bbox -2 43 2 47 --basemap

# Top-20 biggest additions only, no basemap (offline):
python script/visualize_diff.py --max-images 20 --min-delta-km2 1.0
```

Then open `data/diff-report/index.html` in a browser. Rows are sorted by
added area (descending), so the biggest changes — the most likely to
need a human eyeball — appear first.

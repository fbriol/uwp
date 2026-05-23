#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Initialise the UWP workspace on the CNES cluster (or any Linux box).
#
# Steps:
#   1. Locate (or fail with a clear message) a conda/mamba install.
#   2. Create or update the `uwp` environment from environment.yml.
#   3. Configure & build the C++ binary in build/.
#   4. Create the data directory layout used by script/update_water_polygons.py.
#
# Configuration via environment variables (all optional):
#   UWP_ENV_NAME  — name of the conda env to create/update (default: uwp)
#   UWP_ENV_FILE  — path to environment.yml (default: <repo>/environment.yml)
#   UWP_BUILD_DIR — CMake build directory     (default: <repo>/build)
#   UWP_DATA_DIR  — data root for downloads   (default: <repo>/data)
#                   On the cluster, point this to scratch space, e.g.
#                   UWP_DATA_DIR=/work/scratch/$USER/uwp-data
#   UWP_BUILD_TYPE— CMake build type          (default: Release)
#   UWP_JOBS      — parallel build jobs       (default: nproc)
#
# Usage:
#   ./script/init_workspace.sh
#
# After this script finishes, activate the env and run an update:
#   conda activate "$UWP_ENV_NAME"
#   python script/update_water_polygons.py --areas france italy ...
# ----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_NAME="${UWP_ENV_NAME:-uwp}"
ENV_FILE="${UWP_ENV_FILE:-$REPO_ROOT/environment.yml}"
BUILD_DIR="${UWP_BUILD_DIR:-$REPO_ROOT/build}"
DATA_DIR="${UWP_DATA_DIR:-$REPO_ROOT/data}"
BUILD_TYPE="${UWP_BUILD_TYPE:-Release}"
JOBS="${UWP_JOBS:-$(command -v nproc >/dev/null 2>&1 && nproc || echo 4)}"

log()  { printf '[init] %s\n' "$*" >&2; }
die()  { printf '[init] ERROR: %s\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------------------
# 1. Locate conda / mamba
# ----------------------------------------------------------------------------
if command -v mamba >/dev/null 2>&1; then
  CONDA_TOOL=mamba
elif command -v conda >/dev/null 2>&1; then
  CONDA_TOOL=conda
else
  die "Neither 'conda' nor 'mamba' found in PATH.
       On the CNES cluster, load it first, e.g.: module load conda
       Or install Miniforge: https://github.com/conda-forge/miniforge"
fi
log "Using $CONDA_TOOL ($(command -v $CONDA_TOOL))"

# CNES HPC detection: hostnames look like trex001.sis.cnes.fr, hal0123.cnes.fr,
# etc. The cluster's outbound TLS interception breaks conda's SSL verification
# against conda-forge, so we have to opt out there. Allow an explicit override
# via UWP_DISABLE_SSL_VERIFY=1 for other environments behind similar proxies.
#
# Note: the `--ssl-verify` CLI flag is NOT accepted by the `env` subcommands
# of conda/mamba (they are thin wrappers). The supported mechanism is the
# CONDA_SSL_VERIFY environment variable, which the underlying conda library
# reads for both `env create/update` and plain `install`.
HOST_FQDN="$(hostname -f 2>/dev/null || hostname)"
if [[ "${UWP_DISABLE_SSL_VERIFY:-0}" == "1" ]] \
   || [[ "$HOST_FQDN" == *.cnes.fr ]]; then
  log "Detected CNES host ($HOST_FQDN) — disabling SSL verification for $CONDA_TOOL"
  export CONDA_SSL_VERIFY=false
fi

# Make `<tool> activate` usable inside this non-interactive shell. The hook
# command syntax changed between mamba 1.x and mamba 2.x:
#   - conda (any version): `conda shell.bash hook`
#   - mamba 1.x:           `mamba shell.bash hook`   (legacy, conda-compatible)
#   - mamba 2.x:           `mamba shell hook --shell bash` (new syntax)
# We try the new mamba-2 syntax first when applicable, then fall back to the
# legacy one. The eval'd output defines the activate function we need.
ACTIVATE_CMD="$CONDA_TOOL activate"
if [[ "$CONDA_TOOL" == "mamba" ]] \
   && mamba shell hook --shell bash >/dev/null 2>&1; then
  # mamba 2.x
  # shellcheck disable=SC1090
  eval "$(mamba shell hook --shell bash)"
else
  # conda, or mamba 1.x
  # shellcheck disable=SC1090
  eval "$($CONDA_TOOL shell.bash hook)"
fi

# ----------------------------------------------------------------------------
# 2. Create or update the env
# ----------------------------------------------------------------------------
[[ -f "$ENV_FILE" ]] || die "Environment file not found: $ENV_FILE"

if $CONDA_TOOL env list | awk 'NR>2 {print $1}' | grep -qx "$ENV_NAME"; then
  log "Updating existing conda env '$ENV_NAME' from $ENV_FILE"
  $CONDA_TOOL env update --name "$ENV_NAME" --file "$ENV_FILE" --prune
else
  log "Creating conda env '$ENV_NAME' from $ENV_FILE"
  $CONDA_TOOL env create --name "$ENV_NAME" --file "$ENV_FILE"
fi

log "Activating env '$ENV_NAME' ($ACTIVATE_CMD)"
$ACTIVATE_CMD "$ENV_NAME"

# Quick sanity checks on the tools the Python script depends on.
for tool in osmium ogr2ogr cmake; do
  command -v "$tool" >/dev/null 2>&1 \
    || die "Tool '$tool' is missing from the env. Re-check environment.yml."
done

# ----------------------------------------------------------------------------
# 3. Configure and build the C++ binary
# ----------------------------------------------------------------------------
log "Configuring CMake (build type: $BUILD_TYPE) in $BUILD_DIR"
cmake -S "$REPO_ROOT" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE="$BUILD_TYPE"

log "Building uwp with $JOBS parallel jobs"
cmake --build "$BUILD_DIR" -j "$JOBS"

[[ -x "$BUILD_DIR/uwp" ]] || die "Build finished but '$BUILD_DIR/uwp' is not executable"

# ----------------------------------------------------------------------------
# 4. Data directory layout
# ----------------------------------------------------------------------------
# The Python script expects DATA_DIR to be the repo's data/ folder. If
# UWP_DATA_DIR points elsewhere (e.g. scratch space on the cluster), create a
# symlink so the script finds it transparently.
if [[ "$DATA_DIR" != "$REPO_ROOT/data" ]]; then
  log "Linking $REPO_ROOT/data -> $DATA_DIR (scratch space)"
  mkdir -p "$DATA_DIR"
  if [[ -L "$REPO_ROOT/data" ]]; then
    rm "$REPO_ROOT/data"
  elif [[ -e "$REPO_ROOT/data" ]]; then
    die "$REPO_ROOT/data exists and is not a symlink — refusing to overwrite. Move it manually."
  fi
  ln -s "$DATA_DIR" "$REPO_ROOT/data"
fi

mkdir -p "$DATA_DIR/osm-pbf" "$DATA_DIR/shapefiles"

# ----------------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------------
cat <<EOF >&2

[init] Workspace ready.

  Repo root  : $REPO_ROOT
  Build dir  : $BUILD_DIR  (binary: $BUILD_DIR/uwp)
  Data dir   : $DATA_DIR
  Conda env  : $ENV_NAME

Next steps:

  $ACTIVATE_CMD $ENV_NAME
  python $REPO_ROOT/script/update_water_polygons.py --help

EOF

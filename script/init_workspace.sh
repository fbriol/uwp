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
#   UWP_ENV_NAME   — name of the conda env (default: uwp). Only used to
#                    derive the default UWP_ENV_PREFIX below.
#   UWP_ENV_PREFIX — absolute path the env is installed into. Default:
#                    "<mamba_install_root>/envs/$UWP_ENV_NAME" — keeps the
#                    env *outside* $HOME so it doesn't fill up cluster
#                    HOME quotas. Override only if you want the env in a
#                    specific location.
#   UWP_ENV_FILE   — path to environment.yml (default: <repo>/environment.yml)
#   UWP_BUILD_DIR  — CMake build directory     (default: <repo>/build)
#   UWP_DATA_DIR   — data root for downloads   (default: <repo>/data)
#                    On the cluster, point this to scratch space, e.g.
#                    UWP_DATA_DIR=/work/scratch/$USER/uwp-data
#   UWP_BUILD_TYPE — CMake build type          (default: Release)
#   UWP_JOBS       — parallel build jobs       (default: nproc)
#   UWP_PRUNE=1    — pass --prune to env update (slow; only when removing deps)
#   UWP_ALLOW_HOME_ENV=1
#                  — bypass the safeguard that refuses to install the env
#                    under $HOME. Only set if your $HOME has plenty of quota.
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

# Whether to pass --prune to `env update`. Pruning forces a full re-solve of
# the dep graph (slow on CNES). Disabled by default for speed; set
# UWP_PRUNE=1 if you removed a dependency from environment.yml and want it
# uninstalled from the env.
UWP_PRUNE="${UWP_PRUNE:-0}"

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

# ----------------------------------------------------------------------------
# Choose where the env lives.
#
# Default behaviour of `conda/mamba env create --name X` is to put the env
# under the first writable entry of `envs_dirs`, which on a fresh install is
# `~/.conda/envs/`. On the CNES cluster (and many HPCs) the user's HOME has
# a small quota that the env quickly fills up, so we want the env to live
# under the mamba install root instead (typically `<install>/envs/`).
#
# Strategy:
#   1. If UWP_ENV_PREFIX is set, use it as-is.
#   2. Otherwise auto-detect the mamba/conda root prefix and build
#      "<root>/envs/<ENV_NAME>".
#   3. Pass it via `--prefix` instead of `--name`.
#
# Using --prefix bypasses the envs_dirs config entirely — the env goes
# exactly where we ask, no matter what's in ~/.condarc.
if [[ -n "${UWP_ENV_PREFIX:-}" ]]; then
  ENV_PREFIX="$UWP_ENV_PREFIX"
else
  # Auto-detect the mamba/conda root install prefix.
  #
  # We deliberately do NOT use `$CONDA_TOOL info --base` here: in mamba 2.x
  # it prints descriptive text like " base environment : /path/to/root"
  # instead of just the path, breaking the parse.
  #
  # The mamba/conda binary always lives at `<root>/bin/<tool>` (or
  # `<root>/condabin/<tool>`), so stripping two path components from the
  # resolved executable path gives us the root reliably across versions.
  # `realpath` (or `readlink -f`) follows the symlinks installers commonly
  # create (e.g. `~/.local/bin/mamba -> /opt/miniforge/bin/mamba`).
  ROOT_PREFIX="${MAMBA_ROOT_PREFIX:-${CONDA_ROOT:-}}"
  if [[ -z "$ROOT_PREFIX" ]]; then
    TOOL_PATH="$(command -v "$CONDA_TOOL")"
    if command -v realpath >/dev/null 2>&1; then
      TOOL_PATH="$(realpath "$TOOL_PATH")"
    elif command -v readlink >/dev/null 2>&1; then
      TOOL_PATH="$(readlink -f "$TOOL_PATH" 2>/dev/null || echo "$TOOL_PATH")"
    fi
    ROOT_PREFIX="$(dirname "$(dirname "$TOOL_PATH")")"
  fi
  if [[ -z "$ROOT_PREFIX" ]] || [[ ! -d "$ROOT_PREFIX" ]]; then
    die "Could not determine $CONDA_TOOL root prefix ($ROOT_PREFIX). Set UWP_ENV_PREFIX explicitly."
  fi
  ENV_PREFIX="$ROOT_PREFIX/envs/$ENV_NAME"
fi
log "Env prefix: $ENV_PREFIX"

# Sanity check: refuse to put the env under HOME unless the user is
# explicit. Keeps a misconfigured cluster from filling up the HOME quota.
if [[ "$ENV_PREFIX" == "$HOME"* ]] \
   && [[ "${UWP_ALLOW_HOME_ENV:-0}" != "1" ]]; then
  die "Resolved ENV_PREFIX is under \$HOME ($ENV_PREFIX). Set UWP_ENV_PREFIX
       to a path outside HOME (scratch / work), or set UWP_ALLOW_HOME_ENV=1
       to silence this check."
fi

# CNES HPC detection: hostnames look like trex001.sis.cnes.fr, hal0123.cnes.fr,
# etc. The cluster's outbound TLS interception (self-signed CA in the chain)
# breaks both the Python conda layer and the C++ libmamba solver used by
# mamba 2.x. Allow an explicit override via UWP_DISABLE_SSL_VERIFY=1 for
# other environments behind similar proxies.
#
# Disabling SSL has to be done at *both* layers:
#
#   - conda Python layer  → reads CONDA_SSL_VERIFY (env var).
#   - libmamba (mamba 2)  → does NOT read CONDA_SSL_VERIFY; only the
#     `ssl_verify` setting in a condarc-style config file. We write a
#     dedicated rc file and point CONDARC / MAMBARC at it.
#
# Using a dedicated rc file (instead of `conda config --set`) keeps the
# user's ~/.condarc untouched and avoids surprising them with permanent
# state changes.
HOST_FQDN="$(hostname -f 2>/dev/null || hostname)"
if [[ "${UWP_DISABLE_SSL_VERIFY:-0}" == "1" ]] \
   || [[ "$HOST_FQDN" == *.cnes.fr ]]; then
  log "Detected CNES host ($HOST_FQDN) — disabling SSL verification for $CONDA_TOOL"
  export CONDA_SSL_VERIFY=false
  UWP_RC_FILE="$(mktemp -t uwp_condarc.XXXXXX.yml)"
  cat > "$UWP_RC_FILE" <<'EOF'
# Auto-generated by script/init_workspace.sh — do not edit.
# Disables TLS verification so libmamba can talk to conda-forge through
# the CNES corporate TLS-interception proxy.
ssl_verify: false
# Strict channel priority restricts the solver to packages from the first
# matching channel (conda-forge in our environment.yml). Cheap speed-up,
# also makes the resolution deterministic across machines.
channel_priority: strict
EOF
  export CONDARC="$UWP_RC_FILE"
  export MAMBARC="$UWP_RC_FILE"
  # Clean up the temp file when the script exits (success or failure).
  trap 'rm -f "$UWP_RC_FILE"' EXIT
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

# Note: we used to add --strict-channel-priority here, but the `env`
# subcommands of mamba 2.x reject it (it is a `mamba install` flag only).
# Strict priority is enforced declaratively via `channel_priority: strict`
# in the rc file when one is active (see CNES section above).
ENV_EXTRA_ARGS=()

# An env at a specific prefix exists iff its conda-meta dir is populated.
# `env list` doesn't always show prefix-based envs, so we check the path
# directly — more reliable.
if [[ -d "$ENV_PREFIX/conda-meta" ]]; then
  log "Updating existing conda env at $ENV_PREFIX from $ENV_FILE"
  PRUNE_ARGS=()
  if [[ "$UWP_PRUNE" == "1" ]]; then
    log "UWP_PRUNE=1 → forcing full --prune re-solve (slower)"
    PRUNE_ARGS+=(--prune)
  fi
  $CONDA_TOOL env update \
    --prefix "$ENV_PREFIX" --file "$ENV_FILE" \
    "${PRUNE_ARGS[@]}" "${ENV_EXTRA_ARGS[@]}"
else
  log "Creating conda env at $ENV_PREFIX from $ENV_FILE"
  # Python is pinned inside environment.yml — passing it here as a positional
  # spec is silently ignored by `env create` (only --prefix / --file are
  # honoured) and would not affect the solver anyway.
  mkdir -p "$(dirname "$ENV_PREFIX")"
  $CONDA_TOOL env create \
    --prefix "$ENV_PREFIX" --file "$ENV_FILE" \
    "${ENV_EXTRA_ARGS[@]}"
fi

# Activation by prefix works the same way as by name for both conda and
# mamba (1.x and 2.x).
log "Activating env at $ENV_PREFIX ($ACTIVATE_CMD)"
$ACTIVATE_CMD "$ENV_PREFIX"

# Quick sanity checks on the tools the Python script depends on.
for tool in osmium ogr2ogr cmake; do
  command -v "$tool" >/dev/null 2>&1 \
    || die "Tool '$tool' is missing from the env. Re-check environment.yml."
done

# ----------------------------------------------------------------------------
# 3. Configure and build the C++ binary
# ----------------------------------------------------------------------------
# Invalidate the CMake cache if it references compilers from an env that no
# longer exists (typical after moving the env prefix — e.g. relocating from
# ~/.conda/envs/uwp to <mamba_root>/envs/uwp). CMake's cached
# CMAKE_C_COMPILER / CMAKE_CXX_COMPILER paths point absolutely into the env
# bin dir; if those files vanished, every subsequent configure errors with
# "is not a full path to an existing compiler tool". Wiping the cache makes
# CMake rediscover the active env's compilers via the CC / CXX env vars set
# by conda activation.
if [[ -f "$BUILD_DIR/CMakeCache.txt" ]]; then
  CACHED_CXX="$(
    awk -F= '/^CMAKE_CXX_COMPILER:/ {print $2; exit}' \
      "$BUILD_DIR/CMakeCache.txt"
  )"
  if [[ -n "$CACHED_CXX" ]] && [[ ! -x "$CACHED_CXX" ]]; then
    log "Stale CMake cache (compiler $CACHED_CXX missing). Wiping $BUILD_DIR."
    rm -rf "$BUILD_DIR"
  fi
fi

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
  Env prefix : $ENV_PREFIX

Next steps:

  $ACTIVATE_CMD $ENV_PREFIX
  python $REPO_ROOT/script/update_water_polygons.py --help

EOF

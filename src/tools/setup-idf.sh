#!/usr/bin/env bash
# setup-idf.sh
#
# Idempotent ESP-IDF environment setup for the Annealage Pod build.
#
# Strategy: prefer the IDF v5.5.1 checkout already provisioned on this
# host by Phase 0.3 at /home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf.
# Fall back to $IDF_PATH if exported by the caller, else clone v5.5.1
# under $HOME/esp/esp-idf.
#
# Usage: source src/tools/setup-idf.sh
# (Sourcing exports IDF_PATH and runs export.sh so idf.py is on PATH.)
#
# After sourcing, idf.py --version should report "ESP-IDF v5.5.x".

set -e

# Default upstream IDF this build is pinned to. Track the version note
# in src/VERSIONS; keep this in sync.
DEFAULT_IDF_VERSION="v5.5.1"

# Prefer the host-provisioned IDF from Phase 0.3 if present.
HOST_IDF="/home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf"

# Otherwise honour an explicit IDF_PATH from the caller environment.
if [ -n "${IDF_PATH:-}" ] && [ -d "${IDF_PATH}" ] && [ -f "${IDF_PATH}/export.sh" ]; then
    :
elif [ -d "${HOST_IDF}" ] && [ -f "${HOST_IDF}/export.sh" ]; then
    export IDF_PATH="${HOST_IDF}"
else
    # Last resort: clone our own copy.
    LOCAL_IDF="${HOME}/esp/esp-idf-${DEFAULT_IDF_VERSION}"
    if [ ! -d "${LOCAL_IDF}" ]; then
        mkdir -p "${HOME}/esp"
        git clone --branch "${DEFAULT_IDF_VERSION}" --depth 1 \
            --recursive https://github.com/espressif/esp-idf.git "${LOCAL_IDF}"
        "${LOCAL_IDF}/install.sh" esp32s3
    fi
    export IDF_PATH="${LOCAL_IDF}"
fi

# shellcheck disable=SC1091
. "${IDF_PATH}/export.sh" >/dev/null

# Sanity: idf.py must now be on PATH and reporting v5.5.x.
if ! command -v idf.py >/dev/null 2>&1; then
    echo "setup-idf.sh: idf.py not on PATH after sourcing export.sh" >&2
    return 1 2>/dev/null || exit 1
fi

IDF_REPORTED_VERSION="$(idf.py --version 2>&1 | tail -n1)"
case "${IDF_REPORTED_VERSION}" in
    *v5.5.*) ;;
    *)
        echo "setup-idf.sh: WARNING expected ESP-IDF v5.5.x, got: ${IDF_REPORTED_VERSION}" >&2
        ;;
esac

export IDF_PATH

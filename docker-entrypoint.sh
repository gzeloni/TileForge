#!/bin/sh
set -eu

MAPS_ROOT="${MAPS_ROOT:-/osm_maps}"
CACHE_DIR="${MAPS_ROOT}/cache"
SOURCES_DIR="${MAPS_ROOT}/sources"

if [ "$(id -u)" = "0" ]; then
    install -d -m 0775 -o appuser -g appuser "${MAPS_ROOT}"
    install -d -m 0775 -o appuser -g appuser "${CACHE_DIR}"
    install -d -m 0775 -o appuser -g appuser "${SOURCES_DIR}"
    if [ ! -f "${SOURCES_DIR}/manifest.json" ]; then
        gosu appuser python -c "from pathlib import Path; from app.services.maps import ensure_sources_manifest; ensure_sources_manifest(Path('${MAPS_ROOT}'), Path('${SOURCES_DIR}/manifest.json'))"
    fi
    exec gosu appuser "$@"
fi

exec "$@"

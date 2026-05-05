#!/bin/bash
# Substack archiver entrypoint. Loads config.sh, then runs the archiver.
# Do not edit values here — edit config.sh.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f config.sh ]; then
  echo "config.sh not found. Copy config.sh.example to config.sh and edit it." >&2
  exit 1
fi

# shellcheck source=/dev/null
. ./config.sh

# Bail cleanly if the configured drive isn't mounted (so scheduled runs
# don't spam errors when an external drive is unplugged).
if [ -n "${REQUIRE_MOUNT:-}" ] && [ ! -d "$REQUIRE_MOUNT" ]; then
  echo "$(date): $REQUIRE_MOUNT not mounted, skipping." >&2
  exit 0
fi

mkdir -p "$OUTPUT_DIR"

exec "${PYTHON_BIN:-/usr/local/bin/python3}" archive_substack.py \
  --url "$SUBSTACK_URL" \
  --out "$OUTPUT_DIR" \
  --cookie "$COOKIE"

#!/usr/bin/env bash
# Load Vibony Docker images from a tarball saved by docker-save.sh.
# After loading, docker compose up --no-build works fully offline.
#
# Usage:
#   ./scripts/docker-load.sh                 # loads from vibony-images.tar
#   ./scripts/docker-load.sh my-images.tar   # loads from custom path

set -euo pipefail

SRC="${1:-vibony-images.tar}"

if [ ! -f "$SRC" ]; then
  echo "Error: $SRC not found" >&2
  exit 1
fi

echo "Loading images from $SRC ..."
docker load -i "$SRC"

echo "Done. Run the stack with:"
echo "  docker compose up --no-build"

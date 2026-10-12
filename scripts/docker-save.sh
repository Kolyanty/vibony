#!/usr/bin/env bash
# Save all Vibony Docker images to a tarball for offline transfer.
# Run this once when you have network access; then copy the .tar to the target machine.
#
# Usage:
#   ./scripts/docker-save.sh                 # saves to vibony-images.tar
#   ./scripts/docker-save.sh my-images.tar   # saves to custom path

set -euo pipefail

OUT="${1:-vibony-images.tar}"

echo "Building images..."
docker compose build

echo "Saving images to $OUT ..."
docker save \
  vibony-backend:latest \
  vibony-dashboard:latest \
  postgres:16 \
  -o "$OUT"

SIZE=$(du -sh "$OUT" | cut -f1)
echo "Done: $OUT ($SIZE)"
echo "Transfer the file, then run: ./scripts/docker-load.sh $OUT"

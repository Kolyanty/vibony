#!/bin/sh
set -eu

cd /app/dashboard

# Compose persists node_modules across image rebuilds. Synchronize that volume
# with the bind-mounted manifests before Vite tries to resolve imports.
stamp=node_modules/.vibony-dependencies.sha256
if [ ! -f "$stamp" ] || ! sha256sum -c "$stamp" >/dev/null 2>&1; then
    echo "Dashboard dependencies changed; installing from package-lock.json..."
    npm ci --no-audit --no-fund
    sha256sum package.json package-lock.json > "$stamp"
fi

exec "$@"

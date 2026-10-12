# DEV image only — do NOT deploy. Runs the Vite dev server
# (GHSA-67mh-4wv8-2f99). Production: Dockerfile.prod (vite build + Caddy static serve).
FROM node:20-alpine

WORKDIR /app/dashboard

COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci && sha256sum package.json package-lock.json > node_modules/.vibony-dependencies.sha256

COPY --chmod=755 dashboard/docker-entrypoint.dev.sh /usr/local/bin/vibony-dashboard-dev

# Copy source so the image is self-contained (bind mount shadows this in dev).
COPY dashboard/ .

ENTRYPOINT ["vibony-dashboard-dev"]
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

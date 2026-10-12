# vibony/sandbox-node:20 — Node.js 20 sandbox.
#
# No app deps baked yet — Node tasks land later; this ships the runtime so a
# task can set `runtime_image: vibony/sandbox-node:20` in its spec.yaml.
# Containers stay on --network none (default-deny egress).
FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. The node:20 image ships a `node` user at uid 1000,
# which matches the bundle tar uid so put_archive lands files the candidate
# process can edit. Reuse it rather than creating a second uid-1000 user.
RUN mkdir -p /work && chown node:node /work

WORKDIR /work
USER node

# Container runs indefinitely; backend drives it via docker exec.
ENTRYPOINT ["sleep", "infinity"]

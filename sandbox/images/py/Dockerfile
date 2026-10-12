# vibony/sandbox-py:3.12 — Python sandbox with task-01 deps baked.
#
# Deps are baked at build time so the running container can stay on
# --network none (default-deny egress): the candidate never runs
# `poetry install` against the network.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry

# task-01-training-loop deps (torch ^2.3, CPU build — no CUDA in the sandbox).
# torch from the CPU index; its transitive deps (sympy, networkx, ...) resolve
# from the default PyPI via --extra-index-url. Generous retries/timeout because
# the torch wheels are large and the CPU index can be slow.
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple \
    "torch>=2.3,<3.0"

# Non-root user. uid 1000 matches the bundle tar uid so put_archive
# extraction lands files the candidate process (also uid 1000) can edit. Deps
# above were installed as root into the (now effectively read-only) site-packages.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /work \
    && chown appuser:appuser /work

WORKDIR /work
USER appuser

# Container runs indefinitely; backend drives it via docker exec.
ENTRYPOINT ["sleep", "infinity"]

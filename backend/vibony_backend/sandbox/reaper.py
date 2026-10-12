"""Orphan container reaper — called at backend startup.

Containers from previous backend runs carry the "vibony.session" label.
On restart we remove them; the event log is preserved in DB, and the
candidate can reconnect which will spin up a fresh sandbox.
"""

from __future__ import annotations

import logging

from .docker_provider import DockerSandboxProvider, SandboxHandle

logger = logging.getLogger(__name__)


async def reap_orphans(provider: DockerSandboxProvider) -> int:
    """Remove all containers labelled vibony.session=*. Returns count removed."""
    orphan_ids = await provider.list_orphans()
    for container_id in orphan_ids:
        await provider.teardown(SandboxHandle(sandbox_id=container_id))
    if orphan_ids:
        logger.info("Reaped %d orphaned sandbox container(s)", len(orphan_ids))
    return len(orphan_ids)


__all__ = ["reap_orphans"]

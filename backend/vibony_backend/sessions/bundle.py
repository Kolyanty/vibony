"""Bundle builder.

Builds a deterministic tar.gz from a task's public files.

Private files (NOT included in the bundle):
  - Fixed list: spec.yaml, INTERVIEWER.md, persona-playbook.md, weakness.yaml
  - `_` prefix (reference solutions, internal files)
  - `.` prefix (dotfiles: .git, .venv, .DS_Store)

Bundle properties:
  - Stable ordering: files sorted by name → reproducible SHA-256
  - Fixed mtime=0 and mode=0o644 on tar entries + gzip header with mtime=0
    — the archive bytes are identical for identical content
  - Top-level files only (tasks have no subdirectories for the candidate)
  - Maximum size 10 MB (guards against accidentally committed artifacts)

Cache: in-memory dict[task_id → (bytes, sha256)] — lives until the process restarts.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
import tarfile
from pathlib import Path

logger = logging.getLogger(__name__)

PRIVATE_NAMES: frozenset[str] = frozenset(
    {"spec.yaml", "INTERVIEWER.md", "persona-playbook.md", "weakness.yaml"}
)
MAX_BUNDLE_BYTES = 10 * 1024 * 1024  # 10 MB

_cache: dict[str, tuple[bytes, str]] = {}


def _is_private(name: str) -> bool:
    return name in PRIVATE_NAMES or name.startswith("_") or name.startswith(".")


def _pack(named_blobs: list[tuple[str, bytes]], *, label: str) -> tuple[bytes, str]:
    """Pack ``(name, bytes)`` pairs into a deterministic tar.gz → (bytes, sha256).

    Stable: entries sorted by name, fixed mtime=0 / mode=0o644 / empty uid/gid —
    identical content yields byte-identical archives (so ``bundle_sha256`` is a
    real content hash). Enforces ``MAX_BUNDLE_BYTES``.
    """
    ordered = sorted(named_blobs, key=lambda nb: nb[0])

    # Build the tar uncompressed first, then gzip it with a pinned header mtime.
    # tarfile's "w:gz" mode stamps the *gzip header* with the current time,
    # which defeats the determinism the tar entries carefully preserve — two
    # rebuilds of identical content seconds apart produced different bytes (and
    # thus different bundle_sha256). Compressing through gzip.GzipFile(mtime=0)
    # makes the whole archive byte-identical for identical content.
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        for name, data in ordered:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            # uid/gid 1000 = the non-root sandbox user: docker
            # put_archive preserves these, so the candidate process (uid 1000)
            # owns and can edit the extracted files. Fixed value keeps the
            # archive deterministic. Local-mode extractall uses filter="data",
            # which ignores archive ownership, so dev/test is unaffected.
            info.uid = 1000
            info.gid = 1000
            info.uname = ""
            info.gname = ""
            tar.addfile(info, io.BytesIO(data))

    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(tar_buf.getvalue())
    tar_bytes = gz_buf.getvalue()

    if len(tar_bytes) > MAX_BUNDLE_BYTES:
        raise ValueError(
            f"Bundle for {label} exceeds {MAX_BUNDLE_BYTES // 1024 // 1024} MB"
        )

    sha256 = hashlib.sha256(tar_bytes).hexdigest()
    logger.info(
        "bundle built: task=%s files=%d size=%d sha256=%s...",
        label,
        len(ordered),
        len(tar_bytes),
        sha256[:12],
    )
    return tar_bytes, sha256


def build_bundle(task_dir: Path) -> tuple[bytes, str]:
    """Build a deterministic tar.gz from a task directory (filesystem path).

    Not cached — use get_or_build_bundle() for cached access. Kept for the
    legacy source (seed importer) and tests; the runtime builds the bundle from the DB.
    """
    files = sorted(
        (p for p in task_dir.iterdir() if p.is_file() and not _is_private(p.name)),
        key=lambda p: p.name,
    )
    return _pack([(p.name, p.read_bytes()) for p in files], label=task_dir.name)


def build_bundle_from_files(
    starter_files: list[dict],
    *,
    description_md: str = "",
    label: str = "task",
) -> tuple[bytes, str]:
    """Build the candidate bundle from DB task content.

    ``starter_files`` is the stored ``list[{path, content}]``; ``description_md``
    is synthesized into a top-level ``task.md`` (the candidate-facing brief) unless
    the starter files already provide one. Skips entries with a missing/empty path.
    Re-applies the private-file filter (defense-in-depth): the editor
    writes ``starter_files`` straight to the DB, so the protocol invariant — never
    ship ``weakness.yaml`` / ``spec.yaml`` / ``INTERVIEWER.md`` / ``_solution.py`` —
    is enforced on the serving path too, not only at seed time.
    Not cached — DB content is mutable, and a ~100 KB gzip is cheap to rebuild.
    """
    blobs: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for entry in starter_files:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        name = path.strip()
        if _is_private(name):
            logger.warning("build_bundle_from_files: skipping private file %r", name)
            continue
        content = entry.get("content")
        text = content if isinstance(content, str) else ""
        blobs.append((name, text.encode("utf-8")))
        seen.add(name)

    if description_md.strip() and "task.md" not in seen:
        blobs.append(("task.md", description_md.encode("utf-8")))

    return _pack(blobs, label=label)


def get_or_build_bundle(task_id: str, task_dir: Path) -> tuple[bytes, str]:
    """Return (bytes, sha256) from cache, or build it and cache it."""
    if task_id not in _cache:
        _cache[task_id] = build_bundle(task_dir)
    return _cache[task_id]


def invalidate_cache(task_id: str | None = None) -> None:
    """Invalidate the cache. None — clear the whole cache."""
    if task_id is None:
        _cache.clear()
    else:
        _cache.pop(task_id, None)

"""Allowed sandbox runtime images.

A task's `runtime_image` must be in this shipped whitelist (validated on save).
A per-workspace admin-configurable allowlist is a possible extension; for now
it's a constant matching the images built + shipped in `sandbox/images/`.
"""

from __future__ import annotations

# Shipped sandbox images (sandbox/images/{py,node}/Dockerfile).
ALLOWED_RUNTIME_IMAGES: tuple[str, ...] = (
    "vibony/sandbox-py:3.12",
    "vibony/sandbox-node:20",
)

# Fallback for a template with no `runtime_image` in spec.yaml (e.g. a minimal imported artifact).
DEFAULT_TEMPLATE_IMAGE = "vibony/sandbox-py:3.12"


def is_allowed_image(image: str) -> bool:
    return image in ALLOWED_RUNTIME_IMAGES


__all__ = ["ALLOWED_RUNTIME_IMAGES", "DEFAULT_TEMPLATE_IMAGE", "is_allowed_image"]

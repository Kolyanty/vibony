"""Generator for session codes in the XXXX-XXXX format.

The alphabet is uppercase letters and digits without visually ambiguous
characters (0/O, 1/I/L, 2/Z, 5/S, 8/B). This matters for interviews: the
candidate receives the code by voice or in chat and must read it without errors.

Uses `secrets.choice` — a cryptographically strong source, no reason to skimp on
randomness. Space size: 24^8 ≈ 1.1 × 10^11. Collisions are caught in the DB by
the unique constraint on `sessions.code`, and the caller retries generation.
"""

from __future__ import annotations

import secrets

# 24 characters: A-Z + 0-9 minus B,I,L,O,S,Z and 0,1,2,5,8,9
SESSION_CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY3467"
SESSION_CODE_BLOCK_LEN = 4


def generate_session_code() -> str:
    """Return a string in the `XXXX-XXXX` format from the unambiguous alphabet."""

    left = "".join(secrets.choice(SESSION_CODE_ALPHABET) for _ in range(SESSION_CODE_BLOCK_LEN))
    right = "".join(secrets.choice(SESSION_CODE_ALPHABET) for _ in range(SESSION_CODE_BLOCK_LEN))
    return f"{left}-{right}"

"""Session lifecycle: REST, WS broker, bundle.

`PROTOCOL_VERSION` — the integer version of the protocol contract (see
[docs/protocol/session.md](../../../docs/protocol/session.md), the
`protocol_version` section). Initially 1. Bump only on removing/
redefining changes; additive changes need no bump.

Bumped 6 → 7: async mode adds the `timer_tick` advisory frame and the
inbound `session_submit` frame, plus `mode`/`time_limit_seconds` session config
fields. Technically additive, but the bump lets clients assert async support
cleanly.
"""

PROTOCOL_VERSION = 7
SESSION_TTL_HOURS = 8
# How long a minted replay share-token stays valid. Stamped at mint
# from now(); the share-token gates reject a past expiry like an invalid token.
SHARE_TOKEN_TTL_DAYS = 30


def is_expired(expires_at) -> bool:
    """True if a session's deadline has passed. The candidate gates
    treat an expired session like a completed one, so a leaked candidate_token
    stops working at the advertised TTL even if the session was never ended."""
    if expires_at is None:
        return False
    from datetime import datetime, timezone

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


def is_share_token_valid(stored: str | None, expires_at, presented: str) -> bool:
    """True if `presented` matches the stored replay share-token and it hasn't
    expired. Constant-time compare so the gate can't be probed
    by timing; a NULL stored token or a past expiry both fail (same as a wrong
    token → no completion-state leak)."""
    import hmac

    if not stored:
        return False
    if not hmac.compare_digest(stored, presented):
        return False
    return not is_expired(expires_at)

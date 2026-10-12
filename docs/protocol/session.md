# Session config, lifecycle, protocol version

**Context:** [README.md](README.md), [events.md](events.md), [ws-contract.md](ws-contract.md), [task-format.md](task-format.md), [architecture.md](../architecture.md).

This document describes what the backend gives the candidate on session startup, how a session changes status through its lifecycle, and how the contract is versioned.

---

## Token model

Three distinct tokens with separate lifetimes and transport channels:

| Token | Holder | Transport | Stored |
|-------|--------|-----------|--------|
| `user_session` | Interviewer | `HttpOnly` signed cookie (itsdangerous) | DB — user/cookie mapping |
| `candidate_token` | Candidate | URL query param (candidate link) | DB — `Session.candidate_token` (opaque uuid4) |
| `share_token` | Anyone with link | URL query param (`?share_token=`) | DB — `Session.share_token` (nullable) |

`candidate_token` replaces the legacy `owner_token == session_code` shortcut.

---

## Bootstrap sequence

The candidate opens the link `…/candidate?token=TOKEN` in a browser. The browser
IDE resolves the session by token before opening WS:

1. **`GET /sessions/by-candidate-token/{token}`** → gets [session config](#session-config) (JSON). If the session is `completed` or doesn't exist — corresponding error.

After this the browser opens the candidate-web WebSocket (see [ws-contract.md](ws-contract.md)); on the first connect the backend provisions the sandbox (laying down the task scaffold), starts the `AgentHost`, and emits `session_start`. On reconnect the backend replays the missed events from `last_seq` over the WS — the client does not fetch the event log over REST.

The sandbox scaffold (the candidate's starter files) is provisioned server-side from the task bundle; the browser never downloads a tarball.

---

## Session config

JSON object returned by `GET /sessions/{code}`:

```json
{
  "session_code": "K7M2-P9Q4",
  "session_status": "pending | active",
  "protocol_version": 7,
  "ws_token": "<candidate_token>",
  "task_id": "task-01-training-loop",
  "task_dir_name": "training-loop",
  "mode": "live",
  "time_limit_seconds": null,
  "ai_config": {
    "primary_model": "deepseek-v4-flash",
    "fallback_models": ["anthropic-haiku-4-5"]
  },
  "bundle_sha256": "ab12cd34...",
  "created_at": "2026-05-14T18:00:00Z",
  "expires_at": "2026-05-15T02:00:00Z"
}
```

### Fields

| Field | Type | Value |
|-------|------|-------|
| `session_code` | string `XXXX-XXXX` | Duplicates the path parameter, for convenience of logging |
| `session_status` | enum | `pending` (created, nobody connected) or `active` (candidate already connected). When trying to open `completed` — backend returns 410 Gone, not this JSON |
| `protocol_version` | int | Backend contract version. Currently `7` |
| `ws_token` | string | Opaque `candidate_token` (DB-stored, single session, uuid4). Passed as `?token=` in the candidate WS URL. Also returned as `candidate_token` in the `POST /sessions` response (interviewer side), which builds the candidate link |
| `task_id` | string | Task **slug** (DB `tasks.slug`; for reference tasks == the repo folder name). The session also stores `task_ref_id` (FK → `tasks`) internally — only the slug is on the wire |
| `task_dir_name` | string | Name of the candidate's working directory (`training-loop/`). Just a human-readable name, not a path |
| `mode` | enum | `live` (default) or `async`. `live` = interviewer watches in real time, no timer. `async` = timer, no interviewer, AI-generated assessment. Set at creation, immutable |
| `time_limit_seconds` | int or null | Async deadline window in seconds. Required for `mode=async` (range 300–14400, i.e. 5 min – 4 h); must be `null`/absent for `mode=live`. The server-side deadline is `started_at + time_limit_seconds`; `started_at` is set when the candidate first connects. |
| `ai_config` | object | See below |
| `bundle_sha256` | hex string or null | SHA-256 of the server-built task scaffold (built from the DB task's `starter_files` + `description_md`, not the filesystem). Null until first built |
| `created_at` | ISO 8601 UTC | Session creation time |
| `expires_at` | ISO 8601 UTC | `created_at + 8h` (hard timeout from [architecture.md](../architecture.md)) |

### `ai_config`

| Field | Type | Value |
|-------|------|-------|
| `primary_model` | string | Primary model ID in litellm notation (`deepseek-v4-flash`, `anthropic-haiku-4-5`, ...) |
| `fallback_models` | array of strings | Fallback chain on primary 5xx/timeout. Order matters. At least one fallback from a different provider (see [architecture.md](../architecture.md), Graceful degradation). Empty array = no fallback (allowed for dev environments) |

The weakness prompt itself does **not** go to the client. It only exists on the backend (the DB `tasks.weakness` mapping, rendered once per session via `render_weakness_dict`) and is injected into LLM requests inside the AI proxy. This is critical: if the weakness config were given to the client, the candidate could view the instructions and prepare to circumvent them.

---

## Session config by candidate token

The browser candidate fetches session config by token (no session code in URL):

```
GET /sessions/by-candidate-token/{token}
```

Public endpoint — no cookie auth required. Returns the same `SessionConfig` JSON as `GET /sessions/{code}`. Used by web candidate UI on first load (it only knows the token from the URL).

---

## Task bundle

The task bundle is the tarball with **only the candidate's portion** of the task (see [task-format.md](task-format.md) for what's included and excluded). It is built server-side from the DB task's `starter_files` + a synthesized `task.md` (from `description_md`), with the private-file filter re-applied — not from the filesystem. `GET /sessions/{code}/bundle.tar.gz` still exposes it (`bundle_sha256` is its checksum) and it is laid down into the candidate's sandbox during provisioning — the browser candidate never downloads or unpacks it.

**Sandbox provisioning.** On the candidate's first `/candidate-web` connect the backend provisions a sandbox: it lays down the bundle scaffold as the working directory (the cwd for the agent's shell + file tools). On a backend restart mid-session the sandbox is re-provisioned and the file state is restored from the `file_changed` event log, so the candidate's edits survive; the missed events are replayed over the WS from `last_seq`.

---

## Session lifecycle

```
                   POST /sessions
                   (interviewer)
                        │
                        ▼
                   ┌─────────┐
                   │ pending │
                   └────┬────┘
                        │ first successful /candidate-web WS connect
                        ▼
                   ┌─────────┐
                   │ active  │
                   └────┬────┘
                        │ interviewer clicked "end"
                        │ OR created_at + 8h
                        │ OR fatal error on backend
                        ▼
                   ┌──────────┐
                   │completed │
                   └──────────┘
```

| Transition | Trigger | Who emits event | Effect |
|-----------|---------|-----------------|--------|
| `(none) → pending` | `POST /sessions` from interviewer | — | Session config created in DB; `candidate_token` (uuid4) issued and returned in response. `mode` (`live`/`async`) + `time_limit_seconds` fixed here |
| `pending → active` | `AgentHost.start()` on first `/candidate-web` connect | backend emits `session_start` | Hard timeout of 8 hours from `created_at` starts |
| `active → completed` | Interviewer clicks "End" in dashboard (`POST /sessions/{code}/end`, cookie auth) | backend emits `session_end(reason: interviewer)` | LLM proxy starts rejecting; WS connections close with `1000` |
| `active → completed` | Candidate ends early (`POST /sessions/{code}/end?candidate_token=`) | backend emits `session_end(reason: candidate_quit)` | Same |
| `active → completed` | `created_at + 8h` reached | backend emits `session_end(reason: timeout)` | Same |
| `active → completed` | Fatal error on backend (repeated failures of all models in `ai_config`) | backend emits `session_end(reason: error, details: ...)` | Same |
| `active → completed` | Candidate clicks **Submit** in an `async` session (`session_submit` WS frame) | backend emits `assessment_pending` + `session_end(reason: submitted)` | Sandbox torn down; countdown stopped |
| `active → completed` | `async` deadline (`started_at + time_limit_seconds`) reached | server-side timer emits `assessment_pending` + `session_end(reason: timeout)` | Same |

After `completed`:
- `POST /sessions/{code}/end` is idempotent (repeated call — no-op).
- Candidate endpoint WS returns close `4409` on connection attempt.
- Interviewer endpoint WS works in read-only mode: backend streams the full event log from `seq 0` to end and keeps the connection open (for possible future interviewer notes; in read-only).
- LLM proxy rejects any requests on this session (budget protection).
- Event log remains accessible via `GET /sessions/{code}/events` (cookie auth) and `GET /sessions/{code}/replay?share_token=...` (shared-link access).

**Replay share-token lifecycle.** `POST /sessions/{code}/share` (cookie auth, member) mints the `share_token` and is allowed **only on a completed session** (409 `not_completed` otherwise — the replay it unlocks is completed-only). The response is `{ share_token, expires_at }`; the token is stamped with an expiry (now + 30 days) at mint and a re-mint on an unexpired token is idempotent. `DELETE /sessions/{code}/share` (cookie auth, member, 204) revokes it — nulls the token + expiry so every outstanding replay link stops working. The share-token gates (`/replay`, share-scoped `/events`) compare the token in constant time and reject a past `expires_at` the same as a wrong token (uniform 403 — no completion-state leak). A `NULL expires_at` (a token minted before expiries were introduced) is treated as non-expiring.

**Subject erasure.** `DELETE /sessions/{code}` (cookie auth, member, 204) **irreversibly** hard-deletes the session and every piece of candidate personal data it holds — the transcript event log, the persisted LLM message blob (`agent_messages`), and the AI assessment (events + `session_assessments` cascade at the DB via `ON DELETE CASCADE`). It also tears down any live in-process runtime (agent host / async timer / fs-proxy) and scrubs the session's own audit rows. After erasure the replay/timeline for that session stop resolving (404). Non-members get 403, unauthenticated 401 (same gate as the other interviewer endpoints).

**Async mode.** For `mode=async` sessions, `started_at` is set
when the candidate first connects (`/candidate-web`) and anchors the
server-side deadline (`started_at + time_limit_seconds`). A server-side
countdown timer publishes advisory `timer_tick` frames (~every 10 s, see
[ws-contract.md](ws-contract.md)) and, at the deadline, drives the terminal
transition. Both async terminal paths — the candidate's `session_submit` frame
and the timer deadline — record `assessment_pending` (assessment-job stub)
+ `session_end` with `reason: submitted` / `timeout`, then tear down the
sandbox. The timer is server-authoritative: it is not cancelled on candidate
disconnect and is re-established on reconnect (recomputing the deadline from the
persisted `started_at`). On a backend restart the timer is re-armed for every
already-anchored active async session, so a deadline still fires even if the
candidate never reconnects.

**Server restart during `active`.** Session stays in DB, status doesn't change. The browser candidate and dashboard reconnect and catch up through replay. If there was an in-flight LLM call at crash time — it's lost; the candidate notices the absence of an `assistant_message` after their last `user_message` and re-sends the prompt.

---

## Error responses

REST endpoints return errors as `HTTPException` with a **structured `detail` object**:

```json
{
  "detail": {
    "reason": "not_found",
    "code": "AAAA-BBBB"
  }
}
```

`reason` — stable en-snake_case identifier, not user text. Client uses it as machine-id (for UI localization / code routing); RU/EN wordings are built on the client side. Additional fields (`code`, `status`, …) — reference.

Currently encountered reasons:

| `reason` | Where | HTTP code |
|---------|-------|----------|
| `not_found` | `GET /sessions/{code}`, `POST /sessions/{code}/end`, `GET /sessions/{code}/events`, `GET /sessions/{code}/bundle.tar.gz` | 404 |
| `unauthorized` | `POST /sessions/{code}/end`, `POST /v1/chat` (missing/wrong Bearer) | 401 / 403 |
| `completed` | `GET /sessions/{code}` on completed session, `POST /v1/chat` (unknown session or `status != "active"`) | 410 |

`POST /v1/chat` intentionally uses **the same reason `completed`** for both unknown session and already completed session — so the client can't distinguish these cases by response shape and enumerate the `session_code` space without knowing the token. `code`/`status` fields in the 410 response to `/v1/chat` are also absent for the same reason. For REST `GET /sessions/{code}` 404 and `/v1/chat` 410 are separated intentionally: REST gates the public bootstrap endpoint, `/v1/chat` — the private AI proxy.

**Back-compat.** Older backends or intermediate proxies (nginx with a rewritten body) could return `detail` as a string; clients must parse both formats and treat a string `detail` as a human-readable message without a reason. No `protocol_version` bump for this extension — an additive change (see below).

`event.type`, `tool_name`, event payload fields and WS `error.code` — remain stable en-identifiers, independent of the UI language.

---

## `protocol_version`

Integer version of the contract (this entire set of documents). Not semver: one number for the whole contract, including events, WS, session config, task format, Toolset.

### When to bump

| Change | Bump? |
|--------|-------|
| New event type | No |
| New field in `payload` of existing event | No |
| New field in session config | No, provided that field absence → correct fallback |
| New WS frame kind | No (old clients ignore) |
| Removing a field from payload | Yes |
| Semantic redefinition of a field (same name, different meaning) | Yes |
| Changing `seq` format (e.g., to uuid) | Yes |
| Removing/renaming an event type | Yes |
| Changing endpoint URL | Yes |
| Changing WS close code | Depends: new code — no, changed meaning of old code — yes |

Simple rule: **additive — no, removing/redefining — yes.**

### Compatibility check

The browser IDE (dashboard) is served by the same deployment as the backend, so the candidate client and backend are always on the same `protocol_version` — there is no separately versioned client to skew. The backend still advertises `protocol_version` in the session config and `hello` frame; a client that reads it can surface a mismatch, but in practice deploy coupling keeps them in lock-step.

No schema storage by version in the repository — the `main` branch always reflects the current `protocol_version`. History is via git.

---

## Not in scope

- Session pause/resume (separate `paused` status) — not needed for a 30–40-minute interview.
- Per-task override of the weakness prompt by the candidate — the weakness config is resolved server-side from `task_id`, never chosen by the candidate.
- Multi-interviewer on one session — one cookie auth, one dashboard. A possible future extension.

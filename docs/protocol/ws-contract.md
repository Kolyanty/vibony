# WebSocket contract

Current `PROTOCOL_VERSION = 7`.
**Context:** [README.md](README.md), [events.md](events.md), [session.md](session.md), [architecture.md](../architecture.md).

This document describes the transport layer: what WebSocket endpoints exist, how the handshake works, what frames flow in both directions, how reconnect is handled.

---

## Endpoints

Two WebSocket endpoints, all under the common backend:

| Path | Who connects | Purpose |
|------|-------------|---------|
| `wss://<backend>/sessions/{code}/interviewer` | Interviewer's dashboard | Receives events only (read-only) |
| `wss://<backend>/sessions/candidate-web?token=<candidate_token>&last_seq=N` | Browser candidate | Bidirectional: sends `user_message`/`inline_bash`/`bash_confirm_response`/`fs_request`/`session_submit`; receives `event`/`hello`/`error`/`bash_confirm_request`/`fs_response`/`timer_tick`/`agent_status` |

`{code}` — session code in `XXXX-XXXX` format (see [architecture.md](../architecture.md), Security).

Note: `/candidate-web` uses no `{code}` in the path — the session is resolved by `candidate_token` directly. The browser IDE is the only candidate runtime.

**Authorization.**
- Candidate web endpoint: `token` — opaque `candidate_token` (DB-stored, uuid4). Backend matches against `Session.candidate_token`. The token is the private credential, obtained from the `POST /sessions` response (interviewer side) and handed to the candidate as a link. Additionally `last_seq=N` for replay on reconnect.
- Interviewer endpoint: authenticated by `user_session` signed cookie (HttpOnly, set on login/signup). Backend verifies the cookie, loads the user, and checks `Membership(user_id, session.company_id)`. No `token` query param.

No/invalid cookie → close code `4401`. Invalid `candidate_token` → close code `4401`. Cross-tenant access (cookie valid but user not a member of the session's company) → close code `4403` (see §"Connection closure").

---

## Frame format

All frames — **WebSocket text frames with JSON payload**. No binary frames in MVP. Each frame is a JSON object with required field `kind`, further fields depend on kind.

```json
{ "kind": "event", "event": { ... } }
```

`kind` — discriminator. Unknown `kind` → client ignores (forward compatibility) or backend responds with `error` (if received from outside).

---

## Frame kinds

### From backend to candidate (filesystem)

The candidate never pushes raw `event` frames — session events are generated
server-side by the `AgentHost` and written via `store_event`. The browser
candidate's inbound frames (`user_message`/`inline_bash`/`fs_request`/…) are
documented in §"From web candidate to backend".

#### `fs_response`
Response to a candidate `fs_request`. Field `id` correlates with the request.
Backend resolves the op against the sandbox and sends the result back to the
browser over the candidate-web WS.

```json
{
  "kind": "fs_response",
  "id": "req-uuid-...",
  "op": "tree",
  "entries": [
    { "path": "train.py", "kind": "file", "size": 2048 },
    { "path": "data.py", "kind": "file", "size": 512 },
    { "path": "scratch", "kind": "dir" }
  ]
}
```

`kind` is `"file"` or `"dir"`. Only **empty** directories are emitted as `"dir"`
entries (`size` omitted) — non-empty directories are implied by their file paths.
This surfaces a freshly created empty dir (e.g. `mkdir`) in the tree. Consumers
must ignore entries with an unknown `kind` for forward compatibility.

For `op: "read"`:
```json
{
  "kind": "fs_response",
  "id": "req-uuid-...",
  "op": "read",
  "path": "train.py",
  "content": "import torch\n..."
}
```

For `op: "write"`:
```json
{ "kind": "fs_response", "id": "req-uuid-...", "op": "write", "path": "train.py" }
```

On error (path not found, binary file, invalid path, sandbox not ready):
```json
{ "kind": "fs_response", "id": "req-uuid-...", "error": "not found: train.py" }
```

### From backend to candidate and dashboard

#### `event`
A recorded event — with assigned `seq`. Sent to all active subscribers of the session (candidate-web WS + interviewer WS sessions) in `seq` order.

```json
{
  "kind": "event",
  "event": {
    "id": "8b1c4e3a-...",
    "seq": 142,
    "session_code": "K7M2-P9Q4",
    "timestamp": "2026-05-14T18:23:45.812Z",
    "type": "user_message",
    "payload": { "text": "Extend train.py to checkpoint/resume" }
  }
}
```

The candidate receives its own events back — the backend broadcasts every recorded event to all subscribers.

#### `error`
Processing error for an inbound candidate frame (e.g. a malformed `inline_bash`, an oversized frame, or a read-only interviewer frame).

```json
{
  "kind": "error",
  "id": "8b1c4e3a-...",
  "code": "PAYLOAD_INVALID | FRAME_TOO_LARGE | READ_ONLY | RATE_LIMITED | BUSY",
  "message": "inline_bash requires a non-empty cmd"
}
```

`id` is absent if the error is not tied to a specific incoming frame (e.g., protocol handshake violation).

`BUSY`: a `user_message` arrived while a turn is already in flight, or an `inline_bash` while a command is already running. The backend is single-flight per session (one turn, one candidate command at a time) and rejects the concurrent frame instead of queueing it — the client should wait for the in-flight work to finish (its events arrive normally) and retry. Clients may ignore it (the dashboard disables send while the assistant is responding).

#### `hello`
First frame after connection establishment. Backend communicates protocol compatibility and current stream state.

```json
{
  "kind": "hello",
  "protocol_version": 1,
  "session_status": "pending | active | completed",
  "last_seq": 142
}
```

- `protocol_version` — version of the contract the backend is running on.
- `session_status` — current session status. If `completed`, the client opens the connection in read-only mode (for viewing the recording).
- `last_seq` — `seq` of the last recorded event. On fresh connect = 0. On reconnect (see below) — `seq` of the event from which backend will start catch-up.

Session config (task, AI config, tokens) does **not** come via WS — the candidate gets it via REST before opening WS, see [session.md](session.md).

#### `fs_request`
Filesystem proxy request. The browser candidate sends it to the backend; the backend resolves the op against the sandbox and replies with a matching `fs_response` (same `id`). Backend waits 10 seconds then times out. (Listed here next to `fs_response` because the two form one round-trip; `fs_request` itself is a candidate→backend frame.)

```json
{
  "kind": "fs_request",
  "id": "req-uuid-...",
  "op": "tree"
}
```

For `op: "read"`:
```json
{
  "kind": "fs_request",
  "id": "req-uuid-...",
  "op": "read",
  "path": "train.py"
}
```

Paths are relative to the candidate's workdir. Private paths (starting with `.` or `_`, or `.pyc`/`.pyo`) are rejected by the backend. Absolute paths and `..` traversal are rejected by the backend.

#### `presence_updated` (updated)
Broadcast on every presence state change to all active connections (candidate + interviewer). Sent immediately after `hello` on both interviewer and candidate connect (initial state snapshot).

```json
{
  "kind": "presence_updated",
  "attendees": [
    {
      "name": "Alice",
      "role": "candidate",
      "is_active": true,
      "first_seen": "2026-05-21T10:00:00Z",
      "last_seen": "2026-05-21T10:00:00Z"
    },
    {
      "name": "john@example.com",
      "role": "interviewer",
      "is_active": true,
      "first_seen": "2026-05-21T10:00:00Z",
      "last_seen": "2026-05-21T10:00:00Z"
    }
  ]
}
```

- `is_active: true` — currently connected. `is_active: false` — was connected, disconnected.
- `role` — `"candidate"` or `"interviewer"`. Older server versions may omit it — clients must treat an absent value as `"candidate"`.
- `first_seen` — timestamp of first join for this name/token. (Field was `joined_at` in the draft — renamed to match the actual implementation.)
- `last_seen` — last moment the connection was observed live (updated on disconnect).
- List is ordered by `first_seen` ascending. Empty list `[]` is valid.
- Interviewer name is the user's email address (up to 64 chars). This is visible to all attendees including the candidate — intentional for transparency in a live interview.

#### `presence_alert` (backend → interviewer only)
Sent **only** to the interviewer WS when a duplicate simultaneous connection is detected on the same `candidate_token` (two browser tabs open at once). The candidate does not receive this frame.

```json
{ "kind": "presence_alert", "reason": "duplicate_connection" }
```

`reason` ∈ `duplicate_connection`. Future reasons may be added — client should ignore unknown values.

This is a transient WS frame — not stored in the event log and not replayed on reconnect.

#### `timer_tick` (backend → clients, async sessions only)
Advisory countdown frame for `mode=async` sessions. Published by the
server-side timer (~every 10 s) to all subscribers of the session. The client
displays `remaining_seconds` and may interpolate between ticks for smoothness,
but the **server is authoritative**: the deadline is
`started_at + time_limit_seconds`, and the timeout fires from the server-side
timer regardless of whether ticks reach the client. A candidate cannot extend
their time by dropping the connection.

```json
{ "kind": "timer_tick", "remaining_seconds": 1840 }
```

`remaining_seconds` — integer seconds left until the deadline (floored, never
negative). This is a transient WS frame — **not** stored in the event log and
not replayed on reconnect. The terminal signal at the deadline is a
`session_end` event with `reason: "timeout"` (see [events.md](events.md)), not a
separate frame.

#### `agent_status` (backend → clients)
Advisory hint that an agent turn is/ isn't in flight, so the candidate can show an
"AI is working…" indicator for the **whole** turn — including the silent gap
between an intermediate assistant message and the next LLM call, where a tool-heavy
turn would otherwise look idle. Published (drop-if-full) when a turn starts
(`working`) and ends (`idle`); a candidate that reconnects mid-turn is sent the
current `working` on connect (the frames are not replayed).

```json
{ "kind": "agent_status", "state": "working" }
```

`state` — `"working"` | `"idle"`. Transient WS frame — **not** stored in the event
log and not replayed. Forward-compatible: clients that don't know it ignore it (the
interviewer client does). Additive; no `PROTOCOL_VERSION` bump.

### From web candidate to backend (+, `/candidate-web` only)

#### `user_message`
Sends a new user turn to the running agent loop.

```json
{ "kind": "user_message", "text": "Add early stopping to train.py" }
```

Backend starts `submit_user_message` as a background task (recv loop stays alive). Agent's reply arrives as `event` frames (`assistant_message`, `tool_call`, `tool_result`, ...).

`text` is capped at 16 000 chars (over → `error` `PAYLOAD_INVALID`). Single-flight: if a turn is already running the frame is rejected with `error` `BUSY` rather than queued.

#### `fs_request`
Browser sends a filesystem request to read/write/list sandbox files. Backend resolves against the sandbox and replies with a matching `fs_response` (same `id`).

```json
{ "kind": "fs_request", "id": "req-uuid-...", "op": "tree" }
```

For `op: "read"`:
```json
{ "kind": "fs_request", "id": "req-uuid-...", "op": "read", "path": "train.py" }
```

For `op: "write"`:
```json
{
  "kind": "fs_request",
  "id": "req-uuid-...",
  "op": "write",
  "path": "train.py",
  "content": "import torch\n..."
}
```

Paths must be relative to the workdir. Private paths (`.*`, `_*`, `.pyc`/`.pyo`) are rejected by the backend.

#### `inline_bash`
Browser sends a shell command for execution in the sandbox.

```json
{ "kind": "inline_bash", "cmd": "pytest tests/" }
```

`cmd` is capped at 4096 chars (over → `error` `PAYLOAD_INVALID`). Single-flight: candidate inline-bash is serialised per session — a second command sent while one is running is rejected with `error` `BUSY`.

Backend classifies the command via the bash gate:
- `allow` — executes immediately; emits `command_run`/`command_output`/`command_finished` events.
- `deny` — emits `command_run` (with `permission_decision: "policy_deny"`) + `command_finished` (exit_code 1).
- `confirm` — sends `bash_confirm_request` frame (see below); waits up to 300 s for `bash_confirm_response`.

#### `bash_confirm_response`
Browser responds to a `bash_confirm_request` frame.

```json
{ "kind": "bash_confirm_response", "cmd_id": "abc123", "choice": "allow_once" }
```

`choice` ∈ `allow_once | allow_prefix | deny`.
- `allow_once` — execute once.
- `allow_prefix` — save extracted prefix for the session; execute.
- `deny` — reject; emits `command_run` (with `permission_decision: "user_deny"`) + `command_finished` (exit_code 1).

If no response within 300 s, backend treats as `deny`.

#### `bash_confirm_request` (backend → browser)
Backend sends this frame when a command requires user confirmation. This is a transient WS frame — not stored in the event log and not replayed on reconnect.

```json
{
  "kind": "bash_confirm_request",
  "cmd_id": "abc123",
  "cmd": "pip install numpy",
  "prefix": "pip install",
  "choices": ["allow_once", "allow_prefix", "deny"]
}
```

`prefix` may be `null` if the command has no extractable executable prefix (e.g., multi-segment chains). When `prefix` is `null`, the `allow_prefix` button should be disabled in the UI.

#### `session_submit` (async sessions only)
Candidate clicks **Submit** to end an async session early (before the deadline).

```json
{ "kind": "session_submit" }
```

- Valid only for `mode=async` sessions. On a `live` session the backend replies
  with `{ "kind": "error", "code": "NOT_ASYNC", ... }` and ignores it.
- On success the backend records a `session_end` event with `reason:
  "submitted"` (preceded by the `assessment_pending` stub, see
  [events.md](events.md)), tears down the sandbox, and stops the countdown
  timer. The `session_end` event is broadcast back so the candidate UI shows the
  end-screen. Idempotent — a second `session_submit` (or one racing the timeout)
  is a no-op once the session is `completed`.

#### `session_join`
Must be the **first frame** sent by the web candidate after `hello`. Any other frame before `session_join_ok` is received returns `{ kind: 'error', code: 'JOIN_REQUIRED' }`.

```json
{ "kind": "session_join", "name": "Alice" }
```

- `name` — display name chosen by the candidate. Non-empty, ≤ 64 characters.
- If the name is already taken by an **actively connected** presence in this session (case-insensitive) → backend replies with `session_join_error`.
- On success → backend replies with `session_join_ok` + broadcasts `presence_updated` to all connections.
- On reconnect: if `CandidateSocket` has a stored `joinName`, it auto-resends `session_join` after `hello`. Same name is accepted (same token = same person).

#### `session_join_ok` (backend → browser)
Sent in response to a successful `session_join`. After this frame the candidate may send any frame (`user_message`, `inline_bash`, etc.).

```json
{ "kind": "session_join_ok" }
```

#### `session_join_error` (backend → browser)
Sent in response to a rejected `session_join`. Connection remains open — candidate can retry with a different name.

```json
{ "kind": "session_join_error", "reason": "name_taken" }
```

`reason` ∈ `name_taken | invalid_name`. `invalid_name` covers empty name or length > 64.

### From dashboard to backend

Dashboard is a read-only client. The interviewer endpoint accepts no inbound application frames — any frame is answered with an `error` (`READ_ONLY`).

---

## Handshake

1. Client establishes WS connection to endpoint with valid token.
2. Backend verifies token, session status, rate limit.
3. Backend sends `hello` (see above).
4. Client:
   - Reads `protocol_version` from `hello`. The browser IDE is deploy-coupled to the backend, so a mismatch is not expected in practice; `4426` is reserved for it.
   - If `last_seq > local_last_seq` → backend replays missed events (see below).
   - On the candidate's first connect the backend emits `session_start`; on reconnect it resumes the live stream.

---

## Heartbeat

Standard WebSocket Ping/Pong frame, not application-level.

- Server sends `ping` every 20 seconds.
- Client responds with `pong` automatically (library behavior).
- If `pong` didn't arrive in 10 seconds — server closes connection with close code `1011`.
- Symmetrically: client sends `ping` every 20 seconds, didn't receive `pong` in 10 — considers connection dead, starts reconnect.

Application-level heartbeat frames are not used — WS-level ping/pong work out of the box in Python `websockets`, browsers, and standard reverse proxies (Caddy, nginx).

---

## Reconnect and replay

On WS break — the client reconnects with exponential backoff: 1s, 2s, 4s, 8s, ..., ceiling 30s. Between attempts the dashboard shows "reconnecting...".

**Replay by `seq`:**

1. Client keeps a local `last_seq` (last `seq` actually read from the stream). At session start = 0.
2. On reconnect, client establishes WS with the same endpoint + query param `?last_seq=<int>` (non-negative; the backend rejects the handshake on a negative value —).
3. Backend in `hello` frame sends server `last_seq` (last recorded).
4. If server `last_seq > client last_seq` — backend sends missed events (`seq > client last_seq`) in `event` frames, in `seq` order. After catch-up switches to live stream.
5. If server `last_seq == client last_seq` — nothing to catch up, immediately live.
6. If client `last_seq > server last_seq` — client is glitching (or server log was lost); backend sends `error` with code `SEQ_AHEAD_OF_SERVER` and closes connection `1011`.

**Backend behavior on candidate ↔ backend break:**
- Recorded events remain in DB.
- If backend is in the middle of an LLM stream at break time — it continues receiving the LLM response, records it in event log. The candidate will catch up on reconnect.
- Active `command_run` — continue running; `command_output`/`command_finished` are written to log.

**Behavior on dashboard ↔ backend break:**
- Same: reconnect + replay. No data loss.

---

## Connection closure

| Close code | Meaning | Initiator | Client action |
|-----------|---------|-----------|--------------|
| `1000` | Normal closure | client/backend | No action |
| `1001` | Going away (server restart) | backend | Reconnect |
| `1011` | Internal error / heartbeat dead | backend | Reconnect |
| `4401` | No/invalid cookie (interviewer) or invalid `candidate_token` (candidate) | backend | No reconnect, show auth error |
| `4403` | Cross-tenant: interviewer cookie valid but user not a member of session's company | backend | No reconnect, show "access denied" |
| `4404` | Session not found | backend | No reconnect, show "session not found" |
| `4409` | Session already ended | backend | No reconnect for writes; reads — readonly mode allowed (see below) |
| `4426` | Protocol version mismatch | client/backend | No reconnect, show "refresh / update" |
| `4429` | Rate limited | backend | Backoff with long interval (minute), then one attempt |

Codes `4xxx` — application-level (range allowed by WebSocket standard for applications).

**Origin guard.** When `VIBONY_ALLOWED_ORIGINS` is configured, both WS endpoints reject a handshake whose `Origin` is not in the allowlist **before `accept()`** — the browser sees an HTTP `403` handshake failure (no `4xxx` application close code is sent). Unset (dev) = no Origin check. This is a Cross-Site WebSocket Hijacking defense and is independent of the auth close codes above.

**Readonly mode for completed session.** When trying to open a candidate endpoint to a `completed` session, backend returns `4409` and closes. For viewing the recording, use the interviewer endpoint (cookie auth; session in `completed` — endpoint works in "serve the full log + don't accept new events" mode).

---

## Limits

| Limit | Value | Source |
|-------|-------|--------|
| Single frame size | 1 MB | Protection against explosive payloads (huge `command_output` chunk) |
| Single `command_output` payload size | 64 KB | Large outputs are sent in multiple events |
| Inbound candidate frame frequency | 100/sec, burst 500 | Rate limit for accidental loops |
| Connections from one IP | 5/min | From [architecture.md](../architecture.md), Security |

Exceeding frame limit → `error` with code `RATE_LIMITED`, on repeat → close `4429`.

---

## Not in scope

- Binary frames (for efficient transfer of large file diffs) — JSON is sufficient.
- WS compression (permessage-deflate) — can be enabled later at the server level, contract unchanged.
- Multiplexing multiple sessions in one WS — each session has its own connection.
- Subprotocol negotiation — `protocol_version` in `hello` is sufficient.


### Evidence persistence failures

`error` frames may use `code: "AGENT_FAILED"` when a web agent cannot preserve its
session evidence. The candidate should start a new session. Editor `fs_response`
success for writes follows durable `file_changed` storage; persistence failures return
an `error` instead of an acknowledged save. Private task/controller checkpoints and
per-role usage counters are not part of candidate payloads.

Editor writes run in a bounded queue (at most eight pending writes per session),
serialized with agent turns and candidate commands. Waiting for a write does not
block incoming Bash confirmations. A full queue or a closing session returns an
`fs_response.error`; accepted writes drain before `session_end`. A queued save can
outlast the browser's request timeout during a long agent turn; clients must not
treat a timeout as confirmation that the file was saved.

A quota refusal before a new assistant/controller request produces the normal
usage-limit notice, discards any unreviewed proposal, and preserves editor/terminal
access and assessment eligibility. Quota failure during an incomplete tool action
still withholds assessment. Compaction may be skipped when the quota is exhausted.

# Vibony Platform Architecture

**Context:** [README](../README.md)

Vibony is a self-hostable platform for running technical interviews via **vibe-coding**: a candidate solves a task with an AI assistant inside a browser IDE. The interesting engineering is the **trap-control mechanism** — the AI is deliberately weakened with a planted, controlled flaw, so the interview measures how well a candidate catches AI mistakes. This document describes how the platform is built.

---

## 1. Core Decision

Vibony is a **multi-tenant platform** with two long-lived components plus an ephemeral runtime per active session:

| Component | What | Where |
|---|---|---|
| **Backend** | FastAPI + PostgreSQL. Session lifecycle, AgentHost (in-process per active session), SandboxProvider, AI proxy, multi-tenant data | Single backend host |
| **Dashboard** | React + Vite + TS SPA. Employer admin (workspaces, tasks, sessions, members, reports), candidate IDE (browser-side editor + chat + console + file tree), replay viewer | Same origin as backend, served by Caddy/nginx |
| **Sandbox container** | Docker container per active session, language-specific image, ephemeral | On the backend host; managed by `DockerSandboxProvider` over the local Docker socket |

Both candidate and interviewer (employer) reach the platform through the browser. There is **no CLI runtime** — everyone works in the browser.

---

## 2. Two Interview Modes

Both modes share the same backend, dashboard, sandbox, AgentHost, and event schema. They differ in two product-level flags on a session:

| | **Async mode** | **Live mode** |
|---|---|---|
| Timer | Yes (configurable, typical 60 min) | No |
| Interviewer present | No | Yes, via dashboard `SessionPage` |
| Session end trigger | Candidate clicks Submit, or timer fires | Interviewer clicks End |
| AI assessment runs | Yes (post-session) | Yes (post-session), but the employer's live judgment is the primary signal |
| Real-time interviewer view | Not used (no one's watching) | Yes — `SessionPage` IDE layout shows the candidate's state in real time |

A session row carries `mode: 'async' | 'live'` so the dashboard and backend branch behaviour. Both modes use the same WS endpoints, the same event log, and the same sandbox.

---

## 3. Component Map

```
┌─────────────────────┐         ┌──────────────────────────────────────┐         ┌──────────────────┐
│  Candidate browser  │  WS     │  Backend                              │ docker  │  Sandbox         │
│  CandidatePage      │◀───────▶│  AgentHost(per session)               │◀───────▶│  Container       │
│  Monaco + chat +    │  REST   │  SandboxProvider (Docker via docker-py)│ exec    │  language image  │
│  xterm + tree       │◀───────▶│  AI proxy (litellm → DeepSeek/...)    │         │  workdir /work   │
└─────────────────────┘         │  Auth / orgs / usage limits           │         └──────────────────┘
                                 │  PostgreSQL: sessions, events, users  │
┌─────────────────────┐         │  Multi-tenant: Workspace/Member       │
│ Employer browser    │  WS     │                                       │
│ Dashboard:          │◀───────▶│                                       │
│ - SessionsListPage  │  REST   │                                       │
│ - SessionPage (live)│◀───────▶│                                       │
│ - ReplayPage        │         │                                       │
│ - TasksPage         │         │                                       │
│ - Assessment tab    │         │                                       │
│ - TeamPage          │         │                                       │
└─────────────────────┘         └──────────────────────────────────────┘
```

### Backend modules (under `backend/vibony_backend/`)
- `agent_host/` — per-session agent loop (LLM call → dispatch tool_calls → repeat). One `AgentHost` instance per active session, registry keyed by session code.
- `sandbox/` — `DockerSandboxProvider`, `SandboxToolset`, lifecycle reaper, config.
- `sessions/` — WS endpoints (`/candidate-web`, `/interviewer`), REST (session CRUD, bundle, file APIs), presence, event store.
- `tasks/` — task storage: `store` (`LoadedTask` + resolution seam), `seed` (reference-task import + session backfill), `schemas`/`rest` (`/tasks` CRUD), `images` (runtime-image whitelist).
- `db/` — SQLAlchemy models, Alembic migrations.
- `auth/` — password + OIDC, cookie sessions, `current_user`.
- `proxy.py` — LLM proxy (litellm).
- `weakness.py` — renders `configs/global.yaml` + a task's weakness mapping → system prompt. Resolved from the DB `weakness` JSONB via `render_weakness_dict` (resolved once at AgentHost start, passed via `ChatRequest.system_prompt`); filesystem `render_weakness(task_id)` is kept for the legacy `/v1/chat` path.

### Dashboard pages (under `dashboard/src/pages/`)
- `CandidatePage` — browser IDE for the candidate. Multi-column grid: file tree / Monaco / AI chat / console.
- `SessionPage` — interviewer's read-only view of an active session (live mode). Same layout as `CandidatePage`, with Monaco set to `readOnly` and no input bar.
- `ReplayPage` — read-only timeline of a completed session.
- `SessionsListPage` — employer creates and lists sessions.
- `LoginPage`, `SignupPage`, `AcceptInvitePage` — auth flows.
- `TeamPage` — workspace member management.
- `TasksPage` / `TaskEditPage` — task list + authoring form (hybrid: structured fields + raw-YAML weakness/spec editor; `TaskEditPage` is lazy-loaded). The assessment report is a tab in `SessionView`, not a separate page.

---

## 4. Sandbox Architecture

### Design

| Aspect | Decision |
|---|---|
| **Provider** | Own Docker via the `docker-py` SDK over the local Docker socket |
| **Runtime** | `runsc` (gVisor) for kernel isolation from the host, opt-in via `VIBONY_SANDBOX_RUNTIME=runsc`. Unset → `runc` (Docker default) on dev/CI machines that cannot run gVisor. Threaded into `containers.run(runtime=...)`; real escape verification requires a Linux host |
| **Container per session** | Ephemeral, created on first candidate WS connect, destroyed on session end / 2h idle / backend restart cleanup |
| **Image per task** | The task spec carries `runtime_image` (e.g., `vibony/sandbox-py:3.12`, `vibony/sandbox-node:20`). An initial set of supported images ships in `sandbox/images/` and is extensible |
| **Filesystem** | The task bundle is delivered via `put_archive` to `/work`. No bind-mount to the host. File reads/writes go through `exec` (`cat`, `tee`) — never raw host FS access |
| **Network** | Default-deny egress — the sandbox runs with `--network none`, so candidate code cannot reach the public internet. A `VIBONY_SANDBOX_NETWORK=bridge` switch opens the network for dev package installs; fine-grained per-host allowlisting is a planned refinement |
| **Resource limits** | `--memory`, `--cpus`, `--pids-limit`, `--ulimit nofile`. Backend-wide limits via `VIBONY_SANDBOX_*` environment settings; per-task overrides are not implemented |
| **Lifecycle** | Create → attach (WS) → exec (tool calls) → destroy. A 60s reconnect grace is preserved across brief disconnects |
| **Multi-host** | A single backend host runs all sandboxes. Moving to a sandbox fleet (multiple Docker hosts behind a queue) is a future extension for high concurrency |

### `SandboxProvider` interface

Six methods: `create(bundle, mount_path) -> SandboxHandle`, `exec(handle, cmd) -> (stdout, stderr, exit_code)`, `read_file(handle, path) -> str`, `write_file(handle, path, content) -> None`, `list_changed_since(handle, ts) -> list[str]`, `teardown(handle) -> None`. File-change detection is a poll of `list_changed_since` after each `inline_bash`, rather than a long-lived watcher — the same signal with less complexity.

This abstraction is what keeps "swap from a local-mode test shortcut to real Docker" out of the rewrite category, and what makes future migrations (to a K8s-based fleet, or to another sandbox backend) cheap. The contract is stable; implementations change behind it.

### Language-agnostic operation

Tasks declare their stack in `spec.yaml`:

```yaml
runtime_image: vibony/sandbox-py:3.12      # one of the allowed sandbox images
```

Initial shipped images:
- `vibony/sandbox-py:3.12` — Python 3.12 + CPU PyTorch + poetry + git + ca-certificates
- `vibony/sandbox-node:20` — Node 20 + npm + git

The task authoring UI locks `runtime_image` to a shipped whitelist (`ALLOWED_RUNTIME_IMAGES`), validated on save. Broadening this to arbitrary or operator-supplied image references is a straightforward extension.

---

## 5. AgentHost

One instance per active session, its lifecycle owned by the WS endpoint:

- **Owns:** `SandboxToolset` (wraps the `SandboxProvider` handle), conversation history (in memory and a durable message checkpoint), `EventSink` (writes through the broker → DB + WS fan-out).
- **Does:** runs the LLM loop (call LLM → dispatch tool_calls → loop), gates bash via `BashPolicy`, forwards `user_message` and `inline_bash` from the candidate, emits structured events.
- **Does not:** keep a live container across backend restarts. On restart the container is destroyed (the lifespan reaper), but the **event log is the source of truth for the sandbox FS**: a fresh sandbox is derived state, re-created from the task baseline and then re-hydrated by replaying `file_changed` events (`AgentHost._restore_fs_from_events`), so candidate edits, bash-created files, and deletions survive the restart. The employer "Reset sandbox" action deliberately skips the replay to land on the clean task baseline.
- **Async deadlines survive restart too:** the server-side countdown is in-process and is lost on restart. On reconnect the timer is re-armed from the persisted `started_at`; and `recover_async_timers` (lifespan startup) re-arms every already-anchored active async session so a deadline still fires even when the candidate never reconnects — a passed deadline drives the session straight to `timeout`.

### Durable runtime state and evidence

`agent_state` stores the task content, rendered assistant prompt, assistant/controller
model names, unlocked trap indices and controller failure/retry counters. They are
captured on first host start and restored on recreation. The state is private; it is
not serialized into candidate events or REST responses. Legacy sessions without a
checkpoint resolve their current task once. This is not a snapshot of provider model
weights or every future version of the controller/assessment prompt code.

`llm_usage` reserves a call under a PostgreSQL row lock before each model request.
Assistant, controller and compaction share the session quota; assessment has a separate
lifetime quota, including retries and regenerations. Failed/cancelled calls count.
Unknown token usage stays explicit; token caps are soft because usage arrives after a
response, and in-flight calls can overshoot. This is a token/call ledger, not dollar
billing. Calls outside session context (explicit development relay and offline harness)
are not charged to a session.

An in-progress turn/editor-write marker survives crashes. Lost event/message writes
stop the host, and editor saves are acknowledged only after the replay event is stored.
A session with interrupted work, integrity errors, controller fail-open or unresolved
violations cannot receive an automated assessment; its assessment job fails with an
explicit reason. Start a new session rather than resume a partial tool history.

The controller reviews assistant text and Write/Edit proposals before dispatch. While
any trap is locked, assistant Bash is blocked, including read-only commands and tests;
use Read/Glob/Grep for inspection. Candidate terminal commands remain available. This
conservative restriction avoids trying to infer arbitrary shell side effects, but it
is still a behavioral mechanism, not a security boundary.

Code at `backend/vibony_backend/agent_host/{host,loop,tools,bash_gate}.py`.

---

## 6. Multi-Tenancy Model

The auth/orgs foundation implements `User`, `Workspace`, `Membership`, `Invite`, `OAuthIdentity`. Sessions are scoped by `workspace_id`.

| Concept | Notes |
|---|---|
| **Usage limits** | A workspace carries a plan column (`trial` default). It is a stub for future usage enforcement; per-session LLM token budgets are the enforcement that exists today |
| **Task sharing** | Tasks are rows in the `tasks` table, scoped to a workspace. Visibility: `private` (author only) / `workspace` (members) / `public` (stored). Reference tasks are `is_template` rows owned by a system workspace, surfaced read-only to all |
| **Roles** | `admin` / `interviewer`. Any workspace member edits workspace tasks; private tasks are author-gated |

No org-of-orgs hierarchy. The workspace is the tenancy boundary. If an operator wants multiple departments isolated, they create multiple workspaces.

---

## 7. LLM Provider Strategy

### Primary
**DeepSeek** is the default model name through LiteLLM, not a hard dependency or a measured quality recommendation. The project has no comprehensive quality benchmark.

### Multi-provider via litellm
Anthropic, OpenAI, and any OpenAI-compatible HTTP endpoint are supported via the existing `litellm` integration. Provider keys live in the backend env — the operator who self-hosts owns the keys and sets them at deploy time.

### Weakness configuration
Per-task `weakness.yaml` instructs the AI to behave with specific failure patterns. Backend `weakness.py` renders `configs/global.yaml` (baseline behaviour) + the task weakness → system prompt. This is the **methodology asset** that calibrates the "weak AI" so candidate signal is extractable. See [weakness-format.md](weakness-format.md).

### Prompt resolution seam + model roles
Two concerns the `weakness.py` mechanism conflates are split so that traps become **controlled** (see [task-design/format.md](task-design/format.md) §4.5):
- **Content** (data, per-task, model-agnostic): a task's trap *contract* — `spec.yaml`'s `expected_ai_pitfalls` (trigger / flawed_behavior / correct_behavior / defense / check), the DB `weakness` mapping. A task is never rewritten per model.
- **Template** (code/config, in git): the *framing* that renders trap content into the assistant system prompt, the judge prompt, and any control prompts.

`prompt_seam.py` is the seam for the template concern. `resolve_prompt(model_id, kind)` returns the prompt variant for a `(model, kind)` slot, falling back to a `default` when no model-specific variant is registered (`kind` ∈ `assistant` | `judge` | reserved `controller`). Running a single assistant model populates only the `default` slots — the per-model dimension is **built but empty**; adding a model-specific variant later (a fallback model, a model swap, per-model tuning) is a registration, not a refactor, and never touches task data.

**Model roles are independent config dimensions:** `assistant_model` (under test, `VIBONY_ASSISTANT_MODEL` → proxy default), `judge_model` (independent — `VIBONY_JUDGE_MODEL`, may be a stronger model; **anti-circularity**: the judge must not be the system it grades), `controller_model` (`VIBONY_CONTROLLER_MODEL`).

### Graceful degradation
The web agent uses the session’s primary model. Provider or persistence failures stop the affected turn and mark its evidence incomplete. There is no automatic model-fallback guarantee.

---

## 8. Hosting and Infrastructure

### Deployment
- **Compose:** the hardened **`docker-compose.prod.yml`** — prod images (`Dockerfile.prod`), a `docker-socket-proxy` instead of a raw `docker.sock` mount, a one-shot migration job, Postgres not published to the host, and the dashboard as a static `vite build` served by Caddy. The root `docker-compose.yml` is **dev only**. A single host runs everything; scale-out via a sandbox fleet is a future extension.
- **Status:** the production Compose file is an unverified Linux template. Its bundled
  Postgres service does not configure the database TLS required by production startup;
  supply an external TLS database or provision certificates before using it.
- **Single worker:** active hosts, the broker, async timers and rate limiting are
  process-local. Run one backend worker/replica; durable checkpoints do not add
  distributed host ownership.
- **TLS:** Caddy / Let's Encrypt (ACME via `VIBONY_SITE_ADDRESS`) covers HTTP only.

### Local dev
- `docker compose up -d postgres` + uvicorn + vite.
- Sandbox image build: `docker build -t vibony/sandbox-py:3.12 sandbox/images/py/` (+ `vibony/sandbox-node:20 sandbox/images/node/`).
- `VIBONY_SANDBOX_PROVIDER=local` for backend tests that don't need Docker; `docker` in dev and prod.

---

## 9. Security Model

| Threat | Mitigation |
|---|---|
| Candidate runs malicious code in the sandbox to escape to the host | gVisor runtime; `--cap-drop=ALL`; no privileged; default-deny network; ephemeral container; reaper at backend startup; the backend reaches the daemon via a gated `docker-socket-proxy` (not a raw `docker.sock` mount) and runs non-root |
| Candidate exfiltrates the LLM prompt / proxy credentials | The LLM proxy is reachable only through the backend; the sandbox can't talk to the LLM directly — only `inline_bash` outputs go through the AgentHost, which mediates LLM calls |
| Unauthorized session access | `candidate_token` (UUIDv4) in the invite URL; interviewer cookie auth; WS handshake enforced; `share_token` for replay viewers |
| Cross-workspace data leakage | All session/event/task queries are scoped by `workspace_id` from `current_user`. Tested explicitly |
| LLM cost runaway | Per-session token budgets; auto-cutoff on budget exceed |
| Brute-force on session codes | `session_code` is 8 chars `[A-Z2-9]` (~40 bits) — sufficient combined with rate limiting (5 attempts/IP/min, in-memory) |
| Personal-data handling | Subject-erasure `DELETE /sessions/{code}` hard-deletes the transcript + assessment + scrubs audit; audit emails are resolved at read time, not frozen in metadata; audit retention window via `VIBONY_AUDIT_RETENTION_DAYS`; access logs |

### Out of scope (deliberate)
- Protection from "candidate runs a separate AI in a neighboring window" — unsolvable; resolved by a session-rules agreement.
- Distributed rate-limiting / Redis-backed limits — single-instance is enough until the backend is sharded.
- A full audit log of every employer action.

---

## 10. Repository Layout

```
vibony/
├── README.md                    ← project overview + self-host quick-start
├── LICENSE                      ← Apache-2.0
├── backend/                     ← FastAPI + PostgreSQL
├── dashboard/                   ← React + Vite + TS
├── sandbox/                     ← Dockerfiles for runtime images
│   ├── images/
│   │   ├── py/Dockerfile        ← vibony/sandbox-py:3.12
│   │   └── node/Dockerfile      ← vibony/sandbox-node:20
│   └── README.md
├── tasks/                       ← reference interview tasks (trap corpus)
├── configs/                     ← global.yaml (baseline weakness preamble)
├── docker-compose.yml           ← dev stack
├── docker-compose.prod.yml      ← hardened production stack
└── docs/
    ├── architecture.md          ← this file
    ├── weakness-format.md       ← weakness YAML reference
    ├── protocol/                ← wire contracts (events, WS, session, task format, Toolset, SandboxProvider)
    └── task-design/             ← task spec format + examples
```

Monorepo justification: a contract change touching backend + dashboard lands in one atomic commit. `docs/protocol/` is the source of truth — both sides reference it.

---

## 11. Protocol Versioning

`protocol_version: int` is carried in the session config and every WS frame. It is bumped on any change to the WS contract or event schema, so a client and server can detect a mismatch. Presence frames, async-mode events (`timer_tick` / `session_timeout` / `session_submitted`), and the `assessment_ready` event are all part of the current schema.

---

## 12. What's NOT in Scope

- **CLI candidate runtime** — the platform is browser-only.
- **Live container across backend restarts** — out. The container is ephemeral, but the sandbox **FS state** survives restarts via event-log replay (the event log is the source of truth for the sandbox FS; the container is derived state).
- **Multi-host sandbox fleet** — a future extension for high concurrency.
- **Container pool** — cold-start is ~1.2s, deferred until it becomes a UX complaint.
- **Numeric scoring / rubric panels** — removed. The AI report is qualitative.
- **Keystroke-level replay** — the event timeline plus file snapshots at key events, not every keystroke.
- **Mobile app, ATS integrations, candidate marketplace** — out.

---

## 13. Forward-looking notes

- **gVisor escape verification.** The `runsc` runtime is wired in and opt-in, but a real host-escape test requires a Linux host with gVisor installed.
- **Sandbox fleet.** The `SandboxProvider` abstraction is designed so that moving from a single Docker host to a multi-host fleet is an implementation swap behind a stable contract, not a rewrite.

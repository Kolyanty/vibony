# README media

`candidate-demo.gif` is a continuous recording of the actual candidate workspace,
with typed input, animated responses and an edit in Monaco. `candidate-demo.png`
is a still from the corrected-code step. Both are generated
by [capture-readme-demo.cjs](../../scripts/capture-readme-demo.cjs).

## Provenance

The dialogue and file changes are authored synthetic fixtures, not a recorded LLM
session or benchmark output. The example is a small orders query written for this
demonstration, not a measured run of one of the reference tasks. No SQL, candidate
code, model or assessment is executed. The capture shows:

1. A candidate typing a request for their recent orders.
2. An assistant proposal missing an ownership filter.
3. The candidate typing a diagnosis of the missing `caller_id` condition.
4. A proposed correction using `WHERE user_id = ?` in the open editor.

Each frame retains a visible **SCRIPTED DEMO · Simulated streaming** label (apart from
the brief fade at the loop boundary). The frame, captions and corrected-line highlight
are capture-only annotations; the application inside is the real built dashboard.

**The current application receives complete assistant messages, not token streams.**
For this recording, a capture-only DOM animation gradually reveals the complete mocked
reply. This does not implement or claim production streaming. The recording uses the
real candidate input, WebSocket client, chat components, file tree and editor, with
scripted responses through Playwright's HTTP and WebSocket interception. Nothing is
recorded in the replay view. The GIF does not represent model latency, prove controller
behavior, or demonstrate assessment accuracy.

## Reproduce locally

Requirements: Node.js from `dashboard/.nvmrc`, FFmpeg/ffprobe on PATH, and Playwright 1.62.1+ with its
Chromium browser. No backend, Docker, provider keys, account or public server is needed.

Build the dashboard from the repository root:

```bash
npm --prefix dashboard ci
npm --prefix dashboard run build
```

Install the capture dependency in a temporary directory so the application's package
manifest and lockfile stay unchanged:

```bash
media_tools=$(mktemp -d)
npm install --prefix "$media_tools" --no-save --package-lock=false playwright@1.62.1
"$media_tools/node_modules/.bin/playwright" install chromium
NODE_PATH="$media_tools/node_modules" node scripts/capture-readme-demo.cjs
```

An existing Chromium executable can be selected with `DEMO_CHROMIUM_PATH`. The script
uses an isolated browser context and intercepts application requests: HTML/assets come
from `dashboard/dist`, session responses come from the synthetic fixture, and all
unexpected requests fail. It never contacts the real application backend or an LLM.

The script overwrites only the two named media files and removes its temporary video.
Review the complete animation after regenerating, especially typing, text reveal,
scrolling, code changes and clipping. Capture reports page errors or unexpected requests.
Public release exports should deliberately include this directory and the capture
helper; raw personal recordings and real session data must stay excluded.

Capture uses Playwright's [screencast](https://playwright.dev/docs/api/class-screencast)
and [WebSocket interception](https://playwright.dev/docs/api/class-websocketroute).
GIF encoding uses FFmpeg's [palettegen](https://ffmpeg.org/ffmpeg-filters.html#palettegen)
and [paletteuse](https://ffmpeg.org/ffmpeg-filters.html#paletteuse) filters.

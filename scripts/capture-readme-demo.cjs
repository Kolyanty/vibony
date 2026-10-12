#!/usr/bin/env node
/** Record the real candidate UI with scripted HTTP/WS data and simulated streaming.
 * No model calls. Requires a built dashboard, Playwright 1.62.1+, Chromium and FFmpeg.
 * See docs/media/README.md for provenance and reproduction.
 */
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const dist = path.join(root, 'dashboard/dist');
const output = path.join(root, 'docs/media');
// A secure-context localhost origin is needed for the app's crypto.randomUUID.
// All HTTP and WS traffic is intercepted; no listener or backend is started.
const origin = 'http://localhost:4173';
const code = 'DEMO-ONLY';
const start = Date.parse('2026-10-12T10:00:00Z');
const scaffold = `"""Return the signed-in user's recent orders."""


def recent_orders(conn, caller_id, limit=20):
    raise NotImplementedError
`;
const query = `"""Return the signed-in user's recent orders."""


def recent_orders(conn, caller_id, limit=20):
    limit = max(1, min(limit, 100))
    return conn.execute(
        """
        SELECT id, user_id, total_cents
        FROM orders
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
`;
const fixed = query
  .replace('        ORDER BY id DESC', '        WHERE user_id = ?\n        ORDER BY id DESC')
  .replace('(limit,),', '(caller_id, limit),');
const config = {
  session_code: code, session_status: 'active', protocol_version: 6,
  task_id: 'Orders query', task_dir_name: 'demo-orders',
  ai_config: { primary_model: 'Demo assistant', fallback_models: [] },
  ws_token: '', bundle_sha256: null, created_at: new Date(start).toISOString(),
  expires_at: new Date(start + 3600000).toISOString(), mode: 'live',
};
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.png': 'image/png' };

async function main() {
  await fs.access(path.join(dist, 'index.html'));
  await fs.mkdir(output, { recursive: true });
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'vibony-demo-'));
  let browser;
  try {
    browser = await chromium.launch({
      ...(process.env.DEMO_CHROMIUM_PATH ? { executablePath: process.env.DEMO_CHROMIUM_PATH } : {}),
    });
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 },
      deviceScaleFactor: 1, locale: 'en-US', colorScheme: 'light', serviceWorkers: 'block' });
    const unexpected = [];
    await context.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== origin) {
        unexpected.push(request.url());
        return route.abort();
      }
      if (request.resourceType() === 'document') {
        return route.fulfill({ path: path.join(dist, 'index.html'), contentType: 'text/html' });
      }
      if (url.pathname === '/sessions/by-candidate-token/synthetic-demo') {
        return route.fulfill({ json: config });
      }
      const asset = path.resolve(dist, `.${decodeURIComponent(url.pathname)}`);
      if (asset.startsWith(`${dist}${path.sep}`)) {
        try {
          await fs.access(asset);
          return await route.fulfill({ path: asset, contentType: mime[path.extname(asset)] });
        } catch { /* Unknown requests fail below instead of reaching a backend. */ }
      }
      unexpected.push(url.pathname);
      return route.abort();
    });
    let socket;
    let seq = 0;
    let content = scaffold;
    const send = (frame) => socket.send(JSON.stringify(frame));
    const event = (type, payload) => send({ kind: 'event', event: {
      id: `demo-${++seq}`, seq, session_code: code,
      timestamp: new Date(start + seq * 1000).toISOString(), type, payload,
    } });
    const file = (text, source = 'agent') => {
      content = text;
      event('file_changed', { file_path: 'orders.py', content, source, diff: null,
        op: source === 'baseline' ? 'created' : 'modified' });
    };
    await context.routeWebSocket('**/*', (ws) => {
      if (!ws.url().startsWith('ws://localhost:4173/sessions/candidate-web?')) {
        unexpected.push(ws.url());
        ws.close();
        return;
      }
      socket = ws;
      ws.onMessage((raw) => {
        const frame = JSON.parse(String(raw));
        if (frame.kind === 'session_join') {
          send({ kind: 'session_join_ok' });
          event('session_start', { task_id: config.task_id,
            ai_config: { model: 'synthetic' }, protocol_version: 6, cli_version: 'demo' });
          file(scaffold, 'baseline');
        } else if (frame.kind === 'fs_request') {
          const response = { kind: 'fs_response', id: frame.id };
          if (frame.op === 'tree') response.entries = [{ path: 'orders.py', kind: 'file' }];
          else if (frame.op === 'read') response.content = content;
          else unexpected.push(`fs:${frame.op}`);
          send(response);
        } else if (frame.kind === 'user_message') {
          event('user_message', { text: frame.text });
          send({ kind: 'agent_status', state: 'working' });
        } else unexpected.push(`ws:${frame.kind}`);
      });
      send({ kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0 });
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.clock.setFixedTime(new Date(start + 60000));
    await page.goto(`${origin}/candidate?token=synthetic-demo`);
    await page.getByTestId('presence-name-input').fill('Alex');
    await page.getByTestId('presence-join-button').click();
    await page.getByTestId('tree-file-orders.py').click();
    await page.locator('.monaco-editor .view-lines').waitFor();
    await page.evaluate(() => document.fonts.ready);
    // Use the application's real resizing controls for a readable recording layout.
    for (const [selector, delta] of [['sidebarResizer', -40], ['rightResizer', -60]]) {
      const box = await page.locator(`[class*="${selector}"]`).boundingBox();
      await page.mouse.move(box.x + 2, box.y + 50);
      await page.mouse.down();
      await page.mouse.move(box.x + 2 + delta, box.y + 50, { steps: 12 });
      await page.mouse.up();
    }
    // Capture-only framing. Streaming is a visual reveal of an already complete
    // mocked response, NOT a new application protocol or production feature.
    await page.addStyleTag({ content: `
      body { background: #192a2b !important; }
      [class*="appShell"] { height: calc(100vh - 94px) !important; margin: 76px 16px 18px;
        background: var(--background); border-radius: 12px;
        box-shadow: 0 12px 36px #0004; overflow: hidden; }
      #capture-title { position: fixed; inset: 0 0 auto; height: 76px; padding: 15px 24px;
        background: #192a2b; box-sizing: border-box; color: #faf8f3;
        font: 700 23px Manrope, sans-serif;
        display: flex; align-items: center; justify-content: space-between; gap: 24px; }
      #capture-title small { display: block; font-size: 11px; color: #b9ccc8;
        margin-top: 4px; font-weight: 500; letter-spacing: .02em; }
      #capture-step { font-size: 14px; color: #b2e3c8; text-align: right; font-weight: 500; }
      .demo-reveal p::after { content: ''; display: inline-block; width: 2px; height: 1em;
        background: #13a68d; margin-left: 3px; vertical-align: -.12em; }
      .demo-line { background: #c8f3d9 !important; transition: background 1s; }
    ` });
    await page.evaluate(() => {
      const banner = document.createElement('div');
      banner.id = 'capture-title';
      banner.innerHTML = '<div>Vibony <small>SCRIPTED DEMO · Simulated streaming · No LLM calls</small></div><div id="capture-step">Work with AI. Catch what it misses.</div>';
      document.body.append(banner);
      // Install before emitting assistant messages so the complete text never
      // flashes on screen before the capture-only reveal starts.
      const observer = new MutationObserver(() => {
        for (const row of document.querySelectorAll('[data-role="assistant_message"]:not([data-demo-reveal])')) {
          const p = row.querySelector('p');
          if (!p) continue;
          row.dataset.demoReveal = 'working';
          row.classList.add('demo-reveal');
          const text = p.textContent;
          p.textContent = '';
          let i = 0;
          const timer = setInterval(() => {
            i = Math.min(i + 2, text.length);
            p.textContent = text.slice(0, i);
            // Follow the growing response using the actual chat scroller.
            let parent = row.parentElement;
            while (parent && getComputedStyle(parent).overflowY !== 'auto') parent = parent.parentElement;
            if (parent) parent.scrollTo({ top: parent.scrollHeight, behavior: 'smooth' });
            if (i === text.length) {
              clearInterval(timer);
              row.classList.remove('demo-reveal');
              row.dataset.demoReveal = 'done';
            }
          }, 45);
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });
    });
    const pause = (ms) => page.waitForTimeout(ms);
    const input = page.getByTestId('candidate-input');
    async function ask(text) {
      await input.click();
      await input.pressSequentially(text, { delay: 30 });
      await pause(350);
      await page.getByTestId('candidate-send').click();
      await pause(900);
    }
    async function answer(text) {
      send({ kind: 'agent_status', state: 'idle' });
      event('assistant_message', { text });
      await page.locator('[data-role="assistant_message"]').last()
        .locator('[class*="bubble"]').waitFor();
      await page.waitForFunction(() => {
        const rows = document.querySelectorAll('[data-role="assistant_message"]');
        return rows.length && rows[rows.length - 1].dataset.demoReveal === 'done';
      });
    }
    async function caption(text) {
      await page.locator('#capture-step').evaluate((el, value) => {
        el.textContent = value;
        el.animate([{ opacity: 0, transform: 'translateY(4px)' },
          { opacity: 1, transform: 'translateY(0)' }], { duration: 500 });
      }, text);
    }
    await page.mouse.move(1250, 780);
    await pause(600);
    const video = path.join(temp, 'candidate.webm');
    await page.screencast.start({ path: video, size: { width: 1280, height: 800 }, quality: 95 });
    await pause(1200);
    await ask('List the signed-in user’s latest orders.');
    await answer('I’ll add a bounded query, ordered newest first.');
    file(query);
    await caption('A plausible answer. One missing condition.');
    await pause(2100);
    await ask('caller_id is unused. Filter by user_id before LIMIT, or this exposes other users’ orders.');
    await answer('You’re right — the ownership filter is missing. I’ll bind caller_id in WHERE, before ordering and limiting the results.');
    file(fixed);
    await caption('The candidate spots it. The assistant corrects it.');
    await pause(400);
    await page.locator('.view-line').filter({ hasText: 'WHERE user_id = ?' })
      .evaluate((el) => el.classList.add('demo-line'));
    await page.mouse.move(1250, 780);
    await pause(1800);
    await page.screenshot({ path: path.join(output, 'candidate-demo.png') });
    await pause(2400);
    await page.screencast.stop();
    if (errors.length || unexpected.length) {
      throw new Error(JSON.stringify({ pageErrors: errors, unexpectedRequests: unexpected }));
    }
    const duration = Number(execFileSync('ffprobe', ['-v', 'error', '-show_entries',
      'format=duration', '-of', 'default=nw=1:nk=1', video], { encoding: 'utf8' }).trim());
    execFileSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', '-i', video,
      '-filter_complex', `fps=20,fade=t=in:st=0:d=0.3:color=0x192a2b,fade=t=out:st=${duration - 0.5}:d=0.5:color=0x192a2b,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3`,
      '-loop', '0', path.join(output, 'candidate-demo.gif')], { stdio: 'inherit' });
    console.log(`Saved candidate-demo.gif and candidate-demo.png (${duration.toFixed(1)} seconds)`);
  } finally {
    try { await browser?.close(); }
    finally { await fs.rm(temp, { recursive: true, force: true }); }
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });

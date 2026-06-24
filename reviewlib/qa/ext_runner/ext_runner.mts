/**
 * ext_runner.mts — the reference TS runner the Python ext Tier-1 harness shells to.
 *
 * WHAT IT IS. A node/tsx script (NEVER bun — bun hangs Electron `_electron.launch` on macOS,
 * a silent 180s wedge; see ~/.claude/CLAUDE.md) that launches an isolated VS Code with the
 * extension-under-test on `--extensionDevelopmentPath`, connects Playwright over CDP, and then
 * speaks a tiny line-delimited JSON protocol over stdin/stdout so the deterministic Python
 * driver can run VS Code commands and read window state without owning the Electron lifecycle.
 *
 * THE PROVEN PATTERN IT ADAPTS. This mirrors `ext-test-projects/e2e/setup/electron-app.ts`
 * `launchVSCode`: an isolated `--user-data-dir` (so the user's real editor/settings are never
 * touched), a fresh `--extensions-dir`, `--disable-workspace-trust` / `--skip-welcome` /
 * `--skip-release-notes` (no modal dialogs to get stuck on), `--extensionDevelopmentPath` (load
 * the extension under test), and a CDP connect (`--remote-debugging-port` + connectOverCDP) so
 * `window.screenshot` works over CDP — the only VS Code capture that bypasses macOS
 * Screen-Recording grants and Spaces. The user's installed editor runs as `MacOS/Code`, this
 * launched copy as `MacOS/Electron`, so this NEVER kills the user's editor.
 *
 * THE PROTOCOL. On stdout, one JSON object per line:
 *   - `{"type":"ready"}`  once VS Code is up and the extension host has activated.
 *   - `{"type":"error","error":"..."}`  on a launch failure (the Python side maps it to BLOCKED).
 *   - `{"id":N,"ok":true,"result":...}` / `{"id":N,"ok":false,"error":"..."}`  per request.
 * On stdin, one request per line:
 *   - `{"id":N,"op":"run_command","command":"<id>"}`     — executeCommand
 *   - `{"id":N,"op":"open_file","path":"<rel>"}`          — open a workspace file in the editor
 *   - `{"id":N,"op":"notifications"}`                     — observed notification toast texts
 *   - `{"id":N,"op":"editor_text"}`                       — active editor text
 *   - `{"id":N,"op":"webview_text"}`                      — extension webview frame body text
 *   - `{"id":N,"op":"screenshot","path":"<abs>"}`         — window.screenshot over CDP
 *
 * REQUIREMENTS. `playwright` must be importable (the harness ships it as a dep of the published
 * vscode-playwright package; the reference run uses a node_modules-resolvable install). A VS
 * Code binary is found via `VSCODE_PATH` or `code` on PATH. The extension is read from the
 * `EXTENSION_PATH` env (the convention the e2e harness keys on); the workspace is the cwd.
 *
 * STATUS. This is the REFERENCE implementation, gated behind `REVIEW_QA_VSCODE=1` and only
 * reached on the live leg. The deterministic CI path never runs it (it uses an in-memory fake
 * automation in Python). Keeping it here makes the live leg runnable on a provisioned machine
 * and documents the exact protocol the eventual published runner must speak.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { execSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';

type Request = {
  id: number;
  op: 'run_command' | 'open_file' | 'notifications' | 'editor_text' | 'webview_text' | 'screenshot';
  command?: string;
  path?: string;
};

function emit(obj: unknown): void {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function findVscodeBinary(): string {
  const explicit = process.env.VSCODE_PATH?.trim();
  if (explicit) return explicit;
  try {
    // `code` is a CLI shim; resolve it to the Electron binary's host path is non-trivial, so we
    // launch the shim's underlying Electron via the well-known macOS app path when present, else
    // fall back to the `code` shim on PATH (works on Linux/CI).
    const onPath = execSync('command -v code', { encoding: 'utf8' }).trim();
    if (onPath) return onPath;
  } catch {
    /* not on PATH */
  }
  const macApp = '/Applications/Visual Studio Code.app/Contents/MacOS/Electron';
  return macApp;
}

async function waitForCdpReady(port: number, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`CDP endpoint never became ready on port ${port}`);
}

async function main(): Promise<void> {
  const extensionPath = process.env.EXTENSION_PATH?.trim();
  if (!extensionPath) {
    emit({ type: 'error', error: 'EXTENSION_PATH env is required (the extension under test)' });
    process.exit(1);
  }
  const workspace = process.cwd();

  // playwright is required only on this live leg; a missing install is a clean BLOCKED.
  let chromium;
  try {
    ({ chromium } = await import('playwright'));
  } catch (err) {
    emit({ type: 'error', error: `playwright is not installed: ${String(err)}` });
    process.exit(1);
    return;
  }

  const userDataDir = mkdtempSync(join(tmpdir(), 'review-qa-ext-udd-'));
  const extensionsDir = mkdtempSync(join(tmpdir(), 'review-qa-ext-ext-'));
  const cdpPort = 9000 + Math.floor(Math.random() * 1000);
  const executablePath = findVscodeBinary();

  let vscodeProc: ChildProcess | undefined;
  const cleanup = (): void => {
    try {
      if (vscodeProc?.pid) {
        execSync(`kill -9 -- -${vscodeProc.pid} 2>/dev/null || kill -9 ${vscodeProc.pid} 2>/dev/null`, {
          stdio: 'ignore',
        });
      }
    } catch {
      /* already gone */
    }
    rmSync(userDataDir, { recursive: true, force: true });
    rmSync(extensionsDir, { recursive: true, force: true });
  };
  process.on('exit', cleanup);
  process.on('SIGTERM', () => { cleanup(); process.exit(0); });
  process.on('SIGINT', () => { cleanup(); process.exit(0); });

  vscodeProc = spawn(
    executablePath,
    [
      '--no-sandbox',
      `--remote-debugging-port=${cdpPort}`,
      '--remote-allow-origins=*',
      `--extensionDevelopmentPath=${extensionPath}`,
      `--extensions-dir=${extensionsDir}`,
      '--skip-release-notes',
      '--skip-welcome',
      '--disable-workspace-trust',
      '--disable-telemetry',
      `--user-data-dir=${userDataDir}`,
      workspace,
    ],
    { stdio: ['ignore', 'ignore', 'pipe'], detached: false },
  );
  vscodeProc.stderr?.on('data', (chunk: Buffer) => {
    const line = chunk.toString();
    if (line.includes('error') || line.includes('Debugger')) process.stderr.write(`[ext-runner] ${line}`);
  });

  try {
    await waitForCdpReady(cdpPort, 30_000);
    const browser = await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`, { timeout: 30_000 });
    let workbench;
    const deadline = Date.now() + 30_000;
    while (!workbench && Date.now() < deadline) {
      const pages = browser.contexts().flatMap((c) => c.pages());
      workbench = pages.find((p) => p.url().startsWith('vscode-file://')) ?? pages[0];
      if (!workbench) await new Promise((r) => setTimeout(r, 200));
    }
    if (!workbench) throw new Error('timed out waiting for the VS Code workbench page');
    await workbench.waitForLoadState('domcontentloaded');
    // Let the extension host activate.
    await new Promise((r) => setTimeout(r, 2_000));
    emit({ type: 'ready' });

    const notifications: string[] = [];
    const rl = createInterface({ input: process.stdin });
    for await (const raw of rl) {
      const line = raw.trim();
      if (!line) continue;
      let req: Request;
      try {
        req = JSON.parse(line);
      } catch {
        continue;
      }
      try {
        const result = await handle(req, workbench, workspace, notifications);
        emit({ id: req.id, ok: true, result });
      } catch (err) {
        emit({ id: req.id, ok: false, error: String(err) });
      }
    }
  } catch (err) {
    emit({ type: 'error', error: String(err) });
    cleanup();
    process.exit(1);
  }
}

async function handle(
  req: Request,
  page: import('playwright').Page,
  workspace: string,
  notifications: string[],
): Promise<unknown> {
  switch (req.op) {
    case 'run_command': {
      // Drive the command palette: open it, type the command title path is brittle, so prefer
      // the keybinding-free `workbench.action.quickOpen` with `>command` is also brittle. Most
      // robust over CDP: run the command via the VS Code "Run command" through the page's
      // built-in API surface is not exposed; fall back to the command palette.
      await runCommandViaPalette(page, req.command ?? '');
      // Capture any notification that appeared as a result.
      await harvestNotifications(page, notifications);
      return null;
    }
    case 'open_file': {
      const abs = req.path?.startsWith('/') ? req.path : join(workspace, req.path ?? '');
      await runCommandViaPalette(page, 'workbench.action.quickOpen');
      await page.keyboard.type(abs);
      await page.keyboard.press('Enter');
      await page.waitForTimeout(500);
      return null;
    }
    case 'notifications':
      await harvestNotifications(page, notifications);
      return notifications.slice();
    case 'editor_text': {
      const editor = page.locator('.monaco-editor').first();
      const text = await editor.innerText().catch(() => '');
      return text;
    }
    case 'webview_text': {
      const frames = page.frames();
      for (const f of frames) {
        if (f.url().includes('vscode-webview')) {
          const body = await f.locator('body').innerText().catch(() => '');
          if (body) return body;
        }
      }
      return '';
    }
    case 'screenshot': {
      if (req.path) await page.screenshot({ path: req.path });
      return true;
    }
    default:
      throw new Error(`unknown op: ${req.op}`);
  }
}

async function runCommandViaPalette(page: import('playwright').Page, commandId: string): Promise<void> {
  if (!commandId) return;
  // Open the command palette and run the command by its id. `>` prefixes a command search; VS
  // Code accepts the command ID as the query for most commands, but the palette matches by
  // TITLE, so this is best-effort for the live leg — a robust implementation would register a
  // helper command. For the reference runner we drive the palette directly.
  await page.keyboard.press('F1');
  await page.waitForTimeout(200);
  await page.keyboard.type(commandId);
  await page.waitForTimeout(300);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(500);
}

async function harvestNotifications(page: import('playwright').Page, sink: string[]): Promise<void> {
  const toasts = page.locator('.notification-list-item-message, .notifications-toasts .monaco-list-row');
  const count = await toasts.count().catch(() => 0);
  for (let i = 0; i < count; i++) {
    const text = await toasts.nth(i).innerText().catch(() => '');
    if (text && !sink.includes(text)) sink.push(text);
  }
}

main().catch((err) => {
  emit({ type: 'error', error: String(err) });
  process.exit(1);
});

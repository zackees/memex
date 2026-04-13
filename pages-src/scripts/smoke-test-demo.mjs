import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const repoRoot = path.resolve(root, '..');
const metadataPath = path.join(root, 'generated', 'sqlite3.metadata.json');
const allPhases = ['stock', 'strip', 'o1', 'o2', 'os', 'oz'];
const arg = process.argv[2];
const phases = arg === 'all' || !arg ? allPhases : [arg.toLowerCase()];

for (const phase of phases) {
  if (!allPhases.includes(phase)) {
    throw new Error(`Unsupported phase "${phase}". Expected one of: ${allPhases.join(', ')}, all`);
  }
}

if (!fs.existsSync(path.join(repoRoot, 'pages', 'index.db'))) {
  throw new Error('pages/index.db is required for local smoke tests.');
}

const server = startServer();
try {
  await waitForServer('http://127.0.0.1:8080/');
  for (const phase of phases) {
    buildDemo(phase);
    const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
    const result = await smokeTestPhase(phase, metadata);
    console.log(JSON.stringify(result));
  }
} finally {
  server.kill('SIGTERM');
}

function buildDemo(phase) {
  const env = { ...process.env, MEMEX_WASM_PHASE: phase };
  const result = spawnSync(npmCommand(), ['run', 'build:demo'], {
    cwd: root,
    env,
    stdio: 'pipe',
    encoding: 'utf8',
    shell: process.platform === 'win32',
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`build:demo failed for phase=${phase}\n${result.stdout}\n${result.stderr}`);
  }
}

function startServer() {
  return spawn(process.execPath, [path.join(root, 'scripts', 'serve-pages.mjs')], {
    cwd: root,
    env: { ...process.env, PORT: '8080', HOST: '127.0.0.1' },
    stdio: 'ignore',
  });
}

async function waitForServer(url) {
  for (let i = 0; i < 100; i++) {
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (res.ok) return;
    } catch {}
    await delay(200);
  }
  throw new Error(`Timed out waiting for ${url}`);
}

async function smokeTestPhase(phase, metadata) {
  const browser = await chromium.launch({
    headless: true,
    channel: 'chrome',
  });

  const consoleErrors = [];
  const pageErrors = [];
  const rangeRequests = [];
  const wasmResponses = [];
  const httpErrors = [];

  try {
    const context = await browser.newContext({ baseURL: 'http://127.0.0.1:8080' });
    const page = await context.newPage();

    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => pageErrors.push(String(err)));
    page.on('request', (request) => {
      if (request.url().endsWith('/index.db')) {
        rangeRequests.push(request.headers()['range'] || '');
      }
    });
    page.on('response', async (response) => {
      if (response.status() >= 400) {
        httpErrors.push({
          url: response.url(),
          status: response.status(),
        });
      }
      if (response.url().endsWith('/sqlite3.wasm')) {
        wasmResponses.push({
          status: response.status(),
          length: response.headers()['content-length'] || '',
        });
      }
    });

    await page.goto(`/?phase=${phase}`, { waitUntil: 'networkidle' });
    await page.waitForFunction(() => {
      const el = document.getElementById('status');
      return !!el && !el.classList.contains('loading');
    }, { timeout: 60000 });

    const statusText = await page.locator('#status').textContent();
    if (!statusText || statusText.startsWith('Error:')) {
      throw new Error(`Status indicates failure for phase=${phase}: ${statusText}`);
    }

    await assertQueryWorks(page, 'Build metadata', 'table tbody tr');
    await assertQueryWorks(page, 'Recent items', 'table tbody tr');
    await assertQueryWorks(page, 'Search: "ESP32"', 'table tbody tr');

    if (pageErrors.length) {
      throw new Error(`pageerror: ${pageErrors.join(' | ')}`);
    }
    const relevantHttpErrors = httpErrors.filter((entry) => !entry.url.endsWith('/favicon.ico'));
    const relevantConsoleErrors = consoleErrors.filter((message) =>
      /sqlite|wasm|chunk|bundle|worker|exception|uncaught/i.test(message)
    );
    if (relevantHttpErrors.length) {
      throw new Error(`http errors: ${JSON.stringify(relevantHttpErrors)}`);
    }
    if (relevantConsoleErrors.length) {
      throw new Error(`console error: ${relevantConsoleErrors.join(' | ')}`);
    }
    if (!rangeRequests.some(Boolean)) {
      throw new Error(`No range request observed for phase=${phase}`);
    }
    if (!wasmResponses.some((response) => response.status === 200)) {
      throw new Error(`sqlite3.wasm did not load successfully for phase=${phase}`);
    }

    return {
      phase,
      status: statusText,
      wasmBytes: metadata.outputSize,
      exportCount: metadata.exportCount,
      rangeRequests: rangeRequests.filter(Boolean).length,
      wasmResponse: wasmResponses[0],
    };
  } finally {
    await browser.close();
  }
}

async function assertQueryWorks(page, buttonText, rowSelector) {
  await page.getByRole('button', { name: buttonText }).click();
  await page.waitForFunction((selector) => {
    const rows = document.querySelectorAll(selector);
    const status = document.getElementById('results')?.textContent || '';
    return rows.length > 0 || status.includes('No rows returned.') || status.includes('Error:');
  }, rowSelector, { timeout: 60000 });

  const resultText = await page.locator('#results').textContent();
  if (!resultText || resultText.includes('Error:')) {
    throw new Error(`Query "${buttonText}" failed: ${resultText}`);
  }
}

function npmCommand() {
  return process.platform === 'win32' ? 'npm.cmd' : 'npm';
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

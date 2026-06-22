# IMPLEMENT.md — adding SQLite-over-HTTP search to a GitHub Pages site

> **You are reading the right file.** This is the "how do I build what memex
> builds" guide for LLM/AI assistants and engineers integrating SQLite +
> HTTP byte-range loading into another GitHub Pages project. It captures
> non-obvious lessons learned the hard way during memex's development. If
> you skip the *Critical pitfalls* section you will almost certainly hit
> "database disk image is malformed" and waste hours.

---

## What memex actually does (one paragraph)

memex builds a SQLite database (with FTS5 indexes) at CI time, deploys it
to GitHub Pages as a single static `.db` file, and the browser queries it
via HTTP `Range:` requests — fetching only the pages each query touches,
typically **< 1% of the database per query**. No backend, no API, no
download-the-whole-file step. The DB can grow to hundreds of MB without
penalizing page load.

The trick is entirely client-side: `sqlite-wasm-http` exposes a custom
SQLite VFS that intercepts page reads and turns them into ranged `fetch()`
calls against a static URL.

---

## Critical pitfalls (read this before writing code)

These are the things that took the longest to discover. Internalize them.

### 1. GitHub Pages gzips `application/octet-stream` and **ignores Range when it does**

When a browser requests `your-db.db` with the default headers
(`Accept-Encoding: gzip, deflate, br, zstd`) **and** a `Range:` header,
Fastly (the CDN behind GH Pages) does this:

1. Sees `Content-Type: application/octet-stream` → decides to gzip.
2. Sees `Accept-Encoding: gzip` → returns the gzipped representation.
3. **Silently ignores the `Range:` header** → responds with HTTP `200 OK`
   (not `206 Partial Content`) containing the entire gzipped file.
4. Sets `Content-Length:` to the **gzipped** size and `Content-Range:` is
   absent.

Reproduce it:

```bash
curl -fsS -I -X GET -H "Accept-Encoding: gzip" -r 0-4095 \
  https://your-site.github.io/your-db.db
# HTTP/1.1 200 OK
# Content-Length: 664016                    ← gzipped FULL size
# Content-Encoding: gzip                    ← compressed
# Vary: Accept-Encoding
# (no Content-Range — Range was ignored)
```

The library then reads what it thinks is "bytes 0–4095" of the DB but is
actually the first 4 KB of a gzip stream. SQLite sees garbage and reports
`database disk image is malformed`.

**This is not a memex problem, a `sqlite-wasm-http` problem, or a
`sql.js-httpvfs` problem.** It is a GitHub Pages + Fastly + range +
compression interaction. Any client library that issues ranged requests
without the workaround will fail the same way.

### 2. The fix is `Accept-Encoding: identity`

Tell the server you do not accept compression. Fastly then serves the
file uncompressed and honors `Range`:

```bash
curl -fsS -I -X GET -H "Accept-Encoding: identity" -r 0-4095 \
  https://your-site.github.io/your-db.db
# HTTP/1.1 206 Partial Content
# Content-Length: 4096                      ← the actual slice
# Content-Range: bytes 0-4095/9650176       ← the TRUE uncompressed size
# (no Content-Encoding)
```

### 3. `Accept-Encoding` is a "forbidden" request header — but works anyway

Per the WHATWG Fetch spec, JavaScript is not allowed to set
`Accept-Encoding`. Both `XMLHttpRequest.setRequestHeader()` and
`fetch()` are supposed to silently strip it.

**Chrome ignores the spec here.** In worker contexts, both Chrome and
Firefox forward `Accept-Encoding: identity` when set via `fetch()` (and
in many cases via XHR too). You can verify this in Chrome DevTools →
Network → click a request → scroll to "Request Headers" → expand
"Provisional headers are shown" or use CDP's `Network.requestWillBeSentExtraInfo`.

memex relies on this behavior. If you ever need to support a stricter
browser, fall back to chunked mode (see *Alternatives* below).

### 4. Use `sqlite-wasm-http`, **not** `sql.js-httpvfs`

Both libraries do the same job in principle, but only one is workable on
GH Pages:

| | `sqlite-wasm-http` (memex) | `sql.js-httpvfs` (phiresky) |
|---|---|---|
| Transport | `fetch()` | `XMLHttpRequest` |
| Custom headers via config | ✅ `createHttpBackend({ headers: {…} })` | ❌ no public API |
| GH Pages out of the box | ✅ with `Accept-Encoding: identity` | ❌ requires monkey-patching the worker |
| Worker pool / sync mode | ✅ both | sync only |
| Maintenance | Stale (~2023) | Stale (~2023) |

Both libraries are unmaintained. memex uses `sqlite-wasm-http` because
its headers config is the only natively-supported escape hatch for the
gzip problem.

### 4b. `sql.js-httpvfs` cannot be "fixed in place" with an XHR monkey-patch

If you already deployed `sql.js-httpvfs` and want to dodge the rewrite,
the obvious idea is: prepend a shim to `sqlite.worker.js` that wraps
`XMLHttpRequest.prototype.open` and injects `setRequestHeader('Accept-Encoding', 'identity')`
on every request. This does **not** work. Verified empirically on
Chrome 140 (Headless), Firefox 121, Safari 17:

- `XMLHttpRequest.setRequestHeader('Accept-Encoding', anything)` is
  **silently no-op'd** by all major browsers — including in worker
  contexts. There is no error thrown; the call simply has no effect.
- Wrapping `fetch()` in the worker scope does propagate the header
  successfully, but the failure mode is "all-or-nothing" — if the
  library uses XHR (as `sql.js-httpvfs` does), the fetch wrapper does
  nothing.

The wire-level proof (CDP capture from a real Chrome page that loaded
a patched `sql.js-httpvfs` worker):

```
REQ  accept-encoding='gzip, deflate, br, zstd'   range='(no range)'
RESP content-encoding='gzip' content-range='(no cr)' content-length='664016'

Page status:
  'failed: Length of the file not known.
   It must either be supplied in the config or given by the HTTP server.'
```

That error message — "Length of the file not known" — is the
`sql.js-httpvfs` signature for "I got a gzipped 200 instead of a
ranged 206 and have no way to know the real file size." If you see it
on GH Pages, you are hitting this exact problem.

**The real fix is to switch to `sqlite-wasm-http`.** XHR forbids the
header in spec AND in browser implementation; `fetch()` only forbids
it in spec. memex's library uses `fetch()` and threads the header
through `createHttpBackend({ headers })`. That's the supported path.

### 5. The library still needs patching even with custom headers

The default size-detection logic in `sqlite-wasm-http` does a HEAD-ish
probe whose response cannot be trusted under range/gzip interactions.
memex's `pages-src/scripts/patch-sqlite-wasm-http.mjs` rewrites two
files in `node_modules/` after install:

- `dist/vfs-sync-http.js` — switches the synchronous XHR probe to
  `Range: bytes=0-0` and reads the true file size from the
  `Content-Range` header (format `bytes 0-0/<TRUE_SIZE>`).
- `dist/vfs-http-worker.js` — same fix for the async fetch path.

Both patches also propagate the user-supplied `headers` option into the
probe request, so `Accept-Encoding: identity` is sent during the very
first byte too — otherwise the probe itself returns the gzipped full
file and the library records the wrong file size.

You can read the exact regex replacements at
[`pages-src/scripts/patch-sqlite-wasm-http.mjs`](pages-src/scripts/patch-sqlite-wasm-http.mjs).
If you ever bump `sqlite-wasm-http`, re-test the regexes — the upstream
dist file structure occasionally changes.

---

## Architecture

```
                       ┌─────────────────────────────────────────┐
                       │  GitHub Action (build-index.yml)         │
                       │  ──────────────────────────────────────  │
                       │  1. Crawl source repo                    │
                       │  2. Build mirror.db (incremental)        │
                       │  3. Project → presentation index.db      │
                       │     • chunks, search_porter,             │
                       │       search_trigram, meta               │
                       │  4. Stage _site/                         │
                       │     • index.db                           │
                       │     • memex.js + memex-*.js (bundle)     │
                       │     • sqlite3.wasm                       │
                       │     • index.html + style.css             │
                       │  5. actions/deploy-pages@v4              │
                       └────────────────┬────────────────────────┘
                                        │
                                        ▼
                       https://<owner>.github.io/<repo>/
                                        │
                                        ▼
                       ┌─────────────────────────────────────────┐
                       │  Browser (any user, any device)          │
                       │  ──────────────────────────────────────  │
                       │  • Load index.html                       │
                       │  • <script type="module" src="memex.js"> │
                       │  • memex.js spawns Web Worker            │
                       │  • Worker fetches sqlite3.wasm           │
                       │  • Worker probes index.db w/ Range 0-0   │
                       │      └─ Accept-Encoding: identity        │
                       │      └─ reads true size from             │
                       │         Content-Range                    │
                       │  • Each query → 3–10 ranged fetches      │
                       │    of 4 KB SQLite pages                  │
                       └─────────────────────────────────────────┘
```

Three actors:

- **Build time**: a workflow runs `action/build_index.py` which crawls
  the source repo and writes `index.db`.
- **Deploy time**: the workflow stages the bundle and calls
  `actions/deploy-pages@v4`. No intermediate `gh-pages` branch — the
  artifact is uploaded directly.
- **Runtime**: the user's browser talks only to the static file server.
  No backend exists. The "API" is `SELECT … FROM …` over HTTP.

---

## Step-by-step: adding this to a new project

### Step 1 — Pick the right library

```bash
npm install sqlite-wasm-http
```

Do **not** use `sql.js-httpvfs`. See pitfall #4.

### Step 2 — Vendor the patch script

Copy `pages-src/scripts/patch-sqlite-wasm-http.mjs` from this repo into
your project. Wire it into `package.json`:

```json
{
  "scripts": {
    "postinstall": "node scripts/patch-sqlite-wasm-http.mjs",
    "build": "node scripts/patch-sqlite-wasm-http.mjs && webpack ..."
  }
}
```

`postinstall` runs the patch automatically after `npm install`, so fresh
clones and CI runs work without extra steps. The build step re-runs it
defensively in case `node_modules/` was warm from a cache.

### Step 3 — Open the database with identity encoding

```js
import { createSQLiteThread, createHttpBackend } from 'sqlite-wasm-http';

const backend = createHttpBackend({
  maxPageSize: 4096,             // must match the DB's PRAGMA page_size
  timeout: 30000,
  cacheSize: 4096,               // KB of LRU page cache in the worker
  backendType: 'sync',           // single worker, no SharedArrayBuffer
  headers: {
    'Accept-Encoding': 'identity'  // ← THE critical line
  },
});

const db = await createSQLiteThread({ http: backend });

await db('open', {
  filename: 'file:' + encodeURI('https://owner.github.io/repo/index.db'),
  vfs: 'http',
});
```

memex wraps this in `openMemexDb(url, options)` — see
[`pages-src/memex.js`](pages-src/memex.js).

### Step 4 — Build a query helper

The promiser API is callback-style; flatten it into something ergonomic:

```js
async function query(db, sql, bind) {
  const columns = [];
  const rows = [];
  await db('exec', {
    sql, bind,
    callback: (msg) => {
      if (msg.row) {
        rows.push(msg.row);
        if (!columns.length && msg.columnNames) columns.push(...msg.columnNames);
      } else if (msg.columnNames && !columns.length) {
        columns.push(...msg.columnNames);
      }
    },
  });
  return { columns, rows };
}
```

### Step 5 — Build the index.db so queries actually use ranges

Two non-obvious schema rules under byte-range loading:

- **Build with the right page size**: `PRAGMA page_size = 4096;` before
  the first table is created (4 KB is the SQLite default and matches
  `maxPageSize` above). This minimizes per-request payload.
- **Build indexes for every search column**. `LIKE '%foo%'` becomes a
  full table scan, which on byte-range loading means downloading every
  page. Use FTS5 (`search_porter`, `search_trigram`) for name searches
  and B-tree indexes for exact lookups.
- **`VACUUM` at the end** so the file is compact and pages are
  contiguous — fewer requests per query.

memex's index builder uses FTS5 with two tokenizers in parallel:

```sql
CREATE VIRTUAL TABLE search_porter   USING fts5(path, title, body, tokenize='porter unicode61');
CREATE VIRTUAL TABLE search_trigram  USING fts5(path, title, body, tokenize='trigram');
```

Porter for stemmed word search ("running" matches "run"); trigram for
substring/fuzzy ("sqlit" matches "sqlite"). Both are FTS5-indexed so
queries cost a few range requests, not a full scan.

### Step 6 — Deploy via `actions/deploy-pages@v4`

memex uses this workflow shape (see `.github/workflows/build-index.yml`):

```yaml
permissions:
  contents: write
  pages: write
  id-token: write

jobs:
  build:
    steps:
      - uses: actions/checkout@v4
      - run: python action/build_index.py --out index.db
      - run: |
          mkdir -p _site
          cp index.db pages/index.html pages/style.css pages/bundle.js \
             pages/memex-*.js pages/sqlite3.wasm _site/
      - uses: actions/upload-pages-artifact@v3
        with: { path: _site }

  deploy:
    needs: build
    environment: { name: github-pages, url: ${{ steps.dep.outputs.page_url }} }
    steps:
      - uses: actions/deploy-pages@v4
        id: dep
```

You do **not** need a `gh-pages` branch. `deploy-pages` uploads the
artifact directly to Pages.

### Step 7 — Verify the deploy actually works

This is the step everyone skips. Three checks:

```bash
# 1. Range support advertised
curl -fsS -I -H "Accept-Encoding: identity" \
  https://owner.github.io/repo/index.db | grep -i accept-ranges
# Expect: Accept-Ranges: bytes

# 2. Range honored under identity encoding
curl -fsS -I -X GET -H "Accept-Encoding: identity" -r 0-4095 \
  https://owner.github.io/repo/index.db | grep -i 'http\|content-'
# Expect: HTTP/1.1 206 Partial Content
#         Content-Length: 4096
#         Content-Range: bytes 0-4095/<TRUE_SIZE>
#         (no Content-Encoding line)

# 3. Confirm gzip DOES break things (so you know your fix is load-bearing)
curl -fsS -I -X GET -H "Accept-Encoding: gzip" -r 0-4095 \
  https://owner.github.io/repo/index.db | grep -i 'http\|content-'
# Expect: HTTP/1.1 200 OK              ← Range IGNORED
#         Content-Encoding: gzip
```

If check #2 fails (anything but 206), your DB will not load. Stop and
debug before testing in a browser.

### Step 8 — Browser smoke test

Use Chrome DevTools or Playwright with CDP. The `Network` panel shows
what Chrome actually puts on the wire, including the headers your
provisional view might hide.

Playwright recipe (use CDP-level capture, not `req.headers`, because
Playwright's high-level API filters forbidden headers):

```python
client = await ctx.new_cdp_session(page)
await client.send("Network.enable")
client.on("Network.requestWillBeSentExtraInfo",
          lambda p: print("REQ", p["headers"].get("accept-encoding"),
                                p["headers"].get("range")))
client.on("Network.responseReceivedExtraInfo",
          lambda p: print("RESP", p["headers"].get("content-encoding"),
                                  p["headers"].get("content-range")))
```

Run a query on the page; check that requests show `accept-encoding:
identity` and responses show `content-range: bytes X-Y/TOTAL` with no
`content-encoding`. If `content-encoding: gzip` appears, the patch did
not take effect.

---

### 6. Two ways to consume memex: rebuild from source vs vendor `dist/wasm/`

You do not necessarily need to npm-install + webpack-build to use this
pattern. memex publishes a pre-built bundle at
[`dist/wasm/`](dist/wasm/) that any GitHub Pages site can vendor
directly — just copy the files into your bundle root and import:

```html
<script type="module">
import { openMemexDb, query } from './memex.js';
const { db } = await openMemexDb(new URL('your-db.db', location.href).href);
const { columns, rows } = await query(db, 'SELECT … FROM …', [bindParams]);
</script>
```

The companion files (`memex-NNN.js` chunks + `sqlite3.wasm`) are loaded
dynamically by `memex.js` at runtime, so they must sit next to it in
your bundle root.

**Pin to a commit SHA, not `main`.** The chunk filenames
(`memex-141.js`, `memex-901.js`, …) are webpack-generated IDs that
change every time memex is rebuilt. If you reference `main` and memex
republishes, your site breaks because `memex.js` tries to dynamic-import
a chunk ID that no longer exists in the published `dist/wasm/`. Example
download URL with pinned SHA:

```
https://raw.githubusercontent.com/zackees/memex/03fe8df…/dist/wasm/memex.js
```

When you bump the SHA, re-download the full set of `memex-*.js` files
and verify the new chunk list at the same path. memex's `pages-src/`
emits these names from webpack; a clean checkout + `npm run build` is
the canonical way to regenerate them.

### 7. memex's `query()` returns columns + rows arrays, not row objects

```js
const { columns, rows } = await query(db, 'SELECT a, b FROM t LIMIT 1');
// columns: ['a', 'b']
// rows:    [[1, 2]]
```

If your existing code accesses `row.a` / `row.b` (row objects, e.g.
from sql.js's `getAsObject()`), wrap `query()` and zip the result into
per-column objects:

```js
async function rowsAsObjects(db, sql, params) {
  const res = await query(db, sql, params);
  return res.rows.map(r => Object.fromEntries(res.columns.map((c, i) => [c, r[i]])));
}
```

Positional `?` placeholders work — pass an array as the third argument.
Named `$name` placeholders also work — pass an object instead.

### 8. Local development needs explicit MIME types for module scripts

When testing locally with Python's stdlib `http.server` (or
`RangeHTTPServer`), `.js` files are served as `Content-Type: text/plain`
on Windows because the system mime registry doesn't map `.js` →
`application/javascript`. Browsers strictly enforce MIME type for
`<script type="module">` and refuse to load:

> Failed to load module script: Expected a JavaScript-or-Wasm module
> script but the server responded with a MIME type of "text/plain".

Fix the local server:

```python
import mimetypes
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('application/wasm', '.wasm')
```

GitHub Pages serves the correct MIME type, so this is purely a
local-testing artifact. It is **not** the same problem as the
gzip-defeats-Range issue, but it produces an equally cryptic failure
that makes you think the deployment is broken. Eliminate it before you
debug anything else locally.

### 9. The status message matters for debugging

These exact strings, each pointing at a different failure mode, are
worth remembering:

| Status string in the page | What it actually means |
|---|---|
| `database disk image is malformed` | gzip-defeated-Range; library got bytes from a gzip stream and tried to parse as SQLite |
| `Length of the file not known. It must either be supplied in the config or given by the HTTP server.` | sql.js-httpvfs only — size-probe response had no `Content-Range` and `Content-Length` was the gzipped size |
| `Failed to load module script: … MIME type "text/plain"` | Local server MIME issue (see #8 above), not a real deployment problem |
| `Cannot install OPFS: Missing SharedArrayBuffer and/or Atomics` | Benign warning — sqlite-wasm-http tries OPFS first, falls back to in-memory; OPFS needs COOP/COEP headers that GH Pages doesn't set, and you don't need it for HTTP VFS anyway |

Surface them prominently in your loader's error path. The amount of
time saved by `dbStatus.textContent = err.message` vs a console-only
log is dramatic when triaging deployment issues.

## Common debugging mistakes

| Symptom | Cause | Fix |
|---|---|---|
| `database disk image is malformed` on first query | Range request returned gzipped full file | Add `Accept-Encoding: identity` (step 3) and re-run the patch (step 2) |
| Works on `python -m http.server` but fails on GH Pages | Local server doesn't gzip; GH Pages does | Same as above — use Playwright + CDP to compare wire headers |
| Works in curl but fails in browser | curl doesn't auto-send `Accept-Encoding: gzip`; the browser does | Same as above |
| Library reports file size 0 or `NaN` | Size probe response had no `Content-Range` and `Content-Length` was the gzipped size | Patch wasn't applied to `vfs-sync-http.js` / `vfs-http-worker.js` |
| 304 Not Modified loops / weird caching | Browser cached a bad copy from before the fix | Hard reload (Ctrl+Shift+R), or bump a query string `?v=<sha>` on the URL |
| First query is slow (500+ ms) but subsequent are fast | Normal — first query primes the LRU page cache | Raise `cacheSize` in `createHttpBackend` if memory permits |
| CORS error in console even though same origin | A redirect (e.g. `repo` → `repo/`) crossed origins | Use the canonical Pages URL (trailing slash on directories) |

---

## Alternatives we considered and rejected

- **Download the whole DB**: works for small DBs but defeats the point.
  Memex DBs can be hundreds of MB.
- **Chunked mode** (`sql.js-httpvfs` style — split the DB into
  `db.0000.partial`, `db.0001.partial`, …): works around gzip because
  each chunk is fetched whole, but adds a build-time split step, ships
  many tiny files (slow on cold cache), and complicates incremental
  rebuilds. Reserve for when range + identity stops working.
- **Switch to Cloudflare Pages / Netlify**: their `_headers` files can
  disable gzip per path. Cleaner per-request behavior but adds a hosting
  dependency outside the GitHub ecosystem. Keep this in reserve.
- **Service worker that injects `Accept-Encoding`**: works but requires
  SW registration, scope, and dev-vs-prod parity headaches. The
  in-worker `fetch()` override in `sqlite-wasm-http` is simpler.

---

## Files to read in this repo

- [`pages-src/memex.js`](pages-src/memex.js) — the public client API
  (`openMemexDb`, `query`, `fetchRows`, `getSchema`).
- [`pages-src/scripts/patch-sqlite-wasm-http.mjs`](pages-src/scripts/patch-sqlite-wasm-http.mjs)
  — the library patches (size detection + headers propagation).
- [`pages-src/scripts/prepare-wasm.mjs`](pages-src/scripts/prepare-wasm.mjs)
  — the WASM minify pipeline (`wasm-strip` + `wasm-opt -Oz`). Optional;
  shrinks `sqlite3.wasm` from ~1.5 MB to ~540 KB gzipped.
- [`action/build_presentation.py`](action/build_presentation.py) — how
  the presentation DB is shaped (FTS5 tokenizers, indexes, VACUUM).
- [`.github/workflows/build-index.yml`](.github/workflows/build-index.yml)
  — the full reference workflow.

---

## TL;DR for an LLM in a hurry

1. `npm install sqlite-wasm-http`.
2. Copy memex's `patch-sqlite-wasm-http.mjs` and run it as `postinstall`.
3. Open the DB with `headers: { 'Accept-Encoding': 'identity' }`.
4. Build the DB with FTS5 indexes; never `LIKE '%x%'` over big tables.
5. Deploy via `actions/deploy-pages@v4`.
6. Verify with `curl -r 0-4095 -H "Accept-Encoding: identity"` — must
   return `206 Partial Content` with `Content-Range`.
7. Re-verify in Chrome via CDP, not via the Playwright high-level API.

If any of those steps is unclear, re-read the *Critical pitfalls*
section. The cost of getting them wrong is hours of "why does SQLite say
the DB is malformed when I can `sqlite3 index.db` locally just fine".

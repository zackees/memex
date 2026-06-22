# IMPLEMENT.md — adding SQLite-over-HTTP search to a GitHub Pages site

> **You are reading the right file.** This is the "how do I build what memex
> builds" guide for LLM/AI assistants and engineers integrating SQLite +
> HTTP byte-range loading into another GitHub Pages project. It captures
> non-obvious lessons learned the hard way during memex's development. If
> you skip the *Critical pitfalls* section you will almost certainly hit
> "database disk image is malformed" and waste hours.

## Contents

1. [What memex actually does](#what-memex-actually-does-one-paragraph)
2. [Two integration paths](#two-integration-paths-vendor-vs-build-from-source) — pick one
3. [Critical pitfalls](#critical-pitfalls-read-this-before-writing-code) (1–9)
4. [Architecture](#architecture)
5. [Step-by-step recipe](#step-by-step-adding-this-to-a-new-project) (build path)
6. [Schema design rules under byte-range loading](#schema-design-rules-under-byte-range-loading)
7. [Performance / cost model](#performance--cost-model)
8. [Cross-origin DB access (CORS)](#cross-origin-db-access-cors)
9. [Cache invalidation when the DB updates mid-session](#cache-invalidation-when-the-db-updates-mid-session)
10. [Upgrading memex / bumping the vendored SHA](#upgrading-memex--bumping-the-vendored-sha)
11. [Continuous-deploy smoke test (Playwright CI)](#continuous-deploy-smoke-test-playwright-ci)
12. [Common debugging mistakes](#common-debugging-mistakes)
13. [Alternatives we considered and rejected](#alternatives-we-considered-and-rejected)
14. [Files to read in this repo](#files-to-read-in-this-repo)
15. [TL;DR for an LLM in a hurry](#tldr-for-an-llm-in-a-hurry)

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

## Two integration paths (vendor vs build-from-source)

There are exactly two supported ways to add this to a new project. Pick
deliberately — they have different upgrade costs and dependency profiles.

### Path A — Vendor memex's pre-built `dist/wasm/` (recommended for most)

memex publishes a webpack-bundled copy of `sqlite-wasm-http` +
`sqlite3.wasm` + the `openMemexDb()` wrapper at
[`dist/wasm/`](dist/wasm/). Copy those files into your bundle root and
load `memex.js` as an ES module:

```html
<script type="module">
import { openMemexDb, query } from './memex.js';
const { db } = await openMemexDb(new URL('your-db.db', location.href).href);
const { columns, rows } = await query(db, 'SELECT … FROM …', [bindParams]);
</script>
```

- **Pros**: no npm, no webpack, no `node_modules`. The library patches
  are already baked in. Works in any static-site pipeline that can
  `curl` files into a bundle directory.
- **Cons**: pinned to memex's release cadence; you cannot customize the
  library or shrink the WASM binary.
- **Real-world example**: FastLED/boards uses this path — its
  `builders/site.py` downloads `memex.js` + `memex-*.js` + `sqlite3.wasm`
  from `raw.githubusercontent.com/zackees/memex/<SHA>/dist/wasm/` at
  build time. See [pitfall #6](#6-two-ways-to-consume-memex-rebuild-from-source-vs-vendor-distwasm)
  for the SHA-pinning warning.

### Path B — `npm install sqlite-wasm-http` + the patch script

If you need to control the library (custom backend type, smaller WASM,
extra patches), install the library directly and copy memex's
`patch-sqlite-wasm-http.mjs` into your `scripts/`:

- **Pros**: full control of the SQLite WASM build (memex's
  `prepare-wasm.mjs` shrinks `sqlite3.wasm` from ~1.5 MB to ~540 KB
  gzipped via `wasm-strip` + `wasm-opt -Oz`). Direct access to the
  promiser API for custom worker pools.
- **Cons**: requires Node.js, webpack/bundler of your choice, and
  ongoing maintenance of the patch script when bumping
  `sqlite-wasm-http`.
- **Recipe**: see [step-by-step](#step-by-step-adding-this-to-a-new-project) below.

**If unsure, start with Path A.** The vendored bundle is what memex's
own demo page (`pages/`) uses — by definition it works end-to-end on
GH Pages. Upgrade by re-downloading at a new SHA.

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

### 6. Two ways to consume memex: rebuild from source vs vendor `dist/wasm/`

(See [Two integration paths](#two-integration-paths-vendor-vs-build-from-source)
at the top for the path-selection discussion.) This pitfall is about the
**vendor** path specifically.

**Pin to a commit SHA, not `main`.** The chunk filenames
(`memex-141.js`, `memex-901.js`, …) are webpack-generated IDs that
change every time memex is rebuilt. If you reference `main` and memex
republishes, your site breaks because `memex.js` tries to dynamic-import
a chunk ID that no longer exists in the published `dist/wasm/`. Example
download URL with pinned SHA:

```
https://raw.githubusercontent.com/zackees/memex/03fe8df…/dist/wasm/memex.js
```

When you bump the SHA, re-download **the full set** of `memex-*.js`
files and verify the new chunk list at the same path. See
[Upgrading memex](#upgrading-memex--bumping-the-vendored-sha) below for
the precise procedure.

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

Note that stdlib `http.server` also does **not** support `Range:`
requests. Use [`RangeHTTPServer`](https://pypi.org/project/rangehttpserver/)
(pip install) and combine it with the mimetype additions above. The
local server still won't gzip the way GH Pages does, so a successful
local test is necessary but not sufficient — always verify on the
deployed site too.

### 9. The status message matters for debugging

These exact strings, each pointing at a different failure mode, are
worth remembering:

| Status string in the page | What it actually means |
|---|---|
| `database disk image is malformed` | gzip-defeated-Range; library got bytes from a gzip stream and tried to parse as SQLite |
| `Length of the file not known. It must either be supplied in the config or given by the HTTP server.` | sql.js-httpvfs only — size-probe response had no `Content-Range` and `Content-Length` was the gzipped size |
| `Failed to load module script: … MIME type "text/plain"` | Local server MIME issue (see #8 above), not a real deployment problem |
| `Cannot install OPFS: Missing SharedArrayBuffer and/or Atomics` | Benign warning — sqlite-wasm-http tries OPFS first, falls back to in-memory; OPFS needs COOP/COEP headers that GH Pages doesn't set, and you don't need it for HTTP VFS anyway |
| `Failed to fetch dynamically imported module: …/memex-XXX.js` | You bumped memex's SHA but didn't re-download the chunk files — see [Upgrading memex](#upgrading-memex--bumping-the-vendored-sha) |
| `Cross-Origin Resource Blocked` on the DB fetch | Either a redirect crossed origins (use canonical Pages URL with trailing slash on directories) or self-hosted server is missing CORS headers — see [CORS](#cross-origin-db-access-cors) |

Surface them prominently in your loader's error path. The amount of
time saved by `dbStatus.textContent = err.message` vs a console-only
log is dramatic when triaging deployment issues.

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

## Schema design rules under byte-range loading

Under HTTP range fetching, **every query pays per page touched**. The
schema decisions you make at build time determine whether a typical
query costs 3 round-trips or 300. Keep these rules in mind when
designing your DB:

| Do | Don't | Why |
|---|---|---|
| `PRAGMA page_size = 4096` before creating any tables | leave the page size at whatever the default is (varies by SQLite version) | The client's `maxPageSize` must match. Mismatch ⇒ silent corruption or extra fetches. 4 KB matches HTTP/2's typical frame granularity and FTS5's chunking. |
| Index every column used in `WHERE`, `ORDER BY`, or `JOIN` | rely on `LIKE '%foo%'` for search | Each unindexed predicate ⇒ full table scan ⇒ download every page. |
| Use FTS5 for text search (porter + trigram) | use plain TEXT columns + LIKE for fuzzy match | FTS5 reads ~5–20 index pages per query; LIKE reads every page. |
| Inline small auxiliary blobs as TEXT/BLOB columns (e.g. `boards.json_blob`) | store them as separate files | One SQL query > one extra HTTP request, especially when the client is already mid-conversation with the DB. |
| `VACUUM` at the end of the build | leave free-list fragmentation in the file | VACUUM rewrites pages contiguously so adjacent rows live on adjacent pages — the page cache stays warm across nearby queries. |
| Put hot tables first (rowid order matches insert order in newly-built DBs) | sprinkle the hot data across the file | Pages near the file start get fetched on initial probes anyway; cluster hot rows there to ride the same fetches. |
| `LIMIT` aggressively (50–100 rows is plenty for an autocomplete) | return thousands of rows | More rows ⇒ more pages ⇒ more round trips. The UI rarely needs more. |
| Run `EXPLAIN QUERY PLAN` against representative queries before shipping | trust your indexes silently | `EXPLAIN QUERY PLAN` shows `SCAN` vs `SEARCH`. Anything that says `SCAN` will fetch the whole table when range-loaded. |
| Use `JOIN` to walk from FTS5 results to the base table via rowid | duplicate base-table data into the FTS5 row | The `content='base', content_rowid='rowid'` external-content pattern keeps the FTS5 index small. |
| Test queries against the deployed DB, not just the local file | assume "fast locally ⇒ fast remote" | Local SQLite is in-memory; range-loaded SQLite costs ~30–80 ms per round trip. A 5-roundtrip query is ~250 ms in production. |

memex's `action/build_presentation.py` is the reference for shaping a
DB to these rules. Key choices visible in that file:

- Two FTS5 tables (porter + trigram) sharing one `chunks` base table via
  `content='chunks', content_rowid='rowid'`.
- Pre-computed `reference_points` table for the queries the UI fires
  immediately on page load — avoids running an aggregation over the
  whole `chunks` table on first paint.
- `PRAGMA optimize` followed by `VACUUM` as the last build step.

---

## Performance / cost model

Rough numbers for a typical deployment (DB ~10 MB, 4 KB page size,
HTTP/2 to GH Pages CDN, US-to-US latency ~30 ms RTT):

| Action | Pages touched | Range requests | Wall time |
|---|---|---|---|
| Open DB (size probe + header + sqlite_master) | 5–10 | 5–10 | 200–400 ms |
| Cold FTS5 query (`MATCH 'esp32*'`) | 5–15 | 3–8 (HTTP/2 multiplexed) | 200–500 ms |
| Warm FTS5 query (neighboring index pages cached) | 0–3 | 0–2 | 30–100 ms |
| `SELECT … WHERE rowid = ?` (b-tree single-row) | 2–4 | 2–4 | 100–250 ms |
| `LIKE '%foo%'` over a 10 MB table | every page (~2500) | as many as `cacheSize` lets it batch — easily 100+ | **seconds** — avoid |
| `sqlite3.wasm` initial download | n/a (one-time, browser-cached) | 1 (gzipped to ~540 KB) | 200–600 ms |

Tuning levers:

- **`cacheSize`** (KB of LRU page cache in the worker, default 4096
  = 4 MB ≈ 1000 pages). For a 10 MB DB, doubling this often turns
  every query after the first into a single round trip. Costs RAM in
  the worker; rarely a problem.
- **`maxPageSize`** must match the DB's `PRAGMA page_size` exactly. If
  the DB was built at 4 KB and you set `maxPageSize: 8192`, the
  library reads two pages per logical page — silently doubling cost.
- **HTTP/2 multiplexing** helps a lot. Modern browsers issue 6+
  concurrent range requests in parallel. You don't need to coalesce
  small ranges yourself; the library does it.
- **`sqlite3.wasm` is the largest one-time fetch** at ~1.5 MB
  uncompressed (~540 KB gzipped after `wasm-strip` + `wasm-opt -Oz`,
  see `pages-src/scripts/prepare-wasm.mjs`). Browsers cache it for
  weeks via the `Cache-Control: max-age=600` header that GH Pages
  sets — first-visit cost, not repeat-visit cost.

---

## Cross-origin DB access (CORS)

Same-origin DBs (your portal at `https://owner.github.io/repo/` reading
`https://owner.github.io/repo/index.db`) just work — no CORS concerns.

Cross-origin (a different page reading your DB) needs server cooperation:

- **GH Pages sets `Access-Control-Allow-Origin: *`** automatically on
  every static file. This allows the *body* of a cross-origin GET.
- **For `Range` to work cross-origin**, the response must expose
  `Accept-Ranges`, `Content-Range`, and `Content-Length` via
  `Access-Control-Expose-Headers`. GH Pages exposes `*`, which covers
  these. **Verify with curl + `-H "Origin: https://other-site.com"`**:

  ```bash
  curl -sI -H "Origin: https://x.com" -H "Accept-Encoding: identity" \
       -r 0-1023 https://owner.github.io/repo/index.db
  # Expect:
  #   HTTP/1.1 206 Partial Content
  #   Access-Control-Allow-Origin: *
  #   Access-Control-Expose-Headers: *      ← or explicit Content-Range, Accept-Ranges, Content-Length
  #   Content-Range: bytes 0-1023/<size>
  ```

- **If self-hosting on Cloudflare/Netlify/nginx** instead of GH Pages,
  you must configure the equivalent. A minimal `_headers` for
  Cloudflare Pages / Netlify:

  ```
  /*.db
    Access-Control-Allow-Origin: *
    Access-Control-Expose-Headers: Accept-Ranges, Content-Range, Content-Length
    Content-Encoding: identity
  ```

- **HTTPS is required** for cross-origin range fetches in modern
  browsers (mixed-content restrictions block HTTP DBs from HTTPS
  pages). GH Pages is HTTPS by default.

- **Redirects cross origins**. If your URL is
  `https://x.github.io/repo` (no trailing slash) and GH Pages 301s to
  `https://x.github.io/repo/` (trailing slash), the redirect itself
  crosses an origin boundary in some browsers and CORS preflight
  fails. Always link to the canonical URL with trailing slashes for
  directories.

---

## Cache invalidation when the DB updates mid-session

The DB on the server can change while users have a session open. Both
the worker's LRU page cache **and** the browser's HTTP cache hold
fragments of the old DB. Range requests for "new" pages will return
bytes from the *new* DB while the cache still holds bytes from the
*old* DB. SQLite then sees an internally inconsistent file and reports
… you guessed it … `database disk image is malformed` — but this time
the cause is staleness, not the gzip pitfall.

Three mitigation strategies, in increasing order of investment:

- **Tolerate gracefully**: catch the error in your query path and show
  a banner like *"the index has been updated — reload the page to see
  the latest."* Then `location.reload()` on click. Five lines of code,
  acceptable for low-traffic sites.

- **Service-worker snapshot**: register a service worker that caches
  all `Range:` responses keyed by `(url, range-header)` plus the
  initial `ETag`. If the `ETag` changes mid-session, the SW serves
  from the stale cache for the rest of the session and lets new tabs
  pick up the fresh build. Robust; ~50 lines of SW code.

- **Content-addressed DB URL**: write the DB to a path that includes
  the build SHA (e.g. `/index-<sha>.db`) and keep the last N versions.
  Old sessions hold their version until their build is pruned. Pair
  with `Cache-Control: immutable` on the per-build paths. Requires a
  manifest and a pruning step in the build workflow. Robust and
  invisible to users; bigger change to the deploy pipeline.

memex itself uses option 1 (the page reload prompt) — its DBs are
small and rebuild cadence is low, so the cost of an occasional reload
is negligible.

---

## Upgrading memex / bumping the vendored SHA

When you want to pull in a new memex release into your vendored
`dist/wasm/` files:

```bash
# 1. Pick the new SHA (use a commit hash, not a branch name).
NEW_SHA=<full-40-char-sha>

# 2. List the full set of dist/wasm/ files at that SHA — chunk IDs may
#    have changed since your last pin.
gh api repos/zackees/memex/contents/dist/wasm?ref=$NEW_SHA \
  --jq '.[].name' | sort

# 3. Update your downloader script with the new SHA AND the new file list.
#    The chunks are webpack-generated and named like memex-141.js,
#    memex-901.js — they MUST all be present alongside memex.js or
#    dynamic imports fail at runtime with:
#    "Failed to fetch dynamically imported module: .../memex-NNN.js"

# 4. Re-run your bundle build, then verify on the deployed site.
```

After deploying, run the [verification checklist](#step-7--verify-the-deploy-actually-works)
end-to-end. memex bumps the upstream `sqlite-wasm-http` version
periodically; the patch regexes in
`pages-src/scripts/patch-sqlite-wasm-http.mjs` are tied to the
upstream file structure and may need adjustment. If you're on Path A
(vendor), this is memex's problem to fix — your only obligation is to
re-pin and re-test.

---

## Continuous-deploy smoke test (Playwright CI)

Run this as a separate workflow on a schedule (e.g. nightly) or after
every deploy. It catches the failure modes that don't show up in
build-time tests because they only manifest against the real CDN.

```python
# tests/smoke_pages_deploy.py
import asyncio, sys
from playwright.async_api import async_playwright

URL = "https://owner.github.io/repo/"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        client = await ctx.new_cdp_session(page)
        await client.send("Network.enable")
        wire = []
        client.on("Network.requestWillBeSentExtraInfo",
                  lambda p: wire.append(("REQ", p["headers"])))
        client.on("Network.responseReceivedExtraInfo",
                  lambda p: wire.append(("RESP", p["headers"])))

        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        # Wait for the DB to finish loading — adapt to your loader's signal.
        await page.wait_for_function(
            "document.querySelector('#dbCounts')?.textContent?.length",
            timeout=30000,
        )

        # 1. Identity encoding was honored end-to-end.
        identity_reqs = [h for k, h in wire if k == "REQ"
                         and h.get("accept-encoding") == "identity"
                         and "range" in h]
        assert identity_reqs, "no Accept-Encoding: identity range requests issued"

        # 2. Responses came back as 206 with proper Content-Range.
        good_resps = [h for k, h in wire if k == "RESP"
                      and "content-range" in h
                      and h.get("content-encoding", "") != "gzip"]
        assert good_resps, "DB responses were gzipped — Range was defeated"

        # 3. No page errors.
        assert not errors, f"page errors: {errors}"

        print(f"OK: {len(identity_reqs)} identity range requests, "
              f"{len(good_resps)} uncompressed 206 responses")

asyncio.run(main())
```

Wire this into a workflow:

```yaml
# .github/workflows/smoke-test.yml
on:
  schedule: [{ cron: "0 6 * * *" }]
  workflow_dispatch: {}
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install playwright && python -m playwright install --with-deps chromium
      - run: python tests/smoke_pages_deploy.py
```

If this workflow goes red, the deployed site is broken for users even
if `python -m http.server` works locally. **This is the most
load-bearing test in the project.** Don't skip it.

---

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

**Path A — vendor memex's bundle (simpler, recommended)**

1. Pick a memex commit SHA. `gh api repos/zackees/memex/contents/dist/wasm?ref=<SHA> --jq '.[].name'` to list the chunk files at that revision.
2. Download `memex.js`, all `memex-*.js` chunks, and `sqlite3.wasm` from `https://raw.githubusercontent.com/zackees/memex/<SHA>/dist/wasm/` into your bundle root.
3. In your HTML: `<script type="module">import { openMemexDb, query } from './memex.js'; const { db } = await openMemexDb(new URL('index.db', location.href).href);</script>`.
4. Build `index.db` with FTS5 indexes + `PRAGMA page_size=4096` + `VACUUM`. Never `LIKE '%x%'` over big tables. See [schema rules](#schema-design-rules-under-byte-range-loading).
5. Deploy via `actions/deploy-pages@v4`. No `gh-pages` branch needed.
6. Verify with `curl -r 0-4095 -H "Accept-Encoding: identity" https://owner.github.io/repo/index.db` — must return `206 Partial Content` with `Content-Range: bytes 0-4095/<true-size>`. If you get `200 OK` instead, the deploy is broken — diagnose **before** opening the browser.
7. Add the [Playwright smoke test](#continuous-deploy-smoke-test-playwright-ci) as a nightly CI job.

**Path B — build from source**

1. `npm install sqlite-wasm-http`.
2. Copy memex's `patch-sqlite-wasm-http.mjs` and run it as `postinstall`.
3. Open the DB with `headers: { 'Accept-Encoding': 'identity' }` in `createHttpBackend`.
4. Steps 4–7 from Path A apply identically.

**The one critical line in both paths**: `Accept-Encoding: identity`.
Without it, GH Pages returns the full gzipped file with HTTP 200 every
time you ask for a range — and SQLite reports `database disk image is
malformed`. If any step is unclear, re-read [Critical pitfalls](#critical-pitfalls-read-this-before-writing-code).
The cost of getting them wrong is hours of "why does SQLite say the DB
is malformed when I can `sqlite3 index.db` locally just fine".

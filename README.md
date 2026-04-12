# memex

Build a searchable SQLite FTS5 index from any GitHub repo — code, issues, wiki, commits — and deploy it to GitHub Pages.

One action. Any public repo. Full-text fuzzy search over everything. Queries fetch **< 1% of the database** via HTTP range requests.

## What it does

Memex crawls a GitHub repository and builds a SQLite database with FTS5 full-text search indexes. The database is deployed to GitHub Pages as a static file, queryable via HTTP range requests from any client — Rust, Python, Node.js, or directly in the browser via WASM.

**Sources indexed:**
- Repository source files (all text files)
- Git commit history
- GitHub Issues + comments
- Pull Requests + review comments
- Wiki pages

**Search capabilities:**
- **FTS5 trigram** — substring and fuzzy matching (`"sqlit"` matches `sqlite`)
- **FTS5 porter** — stemmed word search (`"running"` matches `run`)
- **BM25 ranking** — relevance-scored results
- **JSON metadata** — structured access to file paths, authors, dates, labels

## Distributables

Pre-built bundles in `dist/` — no npm or bundler needed to use them.

### dist/wasm/ — WASM build (recommended)

Separate `.wasm` file, smallest JS payload. ~648 KB gzip transfer.

```
memex.js        214 KB   Library entry point
memex-141.js    235 KB   Background SQLite worker
sqlite3.wasm    1.45 MB  SQLite 3.44.2 (wasm-opt -Oz)
demo.html        16 KB   Self-contained demo (CSS inlined)
```

### dist/js/ — Pure JS build

WASM base64-inlined in JS. No `.wasm` file needed. Larger but simpler deployment.

```
memex.js          7 KB   Library entry point
memex-*.js     ~2.2 MB   Worker with inlined WASM
demo.html        16 KB   Self-contained demo (CSS inlined)
```

### Usage

```html
<script type="module">
import { openMemexDb, query } from './memex.js';

const { db, close } = await openMemexDb('https://example.github.io/repo/index.db');

// FTS5 porter search with BM25 ranking
const results = await query(db, `
  SELECT path, title, bm25(search_porter, 1,1,5,1,1) as rank
  FROM search_porter WHERE search_porter MATCH 'error handling'
  ORDER BY rank LIMIT 10
`);

console.log(results.columns, results.rows);
await close();
</script>
```

Architecture: **1 background Web Worker** (sync mode). No SharedArrayBuffer required. Works on GitHub Pages, any static host, localhost.

## GitHub Action

Add this workflow to any repo:

```yaml
# .github/workflows/memex.yml
name: Memex Index

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write
  issues: read
  pull-requests: read

jobs:
  index:
    uses: zackees/memex/.github/workflows/build-index.yml@main
    with:
      repo: ${{ github.repository }}
```

The index will be available at `https://<owner>.github.io/<repo>/index.db` with a live query demo.

### Action inputs

| Input | Default | Description |
|-------|---------|-------------|
| `repo` | current repo | GitHub repo to index (`owner/repo`) |
| `subdir` | `""` | Subdirectory to index (e.g. `src`) |
| `branch` | `main` | Branch to index |
| `skip-issues` | `false` | Skip GitHub Issues |
| `skip-prs` | `false` | Skip Pull Requests |
| `skip-wiki` | `false` | Skip Wiki pages |
| `skip-commits` | `false` | Skip git commits |

## Tables

| Table | Source | Tokenizer | Use case |
|---|---|---|---|
| `chunks` | All sources | — | Unified base table |
| `search_trigram` | All sources | `trigram` | Fuzzy/substring search |
| `search_porter` | All sources | `porter unicode61` | Stemmed word search |
| `meta` | Build info | — | Repo name, chunk counts |

## Query examples

```sql
-- Fuzzy search across everything
SELECT source_type, path, title, bm25(search_trigram) as rank
FROM search_trigram WHERE search_trigram MATCH '"FastLED"'
ORDER BY rank LIMIT 10;

-- Stemmed search with snippets
SELECT path, title, snippet(search_porter, 3, '**', '**', '...', 20) as snip
FROM search_porter WHERE search_porter MATCH 'memory leak'
ORDER BY bm25(search_porter) LIMIT 10;

-- Browse issues with metadata
SELECT path, title, json_extract(metadata, '$.state') as state,
       json_extract(metadata, '$.labels') as labels
FROM chunks WHERE source_type = 'issue';
```

## Client access

### Browser (WASM + HTTP range requests)

Use the pre-built bundles from `dist/wasm/` or `dist/js/`. See [Usage](#usage) above.

### Python

```python
import sqlite3, urllib.request
urllib.request.urlretrieve('https://owner.github.io/repo/index.db', 'index.db')
conn = sqlite3.connect('index.db')
rows = conn.execute("""
    SELECT path, title, bm25(search_porter) as rank
    FROM search_porter WHERE search_porter MATCH 'error handling'
    ORDER BY rank LIMIT 10
""").fetchall()
```

### Node.js

```js
const Database = require('better-sqlite3');
// Download index.db first, then:
const db = new Database('index.db', { readonly: true });
const results = db.prepare(`
  SELECT path, title FROM search_porter
  WHERE search_porter MATCH 'authentication' LIMIT 10
`).all();
```

### Rust

```rust
use rusqlite::Connection;
// With sqlite-vfs-http for HTTP range request access:
let conn = Connection::open("https://owner.github.io/repo/index.db")?;
```

## Rebuilding bundles

```bash
cd pages-src
npm install
npm run build          # all: wasm + js + demo
npm run build:wasm     # dist/wasm/ only
npm run build:js       # dist/js/ only
npm run build:demo     # pages/ (GitHub Pages deploy)
```

## The name

**Memex** (memory + index) was described by [Vannevar Bush](https://en.wikipedia.org/wiki/Vannevar_Bush) in his 1945 essay *[As We May Think](https://www.theatlantic.com/magazine/archive/1945/07/as-we-may-think/303881/)*.

> "Consider a future device... in which an individual stores all his books, records, and communications, and which is mechanized so that it may be consulted with exceeding speed and flexibility. It is an enlarged intimate supplement to his memory."
> — Vannevar Bush, 1945

This project is a small, literal implementation of Bush's idea: take everything in a repository — code, documentation, issues, discussions, history — compress it into a single indexed file, and make it instantly searchable with exceeding speed and flexibility.

## License

MIT

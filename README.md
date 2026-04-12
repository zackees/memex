# memex

Build a searchable SQLite FTS5 index from any GitHub repo — code, issues, wiki, commits — and deploy it to GitHub Pages.

One action. Any public repo. Full-text fuzzy search over everything.

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

Each data source gets its own table, so you can search code separately from issues, or query across everything at once.

## Usage

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

The index will be available at `https://<owner>.github.io/<repo>/index.db`.

## Tables

| Table | Source | Tokenizer | Use case |
|---|---|---|---|
| `files` | Repo text files | — | Raw file content + metadata |
| `issues` | GitHub Issues | — | Issue titles, bodies, comments |
| `pull_requests` | GitHub PRs | — | PR titles, bodies, review comments |
| `commits` | Git log | — | Commit messages + metadata |
| `wiki` | GitHub Wiki | — | Wiki page content |
| `search_trigram` | All sources | `trigram` | Fuzzy/substring search |
| `search_porter` | All sources | `porter unicode61` | Stemmed word search |

## Query examples

```sql
-- Fuzzy search across everything
SELECT source_type, path, title, bm25(search_trigram) as rank
FROM search_trigram WHERE search_trigram MATCH '"FastLED"'
ORDER BY rank LIMIT 10;

-- Stemmed search in issues only
SELECT source_type, path, title, snippet(search_porter, 3, '**', '**', '...', 20) as snip
FROM search_porter WHERE search_porter MATCH 'memory leak' AND source_type = 'issue'
ORDER BY bm25(search_porter) LIMIT 10;

-- Browse all issues with metadata
SELECT path, title, json_extract(metadata, '$.state') as state,
       json_extract(metadata, '$.labels') as labels
FROM issues ORDER BY json_extract(metadata, '$.number') DESC;

-- Search code files only
SELECT path, title, json_extract(metadata, '$.lines') as lines
FROM files WHERE body LIKE '%void setup()%';
```

## Client access

### Rust (recommended for agents)

```rust
use rusqlite::Connection;
// With sqlite-vfs-http for HTTP range request access:
let conn = Connection::open("https://owner.github.io/repo/index.db")?;
```

### Python

```python
import sqlite3, urllib.request
urllib.request.urlretrieve('https://owner.github.io/repo/index.db', 'index.db')
conn = sqlite3.connect('index.db')
```

### Node.js

```js
const Database = require('better-sqlite3');
// Download index.db first, then:
const db = new Database('index.db', { readonly: true });
```

### Browser (WASM)

The deployed GitHub Pages site includes a live query demo using the official SQLite WASM build with FTS5 support.

## The name

**Memex** (memory + index) was described by [Vannevar Bush](https://en.wikipedia.org/wiki/Vannevar_Bush) in his 1945 essay *[As We May Think](https://www.theatlantic.com/magazine/archive/1945/07/as-we-may-think/303881/)*, published in The Atlantic Monthly.

Bush envisioned a device that would store all of a person's books, records, and communications, compressed onto microfilm, and mechanized so it could be consulted "with exceeding speed and flexibility." The memex would allow its user to build trails of association between documents — linking ideas across sources in a personal, searchable web of knowledge.

> "Consider a future device... in which an individual stores all his books, records, and communications, and which is mechanized so that it may be consulted with exceeding speed and flexibility. It is an enlarged intimate supplement to his memory."
> — Vannevar Bush, 1945

The memex was never built as a physical device, but its core ideas directly influenced:

- **Hypertext** — Ted Nelson cited Bush when coining the term in 1965
- **The World Wide Web** — Tim Berners-Lee acknowledged Bush's essay as an inspiration
- **Personal knowledge management** — the entire field traces back to Bush's vision
- **Doug Engelbart's oNLine System (NLS)** — the first working hypertext system, built in the 1960s, explicitly inspired by the memex

This project is a small, literal implementation of Bush's idea: take everything in a repository — code, documentation, issues, discussions, history — compress it into a single indexed file, and make it instantly searchable with exceeding speed and flexibility.

The memex that Bush imagined in 1945 is now a SQLite database on GitHub Pages.

## License

MIT

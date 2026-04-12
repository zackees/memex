// --- Web Worker (inline, loaded via Blob URL) ---
const WORKER_CODE = `
let db = null;
let sqlite3 = null;
let baseUrl = '';

self.onmessage = async function(e) {
  if (e.data.type === 'init') {
    try {
      baseUrl = e.data.baseUrl;
      importScripts(e.data.sqlite3Url);
      sqlite3 = await sqlite3InitModule({
        print: ()=>{},
        printErr: ()=>{},
        locateFile: (file) => baseUrl + 'jswasm/' + file
      });

      const resp = await fetch(e.data.dbUrl);
      if (!resp.ok) throw new Error('Failed to fetch database: ' + resp.status);
      const bytes = new Uint8Array(await resp.arrayBuffer());

      db = new sqlite3.oo1.DB(':memory:', 'c');
      const pData = sqlite3.wasm.allocFromTypedArray(bytes);
      const rc = sqlite3.capi.sqlite3_deserialize(
        db.pointer, 'main', pData, bytes.byteLength, bytes.byteLength,
        sqlite3.capi.SQLITE_DESERIALIZE_FREEONCLOSE | sqlite3.capi.SQLITE_DESERIALIZE_RESIZEABLE
      );
      if (rc !== 0) throw new Error('sqlite3_deserialize failed: rc=' + rc);

      const version = sqlite3.capi.sqlite3_libversion();
      const tables = [];
      db.exec({ sql: "SELECT name FROM sqlite_master WHERE type IN ('table') ORDER BY name", callback: (row) => tables.push(row[0]) });
      self.postMessage({ type: 'ready', version: version, tables: tables, dbSize: bytes.byteLength });
    } catch (err) {
      self.postMessage({ type: 'error', error: err.message || String(err) });
    }
  }

  if (e.data.type === 'query') {
    if (!db) {
      self.postMessage({ type: 'result', id: e.data.id, error: 'Database not loaded' });
      return;
    }
    try {
      const t0 = performance.now();
      const columns = [];
      const rows = db.exec({
        sql: e.data.sql,
        returnValue: 'resultRows',
        rowMode: 'array',
        columnNames: columns
      });
      const elapsed = performance.now() - t0;
      self.postMessage({ type: 'result', id: e.data.id, columns, rows: rows || [], elapsed });
    } catch (err) {
      self.postMessage({ type: 'result', id: e.data.id, error: err.message || String(err) });
    }
  }
};
`;

// --- Main thread ---
let worker = null;
let queryId = 0;
const pendingQueries = {};
let currentQuery = '';

function initWorker() {
  const blob = new Blob([WORKER_CODE], { type: 'application/javascript' });
  worker = new Worker(URL.createObjectURL(blob));

  worker.onmessage = function(e) {
    if (e.data.type === 'ready') {
      const el = document.getElementById('status');
      el.className = 'status ready';
      el.textContent = `SQLite ${e.data.version} \u2022 ${(e.data.dbSize / 1024).toFixed(0)} KB \u2022 ${e.data.tables.length} tables`;
      enableButtons();
      // Auto-detect repo name from DB metadata
      runQuery("SELECT value FROM meta WHERE key = 'repo'").then(function(r) {
        if (r.rows && r.rows.length > 0 && r.rows[0][0]) {
          const repo = r.rows[0][0];
          const titleEl = document.getElementById('repo-title');
          if (titleEl) {
            titleEl.textContent = repo;
            document.title = repo + ' — Memex';
          }
        }
      });
    }
    if (e.data.type === 'error') {
      const el = document.getElementById('status');
      el.className = 'status error';
      el.textContent = 'Error: ' + e.data.error;
    }
    if (e.data.type === 'result') {
      const cb = pendingQueries[e.data.id];
      if (cb) {
        cb(e.data);
        delete pendingQueries[e.data.id];
      }
    }
  };

  const base = window.location.href.replace(/\/[^/]*$/, '/');
  worker.postMessage({
    type: 'init',
    baseUrl: base,
    sqlite3Url: base + 'jswasm/sqlite3.js',
    dbUrl: base + 'index.db'
  });
}

function runQuery(sql) {
  return new Promise((resolve) => {
    const id = ++queryId;
    pendingQueries[id] = resolve;
    worker.postMessage({ type: 'query', id, sql });
  });
}

function showQuery(sql) {
  currentQuery = sql;
  const display = document.getElementById('query-display');
  document.getElementById('query-text').textContent = sql;
  display.style.display = 'block';
}

function copyQuery() {
  navigator.clipboard.writeText(currentQuery).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy SQL'; }, 1500);
  }).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = currentQuery;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  });
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function renderResults(data) {
  const el = document.getElementById('results');
  if (data.error) {
    el.innerHTML = '<div class="status error">' + escapeHtml(data.error) + '</div>';
    return;
  }
  if (!data.columns || !data.columns.length) {
    el.innerHTML = '<div class="result-meta">Query executed. No rows returned.</div>';
    return;
  }

  let html = '<div class="result-meta">' + data.rows.length + ' row' + (data.rows.length !== 1 ? 's' : '') + ' in ' + data.elapsed.toFixed(1) + 'ms</div>';
  html += '<div class="table-wrap"><table><thead><tr>';
  for (const col of data.columns) html += '<th>' + escapeHtml(col) + '</th>';
  html += '</tr></thead><tbody>';
  for (const row of data.rows) {
    html += '<tr>';
    for (const val of row) {
      const s = val === null ? '<em style="color:var(--text-muted)">null</em>' : escapeHtml(String(val));
      html += '<td>' + s + '</td>';
    }
    html += '</tr>';
  }
  html += '</tbody></table></div>';
  el.innerHTML = html;
}

function enableButtons() {
  document.querySelectorAll('.query-btn').forEach(function(btn) {
    btn.addEventListener('click', async function() {
      document.querySelectorAll('.query-btn').forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
      const sql = btn.getAttribute('data-query');
      showQuery(sql);
      document.getElementById('results').innerHTML = '<div class="status loading">Running query...</div>';
      const result = await runQuery(sql);
      renderResults(result);
    });
  });
}

document.getElementById('run-btn').addEventListener('click', runCustomQuery);
document.getElementById('custom-sql').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') runCustomQuery();
});
document.getElementById('copy-btn').addEventListener('click', copyQuery);

async function runCustomQuery() {
  const input = document.getElementById('custom-sql');
  const sql = input.value.trim();
  if (!sql) return;
  showQuery(sql);
  document.querySelectorAll('.query-btn').forEach(function(b) { b.classList.remove('active'); });
  document.getElementById('results').innerHTML = '<div class="status loading">Running query...</div>';
  const result = await runQuery(sql);
  renderResults(result);
}

// Boot
initWorker();

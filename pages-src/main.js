/**
 * Memex demo page — interactive query UI using HTTP range requests.
 * Replaces the old app.js that downloaded the entire database.
 */
import { openMemexDb, query } from './memex.js';

let db = null;
let currentQuery = '';

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
    el.innerHTML = '<div class="result-meta">Query executed (' + data.elapsed.toFixed(1) + 'ms). No rows returned.</div>';
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

async function runSQL(sql) {
  const t0 = performance.now();
  try {
    const result = await query(db, sql);
    const elapsed = performance.now() - t0;
    return { columns: result.columns, rows: result.rows, elapsed };
  } catch (err) {
    return { error: err.message || String(err), elapsed: performance.now() - t0 };
  }
}

function enableButtons() {
  document.querySelectorAll('.query-btn').forEach(function(btn) {
    btn.addEventListener('click', async function() {
      document.querySelectorAll('.query-btn').forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
      const sql = btn.getAttribute('data-query');
      showQuery(sql);
      document.getElementById('results').innerHTML = '<div class="status loading">Running query...</div>';
      const result = await runSQL(sql);
      renderResults(result);
    });
  });
}

async function runCustomQuery() {
  const input = document.getElementById('custom-sql');
  const sql = input.value.trim();
  if (!sql) return;
  showQuery(sql);
  document.querySelectorAll('.query-btn').forEach(function(b) { b.classList.remove('active'); });
  document.getElementById('results').innerHTML = '<div class="status loading">Running query...</div>';
  const result = await runSQL(sql);
  renderResults(result);
}

async function init() {
  const statusEl = document.getElementById('status');
  try {
    const base = window.location.href.replace(/\/[^/]*$/, '/');
    const dbUrl = base + 'index.db';

    const memex = await openMemexDb(dbUrl);
    db = memex.db;

    // Get DB info
    const meta = await query(db, 'SELECT key, value FROM meta');
    const metaMap = {};
    for (const row of meta.rows) metaMap[row[0]] = row[1];

    const tables = await query(db, "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name");

    statusEl.className = 'status ready';
    statusEl.textContent = 'SQLite (HTTP range requests) \u2022 ' +
      (metaMap.total_items || '?') + ' items \u2022 ' +
      (metaMap.total_commits || '?') + ' commits \u2022 ' +
      tables.rows.length + ' tables';

    // Set repo title
    if (metaMap.repo) {
      const titleEl = document.getElementById('repo-title');
      if (titleEl) {
        titleEl.textContent = metaMap.repo;
        document.title = metaMap.repo + ' \u2014 Memex';
      }
    }

    enableButtons();
  } catch (err) {
    statusEl.className = 'status error';
    statusEl.textContent = 'Error: ' + (err.message || String(err));
    console.error(err);
  }
}

// Wire up UI
document.getElementById('run-btn').addEventListener('click', runCustomQuery);
document.getElementById('custom-sql').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') runCustomQuery();
});
document.getElementById('copy-btn').addEventListener('click', copyQuery);

// Boot
init();

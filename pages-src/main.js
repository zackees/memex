/**
 * Memex demo page - interactive query UI using HTTP range requests.
 */
import { fetchRows, getSchema, openMemexDb, query } from './memex.js';

let db = null;
let currentQuery = '';

const HIGHLIGHTS = [
  {
    title: 'Top Code Contributor',
    sql: `SELECT r.label, r.value_int, r.secondary_value, c.summary
          FROM reference_points r
          LEFT JOIN contributor_stats c ON c.author = r.entity_key
          WHERE r.category='contributor' AND r.metric='commits' AND r.scope='all_time' AND r.rank=1`,
    detailSql: `SELECT rank, label AS author, value_int AS commits, secondary_value AS additions
                FROM reference_points
                WHERE category='contributor' AND metric='commits' AND scope='all_time'
                ORDER BY rank`,
    describe(row) {
      if (!row) return 'No commit history available.';
      return `${row[0]} leads with ${formatNumber(row[1])} commits. ${row[3] || ''}`.trim();
    },
  },
  {
    title: 'Top Discussion Contributor',
    sql: `SELECT r.label, r.value_int, r.secondary_value, c.summary
          FROM reference_points r
          LEFT JOIN contributor_stats c ON c.author = r.entity_key
          WHERE r.category='contributor' AND r.metric='discussion' AND r.scope='all_time' AND r.rank=1`,
    detailSql: `SELECT rank, label AS author, value_int AS discussion_score, secondary_value AS total_comments
                FROM reference_points
                WHERE category='contributor' AND metric='discussion' AND scope='all_time'
                ORDER BY rank`,
    describe(row) {
      if (!row) return 'No discussion activity available.';
      return `${row[0]} leads discussion with score ${formatNumber(row[1])}. ${row[3] || ''}`.trim();
    },
  },
  {
    title: 'Most Active Overall',
    sql: `SELECT r.label, r.value_int, r.secondary_value, c.summary
          FROM reference_points r
          LEFT JOIN contributor_stats c ON c.author = r.entity_key
          WHERE r.category='contributor' AND r.metric='overall_activity' AND r.scope='all_time' AND r.rank=1`,
    detailSql: `SELECT rank, label AS author, value_int AS activity_score, secondary_value AS commit_count
                FROM reference_points
                WHERE category='contributor' AND metric='overall_activity' AND scope='all_time'
                ORDER BY rank`,
    describe(row) {
      if (!row) return 'No contributor activity available.';
      return `${row[0]} leads overall with activity score ${formatNumber(row[1])}. ${row[3] || ''}`.trim();
    },
  },
];

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatNumber(value) {
  const num = Number(value || 0);
  return Number.isFinite(num) ? num.toLocaleString() : String(value);
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

function formatSchemaAsResult(schema) {
  return {
    columns: ['type', 'name', 'columns'],
    rows: schema.objects.map((object) => [
      object.type,
      object.name,
      object.columns.map((column) => `${column.name}:${column.type || 'UNKNOWN'}`).join(', '),
    ]),
  };
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

async function runAction(action) {
  const t0 = performance.now();
  try {
    let result;
    if (action === 'meta') {
      result = await fetchRows(db, {
        from: 'meta',
        columns: ['key', 'value'],
        orderBy: [{ column: 'key', direction: 'ASC' }],
      });
    } else if (action === 'recent-items') {
      result = await fetchRows(db, {
        from: 'items',
        columns: ['entity_type', 'number', 'title', 'state', 'author'],
        orderBy: [{ column: 'updated_at', direction: 'DESC' }],
        limit: 20,
      });
    } else if (action === 'schema') {
      result = formatSchemaAsResult(await getSchema(db));
    } else {
      throw new Error(`Unsupported action: ${action}`);
    }

    return {
      columns: result.columns,
      rows: result.rows,
      elapsed: performance.now() - t0,
    };
  } catch (err) {
    return { error: err.message || String(err), elapsed: performance.now() - t0 };
  }
}

async function executeAndRender(sql, button) {
  document.querySelectorAll('.query-btn').forEach(function(b) { b.classList.remove('active'); });
  if (button) button.classList.add('active');
  showQuery(sql);
  document.getElementById('results').innerHTML = '<div class="status loading">Running query...</div>';
  const result = await runSQL(sql);
  renderResults(result);
}

function enableButtons() {
  document.querySelectorAll('.query-btn').forEach(function(btn) {
    btn.addEventListener('click', async function() {
      const action = btn.getAttribute('data-action');
      if (action) {
        document.querySelectorAll('.query-btn').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        const label = btn.getAttribute('data-label') || action;
        showQuery(label);
        document.getElementById('results').innerHTML = '<div class="status loading">Running query...</div>';
        renderResults(await runAction(action));
        return;
      }

      const sql = btn.getAttribute('data-query');
      await executeAndRender(sql, btn);
    });
  });
}

async function runCustomQuery() {
  const input = document.getElementById('custom-sql');
  const sql = input.value.trim();
  if (!sql) return;
  await executeAndRender(sql, null);
}

function renderHighlights(cards) {
  const grid = document.getElementById('insight-grid');
  if (!grid) return;

  grid.innerHTML = cards.map(function(card, idx) {
    return '<article class="insight-card">' +
      '<div class="insight-kicker">History question</div>' +
      '<h3>' + escapeHtml(card.title) + '</h3>' +
      '<p>' + escapeHtml(card.summary) + '</p>' +
      '<button class="insight-action" data-highlight-index="' + idx + '">View leaderboard</button>' +
      '</article>';
  }).join('');

  grid.querySelectorAll('.insight-action').forEach(function(button) {
    button.addEventListener('click', async function() {
      const idx = Number(button.getAttribute('data-highlight-index'));
      const highlight = HIGHLIGHTS[idx];
      await executeAndRender(highlight.detailSql, null);
    });
  });
}

async function loadHighlights() {
  const cards = [];
  for (const highlight of HIGHLIGHTS) {
    const result = await runSQL(highlight.sql);
    cards.push({
      title: highlight.title,
      summary: result.error ? result.error : highlight.describe(result.rows[0]),
    });
  }
  renderHighlights(cards);
}

async function init() {
  const statusEl = document.getElementById('status');
  try {
    const base = window.location.href.replace(/\/[^/]*$/, '/');
    const dbUrl = base + 'index.db';

    const memex = await openMemexDb(dbUrl);
    db = memex.db;

    const meta = await fetchRows(db, {
      from: 'meta',
      columns: ['key', 'value'],
      orderBy: [{ column: 'key', direction: 'ASC' }],
    });
    const metaMap = {};
    for (const row of meta.rows) metaMap[row[0]] = row[1];

    const schema = await getSchema(db);

    statusEl.className = 'status ready';
    statusEl.textContent = 'SQLite (HTTP range requests) \u2022 ' +
      (metaMap.total_items || '?') + ' items \u2022 ' +
      (metaMap.total_commits || '?') + ' commits \u2022 ' +
      (metaMap.total_contributors || '?') + ' contributors \u2022 ' +
      (metaMap.total_reference_points || '?') + ' reference points \u2022 ' +
      schema.objects.length + ' tables';

    if (metaMap.repo) {
      const titleEl = document.getElementById('repo-title');
      if (titleEl) {
        titleEl.textContent = metaMap.repo;
        document.title = metaMap.repo + ' - Memex';
      }
    }

    enableButtons();
    await loadHighlights();
  } catch (err) {
    statusEl.className = 'status error';
    statusEl.textContent = 'Error: ' + (err.message || String(err));
    console.error(err);
  }
}

document.getElementById('run-btn').addEventListener('click', runCustomQuery);
document.getElementById('custom-sql').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') runCustomQuery();
});
document.getElementById('copy-btn').addEventListener('click', copyQuery);

init();

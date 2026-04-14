/**
 * Memex — SQLite HTTP range-request client library.
 *
 * Uses a single background Web Worker (sync mode) for all queries.
 * Only fetches the database pages needed per query via HTTP range requests.
 *
 * @module memex
 */
import { createSQLiteThread, createHttpBackend } from 'sqlite-wasm-http';

export { createSQLiteThread, createHttpBackend };

/**
 * Open a remote SQLite database using HTTP range requests.
 *
 * @param {string} url - Full URL to the .db file
 * @param {object} [options]
 * @param {number} [options.maxPageSize=1024] - SQLite page size (must match DB)
 * @param {number} [options.timeout=30000] - HTTP request timeout in ms
 * @param {number} [options.cacheSize=4096] - LRU cache size in KB
 * @returns {Promise<{db: Function, backend: object, close: Function}>}
 */
export async function openMemexDb(url, options = {}) {
  // Force sync mode: single background worker, no SharedArrayBuffer needed
  const backend = createHttpBackend({
    maxPageSize: options.maxPageSize || 4096,
    timeout: options.timeout || 30000,
    cacheSize: options.cacheSize || 4096,
    backendType: 'sync',
    // Prevent CDN gzip — range requests on gzipped content return corrupt data
    headers: { 'Accept-Encoding': 'identity' },
  });

  const db = await createSQLiteThread({ http: backend });

  await db('open', {
    filename: 'file:' + encodeURI(url),
    vfs: 'http',
  });

  return {
    db,
    backend,
    close: async () => {
      await db('close', {});
      db.close();
      await backend.close();
    },
  };
}

/**
 * Run a query and collect all rows into an array.
 *
 * @param {Function} db - The promiser returned by openMemexDb
 * @param {string} sql - SQL query string
 * @param {object} [bind] - Bind parameters, e.g. { $query: 'hello' }
 * @returns {Promise<{columns: string[], rows: any[][]}>}
 */
export async function query(db, sql, bind) {
  const columns = [];
  const rows = [];
  await db('exec', {
    sql,
    bind,
    callback: (msg) => {
      if (msg.row) {
        rows.push(msg.row);
        if (columns.length === 0 && msg.columnNames) {
          columns.push(...msg.columnNames);
        }
      } else if (msg.columnNames && columns.length === 0) {
        columns.push(...msg.columnNames);
      }
    },
  });
  return { columns, rows };
}

function quoteIdentifier(identifier) {
  if (typeof identifier !== 'string' || identifier.trim() === '') {
    throw new TypeError('identifier must be a non-empty string');
  }
  return identifier
    .split('.')
    .map((part) => `"${part.replace(/"/g, '""')}"`)
    .join('.');
}

function normalizeInteger(value, label) {
  if (!Number.isInteger(value) || value < 0) {
    throw new TypeError(`${label} must be a non-negative integer`);
  }
  return value;
}

function normalizeOrderBy(orderBy) {
  if (!orderBy) {
    return '';
  }
  const terms = Array.isArray(orderBy) ? orderBy : [orderBy];
  if (terms.length === 0) {
    return '';
  }
  return terms
    .map((term) => {
      if (typeof term === 'string') {
        return `${quoteIdentifier(term)} ASC`;
      }
      if (!term || typeof term.column !== 'string') {
        throw new TypeError('orderBy entries must be strings or { column, direction } objects');
      }
      const direction = String(term.direction || 'ASC').toUpperCase();
      if (direction !== 'ASC' && direction !== 'DESC') {
        throw new TypeError('orderBy direction must be ASC or DESC');
      }
      return `${quoteIdentifier(term.column)} ${direction}`;
    })
    .join(', ');
}

/**
 * Inspect tables and views exposed by the current SQLite database.
 *
 * @param {Function} db - The promiser returned by openMemexDb
 * @returns {Promise<{objects: Array<{type: string, name: string, sql: string, columns: Array<object>}>}>}
 */
export async function getSchema(db) {
  const objectsResult = await query(
    db,
    "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
  );

  const objects = [];
  for (const row of objectsResult.rows) {
    const [type, name, sql] = row;
    const columnResult = await query(db, `PRAGMA table_xinfo(${quoteIdentifier(name)})`);
    const columns = columnResult.rows.map((columnRow) => ({
      cid: columnRow[0],
      name: columnRow[1],
      type: columnRow[2],
      notnull: Boolean(columnRow[3]),
      defaultValue: columnRow[4],
      pk: columnRow[5],
      hidden: columnRow[6],
    }));
    objects.push({ type, name, sql, columns });
  }

  return { objects };
}

/**
 * Fetch rows from a table or view through the existing WASM-backed query path.
 *
 * @param {Function} db - The promiser returned by openMemexDb
 * @param {object} options
 * @param {string} [options.from] - Table or view name
 * @param {string} [options.table] - Alias for `from`
 * @param {string[]} [options.columns=['*']] - Column names to select
 * @param {string} [options.where] - Optional WHERE clause body
 * @param {object} [options.bind] - Bind parameters
 * @param {string|Array<string|{column: string, direction?: string}>} [options.orderBy] - ORDER BY terms
 * @param {number} [options.limit] - Optional LIMIT
 * @param {number} [options.offset] - Optional OFFSET
 * @returns {Promise<{columns: string[], rows: any[][]}>}
 */
export async function fetchRows(db, options = {}) {
  const source = options.from || options.table;
  if (!source) {
    throw new TypeError('fetchRows requires a from/table option');
  }

  const selectList = Array.isArray(options.columns) && options.columns.length
    ? options.columns.map((column) => quoteIdentifier(column)).join(', ')
    : '*';

  let sql = `SELECT ${selectList} FROM ${quoteIdentifier(source)}`;
  if (options.where) {
    sql += ` WHERE ${options.where}`;
  }

  const orderBy = normalizeOrderBy(options.orderBy);
  if (orderBy) {
    sql += ` ORDER BY ${orderBy}`;
  }

  if (options.limit !== undefined) {
    sql += ` LIMIT ${normalizeInteger(options.limit, 'limit')}`;
  }
  if (options.offset !== undefined) {
    const offset = normalizeInteger(options.offset, 'offset');
    if (options.limit === undefined) {
      sql += ' LIMIT -1';
    }
    sql += ` OFFSET ${offset}`;
  }

  return query(db, sql, options.bind);
}

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

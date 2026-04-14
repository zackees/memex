import test from 'node:test';
import assert from 'node:assert/strict';

import { fetchRows, getSchema } from '../memex.js';

function makeDb(respond) {
  return async (op, args) => {
    assert.equal(op, 'exec');
    const result = respond(args.sql, args.bind);
    for (const message of result.messages) {
      args.callback?.(message);
    }
  };
}

test('getSchema returns sqlite objects with column metadata', async () => {
  const seen = [];
  const db = makeDb((sql) => {
    seen.push(sql);
    if (sql.includes('FROM sqlite_master')) {
      return {
        messages: [
          {
            columnNames: ['type', 'name', 'sql'],
            row: ['table', 'items', 'CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT)'],
          },
          {
            row: ['view', 'recent_items', 'CREATE VIEW recent_items AS SELECT * FROM items'],
          },
        ],
      };
    }
    if (sql === 'PRAGMA table_xinfo("items")') {
      return {
        messages: [
          {
            columnNames: ['cid', 'name', 'type', 'notnull', 'dflt_value', 'pk', 'hidden'],
            row: [0, 'id', 'INTEGER', 1, null, 1, 0],
          },
          {
            row: [1, 'title', 'TEXT', 0, null, 0, 0],
          },
        ],
      };
    }
    if (sql === 'PRAGMA table_xinfo("recent_items")') {
      return {
        messages: [
          {
            columnNames: ['cid', 'name', 'type', 'notnull', 'dflt_value', 'pk', 'hidden'],
            row: [0, 'id', 'INTEGER', 0, null, 0, 0],
          },
          {
            row: [1, 'title', 'TEXT', 0, null, 0, 0],
          },
        ],
      };
    }
    throw new Error(`Unexpected SQL: ${sql}`);
  });

  const schema = await getSchema(db);

  assert.deepEqual(seen, [
    "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name",
    'PRAGMA table_xinfo("items")',
    'PRAGMA table_xinfo("recent_items")',
  ]);
  assert.equal(schema.objects.length, 2);
  assert.deepEqual(schema.objects[0], {
    type: 'table',
    name: 'items',
    sql: 'CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT)',
    columns: [
      { cid: 0, name: 'id', type: 'INTEGER', notnull: true, defaultValue: null, pk: 1, hidden: 0 },
      { cid: 1, name: 'title', type: 'TEXT', notnull: false, defaultValue: null, pk: 0, hidden: 0 },
    ],
  });
});

test('fetchRows builds a quoted SELECT and returns rows in query shape', async () => {
  let seenSql = '';
  let seenBind;
  const db = makeDb((sql, bind) => {
    seenSql = sql;
    seenBind = bind;
    return {
      messages: [
        { columnNames: ['key', 'value'] },
        { row: ['repo', 'zackees/memex'] },
        { row: ['total_items', 42] },
      ],
    };
  });

  const result = await fetchRows(db, {
    from: 'meta',
    columns: ['key', 'value'],
    orderBy: [{ column: 'key', direction: 'ASC' }],
    limit: 2,
    offset: 1,
    where: '"key" != $skip',
    bind: { $skip: 'build_sha' },
  });

  assert.equal(
    seenSql,
    'SELECT "key", "value" FROM "meta" WHERE "key" != $skip ORDER BY "key" ASC LIMIT 2 OFFSET 1'
  );
  assert.deepEqual(seenBind, { $skip: 'build_sha' });
  assert.deepEqual(result, {
    columns: ['key', 'value'],
    rows: [
      ['repo', 'zackees/memex'],
      ['total_items', 42],
    ],
  });
});

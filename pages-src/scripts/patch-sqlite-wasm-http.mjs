import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');

const files = [
  path.join(root, 'node_modules', 'sqlite-wasm-http', 'dist', 'vfs-sync-http.js'),
  path.join(root, 'node_modules', 'sqlite-wasm-http', 'dist', 'vfs-http-worker.js'),
];

for (const filePath of files) {
  let source = fs.readFileSync(filePath, 'utf8');
  const original = source;

  source = source.replace(
    "xhr.open('HEAD', url, false);",
    "xhr.open('GET', url, false);"
  );
  source = source.replace(
    "xhr.send();",
    "xhr.setRequestHeader('Range', 'bytes=0-0');\n                xhr.send();"
  );
  source = source.replace(
    "fh.size = BigInt((_a = xhr.getResponseHeader('Content-Length')) !== null && _a !== void 0 ? _a : 0);",
    "const contentRange = xhr.getResponseHeader('Content-Range');\n                    const sizeMatch = contentRange === null || contentRange === void 0 ? void 0 : contentRange.match(/\\/(\\d+)$/);\n                    fh.size = BigInt((sizeMatch === null || sizeMatch === void 0 ? void 0 : sizeMatch[1]) ?? ((_a = xhr.getResponseHeader('Content-Length')) !== null && _a !== void 0 ? _a : 0));"
  );
  source = source.replace(
    "entry = fetch(msg.url, { method: 'HEAD', headers: Object.assign({}, options === null || options === void 0 ? void 0 : options.headers) })",
    "entry = fetch(msg.url, { method: 'GET', headers: Object.assign(Object.assign({}, options === null || options === void 0 ? void 0 : options.headers), { Range: 'bytes=0-0' }) })"
  );
  source = source.replace(
    "size: BigInt((_a = head.headers.get('Content-Length')) !== null && _a !== void 0 ? _a : 0),",
    "size: BigInt(((_a = head.headers.get('Content-Range')) === null || _a === void 0 ? void 0 : _a.match(/\\/(\\d+)$/))?.[1] ?? (head.headers.get('Content-Length') ?? 0)),"
  );

  if (source === original) {
    console.log(`No patch changes needed for ${path.basename(filePath)}`);
    continue;
  }

  fs.writeFileSync(filePath, source);
  console.log(`Patched ${path.basename(filePath)}`);
}

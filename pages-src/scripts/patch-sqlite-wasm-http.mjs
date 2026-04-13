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

  if (path.basename(filePath) === 'vfs-sync-http.js') {
    source = source.replace(
      /let valid = false;\s*try \{\s*const xhr = new XMLHttpRequest\(\);[\s\S]*?xhr\.send\(\);\s*\}/,
      `let valid = false;
            try {
                const xhr = new XMLHttpRequest();
                xhr.open('GET', url, false);
                for (const h of Object.keys((_a = options === null || options === void 0 ? void 0 : options.headers) !== null && _a !== void 0 ? _a : VFSHTTP.defaultOptions.headers))
                    xhr.setRequestHeader(h, ((_b = options === null || options === void 0 ? void 0 : options.headers) !== null && _b !== void 0 ? _b : VFSHTTP.defaultOptions.headers)[h]);
                xhr.setRequestHeader('Range', 'bytes=0-0');
                xhr.onload = () => {
                    var _a, _b;
                    const fh = Object.create(null);
                    fh.fid = fid;
                    fh.url = url;
                    fh.sq3File = new sqlite3_file(fid);
                    fh.sq3File.$pMethods = httpIoMethods.pointer;
                    const contentRange = xhr.getResponseHeader('Content-Range');
                    const sizeMatch = contentRange === null || contentRange === void 0 ? void 0 : contentRange.match(/\\/(\\d+)$/);
                    fh.size = BigInt((sizeMatch === null || sizeMatch === void 0 ? void 0 : sizeMatch[1]) ?? ((_a = xhr.getResponseHeader('Content-Length')) !== null && _a !== void 0 ? _a : 0));
                    fh.pageCache = new LRUCache({
                        maxSize: ((_b = options === null || options === void 0 ? void 0 : options.cacheSize) !== null && _b !== void 0 ? _b : VFSHTTP.defaultOptions.cacheSize) * 1024,
                        sizeCalculation: (value) => { var _a; return (_a = value.byteLength) !== null && _a !== void 0 ? _a : 4; }
                    });
                    if (xhr.getResponseHeader('Accept-Ranges') !== 'bytes') {
                        console.warn(\`Server for \${url} does not advertise 'Accept-Ranges'. \` +
                            'If the server supports it, in order to remove this message, add "Accept-Ranges: bytes". ' +
                            'Additionally, if using CORS, add "Access-Control-Expose-Headers: *".');
                    }
                    openFiles[fid] = fh;
                    valid = true;
                };
                xhr.send();
            }`
    );
  }

  if (path.basename(filePath) === 'vfs-http-worker.js') {
    source = source.replace(
      /entry = fetch\(msg\.url, \{[\s\S]*?files\.set\(msg\.url, yield entry\);/,
      `entry = fetch(msg.url, { method: 'GET', headers: Object.assign(Object.assign({}, options === null || options === void 0 ? void 0 : options.headers), { Range: 'bytes=0-0' }) })
                .then((head) => {
                var _a;
                if (head.headers.get('Accept-Ranges') !== 'bytes') {
                    console.warn(\`Server for \${msg.url} does not advertise 'Accept-Ranges'. \` +
                        'If the server supports it, in order to remove this message, add "Accept-Ranges: bytes". ' +
                        'Additionally, if using CORS, add "Access-Control-Expose-Headers: *".');
                }
                return {
                    url: msg.url,
                    id: nextId++,
                    size: BigInt(((_a = head.headers.get('Content-Range')) === null || _a === void 0 ? void 0 : _a.match(/\\/(\\d+)$/))?.[1] ?? (head.headers.get('Content-Length') ?? 0)),
                    pageSize: null
                };
            });
            files.set(msg.url, entry);
            files.set(msg.url, yield entry);`
    );
  }

  if (source === original) {
    console.log(`No patch changes needed for ${path.basename(filePath)}`);
    continue;
  }

  fs.writeFileSync(filePath, source);
  console.log(`Patched ${path.basename(filePath)}`);
}

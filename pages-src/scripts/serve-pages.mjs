import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..', '..');
const pagesDir = path.join(root, 'pages');
const port = Number(process.env.PORT || '8080');
const host = process.env.HOST || '127.0.0.1';

const mimeTypes = new Map([
  ['.html', 'text/html; charset=utf-8'],
  ['.js', 'application/javascript; charset=utf-8'],
  ['.css', 'text/css; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.wasm', 'application/wasm'],
  ['.db', 'application/octet-stream'],
]);

const server = http.createServer((req, res) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || `${host}:${port}`}`);
  let pathname = decodeURIComponent(url.pathname);
  if (pathname === '/') pathname = '/index.html';

  const filePath = path.resolve(pagesDir, `.${pathname}`);
  if (!filePath.startsWith(pagesDir + path.sep) && filePath !== pagesDir) {
    res.writeHead(403).end('Forbidden');
    return;
  }

  fs.stat(filePath, (statErr, stats) => {
    if (statErr || !stats.isFile()) {
      res.writeHead(404).end('Not found');
      return;
    }

    const contentType = mimeTypes.get(path.extname(filePath).toLowerCase()) || 'application/octet-stream';
    const range = req.headers.range;

    if (!range) {
      res.writeHead(200, {
        'Content-Length': stats.size,
        'Content-Type': contentType,
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-store',
      });
      fs.createReadStream(filePath).pipe(res);
      return;
    }

    const match = /^bytes=(\d*)-(\d*)$/.exec(range);
    if (!match) {
      res.writeHead(416, { 'Content-Range': `bytes */${stats.size}` }).end();
      return;
    }

    const start = match[1] === '' ? 0 : Number(match[1]);
    const end = match[2] === '' ? stats.size - 1 : Number(match[2]);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start || end >= stats.size) {
      res.writeHead(416, { 'Content-Range': `bytes */${stats.size}` }).end();
      return;
    }

    res.writeHead(206, {
      'Content-Length': end - start + 1,
      'Content-Type': contentType,
      'Content-Range': `bytes ${start}-${end}/${stats.size}`,
      'Accept-Ranges': 'bytes',
      'Cache-Control': 'no-store',
    });
    fs.createReadStream(filePath, { start, end }).pipe(res);
  });
});

server.listen(port, host, () => {
  console.log(`Serving ${pagesDir} at http://${host}:${port}/`);
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
  });
}

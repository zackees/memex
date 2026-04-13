import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const target = process.argv[2];

if (!target || !['wasm', 'js'].includes(target)) {
  throw new Error('Usage: node ./scripts/postbuild.mjs <wasm|js>');
}

const outDir = path.join(root, '..', 'dist', target);
fs.mkdirSync(outDir, { recursive: true });
fs.copyFileSync(path.join(root, 'demo.html'), path.join(outDir, 'demo.html'));

for (const entry of fs.readdirSync(outDir)) {
  if (entry.endsWith('.LICENSE.txt')) {
    fs.rmSync(path.join(outDir, entry), { force: true });
  }
}

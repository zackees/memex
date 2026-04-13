import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const filePath = path.join(root, '..', 'pages', 'index.html');
const buildId = process.argv[2];

if (!buildId || !/^\d+$/.test(buildId)) {
  throw new Error('Usage: node ./scripts/set-build-id.mjs <non-negative integer>');
}

let html = fs.readFileSync(filePath, 'utf8');
const nextMeta = `<meta name="memex-build" content="${buildId}">`;
const nextBadge = `<span id="build-id" style="font-size:0.75em;opacity:0.5">build=${buildId}</span>`;

if (!html.includes('name="memex-build"') || !html.includes('id="build-id"')) {
  throw new Error('pages/index.html is missing the memex build markers.');
}

html = html.replace(/<meta name="memex-build" content="\d+">/, nextMeta);
html = html.replace(/<span id="build-id" style="font-size:0\.75em;opacity:0\.5">build=\d+<\/span>/, nextBadge);
fs.writeFileSync(filePath, html);

console.log(`Updated pages/index.html to build=${buildId}`);

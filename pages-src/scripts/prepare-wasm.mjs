import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const dependencyWasm = path.join(root, 'node_modules', 'sqlite-wasm-http', 'deps', 'dist', 'sqlite3.wasm');
const outputDir = path.join(root, 'generated');
const outputWasm = path.join(outputDir, 'sqlite3.wasm');
const metadataPath = path.join(outputDir, 'sqlite3.metadata.json');
const stockWasm = path.join(outputDir, 'sqlite3.stock.wasm');

const phaseMap = {
  stock: null,
  strip: null,
  o1: '-O1',
  o2: '-O2',
  os: '-Os',
  oz: '-Oz',
};

const phase = (process.env.MEMEX_WASM_PHASE || 'oz').toLowerCase();
if (!(phase in phaseMap)) {
  throw new Error(`Unsupported MEMEX_WASM_PHASE="${phase}". Expected one of: ${Object.keys(phaseMap).join(', ')}`);
}

fs.mkdirSync(outputDir, { recursive: true });
if (!fs.existsSync(stockWasm)) {
  fs.copyFileSync(dependencyWasm, stockWasm);
}

fs.copyFileSync(stockWasm, outputWasm);

if (phase !== 'stock') {
  runTool('wasm-strip', [outputWasm]);
}

if (phaseMap[phase]) {
  const optimized = path.join(outputDir, `sqlite3.${phase}.wasm`);
  runTool('wasm-opt', [phaseMap[phase], outputWasm, '-o', optimized]);
  fs.renameSync(optimized, outputWasm);
}

runTool('wasm-validate', [outputWasm]);

const sourceWasm = stockWasm;
const sourceExports = getExportNames(sourceWasm);
const outputExports = getExportNames(outputWasm);
if (sourceExports.join('\n') !== outputExports.join('\n')) {
  throw new Error(`WASM export mismatch for phase "${phase}". Refusing to use optimized binary.`);
}

const sourceSize = fs.statSync(sourceWasm).size;
const outputSize = fs.statSync(outputWasm).size;
const metadata = {
  phase,
  sourceSize,
  outputSize,
  reductionBytes: sourceSize - outputSize,
  reductionPercent: Number((((sourceSize - outputSize) / sourceSize) * 100).toFixed(2)),
  exportCount: outputExports.length,
  md5: md5(outputWasm),
};

fs.writeFileSync(metadataPath, `${JSON.stringify(metadata, null, 2)}\n`);
fs.copyFileSync(outputWasm, dependencyWasm);
console.log(
  `Prepared sqlite3.wasm phase=${metadata.phase} size=${metadata.outputSize} ` +
  `delta=${metadata.reductionBytes}B (${metadata.reductionPercent}%) exports=${metadata.exportCount}`
);

function md5(filePath) {
  return createHash('md5').update(fs.readFileSync(filePath)).digest('hex');
}

function runTool(toolName, args) {
  const binDir = path.join(root, 'node_modules', '.bin');
  const tool = process.platform === 'win32'
    ? path.join(binDir, `${toolName}.cmd`)
    : path.join(binDir, toolName);
  const result = spawnSync(tool, args, {
    cwd: root,
    encoding: 'utf8',
    stdio: 'pipe',
    shell: process.platform === 'win32',
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    const details = [result.stdout, result.stderr].filter(Boolean).join('\n').trim();
    throw new Error(`${toolName} failed (${result.status ?? 'signal'}): ${details}`);
  }
}

function getExportNames(filePath) {
  const binDir = path.join(root, 'node_modules', '.bin');
  const tool = process.platform === 'win32'
    ? path.join(binDir, 'wasm-objdump.cmd')
    : path.join(binDir, 'wasm-objdump');
  const result = spawnSync(tool, ['-x', filePath], {
    cwd: root,
    encoding: 'utf8',
    stdio: 'pipe',
    shell: process.platform === 'win32',
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    const details = [result.stdout, result.stderr].filter(Boolean).join('\n').trim();
    throw new Error(`wasm-objdump failed (${result.status ?? 'signal'}): ${details}`);
  }

  const exports = [];
  let inExportBlock = false;
  for (const line of result.stdout.split(/\r?\n/)) {
    if (line.startsWith('Export[')) {
      inExportBlock = true;
      continue;
    }
    if (!inExportBlock) continue;
    if (/^(Elem|Code|Data|Custom)\[/.test(line)) break;
    const match = line.match(/->\s+"([^"]+)"/);
    if (match) exports.push(match[1]);
  }
  return exports;
}

import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default {
  mode: 'production',
  entry: './memex.js',
  output: {
    filename: 'memex.js',
    chunkFilename: 'memex-[name].js',
    assetModuleFilename: 'sqlite3.wasm',
    path: path.resolve(__dirname, '../dist/wasm'),
    library: { type: 'module' },
    clean: true,
  },
  experiments: {
    outputModule: true,
  },
  optimization: {
    splitChunks: false,
    runtimeChunk: false,
  },
  module: {
    parser: {
      javascript: {
        // Inline all dynamic imports — no async chunk splitting
        dynamicImportMode: 'eager',
      },
    },
  },
  resolve: {
    alias: {
      // Stub out unused modules to reduce chunk count
      [path.resolve(__dirname, 'node_modules/sqlite-wasm-http/deps/dist/sqlite3-opfs-async-proxy.js')]:
        false,
    },
    fallback: {},
  },
};

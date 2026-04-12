import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const splitChunks = {
  cacheGroups: {
    'sqlite3-core': {
      test: /sqlite3-bundler-friendly\.mjs/,
      name: 'sqlite3-core',
      chunks: 'all',
      priority: 20,
    },
    'sqlite3-opfs': {
      test: /sqlite3-opfs-async-proxy/,
      name: 'sqlite3-opfs',
      chunks: 'all',
      priority: 15,
    },
    'sqlite3-utils': {
      test: /[\\/](lru-cache|endianness)[\\/]/,
      name: 'sqlite3-utils',
      chunks: 'all',
      priority: 10,
    },
  },
};

export default {
  mode: 'production',
  entry: './memex.js',
  output: {
    filename: 'memex.js',
    chunkFilename: 'memex-[name].js',
    path: path.resolve(__dirname, '../dist/node'),
    library: { type: 'module' },
    clean: true,
  },
  experiments: {
    outputModule: true,
  },
  optimization: {
    splitChunks,
  },
  module: {
    rules: [
      {
        // Inline WASM as base64 — no external .wasm file needed
        test: /\.wasm$/,
        type: 'asset/inline',
      },
    ],
  },
  resolve: {
    fallback: {},
  },
};

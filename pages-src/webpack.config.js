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
  entry: './main.js',
  output: {
    filename: 'bundle.js',
    chunkFilename: 'memex-[name].js',
    assetModuleFilename: 'sqlite3.wasm',
    path: path.resolve(__dirname, '../pages'),
  },
  optimization: {
    splitChunks,
  },
  resolve: {
    fallback: {},
  },
};

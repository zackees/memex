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
    path: path.resolve(__dirname, '../dist/browser'),
    library: { type: 'module' },
    clean: true,
  },
  experiments: {
    outputModule: true,
  },
  optimization: {
    splitChunks: {
      // Only split chunks shared between workers (avoid duplication)
      // Don't create vendor splits for the main bundle
      minSize: 50000,
      cacheGroups: {
        default: false,
        defaultVendors: false,
      },
    },
  },
  resolve: {
    fallback: {},
  },
};

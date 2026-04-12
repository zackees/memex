import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default {
  mode: 'production',
  entry: './main.js',
  output: {
    filename: 'bundle.js',
    chunkFilename: 'memex-[name].js',
    assetModuleFilename: 'sqlite3.wasm',
    path: path.resolve(__dirname, '../pages'),
    clean: {
      keep: /^(index\.html|style\.css|index\.db)$/,
    },
  },
  optimization: {
    splitChunks: {
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

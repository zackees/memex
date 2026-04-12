import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default {
  mode: 'production',
  entry: './memex.js',
  output: {
    filename: 'memex.js',
    chunkFilename: 'memex-[name].js',
    path: path.resolve(__dirname, '../dist/js'),
    library: { type: 'module' },
    clean: true,
  },
  experiments: {
    outputModule: true,
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
  module: {
    rules: [
      {
        test: /\.wasm$/,
        type: 'asset/inline',
      },
    ],
  },
  resolve: {
    fallback: {},
  },
};

import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default {
  mode: 'production',
  entry: './memex.js',
  output: {
    filename: 'memex.js',
    path: path.resolve(__dirname, '../dist/node'),
    library: { type: 'module' },
    clean: true,
  },
  experiments: {
    outputModule: true,
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

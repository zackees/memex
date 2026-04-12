import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default {
  mode: 'production',
  entry: './memex.js',
  output: {
    filename: 'memex.js',
    path: path.resolve(__dirname, '../dist/browser'),
    library: { type: 'module' },
    clean: true,
  },
  experiments: {
    outputModule: true,
  },
  resolve: {
    fallback: {},
  },
};

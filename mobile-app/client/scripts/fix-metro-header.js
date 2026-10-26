const fs = require('fs');
const path = require('path');
const file = path.join(
  __dirname,
  '..',
  'node_modules/@react-native-community/cli-server-api/build/statusPageMiddleware.js',
);
if (!fs.existsSync(file)) process.exit(0);
let src = fs.readFileSync(file, 'utf8');
if (src.includes('replace(/[^\\x20-\\x7E]/g')) process.exit(0);
src = src.replace(
  "res.setHeader('X-React-Native-Project-Root', process.cwd());",
  "res.setHeader('X-React-Native-Project-Root', String(process.cwd()).replace(/[^\\x20-\\x7E]/g, '_'));",
);
fs.writeFileSync(file, src);
console.log('fixed metro status header for non-ASCII project paths');

// Source-level + behavioral regression runner for the token-savings page's
// failed-query path: no compilation needed for the source pin; the behavioral
// companion transpiles the page with the dashboard's babel toolchain and
// drives the real query path with manually-resolved responses (same approach
// as the conversation failed-query regression suite).
const { execFileSync } = require('node:child_process');

execFileSync('node', [
  '--test',
  'tests/token-savings-failed-query-regression.test.cjs',
], {
  stdio: 'inherit',
});

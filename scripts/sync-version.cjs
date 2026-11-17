// VERSION is the release source for the backend, desktop package and asset URLs.
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const release = fs.readFileSync(path.join(root, 'VERSION'), 'utf8').trim();
const match = /^(\d+\.\d+\.\d+)\+([0-9A-Za-z.-]+)$/.exec(release);
if (!match) throw new Error('VERSION must contain version+build');
const [, version, build] = match;
const assetVersion = `${version}-${build.replaceAll('.', '-')}`;
const check = process.argv.includes('--check');
let changed = 0;
function update(file, content) {
  const previous = fs.readFileSync(file, 'utf8');
  if (previous === content) return;
  changed++;
  if (check) console.error(`Version mismatch: ${path.relative(root, file)}`);
  else fs.writeFileSync(file, content);
}
for (const relative of ['desktop/package.json', 'desktop/package-lock.json']) {
  const file = path.join(root, relative);
  const data = JSON.parse(fs.readFileSync(file, 'utf8'));
  data.version = version;
  if (data.packages?.['']) data.packages[''].version = version;
  update(file, JSON.stringify(data, null, 2) + '\n');
}
for (const directory of ['frontend', 'frontend/pages']) {
  for (const name of fs.readdirSync(path.join(root, directory))) {
    if (!name.endsWith('.html')) continue;
    const file = path.join(root, directory, name);
    const text = fs.readFileSync(file, 'utf8');
    update(file, text.replace(/(\/static\/[^"'\s?]+\?v=)[^"'\s&]+/g, `$1${assetVersion}`));
  }
}
if (check && changed) process.exitCode = 1;
else console.log(`${release}: ${check ? 'version sources consistent' : `synchronized ${changed} files`}`);

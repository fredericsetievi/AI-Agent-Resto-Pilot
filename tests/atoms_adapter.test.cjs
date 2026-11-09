const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const shim = fs.readFileSync(path.join(__dirname, '../deploy/atoms/frontend/restopilot-adapter/api-base.js'), 'utf8');

test('Atoms registers every dashboard script and stylesheet for preview and publication', () => {
  const frontendDir = path.join(__dirname, '../frontend');
  const html = fs.readFileSync(path.join(frontendDir, 'index.html'), 'utf8');
  const plugin = fs.readFileSync(path.join(__dirname, '../deploy/atoms/frontend/plugins/restopilot-dashboard.ts'), 'utf8');
  const registry = plugin.slice(plugin.indexOf('const ASSETS:'), plugin.indexOf('function dashboardHtml'));
  const required = [...html.matchAll(/(?:src|href)="(\/assets\/[^"?#]+\.(?:js|css))"/g)].map(match => match[1]);
  assert.ok(required.length, 'dashboard must declare its local assets');
  for (const asset of required) {
    const filename = asset.slice('/assets/'.length);
    assert.ok(fs.existsSync(path.join(frontendDir, filename)), `${asset} source is missing`);
    const escaped = asset.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    assert.match(registry, new RegExp(`["']${escaped}["']\\s*:`), `${asset} must be served and copied by Atoms`);
  }
});

test('Atoms serves the landing at / and the dashboard at /app.html', () => {
  const landing = fs.readFileSync(path.join(__dirname, '../landing/index.html'), 'utf8');
  const api = fs.readFileSync(path.join(__dirname, '../service/api.py'), 'utf8');
  const plugin = fs.readFileSync(path.join(__dirname, '../deploy/atoms/frontend/plugins/restopilot-dashboard.ts'), 'utf8');

  // The landing is script-free HTML, links only to the dashboard's real file
  // path, and references its stylesheet through the served landing-assets prefix.
  assert.equal([...landing.matchAll(/<script/g)].length, 0, 'landing must stay script-free');
  assert.ok(landing.includes('href="/app.html"'), 'landing must link /app.html');
  assert.ok(!landing.includes('href="/app"'), 'landing must not link /app (it falls back to the landing on Atoms)');
  assert.ok(landing.includes('href="/landing-assets/styles.css"'), 'landing must reference its served stylesheet');

  // The plugin resolves sources from the Atoms integration copy, serves the
  // landing at /, emits the dashboard as a real app.html file, and keeps the
  // missing-source and copy-verification protections.
  const registry = plugin.slice(plugin.indexOf('const LANDING_ASSETS:'), plugin.indexOf('function readSource'));
  assert.match(registry, /["']\/landing-assets\/styles\.css["']\s*:/, 'landing stylesheet must be served and copied by Atoms');
  assert.match(plugin, /path\.resolve\(adapterDir, '\.\.\/deploy\/restopilot-integration'\)/, 'sources must come from the Atoms integration copy');
  assert.match(plugin, /url === '\/' \|\| url === '\/index\.html'/, 'dev middleware must serve the landing at /');
  assert.match(plugin, /url === '\/app' \|\| url === '\/app\.html'/, 'dev middleware must serve the dashboard at /app.html');
  assert.match(plugin, /fileName: 'app\.html', source: Buffer\.from\(dashboardHtml\(\)\)/, 'build must emit app.html');
  assert.match(plugin, /fs\.writeFileSync\(indexTarget, landingHtml\(\)\)/, 'build must write the landing as index.html');
  assert.match(plugin, /RestoPilot source is missing/, 'build must fail when a mapped source is missing');
  assert.match(plugin, /does not match its source/, 'build must verify copied files against their sources');

  // The Python app mirrors /app at /app.html so the same landing links work locally.
  assert.match(api, /@app\.get\("\/app\.html", include_in_schema=False\)/, 'api must serve /app.html');
});

function setup() {
  const calls = [];
  const window = { fetch: (...args) => { calls.push(args); return Promise.resolve('ok'); } };
  vm.runInNewContext(shim, { window, location: { origin: 'https://atoms.test' }, URL, Request });
  return { fetch: window.fetch, calls };
}

test('Atoms shim routes API and health requests while preserving mutation options', async () => {
  const { fetch, calls } = setup();
  const init = { method: 'POST', body: '{"message":"test"}', headers: { 'Idempotency-Key': 'one' } };
  await fetch('/v1/intake/text?mode=check', init);
  await fetch(new URL('https://atoms.test/health/ready'));
  assert.equal(calls[0][0], '/api/v1/restopilot/v1/intake/text?mode=check');
  assert.equal(calls[0][1], init);
  assert.equal(calls[1][0], '/api/v1/restopilot/health/ready');
});

test('Atoms shim preserves Request bodies, headers and credentials', async () => {
  const { fetch, calls } = setup();
  const request = new Request('https://atoms.test/v1/check', {
    method: 'POST', body: 'payload', headers: { 'X-Check': 'kept' }, credentials: 'include',
  });
  // Node requires absolute URLs; browsers resolve the adapter's relative URL against location.
  class BrowserRequest extends Request {
    constructor(input, init) {
      super(typeof input === 'string' ? new URL(input, 'https://atoms.test') : input, init);
    }
  }
  const window = { fetch: (...args) => { calls.push(args); return Promise.resolve(); } };
  vm.runInNewContext(shim, { window, location: { origin: 'https://atoms.test' }, URL, Request: BrowserRequest });
  const browserRequest = new BrowserRequest(request);
  await window.fetch(browserRequest);
  assert.equal(calls[0][0].url, 'https://atoms.test/api/v1/restopilot/v1/check');
  assert.equal(calls[0][0].headers.get('x-check'), 'kept');
  assert.equal(calls[0][0].credentials, 'include');
  assert.equal(await calls[0][0].text(), 'payload');
});

test('Atoms shim leaves static assets, platform routes and external APIs untouched', async () => {
  const { fetch, calls } = setup();
  for (const url of ['/assets/app.js', '/api/v1/users/me', 'https://external.test/v1/data']) {
    await fetch(url);
    assert.equal(calls.at(-1)[0], url);
  }
});

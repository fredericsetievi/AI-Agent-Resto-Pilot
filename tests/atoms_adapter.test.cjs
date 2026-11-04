const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const shim = fs.readFileSync(path.join(__dirname, '../deploy/atoms/frontend/restopilot-adapter/api-base.js'), 'utf8');

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

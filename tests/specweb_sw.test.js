const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ORIGIN = 'http://specweb.test';
const WORKER = fs.readFileSync(
  path.join(__dirname, '..', 'reviewlib', 'specweb', 'static', 'sw.js'),
  'utf8'
);

function requestKey(request, options = {}) {
  const href = typeof request === 'string' ? new URL(request, ORIGIN).href : request.url;
  const url = new URL(href);
  if (options.ignoreSearch) url.search = '';
  return url.href;
}

function workerHarness(fetchImpl) {
  const listeners = {};
  const stores = new Map();
  const messages = [];
  const pending = [];
  let unregisters = 0;

  function cacheFor(name) {
    if (!stores.has(name)) stores.set(name, new Map());
    const entries = stores.get(name);
    return {
      addAll: async (urls) => {
        for (const url of urls) entries.set(requestKey(url), new Response(url));
      },
      match: async (request, options) => {
        const exact = entries.get(requestKey(request));
        if (exact || !options || !options.ignoreSearch) return exact;
        return entries.get(requestKey(request, options));
      },
      put: async (request, response) => entries.set(requestKey(request), response),
    };
  }

  const caches = {
    open: async (name) => cacheFor(name),
    keys: async () => Array.from(stores.keys()),
    delete: async (name) => stores.delete(name),
    match: async (request, options) => {
      for (const name of stores.keys()) {
        const found = await cacheFor(name).match(request, options);
        if (found) return found;
      }
      return undefined;
    },
  };
  const self = {
    location: { origin: ORIGIN },
    clients: {
      claim: async () => {},
      get: async () => ({ postMessage: (message) => messages.push(message) }),
    },
    registration: {
      unregister: async () => { unregisters += 1; },
    },
    skipWaiting: async () => {},
    addEventListener: (name, handler) => { listeners[name] = handler; },
  };
  vm.runInNewContext(WORKER, {
    URL,
    Promise,
    Error,
    Response,
    caches,
    fetch: fetchImpl,
    self,
  }, { filename: 'sw.js' });

  async function lifecycle(name) {
    listeners[name]({ waitUntil: (promise) => { pending.push(promise); } });
    await drain();
  }

  async function dispatch(url, mode = 'cors', method = 'GET') {
    let responsePromise;
    listeners.fetch({
      request: { method, mode, url },
      clientId: 'client-1',
      respondWith: (promise) => { responsePromise = promise; },
      waitUntil: (promise) => { pending.push(promise); },
    });
    if (!responsePromise) return undefined;
    return responsePromise;
  }

  async function drain() {
    await Promise.all(pending.splice(0));
  }

  return { cacheFor, dispatch, drain, lifecycle, messages, unregisters: () => unregisters };
}

test('offline reload returns cached spec page and API payload', async () => {
  const harness = workerHarness(async () => { throw new Error('daemon unreachable'); });
  const content = harness.cacheFor('review-specweb-content-v2');
  await content.put(`${ORIGIN}/spec/sample`, new Response('cached shell'));
  await content.put(`${ORIGIN}/spec/sample/api/spec`, new Response('{"html":"cached spec"}'));

  const page = await harness.dispatch(`${ORIGIN}/spec/sample`, 'navigate');
  const api = await harness.dispatch(`${ORIGIN}/spec/sample/api/spec`);
  assert.equal(await page.text(), 'cached shell');
  assert.equal(await api.text(), '{"html":"cached spec"}');
  assert.ok(harness.messages.some((message) => message.type === 'specweb-offline-cache'));
});

test('uncached offline navigation returns the app fallback', async () => {
  const harness = workerHarness(async () => { throw new Error('offline'); });
  await harness.cacheFor('review-specweb-shell-v2').put(
    `${ORIGIN}/offline.html`,
    new Response('This spec is not available offline')
  );

  const response = await harness.dispatch(`${ORIGIN}/spec/never-opened`, 'navigate');
  assert.match(await response.text(), /not available offline/);
});

test('install cache makes manifest start_url available offline', async () => {
  const harness = workerHarness(async () => { throw new Error('offline'); });
  await harness.lifecycle('install');
  await harness.lifecycle('activate');

  const response = await harness.dispatch(`${ORIGIN}/`, 'navigate');
  assert.equal(await response.text(), '/');
  const pngIcon = await harness.dispatch(`${ORIGIN}/app-icon.png`);
  assert.equal(await pngIcon.text(), '/app-icon.png');
  const icon = await harness.dispatch(`${ORIGIN}/app-icon.svg`);
  assert.equal(await icon.text(), '/app-icon.svg');
});

test('successful spec responses enter the content cache', async () => {
  const harness = workerHarness(async () => new Response('fresh spec', {
    status: 200,
    headers: { 'X-Review-Specweb': '1' },
  }));
  const url = `${ORIGIN}/spec/fresh`;
  assert.equal(await (await harness.dispatch(url, 'navigate')).text(), 'fresh spec');
  await harness.drain();
  assert.equal(harness.unregisters(), 0);
  const cached = await harness.cacheFor('review-specweb-content-v2').match(url);
  assert.equal(await cached.text(), 'fresh spec');
});

test('foreign navigations unregister the root-scoped worker without caching', async () => {
  const harness = workerHarness(async () => new Response('foreign app', {
    status: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  }));
  const url = `${ORIGIN}/`;
  assert.equal(await (await harness.dispatch(url, 'navigate')).text(), 'foreign app');
  await harness.drain();
  assert.equal(harness.unregisters(), 1);
  const cached = await harness.cacheFor('review-specweb-shell-v2').match(url);
  assert.equal(cached, undefined);
});

test('nested daemon API paths are cached without ignoring query parameters', async () => {
  const harness = workerHarness(async () => new Response('nested spec', {
    status: 200,
    headers: { 'X-Review-Specweb': '1' },
  }));
  const url = `${ORIGIN}/spec/team/project/api/spec`;
  assert.equal(await (await harness.dispatch(url)).text(), 'nested spec');
  await harness.drain();
  const onlineCached = await harness.cacheFor('review-specweb-content-v2').match(url);
  assert.equal(await onlineCached.text(), 'nested spec');

  const offline = workerHarness(async () => { throw new Error('offline'); });
  const content = offline.cacheFor('review-specweb-content-v2');
  await content.put(`${ORIGIN}/spec/team/project/api/spec`, new Response('cached nested spec'));
  const cached = await offline.dispatch(url);
  assert.equal(await cached.text(), 'cached nested spec');
  await content.put(`${ORIGIN}/spec/team/project/api/spec?refresh=1`, new Response('fresh query'));
  const cachedWithQuery = await offline.dispatch(`${ORIGIN}/spec/team/project/api/spec?refresh=1`);
  assert.equal(await cachedWithQuery.text(), 'fresh query');
});

test('no-store API responses are not written to content cache', async () => {
  const harness = workerHarness(async () => new Response('private spec', {
    status: 200,
    headers: { 'Cache-Control': 'no-store', 'X-Review-Specweb': '1' },
  }));
  const url = `${ORIGIN}/spec/team/project/api/spec`;
  assert.equal(await (await harness.dispatch(url)).text(), 'private spec');
  await harness.drain();
  const cached = await harness.cacheFor('review-specweb-content-v2').match(url);
  assert.equal(cached, undefined);
});

test('static shell assets refresh from the network and update shell cache', async () => {
  const harness = workerHarness(async () => new Response('fresh css', {
    status: 200,
    headers: { 'X-Review-Specweb': '1' },
  }));
  const url = `${ORIGIN}/static/app.css`;
  await harness.cacheFor('review-specweb-shell-v2').put(url, new Response('old css'));

  const response = await harness.dispatch(url);
  assert.equal(await response.text(), 'fresh css');
  await harness.drain();
  const cached = await harness.cacheFor('review-specweb-shell-v2').match(url);
  assert.equal(await cached.text(), 'fresh css');
});

test('foreign shell subresources do not poison the shell cache', async () => {
  const harness = workerHarness(async () => new Response('foreign css', { status: 200 }));
  const url = `${ORIGIN}/static/app.css`;
  assert.equal(await (await harness.dispatch(url)).text(), 'foreign css');
  await harness.drain();
  const cached = await harness.cacheFor('review-specweb-shell-v2').match(url);
  assert.equal(cached, undefined);
});

test('foreign content responses do not poison the content cache', async () => {
  const harness = workerHarness(async () => new Response('foreign spec', { status: 200 }));
  const specUrl = `${ORIGIN}/spec/other/api/spec`;
  const assetUrl = `${ORIGIN}/spec/other/asset/fig.svg`;
  assert.equal(await (await harness.dispatch(specUrl)).text(), 'foreign spec');
  assert.equal(await (await harness.dispatch(assetUrl)).text(), 'foreign spec');
  await harness.drain();
  const content = harness.cacheFor('review-specweb-content-v2');
  assert.equal(await content.match(specUrl), undefined);
  assert.equal(await content.match(assetUrl), undefined);
});

test('non-GET and cross-origin requests are not handled by the worker', async () => {
  const harness = workerHarness(async () => new Response('unused'));
  assert.equal(await harness.dispatch(`${ORIGIN}/api/comments`, 'cors', 'POST'), undefined);
  assert.equal(await harness.dispatch('https://example.com/static/app.css'), undefined);
});

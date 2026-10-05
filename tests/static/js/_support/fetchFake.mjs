// Shared fetch fake for node --test frontend tests.
//
// installFetchFake() replaces globalThis.fetch with a router: each route
// matches a method and a path (an exact string or a RegExp tested against the
// pathname) and its handler answers. A handler gets { method, url, body } with
// `url` as a URL object, and returns a Response or any value, which goes back
// as a 200 JSON body. Every call is recorded in `calls`; a request no route
// matches gets a 404 and is also listed in `unmatched`, so a test can assert
// that the code under test asked only for what it expected.
//
//   const fake = installFetchFake();
//   fake.route('GET', '/api/history/s1', ({ url }) => ({ history: [] }));
//   ...
//   assert.deepEqual(fake.unmatched, []);
//   fake.restore();

export function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function baseUrl() {
  return globalThis.location?.href || 'http://localhost/';
}

function pathMatches(path, pathname) {
  return path instanceof RegExp ? path.test(pathname) : path === pathname;
}

export function installFetchFake() {
  const original = globalThis.fetch;
  const routes = [];
  const calls = [];
  const unmatched = [];

  const fake = async (input, init = {}) => {
    const rawUrl = typeof input === 'string' || input instanceof URL ? String(input) : input.url;
    const url = new URL(rawUrl, baseUrl());
    const method = String(init.method || input?.method || 'GET').toUpperCase();
    const call = { method, url, body: init.body };
    calls.push(call);
    const route = routes.find((r) => r.method === method && pathMatches(r.path, url.pathname));
    if (!route) {
      unmatched.push(`${method} ${url.pathname}${url.search}`);
      return jsonResponse({ detail: 'no fake route' }, { status: 404 });
    }
    const result = await route.handler(call);
    return result instanceof Response ? result : jsonResponse(result);
  };

  globalThis.fetch = fake;

  return {
    calls,
    unmatched,
    route(method, path, handler) {
      routes.push({ method: method.toUpperCase(), path, handler });
      return this;
    },
    restore() {
      globalThis.fetch = original;
    },
  };
}

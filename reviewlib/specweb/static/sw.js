/* Root-scoped app shell for spec-web. Bump SHELL_CACHE when shell assets or cache
 * strategy change; bump CONTENT_CACHE when cached spec/API semantics change. */
const SHELL_CACHE = "review-specweb-shell-v2";
const CONTENT_CACHE = "review-specweb-content-v2";
const SHELL_PREFIX = "review-specweb-shell-";
const CONTENT_PREFIX = "review-specweb-content-";
const OFFLINE_URL = "/offline.html";
const SHELL_ASSETS = [
  "/",
  "/static/app.css",
  "/static/app.js",
  "/manifest.webmanifest",
  "/offline.html",
  "/app-icon.png",
  "/app-icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) => Promise.all(names.map((name) => {
      const oldShell = name.startsWith(SHELL_PREFIX) && name !== SHELL_CACHE;
      const oldContent = name.startsWith(CONTENT_PREFIX) && name !== CONTENT_CACHE;
      return oldShell || oldContent ? caches.delete(name) : Promise.resolve(false);
    }))).then(() => self.clients.claim())
  );
});

function isContentRequest(url, request) {
  if (request.mode === "navigate") return true;
  if (/\/api\/spec$/.test(url.pathname)) return true;
  return /\/asset\/[^/]+$/.test(url.pathname);
}

function notify(clientId, type) {
  const post = (client) => {
    if (client) client.postMessage({ type });
  };
  if (!clientId) return Promise.resolve();
  return self.clients.get(clientId).then((client) => {
    if (client) post(client);
  });
}

function cacheWrite(cacheName, request, response) {
  const copy = response.clone();
  return caches.open(cacheName).then((cache) => cache.put(request, copy));
}

function foreignNavigation(request, response) {
  return request.mode === "navigate" && response.status === 200 &&
    (response.headers.get("Content-Type") || "").includes("text/html") &&
    response.headers.get("X-Review-Specweb") !== "1";
}

function responseCacheable(cacheName, response) {
  if (!response.ok) return false;
  if ((response.headers.get("Cache-Control") || "").includes("no-store")) return false;
  if (response.headers.get("X-Review-Specweb") !== "1") return false;
  return true;
}

function networkFirst(event, cacheName) {
  const request = event.request;
  return fetch(request).then((response) => {
    if (foreignNavigation(request, response)) {
      event.waitUntil(self.registration.unregister());
      return response;
    }
    if (responseCacheable(cacheName, response)) {
      event.waitUntil(
        cacheWrite(cacheName, request, response).then(() => notify(event.clientId, "specweb-online"))
      );
    }
    return response;
  }).catch(() => {
    const matchOptions = request.mode === "navigate" ? { ignoreSearch: true } : undefined;
    return caches.open(cacheName).then((cache) => cache.match(request, matchOptions)).then((cached) => {
      if (cached) {
        event.waitUntil(notify(event.clientId, "specweb-offline-cache"));
        return cached;
      }
      if (request.mode === "navigate") return caches.match(OFFLINE_URL, { ignoreSearch: true });
      throw new Error("Spec content is not available offline");
    });
  });
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === "/" || url.pathname.startsWith("/static/") ||
      url.pathname === "/manifest.webmanifest" || url.pathname === OFFLINE_URL ||
      url.pathname === "/app-icon.png" || url.pathname === "/app-icon.svg") {
    event.respondWith(networkFirst(event, SHELL_CACHE));
    return;
  }
  if (isContentRequest(url, request)) {
    event.respondWith(networkFirst(event, CONTENT_CACHE));
  }
});

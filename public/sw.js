const CACHE_NAME = "dealnear-v1";
const PUBLIC_ASSETS = ["/", "/offline", "/icons/icon.svg", "/icons/maskable.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(PUBLIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const isDashboard = url.pathname.startsWith("/admin") || url.pathname.startsWith("/business-dashboard") || url.pathname.startsWith("/profile");

  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(() => {
        if (isDashboard) return Response.error();
        return caches.match("/offline");
      }),
    );
    return;
  }

  if (event.request.method !== "GET" || isDashboard) return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        if (!response || response.status !== 200 || response.type !== "basic") return response;
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return response;
      });
    }),
  );
});


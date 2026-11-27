const CACHE_VERSION = "1.0.0";
const SHELL_CACHE = `ssv-shell-v${CACHE_VERSION}`;
const MEDIA_CACHE = `ssv-media-v${CACHE_VERSION}`;
const MAX_MEDIA_ENTRIES = 600;

const SHELL_URLS = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS)).catch(() => null)
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== SHELL_CACHE && k !== MEDIA_CACHE)
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

async function trimMediaCache() {
  try {
    const cache = await caches.open(MEDIA_CACHE);
    const keys = await cache.keys();
    if (keys.length > MAX_MEDIA_ENTRIES) {
      const overflow = keys.length - MAX_MEDIA_ENTRIES;
      await Promise.all(keys.slice(0, overflow).map((req) => cache.delete(req)));
    }
  } catch (_err) {
    // ignore trim failure
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 절대 캐시하지 않는 동적·인증·버전 API
  if (
    url.pathname === "/manifest.json" ||
    url.pathname === "/sw.js" ||
    url.pathname === "/api/version" ||
    url.pathname === "/api/healthz" ||
    url.pathname.startsWith("/api/auth/") ||
    url.pathname.startsWith("/api/admin/") ||
    url.pathname === "/api/pages" ||
    url.pathname === "/api/docs" ||
    url.pathname === "/api/toc" ||
    url.pathname === "/api/meta" ||
    url.pathname.startsWith("/api/favorites")
  ) {
    return;
  }

  // 불변 버전 파라미터(?v=...)가 포함된 장표 WebP는 Cache-First
  const isImmutableMedia =
    /^\/api\/pages\/[^/]+\/(thumb|preview)\.webp$/.test(url.pathname) &&
    url.searchParams.has("v");

  if (isImmutableMedia) {
    event.respondWith(
      caches.open(MEDIA_CACHE).then(async (cache) => {
        const hit = await cache.match(req);
        if (hit) return hit;
        const fresh = await fetch(req);
        if (fresh && fresh.ok) {
          cache.put(req, fresh.clone());
          trimMediaCache();
        }
        return fresh;
      })
    );
    return;
  }

  // 단건 페이지 상세 JSON / 단일 PDF 등은 온라인 우선
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // 앱 쉘(Network-First with Cache Fallback)
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(req, copy)).catch(() => null);
        }
        return res;
      })
      .catch(async () => {
        const cached = await caches.match(req);
        if (cached) return cached;
        if (req.mode === "navigate") {
          const fallback = await caches.match("/");
          if (fallback) return fallback;
        }
        return new Response("오프라인 상태입니다.", {
          status: 503,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
      })
  );
});

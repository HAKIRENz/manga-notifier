// Service worker: ให้ติดตั้งเป็นแอปได้ และเปิดหน้าได้แม้เน็ตหลุด (ข้อมูลยังโหลดสดเสมอ)
const CACHE = "mw-shell-v8";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icon.svg", "icon-192.png"];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname.endsWith(".json")) return; // ข้อมูล/API ไม่แคช
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request).then((r) => r || caches.match("index.html"))));
});

// 📲 แจ้งเตือนตอนใหม่ (Web Push จาก Cloudflare Worker)
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { title: "Manga Watch", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(Promise.all([
    self.registration.showNotification(d.title || "Manga Watch", {
      body: d.body || "", icon: d.icon || "icon-192.png", badge: "icon-192.png", tag: d.tag || undefined, renotify: !!d.tag,
      data: { url: d.url || "./" },
    }),
    // บอกหน้าเว็บที่เปิดอยู่ว่า "เครื่องนี้ได้รับแล้ว" (ใช้ตอนกดส่งทดสอบ)
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((ws) => ws.forEach((w) => w.postMessage({ pushed: d.tag || "" }))),
  ]));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "./", self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((ws) => {
    for (const w of ws) if (w.url.startsWith(self.registration.scope) && "focus" in w) { w.postMessage({ open: url }); return w.focus(); }
    return self.clients.openWindow(url);
  }));
});

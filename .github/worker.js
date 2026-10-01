/**
 * Manga Watch — ประตูหลังบ้าน (Cloudflare Worker, แผนฟรี)
 *
 * หน้าที่: ถือ GitHub Token ไว้ฝั่งเดียว แล้วเปิด API แบบจำกัดสิทธิ์ตามยศ
 *   owner  (เจ้าของ)  ทำได้ทุกอย่าง: จัดการผู้ใช้, ตั้งค่าสรุปประจำวัน, ทดสอบ Discord
 *   mod    (ผู้ดูแล)  เพิ่ม/แก้/ลบ/เปิดปิดเรื่อง, สั่งตรวจ, อนุมัติคำขอ, ส่งข้อความซ้ำ, ดูบันทึก
 *   member (สมาชิก)  ใส่ Discord ID และกดติดตามเรื่องเพื่อให้บอทแท็กชื่อ
 *   ทุกคน (ไม่ล็อกอิน) ดูข้อมูล และส่งคำขอเพิ่มเรื่อง
 * ไม่มีใครนอกจากเจ้าของที่เห็น Token, Webhook, โค้ด หรือแก้ workflow ได้
 *
 * ตั้งค่าใน Cloudflare (Settings → Variables and Secrets):
 *   REPO             เช่น HAKIRENz/manga-notifier          (Text)
 *   BRANCH           main                                    (Text, ไม่บังคับ)
 *   OWNER_NAME       ชื่อล็อกอินของเจ้าของ เช่น boom          (Text)
 *   ALLOWED_ORIGINS  https://hakirenz.github.io              (Text)
 *   GITHUB_TOKEN     Fine-grained token (Contents+Actions RW) (Secret)
 *   OWNER_PASSWORD   รหัสผ่านเจ้าของ                          (Secret)
 *   SESSION_SECRET   ข้อความสุ่มยาว ๆ อย่างน้อย 32 ตัวอักษร   (Secret)
 * และผูก KV namespace ชื่อตัวแปร KV (Bindings → KV namespace)
 * และตั้ง Cron Trigger: Settings → Trigger Events → Add → Cron → ทุก 5 นาที (ดูขั้นตอนใน README)
 *   ทุก 5 นาที Worker จะ "อ่านอย่างเดียว" ว่ามีเรื่องไหนถึงเวลาตรวจ แล้วสั่ง GitHub รันรอบเดียวรวดเดียว
 *   (ไม่เขียน KV ในรอบนี้ → ไม่กินโควตาเขียน 1,000 ครั้ง/วัน)
 */

const ROLE_RANK = { member: 1, mod: 2, owner: 3 };
const MIN_INTERVAL = 10, MAX_INTERVAL = 10080;   // ตรวจได้ตั้งแต่ทุก 10 นาที ถึงทุก 7 วัน
const DATA_BRANCH = "data";                       // state/บันทึก อยู่ branch นี้ (GitHub Actions เขียนทับทุกรอบ)
const SESSION_DAYS = 30;
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36";

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
const fail = (status, msg) => { throw new HttpError(status, msg); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let DEV_LOCAL = false; // ใช้ตอนทดสอบในเครื่องเท่านั้น (DEV_ALLOW_LOCAL=1)
const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");

export default {
  async fetch(request, env, ctx) {
    DEV_LOCAL = env.DEV_ALLOW_LOCAL === "1";
    const cors = corsHeaders(request, env);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    let res;
    try {
      res = await route(request, env, ctx);
    } catch (e) {
      const status = e instanceof HttpError ? e.status : 500;
      const msg = e instanceof HttpError ? e.message : "ระบบหลังบ้านขัดข้อง: " + (e && e.message ? e.message : e);
      res = json({ error: msg }, status);
    }
    res = new Response(res.body, res);
    for (const [k, v] of Object.entries(cors)) res.headers.set(k, v);
    return res;
  },
  // Cron Trigger (ทุก 5 นาที)
  async scheduled(event, env, ctx) {
    DEV_LOCAL = env.DEV_ALLOW_LOCAL === "1";
    ctx.waitUntil(cronTick(env).catch((e) => console.log("cron error", e && e.message)));
  },
};

/* ------------------------------------------------------------------ เส้นทาง API */
async function route(req, env, ctx) {
  const url = new URL(req.url);
  const p = url.pathname.replace(/\/+$/, "") || "/";
  const m = req.method;
  checkConfig(env);

  if (p === "/" || p === "/api") return json({ ok: true, app: "manga-watch", time: nowIso() });
  if (p === "/api/data" && m === "GET") return cached(req, ctx, 20, () => getData(env));
  if (p === "/api/status" && m === "GET") return cached(req, ctx, 30, () => getStatus(env));
  if (p === "/api/login" && m === "POST") return json(await login(req, env));
  if (p === "/api/requests" && m === "POST") return json(await submitRequest(req, env));

  const user = await auth(req, env);
  const purge = () => ctx.waitUntil(caches.default.delete(new Request(url.origin + "/api/data")));

  // ---- ทุกคนที่ล็อกอิน
  if (p === "/api/me" && m === "GET") return json(await getMe(env, user));
  if (p === "/api/me" && m === "PUT") { const r = await setDiscord(env, user, await body(req)); purge(); return json(r); }
  if (p === "/api/subscribe" && m === "POST") { const r = await subscribe(env, user, await body(req)); purge(); return json(r); }

  // ---- ผู้ดูแลขึ้นไป
  need(user, "mod");
  let mm;
  if (p === "/api/series" && m === "POST") { const r = await addSeries(env, user, await body(req)); purge(); return json(r); }
  if ((mm = p.match(/^\/api\/series\/([\w-]{1,40})$/))) {
    if (m === "PATCH") { const r = await editSeries(env, user, mm[1], await body(req)); purge(); return json(r); }
    if (m === "DELETE") { const r = await deleteSeries(env, user, mm[1]); purge(); return json(r); }
  }
  if (p === "/api/check" && m === "POST") {
    const b = await body(req, true);
    const only = b.only ? String(b.only).slice(0, 40) : "";
    const since = await dispatch(env, only ? { only } : {});
    await audit(env, user, "check", only ? "ตรวจเรื่อง " + only : "ตรวจทุกเรื่อง");
    return json({ ok: true, since });
  }
  if (p === "/api/run" && m === "GET") {
    const r = await runAfter(env, url.searchParams.get("since"));
    if (r.status === "completed") purge();   // ให้หน้าเว็บโหลดผลล่าสุดทันที ไม่ติดแคช
    return json(r);
  }
  if (p === "/api/preview" && m === "POST") return json(await preview((await body(req)).url));
  if (p === "/api/resend" && m === "POST") {
    const id = String((await body(req)).id || "").replace(/[^\w]/g, "").slice(0, 40);
    if (!id) fail(400, "ไม่ระบุข้อความ");
    const since = await dispatch(env, { resend: id });
    await audit(env, user, "resend", id);
    return json({ ok: true, since });
  }
  if (p === "/api/audit" && m === "GET") return json(await kvGet(env, "audit", []));
  if (p === "/api/requests" && m === "GET") return json(await kvGet(env, "requests", []));
  if ((mm = p.match(/^\/api\/requests\/([\w]{1,20})\/(approve|reject)$/)) && m === "POST") {
    const r = await decideRequest(env, user, mm[1], mm[2], await body(req, true)); purge(); return json(r);
  }

  // ---- เจ้าของเท่านั้น
  need(user, "owner");
  if (p === "/api/test" && m === "POST") {
    const b = await body(req, true);
    const sid = b.sid ? String(b.sid).replace(/[^\w-]/g, "").slice(0, 40) : "";
    const since = await dispatch(env, sid ? { test_series: sid } : { test: "1" });
    await audit(env, user, "test", sid ? "ทดสอบส่งเรื่อง " + sid : "ทดสอบ Discord");
    return json({ ok: true, since });
  }
  if (p === "/api/users" && m === "GET") return json(await listUsers(env));
  if (p === "/api/users" && m === "POST") return json(await createUser(env, user, await body(req)));
  if ((mm = p.match(/^\/api\/users\/([^/]{1,40})$/))) {
    const name = decodeURIComponent(mm[1]);
    if (m === "PATCH") return json(await updateUser(env, user, name, await body(req)));
    if (m === "DELETE") return json(await deleteUser(env, user, name));
  }
  if (p === "/api/subs" && m === "GET") return json(await readFile(env, "subs.json", { users: {} }));
  if ((mm = p.match(/^\/api\/subs\/([^/]{1,60})$/))) {
    const name = decodeURIComponent(mm[1]);
    if (m === "PUT") { const r = await putSub(env, user, name, await body(req)); purge(); return json(r); }
    if (m === "DELETE") { const r = await deleteSub(env, user, name); purge(); return json(r); }
  }
  if (p === "/api/config" && m === "GET") return json(await readFile(env, "config.json", {}));
  if (p === "/api/config" && m === "PUT") { const r = await setConfig(env, user, await body(req)); purge(); return json(r); }

  fail(404, "ไม่พบคำสั่งนี้");
}

/* ------------------------------------------------------------------ ตัวช่วยทั่วไป */
function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json; charset=utf-8" } });
}
function corsHeaders(req, env) {
  const origin = req.headers.get("Origin") || "";
  const allow = String(env.ALLOWED_ORIGINS || "*").split(",").map((s) => s.trim()).filter(Boolean);
  const value = allow.includes("*") ? "*" : allow.includes(origin) ? origin : allow[0];
  return {
    "Access-Control-Allow-Origin": value,
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}
function checkConfig(env) {
  const miss = ["REPO", "GITHUB_TOKEN", "OWNER_PASSWORD", "SESSION_SECRET"].filter((k) => !env[k]);
  if (miss.length) fail(500, "ยังตั้งค่า Worker ไม่ครบ: " + miss.join(", "));
  if (String(env.SESSION_SECRET).length < 16) fail(500, "SESSION_SECRET สั้นเกินไป (ต้องยาวอย่างน้อย 16 ตัวอักษร)");
  if (!env.KV) fail(500, "ยังไม่ได้ผูก KV namespace ชื่อ KV");
}
async function body(req, optional = false) {
  const len = +(req.headers.get("Content-Length") || 0);
  if (len > 20000) fail(413, "ข้อมูลใหญ่เกินไป");
  const text = await req.text();
  if (!text) { if (optional) return {}; fail(400, "ไม่มีข้อมูล"); }
  try { const j = JSON.parse(text); if (j && typeof j === "object") return j; } catch (e) { /* ด้านล่าง */ }
  fail(400, "ข้อมูลไม่ถูกต้อง");
}
async function cached(req, ctx, ttl, make) {
  const url = new URL(req.url);
  const key = new Request(url.origin + url.pathname);
  const cache = caches.default;
  const hit = await cache.match(key);
  if (hit) return hit;
  const out = await make();
  const res = out instanceof Response ? out : json(out);
  res.headers.set("Cache-Control", "public, max-age=" + ttl);
  ctx.waitUntil(cache.put(key, res.clone()));
  return res;
}
const clean = (s, max) => String(s == null ? "" : s).replace(/[\u0000-\u001f]/g, " ").replace(/\s+/g, " ").trim().slice(0, max);
function validUrl(raw) {
  let u;
  try { u = new URL(String(raw || "").trim()); } catch (e) { fail(400, "ลิงก์ไม่ถูกต้อง"); }
  if (!/^https?:$/.test(u.protocol)) fail(400, "ลิงก์ต้องขึ้นต้นด้วย http:// หรือ https://");
  const h = u.hostname;
  if (!DEV_LOCAL && (h === "localhost" || /^(\d+\.){3}\d+$/.test(h) || h.includes(":") || !h.includes("."))) fail(400, "ลิงก์นี้ไม่อนุญาต");
  if (u.href.length > 500) fail(400, "ลิงก์ยาวเกินไป");
  return u.href;
}
const normUrl = (u) => { try { const x = new URL(u); return (x.hostname.replace(/^www\./, "") + x.pathname.replace(/\/+$/, "") + x.search).toLowerCase(); } catch (e) { return String(u).toLowerCase(); } };
const newId = (n = 8) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => "abcdefghijklmnopqrstuvwxyz0123456789"[b % 36]).join("");

/* ------------------------------------------------------------------ GitHub */
const branch = (env) => env.BRANCH || "main";
let TOKEN_EXP = null;   // วันหมดอายุของ GitHub Token (GitHub บอกมาใน header ทุกครั้งที่เรียก)
function parseGhDate(v) {
  const m = /^(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d(?::\d\d)?)\s*(UTC|Z|[+-]\d\d:?\d\d)?$/.exec(String(v || "").trim());
  if (!m) return null;
  let z = m[3] || "Z";
  if (z === "UTC") z = "Z"; else if (/^[+-]\d{4}$/.test(z)) z = z.slice(0, 3) + ":" + z.slice(3);
  const t = Date.parse(m[1] + "T" + m[2] + z);
  return Number.isFinite(t) ? new Date(t).toISOString() : null;
}
async function gh(env, path, opts = {}) {
  const r = await ghRaw(env, path, opts);
  const exp = r.headers.get("github-authentication-token-expiration");
  if (exp) TOKEN_EXP = parseGhDate(exp) || TOKEN_EXP;
  return r;
}
function ghRaw(env, path, opts = {}) {
  const base = env.GH_API || "https://api.github.com";
  return fetch(base + "/repos/" + env.REPO + path, {
    ...opts,
    headers: {
      Authorization: "Bearer " + env.GITHUB_TOKEN,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "manga-watch-worker",
      ...(opts.headers || {}),
    },
  });
}
const b64e = (s) => { const b = new TextEncoder().encode(s); let r = ""; for (let i = 0; i < b.length; i += 0x8000) r += String.fromCharCode(...b.subarray(i, i + 0x8000)); return btoa(r); };
const b64d = (s) => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\n/g, "")), (c) => c.charCodeAt(0)));

/** อ่านไฟล์เป็นข้อความดิบ (null = ไม่มีไฟล์) */
async function readText(env, file, ref) {
  const r = await gh(env, "/contents/" + file + "?ref=" + encodeURIComponent(ref || branch(env)), { headers: { Accept: "application/vnd.github.raw+json" } });
  if (r.status === 404) return null;
  if (r.status === 401) fail(502, "GitHub Token ใช้ไม่ได้หรือหมดอายุ (เจ้าของต้องตั้ง GITHUB_TOKEN ใหม่)");
  if (!r.ok) fail(502, "อ่าน " + file + " จาก GitHub ไม่ได้ (" + r.status + ")");
  return r.text();
}
async function readFile(env, file, def, ref) {
  const t = await readText(env, file, ref);
  if (t == null) return def;
  try { return JSON.parse(t); } catch (e) { return def; }
}
/** ไฟล์ที่ระบบตรวจเขียน (state/บันทึก) อยู่ใน branch data — ถ้ายังไม่มี (ก่อนรันรอบแรก) ใช้ของ main */
async function readDataText(env, file) {
  const t = await readText(env, file, DATA_BRANCH).catch(() => null);
  return t != null ? t : readText(env, file);
}
const parseOr = (t, def) => { if (t == null) return def; try { return JSON.parse(t); } catch (e) { return def; } };
/** อ่าน-แก้-เขียนไฟล์ใน repo แบบกันชนกัน (ลองใหม่ถ้ามีคนแก้พร้อมกัน) — fn คืน undefined = ไม่ต้องบันทึก */
async function mutateFile(env, file, def, msg, fn) {
  for (let i = 0; i < 4; i++) {
    const r = await gh(env, "/contents/" + file + "?ref=" + branch(env));
    let cur = def, sha;
    if (r.ok) { const j = await r.json(); sha = j.sha; try { cur = JSON.parse(b64d(j.content)); } catch (e) { cur = def; } }
    else if (r.status === 401) fail(502, "GitHub Token ใช้ไม่ได้หรือหมดอายุ");
    else if (r.status !== 404) fail(502, "อ่าน " + file + " ไม่ได้ (" + r.status + ")");
    const next = await fn(JSON.parse(JSON.stringify(cur)));
    if (next === undefined) return cur;
    const payload = { message: msg, content: b64e(JSON.stringify(next, null, 2) + "\n"), branch: branch(env) };
    if (sha) payload.sha = sha;
    const put = await gh(env, "/contents/" + file, { method: "PUT", body: JSON.stringify(payload) });
    if (put.ok) return next;
    if (put.status === 403) fail(502, "Token ไม่มีสิทธิ์เขียน repo (ต้องตั้ง Contents: Read and write)");
    if (put.status !== 409 && put.status !== 422) fail(502, "บันทึก " + file + " ไม่สำเร็จ (" + put.status + ")");
    await sleep(350 * (i + 1));
  }
  fail(409, "มีคนแก้ไขพร้อมกัน ลองใหม่อีกครั้ง");
}
async function dispatch(env, inputs) {
  const since = new Date(Date.now() - 5000).toISOString();
  const r = await gh(env, "/actions/workflows/check.yml/dispatches", { method: "POST", body: JSON.stringify({ ref: branch(env), inputs }) });
  if (r.status === 422) fail(502, "workflow ยังเป็นเวอร์ชันเก่า — อัปโหลด .github/workflows/check.yml ตัวใหม่");
  if (!r.ok) fail(502, "สั่งรันไม่สำเร็จ (GitHub " + r.status + ") — Token ต้องมีสิทธิ์ Actions: Read and write");
  return since;
}
async function runAfter(env, since) {
  const t = Date.parse(since || "");
  if (!t) fail(400, "ไม่ระบุเวลา");
  const r = await gh(env, "/actions/workflows/check.yml/runs?event=workflow_dispatch&per_page=10");
  if (!r.ok) return { status: "unknown" };
  const runs = ((await r.json()).workflow_runs || []).filter((x) => Date.parse(x.created_at) >= t)
    .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  const run = runs[0];
  if (!run) return { status: "waiting" };
  return { status: run.status, conclusion: run.conclusion, url: run.html_url };
}

/* ------------------------------------------------------------------ ข้อมูลสาธารณะ */
async function getData(env) {
  // ส่งต่อไฟล์ใหญ่ (series/state/log) แบบข้อความดิบ ไม่ต้องแปลง JSON → กิน CPU น้อย (แผนฟรีจำกัด 10ms)
  const [seriesT, stateT, pubT, subs, config, requests] = await Promise.all([
    readText(env, "series.json"),
    readDataText(env, "state.json"),
    readText(env, "public_log.json", DATA_BRANCH).catch(() => null),
    readFile(env, "subs.json", { users: {} }),
    readFile(env, "config.json", {}),
    kvGet(env, "requests", []),
  ]);
  let logT = pubT;
  if (logT == null) logT = JSON.stringify(slimLog(parseOr(await readDataText(env, "notify_log.json"), [])));   // ก่อนมี public_log.json
  // ใครติดตามเรื่องไหน (แสดงแค่ชื่อ ไม่ส่ง Discord ID ออกไป)
  const followers = {};
  for (const [name, u] of Object.entries((subs && subs.users) || {})) {
    for (const sid of u.series || []) (followers[sid] = followers[sid] || []).push(name);
  }
  const ok = (t, def) => (t != null && /^\s*[[{]/.test(t) ? t : def);
  return new Response('{"series":' + ok(seriesT, "[]") + ',"state":' + ok(stateT, "{}") + ',"log":' + ok(logT, "[]") +
    ',"followers":' + JSON.stringify(followers) + ',"config":' + JSON.stringify(publicConfig(config)) +
    ',"pending":' + requests.length + ',"at":"' + nowIso() + '"}', { headers: { "Content-Type": "application/json; charset=utf-8" } });
}
function slimLog(log) {
  return (Array.isArray(log) ? log : []).slice(0, 80).map((e) => {
    const em = (e.payload && e.payload.embeds && e.payload.embeds[0]) || {};
    return {
      id: e.id, at: e.at, kind: e.kind, status: e.status, error: e.error, sid: e.sid, name: e.name,
      label: e.label, prev: e.prev, mentions: e.mentions, count: e.count, ref: e.ref, resent: e.resent, late: e.late,
      embed: { title: em.title, description: em.description, url: em.url, color: em.color,
        thumb: em.thumbnail && em.thumbnail.url, footer: em.footer && em.footer.text },
    };
  });
}
function publicConfig(c) {
  c = c || {};
  const a = c.alert || {};
  return { tz_offset: c.tz_offset ?? 7, digest: c.digest || {}, peak: Object.assign({}, PEAK_DEFAULT, c.peak || {}),
    alert: { enabled: a.enabled !== false, set: /^\d{15,22}$/.test(String(a.discord || "")) } };
}
async function getStatus(env) {
  const r = await gh(env, "/actions/workflows/check.yml/runs?per_page=20");
  if (!r.ok) return { runs: [], error: r.status === 401 ? "GitHub Token ใช้ไม่ได้หรือหมดอายุ" : "อ่านสถานะจาก GitHub ไม่ได้ (" + r.status + ")", token_bad: r.status === 401 };
  const runs = ((await r.json()).workflow_runs || []).map((x) => ({
    at: x.created_at, event: x.event, status: x.status, conclusion: x.conclusion, title: x.display_title,
    secs: x.updated_at && x.run_started_at ? Math.max(0, (Date.parse(x.updated_at) - Date.parse(x.run_started_at)) / 1000) : null,
    url: x.html_url,
  }));
  return { runs, every: 5, token_exp: TOKEN_EXP, at: nowIso() };
}

/* ------------------------------------------------------------------ ล็อกอิน / ยศ */
const enc = (s) => new TextEncoder().encode(s);
const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
async function hmac(env, msg) {
  const key = await crypto.subtle.importKey("raw", enc(env.SESSION_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64url(await crypto.subtle.sign("HMAC", key, enc(msg)));
}
function same(a, b) {
  a = String(a); b = String(b);
  let d = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) d |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return d === 0;
}
const ownerName = (env) => String(env.OWNER_NAME || "owner").trim();
const keyOf = (name) => String(name).trim().toLowerCase();
const passHash = (env, salt, pw) => hmac(env, "pw:" + salt + ":" + pw);

async function kvGet(env, key, def) {
  const v = await env.KV.get(key);
  if (v == null) return def;
  try { return JSON.parse(v); } catch (e) { return def; }
}
const getUsers = (env) => kvGet(env, "users", {});

async function login(req, env) {
  const b = await body(req);
  const name = clean(b.name, 40), pw = String(b.password || "");
  if (!name || !pw) fail(400, "กรอกชื่อและรหัสผ่าน");
  const ip = req.headers.get("CF-Connecting-IP") || "local";
  const rlKey = "rl:login:" + ip;
  const tries = +((await env.KV.get(rlKey)) || 0);
  if (tries >= 8) fail(429, "ลองผิดหลายครั้งเกินไป รอ 15 นาทีแล้วลองใหม่");

  let user = null;
  if (keyOf(name) === keyOf(ownerName(env))) {
    if (same(await hmac(env, "x:" + pw), await hmac(env, "x:" + env.OWNER_PASSWORD))) user = { name: ownerName(env), role: "owner", ver: 0 };
  } else {
    const u = (await getUsers(env))[keyOf(name)];
    if (u && same(await passHash(env, u.salt, pw), u.hash)) user = { name: u.name, role: u.role, ver: u.ver || 0 };
  }
  if (!user) {
    await env.KV.put(rlKey, String(tries + 1), { expirationTtl: 900 });
    await sleep(400);
    fail(401, "ชื่อหรือรหัสผ่านไม่ถูกต้อง");
  }
  const payload = b64url(enc(JSON.stringify({ n: user.name, r: user.role, v: user.ver, e: Date.now() + SESSION_DAYS * 864e5 })));
  return { token: payload + "." + (await hmac(env, "s:" + payload)), user: { name: user.name, role: user.role } };
}
async function auth(req, env) {
  const h = req.headers.get("Authorization") || "";
  const tok = h.startsWith("Bearer ") ? h.slice(7) : "";
  const [payload, sig] = tok.split(".");
  if (!payload || !sig || !same(sig, await hmac(env, "s:" + payload))) fail(401, "กรุณาเข้าสู่ระบบ");
  let d;
  try { d = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(payload.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0)))); }
  catch (e) { fail(401, "กรุณาเข้าสู่ระบบ"); }
  if (!d.e || d.e < Date.now()) fail(401, "หมดเวลาเข้าสู่ระบบ กรุณาล็อกอินใหม่");
  if (d.r === "owner" && keyOf(d.n) === keyOf(ownerName(env))) return { name: ownerName(env), role: "owner" };
  const u = (await getUsers(env))[keyOf(d.n)];
  if (!u || (u.ver || 0) !== d.v) fail(401, "บัญชีนี้ถูกเปลี่ยนรหัสหรือถูกลบ กรุณาล็อกอินใหม่");
  return { name: u.name, role: u.role };
}
function need(user, role) {
  if ((ROLE_RANK[user.role] || 0) < ROLE_RANK[role]) fail(403, role === "owner" ? "เฉพาะเจ้าของเท่านั้น" : "ต้องเป็นผู้ดูแลขึ้นไป");
}
async function audit(env, user, act, detail) {
  const list = await kvGet(env, "audit", []);
  list.unshift({ at: nowIso(), who: user.name, role: user.role, act, detail: clean(detail, 160) });
  await env.KV.put("audit", JSON.stringify(list.slice(0, 300)));
}

/* ------------------------------------------------------------------ ผู้ใช้ (เจ้าของ) */
const NAME_RE = /^[\p{L}\p{M}\p{N}_.\- ]{2,24}$/u;
async function listUsers(env) {
  const users = await getUsers(env);
  return Object.values(users).map((u) => ({ name: u.name, role: u.role, created: u.created, by: u.by }))
    .sort((a, b) => (ROLE_RANK[b.role] - ROLE_RANK[a.role]) || a.name.localeCompare(b.name));
}
function checkPw(pw) {
  if (String(pw || "").length < 6) fail(400, "รหัสผ่านต้องยาวอย่างน้อย 6 ตัวอักษร");
  if (String(pw).length > 100) fail(400, "รหัสผ่านยาวเกินไป");
}
async function createUser(env, me, b) {
  const name = clean(b.name, 24), role = b.role === "mod" ? "mod" : "member";
  if (!NAME_RE.test(name)) fail(400, "ชื่อใช้ได้ 2-24 ตัว (ตัวอักษร ตัวเลข _ . -)");
  if (keyOf(name) === keyOf(ownerName(env))) fail(400, "ชื่อนี้เป็นของเจ้าของ");
  checkPw(b.password);
  const users = await getUsers(env);
  if (users[keyOf(name)]) fail(409, "มีชื่อนี้แล้ว");
  const salt = newId(12);
  users[keyOf(name)] = { name, role, salt, hash: await passHash(env, salt, b.password), ver: 1, created: nowIso(), by: me.name };
  await env.KV.put("users", JSON.stringify(users));
  await audit(env, me, "user.add", name + " (" + role + ")");
  return { ok: true, user: { name, role } };
}
async function updateUser(env, me, name, b) {
  const users = await getUsers(env);
  const u = users[keyOf(name)];
  if (!u) fail(404, "ไม่พบผู้ใช้");
  const notes = [];
  if (b.role && b.role !== u.role) {
    if (!["mod", "member"].includes(b.role)) fail(400, "ยศไม่ถูกต้อง");
    u.role = b.role; u.ver = (u.ver || 0) + 1; notes.push("ยศ → " + b.role);
  }
  if (b.password) {
    checkPw(b.password);
    u.salt = newId(12); u.hash = await passHash(env, u.salt, b.password); u.ver = (u.ver || 0) + 1; notes.push("เปลี่ยนรหัส");
  }
  await env.KV.put("users", JSON.stringify(users));
  await audit(env, me, "user.edit", u.name + ": " + notes.join(", "));
  return { ok: true, user: { name: u.name, role: u.role } };
}
async function deleteUser(env, me, name) {
  const users = await getUsers(env);
  const u = users[keyOf(name)];
  if (!u) fail(404, "ไม่พบผู้ใช้");
  delete users[keyOf(name)];
  await env.KV.put("users", JSON.stringify(users));
  await audit(env, me, "user.delete", u.name);
  return { ok: true };
}

/* ------------------------------------------------------------------ โปรไฟล์ / ติดตาม (ทุกยศ) */
async function getMe(env, user) {
  const subs = await readFile(env, "subs.json", { users: {} });
  const mine = (subs.users || {})[user.name] || {};
  return { name: user.name, role: user.role, discord: mine.discord || "", series: mine.series || [] };
}
async function setDiscord(env, user, b) {
  const id = String(b.discord || "").trim();
  if (id && !/^\d{15,22}$/.test(id)) fail(400, "Discord ID ต้องเป็นตัวเลข 15-22 หลัก (ไม่ใช่ชื่อผู้ใช้)");
  await mutateFile(env, "subs.json", { users: {} }, "discord id: " + user.name, (s) => {
    s.users = s.users || {};
    const u = (s.users[user.name] = s.users[user.name] || { series: [] });
    if ((u.discord || "") === id) return undefined;
    u.discord = id;
    return s;
  });
  return getMe(env, user);
}
async function subscribe(env, user, b) {
  const sid = String(b.sid || "").slice(0, 40), on = !!b.on;
  if (!sid) fail(400, "ไม่ระบุเรื่อง");
  await mutateFile(env, "subs.json", { users: {} }, (on ? "follow " : "unfollow ") + sid + " by " + user.name, (s) => {
    s.users = s.users || {};
    const u = (s.users[user.name] = s.users[user.name] || { series: [] });
    const set = new Set(u.series || []);
    if (on === set.has(sid)) return undefined;
    on ? set.add(sid) : set.delete(sid);
    u.series = [...set];
    return s;
  });
  return getMe(env, user);
}

/* ------------------------------------------------------------------ รายชื่อแท็ก Discord (เจ้าของ) */
// เจ้าของเพิ่ม/แก้ Discord ID และเรื่องที่ติดตามให้ใครก็ได้ (ไม่ต้องมีบัญชีในเว็บ)
async function putSub(env, me, name, b) {
  name = clean(name, 30);
  if (!/^[\p{L}\p{M}\p{N}_.\- ]{1,30}$/u.test(name)) fail(400, "ชื่อใช้ได้ 1-30 ตัว (ตัวอักษร ตัวเลข _ . -)");
  const id = b.discord === undefined ? undefined : String(b.discord || "").trim();
  if (id && !/^\d{15,22}$/.test(id)) fail(400, "Discord ID ต้องเป็นตัวเลข 15-22 หลัก (ไม่ใช่ชื่อผู้ใช้)");
  let series = b.series;
  if (series !== undefined) {
    if (!Array.isArray(series)) fail(400, "รายการเรื่องไม่ถูกต้อง");
    series = [...new Set(series.map((x) => String(x).slice(0, 40)))].slice(0, 300);
  }
  let out = null;
  await mutateFile(env, "subs.json", { users: {} }, "subs: " + name + " by " + me.name, (s) => {
    s.users = s.users || {};
    const u = (s.users[name] = s.users[name] || { series: [] });
    if (id !== undefined) u.discord = id;
    if (series !== undefined) u.series = series;
    out = u;
    return s;
  });
  await audit(env, me, "subs.edit", name + (id !== undefined ? " (Discord ID)" : "") + (series !== undefined ? " (" + series.length + " เรื่อง)" : ""));
  return { ok: true, name, user: out };
}
async function deleteSub(env, me, name) {
  let found = false;
  await mutateFile(env, "subs.json", { users: {} }, "subs remove " + name + " by " + me.name, (s) => {
    if (!s.users || !s.users[name]) return undefined;
    delete s.users[name]; found = true;
    return s;
  });
  if (!found) fail(404, "ไม่พบชื่อนี้");
  await audit(env, me, "subs.delete", name);
  return { ok: true };
}

/* ------------------------------------------------------------------ จัดการเรื่อง (ผู้ดูแล) */
function seriesInput(b, partial) {
  const out = {};
  if (!partial || b.name !== undefined) { out.name = clean(b.name, 80); if (!out.name) fail(400, "ใส่ชื่อเรื่อง"); }
  if (!partial || b.url !== undefined) out.url = validUrl(b.url);
  if (b.contains !== undefined) out.contains = clean(b.contains, 100);
  if (b.sched !== undefined) out.sched = schedInput(b.sched);
  if (b.autoslow !== undefined) out.autoslow = !!b.autoslow;
  if (b.enabled !== undefined) out.enabled = !!b.enabled;
  return out;
}
const winLen = (x) => { const m = SLOT_RE.exec(x); if (!m || m[3] == null) return 0; const a = +m[1] * 60 + +m[2], b = +m[3] * 60 + +m[4]; return ((b - a + 1440) % 1440) || 1440; };
function slotsInput(raw, maxWin, what) {
  if (!Array.isArray(raw)) fail(400, "เวลาไม่ถูกต้อง");
  const slots = [...new Set(raw.map((x) => String(x).trim()))];
  if (slots.some((x) => !SLOT_RE.test(x))) fail(400, "เวลาต้องเป็น ชม:นาที เช่น 20:00 หรือช่วง 19:50-21:00");
  if (slots.length > 12) fail(400, "ตั้งได้ไม่เกิน 12 เวลา/ช่วง");
  const total = slots.reduce((a, x) => a + winLen(x), 0);
  if (total > maxWin) fail(400, what + "รวมกันได้ไม่เกิน " + maxWin / 60 + " ชม./วัน (กันเว็บบล็อกเพราะตรวจถี่เกินไป)");
  return slots.sort();
}
function schedInput(sc) {
  if (!sc || typeof sc !== "object") fail(400, "ตั้งเวลาตรวจไม่ถูกต้อง");
  const int = (v, lo, hi, msg) => { const n = Math.round(+v); if (!(n >= lo && n <= hi)) fail(400, msg); return n; };
  if (sc.mode === "peak") return { mode: "peak" };
  if (sc.mode === "every") return { mode: "every", min: int(sc.min, MIN_INTERVAL, MAX_INTERVAL, "ความถี่ต้องอยู่ระหว่างทุก 10 นาที ถึงทุก 7 วัน") };
  if (sc.mode === "custom") {
    const slots = slotsInput(sc.slots, 360, "ช่วงเฝ้า");
    if (!slots.length) fail(400, "เพิ่มเวลาหรือช่วงเฝ้าอย่างน้อย 1 อัน");
    const tz = +sc.tz;
    if (![7, 8, 9].includes(tz)) fail(400, "โซนเวลาไม่ถูกต้อง");
    return { mode: "custom", tz, slots, every: int(sc.every ?? 5, 5, 60, "ความถี่ในช่วงเฝ้าต้อง 5-60 นาที"),
      off: int(sc.off ?? 360, 60, 1440, "นอกช่วงต้องตรวจทุก 1-24 ชม.") };
  }
  fail(400, "ไม่รู้จักโหมดตั้งเวลา");
}
async function addSeries(env, user, b, viaRequest) {
  const inp = seriesInput(b, false);
  const item = { id: newId(8), name: inp.name, url: inp.url, contains: inp.contains || "", enabled: true,
    sched: inp.sched || { mode: "peak" }, by: viaRequest ? viaRequest + " (อนุมัติโดย " + user.name + ")" : user.name, added: nowIso() };
  if (inp.autoslow === false) item.autoslow = false;
  await mutateFile(env, "series.json", [], "add \"" + item.name + "\" by " + user.name, (list) => {
    if (list.length >= 200) fail(400, "รายการเต็มแล้ว (200 เรื่อง)");
    const dup = list.find((s) => normUrl(s.url) === normUrl(item.url));
    if (dup) fail(409, "เรื่องนี้มีแล้ว: " + dup.name);
    list.push(item);
    return list;
  });
  await audit(env, user, "series.add", item.name);
  let since = null, warn = null;
  try { since = await dispatch(env, { only: item.id }); } catch (e) { warn = e.message; }
  return { ok: true, item, since, warn };
}
async function editSeries(env, user, id, b) {
  const inp = seriesInput(b, true);
  let changed = null;
  await mutateFile(env, "series.json", [], "edit " + id + " by " + user.name, (list) => {
    const s = list.find((x) => x.id === id);
    if (!s) fail(404, "ไม่พบเรื่องนี้ (อาจถูกลบไปแล้ว)");
    if (inp.url && normUrl(inp.url) !== normUrl(s.url) && list.some((x) => x.id !== id && normUrl(x.url) === normUrl(inp.url))) fail(409, "ลิงก์นี้มีในเรื่องอื่นแล้ว");
    Object.assign(s, inp);
    if (inp.sched) { delete s.interval; delete s.times; }   // ล้างรูปแบบเก่า
    changed = s;
    return list;
  });
  const what = Object.keys(inp).map((k) => k === "enabled" ? (inp.enabled ? "เปิด" : "ปิด") : k === "sched" ? "เวลาตรวจ" : k).join(", ");
  await audit(env, user, "series.edit", changed.name + " (" + what + ")");
  return { ok: true, item: changed };
}
async function deleteSeries(env, user, id) {
  let gone = null;
  await mutateFile(env, "series.json", [], "remove " + id + " by " + user.name, (list) => {
    const i = list.findIndex((x) => x.id === id);
    if (i < 0) fail(404, "ไม่พบเรื่องนี้ (อาจถูกลบไปแล้ว)");
    gone = list.splice(i, 1)[0];
    return list;
  });
  await audit(env, user, "series.delete", gone.name);
  return { ok: true };
}

/* ------------------------------------------------------------------ คำขอเพิ่มเรื่อง */
async function submitRequest(req, env) {
  const b = await body(req);
  if (b.website) return { ok: true };                 // กับดักบอท (ช่องซ่อน)
  const name = clean(b.name, 80), url = validUrl(b.url), note = clean(b.note, 200), from = clean(b.from, 30) || "ไม่ระบุชื่อ";
  if (!name) fail(400, "ใส่ชื่อเรื่อง");
  const ip = req.headers.get("CF-Connecting-IP") || "local";
  const rlKey = "rl:req:" + ip;
  const n = +((await env.KV.get(rlKey)) || 0);
  if (n >= 6) fail(429, "ส่งคำขอบ่อยเกินไป ลองใหม่ในอีก 1 ชั่วโมง");
  const list = await kvGet(env, "requests", []);
  if (list.length >= 50) fail(429, "คำขอค้างเยอะแล้ว รอผู้ดูแลจัดการก่อน");
  if (list.some((r) => normUrl(r.url) === normUrl(url))) fail(409, "มีคนขอเรื่องนี้ไว้แล้ว รอผู้ดูแลอนุมัติ");
  const series = await readFile(env, "series.json", []);
  const dup = series.find((s) => normUrl(s.url) === normUrl(url));
  if (dup) fail(409, "เรื่องนี้มีในรายการแล้ว: " + dup.name);
  list.push({ id: newId(10), name, url, note, from, at: nowIso() });
  await env.KV.put("requests", JSON.stringify(list));
  await env.KV.put(rlKey, String(n + 1), { expirationTtl: 3600 });
  return { ok: true };
}
async function decideRequest(env, user, id, action, b) {
  const list = await kvGet(env, "requests", []);
  const r = list.find((x) => x.id === id);
  if (!r) fail(404, "ไม่พบคำขอ (อาจมีคนจัดการไปแล้ว)");
  let result = { ok: true };
  if (action === "approve") {
    result = await addSeries(env, user, { name: b.name || r.name, url: r.url, contains: b.contains, sched: b.sched, autoslow: b.autoslow }, r.from);
  } else {
    await audit(env, user, "request.reject", r.name + " (จาก " + r.from + ")");
  }
  const fresh = (await kvGet(env, "requests", [])).filter((x) => x.id !== id);
  await env.KV.put("requests", JSON.stringify(fresh));
  return result;
}

/* ------------------------------------------------------------------ ตั้งค่า (เจ้าของ) */
async function setConfig(env, user, b) {
  const notes = [];
  let digest, peak, alert;
  if (b.digest) {
    const d = b.digest;
    digest = { enabled: !!d.enabled, hour: Math.max(0, Math.min(23, parseInt(d.hour, 10) || 0)), send_empty: !!d.send_empty };
    notes.push("สรุปประจำวัน " + (digest.enabled ? "เปิด " + digest.hour + ":00" : "ปิด"));
  }
  if (b.peak) {
    const windows = slotsInput(b.peak.windows || [], 480, "ช่วงออกตอนหลัก").filter((x) => x.includes("-"));
    if (!windows.length) fail(400, "ต้องมีช่วงออกตอนหลักอย่างน้อย 1 ช่วง (เช่น 22:50-00:30)");
    const every = Math.round(+b.peak.every), off = Math.round(+b.peak.off);
    if (!(every >= 5 && every <= 60)) fail(400, "ในช่วงหลักตรวจได้ทุก 5-60 นาที");
    if (!(off >= 60 && off <= 1440)) fail(400, "นอกช่วงตรวจได้ทุก 1-24 ชม.");
    peak = { windows, every, off };
    notes.push("ช่วงหลัก " + windows.join(", "));
  }
  if (b.alert) {
    const id = String(b.alert.discord || "").trim();
    if (id && !/^\d{15,22}$/.test(id)) fail(400, "Discord ID ต้องเป็นตัวเลข 15-22 หลัก");
    alert = { enabled: b.alert.enabled !== false, discord: id };
    notes.push("แจ้งเตือนเจ้าของ " + (alert.enabled && id ? "เปิด" : "ปิด"));
  }
  if (!notes.length) fail(400, "ไม่มีอะไรให้บันทึก");
  const next = await mutateFile(env, "config.json", {}, "config by " + user.name, (c) => {
    if (digest) c.digest = digest;
    if (peak) c.peak = peak;
    if (alert) c.alert = alert;
    if (c.tz_offset === undefined) c.tz_offset = 7;
    return c;
  });
  await audit(env, user, "config", notes.join(" · "));
  return { ok: true, config: publicConfig(next) };
}

/* ------------------------------------------------------------------ ตารางเวลาตรวจ (ต้องตรงกับ check.py) */
const PEAK_DEFAULT = { windows: ["22:50-00:30", "04:50-06:30"], every: 5, off: 360 };
const SLOT_RE = /^([01]\d|2[0-3]):([0-5]\d)(?:-([01]\d|2[0-3]):([0-5]\d))?$/;
const clampI = (v, lo, hi, d) => { const n = Math.trunc(+v); return Number.isFinite(n) && v !== null && v !== "" ? Math.max(lo, Math.min(hi, n)) : d; };
function schedOf(s) {
  const sc = s.sched;
  if (sc && ["peak", "every", "custom"].includes(sc.mode)) return sc;
  if (s.times && s.times.length) return { mode: "custom", slots: s.times, off: 1440 };
  return { mode: "peak" };
}
function planOf(s, st, cfg, nowMs) {
  const sc = schedOf(s), c = cfg || {}, pk = Object.assign({}, PEAK_DEFAULT, c.peak || {});
  let tz = +(c.tz_offset ?? 7), slots = [], step = 0, base = 360;
  if (sc.mode === "every") base = clampI(sc.min, 10, 10080, 360);
  else if (sc.mode === "custom") { tz = +(sc.tz ?? tz); slots = sc.slots || []; step = clampI(sc.every, 5, 60, 5); base = clampI(sc.off, 10, 10080, 360); }
  else { slots = pk.windows || []; step = clampI(pk.every, 5, 60, 5); base = clampI(pk.off, 10, 10080, 360); }
  const wins = [], pts = [];
  for (const x of slots) {
    const m = SLOT_RE.exec(String(x));
    if (!m) continue;
    const a = +m[1] * 60 + +m[2];
    pts.push(a);
    if (m[3] != null) wins.push([a, +m[3] * 60 + +m[4]]);
  }
  let dormant = false;
  if (s.autoslow !== false && st) {
    const last = Date.parse(st.updated || st.since || "");
    if (last && nowMs - last > 30 * 864e5) { dormant = true; step = 0; base = Math.max(base, 720); }
  }
  return { tz, wins, pts, step: wins.length ? step : 0, base, dormant, mode: sc.mode };
}
function dueAt(p, triedMs, fails, nowMs) {
  if (!triedMs) return true;
  const el = (nowMs - triedMs) / 6e4;
  if (fails >= 1 && fails <= 3 && el >= 4) return true;
  if (el >= p.base - 2) return true;
  const L = Math.floor(nowMs / 6e4 + p.tz * 60), mod = ((L % 1440) + 1440) % 1440;
  if (p.step && el >= p.step - 1.5 && p.wins.some(([a, b]) => (a <= b ? mod >= a && mod <= b : mod >= a || mod <= b))) return true;
  for (const pt of p.pts) if (triedMs < (L - (((mod - pt) % 1440) + 1440) % 1440 - p.tz * 60) * 6e4) return true;
  return false;
}
const isDue = (s, st, cfg, nowMs) => dueAt(planOf(s, st, cfg, nowMs), Date.parse(st.tried || "") || 0, +st.fails || 0, nowMs);

/* ------------------------------------------------------------------ Cron: สั่งตรวจเมื่อมีเรื่องถึงเวลา */
async function cronTick(env) {
  if (!env.REPO || !env.GITHUB_TOKEN) return;
  const now = Date.now();
  const [series, cfg, stateT] = await Promise.all([
    readFile(env, "series.json", []), readFile(env, "config.json", {}), readDataText(env, "state.json"),
  ]);
  const state = parseOr(stateT, {});
  const nowIsoS = new Date(now).toISOString();
  const due = (Array.isArray(series) ? series : []).filter((s) => {
    if (s.enabled === false) return false;
    const st = state[s.id] || {};
    if (st.blocked_until && Date.parse(st.blocked_until) > now) return false;
    return isDue(s, st, cfg, now);
  });
  // ⏳ Token ใกล้หมดอายุ → แจ้งเจ้าของใน Discord วันละครั้ง (KV เขียนแค่วันละ 1 ครั้ง)
  let alert = "";
  if (TOKEN_EXP && Date.parse(TOKEN_EXP) - now < 7 * 864e5) {
    const day = nowIsoS.slice(0, 10);
    if ((await env.KV.get("alert:token")) !== day) { alert = "token:" + TOKEN_EXP; await env.KV.put("alert:token", day, { expirationTtl: 3 * 86400 }); }
  }
  if (!due.length && !alert) return;
  // มีรอบกำลังรัน/รอคิวอยู่แล้ว → ไม่สั่งซ้อน (รอบนั้นจะตรวจเรื่องที่ถึงเวลาให้)
  const r = await gh(env, "/actions/workflows/check.yml/runs?per_page=5");
  if (r.ok && !alert) {
    const busy = ((await r.json()).workflow_runs || []).some((x) => ["queued", "in_progress", "waiting", "pending", "requested"].includes(x.status));
    if (busy) return;
  }
  await dispatch(env, alert ? { auto: "1", alert } : { auto: "1" });
  console.log("dispatch", due.length, "due", alert ? "+alert" : "");
}

/* ------------------------------------------------------------------ ดูตัวอย่างลิงก์ */
const CH_RE = /(?:(?<![a-z])(?:chapter|chap|ch|episode|ep)|ตอนที่|ตอน|บทที่|บท)[\s._\-/:]*(\d+(?:[.\-]\d+)?)/i;
const LEAD_RE = /^\s*(?:第\s*)?(\d+(?:\.\d+)?)/;
const TAIL_RE = /[/\-_](\d+(?:[.\-]\d+)?)\/?$/;
const YEAR_RE = /^\s*(?:19|20)\d\d\s*[-./年]/;
const toNum = (s) => { const n = parseFloat(String(s).replace("-", ".")); return Number.isFinite(n) ? n : null; };
const fmt = (n) => (Number.isInteger(n) ? String(n) : String(n));
function labelFor(n, text) {
  const m = CH_RE.exec(text || "") || LEAD_RE.exec(text || "");
  const x = m ? toNum(m[1]) : null;
  return fmt(x != null ? x : n);
}
function decodeEntities(s) {
  return String(s || "").replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp|#39);/gi, (m, e) => {
    const l = e.toLowerCase();
    if (l[0] === "#") { const c = l[1] === "x" ? parseInt(l.slice(2), 16) : parseInt(l.slice(1), 10); return c ? String.fromCodePoint(c) : ""; }
    return { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", "#39": "'" }[l] || m;
  });
}
/** ตัดชื่อเว็บออกจากชื่อเรื่อง เช่น "《我的哥哥是恶犬》漫画_番奇动漫_..._腾讯动漫" → "我的哥哥是恶犬" */
function niceName(n) {
  const m = n.match(/《([^》]{1,80})》/);
  if (m) return m[1].trim();
  let first = n.split(/\s+[|\-–—]\s+|_|｜/)[0].trim();
  first = first.replace(/(?:漫画|漫畫)$/, "").trim();
  return (first.length >= 2 ? first : n).slice(0, 80);
}
const safeDecode = (s) => { try { return decodeURIComponent(s); } catch (e) { return s; } };

async function preview(raw) {
  const url = validUrl(raw);
  const host = new URL(url).hostname.replace(/^www\./, "");
  const md = url.match(/mangadex\.org\/title\/([0-9a-f-]{36})/i);
  if (md) return previewMangadex(md[1], host);

  let res;
  try {
    res = await fetch(url, { headers: { "User-Agent": UA, Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "th,en;q=0.8" }, redirect: "follow" });
  } catch (e) { fail(422, "เปิดเว็บนี้ไม่ได้: " + e.message); }
  if ([401, 403, 429, 503].includes(res.status)) fail(422, "เว็บปฏิเสธ (HTTP " + res.status + ") — อาจบล็อกบอท ระบบตรวจจริงบน GitHub อาจได้ผลต่างกัน");
  if (!res.ok) fail(422, "เว็บตอบ HTTP " + res.status);

  const ct = res.headers.get("Content-Type") || "";
  if (/xml|rss|atom/i.test(ct)) {
    const text = (await res.text()).slice(0, 2000000);
    let best = null;
    for (const blk of text.match(/<(?:item|entry)[ >][\s\S]*?<\/(?:item|entry)>/g) || []) {
      const t = (blk.match(/<title[^>]*>([\s\S]*?)<\/title>/) || [])[1] || "";
      const title = decodeEntities(t.replace(/<!\[CDATA\[|\]\]>/g, "").trim());
      const l = blk.match(/<link[^>]*?(?:href=["']([^"']+)|>([^<]+)<\/link>)/);
      const link = l ? (l[1] || l[2]).trim() : "";
      const m = CH_RE.exec(title) || CH_RE.exec(link);
      const n = m ? toNum(m[1]) : null;
      if (n != null && (!best || n > best.number)) best = { number: n, url: link, title: title.slice(0, 80) };
    }
    const ft = decodeEntities(((text.match(/<title[^>]*>([\s\S]*?)<\/title>/) || [])[1] || "").replace(/<!\[CDATA\[|\]\]>/g, ""));
    if (!best) fail(422, "ฟีดนี้ไม่มีตอนที่อ่านเลขได้");
    return { host, name: clean(ft, 80), cover: "", label: labelFor(best.number, best.title), chapterTitle: best.title, chapterUrl: best.url, count: null, kind: "rss" };
  }

  let title = "", ogTitle = "", ogImage = "", cur = null, plain = "", inScript = false;
  const links = [], imgs = [], marks = [];
  await new HTMLRewriter()
    .on("meta", { element(e) {
      const p = (e.getAttribute("property") || e.getAttribute("name") || "").toLowerCase();
      const c = e.getAttribute("content");
      if (!c) return;
      if (p === "og:title" && !ogTitle) ogTitle = c;
      if ((p === "og:image" || p === "twitter:image") && !ogImage && !/logo|favicon|default/i.test(c)) ogImage = c;
    } })
    .on("title", { text(t) { if (title.length < 200) title += t.text; } })
    .on("img", { element(e) {
      if (imgs.length < 300) imgs.push({ src: e.getAttribute("data-original") || e.getAttribute("data-src") || e.getAttribute("src") || "", alt: e.getAttribute("alt") || "", cls: e.getAttribute("class") || "" });
    } })
    .on("a", {
      element(e) { if (links.length < 4000) { cur = { href: e.getAttribute("href") || "", text: "", pos: plain.length }; links.push(cur); } else cur = null; },
      text(t) { if (cur && cur.text.length < 120) cur.text += t.text; },
    })
    .on("script,style", { element(e) { inScript = true; e.onEndTag(() => { inScript = false; }); } })
    .on("[data-num]", { element(e) { marks.push({ pos: plain.length, v: e.getAttribute("data-num") }); } })
    .on("li,div,p,br,span,tr", { element() { if (plain.length < 400000) plain += " "; } })
    .onDocument({ text(t) { if (!inScript && plain.length < 400000) plain += t.text; } })
    .transform(res).arrayBuffer();

  // ---- หาตอนล่าสุด (ลำดับเดียวกับ check.py: text → word → count → group)
  const abs = (h) => { try { return new URL(h, url).href; } catch (e) { return h; } };
  const urlNum = (h) => { const m = CH_RE.exec(safeDecode(h)) || TAIL_RE.exec(h.split("?")[0]); return m ? toNum(m[1]) : null; };
  const textNum = (t) => { if (!t || YEAR_RE.test(t)) return null; const m = CH_RE.exec(t) || LEAD_RE.exec(t); const n = m ? toNum(m[1]) : null; return n != null && n < 100000 ? n : null; };
  const maxOf = (arr) => arr.reduce((b, c) => (!b || c.n > b.n ? c : b), null);
  const L = [];
  for (const a of links) {
    const href = a.href.trim();
    if (!href || /^(#|javascript:|mailto:)/i.test(href)) continue;
    L.push({ href, text: decodeEntities(a.text).replace(/\s+/g, " ").trim(), pos: a.pos });
  }
  // กันลิงก์ของเรื่องอื่น (แถบเรื่องยอดนิยม): ถ้าลิงก์ ≥3 ลิงก์มีชื่อเรื่องจาก URL ใช้เฉพาะพวกนั้น
  const segs = new URL(url).pathname.split("/").filter(Boolean);
  const slug = segs.length ? safeDecode(segs[segs.length - 1]) : "";
  if (slug.length >= 5) { const own = L.filter((a) => safeDecode(a.href).includes(slug)); if (own.length >= 3) L.splice(0, L.length, ...own); }
  let pick = null, mode = null, count = 0, used = [];
  const tc = L.filter((a) => urlNum(a.href) != null).map((a) => ({ ...a, n: textNum(a.text) })).filter((a) => a.n != null);
  if (tc.length >= 3) { pick = maxOf(tc); mode = "text"; count = tc.length; used = tc; }
  if (!pick) {
    const wc = L.map((a) => { const m = CH_RE.exec(safeDecode(a.href)) || CH_RE.exec(a.text); return { ...a, n: m ? toNum(m[1]) : null }; }).filter((a) => a.n != null && a.n < 100000);
    if (wc.length) { pick = maxOf(wc); mode = "url"; count = wc.length; used = wc; }
  }
  if (!pick) {
    const cm = decodeEntities(plain).match(/(?:共|全|ทั้งหมด)\s*(\d{1,5})\s*(?:篇正文|话|話|章|集|回|ตอน)/);
    if (cm) { pick = { n: +cm[1], href: url, text: cm[0] }; mode = "count"; }
  }
  if (!pick) {
    const groups = {};
    for (const a of L) {
      const path = a.href.split("?")[0].split("#")[0];
      const m = TAIL_RE.exec(path);
      if (!m) continue;
      const n = toNum(m[1]);
      if (n == null || n > 100000) continue;
      const key = path.slice(0, m.index).replace(/\d+/g, "#");
      (groups[key] = groups[key] || []).push({ ...a, n });
    }
    const big = Object.values(groups).sort((a, b) => b.length - a.length)[0] || [];
    if (big.length >= 3) { pick = maxOf(big); mode = "group"; count = big.length; used = big; }
  }
  // ตอนล็อก/ล่วงหน้า (ไม่มีลิงก์ เช่นต้องใช้เหรียญ) ในบริเวณรายการตอนเดียวกัน — ตรงกับ check.py
  let locked = false;
  if (pick && used.length) {
    const lo = Math.max(0, Math.min(...used.map((a) => a.pos)) - 3000), hi = Math.max(...used.map((a) => a.pos)) + 3000;
    let extra = null;
    const re = new RegExp(CH_RE.source, "gi");
    for (const m of decodeEntities(plain.slice(lo, hi)).matchAll(re)) {
      const x = toNum(m[1]);
      if (x != null && x > pick.n && x <= pick.n + 20 && (!extra || x > extra.n)) extra = { n: x, text: m[0] };
    }
    for (const mk of marks) {
      const x = /^\d+(\.\d+)?$/.test(mk.v || "") ? toNum(mk.v) : null;
      if (mk.pos >= lo && mk.pos <= hi && x != null && x > pick.n && x <= pick.n + 20 && (!extra || x > extra.n)) extra = { n: x, text: "ตอน " + fmt(x) };
    }
    if (extra) { pick = { n: extra.n, href: url, text: extra.text + " (ล็อก/ล่วงหน้า)" }; locked = true; }
  }

  // ---- ปก: og:image (ที่ไม่ใช่โลโก้) ไม่งั้นเดาจาก <img>
  let cover = "";
  const pageTitle = decodeEntities(title);
  let coverSrc = ogImage, best = 0;
  if (!coverSrc) for (const im of imgs) {
    if (!im.src || /logo|icon|avatar|favicon|\.gif|\.svg/i.test(im.src)) continue;
    const alt = decodeEntities(im.alt).trim();
    let sc = /cover|vertical|poster/i.test(im.cls + " " + im.src) ? 100 : 0;
    if (alt.length >= 2 && pageTitle.includes(alt)) sc += alt.length;
    if (sc > best) { best = sc; coverSrc = im.src; }
  }
  if (coverSrc) { try { const c = new URL(decodeEntities(coverSrc), url); if (/^https?:$/.test(c.protocol)) cover = c.href; } catch (e) { /* ไม่มีปก */ } }
  const name = niceName(clean(decodeEntities(ogTitle || title), 120));
  if (!pick) return { host, name, cover, label: null, count: 0, kind: "html",
    warn: "ไม่พบรายการตอนในหน้านี้ — เว็บอาจโหลดตอนด้วย JavaScript ลองใช้หน้ารายการตอน หรือลิงก์ RSS" };
  const chTitle = pick.text.slice(0, 80);
  return { host, name, cover, label: mode === "text" || mode === "count" || locked ? fmt(pick.n) : labelFor(pick.n, chTitle),
    chapterTitle: chTitle, chapterUrl: abs(pick.href), count, kind: mode === "count" ? "count" : "html" };
}
async function previewMangadex(id, host) {
  const api = "https://api.mangadex.org";
  const h = { headers: { "User-Agent": "manga-watch-worker" } };
  const [info, feed] = await Promise.all([
    fetch(api + "/manga/" + id + "?includes[]=cover_art", h).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    fetch(api + "/manga/" + id + "/feed?limit=1&order[chapter]=desc&translatedLanguage[]=th&translatedLanguage[]=en", h).then((r) => (r.ok ? r.json() : null)).catch(() => null),
  ]);
  if (!info || !info.data) fail(422, "ไม่พบเรื่องนี้ใน MangaDex");
  const a = info.data.attributes || {};
  const titles = a.title || {};
  const alt = (a.altTitles || []).find((t) => t.th) || {};
  const name = alt.th || titles.en || Object.values(titles)[0] || "";
  const rel = (info.data.relationships || []).find((r) => r.type === "cover_art" && r.attributes && r.attributes.fileName);
  const cover = rel ? "https://uploads.mangadex.org/covers/" + id + "/" + rel.attributes.fileName + ".256.jpg" : "";
  const ch = feed && feed.data && feed.data[0];
  if (!ch) return { host, name: clean(name, 80), cover, label: null, count: 0, kind: "mangadex", warn: "ยังไม่มีตอนภาษาไทย/อังกฤษ" };
  const n = toNum(ch.attributes.chapter || "0") || 0;
  return { host, name: clean(name, 80), cover, label: fmt(n), chapterTitle: ch.attributes.title || "", chapterUrl: "https://mangadex.org/chapter/" + ch.id, count: null, kind: "mangadex" };
}

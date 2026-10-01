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
 */

const ROLE_RANK = { member: 1, mod: 2, owner: 3 };
const INTERVALS = [15, 30, 60, 180, 720];
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
    const since = await dispatch(env, { test: "1" });
    await audit(env, user, "test", "ทดสอบ Discord");
    return json({ ok: true, since });
  }
  if (p === "/api/users" && m === "GET") return json(await listUsers(env));
  if (p === "/api/users" && m === "POST") return json(await createUser(env, user, await body(req)));
  if ((mm = p.match(/^\/api\/users\/([^/]{1,40})$/))) {
    const name = decodeURIComponent(mm[1]);
    if (m === "PATCH") return json(await updateUser(env, user, name, await body(req)));
    if (m === "DELETE") return json(await deleteUser(env, user, name));
  }
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
  const res = json(await make());
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
function gh(env, path, opts = {}) {
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

async function readFile(env, file, def) {
  const r = await gh(env, "/contents/" + file + "?ref=" + branch(env), { headers: { Accept: "application/vnd.github.raw+json" } });
  if (r.status === 404) return def;
  if (r.status === 401) fail(502, "GitHub Token ใช้ไม่ได้หรือหมดอายุ (เจ้าของต้องตั้ง GITHUB_TOKEN ใหม่)");
  if (!r.ok) fail(502, "อ่าน " + file + " จาก GitHub ไม่ได้ (" + r.status + ")");
  try { return JSON.parse(await r.text()); } catch (e) { return def; }
}
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
  const [series, state, log, subs, config, requests] = await Promise.all([
    readFile(env, "series.json", []),
    readFile(env, "state.json", {}),
    readFile(env, "notify_log.json", []),
    readFile(env, "subs.json", { users: {} }),
    readFile(env, "config.json", {}),
    kvGet(env, "requests", []),
  ]);
  // ใครติดตามเรื่องไหน (แสดงแค่ชื่อ ไม่ส่ง Discord ID ออกไป)
  const followers = {};
  for (const [name, u] of Object.entries((subs && subs.users) || {})) {
    for (const sid of u.series || []) (followers[sid] = followers[sid] || []).push(name);
  }
  const slimLog = (Array.isArray(log) ? log : []).slice(0, 80).map((e) => {
    const em = (e.payload && e.payload.embeds && e.payload.embeds[0]) || {};
    return {
      id: e.id, at: e.at, kind: e.kind, status: e.status, error: e.error, sid: e.sid, name: e.name,
      label: e.label, prev: e.prev, mentions: e.mentions, count: e.count, ref: e.ref, resent: e.resent,
      embed: { title: em.title, description: em.description, url: em.url, color: em.color,
        thumb: em.thumbnail && em.thumbnail.url, footer: em.footer && em.footer.text },
    };
  });
  return {
    series, state, log: slimLog, followers,
    config: { tz_offset: config.tz_offset ?? 7, digest: config.digest || {} },
    pending: requests.length, at: nowIso(),
  };
}
async function getStatus(env) {
  const r = await gh(env, "/actions/workflows/check.yml/runs?per_page=15");
  if (!r.ok) return { runs: [], error: "อ่านสถานะจาก GitHub ไม่ได้ (" + r.status + ")" };
  const runs = ((await r.json()).workflow_runs || []).map((x) => ({
    at: x.created_at, event: x.event, status: x.status, conclusion: x.conclusion,
    secs: x.updated_at && x.run_started_at ? Math.max(0, (Date.parse(x.updated_at) - Date.parse(x.run_started_at)) / 1000) : null,
    url: x.html_url,
  }));
  return { runs, every: 15, at: nowIso() };
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
const NAME_RE = /^[\p{L}\p{N}_.\- ]{2,24}$/u;
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

/* ------------------------------------------------------------------ จัดการเรื่อง (ผู้ดูแล) */
function seriesInput(b, partial) {
  const out = {};
  if (!partial || b.name !== undefined) { out.name = clean(b.name, 80); if (!out.name) fail(400, "ใส่ชื่อเรื่อง"); }
  if (!partial || b.url !== undefined) out.url = validUrl(b.url);
  if (b.contains !== undefined) out.contains = clean(b.contains, 100);
  if (b.interval !== undefined) { const iv = +b.interval; if (!INTERVALS.includes(iv)) fail(400, "ความถี่ไม่ถูกต้อง"); out.interval = iv; }
  if (b.enabled !== undefined) out.enabled = !!b.enabled;
  return out;
}
async function addSeries(env, user, b, viaRequest) {
  const inp = seriesInput(b, false);
  const item = { id: newId(8), name: inp.name, url: inp.url, contains: inp.contains || "", enabled: true,
    interval: inp.interval || 30, by: viaRequest ? viaRequest + " (อนุมัติโดย " + user.name + ")" : user.name, added: nowIso() };
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
    changed = s;
    return list;
  });
  const what = Object.keys(inp).map((k) => k === "enabled" ? (inp.enabled ? "เปิด" : "ปิด") : k).join(", ");
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
    result = await addSeries(env, user, { name: b.name || r.name, url: r.url, contains: b.contains, interval: b.interval }, r.from);
  } else {
    await audit(env, user, "request.reject", r.name + " (จาก " + r.from + ")");
  }
  const fresh = (await kvGet(env, "requests", [])).filter((x) => x.id !== id);
  await env.KV.put("requests", JSON.stringify(fresh));
  return result;
}

/* ------------------------------------------------------------------ ตั้งค่า (เจ้าของ) */
async function setConfig(env, user, b) {
  const d = b.digest || {};
  const hour = Math.max(0, Math.min(23, parseInt(d.hour, 10) || 0));
  const next = await mutateFile(env, "config.json", {}, "config by " + user.name, (c) => {
    c.digest = { enabled: !!d.enabled, hour, send_empty: !!d.send_empty };
    if (c.tz_offset === undefined) c.tz_offset = 7;
    return c;
  });
  await audit(env, user, "config", "สรุปประจำวัน " + (d.enabled ? "เปิด " + hour + ":00" : "ปิด"));
  return { ok: true, config: { tz_offset: next.tz_offset, digest: next.digest } };
}

/* ------------------------------------------------------------------ ดูตัวอย่างลิงก์ */
const CH_RE = /(?:chapter|chap|ch|episode|ep|ตอนที่|ตอน|บทที่|บท)[\s._\-/:]*(\d+(?:[.\-]\d+)?)/i;
const LEAD_RE = /^\s*(?:第\s*)?(\d+(?:\.\d+)?)/;
const TAIL_RE = /[/\-_](\d+(?:[.\-]\d+)?)\/?$/;
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

  let title = "", ogTitle = "", ogImage = "", cur = null;
  const links = [];
  await new HTMLRewriter()
    .on("meta", { element(e) {
      const p = (e.getAttribute("property") || e.getAttribute("name") || "").toLowerCase();
      const c = e.getAttribute("content");
      if (!c) return;
      if (p === "og:title" && !ogTitle) ogTitle = c;
      if ((p === "og:image" || p === "twitter:image") && !ogImage) ogImage = c;
    } })
    .on("title", { text(t) { if (title.length < 200) title += t.text; } })
    .on("a", {
      element(e) { if (links.length < 4000) { cur = { href: e.getAttribute("href") || "", text: "" }; links.push(cur); } else cur = null; },
      text(t) { if (cur && cur.text.length < 120) cur.text += t.text; },
    })
    .transform(res).arrayBuffer();

  let best = null, count = 0;
  for (const a of links) {
    const href = a.href.trim();
    if (!href || /^(#|javascript:|mailto:)/i.test(href)) continue;
    const text = decodeEntities(a.text).replace(/\s+/g, " ").trim();
    const m = CH_RE.exec(safeDecode(href)) || CH_RE.exec(text) || TAIL_RE.exec(href.split("?")[0]);
    const n = m ? toNum(m[1]) : null;
    if (n == null || n > 100000) continue;
    count++;
    if (!best || n > best.number) {
      let abs = href; try { abs = new URL(href, url).href; } catch (e) { /* คงไว้ */ }
      best = { number: n, url: abs, title: text.slice(0, 80) };
    }
  }
  let cover = "";
  if (ogImage) { try { const c = new URL(decodeEntities(ogImage), url); if (/^https?:$/.test(c.protocol)) cover = c.href; } catch (e) { /* ไม่มีปก */ } }
  const name = clean(decodeEntities(ogTitle || title), 80);
  if (!best) return { host, name, cover, label: null, count: 0, kind: "html",
    warn: "ไม่พบลิงก์ตอนในหน้านี้ — อาจต้องใช้หน้ารายการตอน หรือเว็บโหลดตอนด้วย JavaScript" };
  return { host, name, cover, label: labelFor(best.number, best.title), chapterTitle: best.title, chapterUrl: best.url, count, kind: "html" };
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

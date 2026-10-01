#!/usr/bin/env python3
"""ตรวจตอนใหม่ของการ์ตูนจากหลายเว็บ แล้วส่งแจ้งเตือนเข้า Discord (ใช้ไลบรารีมาตรฐานล้วน)

ตัวแปรสภาพแวดล้อม (ตั้งโดย workflow):
  DISCORD_WEBHOOK_URL  ที่อยู่ Webhook (Secret)
  ONLY=<id>            ตรวจเฉพาะเรื่องนี้ ข้ามเวลารอบ
  FORCE=1              ตรวจทุกเรื่องทันที ข้ามเวลารอบ (ยังเคารพการพักเมื่อเว็บบล็อก)
  TEST=1               ส่งข้อความทดสอบเข้า Discord แล้วจบ
"""
import datetime
import html as htmllib
import ipaddress
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
NUM = r"(\d+(?:[.\-]\d+)?)"
CH_RE = re.compile(r"(?:chapter|chap|ch|episode|ep|ตอนที่|ตอน|บทที่|บท)[\s._\-/:]*" + NUM, re.I)
LEAD_RE = re.compile(r"^\s*(?:第\s*)?(\d+(?:\.\d+)?)")
TAIL_RE = re.compile(r"[/\-_]" + NUM + r"/?$")
FAIL_NOTIFY_AT = 6
DEFAULT_INTERVAL_MIN = 30   # ตรวจแต่ละเรื่องทุกกี่นาที (ตั้งต่อเรื่องได้ด้วยฟิลด์ interval)
HISTORY_KEEP = 8


# ---------- ความปลอดภัย: กันไม่ให้ระบบถูกใช้ยิงเข้าเครือข่ายภายใน ----------
def safe_url(url):
    """อนุญาตเฉพาะ http/https ที่ชี้ไป IP สาธารณะ"""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    if os.environ.get("ALLOW_LOCAL") == "1":
        return True
    try:
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
    except (socket.gaierror, ValueError):
        return False
    return True


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_url(newurl):
            raise urllib.error.URLError("redirect ไปยังที่อยู่ที่ไม่อนุญาต")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(SafeRedirect)


class Blocked(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__("เว็บปฏิเสธ (HTTP %s) — ระบบจะพักตรวจเว็บนี้ชั่วคราว" % code)
        self.code, self.retry_after = code, retry_after


class NotModified(Exception):
    pass


def fetch(url, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", cache=None):
    """ดึงหน้าแบบสุภาพ: ใช้ ETag/Last-Modified (ถ้าไม่เปลี่ยนเว็บตอบ 304 แทบไม่เสียทรัพยากร)"""
    if not safe_url(url):
        raise RuntimeError("ลิงก์นี้ไม่อนุญาต (ต้องเป็น http/https สาธารณะ)")
    h = {"User-Agent": UA, "Accept": accept, "Accept-Language": "th,en;q=0.8",
         "Accept-Encoding": "identity"}
    if cache:
        if cache.get("etag"):
            h["If-None-Match"] = cache["etag"]
        if cache.get("lm"):
            h["If-Modified-Since"] = cache["lm"]
    try:
        with OPENER.open(urllib.request.Request(url, headers=h), timeout=30) as r:
            data, enc = r.read(3_000_000), r.headers.get_content_charset() or "utf-8"
            if cache is not None:
                cache["etag"], cache["lm"] = r.headers.get("ETag"), r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            raise NotModified()
        if e.code in (401, 403, 429, 503):
            ra = e.headers.get("Retry-After")
            raise Blocked(e.code, int(ra) if ra and ra.isdigit() else None)
        raise
    return data.decode(enc, errors="replace")


# ---------- อ่านหน้าเว็บ ----------
class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, d):
        if self._href is not None:
            self._text.append(d)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def to_num(s):
    try:
        return float(s.replace("-", "."))
    except ValueError:
        return None


def fmt(n):
    return str(int(n)) if float(n).is_integer() else str(n)


def label_for(number, text):
    """เลขตอนที่แสดงให้คนอ่าน (บางเว็บเลขเรียงภายในไม่ตรงกับเลขตอนจริง ให้ดูจากชื่อตอนก่อน)"""
    m = CH_RE.search(text or "") or LEAD_RE.search(text or "")
    n = to_num(m.group(1)) if m else None
    return fmt(n) if n is not None else fmt(number)


def good_img(url, base):
    if not url:
        return ""
    u = urllib.parse.urljoin(base, htmllib.unescape(url.strip()))
    return u if urllib.parse.urlparse(u).scheme in ("http", "https") and len(u) < 600 else ""


def og_image(text, base):
    for m in re.finditer(r"<meta\b[^>]*>", text[:200000], re.I):
        tag = m.group(0)
        if re.search(r"""(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["']""", tag, re.I):
            c = re.search(r"""content\s*=\s*["']([^"']+)["']""", tag, re.I)
            if c:
                return good_img(c.group(1), base)
    return ""


def latest_feed(text):
    best = None
    for blk in re.findall(r"<(?:item|entry)[ >].*?</(?:item|entry)>", text, re.S):
        t = re.search(r"<title[^>]*>(.*?)</title>", blk, re.S)
        l = re.search(r"""<link[^>]*?(?:href=["']([^"']+)|>([^<]+)</link>)""", blk, re.S)
        title = re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1)).strip() if t else ""
        link = (l.group(1) or l.group(2)).strip() if l else ""
        m = CH_RE.search(title) or CH_RE.search(link)
        n = to_num(m.group(1)) if m else None
        if n is not None and (best is None or n > best["number"]):
            best = {"number": n, "url": link, "title": htmllib.unescape(title)[:80]}
    if not best:
        raise RuntimeError("ฟีดนี้ไม่มีตอนที่อ่านเลขได้")
    best["label"] = label_for(best["number"], best["title"])
    return best


def latest_generic(url, contains="", cache=None):
    """หาตอนที่เลขมากที่สุด: รองรับทั้งฟีด RSS/Atom (เบาที่สุด) และหน้าเว็บทั่วไป"""
    text = fetch(url, cache=cache)
    if re.match(r"\s*(<\?xml[^>]*>\s*)?<(rss|feed)\b", text):
        return latest_feed(text)
    p = Links()
    p.feed(text)
    best = None
    for href, t in p.links:
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if contains and contains not in href:
            continue
        m = CH_RE.search(urllib.parse.unquote(href)) or CH_RE.search(t) or TAIL_RE.search(href.split("?")[0])
        n = to_num(m.group(1)) if m else None
        if n is None or n > 100000:
            continue
        if best is None or n > best["number"]:
            best = {"number": n, "url": urllib.parse.urljoin(url, href), "title": t[:80]}
    if not best:
        raise RuntimeError("ไม่พบลิงก์ตอนในหน้านี้ (ลองใส่ช่อง 'ลิงก์ต้องมีคำว่า')")
    best["label"] = label_for(best["number"], best["title"])
    best["cover"] = og_image(text, url)
    return best


def latest_mangadex(url, need_cover):
    m = re.search(r"mangadex\.org/title/([0-9a-f\-]{36})", url)
    mid = m.group(1)
    api = ("https://api.mangadex.org/manga/%s/feed?limit=1&order[chapter]=desc"
           "&translatedLanguage[]=th&translatedLanguage[]=en" % mid)
    d = json.loads(fetch(api, "application/json"))["data"]
    if not d:
        raise RuntimeError("MangaDex ยังไม่มีตอนภาษาไทย/อังกฤษ")
    a = d[0]["attributes"]
    n = to_num(a.get("chapter") or "0") or 0
    out = {"number": n, "url": "https://mangadex.org/chapter/" + d[0]["id"],
           "title": a.get("title") or "", "label": fmt(n)}
    if need_cover:
        try:
            info = json.loads(fetch("https://api.mangadex.org/manga/%s?includes[]=cover_art" % mid,
                                    "application/json"))["data"]
            for rel in info.get("relationships", []):
                if rel.get("type") == "cover_art" and rel.get("attributes", {}).get("fileName"):
                    out["cover"] = "https://uploads.mangadex.org/covers/%s/%s.256.jpg" % (
                        mid, rel["attributes"]["fileName"])
        except Exception as e:  # ปกเป็นของแถม พลาดได้
            print("cover fail", e, file=sys.stderr)
    return out


def latest(s, cache, need_cover):
    if "mangadex.org/title/" in s["url"]:
        return latest_mangadex(s["url"], need_cover)
    return latest_generic(s["url"], s.get("contains", ""), cache)


# ---------- Discord ----------
def discord(webhook, embeds, content=None):
    for i in range(0, len(embeds), 10):
        body = json.dumps({"content": content, "embeds": embeds[i:i + 10]}).encode()
        req = urllib.request.Request(webhook, data=body, headers={
            "Content-Type": "application/json", "User-Agent": "manga-notifier"})
        urllib.request.urlopen(req, timeout=30).read()


def new_chapter_embed(s, st, now):
    prev = st.get("prev_label")
    desc = []
    if st.get("title"):
        desc.append(st["title"])
    if prev:
        desc.append("ตอนก่อนหน้า **%s** → ตอนใหม่ **%s**" % (prev, st["label"]))
    e = {"title": "📖 %s — ตอนที่ %s ออกแล้ว!" % (s["name"], st["label"]),
         "description": "\n".join(desc), "url": st.get("url") or s["url"], "color": 0x5865F2,
         "footer": {"text": urllib.parse.urlparse(s["url"]).netloc}, "timestamp": now.isoformat()}
    if st.get("cover"):
        e["thumbnail"] = {"url": st["cover"]}
    return e


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if os.environ.get("TEST") == "1":
        if not webhook:
            print("ไม่พบ DISCORD_WEBHOOK_URL — ตรวจชื่อ Secret ให้ตรง", file=sys.stderr)
            sys.exit(1)
        discord(webhook, [{"title": "✅ ทดสอบแจ้งเตือนสำเร็จ",
                           "description": "ถ้าเห็นข้อความนี้ แปลว่า Discord เชื่อมกับระบบแล้ว",
                           "color": 0x2E9E5B, "timestamp": now.isoformat()}])
        print("ส่งข้อความทดสอบแล้ว")
        return

    series = json.load(open("series.json", encoding="utf-8"))
    try:
        state = json.load(open("state.json", encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    only = os.environ.get("ONLY", "").strip()
    force = os.environ.get("FORCE") == "1" or bool(only)
    embeds, errors = [], []

    last_hit = {}   # โดเมน -> เวลาที่ยิงล่าสุด (เว้นระยะระหว่างคำขอ)
    order = list(series)
    random.shuffle(order)
    for s in order:
        if not s.get("enabled", True):
            continue
        if only and s["id"] != only:
            continue
        st = state.setdefault(s["id"], {})
        if st.get("blocked_until") and now.isoformat() < st["blocked_until"] and not only:
            continue                                   # กำลังพักเพราะเว็บบล็อก
        every = int(s.get("interval", DEFAULT_INTERVAL_MIN))
        if not force and st.get("tried") and \
                (now - datetime.datetime.fromisoformat(st["tried"])).total_seconds() < every * 60 - 120:
            continue                                   # ยังไม่ถึงรอบของเรื่องนี้
        dom = urllib.parse.urlparse(s["url"]).netloc
        wait = last_hit.get(dom, 0) + random.uniform(4, 10) - time.time()
        if wait > 0 and not only:
            time.sleep(wait)
        last_hit[dom] = time.time()
        st["tried"] = now.isoformat(timespec="seconds")
        cache = st.setdefault("cache", {})
        need_cover = not st.get("cover")
        if need_cover:
            cache.clear()          # ยังไม่มีปก: ห้ามใช้ 304 ไม่งั้นไม่เคยได้หน้ามาอ่านปก
        try:
            cur = latest(s, cache, need_cover)
        except NotModified:
            st["fails"], st["checked"] = 0, now.isoformat(timespec="seconds")
            continue
        except Blocked as e:
            st["blocks"] = st.get("blocks", 0) + 1
            hrs = min(24, 2 ** (st["blocks"] - 1))      # พัก 1,2,4,8,16,24 ชม.
            secs = max(hrs * 3600, e.retry_after or 0)
            st["blocked_until"] = (now + datetime.timedelta(seconds=secs)).isoformat(timespec="seconds")
            st["error"] = str(e)
            print("BLOCKED", s["name"], e, file=sys.stderr)
            if st["blocks"] == 1:
                errors.append({"title": "⚠️ %s ถูกเว็บปฏิเสธ (HTTP %s)" % (s["name"], e.code),
                               "description": "ระบบพักตรวจเว็บนี้ %d ชม. แล้วลองใหม่เอง" % hrs,
                               "url": s["url"], "color": 0xE67E22})
            continue
        except Exception as e:
            st["fails"] = st.get("fails", 0) + 1
            st["error"] = str(e)[:200]
            print("FAIL", s["name"], e, file=sys.stderr)
            if st["fails"] == FAIL_NOTIFY_AT:
                errors.append({"title": "⚠️ ตรวจ %s ไม่ได้ติดต่อกันหลายครั้ง" % s["name"],
                               "description": st["error"], "url": s["url"], "color": 0xE67E22})
            continue

        st["blocks"], st["blocked_until"] = 0, ""
        st["fails"], st["error"] = 0, ""
        st["checked"] = now.isoformat(timespec="seconds")
        if cur.get("cover"):
            st["cover"] = cur["cover"]
        old = st.get("number")
        if old is None:
            print("เริ่มติดตาม", s["name"], "ตอนล่าสุด", cur["label"])
            st.update(number=cur["number"], label=cur["label"], url=cur["url"], title=cur["title"],
                      first_label=cur["label"], since=now.isoformat(timespec="seconds"))
        elif cur["number"] > old:
            prev_label = st.get("label") or fmt(old)
            st.update(prev_number=old, prev_label=prev_label, number=cur["number"], label=cur["label"],
                      url=cur["url"], title=cur["title"], updated=now.isoformat(timespec="seconds"))
            st["history"] = ([{"prev": prev_label, "label": cur["label"], "title": cur["title"],
                               "url": cur["url"], "at": st["updated"]}] + st.get("history", []))[:HISTORY_KEEP]
            embeds.append(new_chapter_embed(s, st, now))
        elif cur["number"] == old and st.get("label") != cur["label"]:
            st["label"] = cur["label"]                  # ปรับป้ายเลขตอนให้ตรงสูตรใหม่

    # ล้างสถานะของเรื่องที่ถูกลบออกจากรายการ
    ids = {s["id"] for s in series}
    for k in [k for k in state if k not in ids and k != "_meta"]:
        del state[k]
    state["_meta"] = {"heartbeat": now.strftime("%Y-%m-%d")}  # ให้มี commit วันละครั้ง กัน GitHub ปิด schedule

    json.dump(state, open("state.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    msgs = embeds + errors
    if msgs:
        if webhook:
            discord(webhook, msgs)
        else:
            print("ไม่มี DISCORD_WEBHOOK_URL — แจ้งเตือนที่จะส่ง:", json.dumps(msgs, ensure_ascii=False))


if __name__ == "__main__":
    main()

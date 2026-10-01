#!/usr/bin/env python3
"""ตรวจตอนใหม่ของการ์ตูนจากหลายเว็บ แล้วส่งแจ้งเตือนเข้า Discord (ใช้ไลบรารีมาตรฐานล้วน)"""
import json, os, re, sys, time, random, datetime, urllib.request, urllib.parse, urllib.error
from html.parser import HTMLParser

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
NUM = r"(\d+(?:[.\-]\d+)?)"
CH_RE = re.compile(r"(?:chapter|chap|ch|episode|ep|ตอนที่|ตอน|บทที่|บท)[\s._\-/:]*" + NUM, re.I)
TAIL_RE = re.compile(r"[/\-_]" + NUM + r"/?$")
FAIL_NOTIFY_AT = 6
DEFAULT_INTERVAL_MIN = 30   # ตรวจแต่ละเรื่องทุกกี่นาที (ตั้งต่อเรื่องได้ด้วยฟิลด์ interval)


class Blocked(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__("เว็บปฏิเสธ (HTTP %s) — ระบบจะพักตรวจเว็บนี้ชั่วคราว" % code)
        self.code, self.retry_after = code, retry_after


class NotModified(Exception):
    pass


def fetch(url, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", cache=None):
    """ดึงหน้าแบบสุภาพ: ใช้ ETag/Last-Modified (ถ้าไม่เปลี่ยนเว็บตอบ 304 แทบไม่เสียทรัพยากร)"""
    h = {"User-Agent": UA, "Accept": accept, "Accept-Language": "th,en;q=0.8",
         "Accept-Encoding": "identity"}
    if cache:
        if cache.get("etag"):
            h["If-None-Match"] = cache["etag"]
        if cache.get("lm"):
            h["If-Modified-Since"] = cache["lm"]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
            data, enc = r.read(), r.headers.get_content_charset() or "utf-8"
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


def latest_feed(text):
    best = None
    for blk in re.findall(r"<(?:item|entry)[ >].*?</(?:item|entry)>", text, re.S):
        t = re.search(r"<title[^>]*>(.*?)</title>", blk, re.S)
        l = re.search(r"<link[^>]*?(?:href=[\"']([^\"']+)|>([^<]+)</link>)", blk, re.S)
        title = re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1)).strip() if t else ""
        link = (l.group(1) or l.group(2)).strip() if l else ""
        m = CH_RE.search(title) or CH_RE.search(link)
        n = to_num(m.group(1)) if m else None
        if n is not None and (best is None or n > best["number"]):
            best = {"number": n, "url": link, "title": title[:80]}
    if not best:
        raise RuntimeError("ฟีดนี้ไม่มีตอนที่อ่านเลขได้")
    return best


def latest_generic(url, contains="", cache=None):
    """หาตอนที่เลขมากที่สุด: รองรับทั้งฟีด RSS/Atom (เบาที่สุด) และหน้าเว็บทั่วไป"""
    text = fetch(url, cache=cache)
    if re.match(r"\s*(<\?xml[^>]*>\s*)?<(rss|feed)\b", text):
        return latest_feed(text)
    p = Links()
    p.feed(text)
    best = None
    for href, text in p.links:
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if contains and contains not in href:
            continue
        m = CH_RE.search(urllib.parse.unquote(href)) or CH_RE.search(text) or TAIL_RE.search(href.split("?")[0])
        n = to_num(m.group(1)) if m else None
        if n is None or n > 100000:
            continue
        if best is None or n > best["number"]:
            best = {"number": n, "url": urllib.parse.urljoin(url, href), "title": text[:80]}
    if not best:
        raise RuntimeError("ไม่พบลิงก์ตอนในหน้านี้ (ลองใส่ช่อง 'ลิงก์ต้องมีคำว่า')")
    return best


def latest_mangadex(url):
    m = re.search(r"mangadex\.org/title/([0-9a-f\-]{36})", url)
    api = ("https://api.mangadex.org/manga/%s/feed?limit=1&order[chapter]=desc"
           "&translatedLanguage[]=th&translatedLanguage[]=en" % m.group(1))
    d = json.loads(fetch(api, "application/json"))["data"]
    if not d:
        raise RuntimeError("MangaDex ยังไม่มีตอนภาษาไทย/อังกฤษ")
    a = d[0]["attributes"]
    n = to_num(a.get("chapter") or "0") or 0
    return {"number": n, "url": "https://mangadex.org/chapter/" + d[0]["id"],
            "title": a.get("title") or ""}


def latest(s, cache):
    if "mangadex.org/title/" in s["url"]:
        return latest_mangadex(s["url"])
    return latest_generic(s["url"], s.get("contains", ""), cache)


def fmt(n):
    return str(int(n)) if float(n).is_integer() else str(n)


def discord(webhook, embeds, content=None):
    for i in range(0, len(embeds), 10):
        body = json.dumps({"content": content, "embeds": embeds[i:i + 10]}).encode()
        req = urllib.request.Request(webhook, data=body, headers={
            "Content-Type": "application/json", "User-Agent": "manga-notifier"})
        urllib.request.urlopen(req, timeout=30).read()


def main():
    series = json.load(open("series.json", encoding="utf-8"))
    try:
        state = json.load(open("state.json", encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    embeds, errors = [], []
    now = datetime.datetime.now(datetime.timezone.utc)

    last_hit = {}   # โดเมน -> เวลาที่ยิงล่าสุด (เว้นระยะระหว่างคำขอ)
    random.shuffle(series)
    for s in series:
        if not s.get("enabled", True):
            continue
        st = state.setdefault(s["id"], {})
        if st.get("blocked_until") and now.isoformat() < st["blocked_until"]:
            continue                                   # กำลังพักเพราะเว็บบล็อก
        every = int(s.get("interval", DEFAULT_INTERVAL_MIN))
        if st.get("tried") and (now - datetime.datetime.fromisoformat(st["tried"])).total_seconds() < every * 60 - 120:
            continue                                   # ยังไม่ถึงรอบของเรื่องนี้
        dom = urllib.parse.urlparse(s["url"]).netloc
        wait = last_hit.get(dom, 0) + random.uniform(4, 10) - time.time()
        if wait > 0:
            time.sleep(wait)
        last_hit[dom] = time.time()
        st["tried"] = now.isoformat(timespec="seconds")
        try:
            cache = st.setdefault("cache", {})
            cur = latest(s, cache)
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
        old = st.get("number")
        if old is None:
            print("เริ่มติดตาม", s["name"], "ตอนล่าสุด", fmt(cur["number"]))
        elif cur["number"] > old:
            embeds.append({"title": "📖 %s — ตอนที่ %s ออกแล้ว!" % (s["name"], fmt(cur["number"])),
                           "description": cur["title"], "url": cur["url"], "color": 0x5865F2})
        if old is None or cur["number"] >= old:
            st.update(number=cur["number"], url=cur["url"], title=cur["title"])
            if old is None or cur["number"] > old:
                st["updated"] = now.isoformat(timespec="seconds")

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

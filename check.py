#!/usr/bin/env python3
"""ตรวจตอนใหม่ของการ์ตูนจากหลายเว็บ แล้วส่งแจ้งเตือนเข้า Discord (ใช้ไลบรารีมาตรฐานล้วน)

ตัวแปรสภาพแวดล้อม (ตั้งโดย workflow):
  DISCORD_WEBHOOK_URL  ที่อยู่ Webhook (Secret)
  ONLY=<id>            ตรวจเฉพาะเรื่องนี้ ข้ามเวลารอบ
  FORCE=1              ตรวจทุกเรื่องทันที ข้ามเวลารอบ (ยังเคารพการพักเมื่อเว็บบล็อก)
  TEST=1               ส่งข้อความทดสอบเข้า Discord แล้วจบ
  RESEND=<log id>      ส่งข้อความเดิมจากบันทึกซ้ำ แล้วจบ
  TEST_SERIES=<id>     ตรวจเรื่องนี้แล้วส่งข้อความทดสอบบอกตอนล่าสุด
  AUTO=1               รอบที่ Cloudflare สั่ง (ทุก 5 นาที เมื่อมีเรื่องถึงเวลา)
  ALERT=token:<เวลา>   Cloudflare แจ้งว่า GitHub Token ใกล้หมดอายุ
  EVENT=<ชนิด>         github.event_name (schedule = รอบสำรองของ GitHub)

ไฟล์ข้อมูล:
  series.json      รายการเรื่อง (แก้ผ่านหน้าเว็บ)
  state.json       สถานะล่าสุดของแต่ละเรื่อง (ระบบเขียน)
  notify_log.json  บันทึกทุกข้อความที่ส่งเข้า Discord พร้อมผลสำเร็จ/ล้มเหลว (ระบบเขียน)
  public_log.json  บันทึกฉบับย่อสำหรับหน้าเว็บ (ไม่มี Discord ID) (ระบบเขียน)
  * state.json / notify_log.json / public_log.json เก็บใน branch "data" (ไม่ทำให้หน้าเว็บ build ใหม่ทุกรอบ)
  subs.json        ใครติดตามเรื่องไหน + Discord ID สำหรับแท็ก (หน้าเว็บเขียน)
  config.json      ตั้งค่าสรุปประจำวัน ฯลฯ (หน้าเว็บเขียน)
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
CH_RE = re.compile(r"(?:(?<![a-z])(?:chapter|chap|ch|episode|ep)|ตอนที่|ตอน|บทที่|บท)[\s._\-/:]*" + NUM, re.I)
LEAD_RE = re.compile(r"^\s*(?:第\s*)?(\d+(?:\.\d+)?)")
TAIL_RE = re.compile(r"[/\-_]" + NUM + r"/?$")
FAIL_NOTIFY_AT = 6
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
    """เก็บลิงก์ (href, ข้อความ, ตำแหน่งในข้อความทั้งหน้า) + ข้อความทั้งหน้า + data-num (ธีม WordPress มังงะ)"""

    def __init__(self):
        super().__init__()
        self.links, self._href, self._text, self._pos = [], None, [], 0
        self.plain, self.plen, self.marks, self._skip = [], 0, [], 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        if d.get("data-num"):
            self.marks.append((self.plen, d["data-num"]))
        if tag == "a":
            self._href, self._text, self._pos = d.get("href"), [], self.plen
        elif tag in ("li", "div", "p", "br", "tr", "span"):
            self.handle_data(" ")

    def handle_data(self, d):
        if self._skip:
            return
        self.plain.append(d)
        self.plen += len(d)
        if self._href is not None:
            self._text.append(d)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split()), self._pos))
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
    """หาปกเรื่อง: og:image ก่อน (ถ้าไม่ใช่โลโก้เว็บ) ไม่งั้นเดาจาก <img> ในหน้า"""
    for m in re.finditer(r"<meta\b[^>]*>", text[:200000], re.I):
        tag = m.group(0)
        if re.search(r"""(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["']""", tag, re.I):
            c = re.search(r"""content\s*=\s*["']([^"']+)["']""", tag, re.I)
            if c and not re.search(r"logo|favicon|default", c.group(1), re.I):
                return good_img(c.group(1), base)
    tm = re.search(r"<title[^>]*>(.*?)</title>", text[:30000], re.S | re.I)
    page_title = htmllib.unescape(tm.group(1)) if tm else ""
    best, score = "", 0
    for m in re.finditer(r"<img\b[^>]*>", text[:400000], re.I):
        tag = m.group(0)
        src = re.search(r"""(?:data-original|data-src|src)\s*=\s*["']([^"']+)["']""", tag, re.I)
        if not src or re.search(r"logo|icon|avatar|favicon|\.gif|\.svg", src.group(1), re.I):
            continue
        alt = re.search(r"""alt\s*=\s*["']([^"']*)["']""", tag, re.I)
        a = htmllib.unescape(alt.group(1)).strip() if alt else ""
        sc = 100 if re.search(r"cover|vertical|poster", tag, re.I) else 0
        if len(a) >= 2 and a in page_title:
            sc += len(a)
        if sc > score:
            u = good_img(src.group(1), base)
            if u:
                best, score = u, sc
    return best


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
    best["mode"] = "feed"
    return best


YEAR_RE = re.compile(r"^\s*(?:19|20)\d\d\s*[-./年]")


CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNIT = {"十": 10, "百": 100, "千": 1000}
CN_RE = re.compile(r"第\s*([零〇一二两三四五六七八九十百千]+)\s*(?=[话話章回集]|[\s·.:：、,，]|$)")


def cn_num(s):
    """เลขจีน → ตัวเลข: 二十五 → 25, 十 → 10, 一百零三 → 103"""
    total, cur = 0, 0
    for ch in s:
        if ch in CN_DIGIT:
            cur = CN_DIGIT[ch]
        elif ch in CN_UNIT:
            total += (cur or 1) * CN_UNIT[ch]
            cur = 0
    return total + cur


def text_num(t):
    """เลขตอนจากชื่อลิงก์ เช่น 'ตอนที่ 88', 'Chapter 12', '31 排名第十', '第5话', '第二十五话 调停者' (ไม่นับวันที่)"""
    if not t or YEAR_RE.match(t):
        return None
    m = CH_RE.search(t) or LEAD_RE.search(t)
    n = to_num(m.group(1)) if m else None
    if n is None:
        c = CN_RE.search(t)
        n = float(cn_num(c.group(1))) if c else None
    return n if n is not None and 0 < n < 100000 or n == 0 and m else None


def url_num(href):
    m = CH_RE.search(urllib.parse.unquote(href)) or TAIL_RE.search(href.split("?")[0])
    return to_num(m.group(1)) if m else None


COUNT_RE = re.compile(r"(?:共|全|ทั้งหมด)\s*(\d{1,5})\s*(?:篇正文|话|話|章|集|回|ตอน)")


def _pick_max(cands):
    best = None
    for c in cands:
        if best is None or c[0] > best[0]:
            best = c
    return best


LOCKED_AHEAD = 20   # ตอนล็อก (ไม่มีลิงก์) ต้องอยู่ไม่เกินกี่ตอนจากตอนที่มีลิงก์ กันเลขของเรื่องอื่นปน


def latest_generic(url, contains="", cache=None):
    """หาตอนล่าสุดจากหน้าเว็บ (หรือฟีด RSS/Atom) — ลองตามลำดับ แล้วใช้วิธีแรกที่ได้ผล

    1. text  : ลิงก์ตอนมีเลขตอนในชื่อ (เช่น '31 排名第十') ≥3 ลิงก์ → ใช้เลขจากชื่อ
               (แม่นที่สุด บางเว็บใช้รหัสภายในในลิงก์ที่ไม่เรียงตามตอน เช่น Tencent cid/152887)
    2. word  : ลิงก์/ชื่อมีคำว่า chapter / ep / ตอน ฯลฯ ตามด้วยเลข
    3. count : หน้าบอกจำนวนตอน เช่น '共14篇正文' (เว็บที่โหลดรายการตอนด้วย JavaScript เช่น 快看漫画)
    4. group : ลิงก์ที่ลงท้ายด้วยเลข และมีรูปแบบเดียวกัน ≥3 ลิงก์ (กันลิงก์หลงอย่าง /qa1/90227)
    จากนั้นดู "ตอนล็อก" (ต้องใช้เหรียญ/ล่วงหน้า ไม่มีลิงก์) ในบริเวณรายการตอนเดียวกันด้วย
    """
    text = fetch(url, cache=cache)
    if re.match(r"\s*(<\?xml[^>]*>\s*)?<(rss|feed)\b", text):
        return latest_feed(text)
    p = Links()
    p.feed(text)
    plain = "".join(p.plain)
    links = []
    for href, t, pos in p.links:
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if contains and contains not in href:
            continue
        links.append((href, t, pos))
    if not contains:
        # กันลิงก์ของเรื่องอื่นในหน้า (เช่น แถบ "เรื่องยอดนิยม" ข้าง ๆ): ถ้าลิงก์ตอน ≥3 ลิงก์มีชื่อเรื่องจาก URL อยู่ ใช้เฉพาะพวกนั้น
        segs = [x for x in urllib.parse.urlparse(url).path.split("/") if x]
        slug = urllib.parse.unquote(segs[-1]) if segs else ""
        if len(slug) >= 5:
            own = [l for l in links if slug in urllib.parse.unquote(l[0])]
            if len(own) >= 3:
                links = own

    pick, mode, used = None, None, []
    text_c = [(text_num(t), h, t, pos) for h, t, pos in links if url_num(h) is not None]
    text_c = [c for c in text_c if c[0] is not None]
    if len(text_c) >= 3:
        pick, mode, used = _pick_max(text_c), "text", text_c
    if not pick:
        word_c = []
        for h, t, pos in links:
            m = CH_RE.search(urllib.parse.unquote(h)) or CH_RE.search(t)
            n = to_num(m.group(1)) if m else None
            if n is not None and n < 100000:
                word_c.append((n, h, t, pos))
        if word_c:
            pick, mode, used = _pick_max(word_c), "url", word_c
    if not pick:
        cm = COUNT_RE.search(plain)
        if cm:
            pick, mode = (float(cm.group(1)), url, cm.group(0), 0), "count"
    if not pick:
        groups = {}
        for h, t, pos in links:
            path = h.split("?")[0].split("#")[0]
            m = TAIL_RE.search(path)
            if not m:
                continue
            n = to_num(m.group(1))
            if n is None:
                continue
            key = re.sub(r"\d+", "#", path[:m.start()])          # รูปแบบลิงก์ เช่น /m/chapter-#
            groups.setdefault(key, []).append((n, h, t, pos))
        all_groups = groups
        groups = {k: [c for c in v if c[0] <= 100000] for k, v in groups.items()}   # เลขใหญ่ = รหัสภายใน ไม่ใช่เลขตอน
        big = max(groups.values(), key=len) if groups else []
        if len(big) >= 3:
            pick, mode, used = _pick_max(big), "group", big
            if not CH_RE.search(pick[2]) and text_num(pick[2]) is None:
                # 5. order: ชื่อตอนไม่มีเลข (เช่น '弱肉强食') → นับลำดับตอนในรายการแทน (ลิงก์ไม่ซ้ำ)
                uniq = {urllib.parse.urljoin(url, c[1].split("#")[0]) for c in big}
                pick, mode, used = (float(len(uniq)), pick[1], pick[2], pick[3]), "order", []
    if not pick:
        # 5. order (รหัสตอนเป็นเลขใหญ่ เช่น Tencent cid/152887 และชื่อตอนไม่มีเลข) → นับลำดับตอนในรายการ
        big = max(all_groups.values(), key=len) if all_groups else []
        if len(big) >= 3:
            top = _pick_max(big)
            if not CH_RE.search(top[2]) and text_num(top[2]) is None:
                uniq = {urllib.parse.urljoin(url, c[1].split("#")[0]) for c in big}
                pick, mode = (float(len(uniq)), top[1], top[2].strip(" []【】"), top[3]), "order"
    if not pick:
        raise RuntimeError("ไม่พบรายการตอนในหน้านี้ — เว็บอาจโหลดตอนด้วย JavaScript "
                           "(ลองใช้หน้ารายการตอน, ลิงก์ RSS หรือใส่ช่อง 'ลิงก์ต้องมีคำว่า')")
    n, h, t, _ = pick
    best = {"number": n, "url": urllib.parse.urljoin(url, h), "title": " ".join(t.split()).strip("[]【】 ")[:80], "mode": mode}

    # ตอนล็อก/ล่วงหน้า: อยู่ในรายการตอนแต่ไม่มีลิงก์ (เช่น 'Chapter 33' ที่ต้องใช้เหรียญ)
    if used:
        lo = max(0, min(c[3] for c in used) - 3000)
        hi = max(c[3] for c in used) + 3000
        extra = None
        for m in CH_RE.finditer(plain[lo:hi]):
            x = to_num(m.group(1))
            if x is not None and n < x <= n + LOCKED_AHEAD and (extra is None or x > extra[0]):
                extra = (x, m.group(0))
        for pos, dn in p.marks:
            x = to_num(dn) if re.fullmatch(r"\d+(?:\.\d+)?", dn or "") else None
            if lo <= pos <= hi and x is not None and n < x <= n + LOCKED_AHEAD and (extra is None or x > extra[0]):
                extra = (x, "ตอน " + fmt(x))
        if extra:
            best.update(number=extra[0], url=url, title=" ".join(extra[1].split())[:80] + " (ล็อก/ล่วงหน้า)", locked=True)
            n = extra[0]
    best["label"] = fmt(n) if mode in ("text", "count", "order") or best.get("locked") else label_for(n, best["title"])
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
           "title": a.get("title") or "", "label": fmt(n), "mode": "mangadex"}
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
LOG_KEEP = 150
DISCORD_ID = re.compile(r"^\d{15,22}$")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def post_discord(webhook, payload):
    """ส่ง 1 ข้อความ คืนค่า (สำเร็จไหม, ข้อความ error) — ไม่โยน exception เพื่อไม่ให้รอบตรวจพัง"""
    if not webhook:
        return False, "ไม่ได้ตั้ง Secret DISCORD_WEBHOOK_URL"
    body = json.dumps(payload).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(webhook, data=body, headers={
                "Content-Type": "application/json", "User-Agent": "manga-notifier"})
            urllib.request.urlopen(req, timeout=30).read()
            return True, ""
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:          # Discord ให้รอ
                try:
                    wait = float(json.loads(e.read().decode() or "{}").get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(min(wait, 10))
                continue
            return False, "Discord ตอบ HTTP %s" % e.code
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            return False, str(e)[:150]
    return False, "ส่งไม่สำเร็จ"


class Notifier:
    """ส่งข้อความ + จดบันทึกลง notify_log.json ทุกครั้ง"""

    def __init__(self, webhook, now):
        self.webhook, self.now = webhook, now
        self.log = load_json("notify_log.json", [])
        if not isinstance(self.log, list):
            self.log = []

    def send(self, kind, payload, **meta):
        ok, err = post_discord(self.webhook, payload)
        entry = {"id": "%x%04x" % (int(time.time() * 1000), random.randint(0, 0xFFFF)),
                 "at": self.now.isoformat(timespec="seconds"), "kind": kind,
                 "status": "sent" if ok else "failed", "payload": payload}
        if err:
            entry["error"] = err
        entry.update({k: v for k, v in meta.items() if v not in (None, "", [])})
        self.log.insert(0, entry)
        print(("ส่งแล้ว" if ok else "ส่งไม่สำเร็จ: " + err), kind, meta.get("name", ""))
        return ok

    def retry_failed(self):
        """ส่งซ้ำอัตโนมัติ: ข้อความที่ส่งไม่สำเร็จใน 24 ชม. ลองใหม่ได้ 3 ครั้ง ห่างกันอย่างน้อย 10 นาที"""
        if not self.webhook:
            return
        for e in self.log[:80]:
            if e.get("status") != "failed" or e.get("kind") not in ("new", "problem", "alert", "digest"):
                continue
            if e.get("resent") or e.get("retries", 0) >= 3:
                continue
            try:
                at = datetime.datetime.fromisoformat(e.get("retry_at") or e["at"])
                born = datetime.datetime.fromisoformat(e["at"])
            except (KeyError, ValueError):
                continue
            if (self.now - born).total_seconds() > 86400 or (self.now - at).total_seconds() < 600:
                continue
            ok, err = post_discord(self.webhook, e["payload"])
            e["retries"] = e.get("retries", 0) + 1
            e["retry_at"] = self.now.isoformat(timespec="seconds")
            if ok:
                e["status"], e["late"] = "sent", e["retry_at"]
                e.pop("error", None)
            else:
                e["error"] = err
            print("ส่งซ้ำอัตโนมัติ", e.get("name", e.get("kind")), "สำเร็จ" if ok else err)

    def save(self):
        with open("notify_log.json", "w", encoding="utf-8") as f:
            json.dump(self.log[:LOG_KEEP], f, ensure_ascii=False, indent=1)
        with open("public_log.json", "w", encoding="utf-8") as f:
            json.dump([public_entry(e) for e in self.log[:80]], f, ensure_ascii=False, separators=(",", ":"))


def public_entry(e):
    """ฉบับย่อสำหรับหน้าเว็บ: ไม่มี Discord ID (มีแค่ชื่อคนที่ถูกแท็ก)"""
    em = ((e.get("payload") or {}).get("embeds") or [{}])[0]
    out = {k: e.get(k) for k in ("id", "at", "kind", "status", "error", "sid", "name", "label", "prev", "mentions",
                                 "count", "ref", "resent", "late", "retries") if e.get(k) not in (None, "", [])}
    out["embed"] = {k: v for k, v in {
        "title": em.get("title"), "description": em.get("description"), "url": em.get("url"), "color": em.get("color"),
        "thumb": (em.get("thumbnail") or {}).get("url"), "footer": (em.get("footer") or {}).get("text")}.items() if v is not None}
    return out


def owner_ping(cfg):
    """🚨 แท็กเจ้าของในข้อความปัญหาระบบ (ตั้งในหน้าเว็บ แท็บระบบ)"""
    a = cfg.get("alert") or {}
    did = str(a.get("discord") or "")
    return did if a.get("enabled", True) and DISCORD_ID.match(did) else ""


def alert_payload(cfg, embed):
    p = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    did = owner_ping(cfg)
    if did:
        p["content"] = "🚨 <@%s>" % did
        p["allowed_mentions"]["users"] = [did]
    return p


def subscribers(subs, sid):
    """คืน [(ชื่อ, discord id)] ของคนที่กดติดตามเรื่องนี้และใส่ Discord ID แล้ว"""
    out = []
    for name, u in (subs.get("users") or {}).items():
        did = str(u.get("discord") or "")
        if sid in (u.get("series") or []) and DISCORD_ID.match(did):
            out.append((name, did))
    return out


def test_series_payload(s, st, now, mentions):
    lines = []
    if st.get("label") is not None:
        lines.append("ตอนล่าสุดตอนนี้: **ตอนที่ %s**" % st["label"])
        if st.get("title"):
            lines.append(st["title"])
        if st.get("prev_label") and st.get("updated"):
            lines.append("ออกล่าสุด: ตอน %s → %s" % (st["prev_label"], st["label"]))
    else:
        lines.append("ยังไม่มีข้อมูลตอนล่าสุด")
    if st.get("error"):
        lines.append("⚠️ อ่านเว็บไม่ได้: " + st["error"])
    lines.append("แท็ก: " + (", ".join(n for n, _ in mentions) if mentions else "ยังไม่มีคนติดตาม"))
    e = {"title": "🧪 ทดสอบ — %s" % s["name"], "description": "\n".join(lines),
         "url": st.get("url") or s["url"], "color": 0x9B59B6,
         "footer": {"text": urllib.parse.urlparse(s["url"]).netloc + " · ข้อความทดสอบจากเจ้าของ"},
         "timestamp": now.isoformat()}
    if st.get("cover"):
        e["thumbnail"] = {"url": st["cover"]}
    p = {"embeds": [e], "allowed_mentions": {"parse": [], "users": [d for _, d in mentions]}}
    if mentions:
        p["content"] = " ".join("<@%s>" % d for _, d in mentions)
    return p


def new_chapters(prev, label):
    """📦 ออกหลายตอนพร้อมกัน: 102 → 105 = ['103','104','105'] (เฉพาะเลขจำนวนเต็ม ห่างกัน 2-10 ตอน)"""
    a, b = to_num(str(prev or "")), to_num(str(label or ""))
    if a is None or b is None or not a.is_integer() or not b.is_integer() or not 2 <= b - a <= 10:
        return []
    return [fmt(x) for x in range(int(a) + 1, int(b) + 1)]


def new_chapter_payload(s, st, now, mentions):
    prev = st.get("prev_label")
    many = new_chapters(prev, st["label"])
    desc = []
    if st.get("title"):
        desc.append(st["title"])
    if many:
        desc.append("ตอนใหม่ %d ตอน: **%s**" % (len(many), ", ".join(many)))
    if prev:
        desc.append("ตอนก่อนหน้า **%s** → ตอนล่าสุด **%s**" % (prev, st["label"]))
    title = ("📦 %s — ออกใหม่ %d ตอน! (%s–%s)" % (s["name"], len(many), many[0], many[-1]) if many
             else "📖 %s — ตอนที่ %s ออกแล้ว!" % (s["name"], st["label"]))
    e = {"title": title,
         "description": "\n".join(desc), "url": st.get("url") or s["url"], "color": 0x5865F2,
         "footer": {"text": urllib.parse.urlparse(s["url"]).netloc}, "timestamp": now.isoformat()}
    if st.get("cover"):
        e["thumbnail"] = {"url": st["cover"]}
    p = {"embeds": [e], "allowed_mentions": {"parse": [], "users": [d for _, d in mentions]}}
    if mentions:
        p["content"] = " ".join("<@%s>" % d for _, d in mentions)
    return p


def digest_payload(series, state, now_local, since):
    names = {s["id"]: s["name"] for s in series}
    rows = []
    for sid, st in state.items():
        if sid == "_meta" or sid not in names:
            continue
        for h in st.get("history") or []:
            try:
                at = datetime.datetime.fromisoformat(h["at"])
            except (KeyError, ValueError):
                continue
            if at >= since:
                rows.append((at, names[sid], h))
    rows.sort(key=lambda r: r[0], reverse=True)
    if not rows:
        return None, 0
    lines = ["• **%s** — ตอน %s → **%s**" % (n, h.get("prev") or "?", h["label"]) for _, n, h in rows[:30]]
    if len(rows) > 30:
        lines.append("…และอีก %d รายการ" % (len(rows) - 30))
    return {"embeds": [{"title": "📚 สรุปประจำวัน %s — %d ตอนใหม่" % (now_local.strftime("%d/%m/%Y"), len(rows)),
                        "description": "\n".join(lines), "color": 0xF0B232}]}, len(rows)


def maybe_digest(cfg, series, state, notifier, now):
    d = cfg.get("digest") or {}
    if not d.get("enabled"):
        return
    tz = datetime.timezone(datetime.timedelta(hours=float(cfg.get("tz_offset", 7))))
    local = now.astimezone(tz)
    meta = state.setdefault("_meta", {})
    if local.hour < int(d.get("hour", 21)) or meta.get("digest") == local.strftime("%Y-%m-%d"):
        return
    meta["digest"] = local.strftime("%Y-%m-%d")
    payload, n = digest_payload(series, state, local, now - datetime.timedelta(hours=24))
    if payload is None:
        if d.get("send_empty"):
            notifier.send("digest", {"embeds": [{"title": "📚 สรุปประจำวัน %s" % local.strftime("%d/%m/%Y"),
                                                 "description": "วันนี้ยังไม่มีตอนใหม่", "color": 0xF0B232}]}, count=0)
        return
    notifier.send("digest", payload, count=n)


# ---------- ตารางเวลาตรวจ (ต้องตรงกับ planOf/dueAt ใน worker.js และ index.html) ----------
# โหมด:
#   peak   ⭐ ช่วงออกตอนหลัก: เฝ้าถี่ในช่วงที่ตั้งใน config.peak (ค่าเริ่มต้น 22:50-00:30, 04:50-06:30 ทุก 5 นาที) นอกช่วงทุก 6 ชม.
#   every  ⏱ ทุก ๆ X นาที (sched.min)
#   custom 🕘 กำหนดเอง: sched.slots = ["20:00" (ตรวจครั้งเดียว), "19:50-21:00" (ช่วงเฝ้า)], sched.tz = โซนเวลา (ชม.), sched.off = นอกช่วงทุกกี่นาที
PEAK_DEFAULT = {"windows": ["22:50-00:30", "04:50-06:30"], "every": 5, "off": 360}
SLOT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?:-([01]\d|2[0-3]):([0-5]\d))?$")
DORMANT_DAYS = 30          # 💤 ไม่มีตอนใหม่เกินนี้ → ตรวจห่างลงเอง
DORMANT_BASE = 720         # ตอนหลับ: นอกช่วงอย่างน้อยทุก 12 ชม. (ยังตรวจตอนเริ่มช่วงเฝ้า)
RETRY_FAILS = 3            # อ่านเว็บพลาด → ลองใหม่รอบถัดไป (ทุก ~5 นาที) ได้กี่ครั้ง


def _clamp(v, lo, hi, d):
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return d
    return max(lo, min(hi, v))


def sched_of(s):
    sc = s.get("sched")
    if isinstance(sc, dict) and sc.get("mode") in ("peak", "every", "custom"):
        return sc
    if s.get("times"):                       # ข้อมูลรุ่นเก่า: ตั้งเวลาเอง → กำหนดเอง
        return {"mode": "custom", "slots": s["times"], "off": 1440}
    return {"mode": "peak"}                  # รุ่นเก่า (ทุก ๆ X นาที) และเรื่องใหม่ → ช่วงออกตอนหลัก


def plan_of(s, st, cfg, now):
    sc, c = sched_of(s), cfg or {}
    pk = dict(PEAK_DEFAULT, **(c.get("peak") or {}))
    tz, slots, step, base = float(c.get("tz_offset", 7)), [], 0, 360
    if sc["mode"] == "every":
        base = _clamp(sc.get("min"), 10, 10080, 360)
    elif sc["mode"] == "custom":
        tz = float(sc.get("tz", tz))
        slots, step, base = sc.get("slots") or [], _clamp(sc.get("every"), 5, 60, 5), _clamp(sc.get("off"), 10, 10080, 360)
    else:
        slots, step, base = pk.get("windows") or [], _clamp(pk.get("every"), 5, 60, 5), _clamp(pk.get("off"), 10, 10080, 360)
    wins, pts = [], []
    for x in slots:
        m = SLOT_RE.match(str(x))
        if not m:
            continue
        a = int(m.group(1)) * 60 + int(m.group(2))
        pts.append(a)
        if m.group(3) is not None:
            wins.append((a, int(m.group(3)) * 60 + int(m.group(4))))
    dormant = False
    if s.get("autoslow", True) is not False and st:
        last = st.get("updated") or st.get("since")
        try:
            if last and (now - datetime.datetime.fromisoformat(last)).total_seconds() > DORMANT_DAYS * 86400:
                dormant, step, base = True, 0, max(base, DORMANT_BASE)
        except ValueError:
            pass
    return {"tz": tz, "wins": wins, "pts": pts, "step": step if wins else 0, "base": base, "dormant": dormant}


def due_at(p, tried_ts, fails, now_ts):
    """tried_ts/now_ts เป็นวินาที epoch (tried_ts=0 = ยังไม่เคยตรวจ)"""
    if not tried_ts:
        return True
    el = (now_ts - tried_ts) / 60
    if 1 <= fails <= RETRY_FAILS and el >= 4:
        return True                                         # เพิ่งอ่านพลาด → ลองใหม่เร็ว ๆ
    if el >= p["base"] - 2:
        return True                                         # ครบรอบปกติ
    lm = now_ts / 60 + p["tz"] * 60                         # นาทีตามเวลาท้องถิ่นของตาราง
    mod = int(lm // 1) % 1440
    if p["step"] and el >= p["step"] - 1.5:
        for a, b in p["wins"]:
            if (a <= mod <= b) if a <= b else (mod >= a or mod <= b):
                return True                                 # อยู่ในช่วงเฝ้า
    for pt in p["pts"]:                                     # เวลาที่ตั้ง/เวลาเริ่มช่วงเฝ้า ที่ผ่านมาแล้วแต่ยังไม่ได้ตรวจ
        occ = (int(lm // 1) - (mod - pt) % 1440 - p["tz"] * 60) * 60
        if tried_ts < occ:
            return True
    return False


def is_due(s, st, now, cfg):
    tried = st.get("tried")
    try:
        t = datetime.datetime.fromisoformat(tried).timestamp() if tried else 0
    except ValueError:
        t = 0
    return due_at(plan_of(s, st, cfg, now), t, int(st.get("fails") or 0), now.timestamp())


def parse_iso(v):
    try:
        d = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def system_alerts(cfg, series, state, notifier, now, event, alert):
    """🚨 แจ้งเจ้าของเมื่อระบบมีปัญหา (กันส่งซ้ำด้วย _meta)"""
    meta = state.setdefault("_meta", {})
    # Cloudflare แจ้งว่า Token ใกล้หมดอายุ (Worker ส่งมาวันละครั้ง)
    if alert.startswith("token:"):
        exp = parse_iso(alert[6:])
        if exp:
            days = max(0, (exp - now).total_seconds() / 86400)
            notifier.send("alert", alert_payload(cfg, {
                "title": "⏳ GitHub Token จะหมดอายุในอีก %s วัน" % (int(days) if days >= 1 else "ไม่ถึง 1"),
                "description": "หมดอายุ %s (เวลาไทย)\nถ้าหมดอายุ ระบบจะหยุดตรวจทั้งหมด\n"
                               "วิธีต่ออายุ: GitHub → Settings → Developer settings → Fine-grained tokens → "
                               "กด Regenerate → ไปวางแทนค่า GITHUB_TOKEN ใน Cloudflare (Settings → Variables and Secrets)"
                               % exp.astimezone(datetime.timezone(datetime.timedelta(hours=7))).strftime("%d/%m/%Y %H:%M"),
                "color": 0xE67E22}), name="GitHub Token")
    # รอบสำรองของ GitHub เจอเรื่องที่ควรตรวจไปนานแล้วแต่ไม่มีใครสั่ง → Cloudflare Cron ไม่ทำงาน
    if event == "schedule" and meta.get("last_auto"):
        past = now - datetime.timedelta(minutes=20)
        late = [s for s in series if s.get("enabled", True)
                and not (state.get(s["id"], {}).get("blocked_until", "") > now.isoformat())
                and is_due(s, state.get(s["id"], {}), past, cfg)]
        last = parse_iso(meta.get("alert_cron"))
        if late and (not last or (now - last).total_seconds() > 12 * 3600):
            meta["alert_cron"] = now.isoformat(timespec="seconds")
            la = parse_iso(meta["last_auto"])
            notifier.send("alert", alert_payload(cfg, {
                "title": "⚠️ Cloudflare ไม่ได้สั่งตรวจตามเวลา",
                "description": "สั่งตรวจครั้งล่าสุด %s ชม.ที่แล้ว ตอนนี้ใช้รอบสำรองของ GitHub (ทุก ~30 นาที) ไปก่อน\n"
                               "สาเหตุที่พบบ่อย: GitHub Token หมดอายุ หรือ Cron Trigger ใน Cloudflare ถูกลบ"
                               % (round((now - la).total_seconds() / 3600, 1) if la else "?"),
                "color": 0xE67E22}), name="Cloudflare")


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    notifier = Notifier(webhook, now)

    if os.environ.get("TEST") == "1":
        ok = notifier.send("test", {"embeds": [{
            "title": "✅ ทดสอบแจ้งเตือนสำเร็จ",
            "description": "ถ้าเห็นข้อความนี้ แปลว่า Discord เชื่อมกับระบบแล้ว",
            "color": 0x2E9E5B, "timestamp": now.isoformat()}]})
        notifier.save()          # ผลสำเร็จ/ล้มเหลวอยู่ใน notify_log.json (หน้าเว็บอ่านจากตรงนั้น)
        if not ok:
            print("ส่งไม่สำเร็จ — ตรวจ Secret DISCORD_WEBHOOK_URL", file=sys.stderr)
        return

    resend = os.environ.get("RESEND", "").strip()
    if resend:
        orig = next((e for e in notifier.log if e.get("id") == resend), None)
        if not orig:
            print("ไม่พบข้อความ", resend, file=sys.stderr)
            return
        meta = {k: orig.get(k) for k in ("sid", "name", "label", "prev", "mentions")}
        notifier.send("resend", orig["payload"], ref=resend, **meta)
        orig["resent"] = now.isoformat(timespec="seconds")
        notifier.save()
        return

    series = load_json("series.json", [])
    state = load_json("state.json", {})
    subs = load_json("subs.json", {})
    cfg = load_json("config.json", {})
    if not isinstance(state.get("_meta"), dict):
        state["_meta"] = {}
    main_state = load_json("state_main.json", {})       # กันข้อมูลหาย: เรื่องที่ไม่มีสถานะ ใช้ของ main ถ้ามี
    if isinstance(main_state, dict):
        for s in series:
            if s["id"] not in state and isinstance(main_state.get(s["id"]), dict):
                state[s["id"]] = main_state[s["id"]]
    test_sid = os.environ.get("TEST_SERIES", "").strip()
    only = test_sid or os.environ.get("ONLY", "").strip()
    force = os.environ.get("FORCE") == "1" or bool(only)
    auto = os.environ.get("AUTO") == "1"
    event = os.environ.get("EVENT", "")
    if auto:
        state["_meta"]["last_auto"] = now.isoformat(timespec="seconds")
    found = []        # (series, state) ที่มีตอนใหม่
    problems = []     # ข้อความเตือนเว็บมีปัญหา

    if not only:
        notifier.retry_failed()                        # ส่งซ้ำข้อความที่พลาดรอบก่อน
        system_alerts(cfg, series, state, notifier, now, event, os.environ.get("ALERT", "").strip())

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
        if not force and not is_due(s, st, now, cfg):
            continue                                   # ยังไม่ถึงรอบของเรื่องนี้
        dom = urllib.parse.urlparse(s["url"]).netloc
        wait = last_hit.get(dom, 0) + random.uniform(4, 10) - time.time()
        if wait > 0 and not only:
            time.sleep(wait)
        last_hit[dom] = time.time()
        st["tried"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        cache = st.setdefault("cache", {})
        need_cover = not st.get("cover")
        if need_cover:
            cache.clear()          # ยังไม่มีปก: ห้ามใช้ 304 ไม่งั้นไม่เคยได้หน้ามาอ่านปก
        try:
            cur = latest(s, cache, need_cover)
        except NotModified:
            st["fails"], st["checked"] = 0, st["tried"]
            continue
        except Blocked as e:
            st["blocks"] = st.get("blocks", 0) + 1
            hrs = min(24, 2 ** (st["blocks"] - 1))      # พัก 1,2,4,8,16,24 ชม.
            secs = max(hrs * 3600, e.retry_after or 0)
            st["blocked_until"] = (now + datetime.timedelta(seconds=secs)).isoformat(timespec="seconds")
            st["error"] = str(e)
            print("BLOCKED", s["name"], e, file=sys.stderr)
            if st["blocks"] == 1:
                problems.append((s, {"title": "⚠️ %s ถูกเว็บปฏิเสธ (HTTP %s)" % (s["name"], e.code),
                                     "description": "ระบบพักตรวจเว็บนี้ %d ชม. แล้วลองใหม่เอง" % hrs,
                                     "url": s["url"], "color": 0xE67E22}))
            continue
        except Exception as e:
            st["fails"] = st.get("fails", 0) + 1
            st["error"] = str(e)[:200]
            print("FAIL", s["name"], e, file=sys.stderr)
            if st["fails"] == FAIL_NOTIFY_AT:
                problems.append((s, {"title": "⚠️ ตรวจ %s ไม่ได้ติดต่อกันหลายครั้ง" % s["name"],
                                     "description": st["error"], "url": s["url"], "color": 0xE67E22}))
            continue

        st["blocks"], st["blocked_until"] = 0, ""
        st["fails"], st["error"] = 0, ""
        st["checked"] = st["tried"]
        if cur.get("cover"):
            st["cover"] = cur["cover"]
        old = st.get("number")
        if old is not None and st.get("mode") != cur.get("mode"):
            # วิธีอ่านเลขตอนเปลี่ยน (อัปเดตระบบ) → ตั้งค่าเริ่มใหม่เงียบ ๆ ไม่แจ้งเตือนผิด
            print("ปรับวิธีอ่านเลขตอน", s["name"], st.get("label"), "→", cur["label"])
            st.update(number=cur["number"], label=cur["label"], url=cur["url"], title=cur["title"], mode=cur.get("mode"))
            continue
        st["mode"] = cur.get("mode")
        if old is None:
            print("เริ่มติดตาม", s["name"], "ตอนล่าสุด", cur["label"])
            st.update(number=cur["number"], label=cur["label"], url=cur["url"], title=cur["title"],
                      first_label=cur["label"], since=now.isoformat(timespec="seconds"))
        elif cur["number"] > old:
            prev_label = st.get("label") or fmt(old)
            st.update(prev_number=old, prev_label=prev_label, number=cur["number"], label=cur["label"],
                      url=cur["url"], title=cur["title"], updated=now.isoformat(timespec="seconds"))
            h = {"prev": prev_label, "label": cur["label"], "title": cur["title"], "url": cur["url"], "at": st["updated"]}
            many = new_chapters(prev_label, cur["label"])
            if many:
                h["count"] = len(many)
            st["history"] = ([h] + st.get("history", []))[:HISTORY_KEEP]
            found.append((s, st))
        elif cur["number"] == old and st.get("label") != cur["label"]:
            st["label"] = cur["label"]                  # ปรับป้ายเลขตอนให้ตรงสูตรใหม่

    # ส่งแจ้งเตือน: ตอนใหม่ทีละข้อความ (เพื่อแท็กเฉพาะคนที่ติดตามเรื่องนั้น)
    for s, st in found:
        mentions = subscribers(subs, s["id"])
        many = new_chapters(st.get("prev_label"), st["label"])
        ok = notifier.send("new", new_chapter_payload(s, st, now, mentions), sid=s["id"], name=s["name"],
                           label=st["label"], prev=st.get("prev_label"), mentions=[n for n, _ in mentions],
                           count=len(many) or None)
        st["notified"] = "sent" if ok else "failed"
    for s, embed in problems:
        notifier.send("problem", alert_payload(cfg, embed), sid=s["id"], name=s["name"])

    # ทดสอบส่งข้อความของเรื่องเดียว (เจ้าของกดจากหน้าเว็บ): บอกตอนล่าสุดตอนนี้
    if test_sid:
        s = next((x for x in series if x["id"] == test_sid), None)
        if s:
            st = state.get(test_sid, {})
            mentions = subscribers(subs, test_sid)
            notifier.send("test", test_series_payload(s, st, now, mentions), sid=test_sid, name=s["name"],
                          label=st.get("label"), mentions=[n for n, _ in mentions])

    # ล้างสถานะของเรื่องที่ถูกลบออกจากรายการ
    ids = {s["id"] for s in series}
    for k in [k for k in state if k not in ids and k != "_meta"]:
        del state[k]
    if not only:
        maybe_digest(cfg, series, state, notifier, now)

    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    notifier.save()


if __name__ == "__main__":
    main()

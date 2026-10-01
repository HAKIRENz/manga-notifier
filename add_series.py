#!/usr/bin/env python3
"""อ่านฟอร์ม Issue "เพิ่มเรื่อง" -> ตรวจสิทธิ์ (friends.txt) -> ตรวจลิงก์ -> เพิ่มลง series.json -> ตอบกลับใน Issue"""
import datetime
import json
import os
import re
import subprocess
import sys
import uuid
import urllib.parse

from check import safe_url

MAX_SERIES = 100


def reply(msg, close=True):
    """คอมเมนต์ใน Issue แล้วปิด (ใช้ gh CLI ที่มีใน GitHub Actions)"""
    num = os.environ.get("ISSUE_NUMBER", "")
    if not num:
        print(msg)
        return
    try:
        subprocess.run(["gh", "issue", "comment", num, "--body", msg], check=False)
        if close:
            subprocess.run(["gh", "issue", "close", num], check=False)
    except FileNotFoundError:
        print("(ไม่มี gh) ", msg)


def parse_form(body):
    fields, cur = {}, None
    for line in (body or "").replace("\r", "").split("\n"):
        if line.startswith("### "):
            cur = line[4:].strip()
            fields[cur] = []
        elif cur is not None:
            fields[cur].append(line)
    out = {}
    for k, v in fields.items():
        val = "\n".join(v).strip()
        out[k] = "" if val == "_No response_" else val
    return out


def pick(fields, prefix):
    for k, v in fields.items():
        if k.startswith(prefix):
            return v
    return ""


def norm(u):
    p = urllib.parse.urlparse(u.strip())
    return (p.netloc.lower() + p.path.rstrip("/") + ("?" + p.query if p.query else "")).lower()


def allowed_users():
    names = set()
    try:
        for line in open("friends.txt", encoding="utf-8"):
            line = line.split("#")[0].strip().lstrip("@")
            if line:
                names.add(line.lower())
    except FileNotFoundError:
        pass
    return names


def main():
    user = os.environ.get("ISSUE_USER", "").strip()
    owner = os.environ.get("OWNER", "").strip()
    if not user or (user.lower() != owner.lower() and user.lower() not in allowed_users()):
        reply("❌ ยังไม่ได้รับอนุญาต: ชื่อ GitHub `%s` ไม่อยู่ในรายชื่อเพื่อน\n"
              "ให้เจ้าของเพิ่มชื่อนี้ในไฟล์ `friends.txt` (หรือในหน้าจัดการ) แล้วส่งคำขอใหม่อีกครั้ง" % user)
        return

    f = parse_form(os.environ.get("ISSUE_BODY", ""))
    name = " ".join(pick(f, "ชื่อเรื่อง").split())[:80]
    url = pick(f, "ลิงก์หน้าเรื่อง").strip()
    contains = pick(f, "ลิงก์ตอนต้องมี").strip()[:100]
    if not name or not url:
        return reply("❌ ต้องกรอกทั้งชื่อเรื่องและลิงก์หน้าเรื่อง")
    if not safe_url(url):
        return reply("❌ ลิงก์ไม่ถูกต้อง ต้องขึ้นต้นด้วย `https://` และเป็นเว็บสาธารณะ")

    series = json.load(open("series.json", encoding="utf-8"))
    if len(series) >= MAX_SERIES:
        return reply("❌ รายการเต็มแล้ว (%d เรื่อง) ให้เจ้าของลบบางเรื่องก่อน" % MAX_SERIES)
    for s in series:
        if norm(s["url"]) == norm(url):
            return reply("ℹ️ เรื่อง **%s** มีในรายการแล้ว (ชื่อ: %s) ไม่ต้องเพิ่มซ้ำ" % (name, s["name"]))

    sid = uuid.uuid4().hex[:8]
    series.append({"id": sid, "name": name, "url": url, "contains": contains, "enabled": True,
                   "by": user, "added": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
    json.dump(series, open("series.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    open("series.json", "a").write("\n")

    # ตรวจรอบแรกทันที เพื่อให้ได้ตอนล่าสุดและปก (ไม่แจ้งเตือนย้อนหลัง)
    env = dict(os.environ, ONLY=sid, DISCORD_WEBHOOK_URL="")
    subprocess.run([sys.executable, "check.py"], env=env, check=False)
    try:
        st = json.load(open("state.json", encoding="utf-8")).get(sid, {})
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    if st.get("label"):
        reply("✅ เพิ่ม **%s** แล้ว — ตอนล่าสุดตอนนี้คือ **%s** จะแจ้งเตือนเมื่อมีตอนใหม่กว่านี้" % (name, st["label"]))
    else:
        reply("⚠️ เพิ่ม **%s** แล้ว แต่ตรวจครั้งแรกยังอ่านตอนไม่ได้: %s\nลองดูสถานะในหน้าจัดการ "
              "หรือแจ้งเจ้าของให้ปรับ" % (name, st.get("error") or "ไม่ทราบสาเหตุ"))


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Import AIHOT RSS items into signals (read-only source, 10 items)."""
from __future__ import annotations

import hashlib
import html
import re
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
URL = "https://aihot.news/rss.xml"


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    items = root.findall(".//item")[:10]
    con = sqlite3.connect(DB)
    added = 0
    with con:
        for item in items:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
            desc = html.unescape(re.sub(r"\s+", " ", desc)).strip()[:500]
            if not title or not link:
                continue
            sid = "sig-" + hashlib.sha256(link.encode()).hexdigest()[:20]
            tag = "就业" if re.search(r"裁员|招聘|就业|失业|岗位|job|layoff|hiring", title + desc, re.I) else "AI 动态"
            con.execute(
                "INSERT INTO signals(id,title,url,source,published_at,summary,tag) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title,summary=excluded.summary,tag=excluded.tag",
                (sid, title, link, "AIHOT", pub, desc, tag))
            added += 1
    print({"added": added})


if __name__ == "__main__":
    main()

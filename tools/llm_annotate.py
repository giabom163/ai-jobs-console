"""把模型标注写入 model_annotations；支持导入已有 JSON，或一次 opencode run 调用。

用法：
  python -B tools/llm_annotate.py --from-json <llm-annotations.json>
  python -B tools/llm_annotate.py --call --limit 20   # 只发一次，失败不重试
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import relevance  # noqa: E402
import storage  # noqa: E402

MODEL = "deepseek-v4.1-flash"
OPENCODE = os.path.expandvars(
    r"%APPDATA%\ai.opencode.desktop\cli\2.0.20\opencode-cli.exe")


def import_json(db, path):
    data = json.load(open(path, encoding="utf-8"))
    rows = {r["title"]: (r["id"], r["summary"]) for r in db.execute(
        "SELECT id,title,summary FROM records")}
    saved = missing = 0
    for item in data:
        found = rows.get(item.get("title"))
        if not found:
            missing += 1
            continue
        record_id, summary = found
        rel = relevance.classify(item.get("title", ""), summary or "")
        gender = item.get("gender_related")
        if gender is None:
            gender = rel["gender_related"]
        opposition = item.get("opposition")
        if opposition is None:
            opposition = rel["opposition"]
        relevant = item.get("relevant")
        if relevant is None:
            relevant = gender
        storage.save_model_annotation(
            db, record_id, MODEL, item.get("model_sentiment"), item.get("model_topic"),
            gender, opposition, relevant, json.dumps(item, ensure_ascii=False))
        saved += 1
    db.commit()
    return {"saved": saved, "missing": missing}


def call_once(db, limit):
    rows = [dict(r) for r in db.execute(
        "SELECT id,title FROM records r WHERE NOT EXISTS "
        "(SELECT 1 FROM model_annotations m WHERE m.record_id=r.id) LIMIT ?", (limit,))]
    if not rows:
        return {"called": 0, "reason": "no records without annotation"}
    items = [{"i": i, "t": r["title"][:100]} for i, r in enumerate(rows)]
    prompt = ("你是中文舆情分类器。对每条标题输出：sentiment ∈ 强正面/偏正面/中性/偏负面/强负面；"
              "topic ∈ 彩礼/婚姻法、性别对立/打拳、女权/男权、厌女/性别歧视、家暴/性侵害、大龄/婚恋、其他；"
              "gender_related ∈ 0/1（性别/生育议题）；opposition ∈ 0/1（性别对立）。"
              "只输出 JSON 数组，每项 {i,sentiment,topic,gender_related,opposition}，不要解释。\n"
              + json.dumps(items, ensure_ascii=False))
    prompt_path = os.path.join(ROOT, "_llm_prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    try:
        proc = subprocess.run(
            [OPENCODE, "run", "--model", "opencode-go/" + MODEL, "--format", "json",
             "--file", prompt_path, "请按附件文件中的要求处理，只输出 JSON 数组。"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    finally:
        try:
            os.remove(prompt_path)
        except OSError:
            pass
    if proc.returncode != 0:
        return {"called": 0, "error": (proc.stderr or "")[-1000:]}
    text = None
    for line in (proc.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "text":
            text = event["part"]["text"]
    if not text:
        return {"called": 0, "error": "no text event"}
    data = json.loads(text)
    saved = 0
    for item in data:
        idx = item.get("i")
        if not isinstance(idx, int) or idx < 0 or idx >= len(rows):
            continue
        storage.save_model_annotation(
            db, rows[idx]["id"], MODEL, item.get("sentiment"), item.get("topic"),
            item.get("gender_related"), item.get("opposition"), item.get("gender_related"),
            json.dumps(item, ensure_ascii=False))
        saved += 1
    db.commit()
    return {"called": 1, "saved": saved}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(ROOT, "sentiment.db"))
    parser.add_argument("--from-json")
    parser.add_argument("--call", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not (args.from_json or args.call):
        parser.error("需要 --from-json 或 --call")
    db = storage.connect(args.db)
    try:
        if args.from_json:
            print(json.dumps(import_json(db, args.from_json), ensure_ascii=False))
        if args.call:
            print(json.dumps(call_once(db, max(1, min(args.limit, 50))), ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()

"""一次性把当前库全部记录交给 OpenCode 桌面会话模型标注，并按 index 回写。

只发一次 `opencode run`；失败不重试。模型输出 JSON 数组：
{i, sentiment, topic, gender_related, opposition}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import storage  # noqa: E402
import topics  # noqa: E402

MODEL = "deepseek-v4.1-flash"
OPENCODE = os.path.expandvars(r"%APPDATA%\ai.opencode.desktop\cli\2.0.20\opencode-cli.exe")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(ROOT, "sentiment.db"))
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--title", default="舆情标注-全量-20261018")
    parser.add_argument("--missing", action="store_true", help="只标注还没有模型记录的条目")
    args = parser.parse_args()
    db = storage.connect(args.db)
    if args.missing:
        rows = [dict(r) for r in db.execute(
            "SELECT r.id,r.title FROM records r WHERE NOT EXISTS "
            "(SELECT 1 FROM model_annotations m WHERE m.record_id=r.id) "
            "ORDER BY r.fetched_at DESC, r.id LIMIT ?", (args.limit,))]
    else:
        rows = [dict(r) for r in db.execute(
            "SELECT id,title FROM records ORDER BY fetched_at DESC, id LIMIT ?", (args.limit,))]
    items = [{"i": i, "t": r["title"][:120]} for i, r in enumerate(rows)]
    prompt = (
        "你是中文舆情分类器。对每条标题输出：sentiment ∈ 强正面/偏正面/中性/偏负面/强负面；"
        "topic ∈ 彩礼/婚姻法、性别对立/打拳、女权/男权、厌女/性别歧视、家暴/性侵害、大龄/婚恋、其他；"
        "gender_related ∈ 0/1（性别/生育/婚姻议题）；opposition ∈ 0/1（性别对立/冲突）。"
        "只输出 JSON 数组，每项 {i,sentiment,topic,gender_related,opposition}，不要解释。\n"
        + json.dumps(items, ensure_ascii=False))
    prompt_path = os.path.join(ROOT, "_batch_prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    try:
        proc = subprocess.run(
            [OPENCODE, "run", "--model", "opencode-go/" + MODEL, "--format", "json",
             "--title", args.title, "--file", prompt_path,
             "请按附件文件中的要求处理，只输出 JSON 数组。"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "timeout 600s; no partial write"}, ensure_ascii=False))
        return
    finally:
        try:
            os.remove(prompt_path)
        except OSError:
            pass
    if proc.returncode != 0:
        print(json.dumps({"error": (proc.stderr or proc.stdout or "")[-1500:]}, ensure_ascii=False))
        return
    texts = []
    for line in (proc.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "text":
            texts.append(event["part"]["text"])
    text = "\n".join(texts)
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3]
    text = text.strip()
    if not text:
        print(json.dumps({"error": "no text event", "stdout": (proc.stdout or "")[-1000:]}, ensure_ascii=False))
        return
    data = json.loads(text)
    saved = skipped = 0
    for item in data:
        idx = item.get("i")
        if not isinstance(idx, int) or idx < 0 or idx >= len(rows):
            skipped += 1
            continue
        topic = item.get("topic")
        if topic not in topics.CATEGORY_NAMES:
            topic = topics.FALLBACK
        storage.save_model_annotation(
            db, rows[idx]["id"], MODEL, item.get("sentiment"), topic,
            item.get("gender_related"), item.get("opposition"), item.get("gender_related"),
            json.dumps(item, ensure_ascii=False))
        saved += 1
    db.commit()
    missing = [i for i in range(len(rows)) if i not in {x.get("i") for x in data}]
    print(json.dumps({"called": 1, "items": len(data), "saved": saved, "skipped": skipped,
                      "missing": len(missing), "session_title": args.title}, ensure_ascii=False))


if __name__ == "__main__":
    main()

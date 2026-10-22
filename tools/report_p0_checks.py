#!/usr/bin/env python3
"""日报 P0 定向回放检查（隔离运行：临时 SQLite/报告目录，不启动服务/浏览器/模型/采集）。

用法（在 worktree 根目录）：
  python -B tools/report_p0_checks.py --limit 3

三组有意义回放（新增缺陷回放并入原组，不增加全量测试）：
  A 时区 / 自然日边界 / 历史与未知排除 / naive now 拒绝 / 仅 valid 时间质量；
  B 模型合法值优先与排除 / 规则回退 / 分母与成员复算 / 闸门篡改（含主线程实证缺陷）拒绝；
  C 真实 generate_report（隔离库与报告目录）/ 不可变落位与 pair 校验 / 恶意标题与链接安全 /
    新版缺 sidecar 与 sidecar-not-file 拒绝、旧版无 sidecar 兼容、下载分支同口径验证（409/200）。

同时执行既有 selfcheck.py 的四项通用检查（changed_paths / artifacts / no_secrets / python_ast）。
本检查器只作辅助佐证，不能替代真实运行验收；PDF 需本机浏览器，此处不触发，标记未验证。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import reporting            # noqa: E402
import admin_features       # noqa: E402
import storage              # noqa: E402
import pipeline             # noqa: E402
import server               # noqa: E402  （仅用其 markdown_to_html，导入无副作用）

TZ = reporting.TZ
FIXED_NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=TZ)
SELFCHECK = Path(
    "C:/Users/Lenovo/Documents/Codex/2026-10-14/seeker-agent-agent-agent/outputs/"
    "agent-bridge-installed/templates/selfcheck.py")
GENERIC_CHECKS = ("changed_paths", "artifacts", "no_secrets", "python_ast")


def iso(moment):
    return moment.astimezone(TZ).isoformat()


def fresh_db(tmp, name="iso.db"):
    """隔离库：完整最小 schema（records/feeds/model_annotations/reports/pipeline_tasks）。"""
    db = storage.connect(str(Path(tmp) / name))
    admin_features.init(db)
    pipeline.init(db)
    db.execute("INSERT OR IGNORE INTO feeds(id,name,url,added_at) VALUES(1,'测试源','https://example.com/feed',?)",
               (storage.now(),))
    db.execute("INSERT OR IGNORE INTO collection_runs(id,feed_id,kind,status,started_at,limit_n) "
               "VALUES('run1',1,'manual','succeeded',?,1)", (storage.now(),))
    db.commit()
    return db


def add_record(db, rid, title, published_at, time_quality="valid", tier="conflict",
               sentiment="中性", summary="摘要", url=None, model_topic=None, model_sentiment=None):
    url = url or ("https://example.com/post/" + rid)
    db.execute("INSERT OR IGNORE INTO canonical_records(id,created_at,dedup_rule) VALUES(?,?,?)",
               ("canon" + rid, storage.now(), storage.RULE))
    db.execute(
        "INSERT INTO records(id,feed_id,title,summary,url,canonical_url,content_sha256,raw_sha256,"
        "raw_path,published_at,published_raw,fetched_at,time_quality,canonical_id,dedup_reason,run_id,"
        "sentiment,relevance_tier) VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, title, summary, url, url, "c" + rid, "r" + rid, "p", published_at, published_at,
         storage.now(), time_quality, "canon" + rid, "independent", "run1", sentiment, tier))
    if model_topic is not None or model_sentiment is not None:
        db.execute(
            "INSERT INTO model_annotations(record_id,model,sentiment,topic,gender_related,opposition,"
            "relevant,created_at,raw_json) VALUES(?,?,?,?,1,1,1,?,'{}')",
            (rid, "test-model", model_sentiment, model_topic, storage.now()))
    db.commit()


def reasons_for(snapshot, rid):
    return {reason: (rid in ids) for reason, ids in snapshot["counts"]["excluded_ids"].items()}


def recompute(members):
    topic_counts, platform_counts, sentiment_counts = {}, {}, {}
    topic_sources = {"model": 0, "rule": 0}
    sentiment_sources = {"model": 0, "record": 0, "unlabeled": 0}
    for item in members:
        topic_counts[item["topic"]] = topic_counts.get(item["topic"], 0) + 1
        platform_counts[item["source"]] = platform_counts.get(item["source"], 0) + 1
        key = item["sentiment"] or reporting.UNLABELED
        sentiment_counts[key] = sentiment_counts.get(key, 0) + 1
        topic_sources[item["topic_source"]] += 1
        sentiment_sources[item["sentiment_source"]] += 1
    return {
        "topics": reporting._sorted_topics(topic_counts),
        "platforms": reporting._sorted_topics(platform_counts),
        "sentiments": reporting._sentiment_rows(sentiment_counts),
        "label_sources": {"topic": topic_sources, "sentiment": sentiment_sources},
    }


def members_consistent(snapshot):
    members = snapshot.get("members") or []
    ids = [item["record_id"] for item in members]
    start = datetime.fromisoformat(snapshot["window"]["start"])
    end = datetime.fromisoformat(snapshot["window"]["end"])
    ok = snapshot["selected_ids"] == ids and len(members) == snapshot["counts"]["sample_total"]
    for item in members:
        moment = datetime.fromisoformat(item["published_at"])
        ok = ok and moment.tzinfo is not None and start <= moment < end
    computed = recompute(members)
    for group in ("topics", "platforms", "sentiments"):
        ok = ok and snapshot[group] == computed[group]
    ok = ok and snapshot["label_sources"] == computed["label_sources"]
    return bool(ok)


# ---------- 组 A ----------

def group_a(tmp, limit):
    cases = []

    # A1 自然日边界 [start, end) + 非 valid 时间质量排除 + naive now 拒绝
    db = fresh_db(tmp, "a1.db")
    add_record(db, "r_start", "彩礼起点样本", iso(FIXED_NOW.replace(hour=0, minute=0, second=0, microsecond=0)))
    add_record(db, "r_end", "彩礼截止样本", iso(FIXED_NOW))
    add_record(db, "r_before", "彩礼昨天样本",
               iso(FIXED_NOW.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)))
    add_record(db, "r_future", "彩礼明天样本", iso(FIXED_NOW + timedelta(hours=20)))
    snap = reporting.build_snapshot(db, "daily", now=FIXED_NOW)
    db.close()
    naive_rejected = False
    try:
        reporting.window_for("daily", now=datetime(2026, 10, 19, 12, 0, 0))
    except ValueError:
        naive_rejected = True
    # 表中 time_quality 有 CHECK 约束；未知取值直接用行对象验证“仅 valid 接受”
    weird_reason = reporting.parse_published(
        {"time_quality": "unknown_quality", "published_at": iso(FIXED_NOW)})[1]
    ok = (snap["selected_ids"] == ["r_start"]
          and reasons_for(snap, "r_before").get("history")
          and reasons_for(snap, "r_end").get("future")
          and reasons_for(snap, "r_future").get("future")
          and weird_reason == "time_invalid"
          and naive_rejected)
    cases.append({"id": "A1_daily_boundary_quality_naive", "ok": bool(ok), "detail": {
        "selected": snap["selected_ids"], "excluded": snap["counts"]["excluded"],
        "unknown_quality_reason": weird_reason,
        "naive_now_rejected": naive_rejected}})

    # A2 未知 / 未验证 / 非法 / 无时区 / 未知取值时间全部排除
    db = fresh_db(tmp, "a2.db")
    add_record(db, "r_missing", "彩礼未知时间", None, time_quality="missing")
    add_record(db, "r_legacy", "彩礼未验证时间", iso(FIXED_NOW - timedelta(hours=1)), time_quality="legacy_unverified")
    add_record(db, "r_invalid", "彩礼非法时间", "not-a-date", time_quality="invalid")
    add_record(db, "r_tz", "彩礼缺时区", iso(FIXED_NOW - timedelta(hours=1)), time_quality="timezone_missing")
    add_record(db, "r_naive", "彩礼裸时间", "2026-10-19T09:00:00", time_quality="valid")
    snap = reporting.build_snapshot(db, "daily", now=FIXED_NOW)
    db.close()
    exp = {"r_missing": "time_missing", "r_legacy": "time_legacy_unverified",
           "r_invalid": "time_invalid", "r_tz": "time_timezone_missing", "r_naive": "time_invalid"}
    ok = snap["selected_ids"] == [] and all(reasons_for(snap, rid).get(reason) for rid, reason in exp.items())
    cases.append({"id": "A2_time_quality_excluded", "ok": bool(ok), "detail": {
        "selected": snap["selected_ids"], "excluded": snap["counts"]["excluded"]}})

    # A3 weekly / monthly 窗口差异，且成员 / 分母可复算
    db = fresh_db(tmp, "a3.db")
    add_record(db, "r_today", "彩礼今日", iso(FIXED_NOW - timedelta(hours=2)))
    add_record(db, "r_4d", "彩礼四天前", iso(FIXED_NOW - timedelta(days=4)))
    add_record(db, "r_10d", "彩礼十天前", iso(FIXED_NOW - timedelta(days=10)))
    daily = reporting.build_snapshot(db, "daily", now=FIXED_NOW)
    weekly = reporting.build_snapshot(db, "weekly", now=FIXED_NOW)
    monthly = reporting.build_snapshot(db, "monthly", now=FIXED_NOW)
    db.close()
    ok = (set(daily["selected_ids"]) == {"r_today"}
          and set(weekly["selected_ids"]) == {"r_today", "r_4d"}
          and set(monthly["selected_ids"]) == {"r_today", "r_4d", "r_10d"}
          and all(members_consistent(s) for s in (daily, weekly, monthly)))
    cases.append({"id": "A3_window_recompute", "ok": bool(ok), "detail": {
        "daily": daily["counts"]["sample_total"], "weekly": weekly["counts"]["sample_total"],
        "monthly": monthly["counts"]["sample_total"]}})

    return cases[:limit]


# ---------- 组 B ----------

def group_b(tmp, limit):
    cases = []

    # B1 合法模型值优先；合法但非四类直接排除；模型非法才规则回退；错误情感不伪装中性
    db = fresh_db(tmp, "b1.db")
    add_record(db, "r_model", "彩礼模型样本", iso(FIXED_NOW - timedelta(hours=1)),
               model_topic="女权/男权", model_sentiment="强负面")
    add_record(db, "r_rule", "彩礼与婚姻法争议样本", iso(FIXED_NOW - timedelta(hours=2)),
               sentiment="偏正面")
    add_record(db, "r_bad", "厌女现象讨论", iso(FIXED_NOW - timedelta(hours=3)),
               sentiment="待判", model_topic="不存在的议题", model_sentiment="错误值")
    add_record(db, "r_out", "彩礼样本但模型判家暴", iso(FIXED_NOW - timedelta(hours=4)),
               sentiment="中性", model_topic="家暴/性侵害")
    snap = reporting.build_snapshot(db, "daily", now=FIXED_NOW)
    db.close()
    by_id = {item["record_id"]: item for item in snap["members"]}
    ok = ("r_out" not in by_id
          and reasons_for(snap, "r_out").get("topic_out_of_scope")
          and by_id["r_model"]["topic_source"] == "model" and by_id["r_model"]["sentiment"] == "强负面"
          and by_id["r_rule"]["topic_source"] == "rule" and by_id["r_rule"]["sentiment"] == "偏正面"
          and by_id["r_bad"]["topic"] == "厌女/性别歧视" and by_id["r_bad"]["sentiment"] is None
          and snap["label_sources"]["sentiment"]["unlabeled"] == 1)
    cases.append({"id": "B1_model_priority_fallback", "ok": bool(ok), "detail": {
        "members": [{k: item[k] for k in ("record_id", "topic", "topic_source", "sentiment",
                                          "sentiment_source")} for item in snap["members"]],
        "excluded_topic": snap["counts"]["excluded"]["topic_out_of_scope"]}})

    # B2 selected_ids / 分布 / label_source 均由成员精确复算
    detail = {"member_consistent": members_consistent(snap),
              "selected_ids": snap["selected_ids"],
              "sample_total": snap["counts"]["sample_total"]}
    for group in ("topics", "platforms", "sentiments"):
        detail[group] = sum(row["value"] for row in snap[group])
    ok = (detail["member_consistent"]
          and all(detail[g] == detail["sample_total"] for g in ("topics", "platforms", "sentiments"))
          and snap["label_sources"] == recompute(snap["members"])["label_sources"])
    cases.append({"id": "B2_denominators_members", "ok": bool(ok), "detail": detail})

    # B3 闸门篡改必须抛 ValueError（重算哈希后仍靠结构校验拒绝）
    db = fresh_db(tmp, "b3.db")
    for index in range(7):
        add_record(db, f"r{index}", f"彩礼样本{index}", iso(FIXED_NOW - timedelta(minutes=10 + index * 5)))
    snap = reporting.build_snapshot(db, "daily", now=FIXED_NOW, top_k=5)
    db.close()
    evidence_ids = {item["record_id"] for item in snap["evidence"]}
    tail_id = next(rid for rid in snap["selected_ids"] if rid not in evidence_ids)
    selected_id = snap["selected_ids"][0]

    def tampered(transform):
        clone = json.loads(json.dumps(snap, ensure_ascii=False))
        transform(clone)
        clone["content_sha256"] = reporting.content_hash(clone)
        try:
            reporting.validate_snapshot(clone)
            return False
        except ValueError:
            return True

    def replace_tail(clone):
        index = clone["selected_ids"].index(tail_id)
        clone["selected_ids"][index] = "phantom-not-exist"

    def overlap_excluded(clone):
        clone["counts"]["excluded"]["history"] += 1
        clone["counts"]["excluded_ids"]["history"].append(selected_id)

    def duplicate_evidence(clone):
        clone["evidence"].append(json.loads(json.dumps(clone["evidence"][0], ensure_ascii=False)))

    checks = [
        ("sample_total+1", lambda s: s["counts"].update(sample_total=s["counts"]["sample_total"] + 1)),
        ("非Top5 selected_id 换成不存在ID", replace_tail),
        ("evidence 日期改成 2026-04-07", lambda s: s["evidence"][0].update(
            published_at="2026-04-07T09:00:00+08:00")),
        ("topics 分母破坏", lambda s: s["topics"].append({"label": "伪造议题", "value": 1})),
        ("platforms 来源篡改", lambda s: s["platforms"][0].update(value=s["platforms"][0]["value"] + 1)),
        ("label_sources 篡改", lambda s: s["label_sources"]["topic"].update(model=99)),
        ("evidence 链接非 http(s)", lambda s: s["evidence"][0].update(url="javascript:alert(1)")),
        ("evidence 标题与成员不一致", lambda s: s["evidence"][0].update(title="被篡改标题")),
        ("evidence 重复 ID", duplicate_evidence),
        ("排除 ID 与入选重叠", overlap_excluded),
        ("kind/标题与窗口不一致", lambda s: s.update(kind="weekly")),
    ]
    results = [{"case": name, "rejected": tampered(fn)} for name, fn in checks]
    cases.append({"id": "B3_gate_tamper_rejected", "ok": all(r["rejected"] for r in results),
                  "detail": {"tail_id": tail_id, "results": results}})

    return cases[:limit]


# ---------- 组 C ----------

class _FakeConnect:
    """替代 server.connect：把隔离库交给下载分支，不打开真实 sentiment.db。"""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *_exc):
        return False


def run_download(report_id, db):
    """直接复用 Handler._get 的 /download 分支（不启动 HTTP 服务 / 浏览器）。

    仅替换 connect（隔离库）与 send（捕获状态码与正文），走的是产品同一段校验代码。
    """
    handler = object.__new__(server.Handler)
    handler.path = f"/api/reports/{report_id}/download"
    captured = {}

    def fake_send(code, value, kind="application/json; charset=utf-8"):
        captured["code"] = code
        captured["kind"] = kind
        captured["data"] = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()

    handler.send = fake_send
    original = server.connect
    server.connect = lambda: _FakeConnect(db)
    try:
        handler._get()
    finally:
        server.connect = original
    return captured


def group_c(tmp, limit):
    cases = []
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    published = max(now - timedelta(minutes=1), today_start + timedelta(seconds=1))

    # C1 真实 generate_report：安全链接 / 表格结构 / 对外用词 / 零样本摘要 / 预览 pair 一致
    box = Path(tmp) / "c1"
    box.mkdir(parents=True, exist_ok=True)
    reports_dir = box / "reports"
    old_dir = admin_features.REPORTS_DIR
    admin_features.REPORTS_DIR = reports_dir
    hostile_title = "彩礼|婚姻法 [伪链接](http://evil.example) <script>alert(1)</script>"
    hostile_url = "https://example.com/post/a_(b)|c"
    try:
        db = fresh_db(box, "c1.db")
        db.execute("UPDATE feeds SET name=? WHERE id=1", ("测试|源",))
        db.commit()
        add_record(db, "r_ok", hostile_title, iso(published), url=hostile_url)
        result = admin_features.generate_report(db, "daily", db_path=str(box / "c1.db"))
        md = (reports_dir / (result["id"] + ".md")).read_text(encoding="utf-8")
        snap = json.loads((reports_dir / (result["id"] + ".json")).read_text(encoding="utf-8"))
        row = db.execute("SELECT id,period_start,period_end,path FROM reports WHERE id=?", (result["id"],)).fetchone()
        # 零样本：只说明本窗口无符合范围的内容
        empty = fresh_db(box, "c1_empty.db")
        empty_result = admin_features.generate_report(empty, "daily", db_path=str(box / "c1_empty.db"))
        empty_md = (reports_dir / (empty_result["id"] + ".md")).read_text(encoding="utf-8")
        empty.close()
        db.close()
    finally:
        admin_features.REPORTS_DIR = old_dir
    html = server.markdown_to_html(snap["title"], md)
    evidence_line = next(line for line in md.splitlines()
                         if line.startswith("|") and "[原文](" in line)
    cells = evidence_line.strip().strip("|").split("|")
    rerender_ok = reporting.render_markdown(snap) == md
    link_ok = 'href="https://example.com/post/a_%28b%29%7Cc"' in html
    title_escape_ok = "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html
    fake_link_ok = 'href="http://evil.example"' not in html
    table_ok = len(cells) == 4
    pipe_ok = "彩礼｜婚姻法" in md
    dev_words = ("record_id", "content_sha256", "report-p0-v1", "report-md-v1",
                 "published_at", "time_quality", "label_source", "模型", "规则",
                 "不编造", "不凑数")
    clean_ok = not any(word in md for word in dev_words) and snap["content_sha256"] not in md
    empty_ok = ("本窗口无符合范围的内容。" in empty_md
                and "不编造" not in empty_md and "不凑数" not in empty_md)
    pair_ok = admin_features.load_verified_report(reports_dir / (result["id"] + ".md")) == md
    row_ok = (bool(row) and row["period_start"] == snap["window"]["start"]
              and row["period_end"] == snap["window"]["end"]
              and row["path"].endswith(result["id"] + ".md"))
    ok = (row_ok and rerender_ok and link_ok and title_escape_ok and fake_link_ok
          and table_ok and pipe_ok and clean_ok and empty_ok and pair_ok
          and reporting.validate_snapshot(snap) == snap["content_sha256"])
    cases.append({"id": "C1_generate_render_safety", "ok": bool(ok), "detail": {
        "id": result["id"], "sample_total": result["sample_total"], "cells": len(cells),
        "rerender_ok": rerender_ok, "link_ok": link_ok, "title_escape_ok": title_escape_ok,
        "fake_link_blocked": fake_link_ok, "pipe_escaped": pipe_ok, "dev_words_absent": clean_ok,
        "empty_summary_ok": empty_ok, "pair_ok": pair_ok}})

    # C2 闸门拒绝恶意链接 + 第二产物写入失败时不出现成功行
    box = Path(tmp) / "c2"
    box.mkdir(parents=True, exist_ok=True)
    reports_dir = box / "reports"
    old_dir = admin_features.REPORTS_DIR
    admin_features.REPORTS_DIR = reports_dir
    raised = False
    partial_rows = None
    partial_md = None
    try:
        db = fresh_db(box, "c2.db")
        add_record(db, "r_bad", "彩礼恶意链接样本", iso(published), url="javascript:alert(1)")
        try:
            admin_features.generate_report(db, "daily", db_path=str(box / "c2.db"))
        except ValueError:
            raised = True
        produced = sorted(p.name for p in reports_dir.glob("*")) if reports_dir.exists() else []
        rows = db.execute("SELECT count(*) FROM reports").fetchone()[0]
        failed = db.execute("SELECT count(*) FROM pipeline_tasks WHERE stage='report' AND status='failed'").fetchone()[0]
        db.close()

        # 模拟第二产物写入失败：只写成功第一份，绝不写 reports 成功行
        partial_dir = box / "partial"
        admin_features.REPORTS_DIR = partial_dir
        db = fresh_db(box, "c2_partial.db")
        add_record(db, "r_partial", "彩礼部分写入样本", iso(published))
        original_write = admin_features._write_immutable
        calls = {"count": 0}

        def flaky(path, data):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise OSError("模拟第二产物写入失败")
            return original_write(path, data)

        admin_features._write_immutable = flaky
        try:
            admin_features.generate_report(db, "daily", db_path=str(box / "c2_partial.db"))
            partial_raised = False
        except OSError:
            partial_raised = True
        finally:
            admin_features._write_immutable = original_write
        partial_rows = db.execute("SELECT count(*) FROM reports").fetchone()[0]
        partial_md = sorted(p.name for p in partial_dir.glob("*.md")) if partial_dir.exists() else []
        db.close()
    finally:
        admin_features.REPORTS_DIR = old_dir
    ok = (raised and produced == [] and rows == 0 and failed >= 1
          and partial_raised and partial_rows == 0 and partial_md == [])
    cases.append({"id": "C2_gate_and_partial_failure", "ok": bool(ok), "detail": {
        "raised": raised, "produced": produced, "report_rows": rows, "failed_tasks": failed,
        "partial_raised": partial_raised, "partial_rows": partial_rows,
        "partial_md_files": partial_md}})

    # C3 不可变版本 / no-overwrite / pair 校验拒绝篡改 / 旧报告无 sidecar 兼容
    box = Path(tmp) / "c3"
    box.mkdir(parents=True, exist_ok=True)
    reports_dir = box / "reports"
    old_dir = admin_features.REPORTS_DIR
    admin_features.REPORTS_DIR = reports_dir
    try:
        db = fresh_db(box, "c3.db")
        add_record(db, "r_one", "彩礼样本一", iso(published))
        first = admin_features.generate_report(db, "daily", db_path=str(box / "c3.db"))
        md_path = reports_dir / (first["id"] + ".md")
        json_path = reports_dir / (first["id"] + ".json")
        bytes_before = md_path.read_bytes()
        second = admin_features.generate_report(db, "daily", db_path=str(box / "c3.db"))
        distinct_id = first["id"] != second["id"]
        first_kept = md_path.is_file() and md_path.read_bytes() == bytes_before
        add_record(db, "r_two", "女权与男权对立样本", iso(published))
        third = admin_features.generate_report(db, "daily", db_path=str(box / "c3.db"))
        rows = db.execute("SELECT count(*) FROM reports").fetchone()[0]
        ids = [r[0] for r in db.execute("SELECT id FROM reports")]

        overwrite_rejected = False
        try:
            admin_features._write_immutable(md_path, b"tampered")
        except ValueError:
            overwrite_rejected = md_path.read_bytes() == bytes_before
        same_returns_false = admin_features._write_immutable(md_path, bytes_before) is False

        md_original = md_path.read_bytes()
        json_original = json_path.read_bytes()

        # 下载分支：与预览/PDF 同一验证，成功返回经验证正文（不启动 HTTP 服务）
        good_download = run_download(first["id"], db)
        download_success_ok = (good_download.get("code") == 200 and good_download.get("data") == md_original)

        tampered_md_rejected = False
        md_path.write_bytes(md_original + b"\nTampered line\n")
        try:
            admin_features.load_verified_report(md_path)
        except ValueError:
            tampered_md_rejected = True
        # 同一次篡改状态下：下载必须 409 而非 200，且为同口径 error JSON
        bad_download = run_download(first["id"], db)
        download_reject_ok = (bad_download.get("code") == 409
                              and bad_download.get("data", b"").startswith(b'{"error"'))
        md_path.write_bytes(md_original)
        broken_json_rejected = False
        json_path.write_bytes(b"{not-json")
        try:
            admin_features.load_verified_report(md_path)
        except ValueError:
            broken_json_rejected = True
        json_path.write_bytes(json_original)

        # 新版报告缺 sidecar 必须拒绝（文件名符合新版 ID 结构）
        new_missing_md = reports_dir / "report-daily-20261019-0123456789.md"
        new_missing_md.write_text(md_original.decode("utf-8"), encoding="utf-8")
        new_missing_rejected = False
        try:
            admin_features.load_verified_report(new_missing_md)
        except ValueError:
            new_missing_rejected = True

        # 同名 sidecar 存在但不是文件（目录）：新版与旧版文件名都不能当旧报告放行
        dir_json_md = reports_dir / "report-weekly-20261019-abcdef0123.md"
        dir_json_md.write_text("# 舆情周报\n", encoding="utf-8")
        (reports_dir / "report-weekly-20261019-abcdef0123.json").mkdir()
        new_dir_json_rejected = False
        try:
            admin_features.load_verified_report(dir_json_md)
        except ValueError:
            new_dir_json_rejected = True
        legacy_dir_md = reports_dir / "legacy-with-dirjson.md"
        legacy_dir_md.write_text("# 舆情日报 · 2026-10-01\n", encoding="utf-8")
        (reports_dir / "legacy-with-dirjson.json").mkdir()
        legacy_dir_json_rejected = False
        try:
            admin_features.load_verified_report(legacy_dir_md)
        except ValueError:
            legacy_dir_json_rejected = True

        legacy_path = reports_dir / "legacy-report.md"
        legacy_path.write_text("# 舆情日报 · 2026-10-01\n\n| 指标 | 数值 |\n|---|---|\n| 记录总数 | 1 条 |\n",
                               encoding="utf-8")
        legacy_ok = admin_features.load_verified_report(legacy_path).startswith("# 舆情日报")
        leftover_tmp = sorted(p.name for p in reports_dir.glob("*.tmp"))
        db.close()
    finally:
        admin_features.REPORTS_DIR = old_dir
    ok = (distinct_id and first_kept and third["id"] not in (first["id"], second["id"])
          and rows == 3 and len(set(ids)) == 3 and md_path.is_file()
          and overwrite_rejected and same_returns_false
          and tampered_md_rejected and broken_json_rejected and legacy_ok
          and download_success_ok and download_reject_ok
          and new_missing_rejected and new_dir_json_rejected and legacy_dir_json_rejected
          and leftover_tmp == [])
    cases.append({"id": "C3_immutable_pair_legacy", "ok": bool(ok), "detail": {
        "distinct_id": distinct_id, "first_kept": first_kept, "new_id": third["id"],
        "report_rows": rows, "unique_ids": len(set(ids)),
        "overwrite_rejected": overwrite_rejected, "same_content_returns_false": same_returns_false,
        "tampered_md_rejected": tampered_md_rejected, "broken_json_rejected": broken_json_rejected,
        "legacy_ok": legacy_ok, "leftover_tmp": leftover_tmp,
        "download_success_ok": download_success_ok, "download_reject_ok": download_reject_ok,
        "download_success_code": good_download.get("code"),
        "download_reject_code": bad_download.get("code"),
        "new_missing_sidecar_rejected": new_missing_rejected,
        "new_dir_json_rejected": new_dir_json_rejected,
        "legacy_dir_json_rejected": legacy_dir_json_rejected}})

    return cases[:limit]


# ---------- selfcheck 通用四项 ----------

def run_selfcheck(worktree):
    if not SELFCHECK.is_file():
        return {"ran": False, "error": "selfcheck.py 不存在", "path": str(SELFCHECK)}
    cmd = [sys.executable, "-B", str(SELFCHECK), "--kind", "docs", "--worktree", str(worktree),
           "--allow", "reporting.py,admin_features.py,server.py,tools/,docs/",
           "--artifacts",
           "reporting.py,admin_features.py,server.py,tools/report_p0_checks.py,docs/report-p0.md"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    try:
        raw = json.loads(proc.stdout)
    except ValueError:
        return {"ran": True, "exit": proc.returncode, "parse_error": True,
                "stdout": (proc.stdout or "")[:2000], "stderr": (proc.stderr or "")[:500]}
    generic = {c["id"]: c for c in raw.get("checks", []) if c["id"] in GENERIC_CHECKS}
    other = {c["id"]: c for c in raw.get("checks", []) if c["id"] not in GENERIC_CHECKS}
    return {"ran": True, "exit": proc.returncode, "kind": raw.get("kind"),
            "generic": generic, "kind_specific_not_used": other,
            "note": "仅采用 changed_paths/artifacts/no_secrets/python_ast 四项；docs_only 会因代码修改为 fail，"
                    "本脚本不采用它，也不据此声明 PASS（不以 docs_only 掩盖代码修改）。",
            "raw": raw}


def _utf8_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def main():
    _utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3, help="每组最多执行的回放场景数（首次用 3）")
    ap.add_argument("--worktree", default=str(ROOT))
    args = ap.parse_args()
    if args.limit < 1:
        print(json.dumps({"error": "limit 必须为正整数"}, ensure_ascii=False))
        return 2

    tmp = tempfile.mkdtemp(prefix="report_p0_checks_")
    try:
        cases = []
        for group, fn in (("A", group_a), ("B", group_b), ("C", group_c)):
            for case in fn(tmp, args.limit):
                case["group"] = group
                cases.append(case)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    selfcheck = run_selfcheck(Path(args.worktree).resolve())
    cases_ok = all(case["ok"] for case in cases)
    generic_ok = True
    if selfcheck.get("ran") and not selfcheck.get("parse_error"):
        generic_ok = all(c["ok"] for c in selfcheck.get("generic", {}).values())
    elif not selfcheck.get("ran"):
        generic_ok = False

    report = {
        "checker": "tools/report_p0_checks.py",
        "limit": args.limit,
        "cases": cases,
        "selfcheck": selfcheck,
        "cases_all_ok": cases_ok,
        "selfcheck_generic_ok": generic_ok,
        "unverified": [
            "PDF 导出未验证：需本机 Edge/Chrome，本次为隔离检查，不触发浏览器",
            "未做真实库 / 真实客户端 UI 验收，不能据此声明产品 PASS",
        ],
        "verdict": "pass" if (cases_ok and generic_ok) else "fail",
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())

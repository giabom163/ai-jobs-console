#!/usr/bin/env python3
"""贴吧 24 小时增量 + 严格发表时间窗口的定向隔离检查（只读、0 网络、0 模型、0 真实库写入）。

用法（在 worktree 根目录）：
  python -B tools/activity_checks.py --limit 3

`--limit` = 要执行的分组数量：**3 表示完整执行 A/B/C 三组全部场景**（默认），1 表示只跑 A 组。
单次只运行 `--limit 3`，全部 sqlite :memory: 夹具，不 touch 真实 sentiment.db、不用 tempdir 自动删除。

  A 严格发表时间窗口：只 time_quality=valid、真实 [start, end)、边界包含/排除、未知不回退 fetched_at；
    窗口 KPI 与 all 口径、负面 KPI 同窗口同分母、all 分类同口径同 now、unknown_time_ratio 不重复计数、
    topics 与 alertcenter 同一严格语义；
  B 贴吧滚动 24h 准入：无证据全待核、单点 500 待核、50/51、2h 下界、跨帖/字段、近似/未知、
    同刻冲突、回撤、未来/15min stale/基线窗外、重复观测与重复 record URL、id 优先于 url、来源识别；
  C 路由 / 前端契约（含失败清旧）/ server all 同口径 / 旧报告读取路径保留，
    并用托管 Node 跑 `node --check overview-charts.js`。

同时执行既有 selfcheck.py 的四项通用检查（changed_paths / artifacts / no_secrets / python_ast）。
本检查器只作辅助佐证，不能替代真实运行验收；未启动服务、未开浏览器，实机部分标记 BLOCKED。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import activity             # noqa: E402
import alertcenter          # noqa: E402
import storage              # noqa: E402
import topics               # noqa: E402

SELFCHECK = Path(
    "C:/Users/Lenovo/Documents/Codex/2026-10-14/seeker-agent-agent-agent/outputs/"
    "agent-bridge-installed/templates/selfcheck.py")
NODE = Path("C:/Users/Lenovo/.workbuddy/binaries/node/versions/22.22.2-3/node.exe")
GENERIC_CHECKS = ("changed_paths", "artifacts", "no_secrets", "python_ast")
TIEBA_URL = "https://tieba.baidu.com/hottopic/browse/topicList"
FIXED_NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def in_memory_db():
    db = storage.connect(":memory:")
    db.execute("INSERT INTO feeds(id,name,url,added_at) VALUES(1,'普通源','https://example.com/feed',?)",
               (storage.now(),))
    db.execute("INSERT INTO collection_runs(id,feed_id,kind,status,started_at,limit_n) "
               "VALUES('run1',1,'manual','succeeded',?,1)", (storage.now(),))
    db.commit()
    return db


def tieba_db():
    db = in_memory_db()
    db.execute("INSERT INTO feeds(id,name,url,added_at) VALUES(2,?,?,?)",
               ("OpenCLI-贴吧", TIEBA_URL, storage.now()))
    db.commit()
    return db


def add_record(db, rid, title, published_at, time_quality="valid", sentiment="中性",
               fetched_at=None, feed_id=1, canonical_url=None, source_id=None, model_sentiment=None):
    canonical = canonical_url or ("https://example.com/post/" + rid)
    db.execute("INSERT OR IGNORE INTO canonical_records(id,created_at,dedup_rule) VALUES(?,?,?)",
               ("canon" + rid, storage.now(), storage.RULE))
    db.execute(
        "INSERT INTO records(id,feed_id,source_id,title,summary,url,canonical_url,content_sha256,raw_sha256,"
        "raw_path,published_at,published_raw,fetched_at,time_quality,canonical_id,dedup_reason,run_id,"
        "sentiment,relevance_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, feed_id, source_id, title, "摘要", canonical, canonical, "c" + rid, "r" + rid, "p",
         published_at, published_at, fetched_at or storage.now(), time_quality, "canon" + rid,
         "independent", "run1", sentiment, "conflict"))
    if model_sentiment is not None:
        db.execute("INSERT INTO model_annotations(record_id,model,sentiment,topic,gender_related,opposition,"
                   "relevant,created_at,raw_json) VALUES(?,?,?,?,1,1,1,?,'{}')",
                   (rid, "test-model", model_sentiment, None, storage.now()))
    db.commit()


def add_evidence(db, eid, platform, platform_id, canonical_url, observed_at, value, precision="exact",
                 source_field="回复"):
    identity = storage.p1_identity(platform, platform_id, canonical_url)
    db.execute(
        "INSERT INTO p1_evidence(id,identity_key,platform,platform_id,url,canonical_url,title,source_name,"
        "evidence_kind,content,content_sha256,content_raw_sha256,content_source,fragment_start,fragment_end,"
        "observed_at,observed_raw,published_at,published_raw,time_quality,source_response_sha256,"
        "snapshot_sha256,snapshot_path,comment_value,comment_precision,comment_source_field,other_counts,"
        "group_label,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, identity, platform, platform_id, canonical_url, canonical_url, "证据标题", "贴吧", "post_body",
         "正文", "ch" + eid, "cr" + eid, "post_body", 0, 2, observed_at, observed_at, None, None, "valid",
         "0" * 64, "snap" + eid, "snapshots/" + eid + ".json", value, precision, source_field, "{}",
         "人工候选", storage.now()))
    db.commit()


def point(observed_at, value, precision="exact", source_field="回复"):
    return {"observed_at": iso(observed_at), "comment_value": value,
            "comment_precision": precision, "comment_source_field": source_field}


# ---------- 组 A：严格发表时间窗口 ----------

def group_a(limit=None):
    cases = []

    # A1 边界 [start, end) / 未知不回退 fetched_at / naive 拒绝
    start = FIXED_NOW - timedelta(hours=24)
    rows = [
        {"id": "r_start", "published_at": iso(start), "time_quality": "valid", "fetched_at": iso(FIXED_NOW)},
        {"id": "r_before", "published_at": iso(start - timedelta(seconds=1)), "time_quality": "valid",
         "fetched_at": iso(FIXED_NOW)},
        {"id": "r_end", "published_at": iso(FIXED_NOW), "time_quality": "valid", "fetched_at": iso(FIXED_NOW)},
        {"id": "r_future", "published_at": iso(FIXED_NOW + timedelta(hours=1)), "time_quality": "valid",
         "fetched_at": iso(FIXED_NOW)},
        {"id": "r_missing", "published_at": None, "time_quality": "missing", "fetched_at": iso(FIXED_NOW)},
        {"id": "r_invalid", "published_at": "not-a-date", "time_quality": "invalid", "fetched_at": iso(FIXED_NOW)},
        {"id": "r_tz", "published_at": "2026-10-19T09:00:00", "time_quality": "timezone_missing",
         "fetched_at": iso(FIXED_NOW)},
        {"id": "r_legacy", "published_at": iso(FIXED_NOW - timedelta(hours=1)), "time_quality": "legacy_unverified",
         "fetched_at": iso(FIXED_NOW)},
        {"id": "r_naive", "published_at": "2026-10-19T09:00:00", "time_quality": "valid",
         "fetched_at": iso(FIXED_NOW)},
    ]
    selected, excluded, unknown = activity.select(rows, start, FIXED_NOW)
    ids = [row["id"] for row, _ in selected]
    ok = (ids == ["r_start"] and excluded["history"] == 1 and excluded["future"] == 2
          and excluded["time_missing"] == 1 and excluded["time_invalid"] == 2
          and excluded["time_timezone_missing"] == 1 and excluded["time_legacy_unverified"] == 1
          and unknown == 5)
    cases.append({"id": "A1_boundary_quality_no_fallback", "ok": bool(ok),
                  "detail": {"selected": ids, "excluded": excluded, "unknown_total": unknown}})

    # A2 同一时刻不同 offset 视为同一窗口点（解析后比较，不做字符串排序）
    start = FIXED_NOW - timedelta(hours=1)
    same = FIXED_NOW - timedelta(minutes=30)
    rows = [{"id": "r_utc", "published_at": iso(same), "time_quality": "valid"},
            {"id": "r_plus8", "published_at": same.astimezone(timezone(timedelta(hours=8))).isoformat(),
             "time_quality": "valid"},
            {"id": "r_off", "published_at": iso(same - timedelta(hours=2)), "time_quality": "valid"}]
    selected, excluded, _ = activity.select(rows, start, FIXED_NOW)
    ids = sorted(row["id"] for row, _ in selected)
    ok = ids == ["r_plus8", "r_utc"] and excluded["history"] == 1
    cases.append({"id": "A2_offset_normalized", "ok": bool(ok), "detail": {"selected": ids}})

    # A3 窗口 KPI / all 保留全库 / 负面 KPI 同窗口同分母
    db = in_memory_db()
    now = datetime.now(timezone.utc)
    add_record(db, "r_in", "彩礼窗口内", iso(now - timedelta(hours=1)), sentiment="偏负面")
    add_record(db, "r_neg_model", "彩礼窗口内模型负面", iso(now - timedelta(hours=2)), sentiment="中性",
               model_sentiment="强负面")
    add_record(db, "r_out", "彩礼窗口外", iso(now - timedelta(hours=30)), sentiment="强负面")
    add_record(db, "r_missing", "彩礼未知时间", None, time_quality="missing", sentiment="偏负面")
    stats = storage.overview_stats(db, None, 24)
    window = stats["window"]
    keys_ok = all(k in stats for k in ("kpi", "trend", "platforms", "sentiment", "top_groups", "window"))
    ok = (stats["kpi"]["records"] == 2 and stats["kpi"]["negative"] == 2
          and window["scope"] == "24h" and window["time_basis"] == activity.TIME_BASIS
          and window["start"] is not None and window["end"] is not None
          and window["unknown_excluded"] == 1 and keys_ok)
    cases.append({"id": "A3_window_kpi_negative", "ok": bool(ok),
                  "detail": {"records": stats["kpi"]["records"], "negative": stats["kpi"]["negative"],
                             "window": window, "keys_ok": keys_ok}})

    all_stats = storage.overview_stats(db, None, None)
    ok = all_stats["kpi"]["records"] == 4 and all_stats["kpi"]["negative"] == 4
    cases.append({"id": "A4_all_keeps_full", "ok": bool(ok),
                  "detail": {"records": all_stats["kpi"]["records"], "negative": all_stats["kpi"]["negative"]}})
    db.close()

    # A5 topics 与 alertcenter 同一严格语义
    db = in_memory_db()
    now = datetime.now(timezone.utc)
    add_record(db, "r_ok", "彩礼窗口内话题", iso(now - timedelta(hours=1)))
    add_record(db, "r_missing", "彩礼未知时间话题", None, time_quality="missing")
    add_record(db, "r_old", "彩礼窗口外话题", iso(now - timedelta(hours=30)))
    cats = topics.categories(db, 24)
    recs = topics.records_for_category(db, 24, "彩礼/婚姻法")
    rule = {"match_kind": "keyword", "match_value": "彩礼"}
    hits_start = now - timedelta(hours=24)
    total, ids = alertcenter.rule_hits(db, rule, hits_start, now)
    ok = (cats["total_contents"] == 1 and recs["total"] == 1 and total == 1 and ids == ["r_ok"])
    cases.append({"id": "A5_topics_alert_strict", "ok": bool(ok),
                  "detail": {"topics_total": cats["total_contents"], "records_total": recs["total"],
                             "rule_hits": total, "rule_ids": ids}})
    db.close()

    # A6 all 分类/详情与 storage all 全库母体一致（valid+unknown+future 三类）
    db = in_memory_db()
    now = datetime.now(timezone.utc)
    add_record(db, "r_recent", "彩礼近期", iso(now - timedelta(hours=1)))
    add_record(db, "r_old", "彩礼很久以前", iso(now - timedelta(days=40)))
    add_record(db, "r_future", "彩礼未来", iso(now + timedelta(days=1)))
    add_record(db, "r_missing", "彩礼未知时间", None, time_quality="missing")
    all_stats = storage.overview_stats(db, None, None, now=now)
    all_cats = topics.categories(db, None, now=now)
    window_cats = topics.categories(db, 24, now=now)
    all_recs = topics.records_for_category(db, None, "__all__", now=now)
    titles = [item["title"] for item in all_recs["items"]]
    ok = (all_cats["total_contents"] == all_stats["kpi"]["records"] == 4
          and window_cats["total_contents"] == 1
          and all_recs["total"] == 4
          and {"彩礼未知时间", "彩礼未来"} <= set(titles)
          and titles[0] == "彩礼未来"
          and titles[-1] == "彩礼未知时间")
    cases.append({"id": "A6_all_categories_scope", "ok": bool(ok),
                  "detail": {"all_categories": all_cats["total_contents"],
                             "all_kpi_records": all_stats["kpi"]["records"],
                             "window_categories": window_cats["total_contents"],
                             "all_detail_total": all_recs["total"], "order": titles}})
    db.close()

    # A7 all 模式 unknown_time_ratio 不重复计数（分母为全库记录数）
    db = in_memory_db()
    now = datetime.now(timezone.utc)
    add_record(db, "r1", "彩礼一", iso(now - timedelta(hours=1)))
    add_record(db, "r2", "彩礼二", iso(now - timedelta(hours=2)))
    add_record(db, "r3", "彩礼未知", None, time_quality="missing")
    stats = storage.overview_stats(db, None, None, now=now)
    ok = (stats["kpi"]["records"] == 3 and stats["kpi"]["unknown_time"] == 1
          and stats["kpi"]["unknown_time_ratio"] == round(1 / 3, 4)
          and stats["kpi"]["unknown_time_basis"] == "whole_database"
          and "excluded_note" in stats["window"] and "trend_basis" in stats["window"]
          and stats["window"]["unknown_basis"] == "whole_database"
          and sum(row["records"] for row in stats["trend"]) == 2)   # trend 只含有有效发表时间的 2 条
    cases.append({"id": "A7_all_unknown_ratio", "ok": bool(ok),
                  "detail": {"records": stats["kpi"]["records"],
                             "unknown_time": stats["kpi"]["unknown_time"],
                             "ratio": stats["kpi"]["unknown_time_ratio"],
                             "unknown_time_basis": stats["kpi"]["unknown_time_basis"],
                             "trend_sum": sum(row["records"] for row in stats["trend"])}})
    db.close()

    return cases


# ---------- 组 B：贴吧 24h 准入 ----------

def group_b(limit=None):
    cases = []

    # B1 无证据全部待核 + 重复 record URL 不双计 + 贴吧来源别名识别
    db = tieba_db()
    now = datetime.now(timezone.utc)
    add_record(db, "r_a", "贴吧帖 A", None, time_quality="missing", feed_id=2,
               canonical_url="https://tieba.baidu.com/p/100")
    add_record(db, "r_a2", "贴吧帖 A 重采", None, time_quality="missing", feed_id=2,
               canonical_url="https://tieba.baidu.com/p/100")
    add_record(db, "r_b", "贴吧帖 B", None, time_quality="missing", feed_id=2,
               canonical_url="https://tieba.baidu.com/p/101")
    payload = activity.tieba_activity(db, now=now)
    ok = (payload["summary"]["records_total"] == 2 and payload["summary"]["qualified"] == 0
          and payload["summary"]["pending"] == 2
          and payload["db_writes"] == 0 and payload["network_requests"] == 0)
    cases.append({"id": "B1_no_evidence_all_pending_dedup", "ok": bool(ok),
                  "detail": payload["summary"]})
    # 非贴吧来源不计入
    add_record(db, "r_other", "普通帖", None, time_quality="missing",
               canonical_url="https://example.com/other")
    payload2 = activity.tieba_activity(db, now=now)
    cases.append({"id": "B1b_non_tieba_excluded", "ok": payload2["summary"]["records_total"] == 2,
                  "detail": payload2["summary"]})
    db.close()

    # B2 单点累计 500 → 待核 single_point
    rows = [point(FIXED_NOW - timedelta(minutes=5), 500)]
    result = activity.admit(rows, FIXED_NOW)
    cases.append({"id": "B2_single_point_pending", "ok": result["status"] == "pending"
                  and result["reason"] == "single_point" and result["observed_delta"] is None,
                  "detail": result})

    # B3 两实测点净增 50 不达标、51 达标
    base = [point(FIXED_NOW - timedelta(hours=2), 100), point(FIXED_NOW - timedelta(minutes=5), 150)]
    r50 = activity.admit(base, FIXED_NOW)
    up = [point(FIXED_NOW - timedelta(hours=2), 100), point(FIXED_NOW - timedelta(minutes=5), 151)]
    r51 = activity.admit(up, FIXED_NOW)
    cases.append({"id": "B3_threshold_50_51", "ok": r50["status"] == "pending" and r50["observed_delta"] == 50
                  and r51["status"] == "qualified" and r51["observed_delta"] == 51
                  and r51["is_full_24h"] is False and r51["observed_span_seconds"] == 6900.0,
                  "detail": {"delta50": r50, "delta51": r51}})

    # B4 字段变化 / 跨帖 / 近似 / 未知 / 同刻冲突 / 回撤
    field = [point(FIXED_NOW - timedelta(hours=2), 100, source_field="回复"),
             point(FIXED_NOW - timedelta(minutes=5), 200, source_field="评论")]
    approx = [point(FIXED_NOW - timedelta(hours=2), 100),
              point(FIXED_NOW - timedelta(minutes=5), 900, precision="approximate")]
    unknown = [point(FIXED_NOW - timedelta(hours=2), 100),
               point(FIXED_NOW - timedelta(minutes=5), None, precision="unknown")]
    conflict = [point(FIXED_NOW - timedelta(minutes=5), 100),
                point(FIXED_NOW - timedelta(minutes=5), 140)]
    retract = [point(FIXED_NOW - timedelta(hours=3), 200), point(FIXED_NOW - timedelta(hours=2), 250),
               point(FIXED_NOW - timedelta(minutes=5), 240)]
    checks = {"source_changed": activity.admit(field, FIXED_NOW),
              "approximate": activity.admit(approx, FIXED_NOW),
              "unknown": activity.admit(unknown, FIXED_NOW),
              "same_time_conflict": activity.admit(conflict, FIXED_NOW),
              "retraction": activity.admit(retract, FIXED_NOW)}
    ok = all(v["status"] == "pending" for v in checks.values()) \
        and checks["source_changed"]["reason"] == "source_changed" \
        and checks["approximate"]["reason"] == "approximate" \
        and checks["unknown"]["reason"] == "unknown" \
        and checks["same_time_conflict"]["reason"] == "same_time_conflict" \
        and checks["retraction"]["reason"] == "retraction"
    cases.append({"id": "B4_abnormal_points_block", "ok": bool(ok),
                  "detail": {k: {"status": v["status"], "reason": v["reason"]} for k, v in checks.items()}})

    # B5 未来 / stale / 基线窗外不得冒充整窗
    future = [point(FIXED_NOW - timedelta(hours=1), 100), point(FIXED_NOW + timedelta(minutes=1), 999)]
    stale = [point(FIXED_NOW - timedelta(hours=2), 100), point(FIXED_NOW - timedelta(minutes=20), 999)]
    outside = [point(FIXED_NOW - timedelta(hours=25), 10), point(FIXED_NOW - timedelta(hours=20), 20),
               point(FIXED_NOW - timedelta(minutes=5), 80)]
    only_baseline = [point(FIXED_NOW - timedelta(hours=25), 10), point(FIXED_NOW - timedelta(minutes=5), 80)]
    rf, rs = activity.admit(future, FIXED_NOW), activity.admit(stale, FIXED_NOW)
    ro, rb = activity.admit(outside, FIXED_NOW), activity.admit(only_baseline, FIXED_NOW)
    ok = (rf["reason"] == "future_last" and rs["reason"] == "stale_last"
          and ro["status"] == "qualified" and ro["observed_delta"] == 60 and ro["is_full_24h"] is False
          and ro["observed_start"] == iso(FIXED_NOW - timedelta(hours=20))
          and rb["status"] == "pending" and rb["reason"] == "no_window_observation")
    cases.append({"id": "B5_freshness_and_out_of_window_baseline", "ok": bool(ok),
                  "detail": {"future": rf["reason"], "stale": rs["reason"], "outside": ro, "only_baseline": rb}})

    # B6 重复观测合并（同刻同值不双计）
    dup = [point(FIXED_NOW - timedelta(hours=2), 100), point(FIXED_NOW - timedelta(minutes=5), 160),
           point(FIXED_NOW - timedelta(minutes=5), 160)]
    rd = activity.admit(dup, FIXED_NOW)
    cases.append({"id": "B6_duplicate_observation_merged", "ok": rd["points_used"] == 2
                  and rd["observed_delta"] == 60, "detail": rd})

    # B7 身份匹配：优先 platform_id，缺 id 才用 canonical_url；不跨帖借历史
    db = tieba_db()
    now = datetime.now(timezone.utc)
    add_record(db, "r_id", "贴吧帖有ID", None, time_quality="missing", feed_id=2, source_id="P1",
               canonical_url="https://tieba.baidu.com/p/200")
    add_evidence(db, "e_id1", "tieba", "P1", "https://tieba.baidu.com/p/999",
                 iso(now - timedelta(hours=2)), 100)
    add_evidence(db, "e_id2", "tieba", "P1", "https://tieba.baidu.com/p/999",
                 iso(now - timedelta(minutes=5)), 200)
    # 另一原帖有 ID 但历史只挂在 URL 上：按 id 优先，不得借用 url 历史
    add_record(db, "r_idonly", "贴吧帖ID无ID历史", None, time_quality="missing", feed_id=2, source_id="P2",
               canonical_url="https://tieba.baidu.com/p/300")
    add_evidence(db, "e_url1", "tieba", None, "https://tieba.baidu.com/p/300",
                 iso(now - timedelta(hours=2)), 100)
    add_evidence(db, "e_url2", "tieba", None, "https://tieba.baidu.com/p/300",
                 iso(now - timedelta(minutes=5)), 900)
    payload = activity.tieba_activity(db, now=now)
    by_label = {item["label"]: item for item in payload["items"]}
    ok = (by_label["贴吧帖有ID"]["status"] == "qualified" and by_label["贴吧帖有ID"]["observed_delta"] == 100
          and by_label["贴吧帖ID无ID历史"]["status"] == "pending"
          and by_label["贴吧帖ID无ID历史"]["reason"] == "no_observation")
    cases.append({"id": "B7_identity_priority_id_over_url", "ok": bool(ok),
                  "detail": {k: {"status": v["status"], "reason": v["reason"],
                                 "delta": v["observed_delta"]} for k, v in by_label.items()}})
    db.close()

    # B8 贴吧来源识别：优先 feed.url（tieba.baidu.com），兼容 feeds.name 别名
    db = in_memory_db()
    db.execute("INSERT INTO feeds(id,name,url,added_at) VALUES(2,?,?,?)",
               ("自定义分组", TIEBA_URL, storage.now()))                     # 名字不像，但 url 是贴吧
    db.execute("INSERT INTO feeds(id,name,url,added_at) VALUES(3,?,?,?)",
               ("贴吧", "https://example.com/not-tieba", storage.now()))     # 名字是贴吧别名
    db.execute("INSERT INTO feeds(id,name,url,added_at) VALUES(4,?,?,?)",
               ("toutiao-hot", "https://www.toutiao.com/hot", storage.now()))
    db.commit()
    now = datetime.now(timezone.utc)
    add_record(db, "r_url", "url识别贴吧", None, time_quality="missing", feed_id=2,
               canonical_url="https://tieba.baidu.com/p/400")
    add_record(db, "r_name", "name识别贴吧", None, time_quality="missing", feed_id=3,
               canonical_url="https://tieba.baidu.com/p/401")
    add_record(db, "r_non", "非贴吧", None, time_quality="missing", feed_id=4,
               canonical_url="https://www.toutiao.com/a/1")
    payload = activity.tieba_activity(db, now=now)
    ok = payload["summary"]["records_total"] == 2
    cases.append({"id": "B8_tieba_source_detection", "ok": bool(ok), "detail": payload["summary"]})
    db.close()

    return cases


# ---------- 组 C：路由 / 前端契约 / 旧报告路径 / node --check ----------

def node_check():
    node = str(NODE) if NODE.is_file() else "node"
    try:
        proc = subprocess.run([node, "--check", str(ROOT / "overview-charts.js")],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except OSError as exc:
        return {"ok": False, "ran": False, "error": f"{type(exc).__name__}: {exc}", "node": node}
    return {"ok": proc.returncode == 0, "ran": True, "exit": proc.returncode, "node": node,
            "stderr": (proc.stderr or "")[:400]}


def group_c(limit=None):
    cases = []
    server_src = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
    js_src = (ROOT / "overview-charts.js").read_text(encoding="utf-8", errors="replace")

    route_ok = ('/api/tieba-activity' in server_src and 'activity.tieba_activity' in server_src
                and 'active_topics"] = None' in server_src)
    cases.append({"id": "C1_routes", "ok": bool(route_ok),
                  "detail": {"tieba_route": '/api/tieba-activity' in server_src,
                             "active_topics_null": 'active_topics"] = None' in server_src}})

    contract = {
        "window_label": "窗口内新发表记录" in js_src,
        "today_label": "今日新采入库" in js_src,
        "lower_bound_label": "24小时内已观测净增下界" in js_src,
        "token_guard": "loadToken" in js_src and "token !== loadToken" in js_src,
        "tieba_fetch": "/api/tieba-activity" in js_src,
        "error_clears": "读取失败" in js_src and "board.textContent = ''" in js_src,
        "no_fake_zero": "kpi.active_topics || 0" not in js_src,
        "no_derived_zero": "total - qualified - pending" not in js_src,
        "count_placeholder": "countText" in js_src,
        "no_obs_subset": "reasons.no_observation" in js_src,
    }
    cases.append({"id": "C2_frontend_contract", "ok": all(contract.values()), "detail": contract})

    scope_ok = ("cat_hours = hours if hours else (days * 24 if days else None)" in server_src
                and "topics.categories(db, cat_hours, now=end)" in server_src
                and "overview_stats(db, days, hours, now=end)" in server_src)
    cases.append({"id": "C3_server_all_same_scope", "ok": bool(scope_ok),
                  "detail": {"cat_hours_line": "cat_hours = hours if hours else" in server_src,
                             "categories_now": "topics.categories(db, cat_hours, now=end)" in server_src,
                             "overview_now": "overview_stats(db, days, hours, now=end)" in server_src}})

    import reporting
    import admin_features
    legacy_ok = callable(getattr(admin_features, "load_verified_report", None)) \
        and callable(getattr(reporting, "build_snapshot", None))
    cases.append({"id": "C4_legacy_report_path", "ok": bool(legacy_ok),
                  "detail": {"load_verified_report": legacy_ok}})

    result = node_check()
    cases.append({"id": "C5_node_check", "ok": bool(result["ok"]), "detail": result})
    return cases


# ---------- selfcheck 通用四项 ----------

def run_selfcheck(worktree):
    if not SELFCHECK.is_file():
        return {"ran": False, "error": "selfcheck.py 不存在", "path": str(SELFCHECK)}
    cmd = [sys.executable, "-B", str(SELFCHECK), "--kind", "docs", "--worktree", str(worktree),
           "--allow", "activity.py,storage.py,topics.py,alertcenter.py,server.py,overview-charts.js,tools/,docs/",
           "--artifacts",
           "activity.py,storage.py,topics.py,alertcenter.py,server.py,overview-charts.js,"
           "tools/activity_checks.py,docs/activity-24h.md"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    try:
        raw = json.loads(proc.stdout)
    except ValueError:
        return {"ran": True, "exit": proc.returncode, "parse_error": True,
                "stdout": (proc.stdout or "")[:2000], "stderr": (proc.stderr or "")[:500]}
    generic = {c["id"]: c for c in raw.get("checks", []) if c["id"] in GENERIC_CHECKS}
    other = {c["id"]: c for c in raw.get("checks", []) if c["id"] not in GENERIC_CHECKS}
    return {"ran": True, "exit": proc.returncode, "generic": generic, "kind_specific_not_used": other,
            "note": "仅采用 changed_paths/artifacts/no_secrets/python_ast 四项；docs_only 会因代码修改 fail，"
                    "本脚本不采用它。"}


def _utf8_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def main():
    _utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3,
                    help="要执行的分组数量：3 = 完整 A/B/C 三组全部场景（默认），1 = 只跑 A 组")
    ap.add_argument("--worktree", default=str(ROOT))
    args = ap.parse_args()
    if args.limit < 1:
        print(json.dumps({"error": "limit 必须为正整数"}, ensure_ascii=False))
        return 2

    cases = []
    groups = [("A", group_a), ("B", group_b), ("C", group_c)][:args.limit]
    for group, fn in groups:
        for case in fn():
            case["group"] = group
            cases.append(case)

    selfcheck = run_selfcheck(Path(args.worktree).resolve())
    cases_ok = all(case["ok"] for case in cases)
    generic_ok = True
    if selfcheck.get("ran") and not selfcheck.get("parse_error"):
        generic_ok = all(c["ok"] for c in selfcheck.get("generic", {}).values())
    elif not selfcheck.get("ran"):
        generic_ok = False

    report = {
        "checker": "tools/activity_checks.py",
        "limit": args.limit,
        "cases": cases,
        "selfcheck": selfcheck,
        "cases_all_ok": cases_ok,
        "selfcheck_generic_ok": generic_ok,
        "unverified": [
            "真实客户端 UI（贴吧增量区渲染、窗口切换、错误清旧）未验证：本次不启动服务/浏览器，实机 BLOCKED",
            "真实库无 p1 贴吧历史时的全 pending 结论未在真实库验证：本次用内存夹具",
            "两次独立只读复审由他人执行，本脚本不代替审查",
        ],
        "verdict": "pass" if (cases_ok and generic_ok) else "fail",
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())

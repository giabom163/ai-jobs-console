#!/usr/bin/env python3
"""时间解析/指定日采集的定向隔离检查（纯内存、0 真实网络、0 真实库写入）。

用法：python -B tools/time_ingest_checks.py --limit 3
`--limit` = 执行的分组数：3 = A/B/C 全跑（默认），1 = 只跑 A。

  A 解析器保守性：缺年份不猜、ISO/RFC 带时区 valid、naive/相对/不完整年月不伪 valid、pubdate 严格整数；
  B 上海指定日边界：00:00 一次性换 UTC、[start,end) 边界、未来日期拒绝、非法格式拒绝、互斥拒绝；
  C 请求预算/错误/去重/隔离：20s 间隔闸门、上限、429 冷却、HTTP/业务错误非空、dry-run 不碰原库。

本检查器只作辅助佐证，不能替代主线程真实采集；未实采、未开浏览器，实机标记 BLOCKED。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import storage  # noqa: E402
import collect_bilibili_24h as collect  # noqa: E402

SH = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 19, 0, 0, 0, tzinfo=SH)


def case(cid, ok, detail=None):
    return {"id": cid, "ok": bool(ok), "detail": detail or {}}


# ---------- A 解析器保守性 ----------

def group_a():
    pub = storage.publication
    iso_ok = pub("2026-10-18T12:30:00+08:00")
    rfc_ok = pub("Mon, 18 Oct 2026 12:30:00 +0800")
    guess = pub("10月18日12:30")
    naive = pub("2026-10-18")
    partial = pub("2026-10")
    rel = pub("3 hours ago")
    return [
        case("A1_iso_rfc_valid",
             iso_ok[1] == "valid" and iso_ok[0] is not None
             and rfc_ok[1] == "valid" and rfc_ok[0] is not None,
             {"iso": iso_ok, "rfc": rfc_ok}),
        case("A2_no_year_not_guessed", guess == (None, "invalid"), {"actual": guess}),
        case("A3_naive_and_partial", naive[1] == "timezone_missing" and partial[1] == "invalid",
             {"naive": naive, "partial": partial}),
        case("A4_relative_invalid", rel == (None, "invalid"), {"actual": rel}),
        case("A5_pubdate_strict_int",
             collect.parse_pubdate(1729) == (1729, None)
             and collect.parse_pubdate(True)[1] == "pubdate_not_int"
             and collect.parse_pubdate("1729")[1] == "pubdate_not_int"
             and collect.parse_pubdate(None)[1] == "pubdate_missing",
             {"int": collect.parse_pubdate(1729), "bool": collect.parse_pubdate(True),
              "str": collect.parse_pubdate("1729"), "none": collect.parse_pubdate(None)}),
    ]


# ---------- B 上海指定日边界 ----------

def group_b():
    w = collect.date_window("2026-10-18", NOW)
    start_ok = w["start_utc"] == datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)
    end_ok = w["end_utc"] == datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc)
    span_ok = (w["end_utc"] - w["start_utc"]) == timedelta(days=1)
    cases = [
        case("B1_shanghai_midnight_utc", start_ok and end_ok and span_ok,
             {"start_utc": w["start_utc"].isoformat(), "end_utc": w["end_utc"].isoformat()}),
    ]
    try:
        collect.date_window("2026-12-31", NOW)
        cases.append(case("B2_future_rejected", False, {"actual": "no_error"}))
    except ValueError as exc:
        cases.append(case("B2_future_rejected", True, {"msg": str(exc)}))
    bad = []
    for text in ("2026-13-40", "20261018", "2026-1-8"):
        try:
            collect.date_window(text, NOW)
            bad.append(text)
        except ValueError:
            pass
    cases.append(case("B3_bad_format_rejected", not bad, {"accepted": bad}))
    try:
        collect.resolve_window("2026-10-18", 24, None, NOW)
        cases.append(case("B4_mutual_exclusion", False, {"actual": "no_error"}))
    except ValueError as exc:
        cases.append(case("B4_mutual_exclusion", True, {"msg": str(exc)}))
    return cases


# ---------- C 请求预算 / 错误 / 去重 / 隔离 ----------

class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def _fake_get_factory(search_payload=None, nav_payload=None):
    def fake_get(url):
        if "nav" in url:
            return NAV_OK if nav_payload is None else nav_payload
        return search_payload
    return fake_get


class Patch:
    def __enter__(self):
        self.orig = collect._get
        return self

    def __exit__(self, *exc):
        collect._get = self.orig


NAV_OK = {"code": 0, "data": {"wbi_img": {
    "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
    "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png"}}}


def group_c(tmp):
    fc = FakeClock()
    cases = []

    # C1 20s 全局间隔 + 上限（纯闸门，不真实请求）
    collect._reset_requests(2, 20.0, clock=fc.now, sleep_fn=fc.sleep)
    collect._reserve_request()
    collect._reserve_request()
    gate = fc.slept and fc.slept[0] >= 20.0
    try:
        collect._reserve_request()
        budget_blocked = False
    except collect.RequestBudgetError:
        budget_blocked = True
    cases.append(case("C1_interval_and_max", bool(gate) and budget_blocked,
                      {"slept": fc.slept, "blocked": budget_blocked}))

    # C2 429 冷却记录（>=300s）+ 冷却内再次请求被拒
    collect._reset_requests(5, 0.0, clock=fc.now, sleep_fn=fc.sleep)
    orig_get = collect._get

    def raising_get(url):
        raise collect.RateLimitError(status=429, body="too many")
    collect._get = raising_get
    try:
        code = collect.main(["--limit", "3", "--date", "2026-10-18", "--db", str(tmp / "c2.db"),
                             "--snapshot-root", str(tmp), "--output", str(tmp / "c2.json")])
    finally:
        collect._get = orig_get
    report = json.loads((tmp / "c2.json").read_text(encoding="utf-8"))
    cases.append(case("C2_429_cooldown",
                      code == 3 and report["status"] == "cooldown"
                      and report["cooldown_seconds"] == 300 and report["http_status"] == 429,
                      {"exit": code, "status": report["status"],
                       "cooldown_seconds": report["cooldown_seconds"]}))

    # C3 HTTP 500 保留真实 status/body，不变成空列表
    collect._reset_requests(5, 0.0, clock=fc.now, sleep_fn=fc.sleep)
    collect._get = lambda url: (_ for _ in ()).throw(collect.HttpStatusError(500, "server boom"))
    try:
        code = collect.main(["--limit", "3", "--date", "2026-10-18", "--db", str(tmp / "c3.db"),
                             "--snapshot-root", str(tmp), "--output", str(tmp / "c3.json")])
    finally:
        collect._get = orig_get
    report = json.loads((tmp / "c3.json").read_text(encoding="utf-8"))
    cases.append(case("C3_http500_status_body",
                      code == 3 and report["stage"] == "http" and report["http_status"] == 500
                      and report["body"] == "server boom",
                      {"exit": code, "http_status": report.get("http_status"), "body": report.get("body")}))

    # C4 业务非 0 错误码不得当空列表
    collect._reset_requests(5, 0.0, clock=fc.now, sleep_fn=fc.sleep)
    collect._get = _fake_get_factory(search_payload={"code": -400, "message": "err"})
    try:
        code = collect.main(["--limit", "3", "--date", "2026-10-18", "--db", str(tmp / "c4.db"),
                             "--snapshot-root", str(tmp), "--output", str(tmp / "c4.json")])
    finally:
        collect._get = orig_get
    report = json.loads((tmp / "c4.json").read_text(encoding="utf-8"))
    cases.append(case("C4_business_error_not_empty",
                      code == 3 and report["stage"] == "business" and report["error_code"] == -400,
                      {"exit": code, "stage": report.get("stage"), "error_code": report.get("error_code")}))

    # C5 dry-run 隔离：不碰原库、不写原 ROOT 快照
    orig_db = ROOT / "sentiment.db"
    before = orig_db.read_bytes() if orig_db.is_file() else None
    max_req = 2
    epoch = int(datetime(2026, 10, 18, 12, 0, tzinfo=SH).timestamp())
    item = {"bvid": "BV1test", "title": "性别对立 与 彩礼 之争", "pubdate": epoch, "play": 123}
    collect._reset_requests(max_req, 0.0, clock=fc.now, sleep_fn=fc.sleep)
    collect._get = _fake_get_factory(search_payload={"code": 0, "data": {"result": [item]}})
    try:
        code = collect.main(["--limit", "3", "--date", "2026-10-18", "--dry-run",
                             "--db", str(tmp / "iso.db"), "--snapshot-root", str(tmp),
                             "--max-requests", "2", "--output", str(tmp / "c5.json")])
    finally:
        collect._get = orig_get
    report = json.loads((tmp / "c5.json").read_text(encoding="utf-8"))
    after = orig_db.read_bytes() if orig_db.is_file() else None
    untouched = before == after
    no_snap = not (tmp / "snapshots").exists()
    cases.append(case("C5_dryrun_isolated",
                      code == 0 and report["status"] == "dry_run"
                      and report["candidates_found"] == 1 and report["items"][0]["pubdate"] == epoch
                      and untouched and no_snap and not (tmp / "iso.db").exists(),
                      {"exit": code, "found": report.get("candidates_found"),
                       "orig_db_untouched": untouched, "no_snapshot": no_snap}))

    # C6 已有原帖 URL 去重
    dedup_db = tmp / "dedup.db"
    dbb = storage.connect(dedup_db)
    try:
        dbb.execute("INSERT INTO feeds(id,name,url,added_at) VALUES(1,'bilibili-search','https://search.bilibili.com/all?keyword=',?)",
                    (storage.now(),))
        dbb.execute("INSERT INTO collection_runs(id,feed_id,kind,status,started_at,limit_n) "
                    "VALUES('run1',1,'x','succeeded',?,1)", (storage.now(),))
        canon = storage.canonical_url("https://www.bilibili.com/video/BV1test")
        dbb.execute("INSERT INTO canonical_records VALUES('c1',?,?)", (storage.now(), storage.RULE))
        dbb.execute(
            "INSERT INTO records(id,feed_id,source_id,title,summary,url,canonical_url,content_sha256,raw_sha256,"
            "raw_path,published_at,published_raw,fetched_at,time_quality,canonical_id,dedup_reason,run_id,"
            "sentiment,relevance_tier,simhash) VALUES('r1',1,'BV1test','x','','u',?,'ch','rh','p',NULL,NULL,?,"
            "'missing','c1','independent','run1','中性','conflict',?)",
            (canon, storage.now(), f"{storage.simhash64(storage.normalized_text('性别对立 与 彩礼 之争') + ' '):016x}"))
        dbb.commit()
    finally:
        dbb.close()
    collect._reset_requests(2, 0.0, clock=fc.now, sleep_fn=fc.sleep)
    collect._get = _fake_get_factory(search_payload={"code": 0, "data": {"result": [item]}})
    try:
        code = collect.main(["--limit", "3", "--date", "2026-10-18", "--dry-run",
                             "--db", str(dedup_db), "--snapshot-root", str(tmp),
                             "--max-requests", "2", "--output", str(tmp / "c6.json")])
    finally:
        collect._get = orig_get
    report = json.loads((tmp / "c6.json").read_text(encoding="utf-8"))
    cases.append(case("C6_existing_url_dedup",
                      code == 0 and report["candidates_found"] == 0
                      and report["filters"].get("dup_existing", 0) >= 1,
                      {"exit": code, "found": report.get("candidates_found"), "filters": report.get("filters")}))
    return cases


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3, help="执行分组数：3=全跑（默认）")
    args = ap.parse_args()
    if args.limit < 1:
        print(json.dumps({"error": "limit 必须为正整数"}, ensure_ascii=False))
        return 2

    import tempfile
    cases = []
    groups = [("A", lambda: group_a()), ("B", lambda: group_b())]
    for name, fn in groups[:args.limit]:
        for c in fn():
            c["group"] = name
            cases.append(c)
    if args.limit >= 3:
        with tempfile.TemporaryDirectory() as td:
            for c in group_c(Path(td)):
                c["group"] = "C"
                cases.append(c)

    all_ok = all(c["ok"] for c in cases)
    report = {"checker": "tools/time_ingest_checks.py", "limit": args.limit, "cases": cases,
              "cases_all_ok": all_ok,
              "unverified": ["真实网络采集/100条/真实库写入/UI 未执行：主线程负责，实机 BLOCKED",
                             "两次独立只读复审由他人执行，本脚本不代替审查"],
              "verdict": "pass" if all_ok else "fail"}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

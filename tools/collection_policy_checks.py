"""定向检查（纯模拟 + 一次真实 Windows spawn worker/父 writer 隔离验证）。

三组均有必要断言，`--limit` 只控制 spawn 场景的 fixture 行数（默认 3），
**不截断** A/B/C 断言用例：
  A 本轮真实漏网正反例 + 上轮误杀正例 + gender-only 刑案不入正式库；
  B 时间锚点/边界/缺年与 ID 派生拒绝；
  C 多 worker 预算/唯一写入/429/partial + 真实 spawn worker 与父唯一 writer。

不调用真实源/模型；spawn 场景只用临时目录内的 DOM fixture 与本工作区外的临时库，
绝不写真实库。用法：python tools/collection_policy_checks.py --limit 3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collection_policy  # noqa: E402
import content_filter  # noqa: E402
import relevance  # noqa: E402
from tools import collect_multiplatform as mp  # noqa: E402


def _workdir():
    """临时证据放工作区子目录（非系统 TEMP）。"""
    path = ROOT / ("_checks_tmp_" + str(os.getpid()))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _check(group, name, ok, detail=""):
    return {"group": group, "name": name, "ok": bool(ok), "detail": detail}


def group_a():
    bad = [
        ("为了基尼奇攒彩礼的第四百二十七天", "game"),
        ("静待达妮卡｜攒彩礼打卡第53期", "game"),
        ("【陆远音声】被家暴去找前男友", "audio"),
        ("攀比彩礼的代价 #ai故事 #听别人说故事感悟自己的人 【AI全民制作人】", "fiction"),
        ("【2~3季】为了气心上人，我把自己以八毛钱的的彩礼嫁给了村里的小混混", "fiction"),
        ("爱吸血女儿还想偷偷把她送去当尼姑 《刺客信条：影》初见流程实况解说", "game"),
        ("【抢先版】去离婚路上他看到了未来，痛悔家暴堕落，跪求妻子别走（2）", "fiction"),
    ]
    # 上轮误杀正例（首审 P1-2）+ 真实议题正例：都不得判为噪声。
    positives = [
        ("现在年轻人攒彩礼有多难？", "issue"),
        ("女玩家通关率被质疑：游戏圈性别歧视", "conflict"),
        ("女性职场打卡", "issue"),
        ("为什么会有人觉得bl厌女？", "conflict"),
        ("原神女性角色物化与性别歧视争议", "conflict"),
        ("时代如何助长爱泼斯坦？复盘90-00年代的厌女文化与受害者羞辱", "conflict"),
        ("男女退休年龄是否应该统一？", "issue"),
        ("关于彩礼新规下的婚育政策讨论", "issue"),
    ]
    gender_only = [("女子手机上登录前男友账号以1314等金额转走20万！", "unrelated")]

    out = []
    for title, noise in bad:
        reason = content_filter.noise_reason(title)
        tier = relevance.classify_tier(title)
        out.append(_check("A", "bad:" + title[:16],
                          reason == noise and tier == "unrelated",
                          f"noise={reason}(want {noise}) tier={tier}"))
    for title, tier in positives:
        reason = content_filter.noise_reason(title)
        actual = relevance.classify_tier(title)
        out.append(_check("A", "positive:" + title[:16],
                          reason is None and actual == tier,
                          f"noise={reason} tier={actual}(want {tier})"))
    for title, tier in gender_only:
        out.append(_check("A", "gender-only:" + title[:12],
                          relevance.classify_tier(title) == tier,
                          f"tier={relevance.classify_tier(title)}"))
    # 正式入库门拒绝 gender-only 普通刑案（require=substantive）。
    d = collection_policy.gate_candidate(
        title="女子手机上登录前男友账号转走20万！", url="https://www.bilibili.com/video/BVcrawl1",
        published=collection_policy.normalize_published(1792387500, mode="epoch"))
    out.append(_check("A", "gate_reject_gender_only_crime",
                      not d["accepted"] and d["reason"] == "not_substantive",
                      f"accepted={d['accepted']} reason={d['reason']}"))
    d2 = collection_policy.gate_candidate(
        title="反对性别歧视与家暴的婚育政策讨论", url="https://www.bilibili.com/video/BVok1",
        published=collection_policy.normalize_published(1792387500, mode="epoch"))
    out.append(_check("A", "gate_accept_substantive", d2["accepted"], f"reason={d2['reason']}"))
    return out


def group_b():
    observed = "2026-10-18T20:00:00+08:00"
    cases = [
        ("epoch", lambda: collection_policy.normalize_published(1792387500, mode="epoch"),
         lambda r: r["accepted"] and r["precision"] == "second"),
        ("epoch_bool", lambda: collection_policy.normalize_published(True, mode="epoch"),
         lambda r: not r["accepted"]),
        ("epoch_huge", lambda: collection_policy.normalize_published(10 ** 18, mode="epoch"),
         lambda r: not r["accepted"]),
        ("iso_tz", lambda: collection_policy.normalize_published("2026-10-18T14:04:18+00:00", mode="iso"),
         lambda r: r["accepted"]),
        ("iso_no_tz", lambda: collection_policy.normalize_published("2026-10-18T14:04:18", mode="iso"),
         lambda r: not r["accepted"] and r["reason"] == "iso_timezone_missing"),
        ("rel_daytime", lambda: collection_policy.normalize_published(
            "昨天 18:35", mode="relative", observed_at=observed,
            source_timezone="Asia/Shanghai", source_field="published_at", precision="minute"),
         lambda r: r["accepted"] and r["published_at"] == "2026-10-17T10:35:00+00:00"),
        ("rel_daytime_no_precision", lambda: collection_policy.normalize_published(
            "昨天 18:35", mode="relative", observed_at=observed,
            source_timezone="Asia/Shanghai", source_field="published_at"),
         lambda r: r["accepted"] and r["precision"] == "minute"),
        ("rel_bad_hour", lambda: collection_policy.normalize_published(
            "昨天 25:00", mode="relative", observed_at=observed,
            source_timezone="Asia/Shanghai", source_field="published_at", precision="minute"),
         lambda r: not r["accepted"] and r["reason"] == "invalid_time_of_day"),
        ("rel_day_only", lambda: collection_policy.normalize_published(
            "昨天", mode="relative", observed_at=observed,
            source_timezone="Asia/Shanghai", source_field="published_at", precision="day"),
         lambda r: not r["accepted"] and r["reason"] == "day_precision_only"),
        ("rel_no_anchor", lambda: collection_policy.normalize_published(
            "3 小时前", mode="relative", source_timezone="Asia/Shanghai",
            source_field="published_at", precision="hour"),
         lambda r: not r["accepted"] and r["reason"] == "relative_missing_observed_at"),
        ("rel_bad_tz", lambda: collection_policy.normalize_published(
            "3 小时前", mode="relative", observed_at=observed,
            source_timezone="America/New_York", source_field="published_at", precision="hour"),
         lambda r: not r["accepted"] and r["reason"] == "relative_unknown_timezone"),
        ("rel_edit_field", lambda: collection_policy.normalize_published(
            "3 小时前", mode="relative", observed_at=observed,
            source_timezone="Asia/Shanghai", source_field="edited_at", precision="hour"),
         lambda r: not r["accepted"] and r["reason"] == "relative_edit_field"),
        ("id_derived", lambda: collection_policy.normalize_published(
            "2026-10-18T14:00:00+00:00", mode="iso", derived_from="id"),
         lambda r: not r["accepted"] and r["reason"] == "id_derived"),
        ("missing_year", lambda: collection_policy.normalize_published("10月18日 12:30"),
         lambda r: not r["accepted"] and r["reason"] == "missing_year"),
    ]
    out = []
    for name, fn, predicate in cases:
        try:
            result = fn()
            out.append(_check("B", name, predicate(result),
                              f"accepted={result['accepted']} reason={result['reason']} precision={result['precision']}"))
        except Exception as exc:  # noqa: BLE001
            out.append(_check("B", name, False, f"raised {type(exc).__name__}: {exc}"))
    return out


def group_c_unit(limit):
    t0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    out = []

    def budget():
        ledger = {"version": 1, "last_call_at": None, "sources": {}}
        gate = mp.CallGate(ledger, max_calls=2, interval=20, clock=lambda: t0)
        a = gate.check("s1", now=t0)[0]
        gate.record("s1", "ok", now=t0)
        b = gate.check("s2", now=t0 + timedelta(seconds=20))[0]
        gate.record("s2", "ok", now=t0 + timedelta(seconds=20))
        c = gate.check("s3", now=t0 + timedelta(seconds=40))
        return a and b and c[0] is False and c[1] == "budget", f"a={a} b={b} c={c}"

    def cooldown():
        ledger = {"version": 1, "last_call_at": None, "sources": {}}
        gate = mp.CallGate(ledger, max_calls=10, interval=20, clock=lambda: t0)
        gate.arm_cooldown("bili", now=t0)
        # 账本结构必须仍可被 load_ledger 校验通过（arm 同时写 last_call_at）。
        td = _workdir()
        try:
            p = td / "l.json"
            mp.save_ledger(p, ledger)
            mp.load_ledger(p)
        finally:
            shutil.rmtree(td, ignore_errors=True)
        soon = gate.check("bili", now=t0 + timedelta(seconds=100))
        later = gate.check("bili", now=t0 + timedelta(seconds=301))
        # 全局冷却：跨 source 也不能绕过。
        gate2 = mp.CallGate({"version": 1, "last_call_at": None, "sources": {}},
                            max_calls=10, interval=20, clock=lambda: t0)
        gate2.arm_global(now=t0)
        other = gate2.check("another-source", now=t0 + timedelta(seconds=100))
        return (soon[0] is False and soon[1] == "cooldown" and later[0] is True
                and later[1] == "ok" and other[0] is False and other[1] == "cooldown"), \
            f"soon={soon} later={later} other={other}"

    def interval():
        ledger = {"version": 1, "last_call_at": None, "sources": {}}
        gate = mp.CallGate(ledger, max_calls=10, interval=20, clock=lambda: t0)
        gate.record("s1", "ok", now=t0)
        allowed, reason, wait = gate.check("s2", now=t0 + timedelta(seconds=5))
        return allowed and reason == "ok" and abs(wait - 15.0) < 0.01, f"{allowed},{reason},{wait}"

    def sublimits():
        calls = mp.plan_bilibili_calls(400, "女权,彩礼,性别对立,厌女")
        capacity = sum(c["child_limit"] for c in calls)
        ok = (capacity >= 400
              and all(c["child_limit"] <= mp.BILIBILI_SUBLIMIT for c in calls)
              and all(c["pages"] <= 10 for c in calls)
              and all(c["child_max_requests"] <= mp.BILIBILI_SUBREQ for c in calls)
              and all(c["pages"] >= 1 for c in calls))
        return ok, f"capacity={capacity} calls={[(c['keywords'], c['pages'], c['child_limit'], c['child_max_requests']) for c in calls]}"

    def writer_batches():
        sizes = [len(b) for b in mp.plan_batches(list(range(250)), 100)]
        return sizes == [100, 100, 50], f"sizes={sizes}"

    def target():
        partial = mp.evaluate_target(21, 400)
        full = mp.evaluate_target(400, 400)
        return (partial["status"] == "partial" and not partial["target_met"]
                and full["status"] == "ok" and full["target_met"]), f"{partial} {full}"

    def agg():
        decisions = [{"accepted": True, "reason": "ok"},
                     {"accepted": False, "reason": "time:missing"},
                     {"accepted": False, "reason": "noise:game"},
                     {"accepted": False, "reason": "out_of_window"},
                     {"accepted": False, "reason": "duplicate"},
                     {"accepted": False, "reason": "not_substantive"}]
        a = mp.aggregate(decisions)
        return (a["accepted"] == 1 and a["time_invalid"] == 1 and a["noise"] == 1
                and a["out_of_window"] == 1 and a["duplicates"] == 1), str(a)

    def bad_args():
        for argv in (["--limit", "0"], ["--limit", "3", "--workers", "9"],
                     ["--limit", "3", "--interval", "0"], ["--limit", "401"]):
            try:
                mp.validate_args(mp.build_parser().parse_args(argv))
                return False, f"accepted bad argv {argv}"
            except mp.PolicyError:
                continue
        return True, "all rejected"

    def missing_db_failclosed():
        try:
            mp.load_existing_readonly(ROOT / "_nonexistent_checks_db.sqlite")
            return False, "did not raise (would create DB / network)"
        except mp.PolicyError:
            return True, "raised PolicyError (fail-closed)"

    for name, fn in [("budget", budget), ("429_cooldown_ledger", cooldown), ("interval", interval),
                     ("bilibili_sublimit_partition", sublimits), ("writer_batches", writer_batches),
                     ("partial_target", target), ("aggregate", agg), ("bad_args", bad_args),
                     ("missing_db_failclosed", missing_db_failclosed)]:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        out.append(_check("C", name, ok, detail))
    return out


def group_c_spawn(limit):
    """真实 Windows spawn worker + 父唯一 writer 隔离验证（无网络，3 份 DOM fixture 任务）。"""
    out = []
    base = [
        {"title": "男性女性性别歧视争议事件讨论", "url": "https://www.bilibili.com/video/BVfixt1",
         "source_id": "BVfixt1", "published_at": "2026-10-18T05:00:00+00:00"},
        {"title": "关于家暴法律与女性权益的制度讨论", "url": "https://www.bilibili.com/video/BVfixt2",
         "source_id": "BVfixt2", "published_at": "2026-10-18T06:00:00+00:00"},
    ]
    extra = [
        {"title": "彩礼婚育政策争议反复讨论", "url": "https://www.bilibili.com/video/BVfixt3",
         "source_id": "BVfixt3", "published_at": "2026-10-19T05:00:00+00:00"},   # 窗口外
        {"title": "男性女性性别歧视争议事件讨论", "url": "https://www.bilibili.com/video/BVfixt4",
         "source_id": "BVfixt4", "published_at": "2026-10-18T07:00:00+00:00"},   # 近似重复
    ]
    td = _workdir()
    try:
        paths = []
        for i, items in enumerate((base, extra, base[:1])):
            p = td / f"dom{i}.json"
            p.write_text(json.dumps({"platform": "bilibili",
                                     "observed_at": "2026-10-18T20:00:00+08:00",
                                     "items": items}, ensure_ascii=False), encoding="utf-8")
            paths.append(p)
        db = td / "iso.sqlite"
        ledger = td / "ledger.json"
        # 隔离 fixture：先显式建自己的临时库（正式门对缺库 fail-closed）。
        import storage
        storage.connect(db).close()
        argv = ["--limit", str(max(1, limit)), "--date", "2026-10-18", "--workers", "3",
                "--max-calls", "1", "--interval", "20", "--db", str(db), "--ledger", str(ledger),
                "--snapshot-root", str(td / "snap")]
        for p in paths:
            argv += ["--dom-evidence", str(p)]
        try:
            import collect_multiplatform
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = collect_multiplatform.main(argv)
            report = json.loads(buf.getvalue().strip())
        except Exception as exc:  # noqa: BLE001
            return [_check("C-spawn", "spawn_run", False, f"raised {type(exc).__name__}: {exc}")]

        sched = report.get("scheduler", {})
        pids = sched.get("worker_pids", [])
        out.append(_check("C-spawn", "three_worker_pids_distinct",
                          len(pids) >= 3 and all(p != os.getpid() for p in pids)
                          and all(p != sched.get("parent_pid") for p in pids),
                          f"parent={sched.get('parent_pid')} workers={pids}"))
        out.append(_check("C-spawn", "date_window_applied",
                          report["aggregate"]["out_of_window"] >= 1,
                          f"aggregate={report['aggregate']}"))
        out.append(_check("C-spawn", "dedup_in_run",
                          report["aggregate"]["duplicates"] >= 1,
                          f"duplicates={report['aggregate']['duplicates']}"))
        out.append(_check("C-spawn", "workspace_tmp_not_system_temp",
                          db.is_file() and str(db).startswith(str(ROOT)),
                          f"db={db}"))
        out.append(_check("C-spawn", "parent_writer_pid_evidence",
                          report.get("db", {}).get("touched") is True
                          and report.get("db", {}).get("writer_pid") == os.getpid()
                          and sched.get("parent_pid") == os.getpid(),
                          f"db={report.get('db')} parent={sched.get('parent_pid')}"))
        out.append(_check("C-spawn", "limit_and_partial",
                          report["target"]["added"] <= max(1, limit)
                          and report["status"] in ("ok", "partial") and code in (0, 2),
                          f"added={report['target']['added']} status={report['status']} code={code}"))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return out


def group_c_scheduler():
    """直接驱动 run_scheduler（fixture，无网络）：共享许可 + 浏览器串行完成 + 429 即停 + worker 异常退出。"""
    out = []
    td = _workdir()
    try:
        t0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
        row = {"title": "男性女性性别歧视争议讨论", "url": "https://www.bilibili.com/video/BVs1",
               "source_id": "BVs1", "published_at": "2026-10-18T05:00:00+00:00"}

        # Part A：3 workers，浏览器任务串行；共享许可即时持久化；429 即停同源后续任务。
        ledger_a = {"version": 1, "last_call_at": None, "sources": {}}
        gate_a = mp.CallGate(ledger_a, max_calls=10, interval=20, clock=lambda: t0)
        ledger_path = td / "sched-a.json"
        tasks_a = [
            {"id": 1, "kind": "fixture", "platform": "weibo", "source": "fx-b1",
             "browser": True, "fixture": {"permit": True, "rows": [row]}},
            {"id": 2, "kind": "fixture", "platform": "zhihu", "source": "fx-b2",
             "browser": True, "fixture": {"permit": True, "rows": [row]}},
            {"id": 3, "kind": "fixture", "platform": "toutiao", "source": "fx-429",
             "browser": True, "fixture": {"permit": True, "signal": "HTTP_429"}},
            {"id": 4, "kind": "fixture", "platform": "toutiao", "source": "fx-429",
             "browser": True, "fixture": {"rows": [row]}},      # 同源：应被跳过
        ]
        report_a = {}
        outputs_a = mp.run_scheduler(tasks_a, 3, gate_a,
                                     {"allow_fixture": True, "now": t0, "dry_run": False,
                                      "ledger_path": str(ledger_path), "date": None}, report_a)
        sched_a = report_a.get("scheduler", {})
        out.append(_check("C-sched", "shared_permit_and_persist",
                          gate_a.calls_used >= 3 and ledger_path.is_file(),
                          f"calls_used={gate_a.calls_used} ledger_exists={ledger_path.is_file()}"))
        out.append(_check("C-sched", "browser_serial_complete",
                          outputs_a[1].get("ok") and outputs_a[2].get("ok"),
                          f"t1={outputs_a[1].get('ok')} t2={outputs_a[2].get('ok')}"))
        out.append(_check("C-sched", "429_immediate_global_stop",
                          not outputs_a[3].get("ok") and outputs_a[4].get("ok") is False
                          and outputs_a[4].get("error", {}).get("type") == "Skipped"
                          and sched_a.get("stop_all") is True,
                          f"t3={outputs_a[3].get('error')} t4={outputs_a[4].get('error')} stop_all={sched_a.get('stop_all')}"))
        out.append(_check("C-sched", "429_global_cooldown_persisted",
                          mp.parse_iso(ledger_a.get("global_cooldown_until")) is not None,
                          str(ledger_a.get("global_cooldown_until"))))
        out.append(_check("C-sched", "workers3_distinct_from_parent",
                          len(sched_a.get("worker_pids", [])) >= 2
                          and all(p != os.getpid() for p in sched_a.get("worker_pids", [])),
                          f"parent={sched_a.get('parent_pid')} workers={sched_a.get('worker_pids')}"))

        # Part B：1 worker，worker 真实异常退出后回收重启，后续任务仍完成。
        ledger_b = {"version": 1, "last_call_at": None, "sources": {}}
        gate_b = mp.CallGate(ledger_b, max_calls=10, interval=20, clock=lambda: t0)
        tasks_b = [
            {"id": 5, "kind": "fixture", "platform": "tieba", "source": "fx-pre",
             "browser": False, "fixture": {"rows": [row]}},        # 先跑一任务，登记原 worker pid
            {"id": 6, "kind": "fixture", "platform": "tieba", "source": "fx-die",
             "browser": False, "fixture": {"die": True}},
            {"id": 7, "kind": "fixture", "platform": "tieba", "source": "fx-after-die",
             "browser": False, "fixture": {"rows": [row]}},
        ]
        report_b = {}
        outputs_b = mp.run_scheduler(tasks_b, 1, gate_b,
                                     {"allow_fixture": True, "now": t0, "dry_run": False,
                                      "ledger_path": str(td / "sched-b.json"), "date": None}, report_b)
        sched_b = report_b.get("scheduler", {})
        out.append(_check("C-sched", "worker_exit_recovered",
                          outputs_b[5].get("ok") and not outputs_b[6].get("ok")
                          and outputs_b[6].get("error", {}).get("signal") == "WORKER"
                          and outputs_b[7].get("ok")
                          and len(sched_b.get("worker_pids", [])) >= 2,
                          f"t5_ok={outputs_b[5].get('ok')} t6={outputs_b[6].get('error')} t7_ok={outputs_b[7].get('ok')} pids={sched_b.get('worker_pids')}"))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return out


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3, help="spawn 场景 fixture 行数（默认 3）")
    args = parser.parse_args(argv)
    limit = max(1, args.limit)
    results = (group_a() + group_b() + group_c_unit(limit) + group_c_scheduler() + group_c_spawn(limit))
    failed = [r for r in results if not r["ok"]]
    report = {"checker": "collection-policy-checks", "limit": limit,
              "total": len(results), "passed": len(results) - len(failed),
              "failed": len(failed), "isolated": True,
              "notes": ["纯模拟 + 临时目录内真实 spawn worker/父 writer；不调用真实源/模型，不写真实库。"],
              "results": results}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

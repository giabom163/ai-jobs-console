"""采集前准备的三组离线检查（无网络、无模型、无真实库写入）。

    python tools/classification_checks.py --limit 3

- A 组：分类内核跨接口一致性 + 噪声回归 + 作品歧视讨论的有限例外；
- B 组：四轴分离（涉及冲突/立场/强度/热度）、unknown 语义、旧分数与热度分离、
        require 拼写错误的结构化拒绝；
- C 组：prepare_collection 在隔离临时库上的只读导出、分歧队列、缺库/覆盖拒绝、
        以及贴吧 admit 边界（单点/部分窗/净增>50/回撤/字段变化/过期）。

`--limit` 控制隔离临时库的读取/导出分块大小（默认 3），不截断 A/B 断言、
也不限制准备工具扫描全库。fixture 目录保留在本次副本的 `_checks_out/` 下并在报告中给出路径，
不删除任何文件。
退出码：0 全通过，1 有失败。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import activity  # noqa: E402
import collection_policy  # noqa: E402
import content_filter  # noqa: E402
import relevance  # noqa: E402
import storage  # noqa: E402
from tools import prepare_collection  # noqa: E402

NOW = datetime(2026, 10, 20, 4, 0, 0, tzinfo=timezone.utc)


def _check(group, name, ok, detail=""):
    return {"group": group, "name": name, "ok": bool(ok), "detail": detail}


def _hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- A 组：统一内核跨接口 + 噪声回归 ---------------------------------------------------------


def group_a():
    out = []
    corpus = [
        "仇男发言合集",
        "厌女文化与受害者羞辱",
        "女性遭遇家暴后如何维权",
        "短剧《她的选择》为何引发性别歧视争议",
        "游戏《归途》剧情中的性别歧视争议",
        "女性生理科普：月经周期怎么算",
        "妇科医生性骚扰患者被判刑",
        "女子在超市盗窃被拘",
        "关于彩礼新规下的婚育政策讨论",
        "女玩家通关率被质疑：游戏圈性别歧视",
        "现在年轻人攒彩礼有多难？",
        "原神女性角色物化与性别歧视争议",
        "男女退休年龄是否应该统一？",
        "【陆远音声】被家暴去找前男友",
        "短剧《她的选择》第3集：家暴剧情",
    ]
    for title in corpus:
        analysis = relevance.analyze(title)
        classify = relevance.classify(title)
        tier = relevance.classify_tier(title)
        relevant, _score, reason = relevance.explain(title)
        opposition = relevance.is_relevant(title)
        gender = relevance.is_gender_related(title)
        ok = (analysis["tier"] == classify["tier"] == tier
              and analysis["opposition"] == classify["opposition"] == opposition
              == (tier == "conflict")
              and analysis["gender_related"] == classify["gender_related"] == gender
              and relevant == (tier == "conflict")
              and reason == analysis["reason"])
        out.append(_check("A", "cross_interface:" + title[:14], ok,
                          f"tier={tier} opposition={opposition} gender={gender} reason={reason}"))

    # 强特征无需主体词：仅“仇男”即可成立。
    strong_only = relevance.analyze("仇男")
    out.append(_check("A", "strong_without_subject",
                      strong_only["tier"] == "conflict" and strong_only["opposition"],
                      f"tier={strong_only['tier']} path={strong_only['path']}"))

    # 医学排除一致：生理科普不因性别词进入议题；强特征仍优先。
    medical = relevance.classify("女性生理科普：月经周期怎么算")
    out.append(_check("A", "medical_exclusion_consistent",
                      medical["tier"] == "unrelated" and not medical["gender_related"]
                      and medical["reason"].startswith("excluded:"), str(medical["reason"])))
    strong_medical = relevance.classify("妇科医生性骚扰患者被判刑")
    out.append(_check("A", "strong_beats_medical_exclusion",
                      strong_medical["tier"] == "conflict" and strong_medical["opposition"],
                      str(strong_medical["tier"])))

    # 有限噪声例外：作品歧视讨论保留；硬噪声/连载标记仍拦截。
    exception = content_filter.noise_analysis("短剧《她的选择》为何引发性别歧视争议")
    out.append(_check("A", "noise_exception_applied",
                      not exception["is_noise"] and exception["exception"] is not None,
                      json.dumps(exception, ensure_ascii=False)))
    hard_serial = content_filter.noise_analysis("短剧《她的选择》第3集：家暴剧情")
    out.append(_check("A", "noise_exception_blocked_by_serial",
                      hard_serial["is_noise"] and hard_serial["reason"] == "fiction",
                      json.dumps(hard_serial, ensure_ascii=False)))
    for title, want in (("【陆远音声】被家暴去找前男友", "audio"),
                        ("【抢先版】去离婚路上他看到了未来，痛悔家暴（2）", "fiction"),
                        ("为了基尼奇攒彩礼的第四百二十七天", "game"),
                        ("攀比彩礼的代价 #ai故事 #听别人说故事", "fiction")):
        got = content_filter.noise_reason(title)
        out.append(_check("A", "hard_noise:" + title[:12], got == want, f"noise={got} want={want}"))

    # 跨字段不得拼词：标题“他家”+摘要“暴了”不构成“家暴”。
    cross = relevance.analyze("他家的猫", "暴了")
    out.append(_check("A", "no_cross_field_term", cross["tier"] == "unrelated",
                      f"tier={cross['tier']} matched={cross['matched']}"))

    # 纯主体词：旧宽候选兼容（gender_related=True），但不得升级为 strict 分层。
    pure = relevance.classify("女子在超市盗窃被拘")
    pure_analysis = relevance.analyze("女子在超市盗窃被拘")
    out.append(_check("A", "subject_only_is_wide_candidate_only",
                      pure_analysis["tier"] == "unrelated"
                      and pure_analysis["opposition"] is False
                      and pure_analysis["gender_related"] is True
                      and relevance.is_gender_related("女子在超市盗窃被拘") is True
                      and pure["tier"] == "unrelated" and pure["opposition"] is False
                      and pure["gender_related"] is True,
                      json.dumps({"tier": pure["tier"], "opposition": pure["opposition"],
                                  "gender_related": pure["gender_related"]},
                                 ensure_ascii=False)))

    # P1a 窄关系组合（真实样本）：双性别群体+特权/失权；女权+女吸血鬼+共谋。
    power = relevance.analyze("男子在火车上拉屎被压热度？男人又有特权了？女性又失权了？")
    out.append(_check("A", "power_relation_combo_conflict",
                      power["tier"] == "conflict"
                      and power["evidence"]["rule"] == "power_relation"
                      and "特权" in power["reason"] and "失权" in power["reason"]
                      and power["author_stance"] == "unknown"
                      and power["intensity"] == "unknown",
                      json.dumps({"tier": power["tier"], "reason": power["reason"],
                                  "rule": power["evidence"]["rule"]}, ensure_ascii=False)))
    collusion = relevance.analyze(
        "女权主义，一种是高溢价商户和女吸血鬼们的共谋，另一种是书本上的空中楼阁")
    out.append(_check("A", "feminism_vampire_collusion_conflict",
                      collusion["tier"] == "conflict"
                      and collusion["evidence"]["rule"] == "collusion"
                      and "共谋" in collusion["reason"]
                      and collusion["author_stance"] == "unknown",
                      json.dumps({"tier": collusion["tier"], "reason": collusion["reason"]},
                                 ensure_ascii=False)))
    for title in ("公司给员工发放特权福利，员工都很开心",
                  "吸血鬼伯爵的共谋计划",
                  "女权主义与公司共谋"):
        analysis = relevance.analyze(title)
        out.append(_check("A", "combo_negative:" + title[:12],
                          analysis["tier"] != "conflict",
                          f"tier={analysis['tier']} reason={analysis['reason']}"))

    # 极窄议题组合：同字段“反对”+“举报”+男权/女权 → issue（不是 conflict）。
    action_title = "反对并举报男权女权，从我做起"
    action = relevance.analyze(action_title)
    out.append(_check("A", "ideology_action_combo_issue",
                      action["tier"] == "issue" and action["opposition"] is False
                      and action["gender_related"] is True
                      and action["evidence"]["rule"] == "ideology_action"
                      and "反对" in action["reason"] and "举报" in action["reason"]
                      and action["author_stance"] == "unknown"
                      and action["intensity"] == "unknown",
                      json.dumps({"tier": action["tier"], "reason": action["reason"]},
                                 ensure_ascii=False)))
    action_gate = collection_policy.gate_candidate(
        title=action_title, url="https://www.bilibili.com/video/BVreq9",
        published=collection_policy.normalize_published(1792387500, mode="epoch"))
    out.append(_check("A", "ideology_action_substantive_accepts",
                      action_gate["accepted"] and action_gate["classification"]["tier"] == "issue",
                      str(action_gate.get("reason"))))
    conflict_action = relevance.analyze("反对并举报男权女权歧视")
    out.append(_check("A", "ideology_action_conflict_precedence",
                      conflict_action["tier"] == "conflict"
                      and conflict_action["opposition"] is True
                      and relevance.classify_tier(action_title) == "issue",
                      f"with_discrimination={conflict_action['tier']} "
                      f"without={relevance.classify_tier(action_title)}"))
    report_only = relevance.analyze("女子举报公司违规")
    report_gate = collection_policy.gate_candidate(
        title="女子举报公司违规", url="https://www.bilibili.com/video/BVreq10",
        published=collection_policy.normalize_published(1792387500, mode="epoch"))
    out.append(_check("A", "report_alone_not_substantive",
                      report_only["tier"] not in ("issue", "conflict")
                      and not report_gate["accepted"],
                      f"tier={report_only['tier']} gate={report_gate.get('reason')}"))

    # P1b 组合剧情噪声：角色+病弱青梅/白月光+坐不住/跪求/痛悔；不单禁“青梅”。
    drama_title = "妇科检查遇到厌女的女医生后，丈夫的病弱青梅坐不住了_1017GH1"
    drama_noise = content_filter.noise_analysis(drama_title)
    out.append(_check("A", "drama_combo_noise",
                      drama_noise["is_noise"] and drama_noise["reason"] == "drama",
                      json.dumps(drama_noise, ensure_ascii=False)))
    drama_gate = collection_policy.gate_candidate(
        title=drama_title, url="https://www.bilibili.com/video/BVreq8",
        published=collection_policy.normalize_published(1792387500, mode="epoch"))
    out.append(_check("A", "drama_combo_rejected_by_gate",
                      not drama_gate["accepted"] and drama_gate["reason"] == "noise:drama",
                      str(drama_gate["reason"])))
    out.append(_check("A", "drama_combo_negative:no_role_or_reaction",
                      not content_filter.is_noise("病弱青梅竹马的青春回忆")
                      and not content_filter.is_noise("青梅题材新剧开播"),
                      "青梅单词不得单独判噪声"))
    critique = "丈夫的病弱青梅坐不住桥段，为何被批评厌女"
    out.append(_check("A", "drama_combo_discussion_exception",
                      not content_filter.is_noise(critique)
                      and relevance.classify_tier(critique) == "conflict",
                      f"noise={content_filter.noise_reason(critique)}"))
    out.append(_check("A", "drama_combo_serial_keeps_blocked",
                      content_filter.is_noise("丈夫的病弱青梅坐不住 第3集"),
                      f"noise={content_filter.noise_reason('丈夫的病弱青梅坐不住 第3集')}"))
    return out


# --- B 组：四轴 / unknown / 旧分数与热度分离 / require 拼错 ----------------------------------


def group_b():
    out = []
    negative = relevance.analyze("极端女权都是垃圾，男女对立没救")
    out.append(_check("B", "tier_conflict", negative["tier"] == "conflict", negative["tier"]))
    out.append(_check("B", "author_stance_unknown_no_sentiment_substitute",
                      negative["author_stance"] == "unknown"
                      and negative["author_stance_basis"] == "rule_layer_has_no_stance_evidence",
                      str(negative["author_stance"])))
    out.append(_check("B", "intensity_unknown_not_term_count",
                      negative["intensity"] == "unknown"
                      and negative["intensity_basis"] == "no_calibrated_evidence",
                      str(negative["intensity"])))

    heat_cases = [
        ("none", None, False, False),
        ("legacy_zero", 0, False, False),
        ("explicit_zero", 0, True, True),
        ("explicit_positive", 12345, True, True),
        ("bool", True, True, False),
        ("string", "12", True, False),
    ]
    for name, value, explicit, known in heat_cases:
        heat = relevance.observed_heat(value, explicit=explicit)
        out.append(_check("B", "heat:" + name, heat["known"] is known,
                          json.dumps(heat, ensure_ascii=False)))
    analysis = relevance.analyze("女性遭遇家暴", views=0, views_explicit=False)
    out.append(_check("B", "legacy_views_not_heat",
                      analysis["heat"]["known"] is False
                      and analysis["heat"]["basis"] == "legacy_or_unknown_heat_field",
                      json.dumps(analysis["heat"], ensure_ascii=False)))

    score_plain = relevance.conflict_score("女性遭遇家暴", "", 0)
    score_views = relevance.conflict_score("女性遭遇家暴", "", 999999)
    score_repeat = relevance.conflict_score("女性遭遇家暴家暴家暴", "", 0)
    out.append(_check("B", "conflict_score_ignores_views", score_plain == score_views,
                      f"plain={score_plain} views={score_views}"))
    out.append(_check("B", "conflict_score_dedupes_terms", score_plain == score_repeat,
                      f"plain={score_plain} repeat={score_repeat}"))
    out.append(_check("B", "conflict_score_not_calibrated_intensity",
                      score_plain == 10.0 and analysis["intensity"] == "unknown",
                      f"score={score_plain} intensity={analysis['intensity']}"))
    out.append(_check("B", "no_score_key_in_analysis",
                      "conflict_score" not in analysis and "score" not in analysis,
                      ",".join(sorted(analysis))))

    published = collection_policy.normalize_published(1792387500, mode="epoch")
    for bad in ("substantiative", "Substantive", "relevan", "", None, 1):
        decision = collection_policy.gate_candidate(
            title="女性遭遇家暴", url="https://www.bilibili.com/video/BVreq1",
            published=published, require=bad)
        out.append(_check("B", f"require_rejected:{bad!r}",
                          not decision["accepted"] and decision["reason"] == "unknown_require"
                          and decision["allowed_requires"] == list(collection_policy.REQUIRE_ENUM),
                          json.dumps(decision, ensure_ascii=False, default=str)))
    good = collection_policy.gate_candidate(
        title="女性遭遇家暴", url="https://www.bilibili.com/video/BVreq2",
        published=published, require="relevant")
    out.append(_check("B", "require_relevant_accepts_conflict", good["accepted"],
                      str(good["reason"])))
    no_widen = collection_policy.gate_candidate(
        title="关于彩礼新规下的婚育政策讨论", url="https://www.bilibili.com/video/BVreq3",
        published=published, require="relevant")
    out.append(_check("B", "require_relevant_rejects_issue",
                      not no_widen["accepted"] and no_widen["reason"] == "not_opposition",
                      str(no_widen["reason"])))
    substantive = collection_policy.gate_candidate(
        title="关于彩礼新规下的婚育政策讨论", url="https://www.bilibili.com/video/BVreq4",
        published=published, require="substantive")
    out.append(_check("B", "substantive_accepts_issue", substantive["accepted"],
                      str(substantive["reason"])))
    narrow = collection_policy.gate_candidate(
        title="女子在超市盗窃被拘", url="https://www.bilibili.com/video/BVreq5",
        published=published)
    out.append(_check("B", "substantive_rejects_subject_only",
                      not narrow["accepted"] and narrow["reason"] == "not_substantive",
                      str(narrow["reason"])))
    wide = collection_policy.gate_candidate(
        title="女子在超市盗窃被拘", url="https://www.bilibili.com/video/BVreq7",
        published=published, require="gender_related")
    out.append(_check("B", "gender_related_accepts_subject_only_but_substantive_does_not",
                      wide["accepted"] and wide["require"] == "gender_related"
                      and not narrow["accepted"],
                      f"wide={wide['reason']} substantive={narrow['reason']}"))
    noise_gate = collection_policy.gate_candidate(
        title="【陆远音声】被家暴去找前男友", url="https://www.bilibili.com/video/BVreq6",
        published=published)
    out.append(_check("B", "gate_noise_path_no_keyerror",
                      not noise_gate["accepted"] and noise_gate["reason"] == "noise:audio",
                      str(noise_gate["reason"])))
    snapshot = collection_policy.snapshot_evidence(good)
    classification = snapshot.get("classification") or {}
    out.append(_check("B", "snapshot_carries_classification",
                      classification.get("rule_version") == relevance.RULE_VERSION
                      and classification.get("tier") == "conflict"
                      and classification.get("reason")
                      and classification.get("evidence", {}).get("variant")
                      == relevance.MATCH_VARIANT
                      and "noise" in classification.get("evidence", {}),
                      json.dumps(snapshot, ensure_ascii=False, default=str)))
    return out


# --- C 组：prepare_collection 隔离库 + 贴吧边界 ---------------------------------------------


RECORD_COLUMNS = ("id", "feed_id", "source_id", "title", "summary", "url", "canonical_url",
                  "content_sha256", "raw_sha256", "raw_path", "published_at", "published_raw",
                  "fetched_at", "time_quality", "canonical_id", "dedup_reason", "run_id",
                  "sentiment", "views", "relevance_tier", "conflict_score")


def _record(record_id, feed_id, source_id, title, summary, tier, score, views, fetched_at,
            published_at="2026-10-19T03:00:00+00:00", quality="valid"):
    return {
        "id": record_id, "feed_id": feed_id, "source_id": source_id, "title": title,
        "summary": summary, "url": "https://example.com/" + record_id,
        "canonical_url": "https://example.com/" + record_id,
        "content_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(),
        "raw_sha256": hashlib.sha256(record_id.encode("utf-8")).hexdigest(),
        "raw_path": "snapshots/fixture.json", "published_at": published_at,
        "published_raw": published_at, "fetched_at": fetched_at,
        "time_quality": quality, "canonical_id": "canonical-fixture",
        "dedup_reason": "independent", "run_id": "run-fixture", "sentiment": "待判",
        "views": views, "relevance_tier": tier, "conflict_score": score,
    }


def build_fixture(db_path):
    """用真实 storage schema 建隔离临时库，写入 4 条 fixture（含第 4 条证明全库扫描不截断）。"""
    db = storage.connect(db_path)
    try:
        with db:
            db.execute("INSERT INTO feeds(id,name,url,added_at) VALUES(1,'贴吧','https://tieba.baidu.com/f?kw=fixture','2026-10-20T00:00:00+00:00')")
            db.execute("INSERT INTO feeds(id,name,url,added_at) VALUES(2,'B站','https://www.bilibili.com/fixture','2026-10-20T00:00:00+00:00')")
            db.execute("INSERT INTO canonical_records(id,created_at,dedup_rule) VALUES('canonical-fixture','2026-10-20T00:00:00+00:00','exact-text-v1')")
            db.execute("INSERT INTO collection_runs(id,feed_id,kind,status,started_at,finished_at,input_count,added,duplicates,error,input_sha256,limit_n) "
                       "VALUES('run-fixture',2,'fixture','succeeded','2026-10-20T00:00:00+00:00','2026-10-20T00:01:00+00:00',3,3,0,NULL,?,3)",
                       (hashlib.sha256(b"fixture").hexdigest(),))
            rows = [
                _record("record-a", 2, "BVfixture1", "女子手机上登录前男友账号转走20万！", "",
                        "unrelated", 0, 0, "2026-10-20T03:03:00+00:00"),
                _record("record-b", 2, "BVfixture2", "关于彩礼新规下的婚育政策讨论", "",
                        "conflict", 30, 1200, "2026-10-20T03:02:00+00:00"),
                _record("record-c", 1, "post-1", "贴吧原帖：女性职场权益讨论", "",
                        "issue", 0, 0, "2026-10-20T03:01:00+00:00"),
                _record("record-d", 1, "post-2", "贴吧原帖：单点观测样本", "",
                        "unrelated", 0, 0, "2026-10-20T03:00:00+00:00",
                        quality="missing", published_at=None),
            ][:4]
            for row in rows:
                names = list(RECORD_COLUMNS)
                db.execute("INSERT INTO records(%s) VALUES(%s)"
                           % (",".join(names), ",".join("?" * len(names))),
                           tuple(row[name] for name in names))
            db.execute("INSERT INTO model_annotations(record_id,model,sentiment,topic,gender_related,opposition,relevant,created_at,raw_json) "
                       "VALUES('record-a','fixture-model','偏负面','性别','1',1,1,'2026-10-20T02:00:00+00:00','{}')")
            # 非法 opposition 原值（字符串 'yes'）必须原样保留并标待核，不能按 truthy 当 1。
            db.execute("INSERT INTO model_annotations(record_id,model,sentiment,topic,gender_related,opposition,relevant,created_at,raw_json) "
                       "VALUES('record-b','fixture-model','偏负面','婚育','0','yes',0,'2026-10-20T02:01:00+00:00','{}')")
            evidence_columns = ("id,identity_key,platform,platform_id,url,canonical_url,"
                                "title,source_name,evidence_kind,content,content_sha256,"
                                "content_raw_sha256,content_source,fragment_start,fragment_end,"
                                "observed_at,observed_raw,published_at,published_raw,"
                                "time_quality,source_response_sha256,snapshot_sha256,"
                                "snapshot_path,comment_value,comment_precision,"
                                "comment_source_field,other_counts,group_label,imported_at")
            now_real = datetime.now(timezone.utc)
            evidence = [
                ("ev-1a", "post-1", 100, (now_real - timedelta(hours=2)).isoformat()),
                ("ev-1b", "post-1", 160, (now_real - timedelta(minutes=5)).isoformat()),
                ("ev-2a", "post-2", 10, (now_real - timedelta(minutes=5)).isoformat()),
            ]
            for evidence_id, platform_id, value, observed_at in evidence:
                db.execute(
                    "INSERT INTO p1_evidence(%s) VALUES(%s)"
                    % (evidence_columns, ",".join("?" * len(evidence_columns.split(",")))),
                    (evidence_id, "tieba\x1fid\x1f" + platform_id, "tieba", platform_id,
                     "https://tieba.baidu.com/p/" + platform_id,
                     "https://tieba.baidu.com/p/" + platform_id, "fixture", "贴吧",
                     "title_only", "", hashlib.sha256(b"").hexdigest(),
                     hashlib.sha256(b"").hexdigest(), "title", 0, 0, observed_at,
                     observed_at, None, None, "missing", hashlib.sha256(b"r").hexdigest(),
                     hashlib.sha256(evidence_id.encode("utf-8")).hexdigest(),
                     "snapshots/fixture.json", value, "exact", "reply_count", "{}",
                     "人工候选", "2026-10-20T03:00:00+00:00"))
        # 让只读连接不依赖 WAL/-shm：fixture 关闭前回落到 DELETE 日志模式。
        db.execute("PRAGMA journal_mode=DELETE")
    finally:
        db.close()


def _run_prepare(argv):
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            code = prepare_collection.main(argv)
    except Exception as exc:  # noqa: BLE001 失败也要可读，不让检查器整体崩掉
        return 99, {"kind": type(exc).__name__, "detail": str(exc)}
    text = buffer.getvalue().strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"raw": text}
    return code, payload


def _tieba_rows(values):
    return [{"observed_at": observed, "comment_value": value, "comment_precision": "exact",
             "comment_source_field": "reply_count"} for observed, value in values]


def tieba_boundaries():
    out = []
    now = NOW
    cases = [
        ("single_point", [(now - timedelta(minutes=5), 10)], "pending", "single_point",
         "single_point"),
        ("delta_50_partial", [(now - timedelta(hours=2), 100), (now - timedelta(minutes=5), 150)],
         "pending", "partial_interval", "window_net_gain_le_50_partial"),
        ("delta_51_qualified", [(now - timedelta(hours=2), 100), (now - timedelta(minutes=5), 151)],
         "qualified", None, "window_net_gain_gt_50"),
        ("retraction", [(now - timedelta(hours=2), 200), (now - timedelta(minutes=5), 150)],
         "pending", "retraction", "count_retracted"),
        ("source_changed", [(now - timedelta(hours=2), 100), (now - timedelta(minutes=5), 200)],
         "pending", "source_changed", "source_field_changed"),
        ("stale", [(now - timedelta(hours=3), 100), (now - timedelta(minutes=30), 200)],
         "pending", "stale_last", "stale_observation"),
        ("no_observation", [], "pending", "no_observation", "no_count_evidence"),
    ]
    for name, values, want_status, want_reason, want_label in cases:
        rows = _tieba_rows(values)
        if name == "source_changed":
            rows[1]["comment_source_field"] = "comment_count"
        result = activity.admit(rows, now)
        ok = result["status"] == want_status and result.get("reason") == want_reason
        out.append(_check("C-tieba", name, ok,
                          f"status={result['status']} reason={result.get('reason')} "
                          f"delta={result.get('observed_delta')} is_full_24h={result.get('is_full_24h')}"))
    # 50 与 51 的语义分界：50 不得当达标，51 才算。
    fifty = activity.admit(_tieba_rows([(now - timedelta(hours=2), 0),
                                       (now - timedelta(minutes=5), 50)]), now)
    fifty_one = activity.admit(_tieba_rows([(now - timedelta(hours=2), 0),
                                           (now - timedelta(minutes=5), 51)]), now)
    out.append(_check("C-tieba", "threshold_strictly_gt_50",
                      fifty["status"] == "pending" and fifty_one["status"] == "qualified",
                      f"50={fifty['status']} 51={fifty_one['status']}"))
    return out


def _tieba_labels(doc):
    """从 readiness 的 tieba 段直接取标签，避免测试复制映射逻辑。"""
    return doc.get("tieba", {}).get("labels", {})


def group_c(limit):
    out = []
    evidence_root = ROOT / "_checks_out"
    evidence_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    workdir = Path(tempfile.mkdtemp(
        prefix=f"classification-{stamp}-{os.getpid()}-", dir=str(evidence_root)))
    missing = workdir / "missing.sqlite"
    missing_out = workdir / "missing-out"
    code, payload = _run_prepare(["--db", str(missing), "--output-dir", str(missing_out),
                                  "--limit", "3"])
    out.append(_check("C", "missing_db_fail_closed",
                      code == 1 and payload.get("kind") == "db_missing"
                      and not missing_out.exists(),
                      f"code={code} payload={payload} out_exists={missing_out.exists()}"))

    db_path = workdir / "fixture.sqlite"
    build_fixture(db_path)
    sha_before = _hash_file(db_path)
    output_dir = workdir / "out"
    code, payload = _run_prepare(["--db", str(db_path), "--output-dir", str(output_dir),
                                  "--limit", str(max(1, limit))])
    out.append(_check("C", "prepare_success", code == 0 and payload.get("status") == "ok",
                      f"code={code} payload={payload}"))
    queue_path = output_dir / prepare_collection.QUEUE_FILE
    ready_path = output_dir / prepare_collection.READINESS_FILE
    summary_path = output_dir / prepare_collection.SUMMARY_FILE
    out.append(_check("C", "three_outputs_created",
                      queue_path.is_file() and ready_path.is_file() and summary_path.is_file(),
                      f"{queue_path.is_file()} {ready_path.is_file()} {summary_path.is_file()}"))
    queue_doc = json.loads(queue_path.read_text(encoding="utf-8"))
    ready_doc = json.loads(ready_path.read_text(encoding="utf-8"))
    items = queue_doc.get("items", [])
    ids = [item["record_id"] for item in items]
    out.append(_check("C", "queue_pending_and_identified",
                      bool(items) and all(item["resolution_status"] == "pending"
                                          and len(item["input_hash"]) == 64
                                          and item["record_id"]
                                          for item in items),
                      f"items={len(items)}"))
    # 全库扫描：--limit 只是分块，不能把 4 条 fixture 截成前 3 条。
    out.append(_check("C", "scan_all_records_not_sql_limit",
                      queue_doc.get("scan_scope") == "all_records"
                      and queue_doc.get("chunk_size") == max(1, limit)
                      and queue_doc.get("records_total") == 4
                      and queue_doc.get("records_scanned") == 4
                      and ready_doc["limits"]["records_scanned"] == 4
                      and set(ids) == {"record-a", "record-b", "record-c", "record-d"}
                      and len(items) == 4,
                      f"chunk={queue_doc.get('chunk_size')} "
                      f"scanned={queue_doc.get('records_scanned')} items={len(items)} ids={ids}"))
    model_b = next((item["model"] for item in items if item["record_id"] == "record-b"), None)
    out.append(_check("C", "invalid_model_opposition_kept_raw",
                      model_b is not None and model_b["present"] is True
                      and model_b["opposition"] is None
                      and model_b["opposition_valid"] is False
                      and model_b["opposition_raw"] == "yes",
                      json.dumps(model_b, ensure_ascii=False, default=str)))
    current_counts = ready_doc["classification"]["current_disagreement_counts"]
    legacy_counts = ready_doc["classification"]["legacy_disagreement_counts"]
    out.append(_check("C", "current_disagreements_recorded",
                      current_counts.get("model_annotation_missing") == 2
                      and current_counts.get("stored_tier_vs_current_rule", 0) >= 1
                      and current_counts.get("model_relevant_vs_current_gender_related", 0) >= 1,
                      json.dumps(current_counts, ensure_ascii=False)))
    out.append(_check("C", "legacy_disagreements_separate",
                      legacy_counts.get("legacy_tier_vs_model_opposition") == 1
                      and legacy_counts.get("model_opposition_invalid_type") == 1
                      and "legacy_tier_vs_model_opposition" not in current_counts
                      and "model_opposition_invalid_type" not in current_counts,
                      json.dumps(legacy_counts, ensure_ascii=False)))
    out.append(_check("C", "model_missing_flagged",
                      ready_doc["classification"]["model_missing"] == 2
                      and ready_doc["classification"]["model_present"] == 2
                      and ready_doc["classification"]["model_table_usable"] is True,
                      json.dumps({key: ready_doc["classification"][key] for key in
                                  ("model_missing", "model_present", "model_table_usable")},
                                 ensure_ascii=False)))
    out.append(_check("C", "legacy_score_not_recomputed",
                      ready_doc["classification"]["legacy_conflict_score_nonzero"] >= 1,
                      str(ready_doc["classification"]["legacy_conflict_score_nonzero"])))
    out.append(_check("C", "heat_unknown_from_legacy_views",
                      ready_doc["axes"]["heat_unknown"] == 4
                      and ready_doc["axes"]["heat_known"] == 0,
                      json.dumps(ready_doc["axes"], ensure_ascii=False)))
    out.append(_check("C", "grants_all_false",
                      all(value is False for value in ready_doc["grants"].values()),
                      json.dumps(ready_doc["grants"], ensure_ascii=False)))
    out.append(_check("C", "readonly_evidence",
                      ready_doc["db"]["unchanged"] is True
                      and ready_doc["network_requests"] == 0
                      and ready_doc["model_calls"] == 0
                      and ready_doc["db_writes"] == 0
                      and "ROLLBACK" in ready_doc["db"]["read_mode"]
                      and _hash_file(db_path) == sha_before,
                      f"unchanged={ready_doc['db']['unchanged']}"))
    labels = _tieba_labels(ready_doc)
    out.append(_check("C", "tieba_evidence_summarized",
                      labels.get("window_net_gain_gt_50", 0) >= 1
                      and (labels.get("single_point", 0) >= 1 or limit < 2)
                      and ready_doc["tieba"]["real_sampling"] == "not_started"
                      and ready_doc["limits"]["tieba_candidates_max"] == 3,
                      json.dumps(labels, ensure_ascii=False)))

    queue_before = _hash_file(queue_path)
    code, payload = _run_prepare(["--db", str(db_path), "--output-dir", str(output_dir),
                                  "--limit", "3"])
    out.append(_check("C", "overwrite_refused",
                      code == 1 and payload.get("kind") == "output_exists"
                      and _hash_file(queue_path) == queue_before,
                      f"code={code} kind={payload.get('kind')}"))

    original_connect = storage.connect
    storage.connect = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("prepare_collection 不得调用 storage.connect"))
    try:
        fresh = workdir / "out-noconnect"
        code, payload = _run_prepare(["--db", str(db_path), "--output-dir", str(fresh),
                                      "--limit", "3"])
    finally:
        storage.connect = original_connect
    out.append(_check("C", "no_storage_connect", code == 0 and payload.get("status") == "ok",
                      f"code={code} payload={payload}"))

    code, payload = _run_prepare(["--db", str(db_path), "--output-dir", str(workdir / "out-bad"),
                                  "--limit", "0"])
    out.append(_check("C", "bad_limit_rejected",
                      code == 1 and payload.get("kind") == "bad_limit",
                      f"code={code} kind={payload.get('kind')}"))
    out.extend(tieba_boundaries())
    return out, str(workdir)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3,
                        help="隔离临时库的读取/导出分块大小（默认 3）；不限制全库扫描")
    args = parser.parse_args(argv)
    limit = max(1, args.limit)
    group_c_results, fixture_dir = group_c(limit)
    results = group_a() + group_b() + group_c_results
    failed = [row for row in results if not row["ok"]]
    report = {
        "checker": "classification-checks",
        "limit": limit,
        "rule_version": relevance.RULE_VERSION,
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "isolated": True,
        "fixture_dir": fixture_dir,
        "fixture_retained": True,
        "network_requests": 0,
        "model_calls": 0,
        "real_db_writes": 0,
        "notes": ["三组离线检查：跨接口/噪声；四轴/unknown/require；隔离临时库与贴吧边界",
                  "fixture 目录保留在本次副本 _checks_out/ 下（见 fixture_dir），脚本不删除任何文件",
                  "本检查不访问真实来源、不调用模型、不写真实业务库"],
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

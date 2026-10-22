# 分类契约（唯一内核，2026-10-20）

规则版本：`relevance.RULE_VERSION = classification-core-v4`。任何词表或优先级变化必须递增版本；
历史库中的 `relevance_tier` / `conflict_score` 不会因本文件自动重算。

## 1. 一个内核，所有接口

`relevance.analyze(title, summary="", *, views=None, views_explicit=True)` 是唯一分类实现。
以下接口全部调用它，不再各自实现优先级：

| 接口 | 返回 | 说明 |
| --- | --- | --- |
| `classify_tier` | `conflict` / `issue` / `unrelated` | 与 `tier` 相同 |
| `classify` | `{gender_related, opposition, reason, tier, rule_version, involves_conflict, author_stance, intensity, heat}` | 旧主要键保留 |
| `explain` | `(relevant, clue_score, reason)` | `relevant == (tier == "conflict")` |
| `is_relevant` | `bool` | 等于 `opposition` |
| `is_gender_related` | `bool` | 宽口径，仅候选/复核 |
| `collection_policy.gate_candidate` | 决策对象 | 一次 `analyze` 同时取噪声/tier/宽口径 |

`tier == "conflict"` 与 `opposition == True` 必须始终一致；`require="relevant"` 只接受 conflict。

## 2. 统一优先级（从上到下，命中即停）

1. 空文本 → `unrelated`（`empty`）；
2. 噪声门 → `unrelated`（`noise:<类别>`，见第 3 节）；
3. **强特征独立成立** → `conflict`（`strong:<词>`，不需要另有主体词，例如单独出现“仇男”）；
4. 医学/生理科普排除（无强特征、无冲突词）→ `unrelated`（`excluded:<词>`）；
5. 主体词 + 冲突词 → `conflict`（`pair:`）；
5b. 窄关系组合 → `conflict`：双性别群体 + 特权/失权（`power_relation`）；
   或 女权 + 女吸血鬼 + 共谋（`collusion`）。单词本身不构成冲突，
   命中词写入 `reason`，`evidence.terms` 保留字段/词/规范化口径，`evidence.rule` 记录组合名；
5c. 极窄议题组合 → `issue`（`ideology_action`）：**同一字段**同时含“反对”+“举报”，
   且同字段指向“男权/女权”；位于主体词+冲突词之后、`subject_issue/subject_only` 之前，
   `opposition=False`，不推作者立场；
6. 主体词 + 婚育语境或实质议题 → `issue`（`context:`）；
7. 仅主体词 → `unrelated`（`subject_only:`）；**宽候选兼容**：`gender_related=True`，
   `opposition=False`，正式 `substantive` 仍拒绝；
8. 其余 → `unrelated`。

要点：

- 强特征（`STRONG_TERMS`：性别对立/男女对立/性别战争/性别矛盾/女拳/男拳/厌女/媚男/仇女/仇男/
  重男轻女/性骚扰/性侵/极端女权/极端男权）**优先于医学排除**，避免“妇科医生性骚扰”被排除；
- 排除词只在没有强特征、也没有冲突词时生效；所有接口共用同一结果；
- 匹配逐字段进行（title / summary 分别规范化后匹配），不跨字段拼接造词；
- 匹配依据记录 `{field, term, variant}`，`variant=normalized_nfkc_lower_nospace`，
  即 NFKC + 小写 + 去空白；不声称命中了原始字符偏移。

## 3. 噪声门与“作品歧视讨论”有限例外

`content_filter.noise_analysis()` 返回 `{is_noise, reason, base_reason, exception, variant}`；
`noise_reason` / `is_noise` 兼容旧签名。硬噪声类别：`fiction / game / audio / drama / ad / charged`。

有限例外（仅 `fiction`、`drama`）：

- 必须同时出现讨论标记（讨论/争议/批评/反对/质疑/反思/为什么/为何/是否/如何看待/揭露/抵制/声讨…）
  与歧视标记（性别歧视/厌女/仇女/媚男/物化/性骚扰/家暴/刻板印象/男性凝视/性别偏见…）；
- 出现连载/计数标记（第N集/话/季、N天、`【…抢先版…】` 等）、广告、音声、充电标记时**不放行**；
- 例外不覆盖 `game / audio / ad / charged`，也不因“女性/游戏/剧情”单个词放开全部剧情。

例：`短剧《她的选择》为何引发性别歧视争议` → 保留；`短剧《她的选择》第3集：家暴剧情` → 仍为 fiction。

组合剧情噪声（本轮窄修）：家庭关系角色（丈夫/妻子/老公/老婆/婆婆/岳母/未婚夫/未婚妻）
\+ `病弱青梅`/`病弱白月光` + `坐不住`/`跪求`/`痛悔` 三者同时出现 → `drama`。
不单禁“青梅/白月光/病弱”；明确批评该类剧情的性别歧视（讨论/批评词 + 歧视词）仍走上面的有限例外，
连载/广告/音声/充电硬标记继续拦截。

## 4. 四轴分开（不得互相替代）

| 轴 | 取值 | 依据 |
| --- | --- | --- |
| `involves_conflict` | bool | 内容是否涉及性别对立/冲突（规则分层），**不代表作者赞同** |
| `author_stance` | `unknown`（本层唯一取值） | 规则层没有作者立场证据；负面情感不能替代立场 |
| `intensity` | `unknown`（本层唯一取值） | 没有校准证据；词命中数不是强度 |
| `heat` | `{known, value, source_field, basis}` | 只接受显式来源非负整数；否则 unknown |

热度规则：`observed_heat(value, explicit=...)`；历史库 `views`（`explicit=False`）一律
`unknown`，因为旧默认 `0` 可能是未知占位，不当真实热度，也不补零。

## 5. `conflict_score`（legacy 签名保留）

- 仅当 tier 为 conflict 时返回非 0；值为**去重后的冲突词/强特征命中数 × 10**；
- **不含播放量加成**（本轮移除），同一词重复出现不重复计分；
- 只是内容线索排序分，**不是校准的冲突强度或风险等级**；
- 历史 DB 中的旧 `conflict_score` 未重算，属于 legacy，不与新规则比大小。

## 6. 候选门的 `require` 枚举（权威定义）

`collection_policy.REQUIRE_ENUM = ("substantive", "relevant", "gender_related")`：

- `substantive`（默认，正式入库）：`tier ∈ {conflict, issue}`；仅主体词的普通刑案/生活事件拒绝；
- `relevant`：仅 `conflict`；
- `gender_related`：宽候选（含纯主体词，`gender_related=True`），只应写入拒绝/暂存证据，不进正式库。

未知值（拼写错误、大小写不同、空串、`None`、非字符串）→
`{"accepted": False, "reason": "unknown_require", "require_value": ..., "allowed_requires": [...]}`，
在时间/文本/URL 检查之前拒绝，不落入任何隐式宽进分支。

## 7. 采集前只读准备（`tools/prepare_collection.py`）

- 必须显式 `--db` 与 `--output-dir`；`--limit` 默认 3、上限 200，是**只读分块/检查保护的批大小**，
  不是导出上限：全部 records 按 `id` 稳定顺序用 `fetchmany(limit)` 扫描，队列不截断；
  贴吧候选另有硬上限 3 个；
- 库不存在直接失败；SQLite 用 `mode=ro` + `PRAGMA query_only=1` 打开，
  整个读取在**同一个显式 BEGIN 只读事务**内完成，结束 `ROLLBACK` 并关闭；
  不调用 `storage.connect`、不迁移、不建表、不写库；导出前后保留主库 SHA-256；
- 输出三个新文件：`precollection-queue.json`、`precollection-readiness.json`、
  `precollection-summary.md`；任一目标已存在则整体拒绝（不覆盖、不删除）；
- 队列项按 `record_id + input_hash`（`sha256(json[title,summary])`）标识，
  `resolution_status=pending`，保留存储 tier/旧分数/旧 views、模型输出与来源、当前规则四轴；
- **当前规则分歧**（`current_disagreement`）：`stored_tier_vs_current_rule`、
  `model_annotation_missing`、`model_opposition_vs_current_rule`、
  `model_relevant_vs_current_gender_related`、`model_gender_related_vs_current_rule`、
  `summary_missing`；
- **旧值原始分歧**（`legacy_disagreement`，单独统计）：`legacy_conflict_score_not_recomputed`、
  `legacy_score_without_conflict_tier`、`legacy_tier_vs_model_opposition`、
  `model_opposition_invalid_type`；
- 模型标注是否存在以显式 `m.record_id` 是否为 NULL 判定（LEFT JOIN 行存在≠有标注）；
  `model.opposition` 只接受 `0/1` 整数，其他原值保留在 `opposition_raw` 并标待核；
  历史 `model.relevant` 保存的是 gender_related 语义，只与当前 `gender_related` 比较；
- 所有 legacy `views` 都没有来源凭据（含旧正整数），`heat` 一律 `unknown`；
- 队列是**待复核线索**，不是语义裁决，不代表任何条数已人工确认；
- `grants` 恒为 `collection/model/browser/db_write/overwrite_artifacts = false`。

## 8. 贴吧准备

只读复用 `activity.admit`，不启适配器、不采样；候选最多 3 个。标签：
`window_net_gain_gt_50`（窗内已观测净增>50，下界达标）、`window_net_gain_le_50_partial`
（该观测区间≤50，只是下界、不能推出整窗不达标）、`single_point`、`insufficient_window_points`、
`no_count_evidence`、`stale_observation`、`count_retracted`、`source_field_changed`、
`same_time_conflict`、`invalid_count_value` 等。

达标条件：同帖、同身份、同 `comment_source_field`、两条以上 exact 非负计数、末点 15 分钟内、
无回撤，且窗口内实测净增 **> 50**。短区间观测净增 >50 即可作为窗内下界达标；
<=50 只说明该区间未达阈值，不能据此宣布整个 24 小时不达标。不限发帖时间；
当前总回复数、单点计数不能替代观测净增。

## 9. 明确不在本契约内

- 不修改 storage/server/UI/旧标注工具，不迁移/回填真实库；
- 不新增采集来源、不启动持续采样、不调产品分析模型、不接管浏览器；
- 未复核的模型含义不批量覆盖历史标注；真实 GUI 证据、真实采样证据仍为缺项。

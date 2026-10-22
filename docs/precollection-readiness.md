# 采集前准备就绪说明（2026-10-20）

## 最新核验事实（2026-10-20，覆盖下文作者阶段验证快照）

当前代码 `classification-core-v4`；最终限定检查 `92/92`、exit 0、stderr空；原项目真实只读CLI全扫 `111/111`，业务主库SHA前后不变。重放34 conflict/63 issue/14 unrelated；原库存储33/78保留。旧口径与模型分歧52条，当前规则与模型分歧50条，111条队列全部pending，含缺摘要/缺标注等项，未作批量裁决。

OpenCode V4 Pro与GPT-6 Luna已关闭本轮已识别P1；代码/离线检查/只读导出可接受，真实GUI仍未验收，贴吧真实适配器及持续采样未启动，整体采集准备不能宣称PASS。本轮没有新增采集、启停业务服务或浏览器操作。

最终报告：[采集前准备交付报告](C:/Users/Lenovo/.codex/documents/sentiment-precollection-20261020/采集前准备交付报告-20261020.md)。
执行边界及后续顺序：[必须注意的边界与后续顺序](C:/Users/Lenovo/.codex/documents/sentiment-precollection-20261020/必须注意的边界与后续顺序.md)。
最新机器证据：[交付机器证据账本](C:/Users/Lenovo/.codex/documents/sentiment-precollection-20261020/交付机器证据账本.json)。

下文76/78项和“待主线程”的叙述保留为作者阶段历史，不代表当前验证状态；未完成的真实GUI、语义裁决及采样仍然未完成。接手Agent先读本节及最终报告，不重复派发历史阶段。


状态：**实现完成；宽候选兼容修订前宿主检查 76/76，修订后检查与两审待主线程；真实库/GUI/采样未验证**。本文不宣布产品 PASS。

## 1. 本轮实际改动（8 文件白名单）

| 文件 | 变更 |
| --- | --- |
| `relevance.py` | 新增唯一内核 `analyze()`；`classify/classify_tier/explain/is_relevant/is_gender_related` 全部改为调用它；强特征独立成立；医学排除一致；四轴（涉及冲突/立场/强度/热度）分开；`conflict_score` 去 views 加成、去重、仅内容线索 |
| `content_filter.py` | 新增 `noise_analysis()`（可审计返回）；硬噪声保留；新增“明确讨论/批评作品中的性别歧视”有限例外（仅 fiction/drama，连载/广告/音声/充电硬标记不放行） |
| `collection_policy.py` | `gate_candidate` 只接受 `REQUIRE_ENUM=(substantive, relevant, gender_related)`；未知/拼错/非字符串 → `unknown_require` 结构化拒绝（先于时间/文本/URL 检查）；改为一次内核调用取噪声+tier+宽口径 |
| `tools/prepare_collection.py`（新增） | 只读 SQLite（`mode=ro`+`query_only`+单一显式 BEGIN→ROLLBACK）**全库稳定顺序扫描**（`--limit` 仅 fetchmany 分块，不是导出上限）；显式 `--db`/`--output-dir`/`--limit`；缺库失败；拒绝覆盖；不调 `storage.connect`、不联网、不写库；模型缺失按 `m.record_id` 判定；当前规则分歧与旧值原始分歧分开统计 |
| `tools/classification_checks.py`（新增） | 三组离线检查：A 跨接口+噪声回归；B 四轴/unknown/旧分数分离/require 拼错；C 隔离临时库准备 CLI（全库扫描、缺模型、历史分歧、缺库/覆盖/limit）+贴吧边界；fixture 目录保留在 `_checks_out/` 并在报告给路径，不删除 |
| `docs/classification-contract.md`（新增） | 契约正文：优先级、接口映射、四轴、噪声例外、require 枚举、队列语义、贴吧口径 |
| `docs/precollection-readiness.md`（新增） | 本文件 |
| `docs/agent-operations.md` | 追加本轮入口、验证命令与未完成项 |

未修改：`storage.py` / `server.py` / UI / 旧标注工具 / `TASK.md` / `AGENTS.md`。

## 2. 已验证 / 未验证（分开列）

**静态实现层（本轮作者完成）**

- 代码路径与契约逐条对应；标准库依赖（`sqlite3/json/hashlib/argparse/...`）；
- `prepare_collection` 不 import `storage`、不调用 `storage.connect`；
- 全部记录按 id 稳定顺序扫描（`fetchmany(limit)`），`--limit` 不再作为 SQL LIMIT 截断导出；
- 输出文件为三件新产物，目标存在即整体拒绝，无删除逻辑；
- 检查脚本不清理自有 fixture 目录，报告 `fixture_dir`；不删除任何文件；
- `gate_candidate` 未知 require 在任何宽进分支之前返回；
- `model_annotations` 是否存在以 `m.record_id` 判定；非法 opposition 原值保留待核；
  `model.relevant` 只与当前 `gender_related` 比较；旧 tier/model 原始分歧单独统计；
- 通过阅读 `activity`/`storage` 真实字段（`records.relevance_tier/conflict_score/views/time_quality`、
  `model_annotations.opposition/relevant/gender_related`、`p1_evidence.comment_value/precision/source_field`）
  对齐查询与 fixture。

**未验证（重要）**

- **本轮修订后的限定检查与两审尚未运行**：迄今唯一的 76 项证据产生于本次宽候选兼容修订**之前**；
  修订后的检查与 V4/Luna 收束等待主线程，不引用为修订后的通过数。
- **宿主已跑过初次检查（历史证据）**：首次 `classification_checks.py --limit 3` 因
  `KeyError: 'noise'` exit 1（FAIL）；修复后再次运行 exit 0、`total=76 passed=76 failed=0`，
  证据 `C:/Users/Lenovo/.codex/documents/sentiment-precollection-20261020/fixed-checks.json`，
  `fixture_dir=workspace/_checks_out/classification-20261020T142926Z-42184-v_bevwk5`。
- **无真实库**：本副本没有 `.sqlite` 业务库，`prepare_collection` 的真实只读导出、哈希不变、
  队列条数、旧分数/未知热度分布均未实测；需主线程在真实库上执行。
- **无真实 GUI 证据**：未开浏览器，未截图；Ego 指南、静态检查、HTTP 均不能替代 GUI 验收。
- **贴吧未采样**：未启动适配器、未产生新计数；仅提供只读观测契约与下一次采样要求。
- 未做语义人工裁决；分歧队列 `pending` 不代表任何条数已复核。

## 3. 发现与保留的风险 / 未做项

1. **模型含义未复核**：新规则只解决“接口不一致”。`model_annotations` 的 opposition/relevant/
   gender_related 历史值含义未核，不能被新规则批量覆盖，也不能当人工真值。
2. **旧分数 legacy**：真实库 `conflict_score` 仍含历史 views 加成，本轮不重算；
   新函数已移除 views，但旧值只作 legacy 展示，禁止与新值混比。
3. **旧 views 不可当热度**：`views=0` 可能是历史占位；所有 legacy views 都没有来源凭据，
   准备工具一律 `heat=unknown`（旧正整数也不当有凭据）。
4. **准备 CLI 的真实库兼容性未实测**：若真实库缺少 `model_annotations` / `p1_evidence` 或
   `records` 缺少可选列，代码会降级为 `optional_missing` + warning（核心必需列缺失则结构化失败），
   但该降级路径只在隔离 fixture 中设计，未在真实库复现。
5. **`prepare_collection` 的 `--limit` 是分块大小**：全部记录按 id 稳定顺序扫描，
   `queue` 不截断；`chunk_size`、`records_scanned`、`records_total` 均可见。贴吧候选仍最多 3 个。
6. **噪声例外有有限风险**：讨论词 + 歧视词同时出现才放行，仍可能误放“带讨论口吻的剧情号”；
   反向也可能误杀少量真实讨论。需真实样本回放再调，不在本轮扩大词表。
7. **未改旧 `docs/collection-policy.md`**：该文件不在本轮白名单，`require` 权威定义已移到
   `docs/classification-contract.md`；旧文件仍描述 `substantive/relevant/gender_related` 的旧行为，
   未同步，存在文档分叉（已如实记录，不擅自改白名单外文件）。
8. **未做两轮独立审查**：本轮只交实现差异，等待 V4/Luna 只读审查。
9. **检查脚本会保留 fixture 目录**：每次运行在 `_checks_out/classification-<时间>-<pid>-*` 新建，
   报告给出 `fixture_dir`；按不自动清理约束，脚本不删除，需人工决定是否保留。
10. **窄修与历史证据**：初次检查因 `KeyError: 'noise'` exit 1；修复后宿主运行 exit 0、
    `total=76 passed=76 failed=0`（证据见上，属宽候选兼容修订前）。420 秒超时中断的旧修订不计成功。
    本次宽候选/文档修订后未再运行检查，等待主线程复跑与两审。

## 4. 主线程待执行的验证（勿由实现者代跑）

```text
# 隔离离线检查（先 --limit 3；目录会保留，不自动清理）
python tools/classification_checks.py --limit 3

# 真实库只读准备（示例；输出目录必须是新目录）
python tools/prepare_collection.py --db <真实库> --output-dir <新目录> --limit 3
```

验收时至少回读：

- 检查报告 `failed=0`、`fixture_dir` 实际存在，与各失败 detail；保存完整 stdout/stderr 与退出码；
- 准备 CLI 输出的 `db.sha256_before/after` 一致、`network_requests/model_calls/db_writes=0`、
  `grants` 全 false、`queue` 全 `pending`、`records_scanned == records_total`、
  `chunk_size` 可见、当前规则分歧与旧值分歧分开、贴吧候选≤3；
- 真实库与旧标注文件哈希在导出前后不变；
- GUI 走真实标签页：生成日报 → 预览 → 下载落盘 → 打开 PDF 并做视觉核对；
  静态检查/HTTP 不能替代；
- 贴吧：短区间观测净增 >50 可作为窗内下界达标；<=50 只说明该区间未达阈值，不能推出整窗不达标。

## 5. 结论

- 接口不一致的**代码修复**已落地；宽候选兼容修订前宿主检查为 76/76（见第 2 节证据），
  修订后的检查与两审待主线程；真实库、真实采样、真实 GUI 与语义裁决**未验证**。
- 本准备默认**不授予**采集、模型、浏览器、数据库写入权限。
- 在修订后检查与真实库只读导出完成前，不能宣布整套产品 PASS。

# Agent 执行入口

## 浏览器统一入口（用户指定，2026-10-20）

采集、页面展示和真实UI验收统一使用桌面 `ego (lite).lnk` 对应的Ego Lite及 `ego-browser` 接口；项目规则已覆盖旧Chrome/Edge偏好。用户说明账号已配置，后续复用已有登录态，不读取凭据、不重登、不清理存储。

已完整读取当前指南v2.1.0，快捷方式目标已只读核实；本轮没有连接、导航或操作真实浏览器。现有采集脚本尚未因规则写入自动迁移到Ego，需补薄适配并先3条实机核验；登录态和来源页面字段也尚未逐平台实测。历史8790拒绝未取得解除证据，不能把采用Ego当成解除。

详细未改项：[采集前未完成项与Ego统一入口](C:/Users/Lenovo/.codex/documents/sentiment-ego-preflight-20261020/采集前未完成项与Ego统一入口.md)。本轮规则/文档取证：[规则与指南读取证据](C:/Users/Lenovo/.codex/documents/sentiment-ego-preflight-20261020/规则与指南读取证据.json)。


## 最新核验事实（2026-10-20，覆盖下文作者阶段验证快照）

当前代码 `classification-core-v4`；最终限定检查 `92/92`、exit 0、stderr空；原项目真实只读CLI全扫 `111/111`，业务主库SHA前后不变。重放34 conflict/63 issue/14 unrelated；原库存储33/78保留。旧口径与模型分歧52条，当前规则与模型分歧50条，111条队列全部pending，含缺摘要/缺标注等项，未作批量裁决。

OpenCode V4 Pro与GPT-6 Luna已关闭本轮已识别P1；代码/离线检查/只读导出可接受，真实GUI仍未验收，贴吧真实适配器及持续采样未启动，整体采集准备不能宣称PASS。本轮没有新增采集、启停业务服务或浏览器操作。

最终报告：[采集前准备交付报告](C:/Users/Lenovo/.codex/documents/sentiment-precollection-20261020/采集前准备交付报告-20261020.md)。
执行边界及后续顺序：[必须注意的边界与后续顺序](C:/Users/Lenovo/.codex/documents/sentiment-precollection-20261020/必须注意的边界与后续顺序.md)。
最新机器证据：[交付机器证据账本](C:/Users/Lenovo/.codex/documents/sentiment-precollection-20261020/交付机器证据账本.json)。

下文76/78项和“待主线程”的叙述保留为作者阶段历史，不代表当前验证状态；未完成的真实GUI、语义裁决及采样仍然未完成。接手Agent先读本节及最终报告，不重复派发历史阶段。


本文件由接手 Agent 维护。它是项目的操作索引，不是用户逐项签字清单；不能替代 AGENTS.md 的授权、安全与验收规则。

## 固定证据入口

- 当前业务现状与优化顺序：[2026-10-20 现状报告](C:/Users/Lenovo/.codex/documents/sentiment-status-20261020/舆情现状与识别优化报告-20261020.md)。
- 同时刻本库只读证据：[现状只读证据.json](C:/Users/Lenovo/.codex/documents/sentiment-status-20261020/现状只读证据.json)。
- 规则回放：[三例离线规则回放.json](C:/Users/Lenovo/.codex/documents/sentiment-status-20261020/三例离线规则回放.json)。合成样例不是准确率评测。
- 400条工作包交接：[详细交接报告](C:/Users/Lenovo/.codex/documents/sentiment-activity-20261019/optimization-400/详细交接报告-20261019.md)。
- 完整注意事项：[注意事项完整清单](C:/Users/Lenovo/.codex/documents/sentiment-activity-20261019/optimization-400/注意事项完整清单-20261019.md)。其中当轮暂停、预算和端口状态属于历史，须结合最新用户指令与现场证据。
- 通用恢复工具及 Ego 边界：[简单修复与 Ego 报告](C:/Users/Lenovo/.codex/documents/conversation-interruptions-20261020/简单修复落地与ego评估报告-20261020.md)。该本地检查点尚未接入产品调度。

## 每轮由 Agent 负责的动作

1. 读规则、当前报告及相应账本；只读核工作区、Git dirty、真实 DB/入口。不要复用旧 PID 或将旧数字当实时。
2. 写本轮短任务卡：目标、当前事实、具体授权、范围、允许文件、验证、预算和停止条件。已具备具体授权则继续，不重复问同一问题。
3. 核实际读取方，再派实现或执行；业务改动沿用项目指定 OpenCode 分工。默认不启永久调度，不擅自追加采集规模、来源、模型、评论正文或删除数据。
4. 对应范围保留命令/退出码/输出、前后哈希、原生消息/模型、实际新终态。输出截断回读原文件；不凭派发成功或旧 succeeded 结案。
5. 报告已做、未做、实际阻断与下一动作；未开始的工作不冒称 BLOCKED。缺少决定性验收证据时不宣布 PASS；产品真实路径按机器、独立审计及真实客户端证据结案。

## 业务口径必须保持

- 固定日、最近24小时、最近48小时分别定义带时区的半开区间；发布时间未知不能用 fetched_at 代填。按具体已批准清单删除，不因滚动窗口变化自动清库。
- 贴吧要求最近24小时新增回复 >50，不限发帖时间；总回复数、发帖时间、单点不能替代。窗口内短区间净增>50可作下界达标；<=50不能推出整窗不达标。缺历史标观测不足。
- conflict/issue/unrelated 是相关度分层；作者是否攻击、内容涉及冲突程度、负面情感和传播热度不是同一字段。词出现不代表作者赞同。
- 历史库 conflict_score 是含播放量的 legacy 旧分，未回算；core-v4 新函数仅作不含热度的内容线索分，均未校准成风险等级。规则/模型冲突保留双值与版本、送待复核，不按一方自动批量改库。
- 日报仅按既定四类及有效发表时间取样；保留统一成员及排除原因。跨平台指标不混加；未知计数不当零。已落地适配器不等于实跑成功，事件表数量不等于事件质量。
- 浏览器来源拒绝须走受支持恢复路径；不换浏览器、域名、端口或底层通道绕过。Ego 能力指南不是本项目 GUI 验收证据。

## 当前接手顺序（2026-10-20，需下一轮回读）

1. 本库只读审计仍111条；原400目标新增2，未完成。先核分层/标注分歧及不同入口差异，再决定采样。
2. 先统一分类契约，分离强度和热度，建立少量人工裁决基准；不要用新增条数掩盖识别问题。
3. 补有效发布时间/计数详情适配，先限定实测再扩大来源；贴吧持续计数尚无已启动证据。
4. 真实GUI来源恢复后再验收生成、预览、下载和PDF；不拿后台或静态检查替代。

以上是交接计划，尚未执行的产品优化不记已完成。无需用户维护清单；Agent 每次推进应同步对应现状报告和证据，不能承诺未建立的跨回合后台自动运行。

## 本轮采集前准备入口（2026-10-20，需下一轮回读）

- 契约正文：[classification-contract.md](classification-contract.md)；实现/未验证清单：[precollection-readiness.md](precollection-readiness.md)。
- 唯一分类内核：`relevance.analyze()`；`classify/classify_tier/explain/is_relevant/is_gender_related`
  与 `collection_policy.gate_candidate` 全部走该内核；`require` 权威枚举在契约第 6 节。
- 只读准备 CLI：`python tools/prepare_collection.py --db <真实库> --output-dir <新目录> --limit 3`
  （`mode=ro`+`query_only`+单一 BEGIN/ROLLBACK；`--limit` 仅为 fetchmany 分块，全部 records 稳定顺序扫描，
  不截断导出；贴吧候选最多 3 个；不调 `storage.connect`，不覆盖旧产物，不授予采集/模型/浏览器权限）。
- 离线检查：`python tools/classification_checks.py --limit 3`（三组：跨接口/噪声、四轴/require、隔离库+贴吧边界）。
  宿主已有修复后 exit 0、`total=76 passed=76 failed=0` 证据（宽候选兼容修订前），
  证据 `C:/Users/Lenovo/.codex/documents/sentiment-precollection-20261020/fixed-checks.json`；
  修订后复跑与两审待主线程，不把 76 项当修订后结果。真实库导出同样待主线程。
- 检查 fixture 目录保留在本次副本 `_checks_out/`，报告给出 `fixture_dir`；脚本不删除文件，人工决定保留。
- 分歧队列按 `record_id + input_hash` 标识、`resolution_status=pending`；当前规则分歧与旧 tier/model 原始分歧
  分开统计（`current_disagreement` / `legacy_disagreement`）。是待复核线索，不是语义裁决，
  不能批量覆盖模型标注或历史 `conflict_score`。
- 贴吧仍未启动真实采样：短区间观测净增 >50 可作为窗内下界达标；<=50 不能推出整窗不达标。GUI 仍缺真实证据，
  且必须覆盖生成日报→预览→下载落盘→PDF 打开与视觉核对；旧来源拒绝不绕过，preflight 默认不授权。

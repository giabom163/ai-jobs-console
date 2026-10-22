# 采集策略（collection policy）落地说明

本文件随 `content_filter.py / relevance.py / collection_policy.py / tools/collect_*`
本轮实现记录**实际参数、证据字段与未实现边界**。不伪造未验证结论。

## 1. 统一噪声过滤 `content_filter.py`

- 分类：`fiction / game / audio / drama / ad / charged`。
- 本轮新增标记（来自真实漏网样本归纳，仅按可解释标记，不泛禁单词）：
  - fiction：`剧情 / 假故事 / ai故事 / ai全民制作人 / 听别人说故事 / 故事频道 / 民间故事 / 抢先版`；`第N集/话/季`、`【…抢先版…】`、`【N~M季】` 连载标记；
  - game：**行为/计数标记** `实况 / 初见流程 / 流程解说 / 游戏实况 / 实机演示`，或
    `攒彩礼 / 打卡 / 通关 / 抽卡 / 开箱 / 苟卡` **与游戏专名或计数标记（第N天/期）共现**；
  - audio：`音声 / asmr / 助眠 / 广播剧 / 白噪音 / 催眠`。
- **单字不判**、**不使用游戏专名**泛禁：`现在年轻人攒彩礼有多难？`、`女玩家通关率被质疑：
  游戏圈性别歧视`、`女性职场打卡`、`原神女性角色物化与性别歧视争议` 均保留（正例检查覆盖）。

## 2. 相关度分层 `relevance.py`

- 三档：`conflict`（主体词+冲突词/强特征）、`issue`（主体词+婚育/政策/制度/权益信号）、`unrelated`。
- **不再**“只要有主体词就是 issue”：`女子在超市盗窃` 之类普通刑案/生活事件 → `unrelated`；
  `家暴/彩礼/婚育政策/性别歧视` 等实质议题保留（`ISSUE_TERMS` 覆盖 政策/法律/权益/职场/社保/退休…）。
- 噪声门与采集层共用：`content_filter` 判为噪声时，`classify_tier/classify/explain`
  一律 `unrelated / 不相关`，保证“各接口口径一致”，严格过滤不止放在 CLI 端。

## 3. 候选门与时间证据 `collection_policy.py`

- `gate_candidate(...)`：时间可追溯 + 非噪声 + 相关度 + URL 合法；
  正式入库默认 `require="substantive"`（`classify_tier ∈ {conflict, issue}`），
  **拒绝**仅 `is_gender_related` 为真的普通 gender-only 刑案/生活事件；宽候选
  （`require="gender_related"`）只应写入拒绝/暂存证据，不进正式库。
- `normalize_published(...)` 时间模式：
  - `epoch`：非 bool 精确秒整数，`precision=second`，`valid`；
  - `iso`：带时区 ISO，`valid`；缺时区 `timezone_missing`；
  - `relative`：**必须**同时有 aware `observed_at` + 明确 `source_timezone`（本批仅
    `Asia/Shanghai/UTC`）+ 发布语义 `source_field`（编辑字段拒绝）+ 精度；
    未传精度按格式推断并**记录**（不出现 quality=valid 而 precision=null）；
    日期级（`昨天`无时刻 / `N天前`）→ `day_precision_only` 拒绝，不补成精确小时；
    坏时刻（如 `25:00`）→ `invalid_time_of_day` 结构化拒绝，不中断整批；
  - ID 派生（`derived_from="id"`）、缺年份、抓取时间：一律拒绝。
- 证据块 `snapshot_evidence(...)` 随 `raw_xml` 写入既有快照字段，**无 schema 迁移**；
  快照同时保留**原始发布值**（如 B 站 `pubdate` 原始秒）、显式 `source_field=pubdate`、
  `precision=second` 与原 `time_evidence`，不用规范 ISO 冒充 raw；`views_known=false`
  时不得伪造 0。

## 4. B 站采集器 `tools/collect_bilibili_24h.py`

- 复用 `collection_policy` 统一门（时间 epoch + 噪声 + 相关度 + URL）。
- 停止原因修正为 `target`（达上限）/ `exhausted`（关键词/页耗尽未达目标）/ `budget`
  （HTTP 预算用尽）；未达目标 `status=partial`、退出码非 0；HTTP/业务错误保护保留。
- 新增薄参数 `--page-start`（供父调度器分页，避免重复页 1）；`--full-items` 输出
  完整标题/views/pubdate。单页条数按实测 `page_size=30`（适配器 search 参数 + 真实
  ledger `pagesize=30/result_len=30`），非未核实的 20。

## 5. OpenCLI `tools/collect_opencli.py`

- 新增真实安装路径候选 `D:/DSH/tools/opencli-1.8.8/node_modules/@jackwener/opencli/dist/src/main.js`，
  用前用 `_first_file` 校验存在，缺失即报错，不传 None。
- `xiaohongshu-search` 字段按**真实扁平** `title/url` 映射；上游 `published_at` 是
  **ID 派生日期**，映射为 `None`，不得作为发布证据入有效库。断连/登录墙终止该平台，不读凭据、不装插件。

## 6. 多平台调度 `tools/collect_multiplatform.py`

- CLI：`--limit` 必传 1..400（**父进程总上限**）；`--date`（对**全部来源**生效）；
  `--workers<=3`；`--max-calls` 默认 2 上限 60；`--interval>=20`；`--platform`（六既有平台）；
  `--dom-evidence`（严格 schema，可重复）。
- **真实 multiprocessing(spawn) worker**（1..3），worker 只取源/解析；**父进程唯一
  writer + 配额仲裁**；父进程经 Queue 发 **permit**，B 站 worker **每个真实 HTTP 前**
  也经父进程同一许可（不再先跑完子进程再补记）。浏览器任务同时只允许一个在跑。
- 子限额分块：B 站单次 `--limit<=100`、`--max-requests<=20`、`--pages<=10`（关键词轮换 +
  `--page-start` 递增，不重复页 1，参数上允许累计 >= `--limit`，如 4×100=400），
  OpenCLI 单次 `<=50`；父进程累计 **总 added<=--limit**，其余候选保留为拒绝/暂存证据。
- 持久全局 `CallGate`（父进程独有 JSON 账本，跨启动保留 last_call/cooldown）；
  429/限流 → **本轮立即停全部来源请求** + 持久**全局**冷却 >=300s（换 source 也不能绕过）；
  `LOGIN_WALL`/`AUTH_REQUIRED`/`BROWSER_CONNECT` → 停该来源（浏览器信号停全部浏览器任务）。
- permit 授予前会再收结果/风控，间隔等待分段进行（429 到达可立即全局停）；每次 permit
  即时持久化账本；`--dry-run` 同样节流并持久化（不因 dry-run 绕过限流）。
- worker 死亡/任务超时 → 回收并重启该 worker，任务如实失败，后续任务继续，不无限 busy。
- 全部来源（含 DOM）严格比较上海 `date` 的 `[start,end)`，未来/不明时间拒绝；
  入库前只读去重预检 **fail-closed**（缺库即拒绝，不在缺库时联网或用 storage.connect 顺手建库；
  isolated 测试需先显式 `storage.connect` 建自己的临时库），URL/content hash/SimHash 先去重，
  旧 URL 换 feed 不算新增；每 `storage.start_run/import_batch` 批 <=100。
- OpenCLI 行的 `raw_xml`（既有声明映射的原始条目证据）透传保留；B 站原始 `pubdate`/
  `time_evidence` 透传；`views` 未知不转换 0。
- 原生 OpenCLI 内部 HTTP 次数标 `native_http=unknown`，不宣称硬受控。

## 7. 定向检查 `tools/collection_policy_checks.py`

- `--limit N`（默认 3）只控制 spawn 场景 fixture 行数，**不截断**断言用例；
  三组全覆盖 + 两个 scheduler/spawn 场景：A 真实漏网正反例 + 上轮误杀正例 +
  gender-only 刑案不入正式库、B 时间锚点/边界/缺年/ID 派生拒绝、C 多 worker 预算/
  唯一写入/429/partial、**真实 run_scheduler**（3 worker：共享 permit 即时持久化、
  浏览器串行完成、429 即停全来源 + 全局冷却持久化、worker 死亡回收重启后任务继续）、
  **真实 spawn 父唯一 writer**（3 份 DOM fixture 任务走 3 个不同 worker；校验 worker
  PID≠父 PID、父 writer PID 回读、日期窗、去重、工作区内临时库、总 limit、partial）。
- 本轮 `--limit 3` 实测：**总 53 项、passed 53、failed 0、exit 0**（完整 stdout/stderr 存档
  于工作区 `_checks_out/`）；纯模拟无网络，不调用真实源/模型、不写真实库；临时证据仅在
  工作区子目录且运行后清理。

## 8. 首审（V4）修订对照

- P0-1/P0-2：子采集器单次上限（B 站 100/20、OpenCLI 50）——分块 + 父总 limit 已修；
- P1-1：429/登录墙/断桥结构化信号 + 停来源 + 持久冷却——已修（不再吞成空表）；
- P1-2：`content_filter` 攒彩礼/打卡/通关改为**组合**判定——已修；
- `--workers` 无操作 → 真实 spawn 多进程——已修；
- `--date` 仅 B 站生效 → 父进程统一窗口（含 DOM）——已修；
- `arm_cooldown` 坏账隐患 → 同时写 `last_call_at`，`load_ledger` 兼容——已修；
- 多平台落库丢 `views`、截断标题 → 子 CLI 增 `--full-items`，父落库保留完整标题/views——已修。

## 未实现 / 未验证边界

- 本轮**未**实跑真实来源、**未**写真实库、**未**做两轮独立审查、**未**做 UI/下载/PDF 验收；
  400 条目标由主线程后续实采，本文件不声称达标。
- 原生 OpenCLI 内部 HTTP 受控程度未知；B 站子进程内逐 HTTP 间隔由父 permit 保证。
- DOM 证据 JSON 仅定义严格 schema 与校验，实际采集由主线程真实浏览器完成。
- 真实多进程并发取源（多来源同时在线）尚未实机压测；本轮仅验证 1 个 spawn worker
  的隔离/唯一写库/窗口/去重路径。

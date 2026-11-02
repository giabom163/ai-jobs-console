# AI 岗位核查台

公开岗位的聚合与核查：采集公开招聘页面里的 AI 研发 / AI 支撑岗位，按规则判定是否真属于 AI，核验链接存活，最后发布成静态站。

- 线上站：https://giabom.online
- 核查台源码、数据和发布产物统一维护于本仓库；旧WorkBuddy站点仅保留为迁移来源，不再作为新发布入口。
- 本仓库：数据流水线 + 规则 + 导出（https://github.com/giabom163/ai-jobs-console）

> 旧「舆情观察台」（性别议题监测）已下线并清除，代码只保留在归档仓库
> `giabom163/sentiment-monitor` 与本仓库历史提交里，不再参与运行。

## 目录

```
ai_jobs.db            主库（SQLite，含 jobs / companies / job_changes / job_snapshots）
tools/ai_lexicon.py   AI 判定词典与分级规则（唯一事实源）
tools/collect_*.py    来源适配器；不在请求白名单的操作会在网络派发前拒绝
tools/reclassify_jobs.py  按词典重算 + 剔除 none
tools/dedup_jobs.py   去重
tools/liveness.py     三态存活判定（active / uncertain / expired）
tools/prune_jobs.py   存活复检 → closed / stale
tools/verify_jobs.py  URL 校验 + 真实性打分
tools/export_console.py   导出站点静态数据
tools/build_site.py   只读主库，生成本仓库site/静态产物
site/                HTML、JS、JSON、重定向及构建清单
deploy.ps1           直接发布site/，无需旧Astro工程
tools/ai_server.py    本地 API 服务（127.0.0.1:8792）
web/index.html        核查台前端源码
```

## 日常流水线

采集、重新分类、存活检查和链接核验须根据当前授权与来源限制单独执行，先用 `--limit 3` 小范围试运行；不得直接批量运行旧流水线。去重工具生成待复核候选，不自动合并或删除岗位。静态站构建见下方“统一静态发布”。

## 判定口径（摘要）

- 时间窗：按 `first_seen >= 2026-06-01`，默认近30天首次入库；来源发布/更新时间独立，未知保持空值。
- build：强 AI 词 + 研发动作；adjacent：强 AI 词 + 产品/运营/评测，或 AI 相邻技术词；其余 none（状态 excluded，不物理删除）。
- 排除职能岗（招聘/人事/人力/财务/税务/法务/销售/商务/渠道/客服…）、「（AI 驱动）」类限定语、机器人硬件。
- 商业化/合规/负责人/经理/TPM 归 adjacent（标注 `business:`），不算研发。
- 存活三态：`uncertain` 永不判死；已准入的美团单岗位详情走官方详情接口。ATS 全球职位板、美团未限定地域的列表和 DeepSeek 搜索/抓取当前均不准用于岗位请求；不得先拉取再按地点筛选。

完整收录规则见本仓库AGENTS.md；新静态发布不再依赖旧站点仓库。

## 本地查看

```powershell
.\launch_ai_jobs.cmd
```

打开 http://127.0.0.1:8792/ （只读服务）。

## 边界

- 历史数据覆盖 8 家来源，不代表当前来源请求白名单，也不代表全市场。
- 「已验证」只代表链接当日可访问，不代表岗位真实或仍在招聘。
- 数据靠手动跑流水线，没有定时任务。

## 统一静态发布（2026-10-25）

```powershell
python -B tools\build_site.py --limit 3
# 按已有发布授权，检查通过后执行
.\deploy.ps1 -Publish
```

构建只读取本仓库主库与web/，生成site/，不采集、不运行测试、不部署。
Cloudflare Pages仍使用giabom-blog项目及main分支；控制台若仍绑定旧仓库，自动构建配置尚未迁移，不能把本地入口迁移说成线上配置已改。

职能、招聘类型、四城标签与岗位要求从原始岗位只读派生；不会改变首次入库或AI收录规则。
岗位要求最多10条原文要点，区分要求与优先项；未明确分节时优先保留待确认；历史文本的第二次编号列表结合资格措辞可生成带“边界待复核”的摘录，不把结果当成人工确认。
旧目录未删除；清理需另列文件清单确认。2026-10-26 版本已发布，线上详情和数据经真实页面DOM核对；首次索引加载曾超时，重试成功，原因仍待定位。视觉截图验收尚未完成。

### 美团来源日期门槛（2026-10-29）

默认公开的美团岗位须有不早于北京时间 2026-06-01 的官方来源更新时间（列表 `refreshTime`）。日期过早或缺失时保留原记录并转入待复核/从公开列表排除；仅日期达标不能代替官方详情岗位身份核验，也不会自动恢复待复核状态。历史兼容字段只用于筛查，HTTP 200 不代表岗位仍在招聘。细则见项目 `AGENTS.md`。

### 2026-10-29 数据范围与 P0 入口

美团经官方详情接口核查返回的 AI 岗位保留；未核实记录以 verification_pending 暂离默认展示，保留待复核。历史核查不保证当前仍在招，来源构成不代表全行业。

统一标准化与校验：`tools/ingestion_contract.py`；离线入库、恢复、ATS 均接入，ATS 默认 `--limit 3`、上限15。批次账本 `ingestion_batches` 区分新增来源记录和待跨源复核候选，独立净新增未确认时为 null。

新记录以 review 保存；来源在招声明与网页存活不自动提升业务状态。当前 API 与静态站共用 `export_console.build`；`export_web.py` 仅兼容同一导出。历史 Astro 专用旧字段口径已停止。

重建且清除已枚举的旧生成包：`python -B tools/build_site.py --limit 3 --prune-obsolete`。常规构建不自动删除旧包；部署检查会拒绝不在当前清单中的文件。


### 第一、二批数据安全修复（2026-10-29）

- `prune_jobs.py` 的未知结果只更新观测字段，不按岗位年龄改成 stale/closed。`apply_reviewed_merges.py` 不再覆盖原记录 first_seen，岗位组跨度保存到合并事件。
- `verify_jobs.py --score-only --limit 3` 仅本地重评分，使用独立 scored_at，不刷新 last_verified_at/last_checked_at 或 HTTP 结果。链接可访问不称为岗位已核实。
- 核验与采集先收集有限来源响应，再开始业务写事务；核验提交前检查目标记录是否变化。详情必须匹配来源岗位ID，未知招聘状态代码保持 uncertain，存在证据、原始状态、响应哈希独立保存。
- 当前写入入口在变更schema前生成SQLite一致性备份，迁移单独提交、业务批次单独事务，失败记录落 reports/batch-failures。迁移可重放；业务失败不回滚已完成的独立迁移。
- 真实请求默认顺序执行、同host至少20秒、一轮最多15请求（重试与重定向均计数），拒绝即停；有效请求上限取任务明确授权、项目轮次和来源配额三者最小值，代码上限不构成授权。429冷却至少5分钟且尊重更长Retry-After。BOSS/智联/58未取得平台许可时拒绝自动请求。离线导入的记录上限与联网请求上限是两个概念。
- `reclassify_jobs.py` 默认预览，写入须显式 `--apply`，先 `--limit 3`。旧 `import_signals.py` 暂停写旧表；就业舆情新链路属于第三批。
- 采集/核验的 `--dry-run` 只表示不写库，仍可能发来源请求，需要适用来源授权。没有自动运行测试、采集或迁移主库；新代码运行验收尚待完成。

### 请求准入与跨进程调度

- `tools/request_policy.py` 在共享调度器前执行来源白名单：核对 source ID、operation、method、host、path 与 Mainland 地点/岗位 ID。当前仅允许已存在 Mainland-only 美团记录的官方详情接口；ATS 全球列表、美团 `cityList` 未限定列表、DeepSeek 搜索/抓取和任意 URL 活性检查均在派发前拒绝。新增准入必须先证明请求本身限定中国大陆，再登记具体 endpoint。
- 地点分类唯一入口为 `tools/job_geography.py`；只有 `domestic` 可采集写入和默认公开，`mixed/foreign/unknown` 不通过资格门禁。招聘状态与身份/真实性证据不由地域分类改写。

### 跨进程请求调度

- 通过白名单的采集/核验请求进入 `.request-scheduler.json`：同主机至少20秒、所有进程一轮累计最多15请求、最多5个在途请求；同主机已有请求未关闭时拒绝第二个。重定向占请求次数，响应关闭才释放名额。DeepSeek 当前在白名单前拒绝，不会派发。
- 第一轮共享创建；耗尽后不会随进程重启自动恢复。明确开始新一轮用 `python -B -m tools.request_policy --start-round --round-id <唯一批次名称> --limit 3`；该命令只管理账本，不采集。有在途或意外中断名额时拒绝重置，先查进程与日志，不删除账本来绕过。
- 新轮不清除历史主机间隔、403/验证码等拒绝；429继续遵守共享冷却与更长 Retry-After。异常退出可能留下名额，保守阻断优先于自动放宽。
- 这覆盖接入 RequestBudget 的入口，不代表任意第三方采集工具都受控，也不自动启动并发采集。任务有效额度取明确授权、项目轮次和来源配额最小值；默认本地额度 3。离线场景检查及真实来源样本证据在 Codex 状态卡记录，不能代替产品最终验收。

### 就业线索与来源页（2026-10-29 增量）

- `import_signals.py --input <已有run.json> --limit 3` 默认只预览；显式 `--apply --db <目标副本>` 才导入 accepted 列表，不发请求、不搬旧 RSS。候选迁移接入备份、版本记录和批次事务，字段/证据错误回滚本批；旧 signals 不自动转为已复核内容。
- `review_signals.py --manifest <明确复核清单> --limit 3` 默认预览。每条决定须提供文档 ID、expected_sha256、目标 review_status 和 reason；清单须有 reviewer。批准时另须提供 ai_scope、claim_status、published_at、与原文匹配的 date_evidence（start/end/text）及 evidence_url。显式 `--apply` 后记录 discovery_reviews；pending/excluded 可撤销公开结果。工具校验不能代替人工阅读原始来源。
- 原文内容不变的重复导入只更新最近发现时间，保留既有复核；内容变更重新进入 pending。公开静态包不包含待审原文，本地只读 API 保留待审队列。缺表、已初始化空库和有记录分别返回存储状态。
- 第五项分来源运行状态与公司覆盖；公司按 ID 关联，零岗位公司保留。`source_registry/crawler_runs` 记录正式采集运行及结果；未写入过运行记录的来源仍显示未知，不用岗位 last_seen 倒造成功时间或成功率。预览运行不写入来源运行账本。
- 2026-10-29 已在隔离副本做限定离线检查，并观察本地 Ego 候选页；生产库未迁移或导入新舆情，真实手机与完整视觉验收、独立版本审查仍未完成。构建不等于发布。
- 发布前审查补强：批准就业线索必须有成功 Fetch 的原URL回执、Codex文档目录内可回读的响应文件及匹配哈希/正文，日期摘录必须与获批日期一致；仅搜索摘要不得批准。ATS验证页会持久停止同host，DeepSeek区分未派发阻断与已派发请求尝试，次数不代表实际费用。

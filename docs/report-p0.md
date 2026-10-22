# 日报 P0：自然日快照、统一入口与证据降级

适用范围：本机舆情面板（`sentiment.db`）的报告能力。仅复用既有 reports API/表与 Markdown 渲染器，
不重构采集、存储与聚类，不改表结构，不触发采集 / 标注 / 模型。

## 1. 统一构造器 `reporting.py`

新增 `reporting.py`（仅标准库），集中负责报告数据构造、发布闸门与 Markdown 渲染：

- **窗口**：`daily` = UTC+8 当天 00:00 至真实 now；`weekly` = 本日及之前 6 个自然日至 now；
  `monthly` = 当月 1 日至 now。全程使用带时区的 `datetime` 与 `[start, end)` 比较；
  构造窗口拒绝 naive `now`。
- **时间基准**：只接受 `time_quality='valid'` 且带时区的 `published_at`；其它任何质量取值
  （`missing` / `legacy_unverified` / `invalid` / `timezone_missing` / 未知）一律保守排除。
  历史（早于窗口）、未来（≥ 截至时间）同样不入样本，只按原因计数；绝不把 `fetched_at` 当发表时间。
- **话题范围**：只保留 `relevance_tier='conflict'` 且落入四类议题
  （彩礼/婚姻法、女权/男权、厌女/性别歧视、性别对立/打拳）。
- **议题来源**：模型值合法（复用 `topics.CATEGORY_NAMES`）且在四类内直接采用；
  合法但不在四类内直接排除，不再用规则改判；只有模型缺失或非法时才按 `topics` 规则回退，
  并记录 `label_source`（`model` / `rule`）。
- **情感来源**：优先合法五档模型值（强正面/偏正面/中性/偏负面/强负面），其次规则值，
  否则记 `未标注`；未标注或错误值**不伪装成中性**，同样记录 `label_source`。
- **可复算成员**：快照保存全部入选成员，含 ID、原始标题/URL、发表时间与质量、`tier`、
  议题/情感及来源、平台来源等审计字段。`selected_ids`、`topics` / `platforms` / `sentiments`
  与 `label_sources` 全部由成员精确复算，成员按发表时间与 ID 稳定排序。
- **发布闸门** `validate_snapshot`：校验 `kind` 与自然日窗口 / `period_key` / 标题一致；
  全部成员 aware 且落在 `[开始, 截止)`；ID 唯一非空、范围与词表合法、URL 为安全 http(s)、
  来源非空；`selected_ids` 与成员一一对应；分布与 `label_source` 逐项等于成员复算值；
  排除 ID 唯一、与入选无交集、候选总量一致；`evidence` 只是成员的受控投影（日期/来源/标题/
  URL/标签逐项一致且 ID 不重复）；内容哈希一致。任一不符抛 `ValueError`。
- **渲染** `render_markdown`：输出结构为「标题/截至时间 → 摘要 → 重点线索 → 议题样本分布 →
  数据说明」。对外**不出现** `record_id`、内容哈希、规则/模板版本、`published_at` /
  `time_quality`、`model` / `rule` 等开发或审计用词；这些只保留在 JSON 快照。
  正文保留真实截至时间、统计范围、原帖链接、来源、发表时间、简短证据边界与数据缺失提示。
  有样本时摘要简短说明覆盖范围；零样本只写「本窗口无符合范围的内容。」，不写实现承诺。
  表格单元格折叠换行、竖线转全角、Markdown 链接字符转义；链接目标对括号/竖线/方括号做
  百分号编码，标题或来源里的 `|`、换行与伪链接都改变不了表格结构。

## 2. 统一入口 `admin_features.generate_report`

`generate_report(db, kind, trace_id=None, task_id=None, db_path=None)`：

- 经 `reporting.build_report` 构造并通过闸门后，写入**不可变**的 `reports/<id>.json` 与
  `reports/<id>.md`；ID 形如 `report-<kind>-<period>-<内容哈希前 10 位>`，每次生成都是独立
  版本，旧报告文件与行一律保留、绝不覆盖（JSON 与 Markdown 同一 ID 同名成对）。
- 不可变落位使用唯一临时文件 + 原子硬链接（不支持时退化为 `O_EXCL` 独占创建），
  不再使用 exists + 固定 tmp + `os.replace` 的竞争写法；并发写入只有一个能成功。
- 先写 JSON 快照再写 Markdown，两产物落位后回读校验（快照可过闸门、Markdown 与快照重渲染
  一致），全部成功后才写 reports 成功行；任一步失败只留 `pipeline.fail`，不出现成功行。
- `load_verified_report`：读取报告 Markdown 时，若存在同名 JSON 快照则先过闸门并比对重渲染
  内容，JSON 损坏或 Markdown 被改都拒绝。按文件名结构识别新版 ID（`report-<kind>-<period>-<10 位十六进制哈希>`）：
  新版缺 sidecar、或 sidecar 存在但不是文件（如目录）一律拒绝，不再被当作旧报告放行；
  只有确认的旧版报告（文件名不是新版 ID 结构）无 sidecar 时才原样返回，保持兼容。
- reports 表 `period_start/period_end` 与快照窗口一致；`INSERT OR IGNORE`，不改表结构。
- pipeline 记账写回**同一个库**（`db_path` 或连接真实文件路径），保证隔离测试指向临时库。
- 闸门异常时 `pipeline.fail` 后原样抛出 `ValueError`，**不写成功产物、不写报告行**。

## 3. 预览与 PDF

`server.py`：

- `markdown_to_html`：安全的内联 `[文本](URL)` 链接渲染，支持 URL 中的括号；仅接受不含认证
  信息、空白与控制字符的 http(s)，`javascript:` 等协议按纯文本转义输出。反斜杠转义按字面量
  输出，因此标题或来源里注入的 Markdown 伪链接不会变成真实链接；标题、表格、列表、引用全部
  HTML 转义。
- 报告预览与 PDF 共用同一份持久化 Markdown 和同一个安全渲染器，并先经
  `admin_features.load_verified_report` 校验快照与 Markdown 对应；新版被篡改时返回明确错误，
  不把坏 JSON / 改过的 Markdown 当作成功预览或 PDF。
- 报告下载（`/api/reports/<id>/download`）走与预览 / PDF **同一验证**：先经
  `load_verified_report`，成功只返回经验证正文的 UTF-8 字节；校验失败返回 409 与同口径
  `{"error": "报告校验失败：…"}`，不再有绕过校验的直接读盘分支。
- 旧报告 Markdown 仍可预览 / 下载。

## 4. 验收安全开关

- `server.py --no-background-jobs`：明确开关，默认行为不变。启用后不启动采集 scheduler、
  关键词 crawl worker 与预警 scheduler，不改数据库 `collect_interval`，也不伪造 `get_settings`；
  HTTP 查询与用户主动报告操作仍可用，不宣称禁用所有手动 API。
- `RUNTIME` 显式记录 `no_background_jobs` / `background_jobs`，并记录 `reporting` /
  `admin_features` 模块真实加载路径与启动时哈希（`loaded_sha256`）。

## 5. 定向回放 `tools/report_p0_checks.py`

`python -B tools/report_p0_checks.py --limit N`（首次用 `--limit 3`）在临时隔离 SQLite 与报告目录中执行三组回放：

- A：时区 / 自然日边界 / 历史与未知及非法时间排除 / naive now 拒绝 / 仅 `valid` 质量；
- B：合法模型值优先与非法回退、分母与成员复算、闸门篡改拒绝（含 evidence 日期篡改、
  非 Top5 selected_id 换成不存在 ID、分布/来源篡改后重算哈希仍拒绝）；
- C：真实 `generate_report`（隔离库与报告目录）、不可变落位与 no-overwrite、pair 校验拒绝
  篡改、旧报告兼容、恶意标题/链接与表格结构安全、第二产物失败不写成功行；另含下载分支
  同口径验证（成功 200 同一验证正文、校验失败 409 而非 200）与 sidecar 规则（新版缺
  sidecar / sidecar 为目录均拒绝，旧版无 sidecar 仍可读）。

并调用既有 `selfcheck.py` 的四项通用检查（`changed_paths` / `artifacts` / `no_secrets` / `python_ast`）。
`selfcheck` 只有 docs 专属 kind，代码修改会使其 `docs_only` 为 fail；本包不采用该检查，
也不据此声明 PASS，不以 `docs_only` 掩盖代码修改。

本轮 P0 修复的本地隔离实跑证据（在 worktree 根目录执行）：

- 命令 `python -B tools/report_p0_checks.py --limit 3`，退出码 `0`；`cases_all_ok` 与
  `selfcheck_generic_ok` 均为 `true`，`verdict=pass`。
- 已修 B5（下载校验）：C3 `download_success_code=200` 且返回正文与经验证 Markdown 字节一致，
  `download_reject_code=409`（篡改 Markdown 后不再 200）。
- 已修 B6（sidecar 规则）：C3 `new_missing_sidecar_rejected=true`、`new_dir_json_rejected=true`、
  `legacy_dir_json_rejected=true`，同时 `legacy_ok=true`（旧版无 sidecar 仍可读）。
- 未执行：真实库 / 真实客户端 UI 验收、PDF 导出（需本机浏览器）。上述自我隔离检查不作为
  产品最终 PASS，仍需独立只读审查与用户真实路径证据。

## 6. 边界与未验证项

- 本包不触碰真实 `sentiment.db`，不启动服务 / 浏览器，不触发采集 / 模型，不做全量测试。
- 隔离回放与静态检查**不能替代**真实客户端验收；产品最终 PASS 仍需用户真实路径证据与独立只读审查。
- PDF 导出依赖本机 Edge/Chrome，本次为隔离检查不触发浏览器，标记未验证。

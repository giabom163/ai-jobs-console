# UI v7-A 实现说明：三业务入口、弱数据管理、隐藏技术证据与 390px 修订

任务 `gender-ui-v7-a-flash-20261016`，基线 commit `94019f5a77c90c6db6e2dc72b51e5d30ec49e86d`（工作树干净，`git status` 无待提交改动时开工）。
范围仅 v7 的 **A 呈现修订**；**未做 B/P2/C/P3**，未改服务、存储、数据库、采集、规则、配置与元数据。

状态口径：本文件只描述实现与有限检查证据，**不代表人工验收或产品最终 PASS**。真实 GUI、真实 HTTP、真实 `sentiment.db` 与 1280/390px 截图由主线程按真实路径执行与判定。
当前产品仍保留既有 390px 实机 FAIL、旧 P1-A 执行范围 FAIL、桥完整验收 BLOCKED；本包不改写这些历史结论，也不宣告解除。

## 1. 改动文件（allowed_paths 内，无越界）

| 文件 | 改动 | 结束时 sha256 |
| --- | --- | --- |
| `index.html` | 重排主导航为「总览 / 议题与表达线索 / 原帖检索 /（弱）数据管理」；旧来源与导入、原文与证据、报告模板、运行状态、P1 人工导入移入数据管理内部子视图；新增业务页容器与详情侧抽屉 | `ed487f59e1042dae5a069d6c279e8589590bd6c72c34cdb6468e4a58c8ca36da` |
| `app.js` | 业务状态/统一筛选/总览/议题/检索/抽屉渲染；移除业务页 snapshot/raw/hash/path/字段名/日志 DOM；旧管理区渲染与事件保留并兼容新导航 | `7ffd78c0ac70341561a4876cf604d763cb4b405de0f206a104b4a76bd5c062d3` |
| `style.css` | 业务页布局、双栏总览、议题左右布局、侧抽屉、深层修正 `dl.kv`/`.grid`/`.stats`/`.filters` 收缩与窄屏标签值上下排列 | `9014baca60d46305bdcb86ee2e30610ecbff71537ebbb7aafe838896feabed85` |
| `docs/UI-v7-A-implementation.md` | 本文件（新增） | — |

`git diff --stat`：`app.js | 655`、`index.html | 573`、`style.css | 182`（3 files changed）；产物落于允许路径内。
未改动：`server.py`、`storage.py`、`analysis.py`、`schemas/`、`tools/`、规则、元数据、真实 `sentiment.db`、桥。未提交、未切分支、未启动服务/浏览器、未联网、未调用模型、未读凭据。
未删除任何旧快照或旧数据结构；`/api/records`、`/api/runs`、`/api/runtime`、`/api/analysis` 等旧能力与入口均保留（改置数据管理内部）。

## 2. 复用（不重写框架、不加第三方依赖）

- 复用 `app.js` 既有安全 DOM 构建：`h()`、`appendChild()`、`clear()`、`linkNode()`、`kv()`、`fmtTime()`、`shortHash()`、`$()`/`$$()`。
- 复用既有 `api()`（区分 offline / HTTP，保留状态码与已解析 body）、`show()` 通知条、`selectView()` 视图切换、`.panel/.card/.chip/.badge/.empty/.stats` 视觉组件与 `.table-wrap` 内部横向滚动。
- 数据来源仅本地同源端点：`GET /api/data`、`GET /api/p1/overview?top_k=1..3`、`GET /api/p1/evidence/{id}`、`POST /api/p1/import`、以及数据管理区沿用的旧端点。未新增抓取器/调度/第三方库/CDN。
- 全部动态内容通过 DOM API / `textContent` 写入；**未使用 `innerHTML`/`dangerousHTML`**，`linkNode` 与原链接 `^https?://` 协议校验保持不变（新增入口一律复用 `linkNode`/`isSafeUrl`）。

## 3. 三个业务入口 + 弱数据管理入口（实现，非文档）

- 主导航：`总览`（默认 `aria-selected="true"`，其余 `hidden`）、`议题与表达线索`（标题固定）、`原帖检索`、`数据管理`（`class="tab weak"`）。
- 业务页只读：仅「重新读取本地数据」（触发同源 GET `/api/data` + `/api/p1/overview`）。**外网「刷新来源」按钮移入数据管理**，业务页不出现外网动作。
- 数据管理为弱入口，含内部子导航（`来源与导入 / 原文与证据 / P1 人工证据 / 报告模板 / 运行状态`），旧能力整块保留：来源与关键词配置、RSS JSON 导入、来源列表、原文与证据（含 `raw_snapshot`）、报告模板与分析队列、运行时、采集批次与批次详情、未接入能力清单。
- **不删旧能力**：旧 `record-detail`（含 `raw_snapshot`）、`run-detail`（含 `input_sha256`）、`runtime-body`（含路径与 sha256）仍在数据管理内，属诊断职责。
- 报告轻量入口置于议题页（`#topics-report-entry`），仅说明「当前只有未复核样本模板，不生成议题结论」并提供前往数据管理报告模板的按钮，**不冒充已实现语义报告**。

## 4. 业务页与业务详情不生成技术 DOM（非 CSS 隐藏、非折叠）

- 业务卡片 `p1BusinessCard()` 只渲染：排名/未排名标记、证据类型文案、来源名、标题链接、单点评论口径、`查看详情`/`查看原帖`。
- 业务详情侧抽屉 `renderP1Drawer()` 只渲染用户文案：来源、原标题、原地址（链接）、证据类型（仅标题／帖主正文／来源摘要）、正文或来源摘要、观测时间、发布时间（含时间质量中文）、评论数（累计口径）、评论数来源、其他计数（不计入评论数）。
- **未生成** `snapshot`/raw JSON、`snapshot_sha256`、`source_response_sha256`、`content_sha256`、`snapshot_path`、`evidence_id`、`platform_id`、`fragment_*`、`group_label` 等字段名，也无执行日志节点。
- 实现方式为**在业务渲染函数中根本不构造这些节点**（不是 `hidden`/`<details>` 折叠）。工程诊断另置数据管理 `P1 证据原始返回`（`#p1-diag-body`），按证据 ID 显式读取完整返回；导入失败的工程响应体也只写在该处。

## 5. 真实单点与累计评论口径（不虚构趋势/情感/关联）

- 概览指标以 P1 真实字段计：`采得原帖（人工候选身份）=identity_total`、`人工候选组（非确认事件）=group_count`、`最近观测=本次已载入材料中最晚 observed_at`。
- **口径澄清**：候选组用「人工候选组（非确认事件）」文案，不写「已确认事件」；不出现性别/阵营/群体比例推断。
- 评论数一律显示为**单点当前值 + 精确度**（精确/约数/未知）；未知写「未知」并说明「不计入 0，也不参与排名」，绝不当 0。
- 评论数文案统一注明「该原帖历史累计评论数，单点观测，不代表近 7 天新增或增速」；不画趋势线、饼图、情绪图、关系图，不计算增速。
- 议题概况附短空态「关联：暂无可证的议题关联……证据不足不生成关系图」。
- 空态：无 P1 材料时按真实 `empty_state` 显示「暂无 P1 人工候选材料……」，并指向数据管理的人工导入入口，不画假图。
- 后端存储实现使用 `groups[].top` / `groups[].unranked`（并非 `top_entries`/`unranked_entries`）；本实现按 `storage.p1_overview` 实际字段读取，`top` 带 `rank`、`unranked` 带 `rank_reason`。身份去重沿用后端 latest-per-identity 结果，前端不重复计数、不跨组重复。

## 6. 原帖检索的范围声明

- 检索仅对**本次已载入的 P1 材料**（overview 的 `top` + `unranked`，全库候选身份 ≤3）做本地过滤，页面明确写明「不是全库检索，也不检索旧 RSS 原文」。
- 统一筛选含：来源、证据类型、关键词（匹配标题 / 来源 / 已载入正文或来源摘要）。overview 返回体不含 `content`，因此前端对已载入材料逐条真实 `GET /api/p1/evidence/{id}` 补齐正文用于关键词匹配（数量受 `candidate_limit=3` 上界约束）；未载入成功时筛选口径提示「关键词只匹配标题与来源」。
- 旧 RSS 材料仍只在数据管理「原文与证据」查看；两处文案均标注**不同分母，不混为一组**。
- 不把旧 RSS 检索伪装为性别议题原帖检索。

## 7. 统一筛选上下文与日期控件

- 总览、议题、原帖检索共用同一 `state.filters`（来源/证据类型/关键词），三处各渲染同一套控件（class 化，避免重复 id），切换 Tab 时 `syncFilterBars()` 同步取值，视图内改筛选即时重渲染三处结果。
- **日期范围筛选后端未支持**（overview 仅支持 `top_k`），故不提供日期控件，并在筛选说明中写明「日期范围筛选后端未支持，未提供该控件」，不伪装可用。

## 8. 详情侧抽屉（GET / 390px / 关闭 / ESC / 焦点返回）

- 每张业务卡片的「查看详情」都真实 `GET /api/p1/evidence/{id}`，成功后渲染到右侧抽屉 `#drawer`（`role="dialog" aria-modal="true"`）。
- 打开时记录触发按钮，焦点移至「关闭」；点击关闭按钮 / 点击背景 / 按 `Escape` 关闭后，焦点返回原触发按钮（`document.contains` 校验）。
- 视觉：桌面右侧面板（宽 `min(480px, 100%)`）；`@media (max-width:520px)` 下抽屉 `width:100%`、去左边框、内距 16px，即 390px 全屏。
- 失败态：读取失败先清空抽屉旧内容，显示真实 `HTTP 状态 + 错误信息`，并提示「旧详情已清空，未把旧数据当作成功结果；完整错误响应体请在数据管理 → P1 人工证据读取」。POST 422 同理由 `submitP1Import()` 走 `p1Reset()` 清空旧材料与旧详情。

## 9. 390px 网格溢出与数字竖排修订（待主线程实机核验）

- 根因候选（**未在本包实机证实**）：旧 `style.css` 的 `dl.kv { grid-template-columns: max-content 1fr }` 与 Grid 子项缺 `min-width:0`。
- 本包改动：
  - `dl.kv` → `minmax(0, max-content) minmax(0, 1fr)`，`dt/dd` 加 `min-width:0`；`dl.kv.wide` 第二列改 `minmax(0,1fr)`。
  - `.grid` → `340px minmax(0,1fr)` 且 `.grid > * { min-width:0 }`。
  - `.stats` → `repeat(3, minmax(0,1fr))`（≤900px 两列，≤520px 单列），统计值 `overflow-wrap:anywhere`。
  - `.filters`/`.biz-filters` → `repeat(auto-fit, minmax(160px, 1fr))`；议题/总览双栏在 ≤900px 收为单列。
  - `@media (max-width:520px)`：`dl.kv`/`dl.kv.wide` 改**单列**，标签在上、值在下，长值换行（明确满足「标签值上下排列」）。
  - 未使用 `overflow:hidden` 掩盖根页面溢出；表格沿用既有 `.table-wrap { overflow-x:auto }` 内部滚动。
- 备选根因（390px 文档宽 405px）与以上改动是否已完全消除，**须由主线程在 1280px 与 390px 实测确认**；本包不声称已修复为 PASS。

## 10. 事件绑定与新导航/旧管理结构的兼容

- 主 Tab 循环绑定（`.tab`，含方向键）继续生效；新增数据管理子 Tab 循环（`.admin-tab`，独立 class，不干扰主 Tab 逻辑）。
- `openRunById()` 与原文详情的批次链接改为「切到数据管理 → 运行状态」；`selectView("runs")` 等旧调用已修正，避免指向不存在的视图。
- 旧管理区所有 id 保留，`setWriteAvailability()`、`renderStats()`、`renderFeeds()`、`renderRecords()`、`renderRuns()`、`renderRuntime()`、`loadAnalysis()` 等原逻辑未改。
- 已逐一核对：`app.js` 中 `$("#…")` 引用的元素 id 均在 `index.html` 中存在（无空引用导致初始化崩溃）。
- 真实读取状态由 `setBizStatus()` 写到 `overview-status/topics-status/search-status` 三处并保持一致，**导航切换不会掩盖读取失败状态**。

## 11. 有限语法检查记录（仅一次，未重跑）

```powershell
& "C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe" --check app.js; Write-Output "node_exit=$LASTEXITCODE"
```

- exit code：`0`
- stdout：`node_exit=0`
- stderr：空

结论：**仅一次语法检查通过**。未运行任何测试脚本（未跑 `tools/manual_*_check.py`）、未做 Python compile、未启动服务、未发 HTTP、未做 GUI/DB 检查。检查未失败，故无「保留失败命令」情形。

## 12. 未执行验证 / 已知限制（如实列出）

- **未启动真实服务、未做 GUI/HTTP/真实 DB 验收**：三业务入口与数据管理入口的真实渲染、抽屉打开/关闭、422 清旧详情、390/1280px 溢出与竖排，全部**未在本包验证**。
- 不同步验证项：`enrichP1Contents()` 会在概览加载后对已载入材料（≤3 条）逐条 `GET /api/p1/evidence/{id}` 以支持关键词匹配正文——新增了「打开页面时的本地详情预读」行为，需主线程确认其请求次数与失败降级可接受。
- 已知限制：搜索范围受 `candidate_limit=3` 与 `top_k` 限制，不是全库检索；日期筛选未提供；无增速/曲线/观察清单；无事件关联图；旧 RSS 与 P1 分母分开展示。
- 未新增 P2 观察清单/热度历史接口，未新增 P3 正文逐条证据绑定与关系查询；未评估或安装 ECharts/Cytoscape。
- P1 真实库当前内容（本节不写入任何硬编码样本或数值）：由运行环境真实返回决定，本包未读取真实 DB、未填入任何数字或样本。

## 13. 主线程实机验收建议清单（本包未执行）

1. 启动本机回环服务（既有授权），打开真实入口；确认默认落「总览」，主导航四项，数据管理为弱入口且可达。
2. 确认「刷新来源（外网）」仅在数据管理内；业务页无外网按钮，业务页按钮只触发本地 GET。
3. 逐页核对业务页与抽屉 DOM：**不存在** snapshot/raw JSON/长 hash/本地路径/字段名/日志节点（需查 DOM 而非仅看可见区域）。
4. 点「查看详情」核对真实 GET、侧抽屉、`Escape`/关闭/背景点击与焦点返回；在 390px 确认抽屉全屏。
5. 制造真实失败（如 `GET` 一个不存在的证据 id、或 `POST` 一条违反 schema 的记录得到 422）确认**旧详情被清空**、真实 HTTP 与错误信息如实显示，工程响应体只在数据管理可读。
6. 1280px 与 390px 双视口核对无根页面横向溢出、窄屏标签值上下排列不竖排挤压；保留截图与 DOM 尺寸证据。
7. 以上为真实路径人工验收，由主线程执行与判定；本包不代替，也不宣称产品 PASS。

## 14. 边界声明

- 本文件的「exit 0」仅指第 11 节一次 `node --check app.js`；产品行为、HTTP、GUI 与真实 DB 验收状态为 **未执行（BLOCKED 于人工实机）**。
- 历史结论保留：390px 实机 FAIL、旧 P1-A 执行范围 FAIL、桥完整验收 BLOCKED 均未被本包覆盖或解除；P2/P3 为 NOT_STARTED。
- 后台快照、哈希校验、旧数据留存不被本包破坏；业务页隐藏的是展示，不是删除数据。

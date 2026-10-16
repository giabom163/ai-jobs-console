# UI v7-B 失败候选只读审查

任务：`gender-ui-v7-b-luna-review-20261016`  
候选基线：`bdeebb56ecd6fd3427ba4af9d4b40108f6e4d7fd`（当前 HEAD 与该基线相同）  
对照：`docs/UI-v7-B-plan.md`、候选文件上下文及 `aeac536..HEAD`。本文件是静态审查意见，不是测试、GUI 或人工验收记录。

## 结论

- **A：有限 PASS（静态范围）**——本次所审 B 差异未发现足以推翻既有 A 有限结论的静态证据；不代表 A 全量或真实路径验收。
- **B：FAIL**——至少有窗口分页候选错误、图表异常时丢失可读历史、写失败/清单读取失败残留旧数据、存储 cap 非跨连接原子，以及定向脚本违反合成身份约束等问题。**GUI 未开始**。
- **桥完整验收：BLOCKED，未人工接收**。本任务未读写桥状态；不以静态审查代替人工 accept。
- 进度：审查文档已完成；验收：**BLOCKED**。没有执行任何测试、语法检查、服务、HTTP、网络、GUI、真实库读取或模型调用。

## 静态逐项审查及必修项

### 1. 历史比较、分页与日期共享分母

- `storage.py:668-751`：按归一后的观测时刻排序；先基于完整序列判比较，再套 `from/to` 与 offset/limit。窗口外真实前点可供窗口内首点比较，重复桶/同刻冲突/unknown/approximate/口径变化的处理与方案基本一致。稳定顺序也包含 `observed_at, imported_at, id`（`storage.py:689-702`）。**静态 PASS（该段语义）**。
- `storage.py:689-739` 每次分页都把该身份全历史读入列表并重算；响应有 500 点上限，但读取与计算没有历史长度上限。长历史下分页会重复扫描全量记录，不能视作有界历史工作量。最小修复：迭代有序游标、仅保留比较所需前值及当前页点，避免每页物化全序列；仍需保持完整历史的比较语义。
- **必修**：`app.js:1212-1230, 1236-1242` 每身份最多取 20 页（10,000 点），却在 `has_more` 仍为真时将部分结果标 `truncated` 后仍取 `points[-1]` 作为窗口最新候选。接口顺序为升序，所以长窗口会把最早 10,000 点中的末点错作“窗口内最新”，导致总览/议题/检索列表、分母、精确排序与 TopK 错误。最小修复：候选路径必须取得窗口末页/真实最新点（可增加受控的 latest-in-window 读取方式），或在仍截断时拒绝将该身份计入已完成候选；不得把截断页末点标成最新。图表可继续显式展示截断态。
- `app.js:1233-1244` 的候选集合由 `P1_LOAD_TOP_K=3` 全部候选逐身份生成，随后三视图共享该集合（`app.js:54-55, 800-819, 1174-1189`）；无窗口点会排除、窗口点取最新值。该设计静态成立，但受上项截断错误影响。
- **必修**：窗口请求 pending 时 `renderWindowPending()` 清空了列表/热度，却没有清 `#overview-shortcuts`（`app.js:1267-1282`）。窗口切换期间仍可能显示上一窗口原帖卡片。最小修复：pending 与失败路径同步清空旧捷径，并显示明确加载/失败占位；保留当前过滤窗口说明。
- `fetchHistoryPoints()` 收到新窗口/新选择后不会在分页循环内检查令牌（`app.js:1213-1230`）；调用方只在所有页结束后检查（`app.js:1236-1238, 1571-1573`）。旧响应不能覆盖新状态，但旧请求仍会继续发完最多 20 页。最小修复：每页前后核对对应 overview/history token，过期即停止；不要让旧窗口分页继续消耗请求。

### 2. 观察生命周期与 cap 原子性

- `storage.py:577-622, 625-665`：到期由当前 UTC 推导，退出/到期保留周期；显式重加可创建新周期；active 返回最多 3；API 写入口使用 `server.py:285-286` 的进程内 `WRITE_LOCK`。这些生命周期语义静态基本符合方案。
- **必修**：`storage.py:634-646` 的重复/活跃数读取与插入处于普通 `with db` 中，没有在检查前取得 SQLite 写锁。`storage.connect()` 使用默认连接模式（`storage.py:100-104`），该上下文不保证在前置 SELECT 时已开始写事务；不同进程/连接可同时读到尚有余量并分别插入。服务内 `WRITE_LOCK` 只能串行化单进程 Handler，不能替代存储层原子门。最小修复：检查开始前显式取得 `BEGIN IMMEDIATE`（或等价原子写锁/约束），将重复检查、活跃计数和插入放在同一事务，异常回滚；保留 Handler 锁。
- `storage.py:591-609` 的周期项按 identity 返回该身份最新 evidence；因此 evidence_id 会随新 observation 更新，而身份由 identity_key 稳定。测试和 UI 比较应按身份/当前 evidence 对齐，不能长期拿旧 evidence_id 当身份标识。

### 3. 定向检查脚本：失败保留、身份约束与最小修复

- **已报告失败不得清除**：任务输入记录 Flash 首次 900 秒 timeout 已停稳，`provider.transport` 报 HTTP 200 后 Decode error，且缺少 `docs/UI-v7-B-implementation.md`；定向脚本首次 watch 组异常，后续一轮仍为 FAIL。该历史来自派单信息；本轮未重跑，工作区未提供对应原始 stdout/stderr/result 产物，故不补造原因或退出码，也不把源码推测写成实测结论。
- **必修**：`tools/manual_p1_b_check.py:304-310` 直接插入 `p1w-ghost-cap`，使用虚构 identity_key / evidence_id，且没有对应 `p1_evidence`。这违反“只用三身份、不得造第四身份”约束，`watch_item()` 会为其退化为无 evidence 的观察项。删除该 ghost 注入；不能仅移除 cap 断言来刷绿。
- 生命周期目前只验证 B 显式退出后重加（`tools/manual_p1_b_check.py:273-290`）；C 被设为过期后只验证状态/不活跃（`293-303`），并没有验证**过期身份显式重加生成新周期且保留旧过期周期**。最小修复：C 到期后先断言历史旧周期保留；确保有空余 active 槽位，再经真实 `POST /api/p1/watchlist` 重加 C，断言 201、新 watch_id、旧周期仍 expired、active≤3。
- cap 验证方案：始终只建立 A/B/C 三条 evidence identity；使用这三者的真实 identity_key/evidence_id 构造隔离 cap 前置状态，不伪造第四身份；选择一个没有活跃周期的既有身份调用真实 Handler，断言 409 且周期表/返回 active 数仍≤3。注意在合法状态下，身份总数≤3 且同身份重复优先冲突时，独立的“第四个不同身份观察” cap 分支难以由纯 API 状态自然触达；若需直接准备隔离周期行，必须绑定已有 evidence，并明确标注是 cap 分支夹具状态，不冒充正常生命周期。再恢复到只含有效 A/B/C 周期的状态，验证过期 C 有空位时可显式重加。
- 全库第四身份拒绝的既有设计可以保留：通过真实 `/api/p1/import` 提交 `fixture-D` 并断言 409、`count(DISTINCT identity_key)==3`（`tools/manual_p1_b_check.py:337-341`）。它是被拒绝的请求，不得先写入第 4 identity。
- **`A_off_top2_but_active` 不能因失败而删除**（`tools/manual_p1_b_check.py:320-327`）。按现有夹具顺序，A 的最新 exact 为 170，B 为 240，C 为 320，三者同组；`storage.p1_overview()` 取每身份最新 observation 并组内按 exact 值降序（`storage.py:479-495`），故 Top2 应为 C/B；检查发生在 A 的 08:00 observation 追加之前（追加在脚本 329 行），当时 `ids[a]` 与 watchlist 返回的 A 最新 evidence id 应相同，A 应仍 active。就当前静态代码而言，断言逻辑成立，未发现 `p1_overview` 的旧值口径或分组排序必然导致该失败。保留 FAIL 并取得原失败 detail（top2、active IDs、A/B/C 最新值和 watch status）再归因；若将断言移到 08:00 追加之后，须重新读取 A 的最新 evidence_id 或按 identity 对照，避免把合法 evidence_id 更新误判为观察身份消失。不得只删断言。
- `--limit` 静态判断：manual 检查按 `groups` 前 N 组执行，第一组会创建 A/B/C，第二组只在第一组完成后运行（`tools/manual_p1_b_check.py:97-107, 203-229, 494-505`）；`--limit 1/2` 不会访问未创建样本，但它不是样本数量参数。GUI 助手则始终遍历三身份/多时点（`tools/serve_b_fixture.py:174-184`），却把 `args.limit` 传给每次 `p1_import()`，并将其写为 `server.LIMIT`（`166, 182-185`）；limit=1/2 会在种完三身份前因候选上限拒绝而中断，而非“访问未建样本”。该助手契约命令固定 `--limit 3`（计划 `docs/UI-v7-B-plan.md:159-161`）。最小修复：GUI helper 在创建 evidence-dir 前仅接受 `--limit 3`，或将样本上限固定为 3 并一致记录，不接受会在半初始化后失败的 1/2。

### 4. 安全状态码、本地 ECharts 与隔离 GUI 助手

- **静态 PASS（边界，不代表 HTTP 实测）**：`server.py:236-265, 288-290, 352-381` 有 watch/history 路由、精确 body 字段、同源 Origin/Host 检查和 400/403/404/409/422/500 映射；vendor 以精确路径白名单服务（`server.py:28-34, 266-271`），没有把用户路径拼接成任意静态文件读取。有限检查脚本相应覆盖了状态码和路径拒绝（`tools/manual_p1_b_check.py:239-251, 346-383`），但本次没有运行。
- **静态 PASS（本地加载声明）**：`index.html:492` 使用本地 `/vendor/echarts/echarts.min.js`；`server.py:28-34` 固定白名单；`vendor/echarts/provenance.json:1-35` 声明 ECharts 5.6.0、来源、Apache-2.0/附带 d3 BSD-3-Clause 与文件 SHA-256，相关 LICENSE/NOTICE 路径存在。未联网、未执行浏览器请求，也未在本任务独立重算这些文件哈希；这里只确认声明与代码接线，不宣称哈希/发行包实测复核。
- **必修**：`app.js:1484-1535` 仅处理图表库缺失，没有捕获 `echarts.init()`、`setOption()` 或 `resize()` 抛错；`renderWatchHistory()` 先画图后渲染表（`1537-1555`）。图表异常会进入 `loadWatchHistory()` 的读取失败 catch，并调用 `renderWatchPoints([], false)`（`1575-1582`），把已经成功取得的真实点伪装成空窗口，违反“图表失败仍有可读数据表”。最小修复：先渲染真实历史表；对每张图单独捕获初始化/绘制异常并清空该图、显示短错误占位，保留可读表与另一张可用图，不走“历史 API 读取失败”分支。
- **必修**：观察清单 GET 失败时，`loadWatchlist()` 清了活跃项、周期表与图表，却没有清 `state.history`、`#watch-history-points` 或 detail 状态（`app.js:1593-1601`）；旧历史点仍留在页面。最小修复：令所选周期历史 token 失效、清 state/history 表和详情状态/图，并显示统一失败占位。
- **必修**：加入/退出 POST 失败只显示错误，没有失效/重读 `state.watch`（`app.js:1315-1318, 1333-1336`）。例如 409/422 后页面仍可能把已过期或已改变的周期展示成活跃。最小修复：失败后清旧观察业务缓存并重读 GET；若 GET 也失败则保持清空，不复用旧值。
- GUI helper 静态边界：实际继承 `server.Handler`，仅转发观察 add/exit，其余 POST 在父 Handler 前回 405（`tools/serve_b_fixture.py:85-102`）；监听限定 127.0.0.1 白名单端口并设置最长 180 秒 shutdown（`33-34, 105-123, 226-242`）；样本字典恰有 A/B/C 三 identity（`174-179`）。因此 `--limit 3` 下的预设边界静态成立，但不等于已检查运行 PID/端口或实机结果。
- **必修（证据完整性）**：helper 声称写请求账本保留原始响应，但只存 `body[:4000]`（`tools/serve_b_fixture.py:66-82, 94-100`），没有记录该字段被截断；大响应无法从 ledger 恢复。最小修复：完整保存响应体，或写入独立 body artifact 并以 ledger 引用；若保留长度限制，必须显式标记截断，且不能称作原始完整 body。

### 5. 390px / modal 与 GUI 验收门

- 静态结构：抽屉具有 `role="dialog" aria-modal="true"`（`index.html:481-486`）；背景 inert、Tab 圈定、ESC/关闭后回触发按钮在 `app.js:1606-1634, 2076-2106`；390px 下抽屉全屏、图表在 900px 以下单列、表格有滚动容器（`style.css:490-520, 545-565`）。未发现本次新增结构上的明确缺失，**仅静态 PASS**。
- `1280/390` 无页面横溢、modal 真正可用、图表加载/失败态和键盘交互都没有 GUI 证据。真实 GUI 未开始，不能据 CSS 代替验收；需修复上述必修后，再由主线程在专用隔离标签页按方案检查 1280/390、图表错误及 422/409/500 清旧。

## 失败记录与最小下一步

1. 保留 Flash timeout / Decode error、首次 watch 组异常、后续仍 FAIL、实现说明缺失等派单已知失败；不得用静态审查或后续 PASS 覆盖。原始执行日志未随本工作区提供，本审查不补写不存在的输出/退出码。
2. 下一轮仅修上述明确缺陷，重点先改 `storage.py` cap 事务、`app.js` 截断候选和图表/旧缓存错误路径、`tools/manual_p1_b_check.py` ghost 与过期重加/cap fixture、`tools/serve_b_fixture.py` limit 预检和完整账本；补齐正确路径 `docs/UI-v7-B-implementation.md`，保留既有 `docs/P1-B-implementation.md`（它不是本任务要求的文件）。
3. 修复后按批准范围由实现方执行一次有限定向检查并保留全部失败原文；再由 Luna 复审。修复/检查 PASS 不等于 GUI PASS；主线程真实 GUI 与真实路径验收仍未开始，本报告不作人工接收。

## 本次执行范围

只新增本文件。未改源码、测试脚本、vendor、配置、规则、真实 DB 或桥；未执行测试/语法检查、联网、服务启动、HTTP、GUI、模型调用、提交、分支操作或清理。

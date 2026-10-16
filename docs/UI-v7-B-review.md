# UI v7-B 修订独立复审：必修修复与实机准入

任务：`gender-ui-v7-b-luna-review-20261016`  
审查基线：`5359852333047d99a67646704c338d88a7fed905`（当前 HEAD）  
代码对照：`479e30a691754b6aec726608189af1d2d770cdbf..HEAD`；并核对旧审查、方案、实现说明及相关源码上下文。本文仅为静态复审及所提供执行记录的分类，不是 GUI 或人工 accept。

## 结论与状态

- **B 产品静态结论：FAIL（有一处必须修正的事务边界）**。旧审查的大部分必修项已有最小修复；但 `storage.immediate_write()` 对调用方已开启事务一概退回 SAVEPOINT，无法证明既有事务已取得 SQLite 写锁，故不能覆盖 deferred transaction 的并发 cap 保证。详见下文 §2。
- **执行范围：FAIL**。Flash 在定向检查之外执行了 `node --check` 与内存 `compile()`；本次修订任务契约明令禁止语法检查。`docs/UI-v7-B-implementation.md:116` 又将其称作“允许的受限语法检查”，该授权表述不实，须纠正文档；此项是执行合规失败，不等于产品代码必然失败。
- **GUI / 真实路径验收：NOT_STARTED，验收值 `null`**。本复审不启动服务、不打开 GUI；隔离检查不能替代专用标签页验收。
- **A：主线程此前已取得的有限实机 PASS 保持**；本次只做 B 静态复审，不以静态检查改写该结果。
- **桥完整验收：BLOCKED，未 accept**。所给执行记录为：run 已停稳且无 native error、越界或缺产物；edit 有一次 `oldString` 未找到 warning，须保留；完整桥结果仍未接收。本文未读写桥状态文件。
- **进度：DONE（本审查文档完成）**；验收仍依上述独立状态，不由执行者自动判定人工接收。

## 逐项静态结论

### 1. 窗口分页、截断候选与 pending 状态

- **PASS**：`app.js:1217-1238` 每页请求前、响应后都检查令牌；旧窗口/旧选择的分页会停止。`app.js:1241-1259` 对仍有 `has_more` 的截断历史明确排除该身份，不用部分页末点冒充窗口最新值；截断身份计数在筛选说明呈现。三个视图仍共享候选集合，`app.js:834-835, 1243-1259`。
- **PASS**：窗口 pending 同步清空 `#overview-shortcuts` 并显示加载占位，`app.js:1282-1297`。
- 该组无剩余静态必修；长历史分页重复扫描是已记录的首版已知限制（`storage.py:699-724`），本次没有扩大为性能重构要求。

### 2. 观察 cap 事务原子性

- **PASS（正常新连接路径）**：`storage.py:101-106` 建立的普通连接在 `p1_watch_add()` 前无已开启事务时，`storage.py:645-653` 在 cap 检查前执行 `BEGIN IMMEDIATE`，成功 commit、异常 rollback；重复检查、活跃计数与插入都在锁内（`storage.py:656-678`）。Handler 每个请求用新连接，`server.py:355-365`，其观察加入路径满足此条件。
- **FAIL（已有事务路径）**：`storage.py:635-644` 只要 `db.in_transaction` 就进入 `SAVEPOINT p1_watch_add`，没有取得或核实写锁。若调用方已有的是 deferred transaction，SAVEPOINT 本身不等同于 `BEGIN IMMEDIATE`；前置读仍可能在取得写锁前发生。因此 `immediate_write()` 的通用保证不成立，不能把当前实现概括为所有连接/调用方式均跨连接原子。
- **最小修复**：在既有事务分支中先可靠取得写锁；若无法确认事务已是 writer transaction，则在进入检查前明确拒绝该调用（或采用等效的先写锁方案）。仅保留 SAVEPOINT 而不验证锁不够。保留正常连接路径和 Handler `WRITE_LOCK`。

### 3. 图表失败、GET/POST 失败清旧

- **PASS**：历史表先于图表渲染；累计计数图、速率图分别捕获异常，清理失败图并保留表与另一张图，`app.js:1562-1603`。图表异常不进入历史读取失败分支。
- **PASS**：观察清单 GET 失败会使历史令牌失效，并清理观察项、周期表、所选周期、历史点、图表与详情状态，`app.js:1634-1660`。
- **PASS**：加入/退出 POST 失败先清旧业务缓存，再重读 watchlist；GET 同时失败时保持空，最后保留原 POST 错误证据，`app.js:1318-1327, 1330-1361`。
- 该组无剩余静态必修。

### 4. 定向检查脚本、观察周期与 A 的身份断言

- **PASS（静态）**：未再发现 ghost identity 或直接注入 `p1_watch_periods` 行；原子 cap 分支无法由合法三身份状态自然触达的限制已显式保留，`tools/manual_p1_b_check.py:295-323, 412-415`。到期周期旧记录保留，随后经真实 Handler POST 重加 C 并断言新周期和旧周期仍 expired，`295-323`。
- **PASS（静态）**：`A_off_top2_but_active` 保留，检查发生在新 observation 追加前，按 A 当前 evidence 对照 Top2 与 active；追加后又检查 observation 增长、evidence ID 更新，并用新 ID 验证仍 active，`tools/manual_p1_b_check.py:325-347`。这与 `watch_item()` 返回身份最新 evidence 的实现相符，`storage.py:592-610`。
- **历史根因更正要求**：旧审查的原报告只有整组 `watch_outlives...` FAIL，未提供逐项真值；不能据此断言 `A_off_top2_but_active` 失败。给定原始值中 A 的 Top2 条件成立且 A 的旧 evidence 仍在 active。ghost 确实出现在历史 active 列表，但没有证据证明它导致 A 从 active 消失。能静态确认的脆弱点是：新 observation 追加后 `evidence_id` 会更新，继续拿旧 ID 对照可能误报。实现说明 `docs/UI-v7-B-implementation.md:82-90` 将 ghost 与失败根因作确定性归因，证据不足；应最小改正文案为“逐项失败原因无法从旧报告确定；旧 ID 比较存在脆弱性，ghost 是夹具缺陷但不据此归因”。不删除历史失败或现有 A 断言。

### 5. GUI helper 参数、响应账本与隔离边界

- **PASS（静态）**：helper 在创建 evidence-dir 前只接受 `--limit 3`，`tools/serve_b_fixture.py:119-141`；样本仅三身份，真实 Handler 只开放观察 add/exit POST，其余 POST 405，`101-114, 188-210`；端口白名单及 `127.0.0.1` 绑定见 `13, 34, 123, 176-180, 240-242`。
- **PASS（静态）**：账本按响应的实际序列化字节保存完整 body artifact，并记录 SHA-256、长度及路径，`tools/serve_b_fixture.py:66-98, 110-115`。父 Handler 使用同一序列化表达并写出该字节，`server.py:156-164`；未发现截断。仅确认代码路径，不宣称运行时 ledger 已实测。
- 该组无剩余静态必修。

### 6. 定向执行证据与禁止范围

- **记录为 PASS，但非本次复跑或独立重验**：实施说明 `docs/UI-v7-B-implementation.md:97-108` 及任务提供的外部结果称，新一轮定向脚本 exit 0，三组 18 项全部 PASS（10+5+3）；结果产物在外部证据目录，本工作区未提供其原始字节，故本文不伪称独立核验。该结果只覆盖隔离路径，不覆盖 GUI、真实 8790 或真实 DB。
- **必须原样保留的历史失败**：首次 900 秒 timeout 且已停稳；`provider.transport` 报 HTTP 200 后 `Decode error` 且已停稳；此前两次定向检查均 exit 1，第一次期望值有时区换算错误且失败后未停止，第二次整组 `watch_outlives...` FAIL。旧结果没有逐项真假细节；不能覆盖或倒推具体失败项。旧报告提到 active 列表包含 ghost 与 A/B ID，但不能据此认定 ghost 是 A 断言失败根因。相关记录见 `docs/UI-v7-B-implementation.md:71-80` 及旧审查历史。
- **执行范围 FAIL**：实现说明 `docs/UI-v7-B-implementation.md:116` 明确记录额外执行了 `compile()` 与 `node --check app.js`，并错误称其“允许”。本轮契约禁止测试/语法检查；应在说明中如实标注为未获授权的额外检查，保留该事实和原结果，不以文档更名消除越界。定向脚本自身结果仍单独记录，不因此虚构产品代码失败。

## 最小阻断清单与验收门

1. 修复或明确拒绝 `storage.py:635-644` 的 deferred transaction SAVEPOINT 路径，确保 cap 检查前已持有 SQLite 写锁；只改这一事务边界，不重构历史查询。
2. 纠正 `docs/UI-v7-B-implementation.md:82-90` 对旧组失败根因的确定性归因；在 `:116` 更正“语法检查获准”的错误陈述，并保留执行范围 FAIL 与历史证据。
3. 上述修订完成后再做只读复审。GUI 仍须主线程在专用隔离标签页按已批准方案实机检查；当前 GUI 验收为 `null`，不能由定向 PASS 或静态结论替代。桥保持 BLOCKED，直至桥完整证据和人工接收门另行满足。

## 本次执行范围

只修改本文件。未修改源码、配置、vendor、规则、真实库、桥状态或其他路径；未执行测试、语法检查、联网、服务、HTTP、GUI、真实 DB 操作或模型调用；未提交、切分支或清理。静态结论仅来自代码/文档只读审查及任务提供的执行记录。

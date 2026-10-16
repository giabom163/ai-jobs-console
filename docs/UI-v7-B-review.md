# UI v7-B 窄修只读复审与实机准入

任务：`gender-ui-v7-b-luna-review-20261016`  
审查基线：`389b379c49de058df8bdef37b47fddafabbcc97a`（当前 HEAD）  
代码差异：`c53eef629bd02b0febbf88c18d51195e6c4bba0b..HEAD`。本审查对照上轮审查、方案、实现说明及相关源码；不改源码。

## 结论与状态

- **B 产品静态结论：PASS**。本轮指定的事务边界、图表生命周期、历史区间/窗口说明及单速率区间文案缺陷均已修正，未见实质静态阻断。
- **GUI 准入：可进入主线程获准的有限真实客户端验收**。该结论仅是准入，不是 GUI 或产品最终 PASS；GUI 验收仍为 **NOT_STARTED / `null`**，须由主线程留真实路径证据并决定 accept。
- 实现说明有一处非阻断的精度措辞不一致，见 §3；它不改变产品静态 PASS，但应在后续允许修改说明文档时作最小文字更正。
- **历史隔离检查：保留上轮三组 18 项 PASS（10+5+3）**，本轮未重跑、未独立重验；不能替代 GUI/真实路径验收。
- **历史执行范围 FAIL 保持**：上一轮额外运行内存 `compile()` 和 `node --check app.js`，当时契约未授权；实现说明现已如实标为越界，文档更正不洗去该 FAIL。本轮无任何测试或语法检查。
- **历史其他失败全部保留**：首次 900 秒 timeout、`provider.transport` HTTP 200 后 `Decode error`、两次定向脚本 exit 1（含首次时区期望错误及失败后未停止）、第二次整组 `watch_outlives...` FAIL、编辑工具 warning。旧组报告没有逐项真假细节，不推断 ghost 导致 A 消失。
- **A 有限实机 PASS 保持**；本次静态审查不改判。**桥完整验收仍 BLOCKED、未人工 accept**。进度为 **DONE（审查文档完成）**，验收状态与进度分开。

## 逐项静态结论

### 1. 观察 cap 事务原子性：PASS

- `storage.py:626-646` 定义并抛出 `P1TransactionRequired`；`db.in_transaction` 时在 `BEGIN IMMEDIATE` 与 cap 检查前明确拒绝，不建立 SAVEPOINT，也不提交或回滚调用方事务。此前的 evidence 查询（`storage.py:663-665`）是只读查询。
- 无事务连接在 `storage.py:647-654` 先执行 `BEGIN IMMEDIATE`，再进入重复检查、活跃计数和插入（`storage.py:666-679`）；正常异常路径回滚、成功提交。
- 正常 Handler 加入路径使用新连接（`server.py:352-366`，尤其 `:355-357`），与该前提吻合。没有发现需要再修的事务边界。

### 2. 图表 key、清理、渲染、失败隔离及 resize：PASS

- `app.js:52` 的 `state.charts`、`renderChart` 写入 `:1548-1549`、`clearWatchCharts` 遍历及 dispose `:1464-1474`、历史渲染异常清理 `:1591-1617`、resize 遍历 `:2134-2155` 统一使用 `chart-count` / `chart-rate`。
- 历史切换/重读在 `app.js:1587-1589, 1629-1636` 清理旧实例再重绘；两张图各自 try/catch，单图失败只 dispose/清空该实例并留提示，不吞掉可读历史表或另一张图。窄屏 resize 对两图分别捕获异常，清理失败图且继续处理另一图（`:2134-2155`）。未见遗漏旧 key 或共同失败分支。
- 无需代码修复建议。

### 3. 历史区间、窗口外提示及单个有效速率区间：产品行为 PASS；说明文档有一处非阻断措辞偏差

- `storage.py:740-769` 返回真实 `previous_observed_at`、`interval_seconds`、比较状态及计算出的速率；`app.js:1488-1506` 在确有前值时显示比较起点、终点与格式化时长，无前值显示 `—`。
- “比较起点在观测窗口之外”只在存在前值且它早于 `state.windowFrom` 时出现（`app.js:1493-1497`）；窗口内起点不误标。表格保留真实观测终点时间（`:1498-1506`）。
- 速率图按有效 `rate_per_hour` 数量分别文案处理：一个有效区间明确称“仅 1 个已计算速率区间”，不再说等待或否认已有速率；零个与多个区间另有文案（`app.js:1514-1536`）。
- **最小文档更正建议（非产品阻断）**：`docs/UI-v7-B-implementation.md:84` 称表格显示“间隔秒数”，但 UI 实际调用 `formatInterval()`（`app.js:184-192, 1490-1492`），以分钟/小时格式化且有舍入。后续若允许修改该说明，最小更正为将“间隔秒数”改成“格式化时长”；若契约要求 UI 精确呈现秒数，则须另行明确授权并改 UI，不能把当前舍入显示描述成秒数。

### 4. 历史 `watch_outlives...` FAIL 与 ghost 归因：表述已更正

- 实现说明 `docs/UI-v7-B-implementation.md:94-100` 保留整组 FAIL、原始 Top2/active 值与缺少原始逐项真假明细的事实；`:109-112` 明确不能确定性归因 A 从 active 消失于 ghost。ghost 被单独记为夹具缺陷，不与 A 的断言失败绑定成已证实因果。
- 因此旧失败既未删除也未被新结果覆盖；没有进一步修复建议。

### 5. 实现说明中的检查授权与结果记录：已纠正，历史 FAIL 仍在

- `docs/UI-v7-B-implementation.md:8, 130, 138-139` 说明本窄修未执行检查，并将上一轮 `compile()` / `node --check` 明确记为未获授权的执行范围 FAIL，不再称为“允许的受限语法检查”。
- 上轮隔离检查 exit 0、三组 18 项 PASS 及其范围限制仍记于 `docs/UI-v7-B-implementation.md:119-130`；本次不复跑、不扩大结论。历史 timeout、decode error、两次 exit 1、时区错误、未停止事实与编辑 warning 保留在 `:91-100`。
- 无需改写历史记录或修复建议。

## 验收边界及本次执行范围

- 本轮收到的执行记录称 Flash 窄修 exit 0、停稳，无 native error、缺产物或越界；该轮只做只读状态/日志审阅和编辑，没有执行测试。此为提供的记录，非本审查独立核验。
- 本次只读审查 `git diff c53eef6..HEAD` 涉及 `app.js`、`storage.py`、`docs/UI-v7-B-implementation.md` 三个既有修订文件；本次仅修改本文件。
- 未执行测试、语法检查、HTTP、联网、服务、GUI、真实 DB 操作或模型调用；未提交、切分支、清理或读写桥状态。当前 GUI 验收保持 `null`，审查者不代替人工 accept。
- **准入意见：可进入主线程有限实机验收。** 此意见不等于真实客户端已验证，不改变桥 BLOCKED 或历史执行 FAIL。

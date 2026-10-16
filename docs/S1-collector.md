# S1 采集适配器：OpenCLI 头条热榜/推荐流（只读）

实现：`tools/collect_opencli.py`。状态：**采集器已实现并实机 dry-run 通过；真实写库路径本轮未执行，属未实证项**（见文末）。

本文记录：用了哪个命令、完整参数、执行时间、返回条数、原始字段到本地 schema 的映射、限流策略、失败路径和未实证项。

## 一、复用清单（先复用，后新写）

| 复用对象 | 真实路径 / 版本 | 用途 | 许可与使用方式 |
| --- | --- | --- | --- |
| OpenCLI 头条站点适配器 | `C:\Users\Lenovo\.dsh\profiles\desktop\node_modules\@jackwener\opencli\clis\toutiao\{hot.js,recommend.js,utils.js}`，npm `@jackwener/opencli` **1.8.8** | 取数：公开热榜、公开推荐流 | Apache-2.0。**只作为外部 CLI 调用（subprocess），不复制其代码**，无许可传染 |
| 浏览器自动化配方 | 同上（`hot`/`recommend` 均为 `strategy=public`、`browser=false`） | 这两个端点直连公开 JSON，**不依赖浏览器桥、不需要登录态** | 直接复用，无新写 |
| 项目既有代码 | `storage.py`：`connect/start_run/import_batch/save_snapshot/publication/canonical_url/normalized_text/digest/json_bytes`；`records` 表与 `snapshots/` 约定（`docs/M0-M1-contract.md`） | 写库、快照、时间质量、URL 规范化、正文哈希 | 直接复用，未新开数据库、未改表结构 |
| 项目既有代码 | `server.py`：`classify` | `records.sentiment` 的粗略情感标签 | 复用同一份词典，避免两处实现漂移；仍是未复核初筛 |

**必须新写的部分及理由**（其余都不重复实现）：

1. CLI 定位：OpenCLI 未提供“找到入口 main.js”的 API，只能按已知 profile 路径 / `OPENCLI_MAIN` / `--opencli` 探测。
2. 字段映射：上游字段名到项目 `records` 的对应关系是本项目特有的，上游没有、也不该有。
3. 参数解析与错误呈现：把上游退出码/JSON 错误信封转换成项目统一的失败报告，避免静默降级。

未使用 MediaCrawler；未新增登录、未读取或复制任何 cookie；未引入新依赖（仅 Python 标准库 + 上述本机已装组件）。

## 二、命令与参数

```
python -B tools/collect_opencli.py [--endpoint hot|recommend] [--limit N] [--dry-run]
                                  [--group-id G] [--db PATH] [--node PATH] [--opencli PATH] [--timeout S]
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--endpoint` | `hot` | 读端点，仅允许 `hot` / `recommend`（白名单，argv 由常量拼装，无法传入任何写动作） |
| `--limit` | `3` | 1–50；契约要求单次 ≤3 页或 ≤50 条 |
| `--dry-run` | 关 | 只采集并打印映射结果，**完全不打开/创建数据库**（`db.touched=false`） |
| `--group-id` | 按端点 `toutiao-hot` / `toutiao-recommend` | 来源分组标识，决定 `feeds.name` 与 `feed_id`（见“未实证项 3”） |
| `--db` | `<仓库>/sentiment.db` | 既有证据库路径 |
| `--node` | 自动探测 | 未指定时依次尝试 `OPENCLI_NODE` → `PATH` 中的 `node` → 本机托管 node → `C:\Program Files\nodejs\node.exe` |
| `--opencli` | 自动探测 | 未指定时依次尝试 `OPENCLI_MAIN` → `~/.dsh/profiles/*/node_modules/@jackwener/opencli/dist/src/main.js` → `~/node_modules/...` → `%APPDATA%\npm\node_modules\...` |
| `--timeout` | `60` | 单次调用超时秒数；超时即停止，不重试 |

实际拼装并执行的命令（`call.argv`，`request_count=1`）：

```
<node> <opencli dist/src/main.js> toutiao hot       --limit 3 -f json
<node> <opencli dist/src/main.js> toutiao recommend --limit 3 -f json
```

## 三、真实验证（本轮在 worktree 内实跑）

本轮实测环境：node `C:\Program Files\nodejs\node.EXE`（系统 node，v22.22.2）；OpenCLI 1.8.8；
入口 `C:\Users\Lenovo\.dsh\profiles\desktop\node_modules\@jackwener\opencli\dist\src\main.js`；
被测文件 `tools/collect_opencli.py` SHA-256 = `b350e69594fd77434bcba0282b445cfd58112bc1eefcf46917751d349299b2ac`。

### 3.1 `--dry-run --limit 3`（hot）

- 执行时间（UTC）：`2026-10-16T14:31:21.381896+00:00` → `14:31:21.781058+00:00`（0.399s）
- 返回条数：**3**；退出码 `0`；stderr 空
- 真实 stdout（为便于阅读，仅 `url`/`canonical_url` 与 `argv`/`cwd` 中的绝对路径前缀做了省略标注，其余为原文）：

```json
{
  "collector": "s1-opencli-toutiao",
  "status": "ok",
  "endpoint": "hot",
  "group_id": "toutiao-hot",
  "limit": 3,
  "dry_run": true,
  "node": "C:\\Program Files\\nodejs\\node.EXE",
  "opencli": "C:\\Users\\Lenovo\\.dsh\\profiles\\desktop\\node_modules\\@jackwener\\opencli\\dist\\src\\main.js",
  "opencli_version_expectation": "1.8.8 (npm @jackwener/opencli；实测版本见 docs/S1-collector.md)",
  "access": "read",
  "request_count": 1,
  "call": {
    "argv": ["...node.EXE", "...\\opencli\\dist\\src\\main.js", "toutiao", "hot", "--limit", "3", "-f", "json"],
    "cwd": "...\\attempts\\attempt-1a53ff294ef746c697ae6463c7d00df0\\workspace",
    "timeout_seconds": 60,
    "started_at": "2026-10-16T14:31:21.381896+00:00",
    "finished_at": "2026-10-16T14:31:21.781058+00:00",
    "duration_seconds": 0.399,
    "return_code": 0
  },
  "returned_count": 3,
  "db": { "path": "...\\workspace\\sentiment.db", "present": false, "touched": false },
  "records": [
    {
      "id": "(写库时由 storage.import_batch 生成 record-<uuid4>)",
      "feed_id": "(写库时按来源分组解析/创建)",
      "canonical_id": "(写库时按 exact-text-v1 精确正文规则分配)",
      "raw_sha256": "f8bf802242f9f4b381b6df03c625025763bdf9dec0c45b91d33feba12dce54a7",
      "raw_path": "snapshots/<raw_sha256>.json",
      "title": "男子中1000万不到一月花到仅剩28元",
      "summary": "男子中千万彩票挥霍一空还因诈骗获刑",
      "url": "https://www.toutiao.com/trending/7696707174784909353/?category_name=topic_innerflow&event_type=hot_board&log_pb=%7B...%7D&rank=&style_id=40132&topic_id=7696707174784909353",
      "canonical_url": "（与 url 相同，仅协议/主机/空路径/片段规范化）",
      "source_id": "7696707174784909353",
      "published_at": null,
      "published_raw": null,
      "time_quality": "missing",
      "rank": 1,
      "raw_sha256_method": "sha256(storage.json_bytes(row))，与 save_snapshot 落盘字节一致"
    },
    { "rank": 2, "title": "国务院免去王刚原职 此前已履新河北", "summary": "", "source_id": "7696921328695427113",
      "raw_sha256": "d70ebb41a9e1f69573f67ed97a2c177934b089a8bb202b83520de56c32e3e8d1",
      "published_at": null, "time_quality": "missing" },
    { "rank": 3, "title": "读懂中国粮的丰收密码", "summary": "", "source_id": "7697242262857829926",
      "raw_sha256": "0504b72e229fa8aa30034f20027463e5e20bfc8a26e58e1e53a94f4780364d40",
      "published_at": null, "time_quality": "missing" }
  ],
  "notes": ["本轮只执行 --dry-run；真实写库路径未在本轮执行，属未实证项。", "...（共 4 条，见源码 NOTES）"]
}
```

### 3.2 `--endpoint recommend --dry-run --limit 3`

- 执行时间（UTC）：`2026-10-16T14:31:21.894161+00:00` → `14:31:23.087622+00:00`（1.193s）
- 返回条数：**3**；退出码 `0`；stderr 空
- 关键样例字段（完整 stdout 同上结构）：

| rank | title（截断显示） | summary | source_id | published_raw | time_quality | raw_sha256 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `#好剧推荐# #因为一个片段看了整部剧# #短剧#` | `""` | 7687978633574564375 | `2026-10-16T14:31:22Z` | `valid` | `78ef1fca7bc52ec2c55dd0185b6c2bb36a5e8e0337422910827f7c4f7f85d445` |
| 2 | `听我一句劝， 千万不要去看《玉簟秋》…`（长文，本地折叠空白） | `""` | 1879027772131523 | `2026-10-16T14:23:52Z` | `valid` | `08cba417b1902148bd8ce06ff037f8c395537cafdc8112aaf86a8d3e86aa06b0` |
| 3 | `中方代表团登上北方四岛，高市早苗气急败坏，我外交部反将一军` | `一则来自俄罗斯远东的通告…`（abstract） | 7696940246570730018 | `2026-10-16T14:16:22Z` | `valid` | `850f444e3bf5957317da9a5ecf8aa03b61b030dfe4179d8957c61ce65fbe742f` |

### 3.3 参数与失败路径的定向检查（不产生上游请求）

| 检查 | 命令 | 观察结果 |
| --- | --- | --- |
| 语法（内存 compile，未落盘 pyc） | `python -B -c "compile(open('tools/collect_opencli.py',encoding='utf-8').read(),'tools/collect_opencli.py','exec')"` | `memory compile OK`，退出码 0 |
| limit 越界 | `... --dry-run --limit 0` | 退出码 `2`，标准 JSON `status:"blocked"`，`error.stage="args"`（校验在调用上游之前，故不产生任何上游请求） |
| 端点白名单 | `... --endpoint delete --dry-run` | 退出码 `2`，argparse 原文：`argument --endpoint: invalid choice: 'delete' (choose from 'hot', 'recommend')` |
| 复用 `server.classify` 可调用（纯函数，无 DB、无网络） | `python -B -c "import sys;sys.path.insert(0,'.');import server;print(server.classify('事故 风险 投诉'), server.classify('增长 成功 突破'))"` | 返回 `偏负面 偏正面`，退出码 0 |

### 3.4 本轮上游请求计数（如实披露）

本轮尝试共发出 **8 次** toutiao 只读请求，每次 `--limit 3`（≤50 条，各为单次调用，无分页、无重试）：
2 次直接探测 CLI 输出结构（`toutiao hot --limit 3 -f json`、`toutiao recommend --limit 3 -f json`），
6 次采集器 dry-run（其中 2 次用于修掉下列缺陷后重新取证、2 次用于绑定最终文件哈希）。
每次运行内部 `request_count=1`。

### 3.5 实机发现的缺陷与修复

| 缺陷 | 触发 | 修前表现 | 修后（实测） |
| --- | --- | --- | --- |
| Windows 控制台默认 GBK，上游正文含 emoji（U+1F602）时 `print` 崩溃 | `--endpoint recommend --dry-run --limit 3` | 退出码 `1`，`UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f602'`，无 JSON 输出 | `_force_utf8_stdout()` 把 stdout/stderr 切到 UTF-8；退出码 `0`，stderr 空 |
| `--limit` 越界时 `CollectError` 逃出 `main` | `--limit 0` | 退出码 `1`，Python Traceback | 纳入统一错误报告；退出码 `2`，输出 `status:"blocked"` 的 JSON |

静态检查（`node --check`/AST/compile）都通不过这两个缺陷——只有真跑才暴露。

## 四、字段映射表

上游每行原始字段先整体序列化进快照 `raw_xml`（`json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",",":"))`），再投影到 `records`：

| 本地 `records` 列 | hot 来源 | recommend 来源 | 处理方式（复用哪个既有函数） |
| --- | --- | --- | --- |
| `id` | — | — | `storage.import_batch` 生成 `record-<uuid4>` |
| `feed_id` | `--group-id`（默认 `toutiao-hot`） | `--group-id`（默认 `toutiao-recommend`） | 按 `feeds.url`（端点公开 URL）查既有来源；缺失时按 `server.py` 相同 INSERT 语句建行 |
| `source_id` | `group_id` | `group_id` | 来源内 ID，原样 |
| `title` | `title` | `title` | `storage.normalized_text`（去 HTML、实体解码、折叠空白） |
| `summary` | `query`（等于 `title` 时置 `""`） | `abstract`（缺失为 `""`） | 同上 |
| `url` | `url` | `url` | 原始值入库 |
| `canonical_url` | `url` | `url` | `storage.canonical_url`（仅协议/主机大小写、默认端口、空路径、片段；保留 query） |
| `content_sha256` | `[title, summary]` | `[title, summary]` | `storage.digest(storage.json_bytes(...))`，精确正文去重键 |
| `raw_sha256` / `raw_path` | 整行（含 `raw_xml`） | 同左 | `storage.save_snapshot` → `snapshots/<sha256>.json`，写后回读校验 |
| `raw_xml`（快照内） | 条目原始 JSON | 同左 | 保留全部上游字段，供逐字段追溯 |
| `published_at` | 恒 `null`（热榜条目无发布时间） | `published_at` → UTC ISO | `storage.publication`；非法/无时区→`null` |
| `published_raw` | `null` | 原始字符串 | 原样 |
| `fetched_at` | 观测时间 | 观测时间 | `storage.now()`（UTC） |
| `time_quality` | 实测 `missing` | 实测 `valid` | `storage.publication`：`valid/missing/invalid/timezone_missing` |
| `canonical_id` / `dedup_reason` | — | — | `exact-text-v1` 规则：相同正文复用，否则 `independent` |
| `run_id` | — | — | `storage.start_run(db, feed_id, "opencli-hot"/"opencli-recommend", limit)` |
| `sentiment` | — | — | `server.classify`（未复核词典粗筛，不代表准确情感分析） |

**未映射到本地的上游字段**：`rank`、`hot_value`、`label`、`image_url`（hot）；`rank`、`source`、`tag`、`comments`、`image_url`（recommend）。它们只存在于 `raw_xml` 快照里；尤其 `comments`（评论数）本轮**不写入任何本地计数列**，避免用未核实口径冒充评论总数。

## 五、限流策略

- 每次运行**只发起 1 次**上游请求（`request_count=1`）：不翻页、不重试、不并发、不再追加请求。
- `--limit` 硬上限 50，默认 3，与契约“单次 ≤3 页或 ≤50 条”一致。
- recommend 上游虽回 `next.max_behot_time` 游标但不生效，故不实现分页——分页会是空动作。
- 无定时任务、无后台轮询；只由人工显式执行。建议相邻两次运行留出数十秒间隔（本轮实跑单次 0.4–1.3s；未观测到限流响应）。
- 单次调用超时 60s；超时即停止并报原文，**不自动重试**。
- 上游 4xx/5xx、限流、风控文案都按原始退出码/原文上报（见下节），不降级成“成功但空”。

## 六、失败路径

| 场景 | 退出码 | 行为 |
| --- | --- | --- |
| 找不到 node | 2 | `status:"blocked"`，附全部探测过的候选路径 |
| 找不到 OpenCLI `main.js` | 2 | 同上，提示用 `--opencli` 指定 |
| `--limit` 越界 | 2 | `error.stage="args"`，不调上游 |
| `--endpoint` 非白名单 | 2 | argparse 原始错误原文 |
| opencli 退出码非 0（HTTP 非 2xx / 风控 / `BROWSER_CONNECT` 等） | 3 | 附真实 `return_code`、stderr 原文、stdout 头部；带 JSON 错误信封时附 `error.code` |
| 调用超时 | 3 | 已停止，不重试，附 argv |
| stdout 非合法 JSON | 4 | 附 stdout 前 2000 字符 |
| 上游返回空列表 | 5 | 明确报空，不当成功 |
| 条目缺 `title`/`url` | 6 | 附该条原始 JSON，**不静默丢弃** |
| 打开证据库失败 | 7 | 附库路径与原始异常 |
| 写库异常（校验/磁盘/SQLite） | 7 | 附 `run_id`、异常类型与原文；`import_batch` 内部已把批次置 `failed` |

所有失败都输出机器可读 JSON（`error.stage` + 原文），不把失败伪装成空结果。

## 七、未实证项（本轮未做，不得当成已完成）

1. **真实写库路径未执行**。本轮只跑 `--dry-run`。未验证：`feeds` 建行、`records` 落库、`snapshots/<sha256>.json` 落盘与哈希回读、`run` 状态与 `added/duplicates` 计数。需要用户授权后运行（不加 `--dry-run`），并在独立库或明确批准的库上核对。
2. **上游长期可用性与限流行为未实证**。只观测到本轮若干次 200；未观测 429/风控/空列表的真实响应与原文。
3. **`--group-id` 语义为假设**：本实现解释为“来源分组标识”（决定 `feeds.name`/`feed_id`）。若需求原意是“头条单帖 `group_id` 过滤”，需要改参数语义——未与需求方确认。
4. **hot 无发布时间**：热榜条目不含 `published_at`，故 `time_quality` 恒为 `missing`；hot 的热度/时间口径未与产品确认。
5. **评论数口径未实证**：recommend 的 `comments_count` 是否为“评论总数”未核实，因此本轮不写入任何本地计数列。
6. **OpenCLI 版本漂移**：字段名固定按 1.8.8 实测；升级后是否变化未验证。
7. **跨环境自动定位未验证**：`--node`/`--opencli` 自动探测只在本机 Windows 环境实测；非 Windows 或 profile 布局不同的环境未验证。
8. **快照膨胀未评估**：上游 `hot_value`/`comments` 每次变化都会产生新的 `raw_sha256`（按 `UNIQUE(feed_id,raw_sha256)` 视为一次新观测）。长期是否需要按天/按窗截断，本轮未设计。
9. **未做 UI 接入**：`tools/` 采集器未接入 `server.py` 的任何 HTTP 路径；`docs/` 只描述采集器本身。混入 RSS 来源列表的影响见下。

## 八、已知风险与边界

- 采集器写入的 `feeds` 行**不是 RSS 源**，只是 `records.feed_id` 的关联分组（名称默认 `toutiao-hot`/`toutiao-recommend`）。若在页面对其点击“刷新来源”，`server.py` 会按 RSS 解析该公开 JSON 端点并失败——这是既有 `/api/refresh` 的语义，本轮未改动，也**未加防护**。
- 只读范围：仅 `access=read`、`strategy=public` 的两个端点；无发布/评论/关注/删除动作，不绕过登录与风控，不复制 cookie，不读取凭据。
- 采集器不写桥的状态文件，不启动服务、不订阅定时任务。

# S1 采集适配器：OpenCLI 多来源公开热榜/推荐流（只读）

实现：`tools/collect_opencli.py`。状态：**5 个端点（头条 hot / 头条 recommend / 微博 hot / 贴吧 hot / 36氪 hot）已实机 `--dry-run` 通过；真实写库路径本轮未执行，属未实证项**（见第七节）。

本文记录：用了哪个命令、完整参数、执行时间、返回条数、逐源原始字段到本地 schema 的映射、限流策略、失败路径和未实证项。

## 一、复用清单（先复用，后新写）

| 复用对象 | 真实路径 / 版本 | 用途 | 许可与使用方式 |
| --- | --- | --- | --- |
| OpenCLI 站点适配器（头条） | `C:\Users\Lenovo\.dsh\profiles\desktop\node_modules\@jackwener\opencli\clis\toutiao\{hot.js,recommend.js,utils.js}`，npm `@jackwener/opencli` **1.8.8** | 取数：公开热榜、公开推荐流 | Apache-2.0。**只作为外部 CLI 调用（subprocess），不复制其代码**，无许可传染 |
| OpenCLI 站点适配器（微博/贴吧/36氪） | 同目录 `clis\{weibo,tieba,36kr}\hot.js`（本轮新增复用，未改动这些文件） | 取数：微博热搜榜、贴吧热议榜、36氪热榜 | 同上：只调用 CLI，不复制代码 |
| 浏览器自动化配方 | 同上（头条两个端点声明为 `strategy=public`、`browser=false`） | 头条端点直连公开 JSON，**不依赖浏览器桥、不需要登录态** | 直接复用，无新写 |
| 项目既有代码 | `storage.py`：`connect/start_run/import_batch/save_snapshot/publication/canonical_url/normalized_text/digest/json_bytes`；`records` 表与 `snapshots/` 约定（`docs/M0-M1-contract.md`） | 写库、快照、时间质量、URL 规范化、正文哈希 | 直接复用，未新开数据库、未改表结构 |
| 项目既有代码 | `server.py`：`classify` | `records.sentiment` 的粗略情感标签 | 复用同一份词典，避免两处实现漂移；仍是未复核初筛 |

**必须新写的部分及理由**（其余都不重复实现）：

1. CLI 定位：OpenCLI 未提供“找到入口 main.js”的 API，只能按已知 profile 路径 / `OPENCLI_MAIN` / `--opencli` 探测。
2. 声明式映射表 `ENDPOINTS`：上游字段名到项目 `records` 的对应关系是本项目特有的，上游没有、也不该有；三个新来源的字段名与头条不同（微博标题在 `word`，36氪只有 `title/url`），必须逐源声明。
3. 参数解析与错误呈现：把上游退出码/JSON 错误信封转换成项目统一的失败报告，避免静默降级。

未使用 MediaCrawler；未新增登录、未读取或复制任何 cookie；未引入新依赖（仅 Python 标准库 + 上述本机已装组件）。

## 二、命令与参数

```
python -B tools/collect_opencli.py [--endpoint hot|recommend|weibo-hot|tieba-hot|36kr-hot]
                                   [--limit N] [--dry-run]
                                   [--group-id G] [--db PATH] [--node PATH] [--opencli PATH] [--timeout S]
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--endpoint` | `hot` | 读端点白名单：`hot` / `recommend`（头条）、`weibo-hot` / `tieba-hot` / `36kr-hot`（本轮新增）。argv 由常量拼装，无法传入任何写动作 |
| `--limit` | `3` | 1–50；契约要求单次 ≤3 页或 ≤50 条 |
| `--dry-run` | 关 | 只采集并打印映射结果，**完全不打开/创建数据库**（实测 `db.present=false`、`db.touched=false`，运行后仓库内无 `sentiment.db`） |
| `--group-id` | 按端点：`toutiao-hot` / `toutiao-recommend` / `weibo-hot` / `tieba-hot` / `36kr-hot` | 来源分组标识，决定 `feeds.name` 与 `feed_id`（见未实证项 3） |
| `--db` | `<仓库>/sentiment.db` | 既有证据库路径 |
| `--node` | 自动探测 | 依次尝试 `OPENCLI_NODE` → `PATH` 中的 `node` → 本机托管 node → `C:\Program Files\nodejs\node.exe` |
| `--opencli` | 自动探测 | 依次尝试 `OPENCLI_MAIN` → `~/.dsh/profiles/*/node_modules/@jackwener/opencli/dist/src/main.js` → `~/node_modules/...` → `%APPDATA%\npm\node_modules\...` |
| `--timeout` | `60` | 单次调用超时秒数；超时即停止，不重试 |

实际拼装并执行的命令（`call.argv`，每次运行 `request_count=1`）：

```
<node> <opencli dist/src/main.js> toutiao hot       --limit 3 -f json   # --endpoint hot
<node> <opencli dist/src/main.js> toutiao recommend --limit 3 -f json   # --endpoint recommend
<node> <opencli dist/src/main.js> weibo   hot       --limit 3 -f json   # --endpoint weibo-hot
<node> <opencli dist/src/main.js> tieba   hot       --limit 3 -f json   # --endpoint tieba-hot
<node> <opencli dist/src/main.js> 36kr    hot       --limit 3 -f json   # --endpoint 36kr-hot
```

## 三、声明式映射表（端点 → 子命令 / 参数 / 字段 / feed）

`tools/collect_opencli.py` 的 `ENDPOINTS` 是唯一的数据驱动来源；映射逻辑里**没有任何按站点分支的 if/else**，`map_rows()` 只读表里的 `fields`。表在导入时自检键完整性（`_validate_endpoints()`）。

| 端点 | opencli 子命令 | feed 分组名 | feed_url（分组标识，见未实证项 7） |
| --- | --- | --- | --- |
| `hot` | `toutiao hot` | `toutiao-hot` | `https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc` |
| `recommend` | `toutiao recommend` | `toutiao-recommend` | `https://www.toutiao.com/api/pc/feed/` |
| `weibo-hot` | `weibo hot` | `weibo-hot` | `https://s.weibo.com/top/summary` |
| `tieba-hot` | `tieba hot` | `tieba-hot` | `https://tieba.baidu.com/hottopic/browse/topicList` |
| `36kr-hot` | `36kr hot` | `36kr-hot` | `https://36kr.com/hot-list/catalog` |

`fields`（本地字段 → 上游原始字段名，`None` = 上游没有该字段，一律留 null/空串，**不从 URL、标题或其它字段反推**）：

| 本地字段 | `hot` | `recommend` | `weibo-hot` | `tieba-hot` | `36kr-hot` |
| --- | --- | --- | --- | --- | --- |
| `title` | `title` | `title` | **`word`** | `title` | `title` |
| `url` | `url` | `url` | `url` | `url` | `url` |
| `summary` | `query` | `abstract` | `category` | `description` | `None` |
| `source_id` | `group_id` | `group_id` | `None` | `None` | `None` |
| `published_at` | `None` | `published_at` | `None` | `None` | `None` |

`summary_empty_when_same_as_title`：`hot` 为 `true`（头条热榜的 `query` 就是标题本身，相同即置空），其余端点为 `false`。

## 四、字段映射表（逐源）

**上游实测原始字段**（2026-10-16，opencli 1.8.8，`-f json`；头条两个端点沿用首轮实测，本轮未重跑）：

| 端点 | 上游原始字段（实测所见） |
| --- | --- |
| `toutiao hot` | `rank`、`title`、`query`、`group_id`、`hot_value`、`label`、`image_url` |
| `toutiao recommend` | `rank`、`title`、`abstract`、`group_id`、`published_at`、`comments`、`source`、`tag`、`image_url` |
| `weibo hot` | `rank`、`word`、`category`、`hot_value`、`label`、`url`（**无 `title`**） |
| `tieba hot` | `rank`、`title`、`description`、`discussions`、`url` |
| `36kr hot` | `rank`、`title`、`url`（实测仅此三字段） |

上游每行先整体序列化进快照 `raw_xml`（`json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",",":"))`），再投影到 `records`：

| 本地 `records` 列 | 各来源取值 | 处理方式（复用哪个既有函数） |
| --- | --- | --- |
| `id` | — | `storage.import_batch` 生成 `record-<uuid4>` |
| `feed_id` | 按 `--group-id`（默认见上表） | 按 `feeds.url`（端点 `feed_url`）查既有来源；缺失时按 `server.py` 相同 INSERT 语句建行 |
| `source_id` | 头条两源取 `group_id`；微博/贴吧/36氪 = `null` | 上游没有独立 ID 字段。**不**从 URL 里反推 `topic_id` / 文章号，保持“缺字段留 null” |
| `title` | 见上表 | `storage.normalized_text`（去 HTML、实体解码、折叠空白） |
| `summary` | 见上表；36氪无对应字段 → `""` | 同上。`records.summary` 为 NOT NULL，故缺字段用空串如实表示，不填猜测文本 |
| `url` | 见上表 | 原始值入库 |
| `canonical_url` | 各来源 `url` | `storage.canonical_url`（仅协议/主机大小写、默认端口、空路径、片段；保留 query） |
| `content_sha256` | `[title, summary]` | `storage.digest(storage.json_bytes(...))`，精确正文去重键 |
| `raw_sha256` / `raw_path` | 整行（含 `raw_xml`） | `storage.save_snapshot` → `snapshots/<sha256>.json`，写后回读校验 |
| `published_at` | 仅头条 recommend 有；其余恒 `null` | `storage.publication`；非法/无时区→`null` |
| `published_raw` | 同上（原样） | 原样 |
| `fetched_at` | 观测时间 | `storage.now()`（UTC） |
| `time_quality` | 新三源实测均为 `missing`；头条 recommend 实测 `valid` | `storage.publication`：`valid/missing/invalid/timezone_missing` |
| `canonical_id` / `dedup_reason` | — | `exact-text-v1` 规则：相同正文复用，否则 `independent` |
| `run_id` | — | `storage.start_run(db, feed_id, "opencli-<endpoint>", limit)`（如 `opencli-weibo-hot`） |
| `sentiment` | — | `server.classify`（未复核词典粗筛，不代表准确情感分析） |

**未映射到本地的上游字段**（只存在于 `raw_xml` 快照里，**不写入任何本地计数列**）：

| 端点 | 未映射字段 | 原因 |
| --- | --- | --- |
| `toutiao hot` | `rank`、`hot_value`、`label`、`image_url` | 热度值/标记非评论数，避免用未核实口径冒充评论总数 |
| `toutiao recommend` | `rank`、`comments`、`source`、`tag`、`image_url` | 同上（`comments` 口径未核实） |
| `weibo hot` | `rank`、`hot_value`、`label` | `hot_value` 是热度值不是评论数；`label` 是「热/新」标记 |
| `tieba hot` | `rank`、`discussions` | `discussions` 形如 `"197.3W实时讨论"`：带单位的近似实时讨论量，既不是评论总数也不是精确值 |
| `36kr hot` | `rank` | 榜单名次 |

`rank` 仅作为 dry-run 预览的展示字段（`records` 无此列）。

## 五、真实验证（本轮在 worktree 内实跑）

### 5.1 实测环境与文件指纹

- 工作区（attempt worktree）：`...\attempts\attempt-6f46943ed94c4e95ad37a14d374dba8b\workspace`，基线 commit `61982d3caed9bc3b3140f8799b0b63b9075f0124`
- node：`C:\Program Files\nodejs\node.EXE`（v22.22.2）；OpenCLI 入口：`C:\Users\Lenovo\.dsh\profiles\desktop\node_modules\@jackwener\opencli\dist\src\main.js`（npm `@jackwener/opencli` 1.8.8）
- 被测文件 `tools/collect_opencli.py`：**SHA-256 `abb81ebae25521d1d5971e277d2154a8706a3b96f4e22b9c17beaecf7b1dd4fd`，20714 字节**（下方 5.2–5.5 的四次运行均在此字节版本上产生）
- 运行后核对：仓库内 `sentiment.db` 与 `snapshots\` **均不存在**（`git status` 只有 `tools/collect_opencli.py` 一处修改）

### 5.2 `--endpoint hot --dry-run --limit 3`（既有端点回归）

- 执行时间（UTC）：`2026-10-16T15:08:48.378445+00:00` → `15:08:48.957975+00:00`（0.580s）；退出码 `0`；stderr 空；返回条数 **3**
- 与首轮一致的关键字段：`feed_url` 未变、`source_id` 来自 `group_id`、`summary` 由 `query` 得到且等于标题时置空、`published_at=null`、`time_quality=missing`
- 真实 stdout（仅两处长 URL 串按首轮同样的约定做了省略标注，其余为原文）：

```json
{
  "collector": "s1-opencli", "status": "ok", "endpoint": "hot", "group_id": "toutiao-hot",
  "limit": 3, "dry_run": true,
  "node": "C:\\Program Files\\nodejs\\node.EXE",
  "opencli": "C:\\Users\\Lenovo\\.dsh\\profiles\\desktop\\node_modules\\@jackwener\\opencli\\dist\\src\\main.js",
  "access": "read", "request_count": 1,
  "call": { "endpoint": "hot",
    "argv": ["...node.EXE", "...\\opencli\\dist\\src\\main.js", "toutiao", "hot", "--limit", "3", "-f", "json"],
    "cwd": "...\\attempt-6f46943ed94c4e95ad37a14d374dba8b\\workspace", "timeout_seconds": 60,
    "started_at": "2026-10-16T15:08:48.378445+00:00", "finished_at": "2026-10-16T15:08:48.957975+00:00",
    "duration_seconds": 0.58, "return_code": 0 },
  "feed_url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
  "field_map": { "title": "title", "url": "url", "summary": "query", "source_id": "group_id", "published_at": null },
  "returned_count": 3,
  "db": { "path": "...\\workspace\\sentiment.db", "present": false, "touched": false },
  "records": [
    { "raw_sha256": "677aaf7d982febea8c5c7b8519e2ea3df1a514a3a5c540da1699f2950feaed8e",
      "title": "内蒙古：美丽秋日黄旗海", "summary": "", "source_id": "7697219932156739603",
      "url": "https://www.toutiao.com/trending/7697219932156739603/?...(长 query 省略)",
      "canonical_url": "(同上规范化)", "published_at": null, "published_raw": null,
      "time_quality": "missing", "rank": 1 },
    { "raw_sha256": "f9a42a1b33c105228b22c43bd20239f2da2e1dbc1940d413c6c4f52fbc601d73",
      "title": "国务院免去王刚原职 此前已履新河北", "summary": "", "source_id": "7696921328695427113",
      "published_at": null, "time_quality": "missing", "rank": 2 },
    { "raw_sha256": "e8dc5439e068aa91deb6bde45904d695ab250ea5ac62deadd51a3c50cbc7b90e",
      "title": "读懂中国粮的丰收密码", "summary": "", "source_id": "7697242262857829926",
      "published_at": null, "time_quality": "missing", "rank": 3 }
  ]
}
```

### 5.3 `--endpoint weibo-hot --dry-run --limit 3`（新增）

- 命令与参数：`python -B tools/collect_opencli.py --endpoint weibo-hot --dry-run --limit 3`
- 执行时间（UTC）：`2026-10-16T15:08:48.377386+00:00` → `15:08:51.913899+00:00`（3.537s）；**退出码 `0`**；stderr 空；**返回条数 3**
- 真实 stdout（完整原文）：

```json
{
  "collector": "s1-opencli", "status": "ok", "endpoint": "weibo-hot", "group_id": "weibo-hot",
  "limit": 3, "dry_run": true,
  "node": "C:\\Program Files\\nodejs\\node.EXE",
  "opencli": "C:\\Users\\Lenovo\\.dsh\\profiles\\desktop\\node_modules\\@jackwener\\opencli\\dist\\src\\main.js",
  "access": "read", "request_count": 1,
  "call": { "endpoint": "weibo-hot",
    "argv": ["...node.EXE", "...\\opencli\\dist\\src\\main.js", "weibo", "hot", "--limit", "3", "-f", "json"],
    "cwd": "...\\attempt-6f46943ed94c4e95ad37a14d374dba8b\\workspace", "timeout_seconds": 60,
    "started_at": "2026-10-16T15:08:48.377386+00:00", "finished_at": "2026-10-16T15:08:51.913899+00:00",
    "duration_seconds": 3.537, "return_code": 0 },
  "feed_url": "https://s.weibo.com/top/summary",
  "field_map": { "title": "word", "url": "url", "summary": "category", "source_id": null, "published_at": null },
  "returned_count": 3,
  "db": { "path": "...\\workspace\\sentiment.db", "present": false, "touched": false },
  "records": [
    { "raw_sha256": "0522c1e7b8ec9f396bb1c5c5f25f95ea0207abea6f47ac6d634801c80c46f66b",
      "title": "母亲将大一儿子生活费从4000降至2000", "summary": "民生新闻", "source_id": null,
      "url": "https://s.weibo.com/weibo?q=%23%E6%AF%8D%E4%BA%B2...%23",
      "canonical_url": "https://s.weibo.com/weibo?q=%23%E6%AF%8D%E4%BA%B2...%23",
      "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 1 },
    { "raw_sha256": "bc1c93e422f448169225328ceaa1546cb443297b6e176048cc5858b05f5df3a3",
      "title": "河北卫视靠孙颖莎收视登顶了", "summary": "体育", "source_id": null,
      "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 2 },
    { "raw_sha256": "c2b93b88103fc2eada807cf9f93c38a70b87c1f88837b838e03da0e3310d9217",
      "title": "中央红军长征路线全览", "summary": "民生新闻", "source_id": null,
      "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 3 }
  ],
  "notes": ["本轮只执行 --dry-run；真实写库路径未在本轮执行，属未实证项。", "...（共 5 条，见源码 NOTES）"]
}
```

（`url`/`canonical_url` 两处百分号编码串为节省版面截断，原文是完整 URL；标题与 `summary` 为原样。）
真实上游条目原文（同一次探测，`opencli weibo hot --limit 3 -f json`）：`rank/word/category/hot_value/label/url`，例如
`{"rank":1,"word":"母亲将大一儿子生活费从4000降至2000","category":"民生新闻","hot_value":1322345,"label":"热","url":"https://s.weibo.com/weibo?q=%23...%23"}`。

### 5.4 `--endpoint tieba-hot --dry-run --limit 3`（新增）

- 命令与参数：`python -B tools/collect_opencli.py --endpoint tieba-hot --dry-run --limit 3`
- 执行时间（UTC）：`2026-10-16T15:08:48.382824+00:00` → `15:08:49.845533+00:00`（1.463s）；**退出码 `0`**；stderr 空；**返回条数 3**
- 真实 stdout（完整原文）：

```json
{
  "collector": "s1-opencli", "status": "ok", "endpoint": "tieba-hot", "group_id": "tieba-hot",
  "limit": 3, "dry_run": true,
  "access": "read", "request_count": 1,
  "call": { "endpoint": "tieba-hot",
    "argv": ["...node.EXE", "...\\opencli\\dist\\src\\main.js", "tieba", "hot", "--limit", "3", "-f", "json"],
    "started_at": "2026-10-16T15:08:48.382824+00:00", "finished_at": "2026-10-16T15:08:49.845533+00:00",
    "duration_seconds": 1.463, "return_code": 0 },
  "feed_url": "https://tieba.baidu.com/hottopic/browse/topicList",
  "field_map": { "title": "title", "url": "url", "summary": "description", "source_id": null, "published_at": null },
  "returned_count": 3,
  "db": { "path": "...\\workspace\\sentiment.db", "present": false, "touched": false },
  "records": [
    { "raw_sha256": "6f2b9896f495f53593eaaabb1859707416ac9054af82ec3f243706d56e5fafc3",
      "title": "没吃回扣!TES郭皓回应签证问题",
      "summary": "TES经理郭皓回应Zuian签证争议，澄清贪腐及“打招呼”等不实传闻：俱乐部统一申请B签，不存在为节约成本选择B签，若贪过一毛钱，接受任何指...",
      "source_id": null,
      "url": "https://tieba.baidu.com/hottopic/browse/hottopic?topic_id=28367277&topic_name=%E6%B2%A1%E5%90%83...",
      "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 1 },
    { "raw_sha256": "96a772c41c71d53216cb94caa70630d8addc3849a69bd2872def0bcda067ed6d",
      "title": "S16开赛!揭幕战欧洲拿首胜",
      "summary": "S16全球总决赛入围赛揭幕战，KC 3-2 击败C9晋级胜者组，EU赛区终结NA赛区在国际赛的BO5连胜。",
      "source_id": null, "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 2 },
    { "raw_sha256": "5a0820a781534219b58ff992201df24db741242802e386e22f7a0c444e9286a6",
      "title": "WE拿下BRO,进军德杯决赛",
      "summary": "德玛西亚杯国际邀请赛半决赛，WE 3-2 击败LCK战队BRO成功挺进总决赛，将与BFX争冠。",
      "source_id": null, "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 3 }
  ]
}
```

真实上游条目原文（同一次探测）：`{"rank":1,"title":"没吃回扣!TES郭皓回应签证问题","discussions":"197.3W实时讨论","description":"TES经理郭皓回应Zuian签证争议…","url":"https://tieba.baidu.com/hottopic/browse/hottopic?topic_id=28367277&topic_name=..."}`，
其中 `discussions`（"197.3W实时讨论"）**未映射到任何本地列**，只留在 `raw_xml` 快照内。

### 5.5 `--endpoint 36kr-hot --dry-run --limit 3`（新增）

- 命令与参数：`python -B tools/collect_opencli.py --endpoint 36kr-hot --dry-run --limit 3`
- 执行时间（UTC）：`2026-10-16T15:08:48.382414+00:00` → `15:08:50.162985+00:00`（1.781s）；**退出码 `0`**；stderr 空；**返回条数 3**
- 真实 stdout（完整原文）：

```json
{
  "collector": "s1-opencli", "status": "ok", "endpoint": "36kr-hot", "group_id": "36kr-hot",
  "limit": 3, "dry_run": true,
  "access": "read", "request_count": 1,
  "call": { "endpoint": "36kr-hot",
    "argv": ["...node.EXE", "...\\opencli\\dist\\src\\main.js", "36kr", "hot", "--limit", "3", "-f", "json"],
    "started_at": "2026-10-16T15:08:48.382414+00:00", "finished_at": "2026-10-16T15:08:50.162985+00:00",
    "duration_seconds": 1.781, "return_code": 0 },
  "feed_url": "https://36kr.com/hot-list/catalog",
  "field_map": { "title": "title", "url": "url", "summary": null, "source_id": null, "published_at": null },
  "returned_count": 3,
  "db": { "path": "...\\workspace\\sentiment.db", "present": false, "touched": false },
  "records": [
    { "raw_sha256": "22e219db160e8fd9597aed469d3e86ad846f155ce167ad40380deeb68c149b9f",
      "title": "8点1氪丨三部门联合调查“尊界V800汽车刹车踏板支架断裂问题”；国内一半AI算力被指在造爽剧；台积电第三季度净利润7631亿元台币",
      "summary": "", "source_id": null, "url": "https://36kr.com/p/4027802678186372",
      "canonical_url": "https://36kr.com/p/4027802678186372",
      "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 1 },
    { "raw_sha256": "a9ad6a14f63d3791bebba2cffe65569ab51e75eb8b1162e8a3e8715a9311e008",
      "title": "是的，特斯拉真造了一辆「飞行汽车」", "summary": "", "source_id": null,
      "url": "https://36kr.com/p/4028000383324547", "published_at": null, "time_quality": "missing", "rank": 2 },
    { "raw_sha256": "a3078b2837541a0749d05331629580f6354060fbbfed49a56b0fec571ac2704b",
      "title": "刚刚，Meta亮出美国最强开源权重模型，两台Mac Studio就能跑", "summary": "", "source_id": null,
      "url": "https://36kr.com/p/4027808045224326", "published_at": null, "time_quality": "missing", "rank": 3 }
  ]
}
```

真实上游条目原文（同一次探测）：`{"rank":1,"title":"8点1氪丨三部门联合调查…","url":"https://36kr.com/p/4027802678186372"}`——**只有 `rank`/`title`/`url` 三个字段**，因此 `summary` 留空串。

### 5.6 参数与失败路径的定向检查（不产生上游请求）

| 检查 | 命令 | 观察结果 |
| --- | --- | --- |
| 语法（内存 compile，未落盘 pyc） | `python -B -c "compile(open('tools/collect_opencli.py',encoding='utf-8').read(),'tools/collect_opencli.py','exec')"` | `memory compile OK`，退出码 0 |
| 端点白名单 | `... --endpoint delete --dry-run` | 退出码 `2`，argparse 原文：`argument --endpoint: invalid choice: 'delete' (choose from '36kr-hot', 'hot', 'recommend', 'tieba-hot', 'weibo-hot')` |
| limit 越界 | `... --endpoint weibo-hot --dry-run --limit 0` | 退出码 `2`，`status:"blocked"`，`error.stage="args"`，`endpoint:"weibo-hot"`（校验在调用上游之前，故无上游请求） |
| 映射表完整性自检 | 导入模块 | 5 个端点的声明键与 `fields` 键均通过 `_validate_endpoints()`，无 RuntimeError |
| `--dry-run` 不碰数据库 | 上述四次 dry-run | 每次报告 `db.present=false`、`db.touched=false`；运行后 `Test-Path sentiment.db` = `False`、`Test-Path snapshots` = `False` |

### 5.7 本轮上游请求计数（如实披露）

本轮共发出 **14 次**只读请求（每次单次调用、无分页、无重试、每条 ≤50）：

- 6 次直接探测 CLI 输出结构（`weibo/tieba/36kr hot --limit 1` 与 `--limit 3` 各一次），用于把逐源原始字段名写进映射表；
- 8 次采集器 dry-run：`hot`、`weibo-hot`、`tieba-hot`、`36kr-hot` 各 2 次（第一次落在含注释修订的旧字节版本上，第二次绑定最终文件哈希 `abb81eb…` 后重跑取证）。

每次运行内部 `request_count=1`。未观测到限流/风控响应；**不当成“上游长期可用”的证据**（见第七节）。

### 5.8 首轮（头条）历史证据（保留，未覆盖）

首轮在同一 worktree 家族的另一 attempt（`attempt-1a53ff294ef746c697ae6463c7d00df0`）内实跑，被测文件 SHA-256 为 `b350e69594fd77434bcba0282b445cfd58112bc1eefcf46917751d349299b2ac`：

- `--endpoint hot --dry-run --limit 3` → 退出码 0，3 条，0.399s；
- `--endpoint recommend --dry-run --limit 3` → 退出码 0，3 条，1.193s（`published_at` 为带 Z 的时间串，`time_quality=valid`）；
- 首轮修掉的两个实机缺陷：Windows 控制台 GBK 下含 emoji 时 `print` 崩溃（`_force_utf8_stdout()`）、`--limit` 越界时 Traceback 逃出 `main`（纳入统一错误报告）。这两个修复本轮仍在，且本轮 14 次运行均未复现。

## 六、限流策略

- 每次运行**只发起 1 次**上游请求（`request_count=1`）：不翻页、不重试、不并发、不再追加请求。
- `--limit` 硬上限 50，默认 3，与契约“单次 ≤3 页或 ≤50 条”一致。
- recommend 上游虽回 `next.max_behot_time` 游标但不生效，故不实现分页——分页会是空动作；新三源同样不实现分页。
- 无定时任务、无后台轮询；只由人工显式执行。本轮 5 个端点单次实测 0.4–3.5s，未观测到限流响应。
- 单次调用超时 60s；超时即停止并报原文，**不自动重试**。
- 上游 4xx/5xx、限流、风控文案都按原始退出码/原文上报（见下节），不降级成“成功但空”。

## 七、失败路径

| 场景 | 退出码 | 行为 |
| --- | --- | --- |
| 找不到 node | 2 | `status:"blocked"`，附全部探测过的候选路径 |
| 找不到 OpenCLI `main.js` | 2 | 同上，提示用 `--opencli` 指定 |
| `--limit` 越界 | 2 | `error.stage="args"`，不调上游 |
| `--endpoint` 非白名单 | 2 | argparse 原始错误原文 |
| opencli 退出码非 0（HTTP 非 2xx / 风控 / `BROWSER_CONNECT` 等） | 3 | 附**该端点名**、真实 `return_code`、stderr 原文、stdout 头部；带 JSON 错误信封时附 `error.code`。**不**用别的站点数据顶替 |
| 调用超时 | 3 | 已停止，不重试，附该端点名与 argv |
| stdout 非合法 JSON | 4 | 附 stdout 前 2000 字符 |
| 上游返回空列表 | 5 | 明确报空，不当成功（该站点 0 条就报 0 条） |
| 条目缺 `title`/`url` | 6 | 附该条原始 JSON，**不静默丢弃** |
| 打开证据库失败 | 7 | 附库路径与原始异常 |
| 写库异常（校验/磁盘/SQLite） | 7 | 附 `run_id`、异常类型与原文；`import_batch` 内部已把批次置 `failed` |

每个端点各自独立调用：任一站点失败只影响该站点的报告，其余站点不会被拿来顶替，也不会把失败写成空结果。

## 八、未实证项（本轮未做，不得当成已完成）

1. **真实写库路径未执行**。本轮四次运行全部带 `--dry-run`。未验证：`feeds` 建行（新三源的 `feed_url` 分组行）、`records` 落库、`snapshots/<sha256>.json` 落盘与哈希回读、`run` 状态与 `added/duplicates` 计数。需要用户授权后去掉 `--dry-run` 运行，并在独立库或明确批准的库上核对。
2. **站点级失败路径未触发**。本轮 5 个端点全部退出码 0、非空；上表第 3/4/5 行（非 0 退出、非 JSON、空列表）本轮**没有真实发生**，未取得原文证据。这些分支沿用首轮既有代码，未改动。
3. **“贴吧/36氪是否需要浏览器桥”本轮未独立取证**。本轮三源都是普通 subprocess 调用即成功；而此前同机曾观察到 `tieba hot` 报 `BROWSER_CONNECT: extension not connected`。因此**不能断言**这三个端点“不依赖浏览器桥”。各端点在 OpenCLI 内的 `strategy`/`browser` 声明本轮未从上游源码核实（读取 `~/.dsh/...` 下 OpenCLI 源码在本会话被权限拒绝），只以实跑结果为准。运行前应确认浏览器扩展处于连接状态，否则会把连接层故障当成站点故障。
4. **`--group-id` 语义为假设**：本实现解释为“来源分组标识”（决定 `feeds.name`/`feed_id`）。若需求原意是“头条单帖 `group_id` 过滤”，需要改参数语义——未与需求方确认。
5. **新三源的 `time_quality` 恒为 `missing`**：微博/贴吧/36氪热榜条目本轮实测均不含发布时间字段，故 `published_at` 恒为 `null`。是否要用抓取时刻作为“观测时间”另立列，未与产品确认。
6. **`feed_url` 仅作分组标识，未取证**。新三源的 `feed_url` 是该来源公开热榜页地址，本轮**未验证**它与 OpenCLI 内部实际请求的上游接口是否等价；也**未**对它发起任何请求。它只用于 `feeds.url` 分组与复用判定。
7. **OpenCLI 版本漂移**：字段名固定按 1.8.8 实测；升级后是否变化未验证（`--limit 3` 只观测到榜单头部若干条，非全量字段清单）。
8. **快照膨胀未评估**：上游 `hot_value`/`discussions`/`label` 每次变化都会产生新的 `raw_sha256`（按 `UNIQUE(feed_id,raw_sha256)` 视为一次新观测）。本轮已实测到微博同一标题两次运行哈希不同。长期是否需要按天/按窗截断，本轮未设计。
9. **未做 UI 接入**：`tools/` 采集器未接入 `server.py` 的任何 HTTP 路径；`docs/` 只描述采集器本身。
10. **静态发现的既有缺陷（本轮未触发、未修）**：`main()` 中“打开证据库失败”分支调用 `emit_error(exc, report)` 时会把 `report["status"]="ok"` 覆盖回 `status:"ok"`（其余失败分支正常）。这是首轮就存在的代码，属**静态阅读**发现，本轮走 `--dry-run` 未触发；按最小改动原则本轮未修。

## 九、已知风险与边界

- 采集器写入的 `feeds` 行**不是 RSS 源**，只是 `records.feed_id` 的关联分组（名称如 `weibo-hot`、`36kr-hot`）。若在页面对其点击“刷新来源”，`server.py` 会按 RSS 解析该公开页地址并失败——这是既有 `/api/refresh` 的语义，本轮未改动，也**未加防护**。
- 只读范围：仅 `access=read` 的 5 个公开榜单读取命令；无发布/评论/关注/删除动作，不绕过登录与风控，不复制 cookie，不读取凭据。
- 上游“热度值/讨论量”一律不进本地计数列，也不冒充评论数（贴吧 `discussions` 是带单位的近似值）。
- 采集器不写桥的状态文件，不启动服务、不订阅定时任务。
- 本轮代码改动（相对基线 `61982d3`，只改 `tools/collect_opencli.py` 与本文档）：
  1. `ENDPOINTS` 由两只头条端点扩为 5 只，改为声明式（`argv`/`feed_url`/`default_group`/`fields`/`summary_empty_when_same_as_title`）并加导入期完整性自检；
  2. `map_rows()` 删除按站点分支，改为只读 `fields` 表；新增 `take()`；
  3. `COLLECTOR` 标识由 `s1-opencli-toutiao` 改为 **`s1-opencli`**（工具已多来源，避免误标）；报告新增 `feed_url`、`field_map`、`call.endpoint` 三个字段；
  4. `--endpoint` 白名单与 `--group-id` 帮助文案更新。
  以上 3 项报告字段/标识变化是**有意为之并在此声明**；头条两端的取值语义、`--dry-run` 行为、错误信封转换、快照 `snapshots/<sha256>.json` 写法、`records` 字段映射均未改变（见 5.2 实跑）。

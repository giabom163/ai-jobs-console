# AI 就业舆情核查 · 重构说明

## 1. 复制事实

| 项 | 值 |
|---|---|
| 原系统 | `C:\Users\Lenovo\Documents\Codex\2026-10-15\c-users-lenovo-documents-codex-handoffs\outputs\sentiment-monitor` |
| 新系统 | `D:\DSH\repos\ai-employment-monitor` |
| 复制文件 | 337 个 |
| 数据库 SHA-256 | `134A4C002A0F0E3A2480D12A536E75847FE9176BFE205273602BC344040387EA` |
| 原系统状态 | 未改动；8790 继续保持停止 |
| Git 快照 | `26fc9c7 chore: snapshot copy before AI employment refactor` |
| 重构分支 | `ai-employment` |

新系统可以随便改；原系统保持不动，作为回退和对照。

## 2. 重构原则

1. **先并行，不替换**：新系统独立目录、独立数据库；原系统不动。
2. **岗位与舆情分轨**：岗位是结构化数据，舆情是文本；不要塞进同一张 `records`。
3. **每日增量优先**：先做“新岗位/关闭岗位/改薪岗位”，再做长周期趋势。
4. **优先公开 ATS**：Greenhouse/Lever/Ashby/Workable 公开 board 优先；招聘平台只做合规补充。
5. **AI 岗位要分类**：build / adjacent / exposed / none，不能只搜“AI”两个字。
6. **可回退**：每个阶段保留旧表/旧脚本，不一次性删。

## 3. 现有架构映射

| 原能力 | 新用途 | 处理 |
|---|---|---|
| `storage.py` 证据库/去重/批次 | 复用 | 新增 jobs/job_snapshots/job_changes |
| `server.py` API/鉴权 | 复用 | 新增岗位 API、总览指标 |
| `collect_opencli.py` 舆情采集 | 保留 | 只采新闻/社交 |
| `collect_bilibili_24h.py` | 保留 | 舆情侧视频 |
| `relevance.py` | 重写 | 换成 AI 岗位/技能/裁员词表 |
| `topics.py` / 聚类 | 重写 | 按公司/岗位/技能聚类 |
| `model_annotate_batch.py` | 复用 | 提示词改岗位分类 |
| 日报 HTML/PDF | 重做 | 新增岗位/关闭/薪资/城市 |
| `overview-charts.js` | 重做 | 岗位趋势/公司/技能 |

## 4. P0 范围（先做）

1. 建 5 张表：`companies`、`jobs`、`job_snapshots`、`job_changes`、`job_daily`。
2. 接 5–10 家 AI 公司的公开 ATS board（Greenhouse/Lever/Ashby）。
3. 每日抓取一次，识别 new/changed/closed。
4. 输出“今日新增 AI 岗位”JSON + 一份简版日报。
5. 不接招聘平台登录态，不碰候选人个人信息。

## 5. 非目标

- 不做求职投递、简历优化。
- 不做全网职位搜索。
- 不做候选人画像。
- 不把岗位数当“就业形势”结论；必须配舆情与裁员信号。

## 6. 下一步

1. 确认 P0 的 5–10 家公司名单。
2. 建 schema（`schema/ai_employment.sql`）。
3. 写 `tools/collect_ats.py`，先跑 3 家验证。
4. 再接日报模板。

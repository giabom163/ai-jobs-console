# 前端方案：从「性别舆情」转成「AI 岗位真实性核查」

日期：2026-10-22

## 1. 产品定位

不是“岗位搜索站”，而是“AI 就业形势核查台”：

- 主数据：AI 相关岗位的每日新增/关闭/变更；
- 辅数据：AI 就业新闻、社交情绪、裁员信号；
- 核心信任：每条岗位都能追溯到来源、原始链接、最后确认时间；
- 核心判断：岗位是否真实、是否 AI 相关、是否仍在招。

## 2. 信息架构

```
/                今日总览
/jobs            岗位库（主入口）
/companies       公司雷达
/signals         就业舆情 / 裁员信号
/reports         日报 / 周报
/sources         数据源健康与真实性方法
/about           口径与免责声明
```

### 2.1 今日总览
- KPI：今日新增岗位、今日关闭、AI build 岗位、监控公司数、数据源异常数。
- 趋势：近 30 天新增/关闭双线；按公司/城市切换。
- Top 公司：新增最多、关闭最多、AI 岗位占比最高。
- Top 岗位：按 title 聚类。
- 数据源健康：每个 ATS 最后抓取时间、条数、失败原因。
- 真实性分布：已验证/可信/待核验/低可信。

### 2.2 岗位库（核心）
表格列：

| 列 | 说明 |
|---|---|
| 公司 | 公司名 + 来源徽标 |
| 岗位 | 标题，AI 类型标签 |
| 地点 | 城市/远程 |
| 薪资 | 有则显示，无则“未披露” |
| 来源 | Greenhouse/Lever/Ashby/官网/聚合器 |
| 真实性 | 分数 + 等级；点开看证据 |
| 首次发现 | first_seen |
| 最近确认 | last_seen |
| 状态 | open/closed |

筛选：公司、AI 类型（build/adjacent/exposed）、地点、远程、薪资有无、来源、真实性等级、首次发现日期、状态。

行展开抽屉：
- 原始申请链接（apply URL）；
- 公司 ATS board 链接；
- 内容哈希 / 首次发现 / 最近确认；
- 同标题其他地点/重复项；
- “为什么判 AI 相关”：命中词；
- “真实性证据”：来源层级、域名、跨源确认、最后 HTTP 检查。

### 2.3 公司雷达
- 公司列表：在招数、30 天新增、关闭、AI 占比、最后抓取。
- 公司详情：岗位时间线、按部门/地点分布、来源 board。
- 关注公司：只看重点公司。

### 2.4 就业舆情
- 新闻/社交/裁员信号，不再做“性别冲突”；
- 标签：裁员、招聘冻结、AI 替代、技能焦虑、薪资、政策；
- 每条带来源与时间；不与岗位数混算。

### 2.5 数据源与真实性
- 每个源：类型、最后成功、条数、失败原因、速率；
- 真实性方法说明与限制；
- 不展示“绝对真实”，只展示“来源可信度”。

## 3. 真实性模型（核心）

### 3.1 来源分级
| 层级 | 来源 | 基础分 |
|---|---|---:|
| A | 公司 ATS 公开 board（Greenhouse/Lever/Ashby/Workable/SmartRecruiters/Recruitee） | 45 |
| B | 公司官网招聘页/飞书招聘 | 35 |
| C | 聚合 API（Adzuna/Jooble/USAJobs/Remotive/RemoteOK/HN） | 20 |
| D | 招聘平台（Boss/猎聘/智联/LinkedIn/Indeed） | 10 |
| E | 无法追溯的转发/截图 | 0 |

### 3.2 加分/减分
| 信号 | 分值 |
|---|---:|
| apply URL 域名与公司/ATS 匹配 | +20 |
| 今天在 ATS 仍可见（last_seen 当日） | +15 |
| 跨源确认（ATS + 聚合器/官网同岗） | +10 |
| 标题/地点/描述完整 | +10 |
| 同一岗位多城市/猎头重复 | -10 |
| 仅聚合器、无原始链接 | -15 |
| 30 天无更新且无关闭标记 | -10 |
| apply URL 4xx/5xx 或跳转异常 | -25 |

等级：
- 90–100 已验证（ATS 原始 + 域名 + 当日确认）
- 70–89 可信
- 50–69 待核验
- <50 低可信（默认折叠/不展示）

**注意**：这是“来源可信度”，不是对雇主的主观判断；页面必须写清。

### 3.3 后端校验任务
- 每日对 open 岗位做 apply URL HEAD/GET 检查（限速）；
- 校验 apply URL host 是否属于公司域名或 ATS 域名；
- 记录 last_verified_at、http_status、final_url；
- 关闭岗位保留 30 天，标 closed，不删除；
- 不自动把“聚合器岗位”升级为“已验证”。

## 4. 视觉方案

- 基调：数据台，不是情绪大屏；深蓝/灰白，状态色只用于真实性/状态。
- 组件：KPI 卡、趋势图、公司榜、岗位表、证据抽屉、源健康表。
- 图表：ECharts 双线趋势、堆叠柱（新增/关闭）、公司横向条。
- 交互：筛选器可分享到 URL；表格支持排序/固定列；移动端折叠为卡片。
- 空态/错误态：明确写“该源未返回/被限流”，不显示假 0。

## 5. 技术落地

- 不复用旧 `index.html` 的性别文案与页面结构；新建 `web/` 前端壳。
- 保留旧页面在 `/legacy`，迁移完成再删。
- 原生 JS + ECharts，避免引入 Vue/React 重写。
- API 新增：
  - `/api/jobs`（筛选/分页/排序）
  - `/api/jobs/<id>`（证据）
  - `/api/companies`
  - `/api/jobs/trend`
  - `/api/sources/health`
  - `/api/authenticity/<job_id>`
- 数据库：`jobs` 增加 `last_verified_at/http_status/final_url/authenticity_score/authenticity_level`。

## 6. 参考

- [schema.org JobPosting](https://schema.org/JobPosting)：岗位字段标准。
- [Google JobPosting 结构化数据](https://developers.google.com/search/docs/appearance/structured-data/job-posting)：字段完整性参考。
- [EMSCAD 假岗位数据集](https://www.kaggle.com/datasets/shivamb/real-or-fake-fake-jobposting-prediction)：假岗位特征参考。
- GitHub：
  - [JobShield-AI](https://github.com/mrvaibhavbhardwaj/JobShield-AI)：假岗位检测。
  - [InternPilot](https://github.com/Om-5640/InternPilot)：ghost posting 五信号评分。
  - [Job-Posting-Fraud-Verification-Engine](https://github.com/shsial1993-lab/Job-Posting-Fraud-Verification-Engine)：规则+模型判定。
- 版式参考：LinkedIn Jobs / Indeed 的岗位卡与来源标识；Layoffs.fyi 的趋势呈现；AIHOT 的日报/信源健康页。

## 7. 实施顺序

1. 后端补 `authenticity_score` 字段 + 来源分级 + apply URL 校验。
2. `/api/jobs` + `/api/companies` + `/api/sources/health`。
3. 新前端壳：总览 + 岗位库（先只读）。
4. 公司雷达 + 数据源健康。
5. 舆情/裁员页。
6. 旧页面迁 `/legacy`，最后删除。

预计：后端 2–3 天，前端 4–6 天，真实性校验 1–2 天。

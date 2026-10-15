# 舆情观察台

本地运行的 RSS/Atom 舆情初筛 MVP。使用 Python 标准库和 SQLite，无第三方依赖；默认仅监听 `127.0.0.1:8790`。不会定时采集，只有点击“刷新所有来源”才访问已添加的公开 RSS 地址。

## 启动

```powershell
python -B .\server.py --limit 3
```

打开控制台打印的本机地址，添加公开 RSS/Atom 地址并设置关注关键词。数据库 `sentiment.db` 会在项目目录首次启动时创建。按 Ctrl+C 停止服务。

## 功能与边界

- 添加公开 RSS/Atom 来源，手动刷新按编号排列的前 limit 个来源，每源最多 limit 条，响应上限 2 MB。首次限制为 3；扩大前需明确授权。
- SQLite 保存来源记录、规范记录和批次；完全相同正文合并规范记录，保留各来源；同源相同原文重复导入不新增记录。
- 缺失、非法或无时区发布时间保留未知，抓取时间独立保存。原文 JSON 快照保存到 snapshots/ 并回读核验哈希。
- 关键词配置会保存到本地；配置非空时，条目列表只显示标题或摘要命中至少一个关键词的内容。
- 正面/负面标签使用内置中英文词典粗略判断，属于初筛提示，不代表准确情感分析。
- 无登录态、社交平台接口、后台定时任务、云端同步或模型调用。

## 当前限制

来源地址由操作者提供；抓取前会解析并拦截非公网 IP，也会在重定向时检查目标。仍不要将服务暴露到公网；当前没有来源删除、导出、用户权限或长期运行守护功能。

## M0/M1 状态

三项隔离样本检查已返回 PASS；产品真实路径及最终验收仍 BLOCKED。详见 docs/M0-M1-contract.md 和 schemas/import.schema.json。
旧 items 数据原样保留且不自动迁移；接口报告 legacy_items_pending，不能把旧回填日期当作真实发布时间。

三项隔离样本检查（用户已授权本项目小范围自动测试；不访问网络、不改产品数据库）：

```powershell
python -B .\tools\manual_m1_check.py --limit 3 --work-dir 'C:\Users\Lenovo\Documents\Codex\2026-10-15\c-users-lenovo-appdata-local-temp\work\m1-manual'
```

输出 result.json 仅证明本地样本；仍需真实客户端及 DSH 审查。小范围自动测试以本项目 AGENTS.md 为准；服务启动和采集仍按各自授权执行。

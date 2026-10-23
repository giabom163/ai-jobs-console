# AI 岗位核查台

公开岗位的聚合与核查：采集公开招聘页面里的 AI 研发 / AI 支撑岗位，按规则判定是否真属于 AI，核验链接存活，最后发布成静态站。

- 线上站：https://giabom.online
- 站点仓库：https://github.com/giabom163/ai-console-site
- 本仓库：数据流水线 + 规则 + 导出（https://github.com/giabom163/ai-jobs-console）

> 旧「舆情观察台」（性别议题监测）已下线并清除，代码只保留在归档仓库
> `giabom163/sentiment-monitor` 与本仓库历史提交里，不再参与运行。

## 目录

```
ai_jobs.db            主库（SQLite，含 jobs / companies / job_changes / job_snapshots）
tools/ai_lexicon.py   AI 判定词典与分级规则（唯一事实源）
tools/collect_*.py    采集：美团关键词、ATS（Greenhouse / Ashby）
tools/reclassify_jobs.py  按词典重算 + 剔除 none
tools/dedup_jobs.py   去重
tools/liveness.py     三态存活判定（active / uncertain / expired）
tools/prune_jobs.py   存活复检 → closed / stale
tools/verify_jobs.py  URL 校验 + 真实性打分
tools/export_console.py   导出站点静态数据（当前使用）
tools/ai_server.py    本地 API 服务（127.0.0.1:8792）
web/index.html        核查台前端源码
```

## 日常流水线

```powershell
python tools\collect_meituan.py --keywords "算法,大模型,机器学习,AI Agent,计算机视觉,自然语言处理" --limit 400
python tools\collect_ats.py
python tools\reclassify_jobs.py --exclude-none
python tools\dedup_jobs.py
python tools\prune_jobs.py --limit 1500 --workers 6
python tools\verify_jobs.py --limit 800 --workers 6
python tools\export_console.py --out <站点仓库>\public\console-data.json
```

## 判定口径（摘要）

- 时间窗：只收 `posted_at >= 2026-06-01`。
- build：强 AI 词 + 研发动作；adjacent：强 AI 词 + 产品/运营/评测，或 AI 相邻技术词；其余 none（状态 excluded，不物理删除）。
- 排除职能岗（招聘/人事/人力/财务/税务/法务/销售/商务/渠道/客服…）、「（AI 驱动）」类限定语、机器人硬件。
- 商业化/合规/负责人/经理/TPM 归 adjacent（标注 `business:`），不算研发。
- 存活三态：`uncertain` 永不判死；美团走详情接口，ATS 用「缺席即关闭」。

完整规则、部署与回滚见站点仓库的 `docs/交接报告-AI就业核查台-20261023.md`。

## 本地查看

```powershell
.\launch_ai_jobs.cmd
```

打开 http://127.0.0.1:8792/ （只读服务）。

## 边界

- 只覆盖 8 家来源，美团占比高，不代表全市场。
- 「已验证」只代表链接当日可访问，不代表岗位真实或仍在招聘。
- 数据靠手动跑流水线，没有定时任务。

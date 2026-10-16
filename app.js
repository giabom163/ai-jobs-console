"use strict";

/* 舆情观察台 M2 工作台
 * 仅使用标准 HTML/CSS/JS。所有动态内容通过 DOM API 或 textContent 写入，
 * 不使用 innerHTML 插入来自接口/用户的正文，避免 HTML 注入。
 */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const QUALITY_LABELS = {
  valid: "时间格式有效",
  missing: "时间缺失",
  invalid: "时间非法",
  timezone_missing: "缺时区",
  legacy_unverified: "旧数据未核验"
};
const RUN_STATUS_LABELS = {
  running: "进行中",
  succeeded: "成功",
  failed: "失败",
  interrupted: "中断（服务退出）"
};

const state = {
  data: null,
  manualLimit: null,
  feedsById: {},
  runsById: {},
  online: false,
  loadedAt: null,
  p1: null
};

/* ---------- 安全 DOM 构建 ---------- */

function appendChild(node, child) {
  if (child === null || child === undefined || child === false || child === true) return;
  if (Array.isArray(child)) {
    for (const item of child) appendChild(node, item);
    return;
  }
  node.append(child instanceof Node ? child : document.createTextNode(String(child)));
}

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "onclick") node.addEventListener("click", value);
      else node.setAttribute(key, String(value));
    }
  }
  for (const child of children) appendChild(node, child);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function isSafeUrl(value) {
  return typeof value === "string" && /^https?:\/\//i.test(value);
}

function linkNode(value, label) {
  const text = label ?? value;
  if (isSafeUrl(value)) {
    return h("a", { href: value, target: "_blank", rel: "noopener noreferrer", text: text || value });
  }
  return h("span", { class: "muted", text: text || "（无有效 http(s) 链接）" });
}

/* ---------- 接口封装：区分离线 / 后端错误，原样保留状态码与消息 ---------- */

class ApiError extends Error {
  constructor(message, status, kind) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
  }
}

async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store"
    });
  } catch (err) {
    throw new ApiError("无法连接本地服务（离线或服务未启动）", 0, "offline");
  }
  const raw = await response.text();
  let data = null;
  if (raw) {
    try { data = JSON.parse(raw); } catch (err) { data = null; }
  }
  if (!response.ok) {
    const message = data && typeof data.error === "string" && data.error ? data.error : `HTTP ${response.status}`;
    const error = new ApiError(message, response.status, "http");
    error.payload = data;
    throw error;
  }
  return data;
}

/* ---------- 通用展示 ---------- */

function show(message, error = false) {
  const el = $("#notice");
  el.textContent = message || "";
  el.classList.toggle("error", !!error);
  el.classList.toggle("ok", !error && !!message);
}

function setConnection(kind, text) {
  const el = $("#connection");
  el.classList.remove("online", "offline", "error", "loading");
  el.classList.add(kind);
  el.textContent = text;
}

function fmtTime(value) {
  if (value === null || value === undefined || value === "") return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间无法解析";
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit"
  });
}

function fmtRatio(value) {
  if (value === null || value === undefined) return "—（空表）";
  return (value * 100).toFixed(1) + "%";
}

function qualityLabel(quality) {
  return QUALITY_LABELS[quality] || quality || "未知";
}

function statusLabel(status) {
  return RUN_STATUS_LABELS[status] || status || "未知";
}

function shortHash(value) {
  return typeof value === "string" && value ? value.slice(0, 12) + "…" : "—";
}

function feedLabel(feedId) {
  const feed = state.feedsById[feedId];
  return feed ? `#${feedId} ${feed.name}` : `#${feedId ?? "?"}`;
}

function kv(label, valueNode) {
  return [h("dt", { text: label }), h("dd", null, valueNode)];
}

/* ---------- 视图切换 ---------- */

function selectView(name) {
  for (const tab of $$(".tab")) {
    const on = tab.dataset.view === name;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
  }
  for (const view of $$(".view")) {
    view.hidden = view.id !== "view-" + name;
  }
}

/* ---------- 渲染：统计与来源 ---------- */

function renderStats() {
  const data = state.data;
  if (!data) return;
  const stats = data.stats || {};
  $("#stat-records").textContent = String(stats.record_count ?? 0);
  $("#stat-canonical").textContent = String(stats.canonical_count ?? 0);
  $("#stat-unknown").textContent = String(stats.unknown_time_count ?? 0);
  $("#stat-unknown-ratio").textContent = fmtRatio(stats.unknown_time_ratio);
  $("#stat-feeds").textContent = String((data.feeds || []).length);
  $("#stat-shown").textContent = `${(data.items || []).length} / ${data.items_limit ?? 500}`;

  $("#meta-schema").textContent = data.schema_version || "—";
  $("#meta-coverage").textContent = data.coverage === "sample_only"
    ? "sample_only（仅样本，不代表总体）"
    : (data.coverage || "—");
  $("#meta-analysis").textContent = data.analysis_status === "unreviewed_dictionary"
    ? "unreviewed_dictionary（本地词典粗筛，未复核）"
    : (data.analysis_status || "—");

  const counts = stats.time_quality_counts;
  const dist = $("#quality-dist");
  clear(dist);
  if (counts && Object.keys(counts).length) {
    const parts = Object.keys(counts).sort().map((key) => `${qualityLabel(key)}(${key})=${counts[key]}`);
    dist.textContent = parts.join("；");
  } else {
    dist.textContent = "无记录";
  }

  const legacy = data.legacy_items_pending ?? 0;
  $("#legacy-note").textContent = legacy > 0
    ? `${legacy} 条旧 items 原样保留，未自动迁移；旧时间不得当作真实发布时间。`
    : "0 条（无旧 items 待处理）。";
}

function renderRefreshLabel() {
  const button = $("#refresh");
  button.textContent = state.manualLimit == null
    ? "刷新来源"
    : `刷新来源（前 ${state.manualLimit} 个来源，每源最多 ${state.manualLimit} 条）`;
}

function renderFeeds() {
  const body = $("#feeds-body");
  clear(body);
  const feeds = state.data ? state.data.feeds || [] : [];
  if (!feeds.length) {
    body.append(h("tr", null, h("td", { colspan: "6", class: "muted" }, "还没有来源。请在左侧添加公开 RSS 地址。")));
    return;
  }
  for (const feed of feeds) {
    const lastRun = feed.last_run_id ? state.runsById[feed.last_run_id] : null;
    const runCell = feed.last_run_id
      ? h("span", null,
          h("button", { class: "link", type: "button", onclick: () => openRunById(feed.last_run_id) }, feed.last_run_id),
          lastRun ? h("small", { class: "muted" }, ` ${statusLabel(lastRun.status)}`) : null)
      : h("span", { class: "muted", text: "无批次" });
    body.append(h("tr", null,
      h("td", null, h("b", { text: feed.name })),
      h("td", null, linkNode(feed.url)),
      h("td", { class: "num" }, String(feed.item_count ?? 0)),
      h("td", null, fmtTime(feed.last_success_at)),
      h("td", null, runCell),
      h("td", null, h("button", { class: "secondary", type: "button", onclick: () => useFeedForImport(feed.id) }, "用此来源导入"))
    ));
  }
}

function fillSelect(select, feeds, allLabel) {
  const current = select.value;
  clear(select);
  if (allLabel !== undefined) select.append(h("option", { value: "all", text: allLabel }));
  for (const feed of feeds) {
    select.append(h("option", { value: String(feed.id), text: `#${feed.id} ${feed.name}` }));
  }
  if (current && Array.from(select.options).some((option) => option.value === current)) {
    select.value = current;
  }
}

function renderSourceOptions() {
  const feeds = state.data ? state.data.feeds || [] : [];
  fillSelect($("#filter-source"), feeds, "全部来源");
  fillSelect($("#import-feed"), feeds);
}

function renderKeywords() {
  const data = state.data;
  if (!data) return;
  $("#keyword-form [name=keywords]").value = (data.keywords || []).join("，");
}

function useFeedForImport(feedId) {
  const select = $("#import-feed");
  select.value = String(feedId);
  const textarea = $("#import-form [name=records]");
  textarea.focus();
  show(`已选择来源 ${feedLabel(feedId)}，粘贴 JSON 记录后点击导入。`);
}

/* ---------- 渲染：原文与证据 ---------- */

function currentFilters() {
  return {
    source: $("#filter-source").value,
    quality: $("#filter-quality").value,
    sentiment: $("#filter-sentiment").value,
    keyword: $("#filter-keyword").value.trim().toLocaleLowerCase("zh-CN")
  };
}

function filteredItems() {
  const data = state.data;
  if (!data) return [];
  const filters = currentFilters();
  return (data.items || []).filter((item) => {
    if (filters.source !== "all" && String(item.feed_id) !== filters.source) return false;
    if (filters.quality !== "all" && item.time_quality !== filters.quality) return false;
    if (filters.sentiment !== "all" && item.sentiment !== filters.sentiment) return false;
    if (filters.keyword) {
      const haystack = `${item.title || ""} ${item.summary || ""}`.toLocaleLowerCase("zh-CN");
      if (!haystack.includes(filters.keyword)) return false;
    }
    return true;
  });
}

function recordCard(item) {
  return h("article", { class: "card" },
    h("div", { class: "meta" },
      h("span", { class: `badge ${item.sentiment === "偏正面" ? "pos" : item.sentiment === "偏负面" ? "neg" : "neu"}` }, item.sentiment || "未知"),
      h("span", { class: "chip" }, `来源 ${item.feed_name ?? item.feed_id ?? "?"}`),
      h("span", { class: "chip" }, `时间质量 ${qualityLabel(item.time_quality)}`),
      h("time", { class: "muted" }, fmtTime(item.published))
    ),
    h("h3", null, linkNode(item.link, item.title || "（无标题）")),
    h("p", { class: "excerpt" }, (item.summary || "").slice(0, 400) || "（无摘要）"),
    h("dl", { class: "kv" },
      kv("记录ID", h("code", { text: item.id })),
      kv("规范记录ID", h("code", { text: item.canonical_id })),
      kv("快照哈希 sha256", h("code", { title: item.raw_sha256 || "", text: shortHash(item.raw_sha256) })),
      kv("内容哈希 sha256", h("code", { title: item.content_sha256 || "", text: shortHash(item.content_sha256) })),
      kv("去重", item.dedup_reason === "exact_text" ? "exact_text（正文精确重复）" : "independent（独立正文）"),
      kv("抓取时间", fmtTime(item.fetched_at)),
      kv("原始时间字段", item.published_raw || "（空）"),
      kv("批次", item.run_id
        ? h("button", { class: "link", type: "button", onclick: () => openRunById(item.run_id) }, item.run_id)
        : "—")
    ),
    h("div", { class: "card-actions" },
      h("button", { class: "secondary", type: "button", onclick: () => openRecord(item.id) }, "查看证据详情"),
      isSafeUrl(item.canonical_url)
        ? h("a", { class: "link", href: item.canonical_url, target: "_blank", rel: "noopener noreferrer" }, "规范化 URL")
        : null
    )
  );
}

function renderRecords() {
  const container = $("#records");
  clear(container);
  const data = state.data;
  const summary = $("#records-summary");
  if (!data) {
    summary.textContent = "尚未读取数据。";
    container.append(h("p", { class: "empty" }, "尚未读取数据。"));
    return;
  }
  const total = (data.stats && data.stats.record_count) || 0;
  const items = data.items || [];
  const cap = data.items_limit ?? 500;
  const rows = filteredItems();
  summary.textContent =
    `全表 ${total} 条（分母）；接口列表上限 ${cap} 条，本次载入 ${items.length} 条；当前筛选后 ${rows.length} 条。` +
    `（展示条数不等于总量）`;

  if (total === 0) {
    container.append(h("p", { class: "empty" }, "空库：数据库中还没有任何记录。请先在“来源与导入”添加来源，然后导入 JSON 或刷新来源。"));
    return;
  }
  if (rows.length === 0) {
    container.append(h("p", { class: "empty" }, "当前筛选无匹配：全表有记录，但没有符合来源 / 时间质量 / 关键词 / 情感条件的条目。"));
    return;
  }
  for (const item of rows) container.append(recordCard(item));
}

function renderRecordDetail(data) {
  const panel = $("#record-detail");
  const body = $("#record-detail-body");
  clear(body);
  const record = data.record || {};
  const snapshot = data.raw_snapshot;

  body.append(
    h("p", { class: "muted" }, "服务端在回读原文快照时已重新校验 raw_sha256（storage.digest）。以下为真实返回值，未做替代或缓存。"),
    h("dl", { class: "kv wide" },
      kv("记录ID", h("code", { text: record.id })),
      kv("规范记录ID", h("code", { text: record.canonical_id })),
      kv("来源", feedLabel(record.feed_id)),
      kv("来源内ID source_id", record.source_id || "（空）"),
      kv("标题", record.title || "（空）"),
      kv("摘要", record.summary || "（空）"),
      kv("URL", linkNode(record.url)),
      kv("规范化 URL", linkNode(record.canonical_url)),
      kv("时间质量", `${qualityLabel(record.time_quality)}（${record.time_quality}）`),
      kv("发布时间 published_at", fmtTime(record.published_at)),
      kv("原始时间字段 published_raw", record.published_raw || "（空）"),
      kv("抓取时间 fetched_at", fmtTime(record.fetched_at)),
      kv("快照哈希 raw_sha256", h("code", { text: record.raw_sha256 || "—" })),
      kv("内容哈希 content_sha256", h("code", { text: record.content_sha256 || "—" })),
      kv("快照路径 raw_path", h("code", { text: record.raw_path || "—" })),
      kv("去重规则", record.dedup_reason || "—"),
      kv("批次", record.run_id
        ? h("button", { class: "link", type: "button", onclick: () => { selectView("runs"); openRunById(record.run_id); } }, record.run_id)
        : "—"),
      kv("情感（未复核词典）", record.sentiment || "—")
    ),
    h("h3", { class: "detail-sub" }, "raw_snapshot（内容寻址 JSON）"),
    h("pre", { class: "json" }, JSON.stringify(snapshot, null, 2))
  );
  panel.hidden = false;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function openRecord(id) {
  const panel = $("#record-detail");
  const body = $("#record-detail-body");
  clear(body);
  panel.hidden = false;
  body.append(h("p", { class: "muted" }, `正在读取 /api/records/${id} …`));
  try {
    const data = await api(`/api/records/${encodeURIComponent(id)}`);
    renderRecordDetail(data);
  } catch (err) {
    clear(body);
    const status = err.status ? `HTTP ${err.status}` : "连接失败";
    body.append(
      h("h3", { class: "detail-sub" }, "证据详情读取失败"),
      h("p", { class: "error-text" }, `${status}：${err.message}`),
      err.status === 409
        ? h("p", { class: "muted" }, "服务端在回读原文快照时发现哈希不一致或文件缺失（409），此处原样显示，不做替代或降级。")
        : null
    );
  }
}

/* ---------- 渲染：报告模板（未复核样本） ---------- */

function buildReport() {
  const data = state.data;
  if (!data) return "尚未读取数据，无法生成模板。";
  const stats = data.stats || {};
  const items = data.items || [];
  const cap = data.items_limit ?? 500;
  const lines = [];

  lines.push("# 舆情观察台 · 未复核样本报告模板");
  lines.push("");
  lines.push(`生成时间（本地展示）：${fmtTime(new Date().toISOString())}`);
  lines.push("");
  lines.push("> 状态：**未复核（unreviewed）**。本模板由本地工具按键值直接拼装，未经人工复核，");
  lines.push("> 不构成结论、事件认定、预警、通知或准确率声明。自动事件聚类、可靠预警与生产模型当前均未接入。");
  lines.push("");

  lines.push("## 覆盖与分母");
  lines.push(`- coverage：${data.coverage || "—"}${data.coverage === "sample_only" ? "（仅样本，不代表总体）" : ""}`);
  lines.push(`- analysis_status：${data.analysis_status || "—"}${data.analysis_status === "unreviewed_dictionary" ? "（本地词典粗筛，未复核）" : ""}`);
  lines.push(`- schema_version：${data.schema_version || "—"}`);
  lines.push(`- 全表记录数（分母）：${stats.record_count ?? 0}`);
  lines.push(`- 规范记录数：${stats.canonical_count ?? 0}`);
  lines.push(`- 未知时间记录数：${stats.unknown_time_count ?? 0}，占比 ${fmtRatio(stats.unknown_time_ratio)}`);
  lines.push(`- 列表上限：${cap} 条；本次载入 ${items.length} 条（展示条数不是总量）`);
  lines.push("");

  lines.push("## 时间质量分布（全表）");
  const counts = stats.time_quality_counts || {};
  if (Object.keys(counts).length) {
    for (const key of Object.keys(counts).sort()) {
      lines.push(`- ${qualityLabel(key)}（${key}）：${counts[key]}`);
    }
  } else {
    lines.push("- 无记录");
  }
  lines.push("");

  lines.push("## 来源清单");
  const feeds = data.feeds || [];
  if (!feeds.length) {
    lines.push("- 无来源");
  } else {
    for (const feed of feeds) {
      const run = feed.last_run_id ? state.runsById[feed.last_run_id] : null;
      lines.push(
        `- #${feed.id} ${feed.name}｜条目数 ${feed.item_count ?? 0}｜最后成功 ${fmtTime(feed.last_success_at)}｜` +
        `最近批次 ${feed.last_run_id || "无"}${run ? "（" + statusLabel(run.status) + "）" : ""}`
      );
      lines.push(`  - URL：${feed.url}`);
    }
  }
  lines.push("");

  lines.push("## 样本清单（来源 | 时间质量 | 记录ID | 规范记录ID | 标题）");
  if (!items.length) {
    lines.push("- 无样本");
  } else {
    for (const item of items) {
      lines.push(
        `- [${item.feed_name ?? item.feed_id ?? "?"}] 时间质量=${qualityLabel(item.time_quality)}（${item.time_quality}）` +
        ` | 记录ID=${item.id} | 规范ID=${item.canonical_id} | ${item.title || "（无标题）"}`
      );
    }
    if ((stats.record_count ?? 0) > items.length) {
      lines.push(`- …全表另有约 ${(stats.record_count ?? 0) - items.length} 条未在列表上限内展示`);
    }
  }
  lines.push("");

  lines.push("## 未接入能力（不编造）");
  lines.push("- 事件聚类 / 事件 API：未接入");
  lines.push("- 预警 / 通知 / 推送：未接入");
  lines.push("- 模型调用与用量：未接入（标签为本地词典粗筛）");
  lines.push("- 批次取消：未接入");
  lines.push(`- 旧 items 待处理：${data.legacy_items_pending ?? 0} 条（原样保留，未迁移）`);
  lines.push("");
  lines.push("## 复核提示");
  lines.push("- 本模板未经过人工复核，接收前必须由人工逐条核对来源、时间与正文快照。");
  lines.push("- 情感标签与未知时间判定均为规则粗筛，不得作为定性结论或对外口径。");

  return lines.join("\n");
}

function generateReport() {
  if (!state.online) { show("当前服务状态未知，不能生成实时样本模板。", true); return; }
  $("#report-out").value = buildReport();
  show("已生成未复核样本模板（纯文本，未提交、未对外发送）。");
}

/* ---------- 渲染：运行状态 ---------- */

function renderRuns() {
  const body = $("#runs-body");
  clear(body);
  const runs = state.data ? state.data.runs || [] : [];
  if (!runs.length) {
    body.append(h("tr", null, h("td", { colspan: "10", class: "muted" }, "还没有采集批次。")));
    return;
  }
  for (const run of runs) {
    body.append(h("tr", null,
      h("td", null, h("button", { class: "link", type: "button", onclick: () => openRunById(run.id) }, run.id)),
      h("td", null, feedLabel(run.feed_id)),
      h("td", null, run.kind || "—"),
      h("td", null, h("span", { class: `status ${run.status}` }, statusLabel(run.status))),
      h("td", null, fmtTime(run.started_at)),
      h("td", null, fmtTime(run.finished_at)),
      h("td", { class: "num" }, String(run.input_count ?? 0)),
      h("td", { class: "num" }, String(run.added ?? 0)),
      h("td", { class: "num" }, String(run.duplicates ?? 0)),
      h("td", null, run.error
        ? h("span", { class: "error-text", title: run.error }, run.error.slice(0, 60))
        : "—")
    ));
  }
}

function renderRunDetail(run) {
  const panel = $("#run-detail");
  const body = $("#run-detail-body");
  clear(body);
  if (!run || !run.id) {
    body.append(h("p", { class: "error-text" }, "批次数据缺失。"));
    panel.hidden = false;
    return;
  }
  body.append(h("dl", { class: "kv wide" },
    kv("批次ID", h("code", { text: run.id })),
    kv("来源", feedLabel(run.feed_id)),
    kv("类型 kind", run.kind || "—"),
    kv("状态", h("span", { class: `status ${run.status}` }, `${statusLabel(run.status)}（${run.status}）`)),
    kv("开始", fmtTime(run.started_at)),
    kv("结束", fmtTime(run.finished_at)),
    kv("输入条数 input_count", String(run.input_count ?? 0)),
    kv("新增 added", String(run.added ?? 0)),
    kv("重复 duplicates", String(run.duplicates ?? 0)),
    kv("批次限额 limit_n", String(run.limit_n ?? "—")),
    kv("输入哈希 input_sha256", h("code", { text: run.input_sha256 || "—" })),
    kv("错误 error", run.error ? h("span", { class: "error-text" }, run.error) : "（无）")
  ));
  panel.hidden = false;
}

async function openRunById(id) {
  const panel = $("#run-detail");
  const body = $("#run-detail-body");
  selectView("runs");
  clear(body);
  panel.hidden = false;
  body.append(h("p", { class: "muted" }, `正在读取 /api/runs/${id} …`));
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    renderRunDetail(run);
  } catch (err) {
    clear(body);
    const status = err.status ? `HTTP ${err.status}` : "连接失败";
    body.append(h("p", { class: "error-text" }, `${status}：${err.message}`));
  }
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderRuntime(info) {
  const body = $("#runtime-body");
  clear(body);
  appendChild(body, [
    kv("状态", "已读取（GET /api/runtime）"),
    kv("PID", String(info.pid ?? "—")),
    kv("启动时间 started_at", fmtTime(info.started_at)),
    kv("入口 entry", h("code", { text: info.entry || "—" })),
    kv("storage 模块", h("code", { text: info.storage_module || "—" })),
    kv("数据库", h("code", { text: info.database || "—" })),
    kv("快照目录", h("code", { text: info.snapshot_root || "—" })),
    kv("manual_limit", String(info.manual_limit ?? "—")),
    kv("server.py sha256", h("code", { text: (info.loaded_sha256 && info.loaded_sha256["server.py"]) || "—" })),
    kv("storage.py sha256", h("code", { text: (info.loaded_sha256 && info.loaded_sha256["storage.py"]) || "—" }))
  ]);
}

/* ---------- 渲染：P1 议题与表达线索（真实证据页面） ---------- */

const P1_KIND_LABELS = {
  title_only: "仅标题线索",
  post_body: "帖主正文",
  source_summary: "明确来源摘要"
};
const P1_PRECISION_LABELS = { exact: "精确", approximate: "约数", unknown: "未知" };

function p1KindLabel(kind) { return P1_KIND_LABELS[kind] || kind || "未知类型"; }
function p1PrecisionLabel(precision) { return P1_PRECISION_LABELS[precision] || precision || "未知"; }

function p1StatusText(err) {
  if (err instanceof ApiError && err.kind === "offline") return "连接失败（无法连接本地服务）";
  return err && err.status ? `HTTP ${err.status}` : "读取失败";
}

function p1BodyNode(err) {
  // 原样显示真实响应体（JSON 解析成功则结构化，否则回落为纯文本消息）。
  if (err && err.payload && typeof err.payload === "object") {
    return h("pre", { class: "json" }, JSON.stringify(err.payload, null, 2));
  }
  return h("p", { class: "error-text" }, (err && err.message) || "（无响应体）");
}

function p1OtherCountsNode(other) {
  if (!other || !Object.keys(other).length) return h("span", { class: "muted" }, "（无）");
  const rows = [];
  for (const name of Object.keys(other)) {
    const count = other[name] || {};
    const counted = count.value !== null && count.value !== undefined;
    rows.push(h("div", { class: "p1-other" },
      `${name} = ${counted ? count.value : "null（未知）"}`,
      h("span", { class: "muted" }, `（${p1PrecisionLabel(count.precision)}；来源字段 ${count.source_field || "—"}；不计入评论数）`)));
  }
  return h("div", { class: "p1-others" }, rows);
}

function p1DetailPlaceholder(text) {
  const body = $("#p1-detail-body");
  clear(body);
  body.append(h("p", { class: "muted" }, text));
}

function p1Reset() {
  // 读取失败 / 离线时清空旧 P1 展示，绝不把旧缓存当作在线数据。
  state.p1 = null;
  const stats = $("#p1-stats");
  if (stats) stats.hidden = true;
  for (const id of ["p1-identity-total", "p1-group-count", "p1-topk-limit"]) {
    const el = $("#" + id);
    if (el) el.textContent = "—";
  }
  const groups = $("#p1-groups");
  if (groups) clear(groups);
  // 旧详情（含计数/hash/snapshot）不得随概览一起残留：统一清为“无当前详情”占位。
  // invalidateData() 之后会以离线文案再次覆盖本占位，此处不做额外分支。
  p1DetailPlaceholder("当前无 P1 证据详情：概览读取或导入失败后旧详情已清空，请重新点击“查看证据详情”读取本地 GET /api/p1/evidence/<id>。");
}

function p1EntryCard(entry, bucket) {
  const count = entry.comment_count || {};
  const counted = count.value !== null && count.value !== undefined;
  const heat = counted
    ? `当前热度（单点）：评论数 ${count.value}（${p1PrecisionLabel(count.precision)}）；等待更多可比观测，不计算增速。`
    : `评论数未知（${p1PrecisionLabel(count.precision)}）：未采得可核验评论数，不计入 0，也不参与排名；等待更多可比观测，不计算增速。`;
  return h("article", { class: `card p1-entry ${bucket}` },
    h("div", { class: "meta" },
      bucket === "top" ? h("span", { class: "badge pos" }, `#${entry.rank}`) : h("span", { class: "badge neu" }, "未排名"),
      h("span", { class: "chip" }, p1KindLabel(entry.evidence_kind)),
      h("span", { class: "chip" }, `来源 ${entry.source_name || "?"}`)
    ),
    h("h3", null, linkNode(entry.url, entry.title || "（无标题）")),
    entry.evidence_kind === "title_only"
      ? h("p", { class: "excerpt" }, "议题候选 / 证据不足：仅标题线索，无帖主正文或来源摘要；不生成事件梗概或表达线索，不推断作者性别、阵营或群体比例。")
      : null,
    h("p", { class: "p1-heat" }, heat),
    h("dl", { class: "kv" },
      kv("证据 ID", h("code", { text: entry.evidence_id })),
      kv("平台", `${entry.platform || "—"}${entry.platform_id ? " · " + entry.platform_id : ""}`),
      kv("评论计数 comment_count", h("span", null,
        counted ? String(count.value) : "null（未知）",
        h("span", { class: "muted" }, ` · ${p1PrecisionLabel(count.precision)} · 来源字段 ${count.source_field || "—"}`))),
      kv("其他计数（按原名独立，不冒充评论数）", p1OtherCountsNode(entry.other_counts)),
      kv("观测时间 observed_at", fmtTime(entry.observed_at)),
      kv("导入时间 imported_at（与观测时间分开）", fmtTime(entry.imported_at)),
      kv("快照哈希 snapshot_sha256", h("code", { title: entry.snapshot_sha256 || "", text: shortHash(entry.snapshot_sha256) })),
      kv("源响应哈希 source_response_sha256（输入声明）", h("code", { title: entry.source_response_sha256 || "", text: shortHash(entry.source_response_sha256) }))
    ),
    h("div", { class: "card-actions" },
      h("button", { class: "secondary", type: "button", onclick: () => openP1Detail(entry.evidence_id) }, "查看证据详情"),
      isSafeUrl(entry.canonical_url)
        ? h("a", { class: "link", href: entry.canonical_url, target: "_blank", rel: "noopener noreferrer" }, "规范化 URL（仅链接，不加载平台媒体）")
        : null
    )
  );
}

function p1GroupCard(group) {
  const card = h("article", { class: "p1-group" },
    h("div", { class: "p1-group-head" },
      h("h3", null, group.group_label || "（未命名分组）"),
      h("span", { class: "chip" }, `候选身份 ${group.identity_count ?? 0}`),
      h("span", { class: "chip" }, `精确计数 ${group.exact_count ?? 0}`)
    ),
    h("p", { class: "hint" }, "人工指定分组；相似标题不直接合并事件；排名仅按精确评论数降序，约数/未知计数单列。")
  );
  const top = group.top || [];
  if (top.length) {
    card.append(h("h4", { class: "detail-sub" }, `TopK 原帖（前 ${top.length} 条，按精确评论数降序）`));
    for (const entry of top) card.append(p1EntryCard(entry, "top"));
  } else {
    card.append(h("p", { class: "empty" }, "该分组没有可比精确评论数：证据不足，无 TopK 排名（不伪精确、不把未知当 0）。"));
  }
  const unranked = group.unranked || [];
  if (unranked.length) {
    card.append(h("h4", { class: "detail-sub" }, "未参与排名（评论数为约数或未知）"));
    for (const entry of unranked) card.append(p1EntryCard(entry, "unranked"));
  }
  return card;
}

function renderP1Overview(data) {
  state.p1 = data;
  const stats = $("#p1-stats");
  stats.hidden = false;
  $("#p1-identity-total").textContent = String(data.identity_total ?? 0);
  $("#p1-group-count").textContent = String(data.group_count ?? 0);
  $("#p1-topk-limit").textContent = `${data.top_k ?? "—"} / ${data.candidate_limit ?? "—"}`;
  const groups = $("#p1-groups");
  clear(groups);
  const status = $("#p1-status");
  status.classList.remove("error");
  if (data.empty_state || !(data.groups || []).length) {
    status.textContent = "本地 P1 证据库为空：候选身份 0。请展开下方“人工导入已取证证据”，由人工填入一条已取证记录后提交（本页面不预填、不自动导入样本、不抓取）。";
    groups.append(h("p", { class: "empty" }, "暂无候选证据：没有可展示的人工证据或 TopK 原帖。"));
    return;
  }
  status.textContent = `已读取本地 P1 概览：候选身份 ${data.identity_total}，人工分组 ${data.group_count}；排名仅按精确评论数降序（TopK=${data.top_k}）。`;
  for (const group of data.groups) groups.append(p1GroupCard(group));
}

function renderP1Detail(data) {
  const body = $("#p1-detail-body");
  clear(body);
  const ev = data.evidence || {};
  let other = {};
  try { other = ev.other_counts ? JSON.parse(ev.other_counts) : {}; } catch (err) { other = {}; }
  const counted = ev.comment_value !== null && ev.comment_value !== undefined;
  body.append(
    h("p", { class: "muted" }, "以下为 GET /api/p1/evidence/{id} 的真实返回，未做替代或缓存；原地址仅为可点击链接，不加载封面/头像/视频。"),
    h("dl", { class: "kv wide" },
      kv("证据 ID", h("code", { text: ev.id || "—" })),
      kv("平台", `${ev.platform || "—"}${ev.platform_id ? " · " + ev.platform_id : ""}`),
      kv("原地址 url", linkNode(ev.url)),
      kv("规范化 URL canonical_url", linkNode(ev.canonical_url)),
      kv("原标题 title", ev.title || "（空）"),
      kv("来源名称 source_name", ev.source_name || "—"),
      kv("证据类型 evidence_kind", `${p1KindLabel(ev.evidence_kind)}（${ev.evidence_kind || "—"}）`),
      kv("片段 content（帖主正文 / 明确来源摘要）", ev.content
        ? ev.content
        : h("span", { class: "muted" }, "（空：title_only 仅标题线索，无帖主正文/来源摘要 → 议题候选 / 证据不足，不生成事件梗概或表达线索）")),
      kv("content_source", ev.content_source || "—"),
      kv("片段定位 [fragment_start, fragment_end)", `${ev.fragment_start ?? "—"} – ${ev.fragment_end ?? "—"}`),
      kv("content_sha256", h("code", { text: ev.content_sha256 || "—" })),
      kv("content_raw_sha256", h("code", { text: ev.content_raw_sha256 || "—" })),
      kv("观测时间 observed_at", fmtTime(ev.observed_at)),
      kv("观测原始串 observed_raw", ev.observed_raw || "—"),
      kv("发布时间 published_at", fmtTime(ev.published_at)),
      kv("发布原始串 published_raw", ev.published_raw || "—"),
      kv("时间质量 time_quality", `${qualityLabel(ev.time_quality)}（${ev.time_quality || "—"}）`),
      kv("导入时间 imported_at（与观测时间分开）", fmtTime(ev.imported_at)),
      kv("评论计数 comment_count", h("span", null,
        counted ? String(ev.comment_value) : "null（未知）",
        h("span", { class: "muted" }, ` · ${p1PrecisionLabel(ev.comment_precision)} · 来源字段 ${ev.comment_source_field || "—"}`))),
      kv("其他计数 other_counts（按原名）", p1OtherCountsNode(other)),
      kv("分组 group_label", ev.group_label || "—"),
      kv("快照路径 snapshot_path", h("code", { text: ev.snapshot_path || "—" })),
      kv("快照哈希 snapshot_sha256", h("code", { text: ev.snapshot_sha256 || "—" })),
      kv("源响应哈希 source_response_sha256", h("code", { text: ev.source_response_sha256 || "—" }))
    ),
    h("p", { class: "hint" }, `源响应哈希（source_response_sha256）：输入声明，本服务未核源响应字节（source_response_verified=${String(data.source_response_verified)}）。`),
    h("p", { class: "hint" }, `快照哈希（snapshot_sha256）：本地内容寻址快照，详情回读已核验（snapshot_sha256_verified=${String(data.snapshot_sha256_verified)}）。`),
    h("h3", { class: "detail-sub" }, "snapshot（真实输入记录 JSON，不是 HTTP 原始响应）"),
    h("pre", { class: "json" }, JSON.stringify(data.snapshot, null, 2))
  );
}

async function loadP1() {
  const status = $("#p1-status");
  const topk = $("#p1-topk").value || "3";
  status.classList.remove("error");
  status.textContent = `正在读取 /api/p1/overview?top_k=${topk} …`;
  try {
    const data = await api(`/api/p1/overview?top_k=${encodeURIComponent(topk)}`);
    renderP1Overview(data);
  } catch (err) {
    p1Reset();
    status.classList.add("error");
    status.textContent = `P1 概览读取失败（${p1StatusText(err)}）：${(err && err.message) || ""}；旧 P1 展示已清空，未显示缓存。`;
    clear($("#p1-groups"));
    $("#p1-groups").append(h("p", { class: "empty" }, "无法读取本地 P1 概览，当前无可展示的实时证据。"), p1BodyNode(err));
  }
}

async function openP1Detail(evidenceId) {
  const body = $("#p1-detail-body");
  clear(body);
  body.append(h("p", { class: "muted" }, `正在读取 /api/p1/evidence/${evidenceId} …`));
  try {
    const data = await api(`/api/p1/evidence/${encodeURIComponent(evidenceId)}`);
    renderP1Detail(data);
  } catch (err) {
    clear(body);
    body.append(
      h("h3", { class: "detail-sub" }, "P1 证据详情读取失败"),
      h("p", { class: "error-text" }, `${p1StatusText(err)}：${(err && err.message) || ""}；旧详情已清空。`),
      p1BodyNode(err),
      err.status === 409
        ? h("p", { class: "muted" }, "服务端回读本地快照时发现哈希不一致或文件缺失（409），原样显示，不做替代或降级。")
        : null
    );
  }
}

async function submitP1Import() {
  const button = $("#p1-import-submit");
  const textarea = $("#p1-import-records");
  const raw = textarea.value.trim();
  if (!raw) {
    show("请先在文本框中粘贴人工已取证的 records 数组（本页面不预填、不自动导入）。", true);
    return;
  }
  let records;
  try {
    records = JSON.parse(raw);
  } catch (err) {
    show(`JSON 解析失败：${err.message}`, true);
    return;
  }
  if (!Array.isArray(records)) {
    show("内容必须是 records 数组（形如 [{...}]）。", true);
    return;
  }
  if (records.length > 3) {
    show(`P1 候选一次最多 3 条，且全库候选身份总数 ≤3；当前 ${records.length} 条，服务端将返回 422/409。`, true);
    return;
  }
  button.disabled = true;
  try {
    const result = await api("/api/p1/import", { method: "POST", body: { records } });
    show(`P1 导入完成：新增 ${result.added}，重复 ${result.duplicates}，候选身份总数 ${result.identity_total} / ${result.candidate_limit}。`);
    textarea.value = "";
    await loadP1();
  } catch (err) {
    p1Reset();
    const status = $("#p1-status");
    status.classList.add("error");
    status.textContent = `P1 导入失败（${p1StatusText(err)}）：${(err && err.message) || ""}；旧 P1 展示已清空，未显示缓存。`;
    clear($("#p1-groups"));
    $("#p1-groups").append(h("p", { class: "empty" }, "导入未成功，当前未展示 P1 证据。"), p1BodyNode(err));
    show(`P1 导入失败（${p1StatusText(err)}）：${(err && err.message) || ""}`, true);
  } finally {
    button.disabled = !state.online;
  }
}

/* ---------- 数据加载 ---------- */

function applyData(data) {
  state.data = data;
  state.manualLimit = data.manual_limit ?? null;
  state.feedsById = {};
  for (const feed of data.feeds || []) state.feedsById[feed.id] = feed;
  state.runsById = {};
  for (const run of data.runs || []) state.runsById[run.id] = run;
  renderRefreshLabel();
  renderStats();
  renderFeeds();
  renderSourceOptions();
  renderKeywords();
  renderRecords();
  renderRuns();
}

function setWriteAvailability(enabled) {
  for (const selector of ["#refresh", "#report-generate", "#report-copy", "#feed-form button[type=submit]",
                          "#keyword-form button[type=submit]", "#import-form button[type=submit]",
                          "#analysis-form button[type=submit]", "#p1-import-submit"]) $(selector).disabled = !enabled;
}

function invalidateData() {
  state.data = null;
  state.manualLimit = null;
  state.feedsById = {};
  state.runsById = {};
  state.online = false;
  renderRefreshLabel();
  setWriteAvailability(false);
  for (const id of ["stat-records", "stat-canonical", "stat-unknown", "stat-unknown-ratio", "stat-feeds", "stat-shown",
                    "meta-schema", "meta-coverage", "meta-analysis", "quality-dist", "legacy-note"]) $("#" + id).textContent = "未知（读取失败）";
  clear($("#feeds-body"));
  $("#feeds-body").append(h("tr", null, h("td", {colspan:"6"}, "服务离线或读取失败，来源状态未知。")));
  clear($("#runs-body"));
  $("#runs-body").append(h("tr", null, h("td", {colspan:"10"}, "服务离线或读取失败，批次状态未知。")));
  clear($("#records"));
  $("#records").append(h("p", {class:"empty"}, "服务离线或读取失败，当前无可展示的实时数据。"));
  $("#records-summary").textContent = "当前数据不可用，旧数据已隐藏。";
  $("#record-detail").hidden = true;
  $("#run-detail").hidden = true;
  clear($("#runtime-body"));
  $("#runtime-body").append(...kv("状态", "读取失败，旧运行时信息已隐藏。"));
  $("#report-out").value = "";
  clear($("#import-feed"));
  $("#analysis-body").textContent = "读取失败，旧分析队列已隐藏。";
  p1Reset();
  const p1Status = $("#p1-status");
  p1Status.classList.add("error");
  p1Status.textContent = "服务离线或读取失败，旧 P1 展示已清空，未显示缓存。";
  $("#p1-groups").append(h("p", { class: "empty" }, "无法读取本地 P1 概览，当前无可展示的实时证据。"));
  p1DetailPlaceholder("服务离线或读取失败，旧 P1 证据详情已清空。");
}

async function load({keepNotice = false} = {}) {
  setConnection("loading", "正在读取 /api/data …");
  try {
    const data = await api("/api/data");
    applyData(data);
    state.online = true;
    setWriteAvailability(true);
    state.loadedAt = new Date();
    setConnection("online", `在线 · 已读取真实 /api/data（${fmtTime(state.loadedAt.toISOString())}）`);
    if (!keepNotice) show("已从本地服务读取最新数据。");
  } catch (err) {
    invalidateData();
    if (err instanceof ApiError && err.kind === "offline") {
      setConnection("offline", "离线：无法连接本地服务，未显示缓存数据。");
    } else {
      const status = err.status ? `HTTP ${err.status}` : "错误";
      setConnection("error", `后端错误（${status}）：${err.message}`);
    }
    show(err.message, true);
  }
}

/* ---------- 事件绑定 ---------- */

for (const tab of $$(".tab")) {
  tab.addEventListener("click", () => selectView(tab.dataset.view));
  tab.addEventListener("keydown", (event) => {
    const tabs = $$(".tab");
    const index = tabs.indexOf(tab);
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const next = tabs[(index + step + tabs.length) % tabs.length];
      selectView(next.dataset.view);
      next.focus();
    }
  });
}

$("#feed-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget; // 异步前捕获，避免 await 后 currentTarget 失效
  const form = new FormData(formElement);
  try {
    await api("/api/feeds", { method: "POST", body: { name: form.get("name"), url: form.get("url") } });
    formElement.reset();
    show("来源已添加。");
    await load({keepNotice:true});
  } catch (err) {
    show(err.message, true);
  }
});

$("#keyword-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const value = formElement.elements.keywords.value;
  const keywords = value.split(/[，,\n\r]+/).map((item) => item.trim()).filter(Boolean);
  try {
    await api("/api/keywords", { method: "POST", body: { keywords } });
    show("关键词已保存到本地配置。");
    await load({keepNotice:true});
  } catch (err) {
    show(err.message, true);
  }
});

$("#import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const feedId = Number($("#import-feed").value);
  const text = formElement.elements.records.value.trim();
  if (!feedId) {
    show("请先选择导入目标来源。", true);
    return;
  }
  let records;
  try {
    records = JSON.parse(text);
  } catch (err) {
    show(`JSON 解析失败：${err.message}`, true);
    return;
  }
  if (!Array.isArray(records)) {
    show("记录必须是 JSON 数组。", true);
    return;
  }
  if (state.manualLimit != null && records.length > state.manualLimit) {
    show(`记录数 ${records.length} 超过 manual_limit ${state.manualLimit}，服务端将返回 422；请减少后再试。`, true);
    return;
  }
  const button = formElement.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const result = await api("/api/import", { method: "POST", body: { feed_id: feedId, records } });
    show(`导入完成：批次 ${result.id}，状态 ${statusLabel(result.status)}，输入 ${result.input_count}，新增 ${result.added}，重复 ${result.duplicates}。`);
    formElement.elements.records.value = "";
    await load({keepNotice:true});
  } catch (err) {
    if (err.status === 422 && err.payload && err.payload.run_id) {
      show(`导入被拒（HTTP 422）：${err.message}；批次 ${err.payload.run_id} 已记为 ${statusLabel(err.payload.status)}。`, true);
      await load({keepNotice:true});
    } else {
      const status = err.status ? `HTTP ${err.status}` : "连接失败";
      show(`导入失败（${status}）：${err.message}`, true);
    }
  } finally {
    button.disabled = !state.online;
  }
});

$("#refresh").addEventListener("click", async () => {
  const button = $("#refresh");
  const limit = state.manualLimit;
  button.disabled = true;
  show(limit == null ? "正在刷新来源…" : `按编号处理前 ${limit} 个来源，每源最多 ${limit} 条。`);
  try {
    const result = await api("/api/refresh", { method: "POST", body: {} });
    const results = result.results || [];
    if (!results.length) {
      show("没有配置来源，refresh 成功但无输入。");
    } else {
      const summary = results.map((item) => {
        if (item.status === "failed") return `${item.name}：失败（${item.error || "未知错误"}）`;
        return `${item.name}：${statusLabel(item.status)}，新增 ${item.added}，重复 ${item.duplicates}`;
      }).join("；");
      show(`刷新完成。${summary}`);
    }
    await load({keepNotice:true});
  } catch (err) {
    const status = err.status ? `HTTP ${err.status}` : "连接失败";
    show(`刷新失败（${status}）：${err.message}`, true);
  } finally {
    button.disabled = !state.online;
    renderRefreshLabel();
  }
});

for (const id of ["#filter-source", "#filter-quality", "#filter-sentiment"]) {
  $(id).addEventListener("change", () => renderRecords());
}
$("#filter-keyword").addEventListener("input", () => renderRecords());

$("#record-close").addEventListener("click", () => { $("#record-detail").hidden = true; });
$("#run-close").addEventListener("click", () => { $("#run-detail").hidden = true; });

$("#report-generate").addEventListener("click", generateReport);
$("#report-copy").addEventListener("click", async () => {
  const out = $("#report-out");
  if (!out.value) generateReport();
  try {
    await navigator.clipboard.writeText(out.value);
    show("报告模板已复制到剪贴板。");
  } catch (err) {
    out.focus();
    out.select();
    show("剪贴板不可用，已选中文本，请手动复制（Ctrl+C）。", true);
  }
});

$("#runtime-load").addEventListener("click", async () => {
  const button = $("#runtime-load");
  button.disabled = true;
  try {
    const info = await api("/api/runtime");
    renderRuntime(info);
    show("已读取运行时信息（仅回环诊断）。");
  } catch (err) {
    const status = err.status ? `HTTP ${err.status}` : "连接失败";
    show(`读取运行时失败（${status}）：${err.message}`, true);
  } finally {
    button.disabled = false;
  }
});

$("#reload-data").addEventListener("click", () => { load(); loadP1(); });

/* ---------- P1 事件绑定 ---------- */
$("#p1-topk").addEventListener("change", () => loadP1());
$("#p1-reload").addEventListener("click", () => loadP1());
$("#p1-import-submit").addEventListener("click", submitP1Import);
$("#p1-detail-close").addEventListener("click", () => p1DetailPlaceholder("已关闭证据详情。点击上方任一条目的“查看证据详情”可重新读取本地证据。"));

async function loadAnalysis() {
  const body = $("#analysis-body");
  clear(body);
  try {
    const data = await api("/api/analysis");
    body.append(h("p", null, `生产分析：${data.production_status}；适配器 ${data.adapter}；每批请求预算 ${data.request_budget}；费用未知。`));
    body.append(h("p", {class:"muted"}, data.warning));
    for (const item of data.jobs) {
      const card = h("article", {class:"card"}, h("h3", null, item.group_id), h("dl", {class:"kv"},
        kv("批次 ID", item.id), kv("输入哈希", item.input_hash), kv("状态", item.status),
        kv("错误", item.error || "无"), kv("已占用请求", `${item.budget.reserved} / ${item.budget.maximum}`),
        kv("证据统计", JSON.stringify(item.packet.statistics))),
        h("details", null, h("summary", null, "查看证据包与请求账本"), h("pre", {class:"json"}, JSON.stringify(item, null, 2))));
      if (item.status === "human_review") {
        const reason = h("input", {placeholder:"人工决定理由", "aria-label":"人工决定理由", maxlength:"2000"});
        card.append(reason);
        for (const [decision, label] of [["accept", "接收结论"], ["reject", "驳回结论"]]) {
          card.append(h("button", {type:"button", onclick: async () => {
            if (!reason.value.trim()) { show("请填写决定理由。", true); return; }
            try { await api("/api/analysis/decision", {method:"POST", body:{job_id:item.id,decision,reason:reason.value}}); await loadAnalysis(); }
            catch (err) { show(`人工决定失败（HTTP ${err.status}）：${err.message}`, true); }
          }}, label));
        }
      }
      body.append(card);
    }
    if (!data.jobs.length) body.append(h("p", null, "分析队列为空。"));
    body.append(h("h3", null, "质量与基线候选（未复核）"));
    for (const item of data.alerts) body.append(h("pre", {class:"json"}, JSON.stringify(item, null, 2)));
  } catch (err) {
    clear(body);
    body.append(h("p", {class:"error-text"}, `读取失败（HTTP ${err.status || 0}）：${err.message}；旧队列已隐藏。`));
  }
}
$("#analysis-load").addEventListener("click", loadAnalysis);
$("#analysis-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const record_ids = form.elements.record_ids.value.split(/\s+/).filter(Boolean);
  try {
    const result = await api("/api/analysis", {method:"POST", body:{group_id:form.elements.group_id.value,record_ids}});
    show(`证据包已保存，状态 ${result.status}；${result.error || "等待复核"}`);
    await loadAnalysis();
  } catch (err) { show(`创建失败（HTTP ${err.status || 0}）：${err.message}`, true); }
});
load();
loadP1();

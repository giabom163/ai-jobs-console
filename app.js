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
  loadedAt: null
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
  lines.push("> 不构成结论、事件认定、预警、通知或准确率声明。事件 / 预警 / 模型接口当前均未接入。");
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
                          "#keyword-form button[type=submit]", "#import-form button[type=submit]"]) $(selector).disabled = !enabled;
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

$("#reload-data").addEventListener("click", () => load());
load();

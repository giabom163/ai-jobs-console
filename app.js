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
  p1: null,                       // GET /api/p1/overview 真实返回
  p1Entries: [],                  // 已载入材料（top + unranked 展平，每条含 group_label/bucket）
  p1Content: {},                  // evidence_id -> {status, content}：用于本地关键词过滤的内容缓存
  filters: { source: "all", kind: "all", keyword: "" },
  selectedTopic: null,
  drawerTrigger: null,
  topK: "3",
  overviewToken: 0,               // 概览请求令牌：过期响应不得写状态
  drawerToken: 0                  // 抽屉请求令牌：过期响应不得写状态
};

// 概览始终按候选上限（3）载入全部候选，展示 K 由前端在选择范围内重算，避免后端 top_k 截断。
const P1_LOAD_TOP_K = 3;

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
  constructor(message, status, kind, rawText) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
    this.rawText = typeof rawText === "string" ? rawText : "";  // 原始响应体文本（可能非 JSON）
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
    throw new ApiError("无法连接本地服务（离线或服务未启动）", 0, "offline", "");
  }
  const raw = await response.text();
  let data = null;
  if (raw) {
    try { data = JSON.parse(raw); } catch (err) { data = null; }
  }
  if (!response.ok) {
    const message = data && typeof data.error === "string" && data.error ? data.error : `HTTP ${response.status}`;
    const error = new ApiError(message, response.status, "http", raw);
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

// 数据管理内部视图切换（与主视图独立，避免与旧管理结构互相干扰）
function selectAdminView(name) {
  for (const tab of $$(".admin-tab")) {
    const on = tab.dataset.admin === name;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
  }
  for (const view of $$(".admin-view")) {
    view.hidden = view.id !== "admin-" + name;
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
        ? h("button", { class: "link", type: "button", onclick: () => openRunById(record.run_id) }, record.run_id)
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
  selectView("admin");
  selectAdminView("runs");
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

/* ---------- 业务页：统一筛选、业务状态与侧抽屉（不渲染技术证据） ---------- */

const P1_KIND_LABELS = {
  title_only: "仅标题",
  post_body: "帖主正文",
  source_summary: "来源摘要"
};
const P1_PRECISION_LABELS = { exact: "精确", approximate: "约数", unknown: "未知" };
// 其他计数的业务词映射：只保留中文口径，未知字段在业务中省略，不回显原始字段名。
const P1_OTHER_LABELS = {
  view: "播放",
  play: "播放",
  like: "点赞",
  danmaku: "弹幕",
  coin: "投币",
  favorite: "收藏",
  share: "分享"
};
// 评论数来源统一映射为业务词，不输出平台原始字段名 source_field。
const P1_COMMENT_SOURCE_LABEL = "平台公开评论总数";

function p1KindLabel(kind) { return P1_KIND_LABELS[kind] || kind || "未知类型"; }
function p1PrecisionLabel(precision) { return P1_PRECISION_LABELS[precision] || precision || "未知"; }
function p1CommentSourceLabel(sourceField) { return sourceField ? P1_COMMENT_SOURCE_LABEL : "（未标注）"; }

function p1StatusText(err) {
  if (err instanceof ApiError && err.kind === "offline") return "连接失败（无法连接本地服务）";
  return err && err.status ? `HTTP ${err.status}` : "读取失败";
}

function p1BodyNode(err) {
  // 管理区查看用：优先显示原始响应文本（JSON 原文或非 JSON 原文），都保留真实 HTTP 状态单独显示。
  if (!err) return h("p", { class: "error-text" }, "（无响应体）");
  if (typeof err.rawText === "string" && err.rawText) {
    return h("pre", { class: "json" }, err.rawText);
  }
  if (err.payload && typeof err.payload === "object") {
    return h("pre", { class: "json" }, JSON.stringify(err.payload, null, 2));
  }
  return h("p", { class: "error-text" }, err.message || "（无响应体）");
}

function p1OtherCountsNode(other) {
  if (!other || !Object.keys(other).length) return h("span", { class: "muted" }, "（无）");
  const rows = [];
  for (const name of Object.keys(other)) {
    const label = P1_OTHER_LABELS[name];
    if (!label) continue;           // 未知字段：业务中省略，不原样回显
    const count = other[name] || {};
    const counted = count.value !== null && count.value !== undefined;
    rows.push(h("div", { class: "p1-other" },
      `${label}${counted ? " " + count.value : " 未知"}（${p1PrecisionLabel(count.precision)}；不计入评论数）`));
  }
  return rows.length ? h("div", { class: "p1-others" }, rows) : h("span", { class: "muted" }, "（无）");
}

function setBizStatus(kind, text) {
  // 业务页三处状态一致：导航切换不会掩盖真实读取状态。
  for (const id of ["overview-status", "topics-status", "search-status"]) {
    const el = $("#" + id);
    if (!el) continue;
    el.classList.remove("error", "ok");
    if (kind === "error") el.classList.add("error");
    else if (kind === "ok") el.classList.add("ok");
    el.textContent = text;
  }
}

function p1NewestObservedText(entries) {
  let best = null;
  for (const entry of entries) {
    if (!entry.observed_at) continue;
    const time = new Date(entry.observed_at).getTime();
    if (!Number.isNaN(time) && (best === null || time > best)) best = time;
  }
  return best === null ? "时间未知" : fmtTime(new Date(best).toISOString());
}

function p1ContentText(evidenceId) {
  const cached = state.p1Content[evidenceId];
  return cached && cached.status === "ok" ? (cached.content || "") : "";
}

// 精确评论数（视图内重算，不依赖后端 top_k 截断后的 rank）
function p1IsExact(entry) {
  const count = entry && entry.comment_count;
  return !!count && count.precision === "exact" && count.value !== null && count.value !== undefined;
}

function p1ExactSorted(entries) {
  return entries.filter(p1IsExact).slice()
    .sort((a, b) => (b.comment_count.value - a.comment_count.value)
      || String(a.evidence_id).localeCompare(String(b.evidence_id)));
}

function p1Unranked(entries) {
  return entries.filter((entry) => !p1IsExact(entry));
}

function p1FilteredEntries() {
  const filters = state.filters;
  return state.p1Entries.filter((entry) => {
    if (filters.source !== "all" && (entry.source_name || "") !== filters.source) return false;
    if (filters.kind !== "all" && (entry.evidence_kind || "") !== filters.kind) return false;
    if (filters.keyword) {
      const haystack = `${entry.title || ""} ${entry.source_name || ""} ${p1ContentText(entry.evidence_id)}`
        .toLocaleLowerCase("zh-CN");
      if (!haystack.includes(filters.keyword)) return false;
    }
    return true;
  });
}

function p1FilterSummary(filtered) {
  const filters = state.filters;
  const parts = [
    `来源=${filters.source === "all" ? "全部" : filters.source}`,
    `证据类型=${filters.kind === "all" ? "全部" : p1KindLabel(filters.kind)}`,
    `关键词=${filters.keyword ? filters.keyword : "（无）"}`
  ];
  return `统一筛选：${parts.join(" · ")}；本次已载入原帖 ${state.p1Entries.length} 条，筛选后 ${filtered.length} 条。`;
}

function p1ContentNote() {
  const ids = state.p1Entries.map((entry) => entry.evidence_id);
  const missing = ids.filter((id) => !(state.p1Content[id] && state.p1Content[id].status === "ok")).length;
  return missing
    ? `（${missing} 条材料的正文/来源摘要未载入，关键词只匹配标题与来源。）`
    : "（关键词覆盖标题、来源与已载入的正文/来源摘要。）";
}

function buildFilterBars() {
  const sources = [...new Set(state.p1Entries.map((entry) => entry.source_name).filter(Boolean))].sort();
  const kinds = [...new Set(state.p1Entries.map((entry) => entry.evidence_kind).filter(Boolean))];
  for (const id of ["overview-filters", "topics-filters", "search-filters"]) {
    const host = $("#" + id);
    if (!host) continue;
    clear(host);
    const sourceSelect = h("select", { class: "flt-source", "aria-label": "来源筛选" },
      h("option", { value: "all", text: "全部来源" }),
      sources.map((name) => h("option", { value: name, text: name })));
    const kindSelect = h("select", { class: "flt-kind", "aria-label": "证据类型筛选" },
      h("option", { value: "all", text: "全部证据类型" }),
      kinds.map((kind) => h("option", { value: kind, text: p1KindLabel(kind) })));
    const keyword = h("input", { type: "search", class: "flt-keyword", "aria-label": "关键词筛选",
      placeholder: "标题 / 内容 / 来源关键词", autocomplete: "off" });
    sourceSelect.value = state.filters.source;
    kindSelect.value = state.filters.kind;
    keyword.value = state.filters.keyword;
    sourceSelect.addEventListener("change", () => { state.filters.source = sourceSelect.value; renderBusinessViews(); });
    kindSelect.addEventListener("change", () => { state.filters.kind = kindSelect.value; renderBusinessViews(); });
    keyword.addEventListener("input", () => {
      state.filters.keyword = keyword.value.trim().toLocaleLowerCase("zh-CN");
      renderBusinessViews();
    });
    host.append(
      h("label", { class: "flt" }, h("span", { class: "flt-label" }, "来源"), sourceSelect),
      h("label", { class: "flt" }, h("span", { class: "flt-label" }, "证据类型"), kindSelect),
      h("label", { class: "flt flt-wide" }, h("span", { class: "flt-label" }, "关键词"), keyword)
    );
  }
}

function syncFilterBars() {
  for (const id of ["overview-filters", "topics-filters", "search-filters"]) {
    const host = $("#" + id);
    if (!host) continue;
    const sourceSelect = $(".flt-source", host);
    const kindSelect = $(".flt-kind", host);
    const keyword = $(".flt-keyword", host);
    if (sourceSelect) sourceSelect.value = state.filters.source;
    if (kindSelect) kindSelect.value = state.filters.kind;
    if (keyword) keyword.value = state.filters.keyword;
  }
}

function groupFilteredEntries(filtered) {
  const groups = new Map();
  for (const entry of filtered) {
    const label = entry.group_label || "（未命名分组）";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(entry);
  }
  return groups;
}

function p1HeatNode(label, entries) {
  const exact = p1ExactSorted(entries);
  const lead = exact[0] || entries[0];
  if (!lead) return h("p", { class: "muted" }, `「${label}」在当前筛选下没有可展示条目。`);
  const count = lead.comment_count || {};
  const counted = count.value !== null && count.value !== undefined;
  return h("div", { class: "heat-inner" },
    h("p", { class: "heat-value" }, counted ? String(count.value) : "未知"),
    h("p", { class: "heat-cap" }, counted
      ? `评论数（${p1PrecisionLabel(count.precision)}）· 该原帖历史累计评论数，单点观测，不代表近 7 天新增或增速。`
      : `评论数未知（${p1PrecisionLabel(count.precision)}）· 不计入 0，也不参与排名。`),
    h("dl", { class: "kv" },
      kv("所属议题", label),
      kv("来源", lead.source_name || "—"),
      kv("观测时间", fmtTime(lead.observed_at)),
      kv("评论数来源", p1CommentSourceLabel(count.source_field)))
  );
}

function p1BusinessCard(entry, rank) {
  const count = entry.comment_count || {};
  const counted = count.value !== null && count.value !== undefined;
  const heat = counted
    ? `评论数 ${count.value}（${p1PrecisionLabel(count.precision)}）· 该原帖历史累计，单点观测，不代表近期新增。`
    : `评论数未知（${p1PrecisionLabel(count.precision)}）· 不计入 0，也不参与排名。`;
  const showRank = typeof rank === "number";
  return h("article", { class: "card p1-entry" },
    h("div", { class: "meta" },
      showRank
        ? h("span", { class: "badge pos" }, `#${rank}`)
        : (counted ? null : h("span", { class: "badge neu" }, "未排名")),
      h("span", { class: "chip" }, p1KindLabel(entry.evidence_kind)),
      h("span", { class: "chip" }, `来源 ${entry.source_name || "?"}`)
    ),
    h("h3", null, linkNode(entry.url, entry.title || "（无标题）")),
    entry.evidence_kind === "title_only"
      ? h("p", { class: "excerpt" }, "仅标题线索，尚无正文或来源摘要：暂不生成梗概或表达判断，不推断性别、阵营或群体比例。")
      : null,
    h("p", { class: "p1-heat" }, heat),
    h("div", { class: "card-actions" },
      h("button", { class: "secondary", type: "button", onclick: (event) => openP1Drawer(entry.evidence_id, event.currentTarget) }, "查看详情"),
      isSafeUrl(entry.url)
        ? h("a", { class: "link", href: entry.url, target: "_blank", rel: "noopener noreferrer" }, "查看原帖")
        : null
    )
  );
}

function p1TopicSummary(label, entries) {
  const meta = ((state.p1.groups || []).find((group) => group.group_label === label)) || {};
  const exactCount = p1ExactSorted(entries).length;
  return h("div", null,
    h("h3", null, label),
    h("div", { class: "meta" },
      h("span", { class: "chip" }, `本组候选（全体）${meta.identity_count ?? entries.length}`),
      h("span", { class: "chip" }, `当前筛选 ${entries.length}`),
      h("span", { class: "chip" }, `精确计数 ${exactCount}`)),
    h("p", { class: "hint" }, "人工指定分组，相似标题不直接合并事件；排名仅按精确评论数降序，约数/未知计数单列。"),
    p1HeatNode(label, entries),
    h("p", { class: "boundary" }, "关联：暂无可证的议题关联——当前缺少带证据引用的关系查询，证据不足不生成关系图，也不推断群体立场。")
  );
}

function p1Reset() {
  // 读取失败 / 离线 / 导入失败时清空旧 P1 展示与旧详情，绝不把旧缓存当作在线数据。
  state.overviewToken++;          // 使在途概览/onrich 响应失效
  state.drawerToken++;            // 使在途抽屉响应失效
  state.p1 = null;
  state.p1Entries = [];
  state.p1Content = {};
  state.selectedTopic = null;
  const stats = $("#overview-stats");
  if (stats) stats.hidden = true;
  for (const id of ["ov-identity", "ov-groups", "ov-observed"]) {
    const el = $("#" + id);
    if (el) el.textContent = "—";
  }
  for (const id of ["overview-topics", "overview-heat", "overview-shortcuts", "topic-list",
                    "topic-summary", "topic-topk", "search-results",
                    "overview-filters", "topics-filters", "search-filters"]) {
    const el = $("#" + id);
    if (el) clear(el);
  }
  for (const id of ["overview-filter-note", "topics-filter-note", "search-filter-note"]) {
    const el = $("#" + id);
    if (el) el.textContent = "";
  }
  closeDrawer(true);
}

function renderBusinessEmpty() {
  const stats = $("#overview-stats");
  if (stats) stats.hidden = true;
  for (const id of ["ov-identity", "ov-groups", "ov-observed"]) {
    const el = $("#" + id);
    if (el) el.textContent = "—";
  }
  for (const id of ["overview-topics", "overview-heat", "overview-shortcuts", "topic-list",
                    "topic-summary", "topic-topk", "search-results"]) {
    const el = $("#" + id);
    if (el) clear(el);
  }
  const emptyText = "暂无可展示的原帖材料：请到「数据管理 → 人工证据」由人工粘贴已取证记录后提交（本页不预填、不自动导入、不抓取）。";
  for (const id of ["overview-topics", "topic-list", "search-results", "topic-topk"]) {
    const el = $("#" + id);
    if (el) el.append(h("p", { class: "empty" }, emptyText));
  }
  const summary = $("#topic-summary");
  if (summary) summary.append(h("p", { class: "muted" }, "无可展示议题概况。"));
  for (const id of ["overview-filter-note", "topics-filter-note", "search-filter-note"]) {
    const el = $("#" + id);
    if (el) el.textContent = "";
  }
}

function renderOverview(overview, filtered) {
  const stats = $("#overview-stats");
  if (stats) stats.hidden = false;
  $("#ov-identity").textContent = String(overview.identity_total ?? 0);
  $("#ov-groups").textContent = String(overview.group_count ?? 0);
  $("#ov-observed").textContent = p1NewestObservedText(state.p1Entries);

  const groups = groupFilteredEntries(filtered);
  const list = $("#overview-topics");
  clear(list);
  if (!groups.size) {
    list.append(h("p", { class: "empty" }, "当前筛选无匹配议题候选。"));
  } else {
    for (const [label, entries] of groups) {
      list.append(h("button", {
        class: "topic-row", type: "button",
        onclick: () => { state.selectedTopic = label; selectView("topics"); syncFilterBars(); renderBusinessViews(); }
      },
        h("span", { class: "topic-row-name" }, label),
        h("span", { class: "topic-row-meta" }, `当前筛选条目 ${entries.length} · 最近观测 ${p1NewestObservedText(entries)}`)));
    }
  }

  const heat = $("#overview-heat");
  clear(heat);
  const selectedLabel = state.selectedTopic && groups.has(state.selectedTopic)
    ? state.selectedTopic
    : (groups.size ? groups.keys().next().value : null);
  heat.append(selectedLabel
    ? p1HeatNode(selectedLabel, groups.get(selectedLabel))
    : h("p", { class: "muted" }, "当前筛选下无可展示热度。"));

  const shortcuts = $("#overview-shortcuts");
  clear(shortcuts);
  const exactAll = p1ExactSorted(filtered);
  const ranked = exactAll.slice(0, 3);
  const pool = ranked.length ? ranked.map((entry, index) => ({ entry, rank: index + 1 }))
    : filtered.slice(0, 3).map((entry) => ({ entry, rank: undefined }));
  if (!pool.length) shortcuts.append(h("p", { class: "empty" }, "当前筛选下没有原帖捷径。"));
  for (const item of pool) shortcuts.append(p1BusinessCard(item.entry, item.rank));

  $("#overview-filter-note").textContent = p1FilterSummary(filtered);
}

function renderTopics(filtered) {
  const groups = groupFilteredEntries(filtered);
  const list = $("#topic-list");
  clear(list);
  const topkHost = $("#topic-topk");
  const summary = $("#topic-summary");
  if (!groups.size) {
    list.append(h("p", { class: "empty" }, "当前筛选无匹配议题。"));
    clear(summary);
    summary.append(h("p", { class: "muted" }, "无可展示议题概况。"));
    clear(topkHost);
    topkHost.append(h("p", { class: "empty" }, "暂无可展示原帖。"));
    $("#topics-filter-note").textContent = p1FilterSummary(filtered);
    return;
  }
  const selected = groups.has(state.selectedTopic) ? state.selectedTopic : groups.keys().next().value;
  state.selectedTopic = selected;
  const backend = {};
  for (const group of state.p1.groups || []) backend[group.group_label] = group;
  for (const [label, entries] of groups) {
    const meta = backend[label] || {};
    const exactCount = p1ExactSorted(entries).length;
    list.append(h("button", {
      class: `topic-item ${label === selected ? "active" : ""}`, type: "button",
      "aria-pressed": label === selected ? "true" : "false",
      onclick: () => { state.selectedTopic = label; renderBusinessViews(); }
    },
      h("span", { class: "topic-item-name" }, label),
      h("span", { class: "topic-item-meta" }, `本组候选 ${meta.identity_count ?? entries.length} · 当前筛选 ${entries.length} · 精确计数 ${exactCount}`),
      h("span", { class: "topic-item-meta" }, `最近观测 ${p1NewestObservedText(entries)}`)));
  }

  clear(summary);
  summary.append(p1TopicSummary(selected, groups.get(selected) || []));

  clear(topkHost);
  const members = groups.get(selected) || [];
  const exactAll = p1ExactSorted(members);
  const k = Number(state.topK) || 3;
  const topEntries = exactAll.slice(0, k);
  const unranked = p1Unranked(members);
  if (!topEntries.length) {
    topkHost.append(h("p", { class: "empty" }, "该议题没有可比精确评论数：证据不足，无可比排名（不伪精确、不把未知当 0）。"));
  }
  topEntries.forEach((entry, index) => topkHost.append(p1BusinessCard(entry, index + 1)));
  if (unranked.length) {
    topkHost.append(h("h4", { class: "detail-sub" }, "未参与排名（评论数为约数或未知）"));
    for (const entry of unranked) topkHost.append(p1BusinessCard(entry));
  }
  $("#topics-filter-note").textContent = `${p1FilterSummary(filtered)}${p1ContentNote()}`;
}

function renderSearch(filtered) {
  const host = $("#search-results");
  clear(host);
  if (!filtered.length) {
    host.append(h("p", { class: "empty" }, state.p1Entries.length
      ? "当前筛选无匹配原帖（范围仅为本次已载入的原帖）。"
      : "暂无可检索的原帖材料（本次已载入候选 0 条）。"));
  } else {
    for (const entry of filtered) host.append(p1BusinessCard(entry));
  }
  $("#search-filter-note").textContent = `${p1FilterSummary(filtered)}${p1ContentNote()}`;
}

function applyP1(data) {
  state.p1 = data;
  const entries = [];
  for (const group of data.groups || []) {
    for (const entry of group.top || []) entries.push({ ...entry, group_label: group.group_label, bucket: "top" });
    for (const entry of group.unranked || []) entries.push({ ...entry, group_label: group.group_label, bucket: "unranked" });
  }
  state.p1Entries = entries;
  const labels = new Set((data.groups || []).map((group) => group.group_label));
  if (!state.selectedTopic || !labels.has(state.selectedTopic)) {
    state.selectedTopic = (data.groups && data.groups[0] && data.groups[0].group_label) || null;
  }
}

function renderBusinessViews() {
  const overview = state.p1;
  const filtered = p1FilteredEntries();
  if (!overview || overview.empty_state || !(overview.groups || []).length) {
    renderBusinessEmpty();
    return;
  }
  renderOverview(overview, filtered);
  renderTopics(filtered);
  renderSearch(filtered);
}

async function enrichP1Contents(token) {
  // 关键词过滤需要正文/来源摘要；overview 不含 content，这里按已载入材料（全库 ≤3）真实 GET 详情补齐。
  const ids = [...new Set(state.p1Entries.map((entry) => entry.evidence_id).filter(Boolean))];
  await Promise.all(ids.map(async (id) => {
    if (token !== state.overviewToken) return;   // 过期请求：不写状态
    if (state.p1Content[id] && state.p1Content[id].status === "ok") return;
    try {
      const data = await api(`/api/p1/evidence/${encodeURIComponent(id)}`);
      if (token !== state.overviewToken) return;
      const ev = data.evidence || {};
      state.p1Content[id] = { status: "ok", content: ev.content || "" };
    } catch (err) {
      if (token !== state.overviewToken) return;
      state.p1Content[id] = { status: "error", content: "", message: (err && err.message) || "" };
    }
  }));
}

/* ---------- 业务详情侧抽屉（不含 snapshot / 哈希 / 路径 / 字段名） ---------- */

function setBackgroundInert(inert) {
  for (const selector of ["#main", ".top", "#main-nav", "footer"]) {
    const el = $(selector);
    if (!el) continue;
    if (inert) el.setAttribute("inert", "");
    else el.removeAttribute("inert");
  }
}

function showDrawer(open) {
  const drawer = $("#drawer");
  if (!drawer) return;
  drawer.hidden = !open;
  drawer.setAttribute("aria-hidden", open ? "false" : "true");
  setBackgroundInert(open);          // 隔离背景：打开时背景不可聚焦/不可交互
}

function closeDrawer(clearBody = false) {
  state.drawerToken++;               // 关闭使在途抽屉请求失效
  if (clearBody) {
    const body = $("#drawer-body");
    if (body) clear(body);
  }
  showDrawer(false);
  const trigger = state.drawerTrigger;
  state.drawerTrigger = null;
  if (trigger && typeof trigger.focus === "function" && document.contains(trigger)) trigger.focus();
}

function renderP1Drawer(data) {
  const body = $("#drawer-body");
  clear(body);
  const ev = data.evidence || {};
  let other = {};
  try { other = ev.other_counts ? JSON.parse(ev.other_counts) : {}; } catch (err) { other = {}; }
  const count = { value: ev.comment_value, precision: ev.comment_precision, source_field: ev.comment_source_field };
  const counted = count.value !== null && count.value !== undefined;
  const contentText = ev.content || "";
  const contentNode = contentText
    ? h("p", { class: "drawer-content" }, contentText)
    : h("p", { class: "muted" }, ev.evidence_kind === "title_only"
        ? "仅标题线索：尚无帖主正文或来源摘要，暂不生成梗概或表达判断。"
        : "（无正文或来源摘要内容）");
  body.append(
    h("p", { class: "drawer-title-text" }, ev.title || "（无标题）"),
    h("dl", { class: "kv wide" },
      kv("来源", ev.source_name || "—"),
      kv("原标题", ev.title || "（空）"),
      kv("原地址", linkNode(ev.url)),
      kv("证据类型", p1KindLabel(ev.evidence_kind)),
      kv("正文 / 来源摘要", contentNode),
      kv("观测时间", fmtTime(ev.observed_at)),
      kv("发布时间", ev.published_at ? `${fmtTime(ev.published_at)}（${qualityLabel(ev.time_quality)}）` : "（未提供）"),
      kv("评论数（累计口径）", counted
        ? `${count.value}（${p1PrecisionLabel(count.precision)}）；该原帖历史累计评论数，单点观测，不代表近 7 天新增或增速。`
        : `未知（${p1PrecisionLabel(count.precision)}）：未采得可核验评论数，不计入 0。`),
      kv("评论数来源", p1CommentSourceLabel(count.source_field)),
      kv("其他计数（不计入评论数）", p1OtherCountsNode(other))
    ),
    ev.evidence_kind === "title_only"
      ? h("p", { class: "hint" }, "证据不足：仅有标题线索，属议题候选，不作事件承认或表达判断。")
      : null
  );
}

async function openP1Drawer(evidenceId, trigger) {
  const token = ++state.drawerToken;
  const body = $("#drawer-body");
  const title = $("#drawer-title");
  state.drawerTrigger = trigger || document.activeElement;
  if (title) title.textContent = "原帖详情";
  clear(body);
  body.append(h("p", { class: "muted" }, "正在读取原帖详情…"));
  showDrawer(true);
  const closeButton = $("#drawer-close");
  if (closeButton) closeButton.focus();
  try {
    const data = await api(`/api/p1/evidence/${encodeURIComponent(evidenceId)}`);
    if (token !== state.drawerToken) return;   // 过期响应：不覆写新状态
    renderP1Drawer(data);
  } catch (err) {
    if (token !== state.drawerToken) return;
    clear(body);
    body.append(
      h("h3", { class: "detail-sub" }, "原帖详情读取失败"),
      h("p", { class: "error-text" }, `${p1StatusText(err)}：${(err && err.message) || ""}`),
      h("p", { class: "muted" }, "旧详情已清空，未把旧数据当作成功结果。完整错误响应体可在「数据管理 → 人工证据」查看。")
    );
  }
}

async function loadP1() {
  const token = ++state.overviewToken;
  setBizStatus("loading", "正在读取本地数据…");
  try {
    // 始终按候选上限载入全部候选（top_k=3），展示用 K 在前端过滤后重算，避免后端截断候选。
    const data = await api(`/api/p1/overview?top_k=${P1_LOAD_TOP_K}`);
    if (token !== state.overviewToken) return;
    applyP1(data);
    buildFilterBars();
    renderBusinessViews();
    await enrichP1Contents(token);
    if (token !== state.overviewToken) return;
    renderBusinessViews();
    setBizStatus("ok", `已读取本地数据：已载入原帖 ${data.identity_total ?? 0} 条，人工分组 ${data.group_count ?? 0} 组。` +
      `这是单点当前值，不是增速、曲线或观察清单。`);
  } catch (err) {
    if (token !== state.overviewToken) return;
    p1Reset();
    setBizStatus("error", `数据读取失败（${p1StatusText(err)}）：${(err && err.message) || ""}；旧材料已清空，未显示缓存。`);
  }
}

function setAdminStatus(kind, text, bodyNode) {
  const host = $("#p1-admin-status");
  if (!host) return;
  clear(host);
  host.classList.remove("error", "ok");
  if (kind === "error") host.classList.add("error");
  else if (kind === "ok") host.classList.add("ok");
  host.append(h("p", { class: kind === "error" ? "error-text" : "muted" }, text));
  if (bodyNode) host.append(bodyNode);
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
    show(`候选记录一次最多 3 条，且全库候选总数 ≤3；当前 ${records.length} 条，服务端将返回 422/409。`, true);
    return;
  }
  button.disabled = true;
  try {
    const result = await api("/api/p1/import", { method: "POST", body: { records } });
    show(`导入完成：新增 ${result.added}，重复 ${result.duplicates}，候选总数 ${result.identity_total} / ${result.candidate_limit}。`);
    textarea.value = "";
    setAdminStatus("ok", `导入完成：新增 ${result.added}，重复 ${result.duplicates}，候选总数 ${result.identity_total} / ${result.candidate_limit}。`, null);
    await loadP1();
  } catch (err) {
    // 导入失败（含 422）：清空旧材料与旧详情，绝不把旧数据当成功。
    p1Reset();
    setBizStatus("error", `导入失败（${p1StatusText(err)}）：${(err && err.message) || ""}；旧材料与详情已清空，未显示缓存。`);
    show(`导入失败（${p1StatusText(err)}）：${(err && err.message) || ""}`, true);
    // 工程错误证据（真实 HTTP 状态 + 原始响应体）只写数据管理「人工证据」状态容器。
    setAdminStatus("error", `导入失败（${p1StatusText(err)}）：${(err && err.message) || ""}`, p1BodyNode(err));
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
  setBizStatus("error", "服务离线或读取失败，旧材料与详情已清空，未显示缓存。");
}

async function load({keepNotice = false} = {}) {
  setConnection("loading", "正在读取本地数据…");
  try {
    const data = await api("/api/data");
    applyData(data);
    state.online = true;
    setWriteAvailability(true);
    state.loadedAt = new Date();
    setConnection("online", `在线 · 已读取本地数据（${fmtTime(state.loadedAt.toISOString())}）`);
    if (!keepNotice) show("已从本地服务读取最新数据。");
  } catch (err) {
    invalidateData();
    if (err instanceof ApiError && err.kind === "offline") {
      setConnection("offline", "离线：无法连接本地服务，未显示缓存数据。");
    } else {
      const status = err.status ? `HTTP ${err.status}` : "错误";
      setConnection("error", `读取失败（${status}）：${err.message}`);
    }
    show(err.message, true);
  }
}

/* ---------- 事件绑定 ---------- */

for (const tab of $$(".tab")) {
  tab.addEventListener("click", () => { selectView(tab.dataset.view); syncFilterBars(); });
  tab.addEventListener("keydown", (event) => {
    const tabs = $$(".tab");
    const index = tabs.indexOf(tab);
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const next = tabs[(index + step + tabs.length) % tabs.length];
      selectView(next.dataset.view);
      syncFilterBars();
      next.focus();
    }
  });
}

for (const tab of $$(".admin-tab")) {
  tab.addEventListener("click", () => selectAdminView(tab.dataset.admin));
  tab.addEventListener("keydown", (event) => {
    const tabs = $$(".admin-tab");
    const index = tabs.indexOf(tab);
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const next = tabs[(index + step + tabs.length) % tabs.length];
      selectAdminView(next.dataset.admin);
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
for (const id of ["#overview-reload", "#topics-reload", "#search-reload"]) {
  $(id).addEventListener("click", () => { load(); loadP1(); });
}

/* ---------- 业务导航与 P1 事件绑定 ---------- */

$("#topics-topk").addEventListener("change", () => {
  state.topK = $("#topics-topk").value || "3";
  // 展示用 K 只影响视图内重算与显示，不重新请求、不影响已载入候选全集。
  renderBusinessViews();
});
$("#p1-import-submit").addEventListener("click", submitP1Import);
$("#topics-goto-report").addEventListener("click", () => { selectView("admin"); selectAdminView("report"); });

// 详情侧抽屉：关闭按钮 / 背景点击 / ESC，关闭后焦点回到触发按钮；打开时圈定 Tab 焦点。
$("#drawer-close").addEventListener("click", () => closeDrawer(true));
$("#drawer-backdrop").addEventListener("click", () => closeDrawer(true));

const FOCUSABLE_SELECTOR = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

document.addEventListener("keydown", (event) => {
  const drawer = $("#drawer");
  if (!drawer || drawer.hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeDrawer(true);
    return;
  }
  if (event.key !== "Tab") return;
  const panel = $(".drawer-panel", drawer);
  if (!panel) return;
  const items = $$(FOCUSABLE_SELECTOR, panel).filter((el) => el.offsetParent !== null || el === document.activeElement);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (!panel.contains(document.activeElement)) {
    event.preventDefault();
    first.focus();
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

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

/* 总览图表：只读 /api/overview-stats，无第三方库，无网络外链。 */
(function () {
  'use strict';

  var COLORS = ['#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#84cc16', '#f97316'];
  var host = document.getElementById('overview-charts');
  if (!host) return;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function card(title, note) {
    var box = el('div', 'chart-card');
    var head = el('div', 'chart-head');
    head.appendChild(el('h3', null, title));
    if (note) head.appendChild(el('p', 'chart-note', note));
    box.appendChild(head);
    var body = el('div', 'chart-body');
    box.appendChild(body);
    return { box: box, body: body };
  }

  function fmtTime(v) {
    if (!v) return '—';
    return String(v).replace('T', ' ').slice(0, 19);
  }

  function pct(v) {
    return (v === null || v === undefined) ? '—' : (v * 100).toFixed(1) + '%';
  }

  function kpiRow(kpi) {
    var wrap = el('div', 'kpi-row');
    var items = [
      ['记录数', kpi.records],
      ['去重条目', kpi.canonical],
      ['来源', kpi.feeds],
      ['今日新增', kpi.today_added === undefined ? '—' : kpi.today_added],
      ['负面舆情（全量）', kpi.negative === undefined ? '—' : kpi.negative],
      ['未处理预警（全量）', kpi.open_alerts === undefined ? '—' : kpi.open_alerts],
      ['时间缺失', pct(kpi.unknown_time_ratio)],
      ['情感待判', pct(kpi.sentiment_pending_ratio)],
      ['最近成功采集', fmtTime(kpi.last_success_at)]
    ];
    items.forEach(function (it) {
      var box = el('div', 'kpi');
      box.appendChild(el('span', 'kpi-label', it[0]));
      box.appendChild(el('strong', 'kpi-value', String(it[1])));
      wrap.appendChild(box);
    });
    return wrap;
  }

  function trendChart(rows, body) {
    if (!rows.length) { body.appendChild(el('p', 'chart-empty', '暂无数据')); return; }
    var W = 520, H = 180, P = 28;
    var max = Math.max.apply(null, rows.map(function (r) { return r.records; }).concat([1]));
    var step = rows.length > 1 ? (W - P * 2) / (rows.length - 1) : 0;
    var pts = rows.map(function (r, i) {
      var x = P + step * i;
      var y = H - P - (r.records / max) * (H - P * 2);
      return [x, y, r];
    });
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('class', 'chart-svg');
    var area = 'M' + pts[0][0] + ',' + (H - P) + ' ' +
      pts.map(function (p) { return 'L' + p[0] + ',' + p[1]; }).join(' ') +
      ' L' + pts[pts.length - 1][0] + ',' + (H - P) + ' Z';
    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', area);
    path.setAttribute('fill', 'rgba(59,130,246,.16)');
    svg.appendChild(path);
    var line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    line.setAttribute('points', pts.map(function (p) { return p[0] + ',' + p[1]; }).join(' '));
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', '#3b82f6');
    line.setAttribute('stroke-width', '2');
    svg.appendChild(line);
    pts.forEach(function (p) {
      var c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('cx', p[0]); c.setAttribute('cy', p[1]); c.setAttribute('r', '3');
      c.setAttribute('fill', '#3b82f6');
      var t = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      t.textContent = p[2].day + '：' + p[2].records + ' 条（去重 ' + p[2].canonical + '）';
      c.appendChild(t);
      svg.appendChild(c);
    });
    body.appendChild(svg);
    var axis = el('div', 'chart-axis');
    axis.appendChild(el('span', null, rows[0].day));
    axis.appendChild(el('span', null, '峰值 ' + max + ' 条/日'));
    axis.appendChild(el('span', null, rows[rows.length - 1].day));
    body.appendChild(axis);
  }

  function donut(rows, body) {
    var total = rows.reduce(function (s, r) { return s + r.value; }, 0);
    if (!total) { body.appendChild(el('p', 'chart-empty', '暂无数据')); return; }
    var W = 160, R = 60, C = 2 * Math.PI * R, offset = 0;
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 160 160');
    svg.setAttribute('class', 'donut-svg');
    rows.forEach(function (r, i) {
      var len = (r.value / total) * C;
      var seg = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      seg.setAttribute('cx', '80'); seg.setAttribute('cy', '80'); seg.setAttribute('r', String(R));
      seg.setAttribute('fill', 'none');
      seg.setAttribute('stroke', COLORS[i % COLORS.length]);
      seg.setAttribute('stroke-width', '22');
      seg.setAttribute('stroke-dasharray', len + ' ' + (C - len));
      seg.setAttribute('stroke-dashoffset', String(-offset));
      seg.setAttribute('transform', 'rotate(-90 80 80)');
      var t = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      t.textContent = r.label + '：' + r.value;
      seg.appendChild(t);
      svg.appendChild(seg);
      offset += len;
    });
    var wrap = el('div', 'donut-wrap');
    wrap.appendChild(svg);
    var legend = el('ul', 'donut-legend');
    rows.forEach(function (r, i) {
      var li = el('li');
      var dot = el('span', 'dot');
      dot.style.background = COLORS[i % COLORS.length];
      li.appendChild(dot);
      li.appendChild(el('span', null, r.label + ' · ' + r.value + '（' + Math.round(r.value / total * 100) + '%）'));
      legend.appendChild(li);
    });
    wrap.appendChild(legend);
    body.appendChild(wrap);
  }

  function bars(rows, body, unit) {
    if (!rows.length) { body.appendChild(el('p', 'chart-empty', '暂无数据')); return; }
    var max = Math.max.apply(null, rows.map(function (r) { return r.value; }));
    rows.forEach(function (r) {
      var row = el('div', 'bar-row');
      row.appendChild(el('span', 'bar-label', r.label));
      var track = el('div', 'bar-track');
      var fill = el('div', 'bar-fill');
      fill.style.width = (max ? (r.value / max * 100) : 0) + '%';
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('span', 'bar-value', String(r.value) + (unit || '')));
      body.appendChild(row);
    });
  }

  function render(stats) {
    host.textContent = '';
    host.appendChild(kpiRow(stats.kpi));
    var grid = el('div', 'chart-grid');
    var a = card('每日采集趋势', '口径：published_at ?: fetched_at，窗口 ' + (stats.window_days ? stats.window_days + ' 天' : '全量'));
    trendChart(stats.trend, a.body);
    var b = card('情感分布', '规则情感判定（lexicon-zh-v1）；中性含“无情感词”记录');
    donut(stats.sentiment, b.body);
    var c = card('平台分布', '按采集源名称聚合');
    bars(stats.platforms, c.body);
    var d = card('条目 TOP10', '按 canonical_id 去重分组，非语义聚类');
    bars(stats.top_groups, d.body);
    [a, b, c, d].forEach(function (x) { grid.appendChild(x.box); });
    host.appendChild(grid);
    var foot = el('p', 'chart-foot',
      '生成于 ' + stats.generated_at + ' · 覆盖口径 ' + stats.coverage +
      ' · ' + (stats.notes || []).join('；'));
    host.appendChild(foot);
    host.hidden = false;
  }

  function load(days) {
    fetch('/api/overview-stats?window=' + (days || '14d'), { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (e) {
        host.textContent = '';
        host.appendChild(el('p', 'chart-empty', '总览聚合加载失败：' + e.message));
        host.hidden = false;
      });
  }

  window.addEventListener('overview:refresh', function () {
    load(host.getAttribute('data-window') || '14d');
  });

  load(host.getAttribute('data-window') || '14d');
})();

/* 话题分析：KPI + 话题列表 + 趋势对比 + 关联网络（数据来自 /api/topic-analysis）。 */
(function () {
  'use strict';
  var kpiHost = document.getElementById('topic-kpis');
  if (!kpiHost) return;
  var listHost = document.getElementById('topic-list');
  var trendHost = document.getElementById('topic-trend');
  var netHost = document.getElementById('topic-network');
  var windowSelect = document.getElementById('topic-window');
  var refreshBtn = document.getElementById('topic-refresh');
  var COLORS = ['#1f8ef1', '#f59e0b', '#22c55e', '#8b5cf6', '#ef4444', '#06b6d4'];
  var current = '7';

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function svg(tag) { return document.createElementNS('http://www.w3.org/2000/svg', tag); }

  function kpis(data) {
    kpiHost.textContent = '';
    var items = [
      ['活跃话题', data.kpi.topics, 'blue'],
      ['聚类覆盖度', data.kpi.coverage + '%', 'green'],
      ['上升趋势话题', data.kpi.rising, 'orange']
    ];
    items.forEach(function (it) {
      var card = el('div', 'kpi-card ' + it[2]);
      var num = el('div', 'num', String(it[1]));
      card.appendChild(num);
      card.appendChild(el('div', 'label', it[0]));
      kpiHost.appendChild(card);
    });
  }

  function topicList(data) {
    listHost.textContent = '';
    var rows = data.topics.slice();
    rows.push(data.other);
    rows.forEach(function (t, i) {
      var row = el('div', 'topic-row');
      var left = el('div');
      left.appendChild(el('div', 'topic-name', t.name || '其他话题'));
      var kw = (t.keywords || []).length ? t.keywords.join('、') : '未匹配到来源';
      left.appendChild(el('div', 'topic-kw', '来源：' + kw));
      row.appendChild(left);
      var right = el('div', 'topic-stat');
      var trend = Number(t.trend || 0);
      var up = trend > 0 ? '↑ ' + trend.toFixed(1) + '%' : (trend < 0 ? '↓ ' + Math.abs(trend).toFixed(1) + '%' : '↑ 0%');
      var badge = el('span', up.indexOf('↓') === 0 ? 'trend down' : 'trend', up);
      right.appendChild(badge);
      right.appendChild(el('div', 'topic-count', (t.count || 0) + '条'));
      row.appendChild(right);
      row.style.borderLeftColor = COLORS[i % COLORS.length];
      listHost.appendChild(row);
    });
  }

  function trendChart(data) {
    trendHost.textContent = '';
    var days = data.days || [];
    if (!days.length) { trendHost.appendChild(el('p', 'chart-empty', '暂无数据')); return; }
    var W = 720, H = 300, P = 42;
    var series = data.topics.map(function (t) { return { name: t.name, values: t.series, total: t.count, color: COLORS[t.index % COLORS.length] }; });
    var max = 1;
    series.forEach(function (s) { s.values.forEach(function (v) { if (v > max) max = v; }); });

    var root = svg('svg');
    root.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    root.setAttribute('class', 'chart-svg');

    for (var g = 0; g <= 4; g += 1) {
      var y = P + (H - P * 2) * g / 4;
      var line = svg('line');
      line.setAttribute('x1', P); line.setAttribute('x2', W - P);
      line.setAttribute('y1', y); line.setAttribute('y2', y);
      line.setAttribute('stroke', '#eef3f8');
      root.appendChild(line);
      var label = svg('text');
      label.setAttribute('x', P - 8); label.setAttribute('y', y + 4);
      label.setAttribute('text-anchor', 'end');
      label.setAttribute('fill', '#9aa7b6'); label.setAttribute('font-size', '11');
      label.textContent = String(Math.round(max * (4 - g) / 4));
      root.appendChild(label);
    }

    var step = days.length > 1 ? (W - P * 2) / (days.length - 1) : 0;
    series.forEach(function (s) {
      var pts = s.values.map(function (v, i) {
        return [P + step * i, H - P - (v / max) * (H - P * 2)];
      });
      var poly = svg('polyline');
      poly.setAttribute('points', pts.map(function (p) { return p[0] + ',' + p[1]; }).join(' '));
      poly.setAttribute('fill', 'none');
      poly.setAttribute('stroke', s.color);
      poly.setAttribute('stroke-width', '2');
      root.appendChild(poly);
      pts.forEach(function (p, i) {
        var c = svg('circle');
        c.setAttribute('cx', p[0]); c.setAttribute('cy', p[1]); c.setAttribute('r', '3');
        c.setAttribute('fill', s.color);
        var title = svg('title');
        title.textContent = s.name + '：' + days[i] + ' ' + s.values[i] + ' 条';
        c.appendChild(title);
        root.appendChild(c);
      });
    });
    trendHost.appendChild(root);

    var axis = el('div', 'chart-axis');
    axis.appendChild(el('span', null, days[0]));
    axis.appendChild(el('span', null, days[days.length - 1]));
    trendHost.appendChild(axis);

    var legend = el('ul', 'donut-legend legend-inline');
    series.forEach(function (s) {
      var li = el('li');
      var dot = el('span', 'dot');
      dot.style.background = s.color;
      li.appendChild(dot);
      li.appendChild(el('span', null, s.name + '（共 ' + s.total + ' 条）'));
      legend.appendChild(li);
    });
    trendHost.appendChild(legend);
  }

  function network(data) {
    netHost.textContent = '';
    if (!data.topics.length) { netHost.appendChild(el('p', 'chart-empty', '暂无话题')); return; }
    var W = 760, H = 420, cx = W / 2, cy = H / 2, R = 150;
    var pos = data.topics.map(function (t, i) {
      var angle = (Math.PI * 2 * i) / data.topics.length - Math.PI / 2;
      return { x: cx + Math.cos(angle) * R, y: cy + Math.sin(angle) * R };
    });
    var root = svg('svg');
    root.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    root.setAttribute('class', 'chart-svg');
    data.edges.forEach(function (e) {
      var line = svg('line');
      line.setAttribute('x1', pos[e.a].x); line.setAttribute('y1', pos[e.a].y);
      line.setAttribute('x2', pos[e.b].x); line.setAttribute('y2', pos[e.b].y);
      line.setAttribute('stroke', '#c9dff3');
      line.setAttribute('stroke-width', String(1 + e.weight));
      root.appendChild(line);
    });
    var maxCount = Math.max.apply(null, data.topics.map(function (t) { return t.count; }).concat([1]));
    data.topics.forEach(function (t, i) {
      var r = 12 + 24 * (t.count / maxCount);
      var circle = svg('circle');
      circle.setAttribute('cx', pos[i].x); circle.setAttribute('cy', pos[i].y); circle.setAttribute('r', r);
      circle.setAttribute('fill', COLORS[i % COLORS.length]);
      circle.setAttribute('opacity', '0.85');
      var title = svg('title');
      title.textContent = t.full + '：' + t.count + ' 条';
      circle.appendChild(title);
      root.appendChild(circle);
      var text = svg('text');
      text.setAttribute('x', pos[i].x);
      text.setAttribute('y', pos[i].y + r + 18);
      text.setAttribute('text-anchor', 'middle');
      text.setAttribute('font-size', '11');
      text.setAttribute('fill', '#41505f');
      text.textContent = t.name;
      root.appendChild(text);
    });
    netHost.appendChild(root);
  }

  function render(data) {
    kpis(data);
    topicList(data);
    trendChart(data);
    network(data);
  }

  function load() {
    fetch('/api/topic-analysis?window=' + current, { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (e) {
        kpiHost.textContent = '';
        kpiHost.appendChild(el('p', 'chart-empty', '话题数据加载失败：' + e.message));
      });
  }

  if (windowSelect) {
    windowSelect.addEventListener('change', function () { current = windowSelect.value; load(); });
  }
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () {
      refreshBtn.disabled = true;
      refreshBtn.textContent = '分析中…';
      fetch('/api/topics/rebuild', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ limit: 200 })
      }).then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body && body.error ? body.error : 'HTTP ' + r.status);
          return body;
        });
      }).then(load).catch(function (e) {
        alert('重新分析失败：' + e.message);
      }).finally(function () {
        refreshBtn.disabled = false;
        refreshBtn.textContent = '重新分析';
      });
    });
  }

  load();
})();

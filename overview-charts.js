/* 总览大屏：KPI 五卡 + 采集流水线控制台 + 趋势/情感/平台/TOP10（只读）。 */
(function () {
  'use strict';

  var host = document.getElementById('overview-charts');
  if (!host) return;
  var consoleBody = document.getElementById('agent-log');
  var online = document.getElementById('agent-online');
  var windowSelect = document.getElementById('dash-window');
  var updated = document.getElementById('dash-updated');
  var currentWindow = '24h';

  var COLORS = ['#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#84cc16', '#f97316'];

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function svgEl(tag) {
    return document.createElementNS('http://www.w3.org/2000/svg', tag);
  }

  function fmtLocal(v) {
    if (!v) return '—';
    var d = new Date(v);
    if (Number.isNaN(d.getTime())) return String(v).replace('T', ' ').slice(0, 19);
    return new Date(d.getTime() + 8 * 3600 * 1000).toISOString().replace('T', ' ').slice(0, 19);
  }

  function clock(v) {
    var t = fmtLocal(v);
    return t.length >= 19 ? t.slice(11, 19) : t;
  }

  /* ---------- KPI 五卡 ---------- */
  function kpiCards(hostEl, kpi) {
    var items = [
      ['记录总数', kpi.records, 'blue'],
      ['今日新增', '+' + kpi.today_added, 'green'],
      ['负面舆情', kpi.negative, 'red'],
      ['活跃话题', kpi.active_topics === undefined ? '—' : kpi.active_topics, 'orange'],
      ['未处理预警', kpi.open_alerts, 'red']
    ];
    items.forEach(function (it) {
      var card = el('div', 'kpi-card ' + it[2]);
      var num = el('div', 'num', String(it[1]));
      num.appendChild(el('small', null, '条'));
      card.appendChild(num);
      card.appendChild(el('div', 'label', it[0]));
      hostEl.appendChild(card);
    });
  }

  /* ---------- 趋势 ---------- */
  function trendChart(rows, body) {
    if (!rows || !rows.length) {
      body.appendChild(el('p', 'chart-empty', '暂无数据'));
      return;
    }
    var W = 820, H = 300, P = 40;
    var max = Math.max.apply(null, rows.map(function (r) { return r.records; }).concat([1]));
    var step = rows.length > 1 ? (W - P * 2) / (rows.length - 1) : 0;
    var svg = svgEl('svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('class', 'chart-svg');

    for (var g = 0; g <= 4; g += 1) {
      var gy = P + (H - P * 2) * g / 4;
      var line = svgEl('line');
      line.setAttribute('x1', P); line.setAttribute('x2', W - P);
      line.setAttribute('y1', gy); line.setAttribute('y2', gy);
      line.setAttribute('stroke', '#eef3f8');
      svg.appendChild(line);
      var label = svgEl('text');
      label.setAttribute('x', P - 8); label.setAttribute('y', gy + 4);
      label.setAttribute('text-anchor', 'end');
      label.setAttribute('fill', '#9aa7b6'); label.setAttribute('font-size', '11');
      label.textContent = String(Math.round(max * (4 - g) / 4));
      svg.appendChild(label);
    }

    var pts = rows.map(function (r, i) {
      var x = P + step * i;
      var y = H - P - (r.records / max) * (H - P * 2);
      return [x, y, r];
    });
    var area = 'M' + pts[0][0] + ',' + (H - P) + ' ' +
      pts.map(function (p) { return 'L' + p[0] + ',' + p[1]; }).join(' ') +
      ' L' + pts[pts.length - 1][0] + ',' + (H - P) + ' Z';
    var areaPath = svgEl('path');
    areaPath.setAttribute('d', area);
    areaPath.setAttribute('fill', 'rgba(31,142,241,.10)');
    svg.appendChild(areaPath);

    var poly = svgEl('polyline');
    poly.setAttribute('points', pts.map(function (p) { return p[0] + ',' + p[1]; }).join(' '));
    poly.setAttribute('fill', 'none');
    poly.setAttribute('stroke', '#1f8ef1');
    poly.setAttribute('stroke-width', '2');
    svg.appendChild(poly);

    pts.forEach(function (p) {
      var c = svgEl('circle');
      c.setAttribute('cx', p[0]); c.setAttribute('cy', p[1]);
      c.setAttribute('r', '3'); c.setAttribute('fill', '#1f8ef1');
      var t = svgEl('title');
      t.textContent = p[2].day + '：' + p[2].records + ' 条（去重 ' + p[2].canonical + '）';
      c.appendChild(t);
      svg.appendChild(c);
    });

    body.appendChild(svg);
    var axis = el('div', 'chart-axis');
    axis.appendChild(el('span', null, rows[0].day));
    axis.appendChild(el('span', null, '峰值 ' + max + ' 条/' + (currentWindow === '24h' ? '时' : '日')));
    axis.appendChild(el('span', null, rows[rows.length - 1].day));
    body.appendChild(axis);
  }

  /* ---------- 情感环图 ---------- */
  function donut(rows, body) {
    var total = rows.reduce(function (s, r) { return s + r.value; }, 0);
    if (!total) {
      body.appendChild(el('p', 'chart-empty', '暂无数据'));
      return;
    }
    var R = 68, C = 2 * Math.PI * R, offset = 0;
    var svg = svgEl('svg');
    svg.setAttribute('viewBox', '0 0 180 180');
    svg.setAttribute('class', 'donut-svg');
    rows.forEach(function (r, i) {
      var seg = svgEl('circle');
      seg.setAttribute('cx', 90); seg.setAttribute('cy', 90); seg.setAttribute('r', R);
      seg.setAttribute('fill', 'none');
      seg.setAttribute('stroke', COLORS[i % COLORS.length]);
      seg.setAttribute('stroke-width', '26');
      seg.setAttribute('stroke-dasharray', (r.value / total * C) + ' ' + C);
      seg.setAttribute('stroke-dashoffset', -offset);
      seg.setAttribute('transform', 'rotate(-90 90 90)');
      svg.appendChild(seg);
      offset += r.value / total * C;
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

  /* ---------- 平台条形 ---------- */
  function bars(rows, body, unit) {
    if (!rows || !rows.length) {
      body.appendChild(el('p', 'chart-empty', '暂无数据'));
      return;
    }
    var max = Math.max.apply(null, rows.map(function (r) { return r.value; }).concat([1]));
    rows.forEach(function (r) {
      var row = el('div', 'bar-row');
      row.appendChild(el('span', 'bar-label', r.label));
      var track = el('span', 'bar-track');
      var fill = el('span', 'bar-fill');
      var ratio = r.value / max;
      fill.style.width = (ratio * 100) + '%';
      // 数量越大越偏红：低值蓝、高值红，一眼看出量级
      fill.style.background = 'hsl(' + Math.round(210 * (1 - ratio)) + ', 80%, 50%)';
      track.appendChild(fill);
      row.appendChild(track);
      var valueEl = el('span', 'bar-value', String(r.value) + (unit || ''));
      valueEl.style.color = 'hsl(' + Math.round(210 * (1 - ratio)) + ', 75%, 38%)';
      valueEl.style.fontWeight = '600';
      row.appendChild(valueEl);
      body.appendChild(row);
    });
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

  /* ---------- 渲染整页 ---------- */
  function render(stats) {
    host.textContent = '';
    var board = document.getElementById('dash-kpis');
    if (board) {
      board.textContent = '';
      kpiCards(board, stats.kpi);
    }

    var two = el('div', 'chart-two');
    var trendBox = card('近7天舆情热度趋势', '按日统计，口径 published_at ?: fetched_at');
    var trendTitle = currentWindow === '24h' ? '近24小时舆情热度趋势' :
      (currentWindow === 'all' ? '全量舆情热度趋势' : '近' + currentWindow.replace('d', '') + '天舆情热度趋势');
    trendBox.box.querySelector('h3').textContent = trendTitle;
    trendBox.box.querySelector('.chart-note').textContent = currentWindow === '24h'
      ? '按小时统计，口径 published_at ?: fetched_at' : '按日统计，口径 published_at ?: fetched_at';
    var trendPanel = el('section', 'panel');
    trendPanel.appendChild(trendBox.box);
    trendChart(stats.trend, trendBox.body);

    var sentiBox = card('情感倾向分布', '规则情感判定（lexicon-zh-v1）');
    var sentiPanel = el('section', 'panel');
    sentiPanel.appendChild(sentiBox.box);
    donut(stats.sentiment, sentiBox.body);

    two.appendChild(trendPanel);
    two.appendChild(sentiPanel);
    host.appendChild(two);

    var grid = el('div', 'chart-grid');
    var platforms = card('平台分布', '按采集源名称聚合');
    bars(stats.platforms, platforms.body);
    var top = card('条目 TOP10', '按 canonical_id 去重分组，非语义聚类');
    bars(stats.top_groups, top.body);
    var platformPanel = el('section', 'panel');
    platformPanel.appendChild(platforms.box);
    var topPanel = el('section', 'panel');
    topPanel.appendChild(top.box);
    grid.appendChild(platformPanel);
    grid.appendChild(topPanel);
    host.appendChild(grid);

    var foot = el('p', 'chart-foot',
      '生成于 ' + fmtLocal(stats.generated_at) + ' · 覆盖口径 ' + stats.coverage +
      ' · ' + (stats.notes || []).join('；'));
    host.appendChild(foot);
    host.hidden = false;

    if (updated) updated.textContent = fmtLocal(stats.generated_at);
    renderConsole(stats);
  }

  /* ---------- 控制台 ---------- */
  function consoleLine(tagClass, tagText, message, time) {
    var row = el('div', 'console-line');
    row.appendChild(el('span', 't', time));
    row.appendChild(el('span', 'log-tag ' + tagClass, tagText));
    row.appendChild(el('span', 'm', message));
    return row;
  }

  function renderConsole(stats) {
    if (!consoleBody) return;
    Promise.all([
      fetch('/api/collect/status', { headers: { accept: 'application/json' } }).then(function (r) { return r.json(); }),
      fetch('/api/reports', { headers: { accept: 'application/json' } }).then(function (r) { return r.json(); })
        .catch(function () { return { reports: [] }; })
    ])
      .then(function (payload) {
        var status = payload[0];
        var reports = (payload[1] && payload[1].reports) || [];
        consoleBody.textContent = '';
        var log = (status.logs || [])[0];
        if (!log) {
          consoleBody.appendChild(el('p', 'console-empty', '尚无采集记录。点右上角「采集 + 分析」跑一轮。'));
          if (online) online.textContent = '尚无采集记录';
          return;
        }
        var time = clock(log.started_at);
        var results = status.last_results || [];
        consoleBody.appendChild(consoleLine('scheduler', 'SCHEDULER',
          (log.trigger === 'manual' ? '手动' : '定时') + '触发采集 · ' +
          (results.length || 6) + ' 个端点 · 冷却 ' + Math.round((status.cooldown_seconds || 0) / 60) + ' 分钟', time));
        results.forEach(function (r) {
          var ok = r.status === 'ok';
          consoleBody.appendChild(consoleLine('crawler', 'CRAWLER',
            (ok ? '✓ ' : '! ') + r.endpoint + ' ' +
            (ok ? '返回 ' + r.returned + ' 条' : '跳过：' + (r.reason || r.status)), clock(log.finished_at)));
        });
        consoleBody.appendChild(consoleLine('cleaner', 'CLEANER',
          '✓ 入库完成 · 新增 ' + log.added + ' 条（重复 ' + log.duplicates + ' 条）', clock(log.finished_at)));
        if (stats) {
          consoleBody.appendChild(consoleLine('sentiment', 'SENTIMENT',
            '规则词典判定 · 待判 ' + stats.kpi.sentiment_pending + ' 条（未跑模型）', clock(log.finished_at)));
          consoleBody.appendChild(consoleLine('topic', 'TOPIC',
            '聚类事件 ' + (stats.kpi.active_topics || 0) + ' 个（≥2 条）', clock(log.finished_at)));
          consoleBody.appendChild(consoleLine('alert', 'ALERT',
            '未处理预警 ' + stats.kpi.open_alerts + ' 条 · 本轮状态 ' + log.status, clock(log.finished_at)));
        }
        consoleBody.appendChild(consoleLine('report', 'REPORT',
          reports.length ? ('最近报告 ' + reports[0].title) : '尚未生成报告', clock(log.finished_at)));
        if (online) {
          online.textContent = status.running
            ? '采集中…'
            : '流水线就绪 · ' + status.endpoints.length + ' 个端点 · 每 ' + status.interval_minutes + ' 分钟';
        }
      })
      .catch(function () {
        if (online) online.textContent = '状态读取失败';
      });
  }

  /* ---------- 加载 ---------- */
  function load(windowKey) {
    currentWindow = windowKey || currentWindow;
    fetch('/api/overview-stats?window=' + currentWindow, { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (e) {
        host.textContent = '';
        host.appendChild(el('p', 'chart-empty', '总览聚合加载失败：' + e.message));
        host.hidden = false;
      });
  }

  if (windowSelect) {
    windowSelect.value = currentWindow;
    windowSelect.addEventListener('change', function () { load(windowSelect.value); });
  }

  window.addEventListener('overview:refresh', function () { load(currentWindow); });

  load(currentWindow);
})();

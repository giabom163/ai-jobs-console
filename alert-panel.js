/* 预警中心：规则表 + 告警表（只读 GET /api/alert-rules、GET /api/alerts）。 */
(function () {
  'use strict';
  var host = document.getElementById('alert-center');
  if (!host) return;

  var LEVEL_TEXT = { info: '提示', warn: '警告', critical: '严重' };
  var KIND_TEXT = { keyword: '标题/摘要包含', source: '来源等于', sentiment: '情感等于' };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function table(columns, rows, rowClass) {
    var wrap = el('div', 'table-wrap');
    var t = el('table', 'table');
    var thead = el('thead');
    var tr = el('tr');
    columns.forEach(function (c) { tr.appendChild(el('th', null, c)); });
    thead.appendChild(tr);
    t.appendChild(thead);
    var tb = el('tbody');
    rows.forEach(function (r) {
      var row = el('tr');
      if (rowClass) {
        var cls = rowClass(r);
        if (cls) row.className = cls;
      }
      r.forEach(function (cell) {
        var td = el('td');
        if (cell === null || cell === undefined) td.textContent = '—';
        else if (cell.nodeType) td.appendChild(cell);
        else td.textContent = String(cell);
        row.appendChild(td);
      });
      tb.appendChild(row);
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    return wrap;
  }

  function levelCell(level) {
    var span = el('span', 'alert-level alert-level-' + (level || 'info'));
    span.textContent = (LEVEL_TEXT[level] || level || '—') + '（' + (level || '—') + '）';
    return span;
  }

  function fetchJSON(url) {
    return fetch(url, { headers: { accept: 'application/json' } }).then(function (r) {
      if (!r.ok) throw new Error(url + ' HTTP ' + r.status);
      return r.json();
    });
  }

  function render(rules, alerts) {
    rules = rules || [];
    alerts = alerts || [];
    host.textContent = '';

    host.appendChild(el('h3', 'col-title', '预警中心'));
    host.appendChild(el('p', 'hint',
      '规则 ' + rules.length + ' 条 · 告警 ' + alerts.length + ' 条。本面板只读：不新增规则、不触发评估、不做通知。'));

    host.appendChild(el('h3', 'col-title', '规则表（alert_rules）'));
    if (!rules.length) {
      host.appendChild(el('p', 'chart-empty', '暂无规则'));
    } else {
      host.appendChild(table(
        ['规则ID', '名称', '匹配方式', '匹配值', '窗口(分钟)', '阈值', '级别', '冷却(分钟)', '启用', '创建时间'],
        rules.map(function (r) {
          return [r.id, r.name, KIND_TEXT[r.match_kind] || r.match_kind, r.match_value,
                  r.window_minutes, r.threshold, levelCell(r.level), r.cooldown_minutes,
                  r.enabled ? '是' : '否', String(r.created_at || '').slice(0, 19)];
        })));
    }

    host.appendChild(el('h3', 'col-title', '告警表（alerts）'));
    if (!alerts.length) {
      host.appendChild(el('p', 'chart-empty', '暂无告警'));
    } else {
      host.appendChild(table(
        ['告警ID', '规则ID', '级别', '命中数', '阈值', '状态', '首次出现', '最近出现'],
        alerts.map(function (a) {
          var m = a.metrics && typeof a.metrics === 'object' ? a.metrics : {};
          var status = a.status || 'open';
          return [String(a.id || '').slice(0, 24), a.rule_id, levelCell(a.level),
                  m.hit_count, m.threshold, status,
                  String(a.first_seen || '').slice(0, 19), String(a.last_seen || '').slice(0, 19)];
        }),
        function (r) { return 'alert-' + r[5]; }));
    }

    host.appendChild(el('p', 'chart-foot',
      '命中对象为旧 RSS 材料（records）；窗口口径 coalesce(published_at, fetched_at)；冷却期内不重复新建。'));
    host.hidden = false;
  }

  Promise.all([
    fetchJSON('/api/alert-rules'),
    fetchJSON('/api/alerts?limit=100')
  ]).then(function (results) {
    render(results[0], results[1]);
  }).catch(function (e) {
    host.textContent = '';
    host.appendChild(el('p', 'chart-empty', '预警数据加载失败：' + e.message));
    host.hidden = false;
  });
})();

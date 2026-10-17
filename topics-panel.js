/* 话题页：分析作业 + 条目分组（只读 /api/topics）。 */
(function () {
  'use strict';
  var host = document.getElementById('topics-analysis');
  if (!host) return;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function table(columns, rows) {
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
      r.forEach(function (cell) { row.appendChild(el('td', null, String(cell))); });
      tb.appendChild(row);
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    return wrap;
  }

  function render(d) {
    host.textContent = '';
    var sm = d.sentiment_model || {};
    var line = el('p', 'hint',
      '分析作业 ' + (d.jobs || []).length + ' 个 · 条目分组 ' + (d.groups || []).length +
      ' 组 · 模型情感已标注 ' + (sm.calls || 0) + ' 条（与规则不一致 ' + (sm.disagreements || 0) + ' 条）');
    host.appendChild(line);

    host.appendChild(el('h3', 'col-title', '分析作业（analysis_jobs）'));
    if (!(d.jobs || []).length) {
      host.appendChild(el('p', 'chart-empty', '暂无分析作业'));
    } else {
      host.appendChild(table(['作业', '分组', '规则版本', '模型版本', '状态', '角色请求', '创建时间'],
        d.jobs.map(function (j) {
          return [j.id.slice(0, 18), j.group_id, j.rule_version, j.model_version, j.status,
                  (j.requests || []).map(function (r) { return r.role + ':' + r.status; }).join(' ') || '—',
                  String(j.created_at || '').slice(0, 19)];
        })));
    }

    host.appendChild(el('h3', 'col-title', '条目分组（按 canonical_id 精确去重）'));
    host.appendChild(table(['条目', '记录数', '偏正面', '偏负面', '中性', '最近抓取'],
      (d.groups || []).map(function (g) {
        return [g.title, g.records, g.positive, g.negative, g.neutral, String(g.last_fetch || '').slice(0, 19)];
      })));

    (d.notes || []).forEach(function (n) { host.appendChild(el('p', 'chart-foot', n)); });
    host.hidden = false;
  }

  fetch('/api/topics?top_k=10', { headers: { accept: 'application/json' } })
    .then(function (r) { return r.json(); })
    .then(render)
    .catch(function (e) {
      host.textContent = '';
      host.appendChild(el('p', 'chart-empty', '话题数据加载失败：' + e.message));
      host.hidden = false;
    });
})();

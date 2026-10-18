/* 流水线任务面板：只读 /api/pipeline/tasks。 */
(function () {
  'use strict';
  var host = document.getElementById('pipeline-list');
  if (!host) return;
  var refreshBtn = document.getElementById('pipeline-refresh');

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function fmt(v) {
    if (!v) return '—';
    var d = new Date(v);
    if (Number.isNaN(d.getTime())) return String(v).replace('T', ' ').slice(0, 19);
    return new Date(d.getTime() + 8 * 3600 * 1000).toISOString().replace('T', ' ').slice(0, 19);
  }

  function duration(task) {
    if (!task.started_at || !task.finished_at) return '—';
    var ms = new Date(task.finished_at) - new Date(task.started_at);
    if (!(ms >= 0)) return '—';
    return ms < 1000 ? ms + 'ms' : (ms / 1000).toFixed(1) + 's';
  }

  var STATUS = { ok: 'ok', failed: 'danger', dead: 'danger', running: 'muted' };

  function render(tasks) {
    host.textContent = '';
    if (!tasks.length) {
      host.appendChild(el('p', 'chart-empty', '还没有流水线任务。跑一次采集、聚类或生成报告后会出现。'));
      return;
    }
    var table = el('table', 'table');
    var thead = el('thead');
    var tr = el('tr');
    ['开始时间', '阶段', '状态', '尝试', '耗时', '错误'].forEach(function (c) {
      tr.appendChild(el('th', null, c));
    });
    thead.appendChild(tr);
    table.appendChild(thead);
    var tbody = el('tbody');
    tasks.forEach(function (task) {
      var row = el('tr');
      row.appendChild(el('td', null, fmt(task.started_at || task.created_at)));
      row.appendChild(el('td', null, task.stage));
      var statusCell = el('td');
      statusCell.appendChild(el('span', 'pill ' +
        (Object.prototype.hasOwnProperty.call(STATUS, task.status) ? STATUS[task.status] : 'muted'),
        task.status));
      row.appendChild(statusCell);
      row.appendChild(el('td', null, String(task.attempts)));
      row.appendChild(el('td', null, duration(task)));
      row.appendChild(el('td', null, task.last_error || '—'));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  function load() {
    fetch('/api/pipeline/tasks?limit=50', { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) { render(data.tasks || []); })
      .catch(function (e) {
        host.textContent = '';
        host.appendChild(el('p', 'chart-empty', '流水线任务读取失败：' + e.message));
      });
  }

  if (refreshBtn) refreshBtn.addEventListener('click', load);
  load();
})();

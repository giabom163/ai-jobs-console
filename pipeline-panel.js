/* 流水线面板：分析流水线（run/state）+ 单阶段任务（pipeline_tasks）。只读 + 手动触发。 */
(function () {
  'use strict';
  var host = document.getElementById('pipeline-list');
  var runsHost = document.getElementById('pipeline-runs');
  if (!host) return;
  var refreshBtn = document.getElementById('pipeline-refresh');
  var runBtn = document.getElementById('pipeline-run');

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

  var STATUS = { ok: 'ok', failed: 'danger', dead: 'danger', running: 'muted', interrupted: 'danger' };

  function statusClass(status) {
    return Object.prototype.hasOwnProperty.call(STATUS, status) ? STATUS[status] : 'muted';
  }

  function renderTasks(tasks) {
    host.textContent = '';
    if (!tasks.length) {
      host.appendChild(el('p', 'chart-empty', '还没有单阶段任务。跑一次采集、聚类或生成报告后会出现。'));
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
      statusCell.appendChild(el('span', 'pill ' + statusClass(task.status), task.status));
      row.appendChild(statusCell);
      row.appendChild(el('td', null, String(task.attempts)));
      row.appendChild(el('td', null, duration(task)));
      row.appendChild(el('td', null, task.last_error || '—'));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  function renderRuns(runs) {
    if (!runsHost) return;
    runsHost.textContent = '';
    if (!runs.length) {
      runsHost.appendChild(el('p', 'chart-empty', '还没有分析流水线运行记录。点右上角「运行分析流水线」。'));
      return;
    }
    runs.forEach(function (run) {
      var box = el('div', 'pipeline-run');
      var head = el('div', 'pipeline-run-head');
      head.appendChild(el('strong', null, run.trace_id.slice(0, 24)));
      head.appendChild(el('span', 'pill ' + statusClass(run.status), run.status));
      head.appendChild(el('span', 'hint', '开始 ' + fmt(run.started_at) +
        (run.finished_at ? ' · 结束 ' + fmt(run.finished_at) : '') +
        (run.error ? ' · ' + run.error : '')));
      box.appendChild(head);
      var stages = el('div', 'pipeline-stages');
      (run.stages || []).forEach(function (stage) {
        var chip = el('span', 'pill ' + statusClass(stage.status),
          stage.stage + ' · ' + stage.status);
        if (stage.error) chip.title = stage.error;
        stages.appendChild(chip);
      });
      box.appendChild(stages);
      runsHost.appendChild(box);
    });
  }

  function load() {
    fetch('/api/pipeline/runs?limit=5', { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) { renderRuns(data.runs || []); })
      .catch(function (e) { console.error(e); });
    fetch('/api/pipeline/tasks?limit=50', { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) { renderTasks(data.tasks || []); })
      .catch(function (e) {
        host.textContent = '';
        host.appendChild(el('p', 'chart-empty', '流水线任务读取失败：' + e.message));
      });
  }

  function pollState(traceId) {
    fetch('/api/pipeline/state?trace=' + encodeURIComponent(traceId), { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.run) return;
        if (data.run.status === 'running') {
          setTimeout(function () { pollState(traceId); }, 1500);
        } else {
          if (runBtn) { runBtn.disabled = false; runBtn.textContent = '运行分析流水线'; }
          load();
        }
      })
      .catch(function () { setTimeout(function () { pollState(traceId); }, 2000); });
  }

  if (runBtn) {
    runBtn.addEventListener('click', function () {
      runBtn.disabled = true;
      runBtn.textContent = '运行中…';
      fetch('/api/pipeline/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      }).then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body && body.error ? body.error : 'HTTP ' + r.status);
          return body;
        });
      }).then(function (data) {
        load();
        pollState(data.trace_id);
      }).catch(function (e) {
        runBtn.disabled = false;
        runBtn.textContent = '运行分析流水线';
        alert('启动分析流水线失败：' + e.message);
      });
    });
  }

  if (refreshBtn) refreshBtn.addEventListener('click', load);
  load();
})();

/* 报告管理：生成/列表/预览/下载，全部走本机 reports 表。 */
(function () {
  'use strict';
  var listHost = document.getElementById('report-list');
  if (!listHost) return;
  var kpiHost = document.getElementById('report-kpis');
  var dailyBtn = document.getElementById('report-daily');
  var weeklyBtn = document.getElementById('report-weekly');

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

  function sizeText(bytes) {
    if (!bytes) return '0 KB';
    return (bytes / 1024).toFixed(1) + ' KB';
  }

  function kpiCards(reports) {
    if (!kpiHost) return;
    kpiHost.textContent = '';
    var month = new Date();
    var key = month.getFullYear() + '-' + String(month.getMonth() + 1).padStart(2, '0');
    var monthly = reports.filter(function (r) { return String(r.created_at || '').slice(0, 7) === key; }).length;
    var bytes = reports.reduce(function (s, r) { return s + (r.size_bytes || 0); }, 0);
    var storageValue = bytes >= 1048576 ? (bytes / 1048576).toFixed(2) : (bytes / 1024).toFixed(1);
    var storageUnit = bytes >= 1048576 ? 'MB' : 'KB';
    var items = [
      ['报告总数', reports.length, 'blue', '份'],
      ['本月生成', monthly, 'green', '份'],
      ['存储占用', storageValue, 'orange', storageUnit]
    ];
    items.forEach(function (it) {
      var card = el('div', 'kpi-card ' + it[2]);
      var num = el('div', 'num', String(it[1]));
      num.appendChild(el('small', null, it[3]));
      card.appendChild(num);
      card.appendChild(el('div', 'label', it[0]));
      kpiHost.appendChild(card);
    });
  }

  function render(reports) {
    kpiCards(reports);
    listHost.textContent = '';
    if (!reports.length) {
      listHost.appendChild(el('p', 'chart-empty', '还没有报告。点右上角「生成日报」或「生成周报」。'));
      return;
    }
    var table = el('table', 'table');
    var thead = el('thead');
    var tr = el('tr');
    ['报告名称', '类型', '时间范围', '生成时间', '大小', '操作'].forEach(function (c) {
      tr.appendChild(el('th', null, c));
    });
    thead.appendChild(tr);
    table.appendChild(thead);
    var tbody = el('tbody');
    reports.forEach(function (r) {
      var row = el('tr');
      row.appendChild(el('td', null, r.title));
      row.appendChild(el('td', null, r.kind === 'weekly' ? '周报' : '日报'));
      row.appendChild(el('td', null,
        r.kind === 'weekly'
          ? fmt(r.period_start).slice(0, 10) + ' ~ ' + fmt(r.period_end).slice(0, 10)
          : fmt(r.period_start).slice(0, 10)));
      row.appendChild(el('td', null, fmt(r.created_at)));
      row.appendChild(el('td', null, sizeText(r.size_bytes)));
      var ops = el('td');
      var preview = el('a', null, '预览');
      preview.href = '/api/reports/' + encodeURIComponent(r.id) + '/preview';
      preview.target = '_blank';
      preview.rel = 'noreferrer';
      preview.style.marginRight = '10px';
      var download = el('a', null, '下载');
      download.href = '/api/reports/' + encodeURIComponent(r.id) + '/download';
      download.setAttribute('download', r.id + '.md');
      ops.appendChild(preview);
      ops.appendChild(download);
      row.appendChild(ops);
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    listHost.appendChild(table);
  }

  function load() {
    fetch('/api/reports', { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) { render(data.reports || []); })
      .catch(function (e) {
        listHost.textContent = '';
        listHost.appendChild(el('p', 'chart-empty', '报告读取失败：' + e.message));
      });
  }

  function generate(kind, button) {
    if (button) button.disabled = true;
    fetch('/api/reports/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: kind })
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(load).catch(function (e) {
      listHost.appendChild(el('p', 'chart-empty', '生成失败：' + e.message));
    }).finally(function () { if (button) button.disabled = false; });
  }

  if (dailyBtn) dailyBtn.addEventListener('click', function () { generate('daily', dailyBtn); });
  if (weeklyBtn) weeklyBtn.addEventListener('click', function () { generate('weekly', weeklyBtn); });

  load();
})();

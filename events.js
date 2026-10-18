/* 事件聚类列表：只读 /api/events；重建走 /api/topics/rebuild。 */
(function () {
  'use strict';
  var host = document.getElementById('events-list');
  if (!host) return;
  var meta = document.getElementById('events-meta');
  var rebuild = document.getElementById('events-rebuild');

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

  function render(data) {
    var events = (data.events || []).filter(function (ev) { return (ev.records || 0) >= 2; });
    host.textContent = '';
    if (meta) meta.textContent = '活跃话题 ' + (data.active_topics || 0) + ' 个（≥2 条）';
    if (!events.length) {
      host.appendChild(el('p', 'chart-empty', '还没有聚类结果，点右上角「重新聚类」。'));
      return;
    }
    var wrap = el('div', 'table-wrap');
    var table = el('table', 'table');
    var thead = el('thead');
    var tr = el('tr');
    ['事件', '条数', '涉及来源', '首次出现'].forEach(function (h) { tr.appendChild(el('th', null, h)); });
    thead.appendChild(tr);
    table.appendChild(thead);
    var tbody = el('tbody');
    events.forEach(function (ev) {
      var row = el('tr');
      row.appendChild(el('td', null, ev.label));
      row.appendChild(el('td', null, String(ev.records)));
      var feeds = ev.platforms || '[]';
      try { feeds = JSON.parse(feeds).join(', '); } catch (e) { /* 保留原串 */ }
      row.appendChild(el('td', null, feeds || '—'));
      row.appendChild(el('td', null, fmt(ev.first_seen)));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    host.appendChild(wrap);
  }

  function load() {
    fetch('/api/events?limit=50', { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (e) {
        host.textContent = '';
        host.appendChild(el('p', 'chart-empty', '事件加载失败：' + e.message));
      });
  }

  if (rebuild) {
    rebuild.addEventListener('click', function () {
      rebuild.disabled = true;
      rebuild.textContent = '聚类中…';
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
        host.textContent = '';
        host.appendChild(el('p', 'chart-empty', '聚类失败：' + e.message));
      }).finally(function () {
        rebuild.disabled = false;
        rebuild.textContent = '重新聚类';
      });
    });
  }

  load();
})();

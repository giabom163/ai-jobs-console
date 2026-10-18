/* 全库检索：只读 /api/search，检索采集库 records；与 P1 的 3 帖检索分离。 */
(function () {
  'use strict';

  var form = document.getElementById('corpus-search-form');
  if (!form) return;
  var q = document.getElementById('corpus-q');
  var sentiment = document.getElementById('corpus-sentiment');
  var feed = document.getElementById('corpus-feed');
  var from = document.getElementById('corpus-from');
  var to = document.getElementById('corpus-to');
  var status = document.getElementById('corpus-search-status');
  var results = document.getElementById('corpus-results');
  var pager = document.getElementById('corpus-pager');
  var meta = document.getElementById('corpus-search-meta');

  var state = { offset: 0, limit: 20, total: 0, loaded: false };

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
    // fetched_at 存 UTC；展示统一为 Asia/Shanghai（UTC+8），与日期筛选口径一致
    var local = new Date(d.getTime() + 8 * 3600 * 1000);
    return local.toISOString().replace('T', ' ').slice(0, 19);
  }

  function sentimentClass(value) {
    if (value === '强负面' || value === '偏负面') return 'pill danger';
    if (value === '强正面' || value === '偏正面') return 'pill ok';
    return 'pill muted';
  }

  function loadFeeds() {
    fetch('/api/data', { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        while (feed.options.length > 1) feed.remove(1);
        (data.feeds || []).forEach(function (f) {
          var opt = document.createElement('option');
          opt.value = String(f.id);
          opt.textContent = f.name;
          feed.appendChild(opt);
        });
      })
      .catch(function () { /* 来源下拉失败不影响检索 */ });
  }

  function params() {
    var p = new URLSearchParams();
    if (q.value.trim()) p.set('q', q.value.trim());
    if (sentiment.value) p.set('sentiment', sentiment.value);
    if (feed.value) p.set('feed_id', feed.value);
    if (from.value) p.set('from', from.value);
    if (to.value) p.set('to', to.value);
    p.set('limit', String(state.limit));
    p.set('offset', String(state.offset));
    return p;
  }

  function render(data) {
    state.total = data.total || 0;
    results.textContent = '';
    if (!data.items || !data.items.length) {
      results.appendChild(el('p', 'empty', '没有匹配的记录。'));
    }
    (data.items || []).forEach(function (item) {
      var card = el('article', 'p1-card');
      var head = el('div', 'p1-card-head');
      var a = el('a', null, item.title || '(无标题)');
      a.href = item.url || '#';
      a.target = '_blank';
      a.rel = 'noreferrer';
      head.appendChild(a);
      head.appendChild(el('span', sentimentClass(item.sentiment), item.sentiment || '未判定'));
      card.appendChild(head);
      if (item.summary) card.appendChild(el('p', 'muted', item.summary));
      var foot = el('p', 'filter-note',
        (item.source || '未知来源') + ' · 抓取 ' + fmt(item.fetched_at));
      card.appendChild(foot);
      results.appendChild(card);
    });

    var start = state.total ? state.offset + 1 : 0;
    var end = Math.min(state.offset + state.limit, state.total);
    status.textContent = '共 ' + state.total + ' 条，当前第 ' + start + '–' + end + ' 条。';
    meta.textContent = '共 ' + state.total + ' 条';
    renderPager();
  }

  function renderPager() {
    pager.textContent = '';
    var prev = el('button', 'secondary', '上一页');
    var next = el('button', 'secondary', '下一页');
    prev.type = 'button';
    next.type = 'button';
    prev.disabled = state.offset <= 0;
    next.disabled = state.offset + state.limit >= state.total;
    prev.addEventListener('click', function () {
      state.offset = Math.max(0, state.offset - state.limit);
      search();
    });
    next.addEventListener('click', function () {
      state.offset += state.limit;
      search();
    });
    pager.appendChild(prev);
    pager.appendChild(next);
  }

  function search() {
    status.textContent = '正在检索…';
    fetch('/api/search?' + params().toString(), { headers: { accept: 'application/json' } })
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body && body.error ? body.error : 'HTTP ' + r.status);
          return body;
        });
      })
      .then(render)
      .catch(function (e) {
        status.textContent = '检索失败：' + e.message;
      });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    state.offset = 0;
    search();
  });

  loadFeeds();
  window.addEventListener('corpus:refresh-feeds', loadFeeds);
  if (!state.loaded) {
    state.loaded = true;
    search();
  }
})();

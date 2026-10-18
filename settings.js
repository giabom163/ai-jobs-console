/* 系统设置：settings 表读写；关键词同步 keywords 表。 */
(function () {
  'use strict';
  var saveBtn = document.getElementById('settings-save');
  if (!saveBtn) return;
  var interval = document.getElementById('set-interval');
  var limit = document.getElementById('set-limit');
  var fuzzy = document.getElementById('set-fuzzy');
  var chips = document.getElementById('kw-chips');
  var input = document.getElementById('kw-input');
  var addBtn = document.getElementById('kw-add');
  var keywords = [];

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function renderChips() {
    chips.textContent = '';
    if (!keywords.length) {
      chips.appendChild(el('span', 'hint', '暂无关键词'));
      return;
    }
    keywords.forEach(function (word) {
      var chip = el('span', 'pill', word + ' ×');
      chip.style.cursor = 'pointer';
      chip.title = '点击移除';
      chip.addEventListener('click', function () {
        keywords = keywords.filter(function (k) { return k !== word; });
        renderChips();
      });
      chips.appendChild(chip);
    });
  }

  function addKeyword() {
    var value = (input.value || '').trim().slice(0, 80);
    if (!value) return;
    if (keywords.indexOf(value) === -1) keywords.push(value);
    input.value = '';
    renderChips();
  }

  if (addBtn) addBtn.addEventListener('click', addKeyword);
  if (input) {
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); addKeyword(); }
    });
  }

  function load() {
    fetch('/api/settings', { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        interval.value = data.collect_interval;
        limit.value = data.collect_limit;
        fuzzy.checked = !!data.fuzzy;
        keywords = (data.keywords || []).slice();
        renderChips();
      })
      .catch(function () { /* 读取失败保持空表单 */ });
  }

  saveBtn.addEventListener('click', function () {
    saveBtn.disabled = true;
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        collect_interval: Number(interval.value),
        collect_limit: Number(limit.value),
        fuzzy: !!fuzzy.checked,
        keywords: keywords
      })
    }).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) throw new Error(body && body.error ? body.error : 'HTTP ' + r.status);
        return body;
      });
    }).then(function (data) {
      keywords = (data.keywords || []).slice();
      renderChips();
      saveBtn.textContent = '已保存';
      setTimeout(function () { saveBtn.textContent = '保存设置'; }, 2000);
    }).catch(function (e) {
      saveBtn.textContent = '保存失败';
      setTimeout(function () { saveBtn.textContent = '保存设置'; }, 2500);
      alert('保存失败：' + e.message);
    }).finally(function () { saveBtn.disabled = false; });
  });

  load();
})();

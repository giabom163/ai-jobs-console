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
  var loaded = false;
  saveBtn.disabled = true;

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
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        interval.value = data.collect_interval;
        limit.value = data.collect_limit;
        fuzzy.checked = !!data.fuzzy;
        keywords = (data.keywords || []).slice();
        renderChips();
        loaded = true;
        saveBtn.disabled = false;
        saveBtn.title = '';
      })
      .catch(function (e) {
        loaded = false;
        saveBtn.disabled = true;
        saveBtn.title = '设置读取失败，禁止保存以免覆盖现有配置';
        chips.textContent = '';
        chips.appendChild(el('span', 'hint', '设置读取失败：' + e.message));
      });
  }

  saveBtn.addEventListener('click', function () {
    if (!loaded) {
      alert('设置尚未成功读取，禁止保存以免覆盖现有配置。');
      return;
    }
    var intervalValue = Number(interval.value);
    var limitValue = Number(limit.value);
    if (!Number.isInteger(intervalValue) || intervalValue < 0 || intervalValue > 1440) {
      alert('采集间隔必须是 0–1440 的整数（分钟）');
      return;
    }
    if (!Number.isInteger(limitValue) || limitValue < 1 || limitValue > 50) {
      alert('每端点条数必须是 1–50 的整数');
      return;
    }
    saveBtn.disabled = true;
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        collect_interval: intervalValue,
        collect_limit: limitValue,
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

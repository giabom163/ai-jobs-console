import fs from 'node:fs';
import path from 'node:path';

const outDir = 'C:/Users/Lenovo/ego/inspect-live-20261104';
const report = {
  testedAt: new Date().toISOString(),
  targetUrl: 'https://giabom.online/?status=listed&since=2026-10-05&ai=ai&view=signals',
  assertions: []
};

function check(condition, message) {
  if (!condition) {
    const err = new Error(`ASSERTION FAILED: ${message}`);
    err.isAssertion = true;
    report.assertions.push({ status: 'FAIL', message });
    throw err;
  }
  report.assertions.push({ status: 'PASS', message });
  console.log(`[PASS] ${message}`);
}

try {
  const task = await taskSpace('inspect-live-signals-20261104');
  const page = task.page('p1');

  console.log('1. Testing initial view synchronization (explicit assertion)...');
  await page.goto(report.targetUrl + '&_t=' + Date.now());

  // 1. 立即检查初态（在任何异步 API 请求响应前）
  const immediateState = await page.evaluate(() => {
    return {
      activeNav: document.querySelector('nav button.active')?.dataset.view,
      signalsHidden: document.querySelector('#view-signals')?.hidden,
      dashboardHidden: document.querySelector('#view-dashboard')?.hidden
    };
  });
  console.log('Immediate state:', immediateState);
  check(immediateState.activeNav === 'signals', '初态导航高亮必须为 signals');
  check(immediateState.signalsHidden === false, '初态 #view-signals 必须可见 (hidden=false)');
  check(immediateState.dashboardHidden === true, '初态 #view-dashboard 必须隐藏 (hidden=true)，杜绝闪烁');

  // 等待初始化完成
  await page.waitForFunction(() => {
    const status = document.querySelector('#app-status');
    return status && (status.hidden || status.style.display === 'none');
  }, undefined, { timeout: 30000 });

  // 2. 检查线上诚实空状态（未审计数据已彻底下架，不误导公众）
  const signalsState = await page.evaluate(() => {
    const events = document.querySelectorAll('#events-list .event-card').length;
    const signals = document.querySelectorAll('#signals-list .event').length;
    const emptyNotice = document.querySelector('#events-list')?.innerText;
    return { events, signals, emptyNotice };
  });
  console.log('Signals state:', signalsState);
  check(signalsState.events === 0, '重大事件库必须为 0（已诚实撤下未核验事件）');
  check(signalsState.signals === 0, '就业线索库必须为 0（已诚实撤下未核验线索）');

  // 3. 测试岗位库恢复默认筛选
  console.log('3. Testing reset filters in jobs view...');
  await page.click('button[data-view="jobs"]');
  await page.waitForFunction(() => !document.querySelector('#view-jobs')?.hidden, undefined, { timeout: 10000 });

  // 模拟用户输入筛选
  await page.evaluate(() => {
    const bj = document.querySelector('input[name="city"][value="beijing"]');
    if (bj) bj.checked = true;
    const q = document.querySelector('#q');
    if (q) q.value = '测试检索关键词';
    const adv = document.querySelector('#advanced-filters');
    if (adv) adv.open = true;
  });

  // 点击重置按钮
  await page.click('#reset-filters');
  await page.waitForTimeout(1000);

  const stateAfterReset = await page.evaluate(() => ({
    bjChecked: document.querySelector('input[name="city"][value="beijing"]')?.checked,
    qVal: document.querySelector('#q')?.value,
    advOpen: document.querySelector('#advanced-filters')?.open,
    url: location.search
  }));
  console.log('State after reset:', stateAfterReset);
  check(stateAfterReset.bjChecked === false, '重置后北京城市复选框必须取消选中');
  check(stateAfterReset.qVal === '', '重置后搜索框关键词必须清空');
  check(stateAfterReset.advOpen === false, '重置后高级筛选折叠面板必须收起');
  check(stateAfterReset.url.includes('since=all'), '重置后 URL 必须重置为 default');

  report.success = true;
  console.log('ALL STRICT ASSERTIONS PASSED!');
} catch (e) {
  report.success = false;
  report.error = e.message;
  console.error('VERIFICATION FAILED:', e);
  process.exitCode = 1;
} finally {
  fs.writeFileSync(path.join(outDir, 'strict-assertion-result.json'), JSON.stringify(report, null, 2), 'utf8');
}

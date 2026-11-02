// Ego Lite Boss直聘大模型岗位只读提取脚本 (UTF-8, Node.js ESM)
// 运行命令 (在非提权普通终端，复用已有登录态):
// ego-browser nodejs -e "await import('file:///D:/DSH/repos/ai-employment-monitor/tools/browser_extract_boss.mjs')"

const fs = await import('node:fs/promises');

console.log('[boss-extract] 正在连接 Ego Lite 任务空间...');
const task = await taskSpace('boss-jobs-extract');
const page = task.page('p1');

console.log('[boss-extract] 打开 Boss直聘 大模型岗位搜索页 (全国国内/北京)...');
// 搜索 query: 大模型, 默认全国/主要城市
await page.goto('https://www.zhipin.com/web/geek/job?query=%E5%A4%A7%E6%A8%A1%E5%9E%8B&city=100010000');
await page.waitForTimeout(3500);

const results = await page.evaluate(() => {
  const cards = document.querySelectorAll('.job-card-wrapper, .job-card-body, [class*="job-card"]');
  const items = [];
  for (const c of cards) {
    if (items.length >= 30) break;
    const titleEl = c.querySelector('.job-name, [class*="job-title"]');
    const title = titleEl ? titleEl.innerText.trim() : '';
    const companyEl = c.querySelector('.company-name, [class*="company-title"]');
    const company = companyEl ? companyEl.innerText.trim() : '';
    const areaEl = c.querySelector('.job-area, [class*="job-location"]');
    const location = areaEl ? areaEl.innerText.trim() : '全国';
    const salaryEl = c.querySelector('.salary, [class*="salary"]');
    const salary = salaryEl ? salaryEl.innerText.trim() : '';
    const linkEl = c.querySelector('a.job-card-left, a');
    const href = linkEl ? linkEl.href : '';
    const tagEls = c.querySelectorAll('.tag-list li, .job-tags span');
    const tags = Array.from(tagEls).map(t => t.innerText.trim()).filter(Boolean);

    if (title || company) {
      items.push({
        title,
        company,
        location,
        salary,
        url: href,
        tags,
        card_text: c.innerText.trim(),
      });
    }
  }
  return items;
});

const outPath = 'C:/Users/Lenovo/.codex\documents/ai-jobs-validation-20261102/boss-browser-extracted.json';
await fs.writeFile(outPath, JSON.stringify({
  captured_at: new Date().toISOString(),
  source: 'platform:boss',
  count: results.length,
  items: results,
}, null, 2), 'utf8');

console.log(`[boss-extract] 提取完成! 共获取 ${results.length} 条 Boss 直聘岗位，已保存至 ${outPath}`);
await task.finish({ keep: [] });

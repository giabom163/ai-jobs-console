// Ego Lite 百度招聘大模型岗位只读提取脚本 (UTF-8, Node.js ESM)
// 运行命令 (在非提权普通终端):
// ego-browser nodejs -e "await import('file:///D:/DSH/repos/ai-employment-monitor/tools/browser_extract_baidu.mjs')"

const fs = await import('node:fs/promises');
const path = await import('node:path');

console.log('[baidu-extract] 正在连接 Ego Lite 任务空间...');
const task = await taskSpace('baidu-jobs-extract');
const page = task.page('p1');

console.log('[baidu-extract] 打开百度官方社招列表...');
await page.goto('https://talent.baidu.com/jobs/social-list?keyWord=%E5%A4%A7%E6%A8%A1%E5%9E%8B');
await page.waitForTimeout(3000);

const results = await page.evaluate(() => {
  const cards = document.querySelectorAll('.brick-job-card, .job-item, [class*="job-card"]');
  const items = [];
  for (const c of cards) {
    if (items.length >= 30) break;
    const titleEl = c.querySelector('[class*="title"], h3, .job-name');
    const title = titleEl ? titleEl.innerText.trim() : '';
    const linkEl = c.querySelector('a');
    const href = linkEl ? linkEl.href : window.location.href;
    const text = c.innerText.trim();
    if (title || text) {
      items.push({
        title: title || text.split('\n')[0],
        url: href,
        card_text: text,
      });
    }
  }
  return items;
});

const outPath = 'C:/Users/Lenovo/.codex/documents/ai-jobs-validation-20261102/baidu-browser-extracted.json';
await fs.writeFile(outPath, JSON.stringify({
  captured_at: new Date().toISOString(),
  source: 'official:baidu',
  count: results.length,
  items: results,
}, null, 2), 'utf8');

console.log(`[baidu-extract] 提取完成! 共获取 ${results.length} 条岗位，已保存至 ${outPath}`);
await task.finish({ keep: [] });

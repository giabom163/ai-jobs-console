// Ego Lite Boss直聘大模型平滑持续采集引擎 (UTF-8, Node.js ESM)
// 设计目标: 摆脱单次 30 条写死限制，支持步进多页、每页严格间隔 22 秒平滑采集、防风控检测与断点持久化。

const fs = await import('node:fs/promises');
const path = await import('node:path');

// 运行参数配置 (支持环境变量覆盖)
const KEYWORD = process.env.BOSS_KEYWORD || '大模型';
const MAX_PAGES = parseInt(process.env.BOSS_MAX_PAGES || '5', 10);
const INTERVAL_MS = Math.max(22000, parseInt(process.env.BOSS_INTERVAL_MS || '22000', 10));
const OUT_FILE = 'C:/Users/Lenovo/.codex/documents/ai-jobs-validation-20261102/boss-continuous-batch.json';

console.log(`[boss-pipeline] 启动平滑采集引擎: 目标关键词="${KEYWORD}", 最大页数=${MAX_PAGES}, 页面步进间隔=${INTERVAL_MS / 1000}秒`);

const task = await taskSpace('boss-jobs-continuous');
const page = task.page('p1');

const allItems = [];
const seenJobIds = new Set();
let abortedReason = null;

try {
  // 1. 打开 Boss直聘 全国大模型检索页
  const searchUrl = `https://www.zhipin.com/web/geek/job?query=${encodeURIComponent(KEYWORD)}&city=100010000`;
  console.log(`[boss-pipeline] 正在导航至: ${searchUrl}`);
  await page.goto(searchUrl);
  await page.waitForTimeout(4000);

  for (let currentPage = 1; currentPage <= MAX_PAGES; currentPage++) {
    console.log(`[boss-pipeline] 正在提取第 ${currentPage} / ${MAX_PAGES} 页...`);

    // 2. 页面风控/验证码检测
    const pageCheck = await page.evaluate(() => {
      const text = document.body.innerText || '';
      const hasCaptcha = /安全验证|验证码|行为验证|网络异常|请登录后再试/.test(text);
      return { hasCaptcha, title: document.title, url: location.href };
    });

    if (pageCheck.hasCaptcha) {
      abortedReason = 'platform_verification_detected';
      console.warn(`[boss-pipeline] 检测到平台安全验证或登录提示，安全挂起当前采集任务`);
      break;
    }

    // 3. 只读解析当前页岗位卡片
    const pageItems = await page.evaluate(() => {
      const cards = document.querySelectorAll('.job-card-wrapper, .job-card-body, [class*="job-card"]');
      const list = [];
      for (const c of cards) {
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
          list.push({
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
      return list;
    });

    let newCountInPage = 0;
    for (const item of pageItems) {
      const dedupeKey = `${item.title}|${item.company}|${item.url}`;
      if (!seenJobIds.has(dedupeKey)) {
        seenJobIds.add(dedupeKey);
        allItems.push(item);
        newCountInPage++;
      }
    }

    console.log(`[boss-pipeline] 第 ${currentPage} 页提取完毕: 本页获取 ${pageItems.length} 条，去重新增 ${newCountInPage} 条 (累计 ${allItems.length} 条)`);

    // 4. 若已达到最大页数，结束
    if (currentPage >= MAX_PAGES) {
      break;
    }

    // 5. 寻找并点击下一页按钮
    const hasNext = await page.evaluate(() => {
      const nextBtn = document.querySelector('.ui-icon-arrow-right, a.next:not(.disabled), button.next:not([disabled])');
      return !!nextBtn;
    });

    if (!hasNext) {
      console.log(`[boss-pipeline] 已到达最后一页，正常结束分页`);
      break;
    }

    console.log(`[boss-pipeline] 严格节流等待 ${INTERVAL_MS / 1000} 秒，防止触发平台风控...`);
    await page.waitForTimeout(INTERVAL_MS);

    // 触发点击下一页
    const clickSuccess = await page.evaluate(() => {
      const nextBtn = document.querySelector('.ui-icon-arrow-right, a.next:not(.disabled), button.next:not([disabled])');
      if (nextBtn) {
        nextBtn.click();
        return true;
      }
      return false;
    });

    if (!clickSuccess) {
      console.log(`[boss-pipeline] 未能点击下一页，结束循环`);
      break;
    }

    // 等待新页面渲染
    await page.waitForTimeout(3000);
  }
} catch (err) {
  console.error(`[boss-pipeline] 采集过程发生错误: ${err.message}`);
  abortedReason = err.message;
} finally {
  // 6. 持久化输出已抓取的结果数据账本
  const outputData = {
    captured_at: new Date().toISOString(),
    keyword: KEYWORD,
    total_captured: allItems.length,
    pages_completed: seenJobIds.size,
    aborted_reason: abortedReason,
    source: 'platform:boss',
    items: allItems,
  };

  await fs.writeFile(OUT_FILE, JSON.stringify(outputData, null, 2), 'utf8');
  console.log(`[boss-pipeline] 批次数据落盘完成! 累计提取 ${allItems.length} 条记录，已保存至: ${OUT_FILE}`);

  await task.finish({ keep: [] });
}

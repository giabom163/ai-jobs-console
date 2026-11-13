// Ego Lite Boss直聘大模型平滑持续采集引擎 (UTF-8, Node.js ESM)
// 设计目标: 支持境内核心城市轮转调度、历史指纹去重早停(Early-Stop)、步进多页、每页严格间隔 22 秒防风控平滑采集。

const fs = await import('node:fs/promises');
const path = await import('node:path');

// 1. 运行参数与境内城市代码映射
const KEYWORD = process.env.BOSS_KEYWORD || '大模型';
const MAX_PAGES = parseInt(process.env.BOSS_MAX_PAGES || '5', 10);
const INTERVAL_MS = Math.max(22000, parseInt(process.env.BOSS_INTERVAL_MS || '22000', 10));
const OUT_FILE = process.env.BOSS_OUT_FILE || 'C:/Users/Lenovo/.codex/documents/ai-jobs-validation-20261102/boss-continuous-batch.json';
const FINGERPRINT_FILE = path.resolve('tools/boss_seen_fingerprints.json');

// 境内核心城市轮转表 (严格中国境内城市)
const CORE_CITIES = [
  { name: '全国', code: '100010000' },
  { name: '北京', code: '101010100' },
  { name: '上海', code: '101020100' },
  { name: '深圳', code: '101280600' },
  { name: '杭州', code: '101210100' },
  { name: '广州', code: '101280100' },
];

const TARGET_CITY_CODE = process.env.BOSS_CITY_CODE || CORE_CITIES[0].code;
const targetCityObj = CORE_CITIES.find(c => c.code === TARGET_CITY_CODE) || CORE_CITIES[0];

console.log(`[boss-pipeline] 启动平滑采集引擎: 目标城市=${targetCityObj.name}(${targetCityObj.code}), 关键词="${KEYWORD}", 最大页数=${MAX_PAGES}, 页面步进间隔=${INTERVAL_MS / 1000}秒`);

// 2. 加载历史指纹缓存 (用于早停与去重)
let historicalFingerprints = new Set();
try {
  const fpRaw = await fs.readFile(FINGERPRINT_FILE, 'utf8');
  const fpArr = JSON.parse(fpRaw);
  if (Array.isArray(fpArr)) {
    historicalFingerprints = new Set(fpArr);
    console.log(`[boss-pipeline] 已载入历史岗位指纹 ${historicalFingerprints.size} 条`);
  }
} catch (e) {
  console.log(`[boss-pipeline] 首次运行或指纹库尚不存在，将新建指纹记录`);
}

const task = await taskSpace('boss-jobs-continuous');
const page = task.page('p1');

const allItems = [];
const currentRunSeen = new Set();
let abortedReason = null;
let earlyStopped = false;

try {
  // 3. 打开 Boss直聘 指定境内城市检索页
  const searchUrl = `https://www.zhipin.com/web/geek/job?query=${encodeURIComponent(KEYWORD)}&city=${targetCityObj.code}`;
  console.log(`[boss-pipeline] 正在导航至: ${searchUrl}`);
  await page.goto(searchUrl);
  await page.waitForTimeout(4000);

  for (let currentPage = 1; currentPage <= MAX_PAGES; currentPage++) {
    console.log(`[boss-pipeline] 正在提取第 ${currentPage} / ${MAX_PAGES} 页...`);

    // 4. 页面风控/验证码检测
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

    // 5. 只读解析当前页岗位卡片
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
    let duplicateHistoryCount = 0;

    for (const item of pageItems) {
      const fpKey = `${item.title}#${item.company}#${item.location}`;
      if (historicalFingerprints.has(fpKey)) {
        duplicateHistoryCount++;
      }
      if (!currentRunSeen.has(fpKey)) {
        currentRunSeen.add(fpKey);
        allItems.push(item);
        if (!historicalFingerprints.has(fpKey)) {
          newCountInPage++;
        }
      }
    }

    console.log(`[boss-pipeline] 第 ${currentPage} 页解析: 获取 ${pageItems.length} 条，历史已采重复 ${duplicateHistoryCount} 条，纯新增 ${newCountInPage} 条 (本轮累计 ${allItems.length} 条)`);

    // 6. 早停判断: 如果整页所有岗位在历史指纹库中全部已存在，触发早停保护，节省翻页和防风控消耗
    if (pageItems.length > 0 && duplicateHistoryCount === pageItems.length && currentPage > 1) {
      console.log(`[boss-pipeline] [早停触发] 第 ${currentPage} 页数据全为历史已采岗位，已触达增量边界，智能停止继续翻页。`);
      earlyStopped = true;
      break;
    }

    if (currentPage >= MAX_PAGES) {
      break;
    }

    // 7. 寻找并点击下一页按钮
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

    await page.waitForTimeout(3000);
  }
} catch (err) {
  console.error(`[boss-pipeline] 采集过程发生错误: ${err.message}`);
  abortedReason = err.message;
} finally {
  // 8. 更新并持久化历史指纹 (保持上限 2000 条)
  for (const item of allItems) {
    const fpKey = `${item.title}#${item.company}#${item.location}`;
    historicalFingerprints.add(fpKey);
  }
  const fpToSave = Array.from(historicalFingerprints).slice(-2000);
  try {
    await fs.writeFile(FINGERPRINT_FILE, JSON.stringify(fpToSave, null, 2), 'utf8');
    console.log(`[boss-pipeline] 指纹库已更新，当前缓存指纹数: ${fpToSave.length}`);
  } catch (e) {
    console.warn(`[boss-pipeline] 写入指纹库失败: ${e.message}`);
  }

  // 9. 持久化输出已抓取的结果数据账本
  const outputData = {
    captured_at: new Date().toISOString(),
    city: targetCityObj,
    keyword: KEYWORD,
    total_captured: allItems.length,
    aborted_reason: abortedReason,
    early_stopped: earlyStopped,
    source: 'platform:boss',
    items: allItems,
  };

  try {
    await fs.mkdir(path.dirname(OUT_FILE), { recursive: true });
    await fs.writeFile(OUT_FILE, JSON.stringify(outputData, null, 2), 'utf8');
    console.log(`[boss-pipeline] 批次数据落盘完成! 累计提取 ${allItems.length} 条记录，已保存至: ${OUT_FILE}`);
  } catch (e) {
    console.error(`[boss-pipeline] 写入批次数据失败: ${e.message}`);
  }

  await task.finish({ keep: [] });
}

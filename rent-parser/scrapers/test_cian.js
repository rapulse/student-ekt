// Инспекция ЦИАН: сколько объявлений и какие даты на страницах выдачи (node test_cian.js URL1 URL2 …)
import { launchBrowser, newPage, sleep } from './browser_utils.js';
const urls = process.argv.slice(2);
const browser = await launchBrowser();
try {
  const page = await newPage(browser);
  for (const url of urls) {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(3000);
    const r = await page.evaluate(() => {
      const cfg = (window._cianConfig || {})['frontend-serp'] || [];
      const st = (cfg.find((x) => x.key === 'initialState') || {}).value;
      if (!st) return { err: document.title };
      const offs = st.results.offers;
      return { total: st.results.totalOffers, n: offs.length, url: location.href,
        rows: offs.map((o) => `${o.id} ${o.creationDate.slice(0, 16)} ${o.isAuction ? 'AUC' : ''}${o.isTop3 ? 'TOP' : ''}`) };
    });
    console.log('\n==', url, '\n', r.err || `total ${r.total} n ${r.n} → ${r.url}\n ${r.rows.slice(0, 4).join('\n ')} …\n ${r.rows.slice(-2).join('\n ')}`);
    await sleep(2500);
  }
} finally { await browser.close(); }

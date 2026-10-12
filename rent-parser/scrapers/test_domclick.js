import { launchBrowser } from './browser_utils.js';

const DOMCLICK_URL = 'https://ekaterinburg.domclick.ru/search?deal_type=rent&category=living&offer_type=flat&from=main-page-lease';

async function testDomclick() {
  console.log('Тест Домклик: инспекция данных...');
  const browser = await launchBrowser({ headless: false });
  const page = await browser.newPage();
  
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
  });

  try {
    await page.goto(DOMCLICK_URL, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await new Promise(r => setTimeout(r, 6000));
    
    const dump = await page.evaluate(() => {
      // 1. Проверяем глобальные объекты состояния
      const stateKeys = Object.keys(window).filter(k => 
        k.toLowerCase().includes('state') || 
        k.toLowerCase().includes('data') || 
        k.toLowerCase().includes('initial') ||
        k.toLowerCase().includes('redux')
      );

      // 2. Ищем все ссылки, которые ведут на карточки квартир
      const links = Array.from(document.querySelectorAll('a')).map(a => a.href).filter(h => 
        h.includes('/card/') || h.includes('/offer/') || h.includes('/arenda/') || /\/\d{6,}/.test(h)
      );

      // 3. Ищем элементы с ценами и заголовками
      const priceTexts = Array.from(document.querySelectorAll('*')).filter(el => 
        el.children.length === 0 && /\d+[\s\u00A0]*₽/.test(el.textContent)
      ).map(el => el.textContent.trim()).slice(0, 5);

      // 4. Селекторы data-e2e-id
      const e2e = Array.from(document.querySelectorAll('[data-e2e-id]')).map(el => el.getAttribute('data-e2e-id'));

      return {
        title: document.title,
        stateKeys,
        matchingLinksCount: links.length,
        sampleLinks: Array.from(new Set(links)).slice(0, 10),
        samplePrices: priceTexts,
        e2eMarkers: Array.from(new Set(e2e)).slice(0, 15)
      };
    });
    
    console.log('Детальный разбор Домклик:\n', JSON.stringify(dump, null, 2));
  } catch (err) {
    console.error('Ошибка:', err.message);
  } finally {
    console.log('Закрываем браузер.');
    await browser.close();
  }
}

testDomclick();

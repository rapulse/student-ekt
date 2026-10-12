import { launchBrowser } from './browser_utils.js';

const YA_ARENDA_AVITO_URL = 'https://www.avito.ru/brands/arenda-yandex/items/ekaterinburg/kvartiry/sdam/na_dlitelnyy_srok-ASgBAgICAkSSA8gQ8AeQUg?context=H4sIAAAAAAAA_wFFALr_YToxOntzOjg6ImZyb21QYWdlIjtzOjQwOiJleHRlbmRlZF9wcm9maWxlX3RhcmlmZl8yMjQyXzIyNDJfc2VhcmNoIjt9K7b1ZkUAAAA&sellerId=805b663a5bf4ebe779d04008b192e653';

async function test() {
  console.log('Запуск Google Chrome...');
  const browser = await launchBrowser({ headless: false });
  const page = await browser.newPage();
  
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
  });
  
  console.log('Открываем страницу Яндекс Аренды на Авито...');
  try {
    await page.goto(YA_ARENDA_AVITO_URL, { waitUntil: 'networkidle2', timeout: 30000 });
    console.log('Заголовок страницы:', await page.title());
    
    const info = await page.evaluate(() => {
      // Ищем все ссылки с /kvartiry/ или с id
      const links = Array.from(document.querySelectorAll('a[href*="/ekaterinburg/kvartiry/"]')).map(a => ({
        href: a.href,
        text: a.textContent.trim().replace(/\s+/g, ' ').slice(0, 100)
      }));

      // Проверяем различные селекторы карточек
      const markers = Array.from(document.querySelectorAll('[data-marker]')).map(el => el.getAttribute('data-marker'));
      const uniqueMarkers = Array.from(new Set(markers)).filter(m => m && (m.includes('item') || m.includes('catalog') || m.includes('card')));

      return {
        url: window.location.href,
        linksCount: links.length,
        sampleLinks: links.slice(0, 5),
        uniqueMarkers: uniqueMarkers.slice(0, 15),
        bodySnippet: document.body.innerText.slice(0, 500).replace(/\n+/g, ' | ')
      };
    });
    
    console.log('Данные со страницы:\n', JSON.stringify(info, null, 2));
  } catch (err) {
    console.error('Ошибка при тесте:', err.message);
  } finally {
    console.log('Закрываем браузер.');
    await browser.close();
  }
}

test();

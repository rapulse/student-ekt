/**
 * Сборщик Авито через Google Chrome (puppeteer-core).
 *
 *   node avito.js --mode full|inc --known known.json --out out.json [--max-pages N] [--max-enrich N]
 *
 * Один парсер общей выдачи «на длительный срок» (фильтр пользователя), сортировка по дате (s=104),
 * листаем ?p=N. full — вся выдача (первый прогон), inc — пока на странице есть новые ID.
 * В карточке выдачи есть адрес, отметки («Проверено в Росреестре», «Собственник»…), залог / комиссия /
 * ЖКУ, продавец (если Авито его показывает). Координат нет — их добавит геокодер в Python.
 * Яндекс Аренда приходит в этой же выдаче: узнаём её по продавцу «Яндекс Аренда» или тексту описания.
 * Карточки объявлений открываем только выборочно (отмеченные «интересно» и компактные без адреса).
 */
import {
  launchBrowser, newPage, parseArgs, readKnown, writeOut, ensureContent, sleep, jitter,
} from './browser_utils.js';

// Вся выдача «на длительный срок», по дате. Фильтр пользователя (f=…: студия/1к/2к + «Проверены в Росреестре»,
// ~2400) отсекал Яндекс Аренду (у неё нет отметки Росреестра) и 3к — поэтому собираем всё (~4300),
// а отметка «Проверено в Росреестре» сохраняется и фильтруется в просмотрщике.
export const AVITO_REALTY_URL = 'https://www.avito.ru/ekaterinburg/kvartiry/sdam/na_dlitelnyy_srok-ASgBAgICAkSSA8gQ8AeQUg?localPriority=0&s=104';

// inc: листаем, пока на странице есть новые ID; 10 стр. = 500 объявлений — с запасом на сутки
const MAX_PAGES = { full: 100, inc: 10 };
// Сколько карточек открывать за прогон (каждая ~5 с). Остальные — в следующие прогоны.
const MAX_ENRICH = { full: 100, inc: 40 };

const num = (s) => {
  const d = String(s ?? '').replace(/[^\d]/g, '');
  return d ? parseInt(d, 10) : null;
};

/** «1-к. квартира, 43 м², 4/5 эт.» / «Квартира-студия, 26,6 м², 2/18 эт.» / «Комната 14 м² в 3-к., 2/5 эт.» */
export function parseTitle(t) {
  const s = (t || '').replace(/ /g, ' ');
  const area = s.match(/([\d.,]+)\s*м²/);
  const floor = s.match(/(\d+)\/(\d+)\s*эт/);
  let category = 'квартира';
  let rooms = null;
  if (/^комната/i.test(s)) { category = 'Комната'; }
  else if (/студи/i.test(s)) { category = 'Студия'; rooms = 0; }
  else {
    const m = s.match(/(\d+)-к\./);
    if (m) { rooms = +m[1]; category = `${rooms}к`; }
    else if (/своб\. планировк/i.test(s)) category = 'своб. планировка';
  }
  return {
    category, rooms,
    area: area ? parseFloat(area[1].replace(',', '.')) : null,
    floor: floor ? `${floor[1]}/${floor[2]}` : '',
  };
}

/** «Залог 36 000 ₽ · Без комиссии · ЖКУ 6 000 ₽ + счетчики» → условия. null = не указано. */
export function parseTerms(text) {
  const s = (text || '').replace(/ /g, ' ');
  const r = { deposit: null, commission_pct: null, commission_rub: null, utilities_included: null, utilities_rub: null, utilities_info: '' };
  if (/без залога/i.test(s)) r.deposit = 0;
  else { const m = s.match(/залог:?\s*([\d\s]+)\s*₽/i); if (m) r.deposit = num(m[1]); }
  if (/без комиссии/i.test(s)) { r.commission_pct = 0; r.commission_rub = 0; }
  else {
    const p = s.match(/комиссия:?\s*(\d+)\s*%/i);
    const rub = s.match(/комиссия:?\s*([\d\s]+)\s*₽/i);
    if (p) r.commission_pct = +p[1];
    else if (rub) r.commission_rub = num(rub[1]);
  }
  const zh = s.match(/ЖКУ[^·\n]*/i);
  if (zh) {
    r.utilities_info = zh[0].trim();
    if (/включены/i.test(zh[0])) { r.utilities_included = true; r.utilities_rub = 0; }
    else { const m = zh[0].match(/ЖКУ\s*([\d\s]+)\s*₽/i); if (m) { r.utilities_rub = num(m[1]); r.utilities_included = r.utilities_rub === 0 && !/счетчик|счётчик/i.test(zh[0]); } }
  }
  return r;
}

/** Параметры карточки объявления («Залог: 0 ₽», «Комиссия: 0 %», «Другие ЖКУ: 6000 ₽/мес.») → условия. */
function termsFromParams(params) {
  const p = Object.fromEntries(params.map((l) => { const i = l.indexOf(':'); return [l.slice(0, i).trim(), l.slice(i + 1).trim()]; }));
  const r = { deposit: null, commission_pct: null, commission_rub: null, utilities_included: null, utilities_rub: null, utilities_info: '' };
  if (p['Залог'] != null) r.deposit = num(p['Залог']) ?? 0;
  if (p['Комиссия'] != null) {
    if (/%/.test(p['Комиссия'])) r.commission_pct = num(p['Комиссия']) ?? 0;
    else r.commission_rub = num(p['Комиссия']) ?? 0;
  }
  const meter = p['По счётчикам'] || p['По счетчикам'];
  const other = p['Другие ЖКУ'] || p['ЖКУ'];
  r.utilities_info = [other && `ЖКУ ${other}`, meter && `счётчики: ${meter}`].filter(Boolean).join(', ');
  if (other != null) {
    r.utilities_rub = /включ/i.test(other) ? 0 : num(other);
    r.utilities_included = r.utilities_rub === 0 && (!meter || /включ/i.test(meter));
  }
  return { terms: r, params: p };
}

// ---------- разбор страниц (выполняется в браузере) ----------

function extractSerp() {
  const txt = (el) => (el ? el.innerText.replace(/ /g, ' ').trim() : '');
  return Array.from(document.querySelectorAll('[data-marker="item"], [data-marker^="item_list_with_filters/item("]')).map((card) => {
    const a = card.querySelector('a[data-marker="item-title"]') || card.querySelector('a[itemprop="url"]');
    const badges = Array.from(card.querySelectorAll('[data-marker^="iva-item/"], [data-marker^="badge-title"]'))
      .map(txt).filter((t) => t && t.length < 40);
    const addrBox = card.querySelector('[data-marker="item-address"]');
    const street = txt(card.querySelector('[data-marker="street_link"]'));
    const addrLines = addrBox ? txt(addrBox).split('\n').map((l) => l.trim()).filter(Boolean) : [];
    const descMeta = card.querySelector('meta[itemprop="description"]');
    const descP = Array.from(card.querySelectorAll('[class*="bottomBlock"] p, [class*="ivaItemRedesign"] p'))
      .filter((p) => !/₽/.test(p.innerText)).sort((a, b) => b.innerText.length - a.innerText.length)[0];
    // Блок продавца без data-marker: «Имя / N завершённых объявлений / Документы проверены»
    const lines = txt(card).split('\n').map((l) => l.trim()).filter(Boolean);
    const di = lines.findIndex((l) => /завершённ|завершен/i.test(l));
    return {
      id: card.getAttribute('data-item-id'),
      href: a ? a.href.split('?')[0] : '',
      title: a ? (a.getAttribute('title') || txt(a)).replace(/ в Екатеринбурге$/, '') : '',
      price: txt(card.querySelector('[data-marker="item-price-value"]')) || (card.querySelector('meta[itemprop="price"]') || {}).content || '',
      params: txt(card.querySelector('[data-marker="item-specific-params"]')),
      address: addrLines[0] || street,
      addrExtra: addrLines.slice(1),
      badges: [...new Set(badges)],
      date: txt(card.querySelector('[data-marker="item-date"]')),
      description: descMeta ? descMeta.content : txt(descP),
      seller: di > 0 ? lines[di - 1] : '',
      sellerDone: di >= 0 ? lines[di] : '',
      cardText: txt(card).slice(0, 1500),
    };
  }).filter((x) => x.id && /\/ekaterinburg\//.test(x.href));
}

function extractItemPage() {
  // Полные данные объявления лежат в JSON гидратации страницы; DOM — запасной вариант
  const strip = (h) => String(h || '').replace(/<br\s*\/?>/g, '\n').replace(/<\/p>/g, '\n').replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ').trim();
  const kv = (block) => ((block && (block.data || block).items) || []).map((x) => `${x.title}: ${x.description}`);
  let b = null;
  try { b = window.__staticRouterHydrationData.loaderData['catalog-or-main-or-item'].buyerItem; } catch (e) { b = null; }
  if (b && b.item) {
    const it = b.item;
    const geo = it.geo || {};
    const sellerBadges = [
      ...((it.sellerBadgeBar && it.sellerBadgeBar.iconsBadges) || []).map((x) => x.title),
      ...((it.sellerBadgeBar && it.sellerBadgeBar.badges) || []).map((x) => x.title),
      ...((b.realtyExpertBlock && b.realtyExpertBlock.badges) || []).map((x) => x.title),
    ].filter(Boolean);
    const subtitle = ((b.realtyExpertBlock && b.realtyExpertBlock.subtitle) || []).flat().map((x) => x.text).filter((t) => t && t !== 'в сети');
    return {
      via: 'json',
      lat: geo.coords ? geo.coords.lat : null,
      lon: geo.coords ? geo.coords.lng : null,
      address: geo.address || it.address || '',
      metro: (geo.references || []).map((r) => `${r.content}${r.afterWithIcon ? ', ' + r.afterWithIcon.text : ''}`).slice(0, 2).join('; '),
      params: [...kv(it.rentTermsParams), ...kv(b.paramsDto), ...kv(it.houseParams), ...kv(it.rulesParams)],
      seller: (b.seller && b.seller.name) || '',
      sellerKind: subtitle.join(', '),
      sellerIsCompany: !!(b.seller && b.seller.isCompany),
      sellerBadges,
      termsLine: strip(b.priceDataDTO && b.priceDataDTO.depositCommission),
      sellerDesc: (b.priceDataDTO && b.priceDataDTO.sellerDesc) || '',
      priceHistory: b.priceHistory || null,
      views: b.viewStat || null,
      description: strip(it.description),
      date: it.sortFormatedDate || '',
      price: it.price || null,
      active: !(it.isClosed || it.isArchived || it.isDeleted || it.isExpired || it.isFinished),
    };
  }
  const txt = (el) => (el ? el.innerText.replace(/ /g, ' ').trim() : '');
  const map = document.querySelector('[data-marker="item-map-wrapper"]');
  let address = '';
  for (const el of document.querySelectorAll('span, div, p')) {
    if (el.children.length > 2) continue;
    const t = txt(el);
    if (/^(Свердловская обл\.,\s*)?Екатеринбург,/.test(t) && t.length < 140) { address = t.split('\n')[0]; break; }
  }
  return {
    via: 'dom',
    lat: map ? parseFloat(map.getAttribute('data-map-lat')) : null,
    lon: map ? parseFloat(map.getAttribute('data-map-lon')) : null,
    address,
    params: Array.from(document.querySelectorAll('[data-marker="item-view/item-params"] li')).map(txt),
    sellerBadges: [],
    description: txt(document.querySelector('[data-marker="item-view/item-description"]')).replace(/^Описание\s*/, ''),
    date: txt(document.querySelector('[data-marker="item-view/item-date"]')),
  };
}

// ---------- запись ----------

/** Кто сдаёт: по отметке «Собственник», имени продавца (Домклик, «АН …», «агентство») */
function sellerType(it, owner) {
  if (owner) return 'собственник';
  const n = (it.seller || '').toLowerCase();
  if (/домклик/.test(n)) return 'Домклик';
  if (/агентств|недвижимост|риелт|риэлт|\bан\b|realty|estate|групп|компания|ооо|ип /.test(n)) return 'агентство';
  return it.seller ? 'частное лицо / агент' : '';
}

// Описания Яндекс Аренды пишут вперемешку с латиницей («Яндeкc Аpeндoй») — приводим к кириллице
const LAT2CYR = { a: 'а', c: 'с', e: 'е', o: 'о', p: 'р', x: 'х', y: 'у', k: 'к', m: 'м', t: 'т', h: 'н', b: 'в', A: 'А', C: 'С', E: 'Е', O: 'О', P: 'Р', X: 'Х', Y: 'У', K: 'К', M: 'М', T: 'Т', H: 'Н', B: 'В' };
const cyr = (t) => String(t || '').replace(/[a-zA-Z]/g, (ch) => LAT2CYR[ch] || ch);
// Шаблон описаний Яндекс Аренды: «…на 20 этаже в 26-этажном доме на срок от 11 месяцев»
const YA_TEMPLATE = /этаже в \d+-этажном доме на срок от \d+ месяц/i;
export const isYaArenda = (it) => /^яндекс\s*аренда$/i.test((it.seller || '').trim())
  || /яндекс\s*аренд/i.test(cyr(it.description))
  || (YA_TEMPLATE.test(cyr(it.description)) && !(it.badges || []).some((b) => /собственник/i.test(b)));

function toRecord(it, isArenda = isYaArenda(it)) {
  const t = parseTitle(it.title);
  const terms = parseTerms(it.params);
  if (isArenda) {
    // Условия Яндекс Аренды; уточняются из карточки, если открывали её
    if (terms.commission_pct == null && terms.commission_rub == null) { terms.commission_pct = 0; terms.commission_rub = 0; }
  }
  const owner = it.badges.some((b) => /собственник/i.test(b));
  const metro = (it.addrExtra || []).find((l) => /мин\.?$/.test(l)) || '';
  const district = (it.addrExtra || []).find((l) => /^р-н/.test(l)) || '';
  return {
    id: `A${it.id}`,
    source: isArenda ? 'Авито (Яндекс Аренда)' : 'Авито',
    source_type: isArenda ? 'arenda_program' : 'regular',
    url: it.href,
    title: it.title,
    category: t.category,
    price: num(it.price),
    ...terms,
    rooms: t.rooms,
    area: t.area,
    floor: t.floor,
    address: (it.address || '').replace(/^Свердловская обл\.,\s*/, ''),
    district,
    metro,
    lat: null,
    lon: null,
    author_type: isArenda ? 'Яндекс Аренда' : sellerType(it, owner),
    author_name: isArenda ? 'Яндекс Аренда' : (it.seller || ''),
    seller_stats: it.sellerDone || '',
    badges: it.badges.join(' · '),
    description: it.description || '',
    published: it.date,
    raw_data: it,
  };
}

function mergeItemPage(rec, d) {
  if (d.lat && d.lon) { rec.lat = d.lat; rec.lon = d.lon; rec.geo_source = 'сайт'; }
  if (d.address) rec.address = d.address.replace(/^Свердловская обл\.,\s*/, '').replace(/^Екатеринбург,\s*/, '');
  if (d.metro) rec.metro = d.metro;
  const { terms, params } = termsFromParams(d.params || []);
  for (const [k, v] of Object.entries(terms)) if (v != null && v !== '') rec[k] = v;
  if (d.description) rec.description = d.description;
  if (d.price) rec.price = d.price;
  const t = parseTitle(rec.title);
  if (rec.rooms == null && params['Количество комнат']) rec.rooms = /студ/i.test(params['Количество комнат']) ? 0 : num(params['Количество комнат']);
  if (!rec.area && t.area) rec.area = t.area;
  if (/^яндекс\s*аренда$/i.test(d.seller || '') || isYaArenda({ description: d.description })) {
    Object.assign(rec, { source: 'Авито (Яндекс Аренда)', source_type: 'arenda_program', author_type: 'Яндекс Аренда', author_name: 'Яндекс Аренда' });
  }
  if (d.seller && rec.source_type !== 'arenda_program') {
    if (d.seller !== 'Пользователь') rec.author_name = d.seller;   // без входа Авито скрывает имя частника
    const kind = `${d.sellerKind || ''} ${d.sellerDesc || ''}`;
    if (/домклик/i.test(d.seller)) rec.author_type = 'Домклик';
    else if (/собственник/i.test(kind) || /собственник/i.test(rec.badges || '')) rec.author_type = 'собственник';
    else if (/агентств/i.test(kind) || d.sellerIsCompany) rec.author_type = 'агентство';
    else if (/агент|риелтор|риэлтор/i.test(kind)) rec.author_type = 'агент';
    else rec.author_type = rec.author_type || 'частное лицо / агент';
  }
  const extra = [...(d.sellerBadges || [])];
  if (d.sellerKind) extra.push(d.sellerKind);
  rec.badges = [...new Set([...(rec.badges ? rec.badges.split(' · ') : []), ...extra])].filter(Boolean).join(' · ');
  if (d.views) rec.views = d.views.totalViews;
  if (d.active === false) rec.closed_on_site = true;
  rec.raw_data = { ...rec.raw_data, item_page: { ...d, params_map: params } };
  rec.enriched = true;
  return rec;
}

/** Открыть карточки по одной и дополнить записи (адрес, координаты, продавец, условия). */
async function enrich(page, recs, label) {
  let n = 0;
  for (const rec of recs) {
    n++;
    try {
      await page.goto(rec.url, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await jitter(1500, 3000);
      const ok = await ensureContent(page, (pg) => pg.evaluate(() => !!(window.__staticRouterHydrationData || document.querySelector('[data-marker="item-view/item-params"]'))), `${label} (карточка)`);
      if (!ok) break;
      mergeItemPage(rec, await page.evaluate(extractItemPage));
      console.log(`  ${n}/${recs.length} ${rec.id}: ${rec.address || 'адрес ?'} · ${rec.author_type || '?'} ${rec.author_name ? '«' + rec.author_name + '»' : ''} · залог ${rec.deposit ?? '?'}`);
    } catch (e) {
      console.log(`  ${n}/${recs.length} ${rec.id}: ошибка ${e.message}`);
    }
    await jitter(2500, 5000);
  }
}

// ---------- сценарии ----------

const hasItems = (page) => page.evaluate(() =>
  document.querySelectorAll('[data-marker="item"], [data-marker^="item_list_with_filters/item("]').length > 0);

/** Листает выдачу Авито (?p=N), возвращает Map id → запись. Одинаково для общего поиска и поиска «яндекс аренда». */
async function scrapeSerp(page, baseUrl, label, mode, known, maxPages) {
  const out = new Map();
  let complete = false;
  for (let p = 1; p <= maxPages; p++) {
    const url = p === 1 ? baseUrl : `${baseUrl}&p=${p}`;
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await jitter(2500, 4500);
    if (!(await ensureContent(page, hasItems, label))) {
      // пустая страница за концом выдачи — это не капча
      const empty = await page.evaluate(() => /ничего не найдено|нет объявлений/i.test(document.body.innerText));
      if (empty) complete = true;
      break;
    }
    // Нижние карточки рендерятся лениво — прокручиваем страницу, чтобы появились адрес и условия
    await page.evaluate(async () => {
      for (let y = 0; y < document.body.scrollHeight; y += 500) { window.scrollTo(0, y); await new Promise((r) => setTimeout(r, 250)); }
    });
    await sleep(800);
    const items = await page.evaluate(extractSerp);
    let fresh = 0;
    for (const it of items) {
      if (!known[`A${it.id}`] && !out.has(`A${it.id}`)) fresh++;
      out.set(`A${it.id}`, toRecord(it));
    }
    const hasNext = await page.evaluate(() => !!document.querySelector('[data-marker="pagination-button/nextPage"]:not([aria-disabled="true"])'));
    console.log(`[${label}] стр. ${p}: карточек ${items.length}, новых ${fresh}, всего ${out.size}${hasNext ? '' : ' (последняя)'}`);
    if (!hasNext) { complete = true; break; }
    if (mode === 'inc' && fresh === 0) break;
    await jitter(3000, 7000);
  }
  return { out, complete };
}

/** Карточки открываем выборочно (не все 3–4 тыс.): отмеченные «интересно» и компактные без адреса. */
async function enrichSelected(page, out, known, maxEnrich, label) {
  const recs = [...out.values()];
  const need = (r) => !(known[r.id] && known[r.id].enriched);
  const todo = [
    ...recs.filter((r) => need(r) && known[r.id] && known[r.id].want),
    ...recs.filter((r) => need(r) && !r.address),
  ].filter((r, i, arr) => arr.indexOf(r) === i);
  // «Интересные» из прошлых прогонов, которых нет на просмотренных сейчас страницах
  for (const [id, k] of Object.entries(known)) {
    if (k.want && !k.enriched && k.url && !out.has(id)) {
      const stub = { id, source: k.source || 'Авито', source_type: k.source_type || 'regular', url: k.url, title: k.title || '', raw_data: {} };
      out.set(id, stub);
      todo.unshift(stub);
    }
  }
  todo.splice(maxEnrich);
  if (todo.length) {
    console.log(`[${label}] открываем карточки: ${todo.length} (без адреса — ${todo.filter((r) => !r.address).length})`);
    await enrich(page, todo, label);
  }
}

async function scrapeRealty(page, mode, known, maxPages, maxEnrich) {
  const { out, complete } = await scrapeSerp(page, AVITO_REALTY_URL, 'Авито', mode, known, maxPages);
  await enrichSelected(page, out, known, maxEnrich, 'Авито');
  return { items: [...out.values()], complete };
}

if (process.argv[1] && process.argv[1].endsWith('avito.js')) {
  const args = parseArgs();
  const known = readKnown(args.known);
  const browser = await launchBrowser({ headless: args.flags.has('--headless') });
  let result = { items: [], complete: false };
  try {
    const page = await newPage(browser);
    await page.setViewport({ width: 1600, height: 1000 });   // колонка продавца видна только на широком окне
    result = await scrapeRealty(page, args.mode, known, args.maxPages || MAX_PAGES[args.mode] || 5, args.maxEnrich ?? MAX_ENRICH[args.mode] ?? 40);
  } catch (e) {
    console.error(`[Авито] ошибка: ${e.message}`);
  } finally {
    await browser.close();
  }
  writeOut(args.out, { source: 'avito', ...result });
}

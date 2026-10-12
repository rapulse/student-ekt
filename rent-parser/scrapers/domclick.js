/**
 * Сборщик Домклика (аренда квартир, Екатеринбург) через Google Chrome (puppeteer-core).
 *
 *   node domclick.js --mode full|inc --known known.json --out out.json
 *
 * Страница выдачи содержит window.__SSR_STATE__ со всеми данными объявлений: координаты, адрес,
 * описание, «собственник», «проверено в Росреестре», есть ли комиссия, даты публикации.
 * Листаем offset=0,20,40… с сортировкой по дате публикации.
 * Залог и размер комиссии Домклик в выдаче не даёт — вытаскиваем из текста (помечаем «из текста»).
 */
import {
  launchBrowser, newPage, parseArgs, readKnown, writeOut, ensureContent, jitter,
} from './browser_utils.js';

const BASE = 'https://ekaterinburg.domclick.ru/search?deal_type=rent&category=living&offer_type=flat&sort=published&sort_dir=desc';
const PAGE_SIZE = 20;
// sort=published, а не updated: агентства раз в сутки перевыгружают фиды, и «обновлённые» старые
// объявления забивают верх выдачи. Изменения цен ловит еженедельный --full.
const MAX_PAGES = { full: 120, inc: 10 };

const num = (s) => { const d = String(s ?? '').replace(/[^\d]/g, ''); return d ? parseInt(d, 10) : null; };

/** Залог / комиссия / КУ из описания. null — в тексте нет. */
export function termsFromText(text, price) {
  const low = (text || '').toLowerCase().replace(/ /g, ' ');
  const r = { deposit: null, commission_pct: null, commission_rub: null, utilities_included: null, utilities_info: '' };
  if (/без\s+(залога|депозита)/.test(low)) r.deposit = 0;
  else {
    const m = low.match(/(?:залог|депозит|страховой взнос)[^\d\n]{0,25}(\d{1,3}(?:[ .]?\d{3})+|\d{4,6})/);
    if (m) r.deposit = num(m[1]);
    else if (/(?:залог|депозит)[^\n]{0,20}(?:в размере )?(?:1|одн\w*|месяч\w*)\s*(?:мес|плат|аренд)/.test(low) && price) r.deposit = price;
  }
  const pc = low.match(/комисси\w*[^\d\n%]{0,25}(\d{1,3})\s*%/);
  const rc = low.match(/комисси\w*[^\d\n]{0,25}(\d{1,3}(?:[ .]?\d{3})+|\d{4,6})\s*(?:₽|р|руб)/);
  if (/без\s+комисси/.test(low)) { r.commission_pct = 0; r.commission_rub = 0; }
  else if (pc) r.commission_pct = +pc[1];
  else if (rc) r.commission_rub = num(rc[1]);
  if (/(?:ку|коммунальн\w*)[^\n]{0,20}включен/.test(low) || /всё включено|все включено/.test(low)) { r.utilities_included = true; r.utilities_info = 'включены (из текста)'; }
  else if (/\+\s*(?:ку|коммун|счетчик|счётчик)/.test(low)) { r.utilities_included = false; r.utilities_info = 'отдельно (из текста)'; }
  return r;
}

function extractState() {
  // После гидратации страница удаляет window.__SSR_STATE__ — исполняем исходный скрипт в «песочнице»
  let ssr = window.__SSR_STATE__;
  if (!ssr) {
    const sc = Array.from(document.scripts).find((x) => x.textContent.includes('window.__SSR_STATE__='));
    if (sc) {
      const fake = {};
      try { new Function('window', sc.textContent)(fake); } catch (e) { /* хвост скрипта может ссылаться на браузерные API */ }
      ssr = fake.__SSR_STATE__;
    }
  }
  const st = ssr && ssr.search;
  if (!st) return null;
  const pages = st.pages || {};
  const out = [];
  for (const k of Object.keys(pages)) {
    const p = pages[k];
    for (const id of p.ids || []) out.push(p.entities[id]);
  }
  return { total: st.offersCount || st.paginationTotal || null, offers: out };
}

function toRecord(o) {
  const info = o.objectInfo || {};
  const text = o.description || '';
  const terms = termsFromText(text, o.price);
  if (o.hasRentCommission === false) { terms.commission_pct = 0; terms.commission_rub = 0; }   // флаг сайта
  const rooms = info.rooms;
  const studio = rooms === 0 || /студи/i.test(text.slice(0, 60));
  const category = o.roomsOffered ? 'Комната' : studio ? 'Студия' : rooms ? `${rooms}к` : 'квартира';
  const agent = (o.seller && o.seller.agent) || {};
  const badges = [];
  if (o.isOwner) badges.push('Собственник');
  if (o.isRosreestrApproved) badges.push('Проверено в Росреестре');
  if (agent.isSbolVerified) badges.push('Проверен СберID');
  if (agent.isESIAVerified) badges.push('Проверен Госуслугами');
  if (o.hasRentCommission) badges.push('Возможна комиссия');
  if (o.duplicatesOfferCount) badges.push(`дублей на Домклике: ${o.duplicatesOfferCount}`);
  if (o.hasDiscount) badges.push(`скидка ${o.discountValue || ''}`.trim());
  const addr = ((o.address && o.address.displayName) || '').replace(/^Екатеринбург,\s*/, '');
  const parts = addr.split(',').map((x) => x.trim());
  const district = parts.find((x) => /м-н$|р-н$/.test(x)) || '';
  return {
    id: `D${o.id}`,
    source: 'Домклик',
    source_type: 'regular',
    url: o.path || `https://ekaterinburg.domclick.ru/card/rent__flat__${o.id}`,
    title: `${category}, ${info.area || '?'} м², ${info.floor || '?'}/${(o.house && o.house.floors) || '?'} эт.`,
    category,
    price: o.price || null,
    ...terms,
    utilities_rub: null,
    rooms: studio ? 0 : rooms ?? null,
    area: info.area || null,
    floor: info.floor ? `${info.floor}/${(o.house && o.house.floors) || '?'}` : '',
    address: parts.filter((x) => x !== district).join(', '),
    district,
    metro: ((o.address && o.address.subways) || []).map((s) => s.name || s.title || s).filter(Boolean).slice(0, 2).join(', '),
    lat: o.location ? o.location.lat : null,
    lon: o.location ? o.location.lon : null,
    geo_source: o.location ? 'сайт' : '',
    author_type: o.isOwner ? 'собственник' : agent.isAgency ? 'агентство' : agent.isAgent ? 'агент' : '',
    author_name: agent.fullName || agent.companyName || '',
    badges: badges.join(' · '),
    description: text,
    published: (o.publishedDate || '').slice(0, 10),
    updated_on_site: (o.updatedDate || '').slice(0, 10),
    raw_data: o,
  };
}

async function scrape(page, mode, known, maxPages) {
  const out = new Map();
  let complete = false;
  let total = null;
  for (let p = 0; p < maxPages; p++) {
    const url = p === 0 ? BASE : `${BASE}&offset=${p * PAGE_SIZE}`;
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await jitter(2000, 3500);
    const ok = await ensureContent(page, (pg) => pg.evaluate(() => Array.from(document.scripts).some((x) => x.textContent.includes('window.__SSR_STATE__='))), 'Домклик');
    if (!ok) break;
    const st = await page.evaluate(extractState);
    if (!st || !st.offers.length) { complete = true; break; }
    total = st.total;
    let fresh = 0;
    for (const o of st.offers) {
      if (!o || !o.id) continue;
      const r = toRecord(o);
      if (!known[r.id] && !out.has(r.id)) fresh++;
      out.set(r.id, r);
    }
    const pages = total ? Math.ceil(total / PAGE_SIZE) : '?';
    console.log(`[Домклик] стр. ${p + 1}/${pages}: ${st.offers.length} объявлений, новых ${fresh}, всего ${out.size}`);
    if (total && (p + 1) * PAGE_SIZE >= total) { complete = true; break; }
    if (mode === 'inc' && fresh === 0) break;
    await jitter(2500, 5000);
  }
  return { items: [...out.values()], complete, total };
}

if (process.argv[1] && process.argv[1].endsWith('domclick.js')) {
  const args = parseArgs();
  const known = readKnown(args.known);
  const browser = await launchBrowser({ headless: args.flags.has('--headless') });
  let result = { items: [], complete: false };
  try {
    const page = await newPage(browser);
    result = await scrape(page, args.mode, known, args.maxPages || MAX_PAGES[args.mode] || 5);
  } catch (e) {
    console.error(`[Домклик] ошибка: ${e.message}`);
  } finally {
    await browser.close();
  }
  writeOut(args.out, { source: 'domclick', ...result });
}

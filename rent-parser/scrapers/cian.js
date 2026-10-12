/**
 * Сборщик ЦИАН (долгосрочная аренда квартир и комнат, Екатеринбург) через Google Chrome (puppeteer-core).
 *
 *   node cian.js --mode full|inc --known known.json --out out.json
 *
 * curl получает 403 (антибот), в Chrome страница открывается. Выдача содержит
 * window._cianConfig['frontend-serp'] → initialState.results.offers: координаты, адрес, описание целиком,
 * залог, комиссия (%), КУ (сумма / включены / счётчики), собственник, Росреестр, уровень агента.
 * Карточку открывать не нужно: всё нужное уже есть в выдаче.
 *
 * ЦИАН отдаёт не больше 54 страниц по 28 (~1500) на один запрос. Полный проход режет выдачу
 * по цене: срез, в котором объявлений больше лимита, делится пополам, пока не влезет.
 */
import {
  launchBrowser, newPage, parseArgs, readKnown, writeOut, ensureContent, jitter,
} from './browser_utils.js';

const BASE = 'https://ekb.cian.ru/cat.php?deal_type=rent&engine_version=2&offer_type=flat&region=4743&type=4&sort=creation_date_desc';
const SEARCHES = [
  { name: 'квартиры', url: BASE },
  { name: 'комнаты', url: `${BASE}&room0=1` },
];
const PAGE_SIZE = 28;
const PAGE_LIMIT = 54;           // дальше ЦИАН возвращает на первую страницу
const SLICE_MAX = 1400;          // объявлений в срезе (с запасом от 54 × 28)
const PRICE_TOP = 400_000;       // выше — один срез без верхней границы
const MAX_PAGES = { full: PAGE_LIMIT, inc: 10 };

/** initialState выдачи: {total, offers} или null (капча / другая страница). */
function extractState() {
  const cfg = (window._cianConfig || {})['frontend-serp'] || [];
  const st = (cfg.find((x) => x.key === 'initialState') || {}).value;
  if (!st || !st.results) return null;
  return { total: st.results.totalOffers, offers: st.results.offers || [], url: location.href };
}

const DROP_RAW = ['photos', 'adfoxParams', 'descriptionMinhash', 'descriptionWordsHighlighted', 'videos', 'gaLabel',
  'kp', 'newbuildingCplLayer', 'newbuildingDynamicCalltracking', 'promoInfo', 'auction', 'contextToken'];

function toRecord(o) {
  const b = o.bargainTerms || {};
  const g = o.geo || {};
  const u = o.user || {};
  const q = ((u.agentModerationInfo || {}).qualityLevelInfo) || {};
  const room = o.category === 'roomRent' || o.flatType === 'room';
  const studio = o.flatType === 'studio' || o.flatType === 'openPlan';   // своб. планировка — по сути студия
  const rooms = studio ? 0 : o.roomsCount ?? null;
  const category = room ? 'Комната' : studio ? 'Студия' : rooms ? `${rooms}к` : 'квартира';

  // Условия — поля формы ЦИАН, не текст
  const fee = b.clientFee ?? b.agentFee;
  const ut = b.utilitiesTerms || {};
  const utilRub = ut.includedInPrice ? null : (ut.price || null);
  const utilInfo = ut.includedInPrice
    ? (ut.flowMetersNotIncludedInPrice ? 'включены + счётчики' : 'включены')
    : [utilRub && `ЖКУ ${utilRub} ₽`, ut.flowMetersNotIncludedInPrice && '+ счётчики'].filter(Boolean).join(' ');

  // Адрес: улица + дом (или «Светлый микрорайон, 9»), без области/города/районов/метро
  const parts = (g.address || []).filter((a) => a.geoType === 'street' || a.geoType === 'house'
    || (a.geoType === 'location' && !['Свердловская область', 'Екатеринбург'].includes(a.shortName)));
  const address = parts.map((a) => a.shortName).join(', ')
    || (g.userInput || '').replace(/^.*?Екатеринбург,\s*/, '');
  const dists = (g.districts || []);
  const district = (dists.find((d) => d.type === 'mikroraion') || dists[0] || {}).title || '';
  const coords = g.coordinates || {};

  const owner = o.isByHomeowner === true;
  const badges = [];
  if (owner) badges.push('Собственник');
  if (o.isRosreestrChecked) badges.push('Проверено в Росреестре');
  if (q.hasVerifiedDocuments) badges.push('Документы проверены');
  if (u.isPassportVerified) badges.push('Паспорт проверен');
  if (o.isOnlineRentalAgreementEnabled) badges.push('Онлайн-договор ЦИАН');
  if (b.leaseTermType && b.leaseTermType !== 'longTerm') badges.push('⚠️ на несколько месяцев');
  const prepay = (o.formattedAdditionalInfo || '').match(/предоплата (\d+) мес/);
  if (prepay && +prepay[1] > 1) badges.push(`предоплата ${prepay[1]} мес`);
  if (o.isDuplicatedDescription) badges.push('⚠️ описание повторяется у других');
  if (q.isBlocked) badges.push('⚠️ ЦИАН: аккаунт заблокирован');

  const raw = { ...o };
  for (const k of DROP_RAW) delete raw[k];
  // в geo — сотни ссылок-фильтров (qs): оставляем только сами данные
  raw.geo = { userInput: g.userInput, coordinates: g.coordinates, jk: g.jk && g.jk.name,
    address: (g.address || []).map((a) => a.fullName), districts: dists.map((d) => d.title),
    undergrounds: (g.undergrounds || []).map((m) => ({ name: m.name, time: m.time, transportType: m.transportType })) };
  if (raw.user) delete raw.user.billingInfo;
  const floors = (o.building || {}).floorsCount;
  const area = parseFloat((room && o.roomArea) || o.totalArea) || null;   // у комнаты totalArea — вся квартира

  return {
    id: `C${o.id}`,
    source: 'ЦИАН',
    source_type: 'regular',
    url: (o.fullUrl || `https://ekb.cian.ru/rent/flat/${o.id}/`).split('?')[0],
    title: `${category}, ${area || '?'} м², ${o.floorNumber || '?'}/${floors || '?'} эт.`,
    category,
    price: b.priceRur || b.price || null,
    deposit: b.deposit ?? null,
    commission_pct: fee ?? null,
    commission_rub: fee === 0 ? 0 : null,
    utilities_included: ut.includedInPrice ?? null,
    utilities_rub: utilRub,
    utilities_info: utilInfo,
    rooms: room ? null : rooms,
    area,
    floor: o.floorNumber ? `${o.floorNumber}/${floors || '?'}` : '',
    address,
    district,
    metro: (g.undergrounds || []).slice(0, 2).map((m) => (m.time ? `${m.name} ${m.time} мин` : m.name)).join(', '),
    lat: coords.lat ?? null,
    lon: coords.lng ?? null,
    // скрытый точный адрес → точка примерная, main.py попробует уточнить по адресу
    geo_source: coords.lat == null ? '' : o.isNeedHideExactAddress ? 'приблизительно (ЦИАН)' : 'сайт',
    author_type: owner ? 'собственник' : u.accountType === 'agency' ? 'агентство' : u.isAgent || u.accountType === 'specialist' ? 'агент' : '',
    author_name: u.agencyName || '',
    seller_stats: [q.levelName, q.isSuperAgent && q.levelName !== 'Суперагент' && 'суперагент'].filter(Boolean).join(' · '),
    badges: badges.join(' · '),
    description: o.description || '',
    published: (o.creationDate || '').slice(0, 10),
    updated_on_site: o.addedTimestamp ? new Date(o.addedTimestamp * 1000).toISOString().slice(0, 10) : '',
    raw_data: raw,
  };
}

async function loadPage(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await jitter(1500, 3000);
  const ok = await ensureContent(page, (pg) => pg.evaluate(() => {
    const cfg = (window._cianConfig || {})['frontend-serp'] || [];
    return cfg.some((x) => x.key === 'initialState');
  }), 'ЦИАН');
  return ok ? page.evaluate(extractState) : null;
}

/** Листает один запрос. inc: до первой страницы без новых ID. Возвращает {done, skipped}. */
async function walk(page, label, url, mode, known, out, maxPages) {
  for (let p = 1; p <= Math.min(maxPages, PAGE_LIMIT); p++) {
    const st = await loadPage(page, p === 1 ? url : `${url}&p=${p}`);
    if (!st) return { done: false, skipped: true };
    // за последней страницей ЦИАН молча отдаёт первую — признак конца
    if (p > 1 && !/[?&]p=\d/.test(st.url.replace(/[?&]p=1(?!\d)/, ''))) return { done: true };
    let fresh = 0;
    for (const o of st.offers) {
      if (!o || !o.id) continue;
      const r = toRecord(o);
      if (!known[r.id] && !out.has(r.id)) fresh++;
      out.set(r.id, r);
    }
    const pages = Math.min(PAGE_LIMIT, Math.ceil((st.total || 0) / PAGE_SIZE)) || '?';
    console.log(`[ЦИАН] ${label} стр. ${p}/${pages}: ${st.offers.length} объявлений, новых ${fresh}, всего ${out.size}`);
    if (!st.offers.length || p * PAGE_SIZE >= (st.total || 0)) return { done: true };
    if (mode === 'inc' && fresh === 0) return { done: false };
    await jitter(2000, 4000);
  }
  return { done: false };
}

/** Полный проход запроса: делим по цене, пока срез не влезет в лимит ЦИАН. */
async function walkFull(page, s, known, out, lo = 0, hi = null) {
  const range = `${lo ? `&minprice=${lo}` : ''}${hi ? `&maxprice=${hi}` : ''}`;
  const label = `${s.name}${lo || hi ? ` ${lo}–${hi || '∞'} ₽` : ''}`;
  const st = await loadPage(page, s.url + range);
  if (!st) return false;
  if (st.total > SLICE_MAX && (hi || PRICE_TOP) - lo > 1000) {
    const top = hi || PRICE_TOP;
    const mid = Math.round((lo + top) / 2 / 1000) * 1000;
    console.log(`[ЦИАН] ${label}: ${st.total} — больше лимита выдачи, делю по цене на ${lo}–${mid} и ${mid + 1}–${hi || '∞'}`);
    const a = await walkFull(page, s, known, out, lo, mid);
    const b = hi ? await walkFull(page, s, known, out, mid + 1, hi)
      : (await walkFull(page, s, known, out, mid + 1, PRICE_TOP)) && (await walkFull(page, s, known, out, PRICE_TOP + 1, null));
    return a && b;
  }
  const r = await walk(page, label, s.url + range, 'full', known, out, PAGE_LIMIT);
  return r.done;
}

async function scrape(page, mode, known, maxPages) {
  const out = new Map();
  let complete = true;
  for (const s of SEARCHES) {
    if (mode === 'full') {
      if (!(await walkFull(page, s, known, out))) complete = false;
    } else {
      const r = await walk(page, s.name, s.url, mode, known, out, maxPages);
      if (r.skipped) complete = false;
    }
  }
  return { items: [...out.values()], complete: mode === 'full' && complete };
}

if (process.argv[1] && process.argv[1].endsWith('cian.js')) {
  const args = parseArgs();
  const known = readKnown(args.known);
  const browser = await launchBrowser({ headless: args.flags.has('--headless') });
  let result = { items: [], complete: false };
  try {
    const page = await newPage(browser);
    result = await scrape(page, args.mode, known, args.maxPages || MAX_PAGES[args.mode] || 5);
  } catch (e) {
    console.error(`[ЦИАН] ошибка: ${e.message}`);
  } finally {
    await browser.close();
  }
  writeOut(args.out, { source: 'cian', ...result });
}

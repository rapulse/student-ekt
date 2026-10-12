/**
 * Сигнал «в ⭐ Топе новый вариант» — запускается в конце run.sh.
 *
 * Топ берётся из самой карты (map_all.html), той же функцией, что и кнопка «⭐ Топ → надёжность, потом цена»
 * при фильтрах по умолчанию: критерии не дублируются и не расходятся со страницей.
 * Chrome открывается без окна, карту не трогаем.
 *
 * data/top_seen.json — id объявлений, которые уже бывали в топе. Квартира считается новой, если ни одно
 * её объявление (включая дубли на других сайтах) там не встречалось. Первый запуск только запоминает.
 * Есть новые → список в терминал (без уведомлений и звука).
 *
 *   node rent-parser/smart/top_alert.js           # обычный режим
 *   node rent-parser/smart/top_alert.js --test    # показать список на текущем топе, ничего не запоминая
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import puppeteer from "puppeteer-core";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const MAP = path.join(ROOT, "map_all.html");
const SEEN = path.join(ROOT, "data", "top_seen.json");
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const TEST = process.argv.includes("--test");

const rub = (n) => Math.round(n).toLocaleString("ru-RU").replace(/ /g, " ");

(async () => {
  if (!fs.existsSync(MAP)) { console.log("⭐ топ: нет map_all.html — пропускаю"); return; }
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: "new" });
  let top;
  try {
    const page = await browser.newPage();
    await page.goto("file://" + MAP, { waitUntil: "domcontentloaded" });
    await page.waitForFunction("typeof smartApply === 'function' && typeof filtered === 'function'", { timeout: 30000 });
    top = await page.evaluate(() => {
      if (!SMART) return null;
      ui = { ...JSON.parse(JSON.stringify(DEFAULT_UI)), top: "tiers" };
      return filtered().map((r, i) => ({
        place: i + 1,
        ids: r.vis.filter((x) => !x.post).map((x) => String(x.id)),
        fresh: r.vis.some(fromLastRun),
        tier: r.sm.tier, perMonth: r.sm.perMonth, monthly: r.sm.monthly, people: r.sm.people, trip: r.sm.trip,
        addr: r.sm.it.addr, kind: r.sm.it.tkind, area: r.sm.it.area, link: r.sm.it.link,
      }));
    });
  } finally {
    await browser.close();
  }
  if (!top) { console.log("⭐ топ: нет data/smart.json — пропускаю"); return; }

  let seen = null;
  try { seen = new Set(JSON.parse(fs.readFileSync(SEEN, "utf8")).ids); } catch (e) { /* первый запуск */ }
  const fresh = TEST ? top.slice(0, 3) : seen ? top.filter((r) => !r.ids.some((id) => seen.has(id))) : [];

  if (!TEST) {
    const all = new Set([...(seen || []), ...top.flatMap((r) => r.ids)]);
    fs.writeFileSync(SEEN, JSON.stringify({ updated: new Date().toISOString().slice(0, 16).replace("T", " "),
      note: "id объявлений, уже бывших в ⭐ Топе (rent-parser/smart/top_alert.js)", ids: [...all].sort() }, null, 1));
  }
  if (!seen && !TEST) {
    console.log(`⭐ топ: запомнил ${top.length} вариантов, о новых сообщу со следующего запуска`);
    return;
  }
  if (!fresh.length) { console.log(`⭐ топ: новых вариантов нет (в топе ${top.length})`); return; }

  const SYM = { A: "Ⓐ", B: "Ⓑ", C: "Ⓒ" };
  const kind = (k) => (k === "studio" ? "студия" : k ? `${k}к` : "");
  console.log(`\n🔔 ⭐ НОВОЕ В ТОПЕ: ${fresh.length}${TEST ? " (тест)" : ""}`);
  for (const r of fresh) {
    console.log(`  #${r.place} ${SYM[r.tier] || r.tier} ${kind(r.kind)}${r.area ? " " + r.area + " м²" : ""} · ${r.addr}`);
    console.log(`     ${rub(r.monthly)} ₽/мес с КУ${r.people > 1 ? " на чел." : ""} · ${rub(r.perMonth)} ₽/чел, если жить 3 мес` +
      `${r.trip != null ? ` · до НВК ~${r.trip} мин` : ""}${r.fresh ? " · новое объявление" : " · было в базе, вошло в топ"}`);
    if (r.link) console.log(`     ${r.link}`);
  }
  console.log("");
})().catch((e) => { console.log("⚠️  сигнал по топу не сработал: " + e.message); process.exitCode = 1; });

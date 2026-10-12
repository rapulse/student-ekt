import puppeteer from 'puppeteer-core';
import fs from 'fs';
import path from 'path';
import readline from 'readline';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const CHROME_PATH = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
export const PROFILE_DIR = path.resolve(__dirname, '../chrome_profile');

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const jitter = (a, b) => sleep(a + Math.random() * (b - a));

export async function launchBrowser({ headless = false } = {}) {
  const browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: headless ? 'new' : false,
    userDataDir: PROFILE_DIR,
    defaultViewport: null,
    ignoreDefaultArgs: ['--enable-automation'],
    args: [
      '--disable-blink-features=AutomationControlled',
      '--no-first-run',
      '--no-default-browser-check',
      '--window-size=1280,900',
      '--lang=ru-RU,ru',
    ],
  });
  return browser;
}

export async function newPage(browser) {
  const page = await browser.newPage();
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
  });
  return page;
}

/** Аргументы: --target X --mode full|inc --known файл.json --out файл.json --max-pages N и флаги. */
export function parseArgs(argv = process.argv.slice(2)) {
  const a = { target: null, mode: 'inc', known: null, out: null, maxPages: null, maxEnrich: null, flags: new Set() };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === '--mode') a.mode = argv[++i];
    else if (k === '--target') a.target = argv[++i];
    else if (k === '--known') a.known = argv[++i];
    else if (k === '--out') a.out = argv[++i];
    else if (k === '--max-pages') a.maxPages = +argv[++i];
    else if (k === '--max-enrich') a.maxEnrich = +argv[++i];
    else a.flags.add(k);
  }
  return a;
}

/** Известные ID из прошлых прогонов: {id: {has_geo: bool}}. */
export function readKnown(file) {
  if (!file || !fs.existsSync(file)) return {};
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return {}; }
}

export function writeOut(file, payload) {
  if (!file) { console.log(JSON.stringify(payload).slice(0, 2000)); return; }
  fs.writeFileSync(file, JSON.stringify(payload));
  console.log(`💾 Записано: ${payload.items.length} объявлений → ${path.basename(file)}`);
}

function ask(question) {
  if (!process.stdin.isTTY) return Promise.resolve('');
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((res) => rl.question(question, (ans) => { rl.close(); res(ans); }));
}

/**
 * Если вместо выдачи — капча/блокировка, просим человека решить её в окне Chrome.
 * isOk(page) → true, когда на странице есть нужные данные.
 * Возвращает true, если данные появились; false — если человек пропустил.
 */
export async function ensureContent(page, isOk, siteName) {
  for (let attempt = 0; attempt < 3; attempt++) {
    if (await isOk(page)) return true;
    const title = await page.title().catch(() => '');
    console.log(`\n🧩 ${siteName}: вместо объявлений — «${title}». Похоже на капчу или проверку.`);
    if (!process.stdin.isTTY) {
      console.log('   (нет терминала для вопроса — ждём 60 с, вдруг проверка пройдёт сама)');
      await sleep(60000);
      continue;
    }
    console.log('   👉 Реши её в открытом окне Chrome (страница не закрывается).');
    const ans = await ask('   Когда объявления видны — нажми Enter (или введи s, чтобы пропустить источник): ');
    if (ans.trim().toLowerCase() === 's') return false;
    await sleep(1500);
  }
  return isOk(page);
}

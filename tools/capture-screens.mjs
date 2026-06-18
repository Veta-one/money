// Снимает 6 скриншотов мини-аппа с мок-данными → docs/screens/.
// Перед запуском подними preview-сервер (frontend/index.html на http://localhost:8137).
// Запуск:  node tools/capture-screens.mjs
import puppeteer from 'puppeteer-core';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PREVIEW_URL = process.env.PREVIEW_URL || 'http://localhost:8137';
const OUT = path.resolve(ROOT, 'docs/screens');
fs.mkdirSync(OUT, { recursive: true });

// ===== МОК-ДАННЫЕ ===== — реалистичные, но обезличенные
const M = {
  '/api/dashboard': {
    month: 'Июнь 2026',
    safe_to_spend: 42500, per_day: 3270, days_left: 13,
    spent: 87420, spent_prev: 95210,
    income: 165000, income_prev: 165000,
    saved: 28300, saved_prev: 22500,
    net_worth: 854000, net_worth_delta: 38400,
    needs_review: 3, forecast_total: 168000,
    by_category: [
      { id: 1, name: 'Продукты', sum: 28400, prev: 31200, sparkline: [24000, 26500, 31200, 29800, 31200, 28400] },
      { id: 2, name: 'Кафе и рестораны', sum: 12300, prev: 9800, sparkline: [7800, 8400, 9100, 9800, 11200, 12300] },
      { id: 3, name: 'Транспорт', sum: 8200, prev: 7400, sparkline: [6900, 7100, 7800, 7400, 7900, 8200] },
      { id: 4, name: 'Подписки', sum: 4900, prev: 4900, sparkline: [4700, 4700, 4900, 4900, 4900, 4900] },
      { id: 5, name: 'Дети', sum: 6700, prev: 5400, sparkline: [5100, 5400, 5800, 5400, 6200, 6700] },
      { id: 6, name: 'Мед. услуги, лекарства', sum: 3600, prev: 1200, sparkline: [800, 1200, 900, 1200, 2400, 3600] },
    ],
    recent: [
      { id: 101, dt: '2026-06-17T18:30:00', merchant: 'Пятёрочка', amount: 740, currency: 'RUB', type: 'expense', category: 'Продукты', base_rub: 740 },
      { id: 102, dt: '2026-06-17T13:15:00', merchant: 'Шоколадница', amount: 380, currency: 'RUB', type: 'expense', category: 'Кафе и рестораны', base_rub: 380 },
      { id: 103, dt: '2026-06-17T10:00:00', merchant: 'Зарплата', amount: 165000, currency: 'RUB', type: 'income', category: 'Зарплата', base_rub: 165000 },
    ],
  },
  '/api/trends': {
    months: [
      { month: '2026-01', spent: 92000 },
      { month: '2026-02', spent: 88000 },
      { month: '2026-03', spent: 105000 },
      { month: '2026-04', spent: 91000 },
      { month: '2026-05', spent: 95200 },
      { month: '2026-06', spent: 87400 },
    ],
  },
  '/api/transactions': {
    transactions: [
      { id: 201, dt: '2026-06-17T18:30:00', merchant: 'Пятёрочка', amount: 1240, currency: 'RUB', type: 'expense', category: 'Продукты', base_rub: 1240 },
      { id: 202, dt: '2026-06-17T13:15:00', merchant: 'Шоколадница', amount: 380, currency: 'RUB', type: 'expense', category: 'Кафе и рестораны', base_rub: 380 },
      { id: 203, dt: '2026-06-17T10:00:00', merchant: 'Зарплата', amount: 165000, currency: 'RUB', type: 'income', category: 'Зарплата', base_rub: 165000 },
      { id: 204, dt: '2026-06-16T20:40:00', merchant: 'Яндекс Такси', amount: 312, currency: 'RUB', type: 'expense', category: 'Транспорт', base_rub: 312 },
      { id: 205, dt: '2026-06-16T15:10:00', merchant: 'Аптека', amount: 1245, currency: 'RUB', type: 'expense', category: 'Мед. услуги, лекарства', base_rub: 1245 },
      { id: 206, dt: '2026-06-16T09:30:00', merchant: 'Магнит', amount: 2410, currency: 'RUB', type: 'expense', category: 'Продукты', base_rub: 2410 },
      { id: 207, dt: '2026-06-15T19:20:00', merchant: 'IKEA', amount: 4890, currency: 'RUB', type: 'expense', category: 'Квартира', base_rub: 4890 },
      { id: 208, dt: '2026-06-15T12:00:00', merchant: 'Лукойл', amount: 3200, currency: 'RUB', type: 'expense', category: 'Бензин', base_rub: 3200 },
      { id: 209, dt: '2026-06-15T08:00:00', merchant: 'Spotify', amount: 199, currency: 'RUB', type: 'expense', category: 'Подписки', base_rub: 199 },
    ],
    count: 142, has_more: true, sum_expense: 87420, sum_income: 165000,
    offset: 0, limit: 50,
  },
  '/api/analytics': {
    totals: { income: 165000, expense: 87420, net: 77580, savings_rate: 47 },
    cashflow: [
      { month: '2026-01', income: 160000, expense: 92000 },
      { month: '2026-02', income: 160000, expense: 88000 },
      { month: '2026-03', income: 175000, expense: 105000 },
      { month: '2026-04', income: 160000, expense: 91000 },
      { month: '2026-05', income: 165000, expense: 95200 },
      { month: '2026-06', income: 165000, expense: 87400 },
    ],
    compare: [
      { name: 'Кафе и рестораны', cur: 12300, prev: 9800, delta: 2500, delta_pct: 26 },
      { name: 'Дети', cur: 6700, prev: 5400, delta: 1300, delta_pct: 24 },
      { name: 'Продукты', cur: 28400, prev: 31200, delta: -2800, delta_pct: -9 },
      { name: 'Мед. услуги, лекарства', cur: 3600, prev: 1200, delta: 2400, delta_pct: 200 },
    ],
    by_category: [
      { name: 'Продукты', sum: 28400 },
      { name: 'Кафе и рестораны', sum: 12300 },
      { name: 'Транспорт', sum: 8200 },
      { name: 'Дети', sum: 6700 },
      { name: 'Квартира', sum: 5890 },
      { name: 'Подписки', sum: 4900 },
      { name: 'Мед. услуги, лекарства', sum: 3600 },
      { name: 'Прочее', sum: 17430 },
    ],
    top_merchants: [
      { merchant: 'Пятёрочка', sum: 18900 },
      { merchant: 'Магнит', sum: 9610 },
      { merchant: 'Шоколадница', sum: 4730 },
      { merchant: 'Яндекс Такси', sum: 4120 },
    ],
    subscriptions: {
      total: 4900,
      items: [
        { name: 'Spotify', amount: 199, period: 'monthly' },
        { name: 'ChatGPT Plus', amount: 1800, period: 'monthly' },
        { name: 'Claude Pro', amount: 1900, period: 'monthly' },
        { name: 'Яндекс Плюс', amount: 399, period: 'monthly' },
        { name: 'Хостинг VPS', amount: 600, period: 'monthly' },
      ],
    },
  },
  '/api/budgets': {
    month: '2026-06',
    expected_income: 165000, total_budget: 95000, goals_plan: 25000, proficit: 45000,
    items: [
      { id: 1, name: 'Продукты', spent: 28400, budget: 30000, pct: 95, over: false, manual: true },
      { id: 2, name: 'Кафе и рестораны', spent: 12300, budget: 10000, pct: 123, over: true, manual: true },
      { id: 3, name: 'Транспорт', spent: 8200, budget: 9000, pct: 91, over: false, manual: false },
      { id: 4, name: 'Подписки', spent: 4900, budget: 5000, pct: 98, over: false, manual: true },
    ],
  },
  '/api/checkup': {
    savings_rate: 47, cushion_months: 6.2, usd_share: 18, rub_share: 82,
    recommendations: [
      { l: 'good', t: 'Норма сбережений 47% — отлично, держишь больше четверти дохода.' },
      { l: 'good', t: 'Подушка безопасности 6.2 мес. — выше нормы (3-6 мес).' },
      { l: 'warn', t: 'Доля USD 18% — ниже рекомендованной 20-30% для хеджа от рубля.' },
    ],
    insights: [
      { kind: 'anomaly', l: 'warn', title: 'Кафе и рестораны выросли на 26%', text: 'В этом месяце 12 300 ₽ против 9 800 ₽ в мае. Самая дорогая позиция — ужин 17 июня (2 100 ₽).' },
      { kind: 'velocity', l: 'info', title: 'Темп трат на месяц', text: 'За 17 дней потрачено 87 420 ₽. При текущем темпе к концу месяца выйдет ≈ 168 000 ₽ — в рамках плана.' },
      { kind: 'sub_stale', l: 'warn', title: 'Подписка не использовалась', text: 'Spotify (199 ₽/мес) — не было заметных слушаний за 2 месяца по выпискам. Стоит проверить.' },
      { kind: 'goal_pace', l: 'good', title: 'Подушка безопасности — до цели 2 месяца', text: 'Откладываешь +28 300 ₽/мес, до цели 200 000 ₽ осталось 54 700 ₽.' },
    ],
  },
  '/api/income': {
    sources: [
      { id: 1, name: 'Зарплата основная', amount: 135000, currency: 'RUB', period: 'monthly', owner: 'me', plan: 135000, fact: 135000, pct: 100, active: true, end_date: null },
      { id: 2, name: 'Фриланс контракт', amount: 2000, currency: 'USD', period: 'monthly', owner: 'me', plan: 180000, fact: 30000, pct: 17, active: true, end_date: '2027-03-31' },
      { id: 3, name: 'Пособие на детей', amount: 17500, currency: 'RUB', period: 'monthly', owner: 'wife', plan: 17500, fact: 17500, pct: 100, active: true, end_date: '2026-12-31' },
    ],
    plan_total: 332500, fact_total: 182500, unattributed: 0,
    breakdown: [
      { name: 'Зарплата основная', amount: 135000 },
      { name: 'Фриланс контракт', amount: 30000 },
      { name: 'Пособие на детей', amount: 17500 },
    ],
    nudges: [{ name: 'Пособие на детей', end_date: '2026-12-31' }],
    owners: { me: 165000, wife: 17500 },
  },
  '/api/capital': {
    net_worth: 854000, usd_rate: 72.75,
    series: [
      { date: '2026-04-01', total: 740000 },
      { date: '2026-04-15', total: 758000 },
      { date: '2026-05-01', total: 780000 },
      { date: '2026-05-15', total: 802000 },
      { date: '2026-06-01', total: 815600 },
      { date: '2026-06-17', total: 854000 },
    ],
    delta: 38400, delta_days: 17, from_month_start: true,
    savings: 28300, fx: 5400, other: 4700,
    allocation_currency: [
      { name: 'RUB', sum: 700000 },
      { name: 'USD', sum: 154000 },
    ],
    allocation_type: [
      { name: 'card', sum: 320000 },
      { name: 'deposit', sum: 380000 },
      { name: 'crypto', sum: 154000 },
    ],
    emergency: { liquid: 700000, avg_expense: 95000, months: 7.4, target: 6 },
    target: 2000000, monthly_save: 70000, saved_month: 28300,
    eta_months: 16, eta_date: '2027-10-17', income_sources: 3,
  },
  '/api/accounts': {
    accounts: [
      { id: 1, name: 'Тинькофф Black', type: 'card', currency: 'RUB', owner: 'me', balance: 215000, rub: 215000 },
      { id: 2, name: 'Сбер Mir', type: 'card', currency: 'RUB', owner: 'me', balance: 48000, rub: 48000 },
      { id: 3, name: 'Альфа-вклад', type: 'deposit', currency: 'RUB', owner: 'me', balance: 380000, rub: 380000 },
      { id: 4, name: 'Крипта (USDT)', type: 'crypto', currency: 'USD', owner: 'me', balance: 2117, rub: 154000 },
      { id: 5, name: 'Райффайзен (партнёр)', type: 'external', currency: 'RUB', owner: 'wife', balance: 73000, rub: 73000 },
    ],
    net_worth: 854000, usd_rate: 72.75,
  },
  '/api/heatmap': (() => {
    const today = new Date('2026-06-18');
    const pts = [];
    for (let i = 180; i >= 0; i--) {
      const d = new Date(today); d.setDate(d.getDate() - i);
      const dow = d.getDay();
      const base = dow === 0 || dow === 6 ? 1.8 : 1.0;
      const noise = Math.abs(Math.sin(i * 1.3) + Math.cos(i * 0.7));
      const v = Math.round(base * 1500 * (0.3 + noise));
      if (v > 500 && (i * 17) % 100 > 15) pts.push({ date: d.toISOString().slice(0, 10), spent: v });
    }
    return { days: 180, points: pts, max: Math.max(...pts.map(p => p.spent)) };
  })(),
  '/api/debts': { debts: [], owed_to_me: 0, i_owe: 0 },
  '/api/deposits': {
    deposits: [
      { id: 1, name: 'Депозит Тинькофф', bank: 'Тинькофф', principal: 380000, rate: 16.5,
        term_start: '2026-03-01', term_end: '2027-03-01', value_now: 380000, projected: 442700,
        accrued: 18900, owner: 'me' },
    ],
  },
  '/api/fx/history': (() => {
    const pts = [];
    const end = new Date('2026-06-17');
    for (let i = 365; i >= 0; i--) {
      const d = new Date(end); d.setDate(d.getDate() - i);
      const t = (365 - i) / 365;
      const drift = 78 - 5 * t;                              // 78 → 73
      const wave = 1.8 * Math.sin(t * Math.PI * 2.5);
      const noise = (Math.sin(i * 0.7) + Math.cos(i * 1.3)) * 0.5;
      pts.push({ date: d.toISOString().slice(0, 10), rate: +(drift + wave + noise).toFixed(4) });
    }
    return { currency: 'USD', days: 365, points: pts, latest: pts[pts.length - 1].rate };
  })(),
  '/api/goals': {
    goals: [
      { id: 1, name: 'Подушка безопасности', target_amount: 200000, current_amount: 145300, monthly_plan: 27000, pct: 73, eta: '2026-08-30', account: 'Депозит' },
      { id: 2, name: 'Отпуск летом', target_amount: 250000, current_amount: 64500, monthly_plan: 25000, pct: 26, eta: '2027-01-15', account: null },
      { id: 3, name: 'Машина (новая)', target_amount: 1500000, current_amount: 380000, monthly_plan: 35000, pct: 25, eta: '2028-12-01', account: null },
    ],
    suggest: { capacity: 70000 },
  },
  '/api/recurring': {
    recurring: [
      { id: 11, name: 'ЖКХ', amount: 6800, currency: 'RUB', period: 'monthly', type: 'expense' },
      { id: 12, name: 'Интернет', amount: 800, currency: 'RUB', period: 'monthly', type: 'expense' },
      { id: 13, name: 'Spotify', amount: 199, currency: 'RUB', period: 'monthly', type: 'expense' },
      { id: 14, name: 'ChatGPT Plus', amount: 1800, currency: 'RUB', period: 'monthly', type: 'expense' },
    ],
    candidates: [
      { name: 'Аптека', amount: 1100, months: 3 },
    ],
  },
  '/api/categories': {
    categories: [
      { id: 1, name: 'Продукты', type: 'expense', icon: 'cart', color: '#22c55e' },
      { id: 2, name: 'Кафе и рестораны', type: 'expense', icon: 'cup', color: '#f59e0b' },
      { id: 3, name: 'Транспорт', type: 'expense', icon: 'car', color: '#3b82f6' },
      { id: 4, name: 'Подписки', type: 'expense', icon: 'tv', color: '#8b5cf6' },
      { id: 5, name: 'Дети', type: 'expense', icon: 'heart', color: '#ec4899' },
      { id: 6, name: 'Зарплата', type: 'income', icon: 'wallet', color: '#10b981' },
    ],
  },
};

function mockFor(url) {
  const u = new URL(url, 'http://x');
  const p = u.pathname;
  // /api/fx/history?days=365 → ключ /api/fx/history
  for (const key of Object.keys(M)) {
    if (p === key || p.startsWith(key + '/') || (p === key && u.search)) return M[key];
    if (p.startsWith(key)) return M[key];
  }
  return null;
}

const SCREENS = [
  { name: '01-dashboard', tab: 'dashboard' },
  { name: '02-operations', tab: 'ops' },
  { name: '03-analytics', tab: 'analytics' },
  { name: '04-income', tab: 'income' },
  { name: '05-capital', tab: 'capital' },
  { name: '06-goals', tab: 'goals' },
];

async function main() {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox'],
  });
  for (const s of SCREENS) {
    const page = await browser.newPage();
    await page.setViewport({ width: 375, height: 812, deviceScaleFactor: 2 });
    await page.setRequestInterception(true);
    page.on('request', (req) => {
      const url = req.url();
      if (url.includes('/api/')) {
        const data = mockFor(url);
        req.respond({
          status: 200,
          contentType: 'application/json; charset=utf-8',
          body: JSON.stringify(data ?? {}),
        });
      } else {
        req.continue();
      }
    });
    await page.goto(PREVIEW_URL, { waitUntil: 'domcontentloaded' });
    // ждём пока определится функция tab/setTheme
    await page.waitForFunction(() => typeof window.tab === 'function' && typeof window.setTheme === 'function', { timeout: 5000 });
    await page.evaluate((t) => {
      setTheme('light');
      tab(t);
    }, s.tab);
    // ждём пока контент отрисуется
    await new Promise((r) => setTimeout(r, 900));
    const outPath = path.join(OUT, s.name + '.png');
    await page.screenshot({ path: outPath, fullPage: false });
    console.log('✓', s.name);
    await page.close();
  }
  await browser.close();
}

main().catch((e) => { console.error(e); process.exit(1); });

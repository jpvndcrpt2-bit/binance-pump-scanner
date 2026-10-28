'use strict';
/* Binance futures daily-pump scanner - runs entirely in the visitor's browser (no server, no keys). */
const CG = 'https://api.coingecko.com/api/v3';
const DEFAULTS = { minPct: 50, metric: 'close', minVol: 500000, date: '', refresh: 0,
                   base: 'https://fapi.binance.com' };

// ---------------------------------------------------------------- pure logic (unit-tested in node)
const pct = (n, o) => (o ? (n / o - 1) * 100 : 0);
const cleanBase = (b) => b.replace(/^(1000000|1000|1M)(?=[A-Z])/, '');
const dayStartMs = (iso) => Date.parse(iso + 'T00:00:00Z');
const safeUrl = (u) => (/^https?:\/\//i.test(u) ? u : '#');

function evaluate(c, cfg) {
  if (!c || !(c.o > 0) || c.quoteVol < cfg.minVol) return null;
  const chg = pct(c.c, c.o), hi = pct(c.h, c.o);
  const v = cfg.metric === 'close' ? chg : hi;
  if (v < cfg.minPct) return null;
  return { changePct: Math.round(chg * 100) / 100, highPct: Math.round(hi * 100) / 100, open: c.o, close: c.c,
           high: c.h, low: c.l, quoteVol: c.quoteVol };
}

/* Exact shortcut for TODAY's candle: the rolling-24h window contains the whole candle, so
   candle high/open >= 1.5 implies rolling high/low >= ~1.5 and rolling volume >= candle volume. */
function prefilter(tickers, perps, cfg) {
  return tickers.filter((t) => perps.has(t.symbol) && Number(t.quoteVolume) >= cfg.minVol &&
    Number(t.lowPrice) > 0 && Number(t.highPrice) / Number(t.lowPrice) >= (1 + cfg.minPct / 100) * 0.98);
}

async function mapLimit(items, n, fn) {
  const out = new Array(items.length); let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) { const k = i++; out[k] = await fn(items[k], k); }
  }));
  return out;
}

async function fetchCandle(cfg, api, symbol) {
  let url = `${cfg.base}/fapi/v1/klines?symbol=${encodeURIComponent(symbol)}&interval=1d&limit=1`;
  if (cfg.date) url += `&startTime=${dayStartMs(cfg.date)}`;
  const rows = await api(url);
  if (!rows || !rows.length) return null;
  const r = rows[0];
  if (cfg.date && Number(r[0]) !== dayStartMs(cfg.date)) return null;
  return { o: +r[1], h: +r[2], l: +r[3], c: +r[4], quoteVol: +r[7] };
}

async function scan(cfg, api, onProgress) {
  const info = await api(`${cfg.base}/fapi/v1/exchangeInfo`);
  const perps = new Map();
  for (const s of info.symbols || []) {
    if (s.contractType === 'PERPETUAL' && s.status === 'TRADING' && s.quoteAsset === 'USDT')
      perps.set(s.symbol, { symbol: s.symbol, base: s.baseAsset, onboard: Number(s.onboardDate) || 0 });
  }
  let list = [...perps.values()];
  if (!cfg.date) {
    const tk = await api(`${cfg.base}/fapi/v1/ticker/24hr`);
    list = prefilter(tk, perps, cfg).map((t) => perps.get(t.symbol));
  }
  let done = 0, errors = 0;
  const hits = [];
  await mapLimit(list, 6, async (s) => {
    try {
      const h = evaluate(await fetchCandle(cfg, api, s.symbol), cfg);
      if (h) hits.push(Object.assign(h, { symbol: s.symbol, base: s.base, onboard: s.onboard }));
    } catch (e) { errors++; }
    if (onProgress) onProgress(++done, list.length);
  });
  if (list.length && errors > list.length / 2) throw new Error('بیش از نیمی از درخواست‌های کندل شکست خورد');
  hits.sort((a, b) => b.changePct - a.changePct);
  return { hits, scanned: perps.size, checked: list.length, errors };
}

async function futuresContext(cfg, api, symbol) {
  const ctx = { fundingPct: null, oiChangePct: null };
  try {
    const p = await api(`${cfg.base}/fapi/v1/premiumIndex?symbol=${symbol}`);
    if (p && p.lastFundingRate != null) ctx.fundingPct = Math.round(Number(p.lastFundingRate) * 1e6) / 1e4;
  } catch (e) { /* optional */ }
  try {
    const r = await api(`${cfg.base}/futures/data/openInterestHist?symbol=${symbol}&period=1d&limit=2`);
    if (r.length === 2 && Number(r[0].sumOpenInterestValue) > 0)
      ctx.oiChangePct = Math.round(pct(Number(r[1].sumOpenInterestValue), Number(r[0].sumOpenInterestValue)) * 10) / 10;
  } catch (e) { /* optional */ }
  return ctx;
}

async function coingecko(base, api, sleep) {
  const sym = cleanBase(base);
  try {
    const s = await api(`${CG}/search?query=${encodeURIComponent(sym)}`);
    const coins = (s.coins || []).filter((c) => String(c.symbol).toUpperCase() === sym.toUpperCase())
      .sort((a, b) => (a.market_cap_rank || 1e9) - (b.market_cap_rank || 1e9));
    if (!coins.length) return null;
    await sleep(2500);
    const d = await api(`${CG}/coins/${encodeURIComponent(coins[0].id)}?localization=false&tickers=false&market_data=true` +
                        '&community_data=false&developer_data=false&sparkline=false');
    const links = d.links || {};
    return {
      id: coins[0].id, name: d.name || coins[0].name, rank: d.market_cap_rank || null,
      categories: (d.categories || []).filter(Boolean).slice(0, 5),
      description: String((d.description && d.description.en) || '').replace(/<[^>]+>/g, '').trim().slice(0, 320),
      mcap: d.market_data && d.market_data.market_cap ? d.market_data.market_cap.usd : null,
      twitter: links.twitter_screen_name || '',
      ambiguous: coins.length > 1,
      others: coins.slice(1, 4).map((c) => `${c.name} (${c.id})`),
    };
  } catch (e) { return null; }
}

function makeTags(h, cg, cfg, nowMs) {
  const t = [];
  if (h.onboard && nowMs - h.onboard < 14 * 86400000) t.push('لیست جدید در فیوچرز');
  if (h.changePct > 0 && h.highPct > h.changePct * 1.5 + 10) t.push('اسپایک بزرگ‌تر از بسته‌شدن');
  if (cg === null) t.push('CoinGecko پیدا نکرد');
  else if (cg && cg.ambiguous) t.push('چند ارز هم‌نماد؛ تطبیق را چک کن');
  return t;
}

function searchLinks(base, cg) {
  const sym = cleanBase(base), q = encodeURIComponent('$' + sym);
  const name = cg && cg.name && cg.name.toUpperCase() !== sym.toUpperCase() ? `"${cg.name}" OR ` : '';
  const l = [
    ['Google News', 'https://news.google.com/search?q=' + encodeURIComponent(`${name}"${sym}" crypto when:3d`)],
    ['X (live)', `https://x.com/search?q=${q}&f=live`],
    ['Binance Square', 'https://www.binance.com/en/square/search?s=' + encodeURIComponent(sym)],
    ['اعلان‌های بایننس', 'https://www.binance.com/en/support/search?q=' + encodeURIComponent(sym)],
    ['Reddit', 'https://www.reddit.com/search/?q=' + encodeURIComponent(sym + ' crypto') + '&sort=new'],
    ['CoinMarketCap', 'https://coinmarketcap.com/search/?q=' + encodeURIComponent(sym)],
  ];
  if (cg) l.unshift(['CoinGecko', 'https://www.coingecko.com/en/coins/' + cg.id]);
  if (cg && cg.twitter) l.push(['X رسمی', 'https://x.com/' + cg.twitter]);
  return l;
}

const fmtUsd = (x) => {
  if (x == null) return '-';
  for (const [u, d] of [['B', 1e9], ['M', 1e6], ['K', 1e3]]) if (Math.abs(x) >= d) return (x / d).toFixed(1) + u;
  return String(Math.round(x));
};

if (typeof module !== 'undefined') {
  module.exports = { pct, cleanBase, evaluate, prefilter, scan, futuresContext, coingecko, makeTags, searchLinks,
                     mapLimit, dayStartMs, fmtUsd, DEFAULTS };
}

// ---------------------------------------------------------------- browser UI
if (typeof document !== 'undefined') {
  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cache = new Map();          // base -> {cg, ctx}
  let cgQueue = Promise.resolve(), runId = 0, timer = null;

  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === 'class') n.className = v;
      else if (k === 'href') { n.setAttribute('href', safeUrl(v)); n.target = '_blank'; n.rel = 'noopener noreferrer'; }
      else n.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : String(kid));
    return n;
  }

  async function api(url, tries = 3) {
    let last;
    for (let i = 0; i < tries; i++) {
      try {
        const r = await fetch(url, { headers: { Accept: 'application/json' } });
        if (r.status === 451 || r.status === 403) throw Object.assign(new Error('blocked'), { fatal: true, status: r.status });
        if (r.status === 429 || r.status === 418) { await sleep(Math.min(60, Number(r.headers.get('Retry-After')) || 5 * (i + 1)) * 1000); continue; }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return await r.json();
      } catch (e) { last = e; if (e.fatal) throw e; await sleep(800 * (i + 1)); }
    }
    throw last || new Error('request failed');
  }

  function loadCfg() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem('pump-cfg') || '{}'); } catch (e) { /* ignore */ }
    return Object.assign({}, DEFAULTS, saved);
  }
  function readForm() {
    const cfg = {
      minPct: Math.max(1, Number($('minPct').value) || DEFAULTS.minPct), metric: $('metric').value,
      minVol: Math.max(0, Number($('minVol').value) || 0), date: $('date').value,
      refresh: Number($('refresh').value) || 0, base: $('base').value.trim().replace(/\/$/, '') || DEFAULTS.base,
    };
    if (!/^https:\/\//.test(cfg.base)) cfg.base = DEFAULTS.base;
    try { localStorage.setItem('pump-cfg', JSON.stringify(Object.assign({}, cfg, { date: '' }))); } catch (e) { /* ignore */ }
    return cfg;
  }
  function writeForm(c) {
    $('minPct').value = c.minPct; $('metric').value = c.metric; $('minVol').value = c.minVol;
    $('date').value = c.date; $('refresh').value = String(c.refresh); $('base').value = c.base;
  }

  function renderCard(h, cfg) {
    const e = cache.get(h.base) || {};
    const cg = e.cg, ctx = e.ctx || {};
    const tags = makeTags(h, cg === undefined ? false : cg, cfg, Date.now());
    if (cg === undefined) tags.push('در حال جست‌وجوی CoinGecko…');
    const c = el('section', { class: 'card', 'data-symbol': h.symbol },
      el('h2', null, h.symbol, ' ', el('span', { class: 'up' }, `+${h.changePct.toFixed(1)}%`)),
      el('p', { class: 'meta' }, `باز: ${h.open} · بسته: ${h.close} · سقف: ${h.high} (+${h.highPct.toFixed(1)}%) · حجم: $${fmtUsd(h.quoteVol)}` +
        ` · فاندینگ: ${ctx.fundingPct == null ? '-' : ctx.fundingPct + '%'} · تغییر OI: ${ctx.oiChangePct == null ? '-' : ctx.oiChangePct + '%'}`),
      el('p', null, tags.map((t) => el('span', { class: 'tag' }, t))));
    if (cg) {
      c.append(el('p', null, el('b', null, cg.name), cg.rank ? ` (رتبه ${cg.rank})` : '', ` · مارکت‌کپ: $${fmtUsd(cg.mcap)}`,
        cg.categories.length ? ' · دسته‌ها: ' + cg.categories.join('، ') : ''));
      if (cg.description) c.append(el('p', { class: 'desc' }, cg.description));
      if (cg.ambiguous) c.append(el('p', { class: 'warn' }, 'نمادهای هم‌نام: ' + cg.others.join('، ')));
    }
    c.append(el('p', { class: 'links' }, searchLinks(h.base, cg).flatMap(([n, u], i) => [i ? ' · ' : '', el('a', { href: u }, n)])));
    return c;
  }

  function refreshCard(h, cfg) {
    const old = document.querySelector(`[data-symbol="${h.symbol}"]`);
    if (old) old.replaceWith(renderCard(h, cfg));
  }

  function enrich(hits, cfg, myRun) {
    for (const h of hits) {
      if (cache.has(h.base) && cache.get(h.base).cg !== undefined) continue;
      cache.set(h.base, { cg: undefined, ctx: {} });
      futuresContext(cfg, api, h.symbol).then((ctx) => { cache.get(h.base).ctx = ctx; if (myRun === runId) refreshCard(h, cfg); });
      cgQueue = cgQueue.then(async () => {
        if (myRun !== runId) { cache.delete(h.base); return; }       // stale run: let a later scan redo it
        const cg = await coingecko(h.base, api, sleep);
        cache.get(h.base).cg = cg;
        if (myRun === runId) refreshCard(h, cfg);
        await sleep(2500);
      });
    }
  }

  async function run() {
    const cfg = readForm(), myRun = ++runId;
    clearTimeout(timer);
    $('go').disabled = true; $('status').textContent = 'در حال اسکن…'; $('err').hidden = true;
    try {
      const r = await scan(cfg, api, (d, n) => { if (myRun === runId) $('status').textContent = `بررسی کندل‌ها ${d}/${n}`; });
      if (myRun !== runId) return;
      const day = cfg.date || new Date().toISOString().slice(0, 10);
      $('status').textContent = `${r.hits.length} کوین بالای ${cfg.minPct}٪ در کندل روزانه ${day} (UTC) · ${r.scanned} قرارداد` +
        (r.errors ? ` · ${r.errors} خطا` : '') + ` · ${new Date().toLocaleTimeString('fa-IR')}`;
      const box = $('results'); box.replaceChildren();
      if (!r.hits.length) box.append(el('p', { class: 'note' }, 'هیچ کوینی از آستانه عبور نکرده است.'));
      for (const h of r.hits) box.append(renderCard(h, cfg));
      enrich(r.hits, cfg, myRun);
    } catch (e) {
      $('status').textContent = '';
      $('err').hidden = false;
      $('err').textContent = e.fatal
        ? 'بایننس (یا CoinGecko) این شبکه/منطقه را بلاک کرده است (HTTP ' + e.status + '). از VPN استفاده کن یا آدرس API را در «تنظیمات پیشرفته» عوض کن.'
        : 'خطا: ' + e.message + ' — اتصال اینترنت یا آدرس API را بررسی کن.';
    } finally {
      if (myRun === runId) {
        $('go').disabled = false;
        if (cfg.refresh > 0) timer = setTimeout(run, cfg.refresh * 60000);
      }
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    writeForm(loadCfg());
    $('form').addEventListener('submit', (ev) => { ev.preventDefault(); run(); });
    run();
  });
}

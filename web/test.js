'use strict';
// node web/test.js  - offline logic tests for app.js
const assert = require('assert');
const A = require('./app.js');

const DAY0 = A.dayStartMs('2026-10-27');
const kl = (o, h, l, c, qv, t = DAY0) => [[t, String(o), String(h), String(l), String(c), '1', t + 1, String(qv)]];
const cfg = Object.assign({}, A.DEFAULTS);
let calls = [];
const api = async (url) => {
  calls.push(url);
  if (url.includes('exchangeInfo')) return { symbols: [
    { symbol: 'AINUSDT', baseAsset: 'AIN', quoteAsset: 'USDT', contractType: 'PERPETUAL', status: 'TRADING', onboardDate: 1 },
    { symbol: 'BTCUSDT', baseAsset: 'BTC', quoteAsset: 'USDT', contractType: 'PERPETUAL', status: 'TRADING' },
    { symbol: 'DEADUSDT', baseAsset: 'DEAD', quoteAsset: 'USDT', contractType: 'PERPETUAL', status: 'TRADING' },
    { symbol: 'BTCUSDT_261225', baseAsset: 'BTC', quoteAsset: 'USDT', contractType: 'CURRENT_QUARTER', status: 'TRADING' }] };
  if (url.includes('ticker/24hr')) return [
    { symbol: 'AINUSDT', highPrice: '0.17', lowPrice: '0.06', quoteVolume: '725000000' },
    { symbol: 'BTCUSDT', highPrice: '80500', lowPrice: '79000', quoteVolume: '5000000000' },
    { symbol: 'DEADUSDT', highPrice: '3', lowPrice: '1', quoteVolume: '1000' },
    { symbol: 'ALIENUSDT', highPrice: '9', lowPrice: '1', quoteVolume: '9e9' }];
  if (url.includes('klines')) {
    if (url.includes('AINUSDT')) return kl(0.07, 0.17, 0.06, 0.156, 725e6);
    if (url.includes('DEADUSDT')) return kl(1, 3, 1, 2.5, 1000);
    return kl(80000, 80500, 79000, 80200, 5e9);
  }
  throw new Error('unexpected ' + url);
};

(async () => {
  // evaluate
  const c = { o: 100, h: 180, l: 90, c: 140, quoteVol: 1e6 };
  assert.strictEqual(A.evaluate(c, cfg), null);
  assert.strictEqual(A.evaluate(Object.assign({}, c, { c: 150 }), cfg).changePct, 50);
  assert.strictEqual(A.evaluate(c, Object.assign({}, cfg, { metric: 'high' })).highPct, 80);
  assert.strictEqual(A.evaluate(Object.assign({}, c, { c: 150, quoteVol: 10 }), cfg), null);
  assert.strictEqual(A.evaluate(null, cfg), null);
  assert.strictEqual(A.cleanBase('1000PEPE'), 'PEPE');
  assert.strictEqual(A.cleanBase('1INCH'), '1INCH');

  // today: prefilter limits kline calls to the one real candidate
  let r = await A.scan(cfg, api);
  assert.deepStrictEqual(r.hits.map((h) => h.symbol), ['AINUSDT']);
  assert.strictEqual(r.checked, 1);
  assert.strictEqual(calls.filter((u) => u.includes('klines')).length, 1);
  assert.ok(Math.abs(r.hits[0].changePct - 122.86) < 0.01);

  // past date: no prefilter, every perp is checked and startTime is sent
  calls = [];
  r = await A.scan(Object.assign({}, cfg, { date: '2026-10-27' }), api);
  assert.strictEqual(r.checked, 3);
  assert.ok(calls.filter((u) => u.includes('klines')).every((u) => u.includes('startTime=' + DAY0)));
  assert.deepStrictEqual(r.hits.map((h) => h.symbol), ['AINUSDT']);

  // wrong-day candle is ignored
  const wrong = async (u) => (u.includes('klines') ? kl(1, 5, 1, 5, 9e9, DAY0 - 86400000) : api(u));
  assert.strictEqual((await A.scan(Object.assign({}, cfg, { date: '2026-10-27' }), wrong)).hits.length, 0);

  // mostly failing requests -> error
  const boom = async (u) => { if (u.includes('klines')) throw new Error('x'); return api(u); };
  await assert.rejects(A.scan(Object.assign({}, cfg, { date: '2026-10-27' }), boom));

  // coingecko
  const cgApi = async (u) => {
    if (u.includes('/search')) return { coins: [
      { id: 'infinity-ground', name: 'Infinity Ground', symbol: 'AIN', market_cap_rank: 900 },
      { id: 'other-ain', name: 'Other', symbol: 'ain', market_cap_rank: null }, { id: 'n', name: 'N', symbol: 'AINX' }] };
    return { name: 'Infinity Ground', market_cap_rank: 900, categories: ['Gaming', null], description: { en: '<p>AI <b>game</b></p>' },
             links: { twitter_screen_name: 'ain_official' }, market_data: { market_cap: { usd: 12345678 } } };
  };
  const cg = await A.coingecko('AIN', cgApi, async () => {});
  assert.strictEqual(cg.id, 'infinity-ground');
  assert.strictEqual(cg.ambiguous, true);
  assert.strictEqual(cg.description, 'AI game');
  assert.deepStrictEqual(cg.categories, ['Gaming']);
  assert.strictEqual(await A.coingecko('AIN', async () => { throw new Error('429'); }, async () => {}), null);

  // tags + links
  const tags = A.makeTags({ onboard: Date.now() - 86400000, changePct: 60, highPct: 140 }, cg, cfg, Date.now());
  assert.ok(tags.includes('لیست جدید در فیوچرز') && tags.includes('اسپایک بزرگ‌تر از بسته‌شدن'));
  const links = A.searchLinks('1000PEPE', null);
  assert.ok(links.some(([n, u]) => n === 'X (live)' && u.includes('%24PEPE')));
  assert.ok(A.searchLinks('AIN', cg).some(([n, u]) => n === 'X رسمی' && u === 'https://x.com/ain_official'));

  // futures context
  const f = await A.futuresContext(cfg, async (u) => (u.includes('premium') ? { lastFundingRate: '0.0025' }
    : [{ sumOpenInterestValue: '100' }, { sumOpenInterestValue: '180' }]), 'AINUSDT');
  assert.deepStrictEqual(f, { fundingPct: 0.25, oiChangePct: 80 });
  console.log('web logic tests OK');
})().catch((e) => { console.error(e); process.exit(1); });

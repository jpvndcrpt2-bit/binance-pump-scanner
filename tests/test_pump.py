# -*- coding: utf-8 -*-
"""Offline tests with fake HTTP. No network."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pump  # noqa: E402

CFG = pump.load_config(None)
NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
DAY0 = pump.day_start_ms(dt.date(2026, 10, 27))


def kline(o, h, l, c, qv, t=DAY0):
    return [[t, str(o), str(h), str(l), str(c), "1", t + 86399999, str(qv), 1, "0", "0", "0"]]


RSS = """<rss><channel>
<item><title>AIN pumps on partnership - CoinDesk</title><link>https://example.com/a</link><pubDate>Tue, 27 Oct 2026 08:00:00 GMT</pubDate><source>CoinDesk</source></item>
<item><title>AIN pumps on partnership - CoinDesk</title><link>https://example.com/dup</link></item>
<item><title>bad link</title><link>javascript:alert(1)</link></item>
</channel></rss>"""


def fake_get(url, **kw):
    if "exchangeInfo" in url:
        return {"symbols": [
            {"symbol": "AINUSDT", "baseAsset": "AIN", "quoteAsset": "USDT", "contractType": "PERPETUAL",
             "status": "TRADING", "onboardDate": NOW_MS - 3 * 86400000},
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "contractType": "PERPETUAL",
             "status": "TRADING", "onboardDate": 1},
            {"symbol": "DEADUSDT", "baseAsset": "DEAD", "quoteAsset": "USDT", "contractType": "PERPETUAL",
             "status": "TRADING", "onboardDate": 1},
            {"symbol": "BTCUSDT_261225", "baseAsset": "BTC", "quoteAsset": "USDT", "contractType": "CURRENT_QUARTER",
             "status": "TRADING"},
        ]}
    if "klines" in url:
        if "AINUSDT" in url:
            return kline(0.07, 0.17, 0.06, 0.156, 725e6)
        if "DEADUSDT" in url:
            return kline(1, 3, 1, 2.5, 1000)          # +150% but no volume
        return kline(80000, 80500, 79000, 80200, 5e9)
    if "premiumIndex" in url:
        return {"lastFundingRate": "0.00250000"}
    if "openInterestHist" in url:
        return [{"sumOpenInterestValue": "100"}, {"sumOpenInterestValue": "180"}]
    if "/search?" in url:
        return {"coins": [{"id": "infinity-ground", "name": "Infinity Ground", "symbol": "AIN", "market_cap_rank": 900},
                          {"id": "other-ain", "name": "Other Ain", "symbol": "ain", "market_cap_rank": None},
                          {"id": "nope", "name": "Nope", "symbol": "AINX"}]}
    if "/coins/" in url:
        return {"name": "Infinity Ground", "market_cap_rank": 900, "categories": ["Gaming", None],
                "description": {"en": "<p>AI <b>game</b> token &amp; more</p>"},
                "links": {"homepage": ["", "https://ain.example"], "twitter_screen_name": "ain_official"},
                "market_data": {"market_cap": {"usd": 12345678}}, "genesis_date": None}
    if "catalogId" in url:
        return {"data": {"catalogs": [{"articles": [
            {"title": "Binance Futures Will Launch USDⓈ-Margined AIN Perpetual", "code": "abc",
             "releaseDate": NOW_MS - 86400000},
            {"title": "Binance lists BRAIN", "code": "x", "releaseDate": NOW_MS - 1000},
            {"title": "Old AIN news", "code": "old", "releaseDate": NOW_MS - 90 * 86400000}]}]}}
    raise AssertionError("unexpected url " + url)


def fake_text(url, **kw):
    return RSS


class Core(unittest.TestCase):
    def test_pct_and_evaluate(self):
        c = {"o": 100.0, "h": 180.0, "l": 90.0, "c": 140.0, "quote_vol": 1e6}
        self.assertIsNone(pump.evaluate(c, CFG))  # +40% < 50% on close
        c["c"] = 150.0
        self.assertEqual(pump.evaluate(c, CFG)["change_pct"], 50.0)          # threshold inclusive
        cfg = dict(CFG, metric="high")
        c["c"] = 120.0
        self.assertIsNone(pump.evaluate(c, CFG))
        self.assertEqual(pump.evaluate(c, cfg)["high_pct"], 80.0)
        c["quote_vol"] = 10
        self.assertIsNone(pump.evaluate(c, cfg))
        self.assertIsNone(pump.evaluate({"o": 0, "h": 1, "l": 0, "c": 1, "quote_vol": 1e9}, CFG))
        self.assertIsNone(pump.evaluate(None, CFG))

    def test_clean_base(self):
        self.assertEqual(pump.clean_base("1000PEPE"), "PEPE")
        self.assertEqual(pump.clean_base("1MBABYDOGE"), "BABYDOGE")
        self.assertEqual(pump.clean_base("1INCH"), "1INCH")
        self.assertEqual(pump.clean_base("AIN"), "AIN")

    def test_list_symbols_only_usdt_perps(self):
        syms = pump.list_symbols(CFG, fake_get)
        self.assertEqual([s["symbol"] for s in syms], ["AINUSDT", "BTCUSDT", "DEADUSDT"])

    def test_scan_filters_and_sorts(self):
        syms = pump.list_symbols(CFG, fake_get)
        hits, errs = pump.scan(CFG, syms, None, fake_get)
        self.assertEqual([h["symbol"] for h in hits], ["AINUSDT"])
        self.assertAlmostEqual(hits[0]["change_pct"], 122.86, places=1)
        self.assertEqual(errs, 0)

    def test_past_date_needs_matching_candle(self):
        def get(url, **kw):
            return kline(1, 2, 1, 2, 9e9, t=DAY0 - 86400000)     # wrong day returned
        self.assertIsNone(pump.fetch_candle(CFG, "XUSDT", dt.date(2026, 10, 27), get))

    def test_scan_raises_when_most_requests_fail(self):
        def boom(url, **kw):
            raise pump.ApiError("down")
        with self.assertRaises(pump.ApiError):
            pump.scan(CFG, [{"symbol": "A", "base": "A", "onboard": 0}] * 4, None, boom)

    def test_announcement_match_is_word_bound_and_recent(self):
        items = pump.binance_announcements(fake_get)
        m = pump.match_announcements(items, "AIN", 7, NOW_MS)
        self.assertEqual(len(m), 1)                    # not BRAIN, not the 90-day-old one
        self.assertIn("AIN Perpetual", m[0]["title"])

    def test_google_news_dedupes_and_rejects_bad_links(self):
        n = pump.google_news(["Infinity Ground", "AIN"], CFG, fake_text)
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0]["source"], "CoinDesk")

    def test_coingecko_picks_best_rank_and_flags_ambiguity(self):
        cg = pump.coingecko("AIN", CFG, fake_get, lambda s: None)
        self.assertEqual(cg["id"], "infinity-ground")
        self.assertTrue(cg["ambiguous"])
        self.assertEqual(cg["others"][0]["id"], "other-ain")
        self.assertEqual(cg["description"], "AI game token & more")
        self.assertEqual(cg["categories"], ["Gaming"])
        self.assertEqual(cg["homepage"], "https://ain.example")

    def test_coingecko_failure_is_none(self):
        def boom(url, **kw):
            raise pump.ApiError("429")
        self.assertIsNone(pump.coingecko("AIN", CFG, boom, lambda s: None))

    def test_config_unknown_key_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w") as f:
                json.dump({"min_change": 5}, f)
            with self.assertRaises(pump.ApiError):
                pump.load_config(p)
            with open(p, "w") as f:
                json.dump({"min_change_pct": 30, "telegram": {"token": "t"}}, f)
            cfg = pump.load_config(p)
            self.assertEqual(cfg["min_change_pct"], 30)
            self.assertEqual(cfg["telegram"], {"token": "t", "chat_id": ""})

    def test_telegram_off_without_credentials_and_on_with(self):
        self.assertFalse(pump.telegram(CFG, "x", post=lambda u, d: 1 / 0))
        cfg = dict(CFG, telegram={"token": "T", "chat_id": "9"})
        sent = []
        self.assertTrue(pump.telegram(cfg, "hi", post=lambda u, d: sent.append((u, d))))
        self.assertIn("botT", sent[0][0])


class EndToEnd(unittest.TestCase):
    def test_run_once_writes_report_and_caches(self):
        with tempfile.TemporaryDirectory() as d:
            cache = {}
            shown, new, rp, n, errs = pump.run_once(CFG, None, d, cache, True, fake_get, fake_text, lambda s: None, NOW)
            self.assertEqual(n, 3)
            self.assertEqual([h["symbol"] for h in shown], ["AINUSDT"])
            self.assertEqual(len(new), 1)
            h = shown[0]
            self.assertEqual(h["funding_pct"], 0.25)
            self.assertEqual(h["oi_change_pct"], 80.0)
            self.assertIn("لیست جدید در فیوچرز", h["tags"])
            self.assertIn("اعلان بایننس", h["tags"])
            self.assertIn("خبر پیدا شد", h["tags"])
            self.assertIn("چند ارز هم‌نماد؛ تطبیق را چک کن", h["tags"])
            with open(rp, encoding="utf-8") as f:
                page = f.read()
            self.assertIn("AINUSDT", page)
            self.assertIn("ain_official", page)
            self.assertNotIn("javascript:", page)
            self.assertTrue(os.path.exists(os.path.join(os.path.dirname(rp), "results.json")))
            # second pass: already enriched -> nothing new, no extra enrichment calls
            calls = []

            def counting(url, **kw):
                calls.append(url)
                return fake_get(url)
            _, new2, _, _, _ = pump.run_once(CFG, None, d, cache, True, counting, fake_text, lambda s: None, NOW)
            self.assertEqual(new2, [])
            self.assertFalse([u for u in calls if "/coins/" in u or "premiumIndex" in u])

    def test_no_news_mode_skips_external_sites(self):
        with tempfile.TemporaryDirectory() as d:
            def get(url, **kw):
                self.assertNotIn("coingecko", url)
                self.assertNotIn("catalogId", url)
                return fake_get(url)
            shown, new, rp, _, _ = pump.run_once(CFG, None, d, {}, False, get, lambda u, **k: 1 / 0, lambda s: None, NOW)
            self.assertEqual(len(shown), 1)
            self.assertIn("CoinGecko پیدا نکرد", shown[0]["tags"])

    def test_html_escapes_untrusted_text(self):
        h = {"symbol": "X<script>", "change_pct": 60.0, "high_pct": 61.0, "open": 1, "close": 1.6, "high": 1.61,
             "quote_vol": 1e6, "tags": ["<b>"], "coingecko": None, "announcements": [],
             "news": [{"title": "<img onerror=1>", "link": "javascript:alert(1)", "date": "", "source": ""}],
             "links": [], "funding_pct": None, "oi_change_pct": None}
        page = pump.build_report([h], CFG, "2026-10-27", "now")
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img onerror", page)
        self.assertNotIn("javascript:", page)


TG_PAGE = """<html><body>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message"><div class="tgme_widget_message_text js-message_text" dir="auto">#AIN pumping <b>+120%</b> on Binance Futures<br/>watch $AIN</div><div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/goodchan/11"><time datetime="2026-10-27T10:00:00+00:00" class="time">10:00</time></a></div></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message"><div class="tgme_widget_message_text js-message_text" dir="auto">BRAIN and AINU unrelated</div><div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/goodchan/12"><time datetime="2026-10-27T11:00:00+00:00">11:00</time></a></div></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message"><div class="tgme_widget_message_text js-message_text" dir="auto">AINUSDT old post</div><div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/goodchan/3"><time datetime="2026-09-01T11:00:00+00:00">old</time></a></div></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message"><div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/goodchan/4">photo only, no text</a></div></div></div>
</body></html>"""


class NewSources(unittest.TestCase):
    def test_cryptopanic_off_without_key(self):
        self.assertIsNone(pump.cryptopanic("AIN", CFG, lambda u, **k: 1 / 0))

    def test_cryptopanic_parses_and_never_leaks_token(self):
        cfg = dict(CFG, cryptopanic_token="SECRET123")
        seen = []

        def get(url, **kw):
            seen.append(url)
            return {"results": [{"title": "AIN listed", "url": "https://x.example/a", "published_at": "2026-10-27T09:00:00Z",
                                 "source": {"title": "CoinDesk"}},
                                {"title": "No link item", "source": "plain", "created_at": "2026-10-27"},
                                {"title": "", "url": "https://skip"}]}
        r = pump.cryptopanic("1000AIN", cfg, get)
        self.assertEqual([p["title"] for p in r], ["AIN listed", "No link item"])
        self.assertEqual(r[0]["source"], "CoinDesk")
        self.assertEqual(r[1]["link"], "")
        self.assertIn("currencies=AIN", seen[0])

        def boom(url, **kw):
            raise pump.ApiError("401")
        self.assertEqual(pump.cryptopanic("AIN", cfg, boom), [])

    def test_telegram_parse_and_match(self):
        msgs = pump.fetch_telegram(["@goodchan", "bad name!", "x"], lambda u, *a, **k: TG_PAGE)
        self.assertEqual(len(msgs), 6 - 3 + 0)                 # 3 valid messages (photo-only skipped); 1 valid channel
        self.assertTrue(all(m["channel"] == "goodchan" for m in msgs))
        hit = pump.match_telegram(msgs, "AIN", 3, NOW_MS, 4)
        self.assertEqual([m["link"] for m in hit], ["https://t.me/goodchan/11"])   # not BRAIN/AINU, not 2-month-old
        self.assertIn("watch $AIN", hit[0]["text"])
        self.assertEqual(pump.match_telegram(msgs, "AIN", 90, NOW_MS, 4)[-1]["link"], "https://t.me/goodchan/3")  # AINUSDT form

    def test_telegram_channel_failure_is_soft(self):
        def boom(u, *a, **k):
            raise pump.ApiError("x")
        self.assertEqual(pump.fetch_telegram(["goodchan"], boom), [])

    def test_run_once_includes_new_sources(self):
        cfg = dict(CFG, cryptopanic_token="K", telegram_channels=["goodchan"])

        def get(url, **kw):
            if "cryptopanic" in url:
                return {"results": [{"title": "AIN <b>news</b>", "url": "https://cp.example/1", "source": {"title": "CP"}}]}
            return fake_get(url)

        def text(url, *a, **k):
            return TG_PAGE if "t.me" in url else RSS
        with tempfile.TemporaryDirectory() as d:
            shown, new, rp, _, _ = pump.run_once(cfg, None, d, {}, True, get, text, lambda s: None, NOW)
            h = shown[0]
            self.assertIn("خبر CryptoPanic", h["tags"])
            self.assertIn("ذکر در تلگرام", h["tags"])
            with open(rp, encoding="utf-8") as f:
                page = f.read()
            self.assertIn("https://t.me/goodchan/11", page)
            self.assertIn("&lt;b&gt;news&lt;/b&gt;", page)         # untrusted title escaped
            self.assertNotIn("K&", page)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Offline tests for antipump.py. Numbers in comments are worked out by hand."""
import argparse
import contextlib
import csv
import datetime as dt
import io
import json
import os
import random
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import antipump as A  # noqa: E402
import pump  # noqa: E402

H, D = A.HOUR, A.DAY
DAY0 = int(dt.datetime(2026, 3, 10, tzinfo=dt.timezone.utc).timestamp() * 1000)


def mk_event(symbol="AAAUSDT", day_ms=DAY0, base=100.0, dec=1.0, n_after=240, onboard=0, entry_open=None):
    """Pre-day flat, pump bar at hour 5 (+50%), peak 1.6*base at hour 8, then -dec%/hour from 1.5*base.
    Entry bar for delay=4 is hour 10 (open = 1.5*base). Wick = 0.5% of base."""
    w, step = 0.005 * base, dec * base / 100.0
    bars, t = [], day_ms - D

    def add(o, h, l, c):
        nonlocal t
        bars.append([t, o, h, l, c, 1e6])
        t += H
    for _ in range(24):
        add(base, base, base, base)
    for _ in range(5):
        add(base, base, base, base)                           # hours 0-4
    add(base, 1.55 * base, base, 1.5 * base)                  # hour 5: strongest 1h candle (+50%)
    add(1.5 * base, 1.52 * base, 1.5 * base, 1.5 * base)      # hour 6
    add(1.5 * base, 1.53 * base, 1.5 * base, 1.52 * base)     # hour 7
    add(1.52 * base, 1.6 * base, 1.51 * base, 1.58 * base)    # hour 8: peak 1.6*base
    add(1.58 * base, 1.59 * base, 1.5 * base, 1.52 * base)    # hour 9
    for k in range(n_after):                                  # hour 10 onwards
        o = 1.5 * base - k * step
        if k == 0 and entry_open is not None:
            o = entry_open
        c = o - step
        add(o, o + w, c - w, c)
    day_close = bars[24 + 23][4]
    return {"symbol": symbol, "base": symbol[:-4], "day_ms": day_ms, "open": base, "high": 1.6 * base, "low": base,
            "close": day_close, "change_pct": round((day_close / base - 1) * 100, 2), "high_pct": 60.0,
            "quote_vol": 5e7, "onboard": onboard, "bars": bars}


class Stats(unittest.TestCase):
    def test_wilson(self):
        lo, hi = A.wilson(50, 100)
        self.assertAlmostEqual(lo, 0.4038, places=3)
        self.assertAlmostEqual(hi, 0.5962, places=3)
        self.assertEqual(A.wilson(0, 0), (0.0, 0.0))
        self.assertEqual(A.wilson(10, 10)[1], 1.0)

    def test_quantile(self):
        self.assertEqual(A.quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertIsNone(A.quantile([], 0.5))


class Features(unittest.TestCase):
    def test_anchor_and_peak(self):
        f = A.features(mk_event())
        self.assertEqual(f["a"], 24 + 5)                       # the +50% 1h candle
        self.assertEqual(f["anchor_close_t"], DAY0 + 6 * H)
        self.assertEqual(f["peak"], 160.0)
        self.assertEqual(f["hours_to_peak"], 2)                # bar at hour 8 opens 2h after the anchor closed
        self.assertEqual(f["base"], 100.0)

    def test_no_pump_candle_gives_none(self):
        ev = mk_event()
        for b in ev["bars"]:
            b[4] = b[1]                                       # nothing closes above its open
        self.assertIsNone(A.features(ev))


class Simulate(unittest.TestCase):
    COST = 0.15                                                # per side -> 0.30 round trip

    def test_target_hit(self):
        # entry 150, target = back to base 100 -> hit when low <= 100 at bar k=49 (149-49-0.5=99.5)
        r = A.simulate_short(mk_event(), 4, 20, 1.0, 96, self.COST)
        self.assertEqual(r["why"], "target")
        self.assertEqual(r["entry"], 150.0)
        self.assertEqual(r["exit"], 100.0)
        self.assertAlmostEqual(r["pnl_pct"], 50 / 150 * 100 - 0.30, places=6)       # 33.0333
        self.assertAlmostEqual(r["r"], (50 / 150 * 100 - 0.30) / 20, places=6)
        self.assertEqual(r["exit_t"], r["entry_t"] + 50 * H)                         # bar 49 closes after 50 bars
        self.assertAlmostEqual(r["mae_pct"], 0.5 / 150 * 100, places=6)

    def test_partial_target(self):
        # target_frac 0.5 -> 150 - 25 = 125 ; low <= 125 first at k with 149-k-0.5<=125 -> k=24
        r = A.simulate_short(mk_event(), 4, 20, 0.5, 96, self.COST)
        self.assertEqual((r["why"], r["exit"]), ("target", 125.0))
        self.assertEqual(r["exit_t"], r["entry_t"] + 25 * H)

    def test_stop_hit_and_gap(self):
        ev = mk_event()
        i = 24 + 5 + 1 + 4
        ev["bars"][i + 3][2] = 170.0                           # spike through the 165 stop (stop 10%)
        r = A.simulate_short(ev, 4, 10, 1.0, 96, self.COST)
        self.assertEqual((r["why"], r["exit"]), ("stop", 165.0))
        self.assertAlmostEqual(r["pnl_pct"], -10.0 - 0.30, places=6)
        self.assertAlmostEqual(r["r"], (-10.0 - 0.30) / 10, places=6)
        ev2 = mk_event()
        ev2["bars"][i + 3][1] = 170.0                          # gap OPEN above the stop -> filled at the open
        r2 = A.simulate_short(ev2, 4, 10, 1.0, 96, self.COST)
        self.assertEqual(r2["exit"], 170.0)
        self.assertAlmostEqual(r2["pnl_pct"], (150 - 170) / 150 * 100 - 0.30, places=6)

    def test_same_bar_stop_and_target_counts_as_stop(self):
        ev = mk_event()
        i = 24 + 5 + 1 + 4
        ev["bars"][i + 2][2], ev["bars"][i + 2][3] = 170.0, 90.0
        r = A.simulate_short(ev, 4, 10, 1.0, 96, self.COST)
        self.assertEqual(r["why"], "stop")

    def test_time_exit_at_close(self):
        r = A.simulate_short(mk_event(), 4, 20, 1.0, 10, self.COST)    # 10 hours: price only reaches ~140
        self.assertEqual(r["why"], "time")
        self.assertAlmostEqual(r["exit"], 150 - 10, places=6)           # close of bar k=9 = 149-9
        self.assertAlmostEqual(r["pnl_pct"], 10 / 150 * 100 - 0.30, places=6)

    def test_skips(self):
        self.assertEqual(A.simulate_short(mk_event(n_after=30), 4, 20, 1.0, 96, self.COST), "incomplete")
        self.assertEqual(A.simulate_short(mk_event(entry_open=99.0), 4, 20, 1.0, 24, self.COST), "below_base")


class Study(unittest.TestCase):
    def test_cell_numbers(self):
        evs = [mk_event()]
        one = A.study_cell(evs, 4, 1, 3.0)
        # 24h window: lowest low = 149-23-0.5 = 125.5 ; half level = 160-30 = 130 -> yes ; all (<=103) -> no
        self.assertEqual((one["n"], one["full"], one["half"], one["down"], one["squeeze20"]), (1, 0, 1, 1, 0))
        self.assertAlmostEqual(one["median_short_ret"], (150 - 126) / 150 * 100, places=6)     # 16.0
        self.assertEqual(one["half_n"], 1)
        two = A.study_cell(evs, 4, 2, 3.0)
        # 48h window: lowest low = 149-47-0.5 = 101.5 <= 103 (base+3%) -> gave back everything
        self.assertEqual(two["full"], 1)
        self.assertEqual(A.study_cell(evs, 4, 2, 1.0)["full"], 0)                               # 101.5 > 101 with tol 1%

    def test_half_not_counted_when_already_given_back_at_entry(self):
        # entry at 120 is already below the half level 130 -> excluded from the "half" denominator
        c = A.study_cell([mk_event(entry_open=120.0)], 4, 2, 3.0)
        self.assertEqual((c["n"], c["half_n"], c["half"]), (1, 0, 0))

    def test_incomplete_and_below_base_are_not_counted(self):
        evs = [mk_event(n_after=30), mk_event(entry_open=99.0)]
        self.assertEqual(A.study_cell(evs, 4, 2, 3.0)["n"], 0)

    def test_sections_shape(self):
        secs = A.study([mk_event(day_ms=DAY0 + i * D) for i in range(12)])
        self.assertEqual(len(secs), 1 + 5)
        self.assertIn("100% (", secs[0]["rows"][3][1])                                          # peak <= 2h after anchor: all 12


class Portfolio(unittest.TestCase):
    def T(self, entry, exit_, pnl, stop=20):
        return {"entry_t": entry, "exit_t": exit_, "pnl_pct": pnl, "stop_pct": stop, "r": pnl / stop, "why": "time",
                "mae_pct": 1.0, "day_ms": entry, "symbol": "X"}

    def test_equity_compounding(self):
        eq = A.equity_curve([self.T(0, 10, 10.0), self.T(20, 30, -20.0)], risk_pct=1.0, max_lev=3, max_concurrent=3)
        # size 1/20 = 5% of equity: +10% -> x1.005 ; -20% -> x0.99
        self.assertAlmostEqual(eq["final"], 1.005 * 0.99, places=9)
        self.assertAlmostEqual(eq["max_dd"], 0.01, places=9)
        self.assertEqual((eq["taken"], eq["skipped"], eq["worst_streak"]), (2, 0, 1))

    def test_concurrency_cap_and_leverage_cap(self):
        trades = [self.T(0, 100, 5), self.T(10, 100, 5), self.T(20, 100, 5)]
        eq = A.equity_curve(trades, 1.0, 3, 2)
        self.assertEqual((eq["taken"], eq["skipped"]), (2, 1))
        eq2 = A.equity_curve([self.T(0, 10, 10.0, stop=0.1)], risk_pct=1.0, max_lev=3, max_concurrent=3)
        self.assertAlmostEqual(eq2["final"], 1 + 3 * 10 / 100, places=9)                      # size capped at 3x, not 10x

    def test_trade_stats(self):
        ts = [self.T(i, i + 1, p) for i, p in enumerate([10, 10, -20, 10])]
        s = A.trade_stats(ts, boot=200)
        self.assertEqual((s["n"], s["wins"]), (4, 3))
        self.assertAlmostEqual(s["expectancy_pct"], 2.5)
        self.assertAlmostEqual(s["profit_factor"], 30 / 20)
        self.assertAlmostEqual(s["expectancy_r"], 2.5 / 20)
        self.assertEqual(A.trade_stats([])["n"], 0)


def synth_events(n=80, seed=3):
    rng = random.Random(seed)
    evs = []
    for i in range(n):
        base = rng.uniform(0.5, 50)
        evs.append(mk_event("S%dUSDT" % i, DAY0 + i * D, base=base, dec=rng.uniform(0.2, 1.0)))
    return evs


class Grid(unittest.TestCase):
    def test_grid_train_test_no_leakage(self):
        evs = synth_events()
        train, test = A.split_by_time(evs, 0.6)
        self.assertEqual((len(train), len(test)), (48, 32))
        self.assertLess(max(e["day_ms"] for e in train), min(e["day_ms"] for e in test))
        rows, ok = A.grid_search(train, test, 0.15, min_trades=30)
        self.assertEqual(len(rows), 6 * 4 * 4 * 3)
        self.assertTrue(all(r["n_train"] >= 30 for r in ok))
        self.assertTrue(all(ok[i]["t_train"] >= ok[i + 1]["t_train"] for i in range(len(ok) - 1)))

    def test_selection_uses_train_only(self):
        evs = synth_events()
        train, test = A.split_by_time(evs, 0.6)
        # make the test half a disaster: every test trade squeezes through every stop
        for e in test:
            for b in e["bars"][24 + 5 + 1:]:
                b[1] = b[2] = b[3] = b[4] = e["open"] * 5
        rows, ok = A.grid_search(train, test, 0.15, min_trades=30)
        rows2, ok2 = A.grid_search(train, synth_events()[48:], 0.15, min_trades=30)
        key = lambda r: (r["delay"], r["stop"], r["target"], r["hold"])
        self.assertEqual(key(ok[0]), key(ok2[0]))                      # chosen parameters ignore the test half
        self.assertNotEqual(ok[0]["r_test"], ok2[0]["r_test"])


class Labels(unittest.TestCase):
    def test_filters(self):
        evs = [mk_event("AAAUSDT", DAY0), mk_event("BBBUSDT", DAY0 + D), mk_event("CCCUSDT", DAY0 + 2 * D, onboard=DAY0 + 2 * D - 3 * D)]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "labels.csv")
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["symbol", "date", "reason", "auto_reason"])
                w.writerow(["AAAUSDT", A.day_str(DAY0), "no", ""])
                w.writerow(["BBBUSDT", A.day_str(DAY0 + D), "", "yes"])        # journal style
            ns = argparse.Namespace(min_change=0, max_change=0, no_new_listing=False, labels=p, reason="no")
            self.assertEqual([e["symbol"] for e in A.select(evs, ns)], ["AAAUSDT"])
            ns.reason = "yes"
            self.assertEqual([e["symbol"] for e in A.select(evs, ns)], ["BBBUSDT"])
            ns = argparse.Namespace(min_change=0, max_change=0, no_new_listing=True, labels=None, reason=None)
            self.assertEqual([e["symbol"] for e in A.select(evs, ns)], ["AAAUSDT", "BBBUSDT"])   # new listing dropped
            ns = argparse.Namespace(min_change=40, max_change=0, no_new_listing=False, labels=None, reason=None)
            self.assertEqual(len(A.select(evs, ns)), 0)                    # their daily change is only +36%


NOW = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)


def row(t, o, h, l, c, qv):
    return [t, str(o), str(h), str(l), str(c), "1", t + D - 1, str(qv), 1, "0", "0", "0"]


def fake_get(url, **kw):
    if "exchangeInfo" in url:
        return {"symbols": [{"symbol": "AAAUSDT", "baseAsset": "AAA", "quoteAsset": "USDT", "contractType": "PERPETUAL",
                             "status": "TRADING", "onboardDate": 1},
                            {"symbol": "BBBUSDT", "baseAsset": "BBB", "quoteAsset": "USDT", "contractType": "PERPETUAL",
                             "status": "TRADING", "onboardDate": 1}]}
    if "interval=1d" in url and "AAAUSDT" in url:
        today = int(NOW.timestamp() * 1000) // D * D
        return [row(DAY0 - D, 100, 101, 99, 100, 9e7), row(DAY0, 100, 165, 100, 136, 9e7),     # +36%: below the 50% threshold
                row(DAY0 + D, 100, 170, 100, 160, 9e7),                                          # +60% yes (closed)
                row(DAY0 + 2 * D, 100, 101, 99, 100, 1e3),
                row(today, 100, 300, 100, 290, 9e7)]                                              # +190% but today is unfinished
    if "interval=1d" in url:
        return [row(DAY0, 100, 110, 99, 105, 9e7)]
    if "interval=1h" in url:
        ev = mk_event("AAAUSDT", DAY0 + D)
        return [[b[0], b[1], b[2], b[3], b[4], "1", b[0] + H - 1, b[5]] for b in ev["bars"]]
    raise AssertionError(url)


class EndToEnd(unittest.TestCase):
    def test_collect_study_backtest_predict(self):
        with tempfile.TemporaryDirectory() as d:
            data = os.path.join(d, "data", "events.json")
            out = os.path.join(d, "out")
            args = A.build_parser().parse_args(["collect", "--data", data, "--workers", "2", "--pace", "0"])
            evs = A.collect(args, fake_get, NOW, log=lambda *a: None)
            self.assertEqual(len(evs), 1)                                  # one closed >=50% day; low-volume & today excluded
            self.assertEqual(evs[0]["symbol"], "AAAUSDT")
            self.assertEqual(evs[0]["day_ms"], DAY0 + D)
            with open(data, encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)["events"]), 1)
            # CLI commands need >= 10 events: replicate the saved one on different days/symbols
            many = synth_events(40)
            with open(data, "w", encoding="utf-8") as f:
                json.dump({"events": many}, f)
            base = ["--data", data, "--out", out, "--min-change", "0"]

            def run(*argv):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = A.main(list(argv))
                return code, buf.getvalue()
            code, o = run("study", *base)
            self.assertEqual(code, 0, o)
            self.assertIn("Watching 4 day(s)", o)
            self.assertTrue(os.path.exists(os.path.join(out, "study.html")))
            code, o = run("backtest", *base, "--delay", "4", "--stop", "20", "--hold-days", "4")
            self.assertEqual(code, 0, o)
            self.assertIn("equity multiple", o)
            code, o = run("backtest", *base, "--grid", "--min-trades", "10")
            self.assertEqual(code, 0, o)
            self.assertIn("Best on TRAIN", o)
            self.assertIn("unseen", o)
            self.assertTrue(os.path.exists(os.path.join(out, "grid.csv")))
            code, o = run("predict", *base, "--change", "60", "--hours", "4")
            self.assertEqual(code, 0, o)
            self.assertIn("95%", o)

    def test_collect_reports_progress_and_paces(self):
        with tempfile.TemporaryDirectory() as d:
            lines = []
            args = A.build_parser().parse_args(["collect", "--data", os.path.join(d, "e.json"), "--workers", "2", "--pace", "0"])
            A.collect(args, fake_get, NOW, log=lines.append)
            text = "\n".join(lines)
            self.assertIn("step 1/2", text)
            self.assertIn("daily 2/2 (100%)", text)
            self.assertIn("hourly 1/1", text)
        p = A.Pacer(0.05)
        t0 = time.monotonic()
        for _ in range(4):
            p.wait()
        self.assertGreaterEqual(time.monotonic() - t0, 0.14)               # 3 gaps of 0.05s between 4 calls

    def test_predict_symbol_live_state(self):
        def get(url, **kw):
            if "interval=1d" in url:
                return [row(DAY0, 100, 170, 100, 160, 9e7)]
            hb = []
            for h in range(10):
                o = 100 + h
                hb.append([DAY0 + h * H, str(o), str(o + 1), str(o), str(o + (30 if h == 5 else 1)), "1", 0, "5"])
            return hb
        now_ms = DAY0 + 10 * H + 5
        change, hours = A.live_state("AAAUSDT", "https://x", get, now_ms)
        self.assertAlmostEqual(change, 60.0)
        self.assertEqual(hours, 4)                                         # strongest candle = hour 5, closes at hour 6 -> 4h ago

    def test_missing_data_message(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = A.main(["study", "--data", os.path.join(tempfile.gettempdir(), "nope-xyz.json")])
        self.assertEqual(code, 1)
        self.assertIn("collect", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

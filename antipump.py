# -*- coding: utf-8 -*-
"""Anti-pump research tool for Binance USDT-perp futures. Runs on YOUR PC (needs Binance access).

collect   download every past daily-pump event (+ hourly bars after it)        -> data/events.json
study     numbers: how often / how fast pumps give the gains back, squeeze risk, when the peak comes
backtest  short-the-pump rule with fees+slippage; --grid searches parameters on the OLD part of the data
          and reports the NEW part separately (out-of-sample)
predict   empirical probability (with confidence interval) that a pump like this retraces
Places no orders. Standard library only. Not financial advice.
"""
import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import random
import statistics
import sys
import time
import concurrent.futures as cf

import pump

HERE = os.path.dirname(os.path.abspath(__file__))
HOUR, DAY = 3600000, 86400000
DATA = os.path.join(HERE, "data", "events.json")
OUT = os.path.join(HERE, "output", "antipump")
NEW_LISTING_DAYS = 14


# ----------------------------------------------------------------------------- statistics

def wilson(k, n, z=1.96):
    """95% Wilson interval for a proportion, as fractions."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def prop_text(k, n):
    if n == 0:
        return "n=0"
    lo, hi = wilson(k, n)
    return "%.0f%% (%.0f-%.0f%%, n=%d)" % (100.0 * k / n, 100 * lo, 100 * hi, n)


# ----------------------------------------------------------------------------- data collection

def _bar(r):
    return [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7])]


def fetch_daily(base, symbol, get, pages=3):
    """All daily bars (newest 1500 per page, walking back), oldest first."""
    out, end = [], None
    for _ in range(pages):
        url = "%s/fapi/v1/klines?symbol=%s&interval=1d&limit=1500" % (base, symbol)
        if end:
            url += "&endTime=%d" % end
        rows = get(url)
        if not rows:
            break
        out = [_bar(r) for r in rows] + out
        if len(rows) < 1500:
            break
        end = int(rows[0][0]) - 1
    return out


def find_events(symbol, daily, min_pct, min_vol, now_ms):
    ev = []
    for t, o, h, l, c, qv in daily:
        if t + DAY > now_ms or o <= 0 or qv < min_vol:       # closed days only
            continue
        if (c / o - 1) * 100 >= min_pct:
            ev.append({"symbol": symbol, "day_ms": t, "open": o, "high": h, "low": l, "close": c,
                       "change_pct": round((c / o - 1) * 100, 2), "high_pct": round((h / o - 1) * 100, 2),
                       "quote_vol": qv})
    return ev


def fetch_hourly(base, symbol, start_ms, end_ms, get):
    url = "%s/fapi/v1/klines?symbol=%s&interval=1h&startTime=%d&endTime=%d&limit=1500" % (base, symbol, start_ms, end_ms)
    return [_bar(r) for r in get(url)]


def collect(args, get=pump.fetch_json, now=None, log=print):
    now_ms = int((now or dt.datetime.now(dt.timezone.utc)).timestamp() * 1000)
    cfg = {"binance_base": args.base}
    syms = pump.list_symbols(cfg, get)
    log("symbols: %d" % len(syms))
    events, errs = [], 0

    def work(s):
        daily = fetch_daily(args.base, s["symbol"], get)
        evs = find_events(s["symbol"], daily, args.min_change, args.min_vol, now_ms)
        for e in evs:
            e["base"], e["onboard"] = s["base"], s["onboard"]
        return evs
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in cf.as_completed([ex.submit(work, s) for s in syms]):
            try:
                events.extend(fut.result())
            except Exception:
                errs += 1
    if syms and errs > len(syms) / 2:
        raise pump.ApiError("more than half of the daily downloads failed")
    events.sort(key=lambda e: e["day_ms"])
    log("events (>= %.0f%%): %d   (download errors: %d)" % (args.min_change, len(events), errs))

    def hourly(e):
        e["bars"] = fetch_hourly(args.base, e["symbol"], e["day_ms"] - DAY, e["day_ms"] + (2 + args.hold_days) * DAY, get)
        return e
    ok = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in cf.as_completed([ex.submit(hourly, e) for e in events]):
            try:
                e = fut.result()
                if len(e["bars"]) >= 48:
                    ok.append(e)
            except Exception:
                errs += 1
    ok.sort(key=lambda e: (e["day_ms"], e["symbol"]))
    os.makedirs(os.path.dirname(args.data), exist_ok=True)
    with open(args.data, "w", encoding="utf-8") as f:
        json.dump({"saved_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "min_change": args.min_change, "events": ok}, f)
    log("saved %d events with hourly bars -> %s" % (len(ok), args.data))
    return ok


def load_events(path):
    if not os.path.exists(path):
        raise pump.ApiError("no data yet - run:  py antipump.py collect")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["events"]


# ----------------------------------------------------------------------------- per-event features

def features(ev):
    """Anchor = the strongest 1h candle of the event day; peak = highest high until anchor close + 24h."""
    if "_f" in ev:
        return ev["_f"]
    bars, d0 = ev["bars"], ev["day_ms"]
    best = None
    for i, b in enumerate(bars):
        if d0 <= b[0] < d0 + DAY and b[1] > 0:
            r = b[4] / b[1] - 1
            if best is None or r > best[0]:
                best = (r, i)
    f = None
    if best and best[0] > 0:
        a = best[1]
        ac = bars[a][0] + HOUR
        cand = [i for i, b in enumerate(bars) if d0 <= b[0] < ac + DAY]
        p = max(cand, key=lambda i: bars[i][2])
        if bars[p][2] > ev["open"]:
            f = {"a": a, "anchor_close_t": ac, "anchor_ret_pct": round(best[0] * 100, 2), "peak_i": p,
                 "peak": bars[p][2], "peak_t": bars[p][0], "base": ev["open"],
                 "hours_to_peak": (bars[p][0] - ac) / HOUR}
    ev["_f"] = f
    return f


def is_new_listing(ev):
    return bool(ev.get("onboard")) and ev["day_ms"] - ev["onboard"] < NEW_LISTING_DAYS * DAY


def load_labels(path):
    """CSV with symbol,date(YYYY-MM-DD),reason(yes/no)  - or the scanner's journal.csv (auto_reason)."""
    out = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            r = (row.get("reason") or row.get("auto_reason") or "").strip().lower()
            if r in ("yes", "no"):
                out[(row["symbol"].strip().upper(), row["date"].strip())] = r
    return out


def day_str(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def select(events, a):
    out = []
    labels = load_labels(a.labels) if getattr(a, "labels", None) else None
    for ev in events:
        if not features(ev):
            continue
        if ev["change_pct"] < a.min_change or (a.max_change and ev["change_pct"] > a.max_change):
            continue
        if a.no_new_listing and is_new_listing(ev):
            continue
        if labels is not None:
            lab = labels.get((ev["symbol"], day_str(ev["day_ms"])))
            if a.reason and lab != a.reason:
                continue
        out.append(ev)
    return out


# ----------------------------------------------------------------------------- study (no strategy, just facts)

def window(ev, delay, hours):
    f = features(ev)
    i = f["a"] + 1 + delay
    bars = ev["bars"]
    if i + hours > len(bars):
        return None
    return i, bars[i:i + hours]


def study_cell(events, delay, days, tol_pct):
    """Stats for entering `delay` hours after the anchor candle close and watching `days` days."""
    n = full = half = down = squeeze20 = 0
    rets = []
    for ev in events:
        w = window(ev, delay, days * 24)
        if not w:
            continue
        f = features(ev)
        pe = w[1][0][1]
        base = f["base"]
        if pe <= base:
            continue
        n += 1
        lo = min(b[3] for b in w[1])
        hi = max(b[2] for b in w[1])
        height = f["peak"] - base
        full += lo <= base * (1 + tol_pct / 100)
        half += lo <= f["peak"] - 0.5 * height
        down += w[1][-1][4] < pe
        squeeze20 += hi >= pe * 1.20
        rets.append((pe - w[1][-1][4]) / pe * 100)
    return {"n": n, "full": full, "half": half, "down": down, "squeeze20": squeeze20,
            "median_short_ret": quantile(rets, 0.5), "mean_short_ret": statistics.mean(rets) if rets else None}


def study(events, tol_pct=3.0, delays=(0, 2, 4, 6, 8, 12, 24), days=(1, 2, 3, 4, 7)):
    sections = []
    htp = [features(e)["hours_to_peak"] for e in events]
    cum = [("peak <= %dh after anchor candle close" % k, sum(1 for h in htp if h <= k)) for k in (0, 1, 2, 4, 8, 24)]
    sections.append({"title": "When does the peak happen? (hours after the strongest 1h candle closes)",
                     "rows": [["", "share of events"]] + [[c, prop_text(k, len(htp))] for c, k in cum] +
                             [["median / 25% / 75% hours to peak", "%.1f / %.1f / %.1f" % (quantile(htp, .5), quantile(htp, .25), quantile(htp, .75))]]})
    for d in days:
        rows = [["entry delay (h)", "n", "gave back ALL (<= base+%.0f%%)" % tol_pct, "gave back HALF of the pump",
                 "lower at end", "squeeze >= +20% on entry", "median short return %"]]
        for dl in delays:
            c = study_cell(events, dl, d, tol_pct)
            rows.append([dl, c["n"], prop_text(c["full"], c["n"]), prop_text(c["half"], c["n"]), prop_text(c["down"], c["n"]),
                         prop_text(c["squeeze20"], c["n"]),
                         "-" if c["median_short_ret"] is None else "%.1f" % c["median_short_ret"]])
        sections.append({"title": "Watching %d day(s) after entry" % d, "rows": rows})
    return sections


# ----------------------------------------------------------------------------- short backtest

def simulate_short(ev, delay, stop_pct, target_frac, hold_h, cost_side_pct):
    """Short at the open of the bar `delay` hours after the anchor candle closes.
    Stop = entry*(1+stop%). Target = entry - target_frac*(entry-base)  (1.0 = back to pre-pump price).
    Same bar touches both -> the stop counts. Time exit at the close of the last hold bar.
    Returns trade dict, or a string reason it was skipped."""
    f = features(ev)
    i = f["a"] + 1 + delay
    bars = ev["bars"]
    if i + hold_h > len(bars):
        return "incomplete"
    pe = bars[i][1]
    base = f["base"]
    if pe <= base:
        return "below_base"
    stop = pe * (1 + stop_pct / 100)
    target = pe - target_frac * (pe - base)
    px, why, j_exit = None, "time", i + hold_h - 1
    mae = 0.0
    for j in range(i, i + hold_h):
        t, o, h, l, c, _ = bars[j]
        mae = max(mae, h / pe - 1)
        if o >= stop:
            px, why, j_exit = o, "stop", j
            break
        if h >= stop:
            px, why, j_exit = stop, "stop", j
            break
        if l <= target:
            px, why, j_exit = target, "target", j
            break
    if px is None:
        px = bars[j_exit][4]
    pnl = (pe - px) / pe * 100 - 2 * cost_side_pct
    return {"symbol": ev["symbol"], "day_ms": ev["day_ms"], "entry_t": bars[i][0], "exit_t": bars[j_exit][0] + HOUR,
            "entry": pe, "exit": px, "why": why, "pnl_pct": pnl, "r": pnl / stop_pct, "stop_pct": stop_pct,
            "mae_pct": mae * 100}


def run_rule(events, delay, stop_pct, target_frac, hold_days, cost_side_pct):
    trades, skipped = [], {"incomplete": 0, "below_base": 0}
    for ev in events:
        r = simulate_short(ev, delay, stop_pct, target_frac, hold_days * 24, cost_side_pct)
        if isinstance(r, str):
            skipped[r] += 1
        else:
            trades.append(r)
    return trades, skipped


def trade_stats(trades, boot=2000, seed=7):
    n = len(trades)
    if n == 0:
        return {"n": 0}
    pn = [t["pnl_pct"] for t in trades]
    rs = [t["r"] for t in trades]
    wins = [x for x in pn if x > 0]
    losses = [x for x in pn if x <= 0]
    gp, gl = sum(wins), -sum(losses)
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(rs, k=n)) for _ in range(boot)) if boot else [0, 0]
    return {"n": n, "wins": len(wins), "win_rate": len(wins) / n, "wilson": wilson(len(wins), n),
            "avg_win": statistics.mean(wins) if wins else 0.0, "avg_loss": statistics.mean(losses) if losses else 0.0,
            "expectancy_pct": statistics.mean(pn), "expectancy_r": statistics.mean(rs),
            "r_ci": (means[int(0.025 * (len(means) - 1))], means[int(0.975 * (len(means) - 1))]),
            "profit_factor": (gp / gl) if gl > 0 else float("inf"),
            "t_stat": max(-99.0, min(99.0, statistics.mean(rs) / (sd / math.sqrt(n)))) if sd > 1e-12
            else (99.0 if statistics.mean(rs) > 0 else 0.0),
            "worst": min(pn), "best": max(pn), "stopped": sum(1 for t in trades if t["why"] == "stop") / n,
            "targeted": sum(1 for t in trades if t["why"] == "target") / n,
            "mean_mae": statistics.mean(t["mae_pct"] for t in trades), "max_mae": max(t["mae_pct"] for t in trades)}


def equity_curve(trades, risk_pct, max_lev, max_concurrent):
    """Risk `risk_pct` of equity per trade (size = risk/stop, capped at max_lev). Trades that overlap more than
    max_concurrent open positions are skipped. P&L is booked in exit order, sized on equity at that time."""
    taken, open_exits, skipped = [], [], 0
    for t in sorted(trades, key=lambda t: t["entry_t"]):
        open_exits = [x for x in open_exits if x > t["entry_t"]]
        if len(open_exits) >= max_concurrent:
            skipped += 1
            continue
        open_exits.append(t["exit_t"])
        taken.append(t)
    eq, peak, mdd, streak, worst_streak = 1.0, 1.0, 0.0, 0, 0
    curve = []
    for t in sorted(taken, key=lambda t: t["exit_t"]):
        size = min(risk_pct / t["stop_pct"], max_lev)
        eq *= 1 + size * t["pnl_pct"] / 100
        eq = max(eq, 0.0)
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak if peak else 0)
        streak = streak + 1 if t["pnl_pct"] <= 0 else 0
        worst_streak = max(worst_streak, streak)
        curve.append((t["exit_t"], eq))
    return {"final": eq, "max_dd": mdd, "taken": len(taken), "skipped": skipped, "worst_streak": worst_streak, "curve": curve}


def by_year(trades):
    ys = {}
    for t in trades:
        ys.setdefault(day_str(t["entry_t"])[:4], []).append(t)
    return {y: trade_stats(v, boot=0) for y, v in sorted(ys.items())}


GRID = {"delay": (0, 2, 4, 6, 8, 12), "stop": (10, 15, 20, 30), "target": (0.3, 0.5, 0.75, 1.0), "hold": (2, 4, 7)}


def grid_search(train, test, cost, min_trades=30, grid=GRID):
    rows = []
    for dl in grid["delay"]:
        for sp in grid["stop"]:
            for tg in grid["target"]:
                for hd in grid["hold"]:
                    a, _ = run_rule(train, dl, sp, tg, hd, cost)
                    b, _ = run_rule(test, dl, sp, tg, hd, cost)
                    sa, sb = trade_stats(a, boot=0), trade_stats(b, boot=0)
                    rows.append({"delay": dl, "stop": sp, "target": tg, "hold": hd,
                                 "n_train": sa["n"], "r_train": sa.get("expectancy_r"), "t_train": sa.get("t_stat"),
                                 "n_test": sb["n"], "r_test": sb.get("expectancy_r"), "pf_test": sb.get("profit_factor")})
    ok = [r for r in rows if r["n_train"] >= min_trades]
    ok.sort(key=lambda r: -r["t_train"])
    return rows, ok


def split_by_time(events, frac):
    ev = sorted(events, key=lambda e: e["day_ms"])
    k = int(len(ev) * frac)
    return ev[:k], ev[k:]


# ----------------------------------------------------------------------------- predict

def similar(events, change, width=0.3, min_n=20):
    lo, hi = change * (1 - width), change * (1 + width)
    s = [e for e in events if lo <= e["change_pct"] <= hi]
    return (s, "%.0f-%.0f%%" % (lo, hi)) if len(s) >= min_n else (events, "all events (too few close to %.0f%%)" % change)


def predict_table(events, change, delay, tol_pct):
    pool, desc = similar(events, change)
    rows = [["horizon", "all the gain given back (<= base+%.0f%%)" % tol_pct, "half given back", "lower than entry",
             "squeeze >= +20% first"]]
    for d in (1, 2, 4, 7):
        c = study_cell(pool, delay, d, tol_pct)
        rows.append(["%d day(s)" % d, prop_text(c["full"], c["n"]), prop_text(c["half"], c["n"]), prop_text(c["down"], c["n"]),
                     prop_text(c["squeeze20"], c["n"])])
    return desc, len(pool), rows


def live_state(symbol, base_url, get, now_ms):
    """Today's change and hours since the strongest 1h candle closed, for --symbol."""
    d = get("%s/fapi/v1/klines?symbol=%s&interval=1d&limit=1" % (base_url, symbol))[0]
    change = (float(d[4]) / float(d[1]) - 1) * 100
    hb = [_bar(r) for r in get("%s/fapi/v1/klines?symbol=%s&interval=1h&startTime=%d&limit=48" % (base_url, symbol, int(d[0])))]
    best = max(hb, key=lambda b: b[4] / b[1] if b[1] > 0 else 0)
    hours = max(0, int((now_ms - (best[0] + HOUR)) // HOUR))
    return change, hours


# ----------------------------------------------------------------------------- output

def print_table(rows):
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for k, r in enumerate(rows):
        print("  " + "  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)))
        if k == 0:
            print("  " + "  ".join("-" * x for x in w))


def write_html(path, title, sections, notes):
    parts = ["<!doctype html><html lang='fa' dir='rtl'><head><meta charset='utf-8'><title>%s</title><style>"
             "body{font:15px/1.7 Tahoma,sans-serif;max-width:1000px;margin:auto;padding:14px}table{border-collapse:collapse;width:100%%;margin:8px 0 18px}"
             "td,th{border:1px solid #8884;padding:4px 8px;text-align:left;direction:ltr;font-size:.9em}th{background:#8882}"
             ".n{color:#777}</style></head><body><h1>%s</h1>" % (html.escape(title), html.escape(title))]
    for n in notes:
        parts.append("<p class='n'>%s</p>" % html.escape(n))
    for s in sections:
        parts.append("<h2>%s</h2>" % html.escape(s["title"]))
        if s.get("text"):
            parts.append("<p>%s</p>" % html.escape(s["text"]))
        if s.get("rows"):
            parts.append("<table>%s</table>" % "".join("<tr>%s</tr>" % "".join(
                "<%s>%s</%s>" % ("th" if k == 0 else "td", html.escape(str(c)), "th" if k == 0 else "td") for c in r)
                for k, r in enumerate(s["rows"])))
    parts.append("</body></html>")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))


CAVEATS = [
    "Only coins that are listed on Binance Futures TODAY are in the data. Delisted coins are missing (survivorship bias).",
    "Past results do not predict future results. Shorting pumped coins can squeeze far past any stop; check liquidation and funding on your account.",
    "Funding is ignored: shorts usually RECEIVE positive funding on pumped coins, so this is conservative for shorts. Costs per side = fee + slippage.",
    "Events on the same day are correlated (one market move = many events); the effective sample is smaller than n.",
    "A 'no fundamental reason' filter needs labels: pass --labels labels.csv (symbol,date,reason=yes/no) or the scanner's output/journal.csv.",
]


def fmt_stats(s):
    if not s["n"]:
        return [["trades", 0]]
    return [["trades", s["n"]], ["win rate", prop_text(s["wins"], s["n"])],
            ["avg win / avg loss (%)", "%.1f / %.1f" % (s["avg_win"], s["avg_loss"])],
            ["expectancy per trade (%)", "%.2f" % s["expectancy_pct"]],
            ["expectancy (R)  [95% CI]", "%.2f  [%.2f, %.2f]" % (s["expectancy_r"], s["r_ci"][0], s["r_ci"][1])],
            ["profit factor", "%.2f" % s["profit_factor"]], ["stopped out", "%.0f%%" % (100 * s["stopped"])],
            ["reached target", "%.0f%%" % (100 * s["targeted"])],
            ["worst / best trade (%)", "%.1f / %.1f" % (s["worst"], s["best"])],
            ["max adverse excursion (squeeze) %", "%.1f" % s["max_mae"]]]


# ----------------------------------------------------------------------------- CLI

def cmd_study(a):
    ev = select(load_events(a.data), a)
    if len(ev) < 10:
        raise pump.ApiError("only %d usable events - collect more or loosen filters" % len(ev))
    print("%d events  (%s .. %s)" % (len(ev), day_str(ev[0]["day_ms"]), day_str(ev[-1]["day_ms"])))
    secs = study(ev, a.tol)
    for s in secs:
        print("\n" + s["title"])
        print_table(s["rows"])
    p = os.path.join(a.out, "study.html")
    write_html(p, "Anti-pump study", secs, CAVEATS)
    print("\nreport:", p)


def cmd_backtest(a):
    ev = select(load_events(a.data), a)
    cost = a.fee + a.slip
    if len(ev) < 10:
        raise pump.ApiError("only %d usable events" % len(ev))
    secs = []
    if a.grid:
        train, test = split_by_time(ev, a.split)
        print("train: %d events (%s..%s)   test: %d events (%s..%s)" % (
            len(train), day_str(train[0]["day_ms"]), day_str(train[-1]["day_ms"]),
            len(test), day_str(test[0]["day_ms"]), day_str(test[-1]["day_ms"])))
        rows, ok = grid_search(train, test, cost, a.min_trades)
        if not ok:
            raise pump.ApiError("no parameter set has >= %d training trades" % a.min_trades)
        best = ok[0]
        sig = [r for r in rows if r["n_test"] >= a.min_trades and r["r_test"] is not None]
        pos = sum(1 for r in sig if r["r_test"] > 0)
        print("\nBest on TRAIN: delay=%dh stop=%d%% target=%.2f hold=%dd  (train %.2fR, t=%.1f, n=%d)" % (
            best["delay"], best["stop"], best["target"], best["hold"], best["r_train"], best["t_train"], best["n_train"]))
        print("Same parameters on TEST (unseen): %s R, n=%d" % ("%.2f" % best["r_test"] if best["r_test"] is not None else "-", best["n_test"]))
        print("Among all %d parameter sets with enough test trades, %d%% have positive test expectancy (%d/%d)." % (
            len(sig), round(100 * pos / len(sig)) if sig else 0, pos, len(sig)))
        top = [["delay h", "stop %", "target", "hold d", "n train", "R train", "t train", "n test", "R test"]] + [
            [r["delay"], r["stop"], r["target"], r["hold"], r["n_train"], "%.2f" % r["r_train"], "%.1f" % r["t_train"],
             r["n_test"], "-" if r["r_test"] is None else "%.2f" % r["r_test"]] for r in ok[:10]]
        print("\nTop 10 on train:")
        print_table(top)
        secs.append({"title": "Top 10 parameter sets on TRAIN, with their TEST result", "rows": top,
                     "text": "If the test column is much worse than train, the 'edge' was chosen by luck (overfitting)."})
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "grid.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        use, label = test, "TEST (unseen) with the train-selected parameters"
        a.delay, a.stop, a.target, a.hold_days = best["delay"], best["stop"], best["target"], best["hold"]
    else:
        use, label = ev, "ALL selected events (in-sample, no parameter search)"
    trades, skipped = run_rule(use, a.delay, a.stop, a.target, a.hold_days, cost)
    st = trade_stats(trades)
    eq = equity_curve(trades, a.risk, a.max_lev, a.max_concurrent)
    print("\nRule: short %dh after anchor, stop +%.0f%%, target %.0f%% of the way back to pre-pump, hold <= %dd, cost/side %.2f%%" % (
        a.delay, a.stop, a.target * 100, a.hold_days, cost))
    print("Result on %s   (skipped: %s)" % (label, skipped))
    rows = [["metric", "value"]] + fmt_stats(st) + [
        ["equity multiple (risk %.1f%%/trade, lev<=%.0f, <=%d open)" % (a.risk, a.max_lev, a.max_concurrent), "%.2fx" % eq["final"]],
        ["max drawdown", "%.0f%%" % (100 * eq["max_dd"])], ["longest losing streak", eq["worst_streak"]],
        ["trades taken / skipped (concurrency)", "%d / %d" % (eq["taken"], eq["skipped"])]]
    print_table(rows)
    secs.append({"title": "Result: " + label, "rows": rows})
    yr = by_year(trades)
    if yr:
        yrows = [["year", "n", "win rate", "expectancy %", "expectancy R"]] + [
            [y, s["n"], "%.0f%%" % (100 * s["win_rate"]), "%.2f" % s["expectancy_pct"], "%.2f" % s["expectancy_r"]] for y, s in yr.items()]
        print("\nBy year:")
        print_table(yrows)
        secs.append({"title": "By year", "rows": yrows})
    p = os.path.join(a.out, "backtest.html")
    write_html(p, "Anti-pump backtest", secs, CAVEATS)
    print("\nreport:", p)


def cmd_predict(a, get=pump.fetch_json, now=None):
    ev = select(load_events(a.data), a)
    if len(ev) < 10:
        raise pump.ApiError("only %d usable events" % len(ev))
    change, hours = a.change, a.hours
    if a.symbol:
        now_ms = int((now or dt.datetime.now(dt.timezone.utc)).timestamp() * 1000)
        change, hours = live_state(a.symbol.upper(), a.base, get, now_ms)
        print("%s today: %+.1f%%, %dh since its strongest 1h candle closed" % (a.symbol.upper(), change, hours))
    if change is None:
        raise pump.ApiError("give --change or --symbol")
    delay = min(max(0, hours or 0), 24)
    desc, n, rows = predict_table(ev, change, delay, a.tol)
    print("Similar past events: %d (change %s), entry %dh after the anchor candle" % (n, desc, delay))
    print_table(rows)
    print("Empirical frequencies of the past with 95%% confidence intervals - not a forecast.")


def build_parser():
    ap = argparse.ArgumentParser(description="Anti-pump research for Binance futures")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, collect_opts=False):
        p.add_argument("--data", default=DATA)
        p.add_argument("--out", default=OUT)
        p.add_argument("--base", default="https://fapi.binance.com")
        p.add_argument("--min-change", type=float, default=50.0)
        p.add_argument("--max-change", type=float, default=0.0)
        p.add_argument("--no-new-listing", action="store_true", help="drop coins listed < 14 days before the event")
        p.add_argument("--labels", help="CSV symbol,date,reason(yes/no) or the scanner journal.csv")
        p.add_argument("--reason", choices=("yes", "no"), help="with --labels: keep only events WITH (yes) / WITHOUT (no) a fundamental reason")
        p.add_argument("--tol", type=float, default=3.0, help="'gave back all' = price <= base + tol%%")
    c = sub.add_parser("collect")
    common(c)
    c.add_argument("--min-vol", type=float, default=500000)
    c.add_argument("--hold-days", type=int, default=10)
    c.add_argument("--workers", type=int, default=4)
    s = sub.add_parser("study")
    common(s)
    b = sub.add_parser("backtest")
    common(b)
    b.add_argument("--delay", type=int, default=4, help="hours after the strongest 1h candle closes")
    b.add_argument("--stop", type=float, default=20.0)
    b.add_argument("--target", type=float, default=1.0, help="fraction of the way back to the pre-pump price")
    b.add_argument("--hold-days", type=int, default=4)
    b.add_argument("--fee", type=float, default=0.10, help="%% per side")
    b.add_argument("--slip", type=float, default=0.05, help="%% per side")
    b.add_argument("--risk", type=float, default=1.0, help="%% of equity risked per trade")
    b.add_argument("--max-lev", type=float, default=3.0)
    b.add_argument("--max-concurrent", type=int, default=3)
    b.add_argument("--grid", action="store_true", help="search parameters on old data, report on new data")
    b.add_argument("--split", type=float, default=0.6)
    b.add_argument("--min-trades", type=int, default=30)
    p = sub.add_parser("predict")
    common(p)
    p.add_argument("--change", type=float)
    p.add_argument("--hours", type=int, default=4)
    p.add_argument("--symbol")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "collect":
            collect(a)
        elif a.cmd == "study":
            cmd_study(a)
        elif a.cmd == "backtest":
            cmd_backtest(a)
        else:
            cmd_predict(a)
        return 0
    except KeyboardInterrupt:
        return 0
    except (pump.ApiError, OSError, ValueError) as ex:
        print("ERROR:", ex, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

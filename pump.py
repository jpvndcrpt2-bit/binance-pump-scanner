# -*- coding: utf-8 -*-
"""Binance USDT-perpetual pump scanner.

Finds every futures coin whose DAILY candle (UTC day) is up >= N %, then collects possible reasons:
CoinGecko profile, Google News headlines, Binance announcements, funding / open-interest, plus
ready-made search links for X, Binance Square, Reddit, CoinMarketCap. Writes an HTML report.
Public endpoints only. No API keys. Places no orders. Python 3.8+ standard library only.
"""
import argparse
import calendar
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (compatible; pump-scanner/1.0)"
CG = "https://api.coingecko.com/api/v3"
ANN = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=50&catalogId=%d"
ANN_LINK = "https://www.binance.com/en/support/announcement/"

DEFAULTS = {
    "min_change_pct": 50.0,          # daily candle threshold
    "metric": "close",               # "close" = open->close, "high" = open->high (catches spikes that faded)
    "min_quote_volume_usd": 500000,  # ignore dead contracts
    "binance_base": "https://fapi.binance.com",
    "workers": 6,
    "news_days": 3,
    "max_news": 6,
    "new_listing_days": 14,
    "coingecko_delay_s": 2.5,        # free API is rate limited
    "telegram": {"token": "", "chat_id": ""},   # optional: push a short message for new hits
    "cryptopanic_token": "",         # optional API key (the API may need a paid plan); empty = off
    "cryptopanic_url": "https://cryptopanic.com/api/developer/v2/posts/",
    "telegram_channels": ["binance_announcements"],   # PUBLIC channels, read from t.me/s/<name>; add your own
    "telegram_days": 3,
    "telegram_max": 4,
}


class ApiError(Exception):
    pass


# ----------------------------------------------------------------------------- http

def _open(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_text(url, retries=3, sleep=time.sleep):
    err = None
    for attempt in range(retries):
        try:
            return _open(url)
        except urllib.error.HTTPError as e:
            err = e
            if e.code in (451, 403):
                raise ApiError("HTTP %d from %s - the site blocks this network/region (try VPN or another "
                               "binance_base in config.json)" % (e.code, urllib.parse.urlsplit(url).netloc))
            if e.code in (429, 418):
                wait = int(e.headers.get("Retry-After") or 5 * (attempt + 1))
                sleep(min(wait, 60))
                continue
            sleep(1 + attempt)
        except Exception as e:  # DNS, timeout, reset
            err = e
            sleep(1 + attempt)
    raise ApiError("request failed: %s (%s)" % (urllib.parse.urlsplit(url).netloc, err))


def fetch_json(url, retries=3, sleep=time.sleep):
    return json.loads(fetch_text(url, retries, sleep))


# ----------------------------------------------------------------------------- config

def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            user = json.load(f)
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ApiError("unknown setting(s) in config: %s" % ", ".join(sorted(unknown)))
        for k, v in user.items():
            if isinstance(DEFAULTS[k], dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if cfg["metric"] not in ("close", "high"):
        raise ApiError('metric must be "close" or "high"')
    return cfg


# ----------------------------------------------------------------------------- binance

def list_symbols(cfg, get=fetch_json):
    info = get(cfg["binance_base"] + "/fapi/v1/exchangeInfo")
    out = []
    for s in info.get("symbols", []):
        if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
            out.append({"symbol": s["symbol"], "base": s["baseAsset"], "onboard": int(s.get("onboardDate") or 0)})
    return out


def day_start_ms(d):
    return int(calendar.timegm(d.timetuple()) * 1000)


def fetch_candle(cfg, symbol, date, get=fetch_json):
    q = {"symbol": symbol, "interval": "1d", "limit": 1}
    if date:
        q["startTime"] = day_start_ms(date)
    rows = get(cfg["binance_base"] + "/fapi/v1/klines?" + urllib.parse.urlencode(q))
    if not rows:
        return None
    r = rows[0]
    if date and int(r[0]) != day_start_ms(date):
        return None                       # not listed on that day
    return {"open_time": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
            "c": float(r[4]), "quote_vol": float(r[7])}


def pct(new, old):
    return (new / old - 1.0) * 100.0 if old else 0.0


def evaluate(candle, cfg):
    """Returns the hit dict or None."""
    if not candle or candle["o"] <= 0 or candle["quote_vol"] < cfg["min_quote_volume_usd"]:
        return None
    chg, hi = pct(candle["c"], candle["o"]), pct(candle["h"], candle["o"])
    value = chg if cfg["metric"] == "close" else hi
    if value < cfg["min_change_pct"]:
        return None
    return {"change_pct": round(chg, 2), "high_pct": round(hi, 2), "open": candle["o"], "close": candle["c"],
            "high": candle["h"], "low": candle["l"], "quote_vol": candle["quote_vol"]}


def scan(cfg, symbols, date, get=fetch_json):
    hits, errors, last = [], 0, None

    def work(s):
        return s, fetch_candle(cfg, s["symbol"], date, get)
    with cf.ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        for fut in cf.as_completed([ex.submit(work, s) for s in symbols]):
            try:
                s, candle = fut.result()
            except Exception as e:
                errors, last = errors + 1, e
                continue
            h = evaluate(candle, cfg)
            if h:
                h.update(symbol=s["symbol"], base=s["base"], onboard=s["onboard"])
                hits.append(h)
    if symbols and errors > len(symbols) / 2:
        raise ApiError("more than half of the candle requests failed (%s)" % last)
    hits.sort(key=lambda h: -h["change_pct"])
    return hits, errors


def futures_context(cfg, symbol, get=fetch_json):
    ctx = {"funding_pct": None, "oi_change_pct": None}
    try:
        fr = get(cfg["binance_base"] + "/fapi/v1/premiumIndex?symbol=" + symbol).get("lastFundingRate")
        ctx["funding_pct"] = round(float(fr) * 100, 4) if fr is not None else None
    except Exception:
        pass
    try:
        rows = get(cfg["binance_base"] + "/futures/data/openInterestHist?symbol=%s&period=1d&limit=2" % symbol)
        if len(rows) == 2 and float(rows[0]["sumOpenInterestValue"]) > 0:
            ctx["oi_change_pct"] = round(pct(float(rows[1]["sumOpenInterestValue"]),
                                             float(rows[0]["sumOpenInterestValue"])), 1)
    except Exception:
        pass
    return ctx


# ----------------------------------------------------------------------------- reasons

def clean_base(base):
    """1000PEPE -> PEPE, 1MBABYDOGE -> BABYDOGE (Binance multiplies tiny prices)."""
    return re.sub(r"^(1000000|1000|1M)(?=[A-Z])", "", base)


def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def coingecko(base, cfg, get=fetch_json, sleep=time.sleep):
    sym = clean_base(base)
    try:
        s = get(CG + "/search?query=" + urllib.parse.quote(sym))
        coins = [c for c in s.get("coins", []) if str(c.get("symbol", "")).upper() == sym.upper()]
        coins.sort(key=lambda c: c.get("market_cap_rank") or 10 ** 9)
        if not coins:
            return None
        top = coins[0]
        sleep(cfg["coingecko_delay_s"])
        d = get(CG + "/coins/%s?localization=false&tickers=false&market_data=true&community_data=false"
                     "&developer_data=false&sparkline=false" % urllib.parse.quote(top["id"]))
        links = d.get("links") or {}
        md = d.get("market_data") or {}
        sleep(cfg["coingecko_delay_s"])
        return {"id": top["id"], "name": d.get("name") or top.get("name"), "rank": d.get("market_cap_rank"),
                "categories": [c for c in (d.get("categories") or []) if c][:5],
                "description": strip_tags((d.get("description") or {}).get("en"))[:320],
                "homepage": next((h for h in (links.get("homepage") or []) if h), ""),
                "twitter": links.get("twitter_screen_name") or "",
                "mcap_usd": (md.get("market_cap") or {}).get("usd"),
                "genesis": d.get("genesis_date"),
                "ambiguous": len(coins) > 1,
                "others": [{"id": c["id"], "name": c.get("name"), "rank": c.get("market_cap_rank")} for c in coins[1:4]]}
    except Exception:
        return None


def binance_announcements(get=fetch_json):
    items = []
    for cid in (48, 49):            # new listings, latest news
        try:
            data = get(ANN % cid)
            for cat in (data.get("data") or {}).get("catalogs") or []:
                for a in cat.get("articles") or []:
                    if a.get("title") and a.get("code") and a["code"] not in {i["link"][len(ANN_LINK):] for i in items}:
                        items.append({"title": a["title"], "link": ANN_LINK + a["code"],
                                      "ts": int(a.get("releaseDate") or 0)})
        except Exception:
            continue
    return items


def match_announcements(items, base, days, now_ms):
    sym = clean_base(base)
    pat = re.compile(r"(?<![A-Za-z0-9])(%s|%s)(?![A-Za-z0-9])" % (re.escape(base), re.escape(sym)))
    cutoff = now_ms - days * 86400 * 1000
    return [i for i in items if i["ts"] >= cutoff and pat.search(i["title"])]


def google_news(terms, cfg, get_text=fetch_text):
    terms = list(dict.fromkeys(t for t in terms if t))
    q = "(%s) crypto when:%dd" % (" OR ".join('"%s"' % t for t in terms), cfg["news_days"])
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    try:
        root = ET.fromstring(get_text(url))
    except Exception:
        return []
    out, seen = [], set()
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link.startswith("http") or title in seen:
            continue
        seen.add(title)
        out.append({"title": title, "link": link, "date": (it.findtext("pubDate") or "")[:22],
                    "source": (it.findtext("source") or "").strip()})
        if len(out) >= cfg["max_news"]:
            break
    return out


def cryptopanic(base, cfg, get=fetch_json):
    """None = disabled (no key). [] = asked but nothing / failed. Else a list of posts."""
    if not cfg["cryptopanic_token"]:
        return None
    q = urllib.parse.urlencode({"auth_token": cfg["cryptopanic_token"], "currencies": clean_base(base),
                                "public": "true"})
    try:
        data = get(cfg["cryptopanic_url"] + "?" + q)
    except Exception:
        return []
    out = []
    for p in (data.get("results") or []) if isinstance(data, dict) else []:
        title = p.get("title")
        if not title:
            continue
        src = p.get("source")
        link = p.get("url") or p.get("original_url") or ""
        out.append({"title": title, "link": link if str(link).startswith("http") else "",
                    "date": str(p.get("published_at") or p.get("created_at") or "")[:19],
                    "source": (src.get("title") if isinstance(src, dict) else src) or p.get("domain") or ""})
        if len(out) >= cfg["max_news"]:
            break
    return out


_TG_WRAP = re.compile(r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|\Z)', re.S)
_TG_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TG_LINK = re.compile(r'<a class="tgme_widget_message_date" href="([^"]+)"')
_TG_TIME = re.compile(r'<time datetime="([^"]+)"')


def fetch_telegram(channels, get_text=fetch_text):
    """Recent posts of PUBLIC channels via the t.me/s/<name> preview page. Fails softly per channel."""
    msgs = []
    for ch in channels:
        ch = str(ch).strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", ch):
            continue
        try:
            page = get_text("https://t.me/s/" + ch, 2)
        except Exception:
            continue
        for blk in _TG_WRAP.findall(page):
            tm, lm, dm = _TG_TEXT.search(blk), _TG_LINK.search(blk), _TG_TIME.search(blk)
            if not (tm and lm):
                continue
            try:
                ts = int(dt.datetime.fromisoformat(dm.group(1)).timestamp() * 1000) if dm else 0
            except ValueError:
                ts = 0
            msgs.append({"channel": ch, "link": html.unescape(lm.group(1)), "ts": ts,
                         "text": strip_tags(re.sub(r"<br\s*/?>", " ", tm.group(1)))})
    return msgs


def match_telegram(msgs, base, days, now_ms, limit):
    sym = clean_base(base)
    pat = re.compile(r"(?<![A-Za-z0-9])[#$]?(?:%s|%s)(?:USDT)?(?![A-Za-z0-9])" % (re.escape(base), re.escape(sym)))
    cutoff = now_ms - days * 86400 * 1000
    hits = [m for m in msgs if m["ts"] >= cutoff and pat.search(m["text"])]
    hits.sort(key=lambda m: -m["ts"])
    return [dict(m, text=m["text"][:220]) for m in hits[:limit]]


def social_links(base, cg):
    sym = clean_base(base)
    q = urllib.parse.quote("$" + sym)
    links = [("X (live)", "https://x.com/search?q=%s&f=live" % q),
             ("Binance Square", "https://www.binance.com/en/square/search?s=" + urllib.parse.quote(sym)),
             ("Reddit", "https://www.reddit.com/search/?q=" + urllib.parse.quote(sym + " crypto") + "&sort=new"),
             ("CoinMarketCap", "https://coinmarketcap.com/search/?q=" + urllib.parse.quote(sym))]
    if cg:
        links.insert(0, ("CoinGecko", "https://www.coingecko.com/en/coins/" + cg["id"]))
        if cg.get("twitter"):
            links.append(("X رسمی","https://x.com/" + cg["twitter"]))
    return links


def make_tags(h, cg, anns, news, cfg, now_ms):
    tags = []
    if h["onboard"] and now_ms - h["onboard"] < cfg["new_listing_days"] * 86400 * 1000:
        tags.append("لیست جدید در فیوچرز")
    if anns:
        tags.append("اعلان بایننس")
    if news:
        tags.append("خبر پیدا شد")
    if not news and not anns:
        tags.append("خبر/اعلانی پیدا نشد")
    if h["change_pct"] > 0 and h["high_pct"] > h["change_pct"] * 1.5 + 10:
        tags.append("اسپایک بزرگ‌تر از بسته‌شدن")
    if cg and cg.get("ambiguous"):
        tags.append("چند ارز هم‌نماد؛ تطبیق را چک کن")
    if cg is None:
        tags.append("CoinGecko پیدا نکرد")
    return tags


def enrich(h, cfg, ann_items, now_ms, get=fetch_json, get_text=fetch_text, sleep=time.sleep, news=True, tg_msgs=()):
    h.update(futures_context(cfg, h["symbol"], get))
    cg = coingecko(h["base"], cfg, get, sleep) if news else None
    anns = match_announcements(ann_items, h["base"], cfg["news_days"] + 4, now_ms)
    items = google_news([cg["name"] if cg else None, clean_base(h["base"])], cfg, get_text) if news else []
    cp = cryptopanic(h["base"], cfg, get) if news else None
    tg = match_telegram(tg_msgs, h["base"], cfg["telegram_days"], now_ms, cfg["telegram_max"]) if news else []
    h["coingecko"], h["announcements"], h["news"] = cg, anns, items
    h["cryptopanic"], h["telegram"] = cp or [], tg
    h["links"] = social_links(h["base"], cg)
    h["tags"] = make_tags(h, cg, anns, items + (cp or []) + tg, cfg, now_ms)
    if cp:
        h["tags"].insert(0, "خبر CryptoPanic")
    if tg:
        h["tags"].insert(0, "ذکر در تلگرام")
    return h


# ----------------------------------------------------------------------------- report

def e(x):
    return html.escape(str(x), quote=True)


def safe_url(u):
    return u if str(u).startswith(("http://", "https://")) else "#"


def fmt_usd(x):
    if x is None:
        return "-"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return "%.1f%s" % (x / div, unit)
    return "%.0f" % x


def card(h):
    cg = h.get("coingecko")
    rows = ["<h2>%s <span class='up'>+%.1f%%</span></h2>" % (e(h["symbol"]), h["change_pct"]),
            "<p class='meta'>باز: %s · بسته: %s · سقف: %s (+%.1f%%) · حجم: $%s · فاندینگ: %s · تغییر OI: %s</p>" % (
                e(h["open"]), e(h["close"]), e(h["high"]), h["high_pct"], fmt_usd(h["quote_vol"]),
                "-" if h.get("funding_pct") is None else "%s%%" % h["funding_pct"],
                "-" if h.get("oi_change_pct") is None else "%s%%" % h["oi_change_pct"]),
            "<p>%s</p>" % " ".join("<span class='tag'>%s</span>" % e(t) for t in h["tags"])]
    if cg:
        rows.append("<p><b>%s</b>%s · مارکت‌کپ: $%s%s</p>" % (
            e(cg["name"]), " (رتبه %s)" % e(cg["rank"]) if cg.get("rank") else "", fmt_usd(cg.get("mcap_usd")),
            " · دسته‌ها: " + e("، ".join(cg["categories"])) if cg["categories"] else ""))
        if cg["description"]:
            rows.append("<p class='desc'>%s</p>" % e(cg["description"]))
        if cg["ambiguous"]:
            rows.append("<p class='warn'>نمادهای هم‌نام: %s</p>" % e("، ".join(
                "%s (%s)" % (o["name"], o["id"]) for o in cg["others"])))
    if h["announcements"]:
        rows.append("<h3>اعلان‌های بایننس</h3><ul>%s</ul>" % "".join(
            "<li><a href='%s' target='_blank' rel='noopener'>%s</a></li>" % (safe_url(a["link"]), e(a["title"]))
            for a in h["announcements"]))
    if h["news"]:
        rows.append("<h3>خبرهای اخیر (Google News)</h3><ul>%s</ul>" % "".join(
            "<li><a href='%s' target='_blank' rel='noopener'>%s</a> <small>%s</small></li>" % (
                safe_url(n["link"]), e(n["title"]), e(n["date"])) for n in h["news"]))
    if h.get("cryptopanic"):
        rows.append("<h3>CryptoPanic</h3><ul>%s</ul>" % "".join(
            "<li>%s <small>%s %s</small></li>" % (
                "<a href='%s' target='_blank' rel='noopener'>%s</a>" % (safe_url(n["link"]), e(n["title"]))
                if n["link"] else e(n["title"]), e(n["source"]), e(n["date"])) for n in h["cryptopanic"]))
    if h.get("telegram"):
        rows.append("<h3>تلگرام (کانال‌های عمومی)</h3><ul>%s</ul>" % "".join(
            "<li><a href='%s' target='_blank' rel='noopener'>@%s</a>: %s</li>" % (
                safe_url(m["link"]), e(m["channel"]), e(m["text"])) for m in h["telegram"]))
    rows.append("<p class='links'>%s</p>" % " · ".join(
        "<a href='%s' target='_blank' rel='noopener'>%s</a>" % (safe_url(u), e(n)) for n, u in h["links"]))
    return "<section class='card'>%s</section>" % "".join(rows)


CSS = """:root{--bg:#fff;--fg:#1a1a1a;--card:#f6f6f6;--mut:#666;--up:#0a8f4d;--tag:#e6eefc;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--card:#1e1e1e;--mut:#9a9a9a;--up:#37d67a;--tag:#23314d;--warn:#f59e0b}}
body{background:var(--bg);color:var(--fg);font:16px/1.7 Tahoma,Vazirmatn,sans-serif;margin:0;padding:16px;max-width:900px;margin-inline:auto}
.card{background:var(--card);border-radius:10px;padding:12px 16px;margin:14px 0}h2{margin:.2em 0}h3{margin:.6em 0 .2em;font-size:1em}
.up{color:var(--up)}.meta,.desc,small{color:var(--mut)}.tag{background:var(--tag);border-radius:6px;padding:1px 8px;font-size:.85em}
.warn{color:var(--warn)}a{color:inherit}ul{margin:.2em 0;padding-inline-start:1.2em}.note{color:var(--mut);font-size:.9em}"""


def build_report(hits, cfg, date_label, generated_at):
    body = "".join(card(h) for h in hits) or "<p>امروز هیچ کوینی از آستانه عبور نکرده است.</p>"
    return ("<!doctype html><html lang='fa' dir='rtl'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>اسکنر پامپ بایننس</title><style>%s</style></head><body>"
            "<h1>کوین‌های بالای %s%% - کندل روزانه %s (UTC)</h1>"
            "<p class='note'>معیار: %s · حداقل حجم: $%s · ساخته‌شده: %s UTC · کندل روزانه بایننس ساعت ۰۳:۰۰ بغداد شروع می‌شود.</p>"
            "<p class='note'>خبرها «سرنخ» هستند، نه اثبات علت. شبکه‌های اجتماعی (X، تلگرام، Square) به‌صورت خودکار خوانده نمی‌شوند؛ "
            "لینک جست‌وجوی آن‌ها در هر کارت هست. پامپ بدون خبر معمولاً نقدینگی کم یا دستکاری است. توصیه مالی نیست.</p>%s</body></html>"
            % (CSS, e(cfg["min_change_pct"]), e(date_label), "بسته‌شدن نسبت به باز" if cfg["metric"] == "close" else "سقف نسبت به باز",
               fmt_usd(cfg["min_quote_volume_usd"]), e(generated_at), body))


def telegram(cfg, text, post=None):
    t = cfg["telegram"]
    if not (t.get("token") and t.get("chat_id")):
        return False
    url = "https://api.telegram.org/bot%s/sendMessage" % t["token"]
    data = urllib.parse.urlencode({"chat_id": t["chat_id"], "text": text[:3800]}).encode()
    try:
        (post or (lambda u, d: urllib.request.urlopen(urllib.request.Request(u, data=d), timeout=15).read()))(url, data)
        return True
    except Exception:
        return False


def summary_line(h):
    first = (h["news"] or h.get("cryptopanic") or [{"title": ""}])[0]["title"]
    top = first or (h.get("telegram") or [{"text": ""}])[0]["text"] or "; ".join(h["tags"])
    return "%s +%.1f%% | %s" % (h["symbol"], h["change_pct"], top)


# ----------------------------------------------------------------------------- run

def run_once(cfg, date, out_dir, cache, news=True, get=fetch_json, get_text=fetch_text, sleep=time.sleep, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    symbols = list_symbols(cfg, get)
    hits, errors = scan(cfg, symbols, date, get)
    ann = binance_announcements(get) if (news and hits) else []
    tg_msgs = fetch_telegram(cfg["telegram_channels"], get_text) if (news and hits and cfg["telegram_channels"]) else []
    new = []
    for h in hits:
        key = h["symbol"]
        if key in cache:
            cache[key].update({k: h[k] for k in ("change_pct", "high_pct", "close", "high", "quote_vol")})
            continue
        cache[key] = enrich(h, cfg, ann, now_ms, get, get_text, sleep, news, tg_msgs)
        new.append(cache[key])
    day = date or now.date()
    shown = sorted((cache[h["symbol"]] for h in hits), key=lambda x: -x["change_pct"])
    folder = os.path.join(out_dir, day.isoformat())
    os.makedirs(folder, exist_ok=True)
    rp = os.path.join(folder, "report.html")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(build_report(shown, cfg, day.isoformat(), now.strftime("%Y-%m-%d %H:%M")))
    with open(os.path.join(folder, "results.json"), "w", encoding="utf-8") as f:
        json.dump(shown, f, ensure_ascii=False, indent=1)
    return shown, new, rp, len(symbols), errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="Binance futures daily-pump scanner")
    ap.add_argument("--date", help="UTC day YYYY-MM-DD (default: today's running candle)")
    ap.add_argument("--min", type=float, help="min daily change %% (default from config: 50)")
    ap.add_argument("--metric", choices=("close", "high"))
    ap.add_argument("--no-news", action="store_true", help="skip CoinGecko/news (fast)")
    ap.add_argument("--open", action="store_true", help="open the report in the browser")
    ap.add_argument("--loop", type=int, metavar="MIN", help="repeat every MIN minutes (Ctrl+C to stop)")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "output"))
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.min is not None:
            cfg["min_change_pct"] = a.min
        if a.metric:
            cfg["metric"] = a.metric
        date = dt.date.fromisoformat(a.date) if a.date else None
        cache, opened = {}, False
        while True:
            shown, new, rp, n, errs = run_once(cfg, date, a.out, cache, not a.no_news)
            print("%s  scanned %d symbols (%d errors) -> %d hit(s), %d new" % (
                dt.datetime.now().strftime("%H:%M:%S"), n, errs, len(shown), len(new)))
            for h in shown:
                print("  %-14s %+8.1f%%  vol $%-8s %s" % (h["symbol"], h["change_pct"], fmt_usd(h["quote_vol"]),
                                                          " | ".join(h["tags"])))
            if new and telegram(cfg, "\n".join(summary_line(h) for h in new)):
                print("  telegram sent")
            print("  report:", rp)
            if a.open and shown and not opened:
                webbrowser.open("file:///" + rp.replace("\\", "/"))
                opened = True
            if not a.loop:
                return 0
            time.sleep(a.loop * 60)
    except KeyboardInterrupt:
        return 0
    except (ApiError, ValueError, OSError) as ex:
        print("ERROR:", ex, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

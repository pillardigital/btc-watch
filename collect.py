"""
btc-watch collector.

Pulls a small set of free sources, computes three scores (thesis, regime, tactical),
evaluates four alert rules, and writes docs/data.json for the static dashboard.

Every source is best-effort. If a fetch fails, the previous value is carried forward and
flagged stale so one broken API never blanks the page.
"""

import json
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta

import requests
import yaml

UA = {"User-Agent": "Mozilla/5.0 (btc-watch; +https://github.com)"}
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(ROOT, "docs", "data.json")
ALERTS_PATH = os.path.join(ROOT, "docs", "alerts.json")
NOW = datetime.now(timezone.utc)

CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))
CAT = yaml.safe_load(open(os.path.join(ROOT, "catalysts.yaml")))


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


PREV = load_json(DATA_PATH, {})
ALERT_LOG = load_json(ALERTS_PATH, [])
STATUS = {}  # source -> "ok" | "stale" | "missing"
ERRORS = {}  # source -> last error text, for debugging from the dashboard


def get(url, params=None, headers=None, timeout=25):
    h = dict(UA)
    if headers:
        h.update(headers)
    r = requests.get(url, params=params, headers=h, timeout=timeout)
    r.raise_for_status()
    return r


def fail(source, e):
    ERRORS[source] = str(e)[:300]
    print(f"{source} failed:", e, file=sys.stderr)


def carry(key, value, source):
    """Return fresh value if present, else previous value flagged stale."""
    if value is not None:
        STATUS[source] = "ok"
        return value
    prev = PREV.get(key)
    STATUS[source] = "stale" if prev is not None else "missing"
    return prev


# ---------------------------------------------------------------- price and technicals

def fetch_price_history():
    """Daily closes, full history. Coin Metrics community first (no key, no range cap),
    CoinGecko public API second (capped to the last 365 days, so no 200-week average)."""
    try:
        r = get("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
                params={"assets": "btc", "metrics": "PriceUSD", "frequency": "1d",
                        "page_size": 10000, "start_time": "2012-01-01"})
        rows = [float(x["PriceUSD"]) for x in r.json()["data"] if x.get("PriceUSD")]
        if len(rows) > 1500:
            # append today's live price so the last point is current, not yesterday's close
            try:
                live = get("https://api.coingecko.com/api/v3/simple/price",
                           params={"ids": "bitcoin", "vs_currencies": "usd"}).json()["bitcoin"]["usd"]
                rows.append(float(live))
            except Exception as e:
                fail("coingecko_live", e)
            return rows
    except Exception as e:
        fail("coinmetrics_price", e)
    r = get("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": "365"})
    return [p[1] for p in r.json()["prices"]]


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / n, sum(losses) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def technicals():
    try:
        closes = fetch_price_history()
    except Exception as e:
        fail("price", e)
        closes = None
    if not closes:
        keys = ["price", "dma50", "dma200", "wma200", "rsi14", "chg7d_pct", "chg30d_pct",
                "price_vs_200dma_pct", "weeks_below_200wma"]
        return {k: carry(k, None, "price") for k in keys}

    price = closes[-1]
    dma50 = statistics.mean(closes[-50:])
    dma200 = statistics.mean(closes[-200:])
    weekly = closes[::-1][::7][::-1]          # every 7th day back from today
    wma200 = statistics.mean(weekly[-200:]) if len(weekly) >= 200 else None
    weeks_below = 0
    if wma200:
        for i in range(1, 9):
            if len(weekly) >= 200 + i:
                w = statistics.mean(weekly[-200 - i:-i])
                if weekly[-i] < w:
                    weeks_below += 1
                else:
                    break
    out = {
        "price": round(price),
        "dma50": round(dma50),
        "dma200": round(dma200),
        "wma200": round(wma200) if wma200 else None,
        "rsi14": rsi(closes),
        "chg7d_pct": round((price / closes[-8] - 1) * 100, 1),
        "chg30d_pct": round((price / closes[-31] - 1) * 100, 1),
        "price_vs_200dma_pct": round((price / dma200 - 1) * 100, 1),
        "weeks_below_200wma": weeks_below,
    }
    STATUS["price"] = "ok"
    return out


# ---------------------------------------------------------------- on-chain (Coin Metrics community, no key)

def coinmetrics():
    """Realized price, MVRV, and 1y-dormant supply from Coin Metrics community (no key).
    Prefers direct single metrics; batches them in one request; records the exact
    server error (metric names and all) in ERRORS so failures are diagnosable."""
    keys = ["realized_price", "mvrv", "supply_untouched_1y_pct", "supply_untouched_1y_pct_30d_ago"]
    wanted = ["PriceRealizedUSD", "CapMVRVCur", "SplyAct1yr", "SplyCur", "CapRealUSD", "CapMrktCurUSD"]
    try:
        try:
            r = get("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
                    params={"assets": "btc", "metrics": ",".join(wanted), "frequency": "1d",
                            "page_size": 40, "paging_from": "end"})
            rows = r.json().get("data", [])
        except requests.HTTPError as e:
            # Community tier rejects the whole call if any metric is unsupported and names it.
            body = ""
            try:
                body = e.response.text[:400]
            except Exception:
                pass
            fail("coinmetrics_batch", f"{e} :: {body}")
            # Retry with only the widely-available core set.
            r = get("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
                    params={"assets": "btc", "metrics": "CapRealUSD,CapMrktCurUSD,SplyAct1yr,SplyCur",
                            "frequency": "1d", "page_size": 40, "paging_from": "end"})
            rows = r.json().get("data", [])
        if not rows:
            raise ValueError("no rows")
        def col(name):
            vals = [float(x[name]) for x in rows if x.get(name) is not None]
            return vals or None
        last = rows[-1]
        g = lambda k: float(last[k]) if last.get(k) is not None else None
        out = {}
        # realized price: direct metric first, else CapReal / supply
        rp = g("PriceRealizedUSD")
        if rp is None and g("CapRealUSD") and g("SplyCur"):
            rp = g("CapRealUSD") / g("SplyCur")
        if rp is not None:
            out["realized_price"] = round(rp)
        # mvrv: direct metric first, else market cap / realized cap
        mv = g("CapMVRVCur")
        if mv is None and g("CapMrktCurUSD") and g("CapRealUSD"):
            mv = g("CapMrktCurUSD") / g("CapRealUSD")
        if mv is not None:
            out["mvrv"] = round(mv, 2)
        # 1y-dormant supply, now and 30 days ago
        act, cur = col("SplyAct1yr"), col("SplyCur")
        if act and cur:
            out["supply_untouched_1y_pct"] = round((1 - act[-1] / cur[-1]) * 100, 1)
            i = max(0, len(act) - 31)
            j = max(0, len(cur) - 31)
            out["supply_untouched_1y_pct_30d_ago"] = round((1 - act[i] / cur[j]) * 100, 1)
        if not out:
            raise ValueError("no usable metrics")
        for k in keys:
            out.setdefault(k, PREV.get(k))
        STATUS["coinmetrics"] = "ok"
        return out
    except Exception as e:
        fail("coinmetrics", e)
        return {k: carry(k, None, "coinmetrics") for k in keys}


# ---------------------------------------------------------------- on-chain fallback (bitcoin-data.com, no key)

def bitcoin_data_fallback(cur):
    """Fill realized_price / mvrv / long-term-holder share if Coin Metrics did not. Best effort."""
    def last(path, *names):
        j = get("https://bitcoin-data.com/v1/" + path + "/last").json()
        for n in names:
            if isinstance(j, dict) and n in j:
                return float(j[n])
        for v in (j.values() if isinstance(j, dict) else []):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        raise ValueError("no numeric field in " + path)
    out = dict(cur)
    for key, path, names in (
        ("realized_price", "realized-price", ("realizedPrice", "realized_price", "value")),
        ("mvrv", "mvrv", ("mvrv", "value")),
    ):
        if out.get(key) is None:
            try:
                v = last(path, *names)
                if key == "supply_untouched_1y_pct" and v > 100:
                    v = v / 19.9e6 * 100 if v > 1e6 else v
                out[key] = round(v, 2 if key == "mvrv" else 1)
                STATUS["bitcoin_data"] = "ok"
            except Exception as e:
                fail("bitcoin_data_" + path, e)
    return out


# ---------------------------------------------------------------- exchange reserves (CryptoQuant free key, optional)

def exchange_reserve():
    keys = ["exchange_reserve_btc", "exchange_reserve_chg30d_btc"]
    key = os.getenv("CRYPTOQUANT_API_KEY")
    if not key:
        STATUS["cryptoquant"] = "missing"
        return {k: PREV.get(k) for k in keys}
    try:
        r = get("https://api.cryptoquant.com/v1/btc/exchange-flows/reserve",
                params={"exchange": "all_exchange", "window": "day", "limit": 31},
                headers={"Authorization": f"Bearer {key}"})
        rows = r.json()["result"]["data"]
        cur, ago = float(rows[0]["reserve"]), float(rows[-1]["reserve"])
        out = {"exchange_reserve_btc": round(cur), "exchange_reserve_chg30d_btc": round(cur - ago)}
        STATUS["cryptoquant"] = "ok"
        return out
    except Exception as e:
        fail("cryptoquant", e)
        return {k: carry(k, None, "cryptoquant") for k in keys}


# ---------------------------------------------------------------- ETF flows (Farside table)

def etf_flows():
    """Spot BTC ETF net flow (US$m), streak, and latest date.
    SoSoValue's open endpoint first (JSON, no key, not Cloudflare-blocked); Farside HTML last.
    Values are US$ millions, positive = net inflow."""
    keys = ["etf_flow_last_musd", "etf_flow_7d_musd", "etf_streak", "etf_last_date"]

    def finalize(vals):
        # vals: list of (date_str, flow_musd) oldest..newest
        last_date, last = vals[-1]
        streak, sign = 0, (1 if last > 0 else -1)
        for _, v in reversed(vals):
            if (v > 0 and sign > 0) or (v < 0 and sign < 0):
                streak += 1
            else:
                break
        return {"etf_flow_last_musd": round(last, 1),
                "etf_flow_7d_musd": round(sum(v for _, v in vals[-5:]), 1),
                "etf_streak": streak * sign,
                "etf_last_date": last_date}

    # 1) SoSoValue open API
    try:
        r = get("https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart",
                params={"type": "us-btc-spot"},
                headers={"Accept": "application/json"})
        j = r.json()
        data = j.get("data") or j.get("result") or []
        vals = []
        for row in data:
            ts = row.get("date") or row.get("time") or row.get("timestamp")
            flow = row.get("totalNetInflow", row.get("netInflow", row.get("value")))
            if ts is None or flow is None:
                continue
            # timestamps may be ms epoch or ISO; flows may be raw USD
            import datetime as _dt
            if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
                ds = _dt.datetime.utcfromtimestamp(int(ts) / (1000 if int(ts) > 1e12 else 1)).date().isoformat()
            else:
                ds = str(ts)[:10]
            fv = float(flow)
            if abs(fv) > 1e6:      # raw USD -> millions
                fv /= 1e6
            vals.append((ds, fv))
        if len(vals) >= 5:
            out = finalize(vals)
            STATUS["etf"] = "ok"
            return out
        raise ValueError("sosovalue returned too few rows")
    except Exception as e:
        fail("etf_sosovalue", e)

    # 2) Farside HTML (often Cloudflare-blocked from CI, kept as last resort)
    try:
        import pandas as pd
        from io import StringIO
        hdr = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
        html = get("https://farside.co.uk/btc/", headers=hdr).text
        t = max(pd.read_html(StringIO(html)), key=len)
        t.columns = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in t.columns]
        date_col = t.columns[0]
        total_col = [c for c in t.columns if "total" in c.lower()][-1]
        vals = []
        for _, row in t.iterrows():
            d = str(row[date_col])
            v = str(row[total_col]).replace(",", "").replace("(", "-").replace(")", "")
            try:
                fv = float(v)
            except ValueError:
                continue
            if any(ch.isdigit() for ch in d) and "total" not in d.lower():
                vals.append((d, fv))
        if not vals:
            raise ValueError("no rows parsed")
        out = finalize(vals)
        STATUS["etf"] = "ok"
        return out
    except Exception as e:
        fail("etf_farside", e)
        return {k: carry(k, None, "etf") for k in keys}


# ---------------------------------------------------------------- ETF custody proxy (iShares IBIT holdings file, no key)

IBIT_CSV = ("https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf/"
            "1467271812596.ajax?fileType=csv&fileName=IBIT_holdings&dataType=fund")


def ibit_holdings():
    """BTC held by IBIT, straight from BlackRock. Daily change x price is a flow measure that
    does not depend on any third-party scraper. Keeps a 40-day history in data.json."""
    hist = PREV.get("ibit_history", [])
    try:
        text = get(IBIT_CSV, headers={"Accept": "text/csv,*/*"}).text
        import csv, io
        btc = None
        reader = csv.reader(io.StringIO(text))
        for cells in reader:
            joined = " ".join(cells).upper()
            # match the bitcoin holding row: contains BITCOIN, or a standalone BTC / XBT token
            toks = [c.strip().upper() for c in cells]
            if "BITCOIN" in joined or "BTC" in toks or "XBT" in toks:
                nums = []
                for c in cells:
                    cc = c.replace(",", "").replace('"', "").strip()
                    try:
                        nums.append(float(cc))
                    except ValueError:
                        pass
                nums = [n for n in nums if 100000 < n < 10000000]
                if nums:
                    btc = max(nums)   # the coin quantity is the largest plausible number on the row
                    break
        if btc is None:
            raise ValueError("BTC row not found in holdings CSV")
        today = NOW.date().isoformat()
        if not hist or hist[-1]["date"] != today:
            hist.append({"date": today, "btc": round(btc)})
        else:
            hist[-1]["btc"] = round(btc)
        hist = hist[-40:]
        chg1 = round(hist[-1]["btc"] - hist[-2]["btc"]) if len(hist) >= 2 else None
        chg7 = round(hist[-1]["btc"] - hist[max(0, len(hist) - 8)]["btc"]) if len(hist) >= 2 else None
        STATUS["ibit"] = "ok"
        return {"ibit_btc": round(btc), "ibit_chg1d_btc": chg1, "ibit_chg7d_btc": chg7, "ibit_history": hist}
    except Exception as e:
        fail("ibit", e)
        return {"ibit_btc": PREV.get("ibit_btc"), "ibit_chg1d_btc": PREV.get("ibit_chg1d_btc"),
                "ibit_chg7d_btc": PREV.get("ibit_chg7d_btc"), "ibit_history": hist}


# ---------------------------------------------------------------- stablecoins (DefiLlama, no key)

def stablecoins():
    keys = ["stable_supply_busd", "stable_chg30d_pct"]
    try:
        rows = get("https://stablecoins.llama.fi/stablecoincharts/all").json()
        cur = rows[-1]["totalCirculatingUSD"]["peggedUSD"]
        ago = rows[-31]["totalCirculatingUSD"]["peggedUSD"]
        out = {"stable_supply_busd": round(cur / 1e9, 1),
               "stable_chg30d_pct": round((cur / ago - 1) * 100, 2)}
        STATUS["defillama"] = "ok"
        return out
    except Exception as e:
        fail("defillama", e)
        return {k: carry(k, None, "defillama") for k in keys}


# ---------------------------------------------------------------- funding (Bybit first, Binance fallback; no key)

def funding():
    keys = ["funding_3d_avg_pct"]
    now_ms = int(NOW.timestamp() * 1000)
    venues = [
        # (name, url, params, extractor -> list of per-8h rates as fractions)
        ("bitmex", "https://www.bitmex.com/api/v1/funding",
         {"symbol": "XBTUSD", "count": 9, "reverse": "true"},
         lambda j: [float(x["fundingRate"]) for x in j]),
        ("okx", "https://www.okx.com/api/v5/public/funding-rate-history",
         {"instId": "BTC-USDT-SWAP", "limit": 9},
         lambda j: [float(x["fundingRate"]) for x in j["data"]]),
        ("deribit", "https://www.deribit.com/api/v2/public/get_funding_rate_history",
         {"instrument_name": "BTC-PERPETUAL", "start_timestamp": now_ms - 3 * 86400000, "end_timestamp": now_ms},
         lambda j: [float(x["interest_8h"]) for x in j["result"]]),
        ("bybit", "https://api.bybit.com/v5/market/funding/history",
         {"category": "linear", "symbol": "BTCUSDT", "limit": 9},
         lambda j: [float(x["fundingRate"]) for x in j["result"]["list"]]),
    ]
    for name, url, params, extract in venues:
        try:
            rates = extract(get(url, params=params).json())
            if not rates:
                raise ValueError("empty")
            out = {"funding_3d_avg_pct": round(statistics.mean(rates) * 100, 4), "funding_venue": name}
            STATUS["funding"] = "ok"
            return out
        except Exception as e:
            fail("funding_" + name, e)
    return {k: carry(k, None, "funding") for k in keys}


# ---------------------------------------------------------------- fear and greed (no key)

def fear_greed():
    keys = ["fear_greed"]
    try:
        j = get("https://api.alternative.me/fng/", params={"limit": 1}).json()
        out = {"fear_greed": int(j["data"][0]["value"])}
        STATUS["fng"] = "ok"
        return out
    except Exception as e:
        fail("fng", e)
        return {k: carry(k, None, "fng") for k in keys}


# ---------------------------------------------------------------- scores

def sei_score():
    total_w, acc, pillars = 0, 0.0, []
    for pid, p in CAT["pillars"].items():
        items = p["items"].values()
        pct = statistics.mean(i["maturity"] for i in items) / 3 * 100
        pillars.append({"id": pid, "label": p["label"], "score": round(pct), "weight": p["weight"],
                        "items": [{"label": i["label"], "maturity": i["maturity"]} for i in p["items"].values()]})
        acc += pct * p["weight"]
        total_w += p["weight"]
    return round(acc / total_w), pillars


def regime(d):
    """Plain-language cycle position from what we can measure for free."""
    p, r200, real = d.get("price"), d.get("dma200"), d.get("realized_price")
    flow7 = d.get("etf_flow_7d_musd")
    if p is None or r200 is None:
        return "Unknown"
    if real and p < real:
        return "Deep value"
    if p < r200 and (flow7 is None or flow7 <= 0):
        return "Contraction"
    if p < r200:
        return "Accumulation"
    if d.get("rsi14", 50) > 75 and (d.get("fear_greed") or 50) > 80:
        return "Euphoria"
    return "Expansion"


def light_and_alerts(d, sei):
    c = CFG
    reasons, fired = [], []
    f = d.get("funding_3d_avg_pct")
    fg = d.get("fear_greed")
    dev = d.get("price_vs_200dma_pct")
    rsi14 = d.get("rsi14")
    streak = d.get("etf_streak") or 0
    mvrv = d.get("mvrv")

    # Kill switch first
    ks = c["kill_switch"]
    if (d.get("weeks_below_200wma") or 0) >= ks["weeks_below_200wma"] and mvrv is not None and mvrv < ks["mvrv_max"]:
        fired.append(("kill_switch",
                      f"Kill switch: {d['weeks_below_200wma']} weekly closes below the 200-week average with MVRV {mvrv}. "
                      f"No new long-dated exposure. Invalidation: weekly close back above ${d['wma200']:,}."))
        return "red", "Kill-switch conditions met", fired

    # Structural add
    s = c["structural_add"]
    conds = {
        "thesis": sei >= s["sei_min"],
        "price below 200-day": dev is not None and dev <= s["price_vs_200dma_max_pct"],
        "funding flat or negative": f is not None and f <= s["funding_max"],
        "fear": fg is not None and fg <= s["fear_greed_max"],
        "ETF outflow streak": streak <= -s["etf_outflow_streak_min"],
    }
    hits = [k for k, v in conds.items() if v]
    if conds["thesis"] and len(hits) >= 4:
        fired.append(("structural_add",
                      f"Structural add window. Thesis {sei}/100, price {dev}% vs 200-day, funding {f}%, fear {fg}, "
                      f"ETF streak {streak}. Long-dated calls. Invalidation: weekly close below ${d.get('wma200') or 0:,}."))
        return "green", "Structural add window: " + ", ".join(hits), fired

    # Swing entry
    w = c["swing_entry"]
    if sei >= w["sei_min"] and rsi14 is not None and rsi14 <= w["rsi14_max"] and f is not None and f <= w["funding_max"] and fg is not None and fg <= w["fear_greed_max"]:
        fired.append(("swing_entry",
                      f"Swing entry. RSI {rsi14}, funding {f}%, fear {fg}, thesis {sei}. 30-90 day calls. "
                      f"Invalidation: daily close below this week's low."))
        return "green", f"Swing entry: oversold (RSI {rsi14}) with negative funding", fired

    # Trim
    t = c["trim"]
    hot = [k for k, v in {
        f"RSI {rsi14}": rsi14 is not None and rsi14 >= t["rsi14_min"],
        f"funding {f}%": f is not None and f >= t["funding_min"],
        f"greed {fg}": fg is not None and fg >= t["fear_greed_min"],
        f"{dev}% above 200-day": dev is not None and dev >= t["price_vs_200dma_min_pct"],
    }.items() if v]
    if len(hot) >= 2:
        fired.append(("trim", "Trim or hedge. " + ", ".join(hot) + ". Sell covered calls or buy put spreads against structural holdings."))
        return "amber", "Stretched: " + ", ".join(hot), fired

    if sei < 45:
        return "amber", f"Thesis score {sei} is below the comfort line; tactical only", fired
    return "grey", "Nothing to do. Watching.", fired


# ---------------------------------------------------------------- alert delivery

def send(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=15)
        except Exception as e:
            fail("telegram", e)
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=text.encode(), timeout=15)
        except Exception as e:
            fail("ntfy", e)


def dispatch(fired):
    cooldown = timedelta(days=CFG["alerts"]["cooldown_days"])
    sent = []
    for kind, text in fired:
        recent = [a for a in ALERT_LOG if a["kind"] == kind
                  and NOW - datetime.fromisoformat(a["ts"]) < cooldown]
        if recent:
            continue
        send("BTC watch: " + text)
        entry = {"ts": NOW.isoformat(), "kind": kind, "text": text}
        ALERT_LOG.insert(0, entry)
        sent.append(entry)
    return sent


# ---------------------------------------------------------------- main

def main():
    d = {}
    for fn in (technicals, coinmetrics, exchange_reserve, etf_flows, ibit_holdings, stablecoins, funding, fear_greed):
        d.update(fn())
    d.update(bitcoin_data_fallback(d))

    sei, pillars = sei_score()
    d["sei"] = sei
    d["sei_pillars"] = pillars
    hist = PREV.get("sei_history", [])
    today = NOW.date().isoformat()
    if not hist or hist[-1]["date"] != today:
        hist.append({"date": today, "sei": sei})
    d["sei_history"] = hist[-400:]
    d["regime"] = regime(d)
    d["light"], d["light_reason"], fired = light_and_alerts(d, sei)
    d["events"] = [{**e, "date": str(e["date"])} for e in CAT.get("events", [])[:10]]
    d["issuance_btc_per_day"] = CFG["issuance_btc_per_day"]

    # Mechanical-selling hint: outflows while price holds is usually basis unwind, not conviction selling
    streak, chg7 = d.get("etf_streak") or 0, d.get("chg7d_pct")
    d["etf_note"] = ("Outflows while price holds: likely mechanical (CME basis unwind), not conviction selling."
                     if streak <= -3 and chg7 is not None and chg7 > -3 else "")

    d["alerts_sent"] = dispatch(fired)
    d["sources"] = STATUS
    d["errors"] = ERRORS
    d["updated"] = NOW.isoformat(timespec="minutes")

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(d, f, indent=1)
    with open(ALERTS_PATH, "w") as f:
        json.dump(ALERT_LOG[:100], f, indent=1)
    print(json.dumps({k: v for k, v in d.items() if k not in ("sei_history", "events", "sei_pillars", "ibit_history")}, indent=1))


if __name__ == "__main__":
    main()

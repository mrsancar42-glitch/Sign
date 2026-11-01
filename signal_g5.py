#!/usr/bin/env python3
"""SIGNAL G5 FULL ICT — OKX USDT perpetual PAPER scanner. No orders are placed."""
import os, sys, json, time, requests
from datetime import datetime, timezone

BASE = "https://www.okx.com"
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MIN_VOL = float(os.getenv("MIN_24H_QUOTE_VOL", "3000000"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "100"))
COOLDOWN = int(os.getenv("COOLDOWN_MINUTES", "90"))
REQUIRE_KILLZONE = os.getenv("REQUIRE_KILLZONE", "0") == "1"
STATE_FILE = os.getenv("STATE_FILE", "signal_g5_ict_state.json")
S = requests.Session()
S.headers.update({"User-Agent": "SignalG5-FullICT-Paper/1.0"})


def api(path, params=None):
    r = S.get(BASE + path, params=params, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "0":
        raise RuntimeError(j.get("msg", "OKX API error"))
    return j.get("data", [])


def send(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID.")
    r = S.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
               json={"chat_id": CHAT_ID, "text": text}, timeout=15)
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError("Telegram sendMessage failed")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(data):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def candles(symbol, tf, limit=100):
    rows = api("/api/v5/market/candles", {"instId": symbol, "bar": tf, "limit": str(limit)})
    rows = [r for r in rows if len(r) > 8 and r[8] == "1"]
    rows.reverse()
    return [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
             "l": float(r[3]), "c": float(r[4]), "qv": float(r[7] or 0)}
            for r in rows]


def swings(c, left=2, right=2):
    hi, lo = [], []
    for i in range(left, len(c)-right):
        if all(c[i]["h"] > c[j]["h"] for j in range(i-left, i+right+1) if j != i):
            hi.append((i, c[i]["h"]))
        if all(c[i]["l"] < c[j]["l"] for j in range(i-left, i+right+1) if j != i):
            lo.append((i, c[i]["l"]))
    return hi, lo


def bias(c):
    hi, lo = swings(c)
    if len(hi) < 2 or len(lo) < 2:
        return "NEUTRAL"
    if hi[-1][1] > hi[-2][1] and lo[-1][1] > lo[-2][1]:
        return "BULLISH"
    if hi[-1][1] < hi[-2][1] and lo[-1][1] < lo[-2][1]:
        return "BEARISH"
    return "NEUTRAL"


def sweep(c, n=20):
    if len(c) < n + 2:
        return None
    prior, last = c[-n-1:-1], c[-1]
    ph = max(x["h"] for x in prior)
    pl = min(x["l"] for x in prior)
    if last["h"] > ph and last["c"] < ph:
        return ("BSL Sweep", "SHORT", ph)
    if last["l"] < pl and last["c"] > pl:
        return ("SSL Sweep", "LONG", pl)
    return None


def mss(c, side, n=12):
    w, close = c[-n-1:-1], c[-1]["c"]
    if len(w) < n:
        return False
    return close < min(x["l"] for x in w) if side == "SHORT" else close > max(x["h"] for x in w)


def fvg(c, side):
    for i in range(len(c)-1, 1, -1):
        a, z = c[i-2], c[i]
        if side == "LONG" and a["h"] < z["l"]:
            return (a["h"], z["l"])
        if side == "SHORT" and a["l"] > z["h"]:
            return (z["h"], a["l"])
    return None


def order_block(c, side):
    for i in range(len(c)-1, max(1, len(c)-15), -1):
        p, d = c[i-1], c[i]
        span = max(d["h"] - d["l"], 1e-12)
        if abs(d["c"] - d["o"]) / span < .60:
            continue
        if side == "LONG" and d["c"] > d["o"] and p["c"] < p["o"]:
            return (p["l"], p["h"])
        if side == "SHORT" and d["c"] < d["o"] and p["c"] > p["o"]:
            return (p["l"], p["h"])
    return None


def premium_discount(c, side):
    w = c[-40:]
    mid = (max(x["h"] for x in w) + min(x["l"] for x in w)) / 2
    return c[-1]["c"] < mid if side == "LONG" else c[-1]["c"] > mid


def killzone():
    now = datetime.now(timezone.utc)
    m = now.hour * 60 + now.minute
    return 7*60 <= m < 10*60 or 12*60 <= m < 15*60


def analyze(symbol, ticker):
    vol24 = float(ticker.get("volCcy24h") or 0)
    if vol24 < MIN_VOL:
        return None
    c4, c1, c15, c5 = (candles(symbol, tf) for tf in ("4H", "1H", "15m", "5m"))
    if min(map(len, (c4, c1, c15, c5))) < 35:
        return None
    b4, b1 = bias(c4), bias(c1)
    if b4 == "NEUTRAL" or b4 != b1:
        return None
    sw = sweep(c15)
    if not sw:
        return None
    sweep_name, side, sweep_level = sw
    if (side == "LONG" and b4 != "BULLISH") or (side == "SHORT" and b4 != "BEARISH"):
        return None
    if not mss(c5, side):
        return None
    gap, ob = fvg(c5, side), order_block(c5, side)
    if not gap or not ob or not premium_discount(c1, side):
        return None
    if REQUIRE_KILLZONE and not killzone():
        return None

    # Require overlapping/nearby FVG and OB zones.
    lo, hi = max(min(gap), min(ob)), min(max(gap), max(ob))
    if lo > hi:
        if lo - hi > c5[-1]["c"] * .002:
            return None
        lo, hi = min(gap), max(gap)
    entry = (lo + hi) / 2
    recent = c5[-20:]
    if side == "LONG":
        sl = min(x["l"] for x in recent)
        risk = entry - sl
        tp = entry + 2 * risk
    else:
        sl = max(x["h"] for x in recent)
        risk = sl - entry
        tp = entry - 2 * risk
    if risk <= 0 or tp <= 0 or not .10 <= risk / entry * 100 <= 3:
        return None

    score = 8  # HTF alignment + sweep + MSS + FVG + OB + PD
    score += int(vol24 >= 20_000_000) + int(killzone())
    return {"symbol": symbol, "side": side, "entry": entry, "tp": tp,
            "sl": sl, "score": score, "sweep": sweep_name, "vol24": vol24}


def fmt(x):
    if x >= 1000: return f"{x:.2f}"
    if x >= 1: return f"{x:.5f}".rstrip("0").rstrip(".")
    if x >= .01: return f"{x:.6f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def message(x):
    coin = x["symbol"].replace("-USDT-SWAP", "")
    return ("🎯 SIGNAL G5 — FULL ICT\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Монета: {coin}-USDT\n"
            f"Сценарий: {x['side']} (paper)\n"
            f"HTF Bias: {'Bullish' if x['side']=='LONG' else 'Bearish'}\n"
            f"Liquidity: {x['sweep']}\n"
            "Structure: MSS\nConfirmation: FVG + OB\n"
            f"Entry reference: {fmt(x['entry'])}\n"
            f"TP: {fmt(x['tp'])}\nSL: {fmt(x['sl'])}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "ICT PAPER SETUP · Учебная симуляция, не торговая рекомендация.")


def main():
    inst = api("/api/v5/public/instruments", {"instType": "SWAP"})
    tick = api("/api/v5/market/tickers", {"instType": "SWAP"})
    tm = {x["instId"]: x for x in tick}
    markets = []
    for x in inst:
        sym = x.get("instId", "")
        t = tm.get(sym)
        if (x.get("state") == "live" and x.get("settleCcy") == "USDT"
                and sym.endswith("-USDT-SWAP") and t
                and float(t.get("volCcy24h") or 0) >= MIN_VOL):
            markets.append((float(t.get("volCcy24h") or 0), sym, t))
    markets.sort(reverse=True, key=lambda z: z[0])
    markets = markets[:MAX_MARKETS]
    print(f"Scanning {len(markets)} liquid OKX USDT swaps...")
    found = []
    for _, sym, t in markets:
        try:
            x = analyze(sym, t)
            if x:
                found.append(x)
                print("ICT candidate:", sym, "score", x["score"])
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as e:
            print("Skipped", sym, e)
        time.sleep(.08)
    if not found:
        print("No complete ICT setup; no Telegram message sent.")
        return
    found.sort(key=lambda x: (x["score"], x["vol24"]), reverse=True)
    best = found[0]
    state = load_state()
    now = int(time.time())
    if best["symbol"] == state.get("last_symbol") and now - int(state.get("last_sent_ts", 0)) < COOLDOWN*60:
        print("Same top instrument is in cooldown; no message.")
        return
    send(message(best))
    save_state({"last_symbol": best["symbol"], "last_sent_ts": now, "last_side": best["side"]})
    print("Sent one Full ICT paper setup:", best["symbol"], best["side"])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL:", e, file=sys.stderr)
        sys.exit(1)

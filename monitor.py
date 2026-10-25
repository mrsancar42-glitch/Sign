#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import ccxt
import pandas as pd

# ---------- конфигурация ----------
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
COINS_ENV = os.environ.get("COINS", "ALL").upper()
FIXED_COINS = None if COINS_ENV in ("", "ALL") else [c.strip() for c in COINS_ENV.split(",")]

def load_params():
    return {
        "timeframes": ["5m", "15m", "1h"],
        "limit": 200,
        "rsi_n": 14,
        "rsi_long_max": 65,
        "rsi_short_min": 35,
        "ema_fast": 9,
        "ema_slow": 21,
        "ema_trend": 50,
        "vol_mult": 1.8,
        "sl_atr_mult": 1.5,
        "rr": 2.0,
        "cooldown_min": 60,
        "max_alerts": 5,
        "min_turnover_usd": 20_000_000,
        "workers": 6,
        "min_score": 3,
    }

# ---------- индикаторы на чистом pandas ----------
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    return true_range.rolling(period).mean()

def add_indicators(df, P):
    df = df.copy()
    df["rsi"] = rsi(df["close"], P["rsi_n"])
    df["ema_fast"] = ema(df["close"], P["ema_fast"])
    df["ema_slow"] = ema(df["close"], P["ema_slow"])
    df["ema_trend"] = ema(df["close"], P["ema_trend"])
    df["atr"] = atr(df, 14)
    df["vol_ma"] = df["volume"].rolling(P["rsi_n"]).mean()
    return df

# ---------- биржи ----------
def get_exchange(name):
    return getattr(ccxt, name)({"enableRateLimit": True, "options": {"defaultType": "swap"}})

def fetch_universe(exchange, min_turnover, max_coins):
    try:
        markets = exchange.load_markets()
        tickers = exchange.fetch_tickers()
    except Exception:
        return []
    rows = []
    for sym, m in markets.items():
        if not m.get("swap") or m.get("quote") != "USDT":
            continue
        t = tickers.get(sym)
        if t and t.get("quoteVolume") and t["quoteVolume"] >= min_turnover:
            rows.append((t["quoteVolume"], sym))
    rows.sort(reverse=True)
    return [sym for _, sym in rows[:max_coins]]

def fetch_ohlcv(exchange, symbol, timeframe, limit):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df
    except Exception:
        return None

# ---------- анализ ----------
def find_order_block(df, bullish=True):
    for i in range(len(df) - 3, 1, -1):
        prior = df.iloc[i - 1]
        trio = df.iloc[i:i+3]
        if bullish:
            opposite = prior["close"] < prior["open"]
            continuation = all(trio["close"] > trio["open"])
        else:
            opposite = prior["close"] > prior["open"]
            continuation = all(trio["close"] < trio["open"])
        if opposite and continuation:
            return {"high": prior["high"], "low": prior["low"]}
    return None

def analyze_symbol(symbol, exchange, P):
    dfs = {}
    for tf in P["timeframes"]:
        df = fetch_ohlcv(exchange, symbol, tf, P["limit"])
        if df is None or len(df) < P["limit"] // 2:
            return None
        dfs[tf] = add_indicators(df, P)

    df5 = dfs["5m"]
    df15 = dfs["15m"]
    df1h = dfs["1h"]
    last5 = df5.iloc[-1]
    last15 = df15.iloc[-1]
    last1h = df1h.iloc[-1]

    trend_up = last1h["close"] > last1h["ema_trend"]
    trend_down = last1h["close"] < last1h["ema_trend"]

    long_score, short_score = 0, 0
    if trend_up: long_score += 1
    if trend_down: short_score += 1
    if last5["rsi"] < P["rsi_long_max"] and last15["rsi"] < P["rsi_long_max"]: long_score += 1
    if last5["rsi"] > P["rsi_short_min"] and last15["rsi"] > P["rsi_short_min"]: short_score += 1
    if last5["volume"] > P["vol_mult"] * last5["vol_ma"]:
        long_score += 1
        short_score += 1
    
    ob = find_order_block(df5, bullish=True)
    if ob and last5["low"] <= ob["high"] and last5["close"] > ob["low"]: long_score += 1
    ob_short = find_order_block(df5, bullish=False)
    if ob_short and last5["high"] >= ob_short["low"] and last5["close"] < ob_short["high"]: short_score += 1
    
    if last5["ema_fast"] > last5["ema_slow"]: long_score += 1
    if last5["ema_fast"] < last5["ema_slow"]: short_score += 1

    side, score = None, 0
    if long_score >= P["min_score"] and long_score > short_score:
        side, score = "LONG", long_score
    elif short_score >= P["min_score"] and short_score > long_score:
        side, score = "SHORT", short_score
    else:
        return None

    entry = last5["close"]
    dist = P["sl_atr_mult"] * last5["atr"] if last5["atr"] and last5["atr"] > 0 else entry * 0.01
    sl = entry - dist if side == "LONG" else entry + dist
    tp = entry + P["rr"] * dist if side == "LONG" else entry - P["rr"] * dist

    return {"symbol": symbol.split("/")[0], "side": side, "entry": entry, "tp": tp, "sl": sl, "score": score}

def scan_all(P):
    exchanges = []
    for name in ["bybit", "binance", "okx"]:
        try:
            ex = get_exchange(name)
            ex.load_markets()
            exchanges.append(ex)
        except Exception:
            pass

    if not exchanges: return []
    primary = next((e for e in exchanges if e.id == "bybit"), exchanges[0])
    universe = fetch_universe(primary, P["min_turnover_usd"], 100)
    if FIXED_COINS: universe = [s for s in universe if s.split("/")[0] in FIXED_COINS]

    def work(sym):
        for ex in exchanges:
            if sym in ex.symbols:
                try:
                    sig = analyze_symbol(sym, ex, P)
                    if sig: return sig
                except Exception: continue
        return None

    with ThreadPoolExecutor(max_workers=P["workers"]) as pool:
        results = list(pool.map(work, universe))
    signals = [s for s in results if s]
    signals.sort(key=lambda s: s["score"], reverse=True)
    return signals[:P["max_alerts"]]

# ---------- отправка ----------
def build_message(sig):
    arrow = "🟢 LONG" if sig["side"] == "LONG" else "🔴 SHORT"
    return f"Монета: {sig['symbol']}USDT | {arrow}\nTP: {sig['tp']:.6g}\nSL: {sig['sl']:.6g}"

def send_telegram(token, chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=15) as r:
        if not json.loads(r.read().decode()).get("ok"): raise RuntimeError("telegram error")

def load_state(path):
    try:
        with open(path) as f: return json.load(f)
    except: return {"sent": {}}

def save_state(path, state):
    with open(path, "w") as f: json.dump(state, f, indent=1)

# ---------- main ----------
def main(argv):
    P = load_params()
    dry, test = "--dry" in argv, "--test" in argv
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if test:
        msg = "✅ Бот запущен. Многофакторный анализ активен."
        if not dry and token and chat_id:
            send_telegram(token, chat_id, msg)
        print(msg)
        return 0

    signals = scan_all(P)
    if not signals:
        print("Сигналов не найдено.")
        return 0

    state = load_state(STATE_FILE)
    now = time.time()
    for sig in signals:
        key = f"{sig['symbol']}|{sig['side']}"
        if now - state["sent"].get(key, 0) < P["cooldown_min"] * 60: continue
        msg = build_message(sig)
        if not dry and token and chat_id:
            send_telegram(token, chat_id, msg)
        else:
            print(msg + "\n---")
        state["sent"][key] = now
    
    if not dry: save_state(STATE_FILE, state)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

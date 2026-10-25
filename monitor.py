#!/usr/bin/env python3
"""Многофакторный сканер крипто-сигналов (Bybit, Binance, OKX).

Анализирует RSI, EMA, объёмы и Order Blocks на нескольких таймфреймах.
Выдаёт краткий сигнал: монета, направление, TP, SL.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import ccxt
import pandas as pd
import pandas_ta as ta

# ---------- конфигурация ----------
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
COINS_ENV = os.environ.get("COINS", "ALL").upper()
FIXED_COINS = None if COINS_ENV in ("", "ALL") else [c.strip() for c in COINS_ENV.split(",")]

def load_params():
    return {
        "timeframes": ["5m", "15m", "1h"],      # анализируем три ТФ
        "limit": 200,                            # свечей на ТФ
        "rsi_n": 14,
        "rsi_long_max": 65,                      # не покупаем выше
        "rsi_short_min": 35,                     # не шортим ниже
        "ema_fast": 9,
        "ema_slow": 21,
        "ema_trend": 50,
        "vol_mult": 1.8,                         # всплеск объёма
        "sl_atr_mult": 1.5,
        "rr": 2.0,
        "cooldown_min": 60,
        "max_alerts": 5,
        "min_turnover_usd": 20_000_000,
        "workers": 6,
        "min_score": 3,                          # минимум совпавших факторов
    }

# ---------- биржи через CCXT ----------
def get_exchange(name):
    """Возвращает объект биржи CCXT с публичным доступом."""
    ex = getattr(ccxt, name)({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},      # фьючерсы/перпетуалы
    })
    return ex

def fetch_universe(exchange, min_turnover, max_coins):
    """Список ликвидных USDT-перпетуалов с оборотом > min_turnover."""
    try:
        markets = exchange.load_markets()
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"Ошибка загрузки рынков {exchange.id}: {e}")
        return []
    rows = []
    for sym, m in markets.items():
        if not m.get("swap") or m.get("quote") != "USDT":
            continue
        t = tickers.get(sym)
        if not t or not t.get("quoteVolume"):
            continue
        if t["quoteVolume"] >= min_turnover:
            rows.append((t["quoteVolume"], sym))
    rows.sort(reverse=True)
    return [sym for _, sym in rows[:max_coins]]

def fetch_ohlcv(exchange, symbol, timeframe, limit):
    """Свечи в виде DataFrame."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df
    except Exception:
        return None

# ---------- индикаторы и паттерны ----------
def add_indicators(df, P):
    df = df.copy()
    df["rsi"] = ta.rsi(df["close"], length=P["rsi_n"])
    df["ema_fast"] = ta.ema(df["close"], length=P["ema_fast"])
    df["ema_slow"] = ta.ema(df["close"], length=P["ema_slow"])
    df["ema_trend"] = ta.ema(df["close"], length=P["ema_trend"])
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(P["rsi_n"]).mean()
    return df

def find_order_block(df, bullish=True):
    """Ищет последний Order Block: противоположная свеча перед импульсом из 3 свечей."""
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
    """Возвращает сигнал или None."""
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

    # --- Фильтр тренда по 1h ---
    trend_up = last1h["close"] > last1h["ema_trend"]
    trend_down = last1h["close"] < last1h["ema_trend"]

    # --- Условия для LONG ---
    long_score = 0
    long_reasons = []
    if trend_up:
        long_score += 1
        long_reasons.append("1h trend up")
    if last5["rsi"] < P["rsi_long_max"] and last15["rsi"] < P["rsi_long_max"]:
        long_score += 1
        long_reasons.append("RSI not overbought")
    if last5["volume"] > P["vol_mult"] * last5["vol_ma"]:
        long_score += 1
        long_reasons.append("volume spike")
    ob = find_order_block(df5, bullish=True)
    if ob and last5["low"] <= ob["high"] and last5["close"] > ob["low"]:
        long_score += 1
        long_reasons.append("bullish OB")
    if last5["ema_fast"] > last5["ema_slow"]:
        long_score += 1
        long_reasons.append("EMA 9>21")

    # --- Условия для SHORT ---
    short_score = 0
    short_reasons = []
    if trend_down:
        short_score += 1
        short_reasons.append("1h trend down")
    if last5["rsi"] > P["rsi_short_min"] and last15["rsi"] > P["rsi_short_min"]:
        short_score += 1
        short_reasons.append("RSI not oversold")
    if last5["volume"] > P["vol_mult"] * last5["vol_ma"]:
        short_score += 1
        short_reasons.append("volume spike")
    ob_short = find_order_block(df5, bullish=False)
    if ob_short and last5["high"] >= ob_short["low"] and last5["close"] < ob_short["high"]:
        short_score += 1
        short_reasons.append("bearish OB")
    if last5["ema_fast"] < last5["ema_slow"]:
        short_score += 1
        short_reasons.append("EMA 9<21")

    # --- Выбор направления ---
    side = None
    score = 0
    reasons = []
    if long_score >= P["min_score"] and long_score > short_score:
        side = "LONG"
        score = long_score
        reasons = long_reasons
    elif short_score >= P["min_score"] and short_score > long_score:
        side = "SHORT"
        score = short_score
        reasons = short_reasons
    else:
        return None

    # --- Расчёт TP/SL ---
    entry = last5["close"]
    atr = last5["atr"]
    dist = P["sl_atr_mult"] * atr if atr and atr > 0 else entry * 0.01
    if side == "LONG":
        sl = entry - dist
        tp = entry + P["rr"] * dist
    else:
        sl = entry + dist
        tp = entry - P["rr"] * dist

    return {
        "symbol": symbol.split("/")[0],
        "side": side,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "score": score,
        "reasons": reasons,
        "exchange": exchange.id,
    }

# ---------- сканирование ----------
def scan_all(P):
    """Сканирует все доступные биржи, возвращает лучшие сигналы."""
    exchanges = []
    for name in ["bybit", "binance", "okx"]:
        try:
            ex = get_exchange(name)
            ex.load_markets()
            exchanges.append(ex)
        except Exception as e:
            print(f"Биржа {name} недоступна: {e}")

    if not exchanges:
        return []

    # Берём юниверс с Bybit как основной (пользователь торгует там)
    primary = next((e for e in exchanges if e.id == "bybit"), exchanges[0])
    universe = fetch_universe(primary, P["min_turnover_usd"], 100)
    if FIXED_COINS:
        universe = [s for s in universe if s.split("/")[0] in FIXED_COINS]

    def work(sym):
        # Пробуем получить сигнал с каждой биржи, где есть символ
        for ex in exchanges:
            if sym not in ex.symbols:
                continue
            try:
                sig = analyze_symbol(sym, ex, P)
                if sig:
                    return sig
            except Exception:
                continue
        return None

    signals = []
    with ThreadPoolExecutor(max_workers=P["workers"]) as pool:
        results = list(pool.map(work, universe))
    for sig in results:
        if sig:
            signals.append(sig)

    # Сортируем по количеству совпавших факторов
    signals.sort(key=lambda s: s["score"], reverse=True)
    return signals[:P["max_alerts"]]

# ---------- отправка ----------
def build_message(sig):
    arrow = "🟢 LONG" if sig["side"] == "LONG" else "🔴 SHORT"
    return "\n".join([
        f"Монета: {sig['symbol']}USDT | {arrow}",
        f"TP: {sig['tp']:.6g}",
        f"SL: {sig['sl']:.6g}",
    ])

def send_telegram(token, chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"User-Agent": "signal-bot"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read().decode())
        if not resp.get("ok"):
            raise RuntimeError(f"telegram: {resp}")

# ---------- состояние (кулдаун) ----------
def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"sent": {}}

def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=1)

# ---------- main ----------
def main(argv):
    P = load_params()
    dry = "--dry" in argv
    test = "--test" in argv

    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if test:
        msg = "✅ Бот запущен. Многофакторный анализ активен."
        if dry or not token or not chat_id:
            print(msg)
        else:
            send_telegram(token, chat_id, msg)
            print("Тестовое сообщение отправлено.")
        return 0

    signals = scan_all(P)
    if not signals:
        print("Сигналов не найдено.")
        return 0

    state = load_state(STATE_FILE)
    now = time.time()
    sent = 0
    for sig in signals:
        key = f"{sig['symbol']}|{sig['side']}"
        if now - state["sent"].get(key, 0) < P["cooldown_min"] * 60:
            continue
        msg = build_message(sig)
        if dry or not token or not chat_id:
            print(msg + "\n---")
        else:
            send_telegram(token, chat_id, msg)
        state["sent"][key] = now
        sent += 1
        print(f"Отправлен сигнал: {sig['symbol']} {sig['side']} (score={sig['score']})")

    if not dry and sent > 0:
        save_state(STATE_FILE, state)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

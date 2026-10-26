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

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
COINS_ENV = os.environ.get("COINS", "ALL").upper()
FIXED_COINS = None if COINS_ENV in ("", "ALL") else [c.strip() for c in COINS_ENV.split(",")]

def load_params():
    return {
        "timeframe": "1h",       # Классический SMC таймфрейм
        "limit": 300,            # Глубина истории
        "swing_length": 5,       # Чувствительность структуры
        "cooldown_min": 240,     # Не спамить 4 часа
        "max_alerts": 3,
        "min_turnover_usd": 30_000_000, 
        "workers": 6,
        "min_rr": 2.0,           # Минимальный риск/прибыль 1:2
    }

# ---------- Биржи ----------
def get_exchange(name):
    return getattr(ccxt, name)({"enableRateLimit": True, "options": {"defaultType": "swap"}})

def fetch_universe(exchange, min_turnover, max_coins=80):
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
        return df
    except Exception:
        return None

# ---------- SMC Логика ----------
def find_swings(df, length=5):
    swings = []
    for i in range(length, len(df) - length):
        if df['high'].iloc[i] == df['high'].iloc[i-length:i+length+1].max():
            swings.append({'type': 'high', 'price': df['high'].iloc[i], 'index': i})
        elif df['low'].iloc[i] == df['low'].iloc[i-length:i+length+1].min():
            swings.append({'type': 'low', 'price': df['low'].iloc[i], 'index': i})
    return swings

def find_fvg(df, index, bullish=True):
    """Ищет Fair Value Gap (Имбаланс) рядом с индексом"""
    # Бычий FVG: low[i+1] > high[i-1] (разрыв между свечами)
    # Медвежий FVG: high[i+1] < low[i-1]
    if bullish:
        if df['low'].iloc[index+1] > df['high'].iloc[index-1]:
            return {"top": df['low'].iloc[index+1], "bottom": df['high'].iloc[index-1]}
    else:
        if df['high'].iloc[index+1] < df['low'].iloc[index-1]:
            return {"top": df['low'].iloc[index-1], "bottom": df['high'].iloc[index+1]}
    return None

def analyze_smc(symbol, exchange, P):
    df = fetch_ohlcv(exchange, symbol, P["timeframe"], P["limit"])
    if df is None or len(df) < 100:
        return None

    swings = find_swings(df, P["swing_length"])
    if len(swings) < 4:
        return None

    current_price = df['close'].iloc[-1]
    
    highs = [s for s in swings if s['type'] == 'high']
    lows = [s for s in swings if s['type'] == 'low']
    
    if len(highs) < 2 or len(lows) < 2:
        return None

    last_swing_high = highs[-1]
    last_swing_low = lows[-1]
    prev_swing_high = highs[-2]
    prev_swing_low = lows[-2]

    recent = df.iloc[-3:]

    # --- СЦЕНАРИЙ 1: LONG ---
    # Условие: Последний свинг был High (цена сломала структуру вверх)
    if last_swing_high['index'] > last_swing_low['index']:
        # Ищем бычий FVG или OB (последняя медвежья свеча перед импульсом)
        for i in range(len(df) - 2, max(0, len(df) - 30), -1):
            if df['close'].iloc[i] < df['open'].iloc[i]:
                if df['high'].iloc[i+1] > df['high'].iloc[i]:
                    # Нашли OB
                    ob_high = df['high'].iloc[i]
                    ob_low = df['low'].iloc[i]
                    
                    # Проверяем, есть ли FVG в этом же импульсе
                    fvg = find_fvg(df, i, bullish=True)
                    zone_high = fvg["top"] if fvg else ob_high
                    zone_low = fvg["bottom"] if fvg else ob_low
                    
                    # Если цена откатилась в зону
                    if recent['low'].min() <= zone_high and current_price > zone_low:
                        sl = zone_low * 0.995 # Стоп за зону
                        
                        # Тейк: Предыдущий максимум (ликвидность)
                        tp = prev_swing_high['price']
                        if tp <= current_price: 
                            tp = df['high'].iloc[-50:].max()
                        
                        risk = current_price - sl
                        reward = tp - current_price
                        
                        if risk > 0 and (reward / risk) >= P["min_rr"]:
                            fvg_text = f" + FVG ({zone_low:.4g} - {zone_high:.4g})" if fvg else ""
                            return {
                                "symbol": symbol.split("/")[0],
                                "side": "LONG",
                                "entry": current_price,
                                "tp": tp,
                                "sl": sl,
                                "analysis": (
                                    f"🔹 **Структура:** BOS Up (слом максимума) на {last_swing_high['price']:.4g}\n"
                                    f"🔹 **Зона входа:** Бычий OB {ob_low:.4g} - {ob_high:.4g}{fvg_text}\n"
                                    f"🔹 **Цель:** Ликвидность на максимуме {tp:.4g}\n"
                                    f"🔹 **Потенциал:** {((tp/current_price)-1)*100:.1f}% (без плеча)\n"
                                    f"🔹 **Риск/Прибыль:** 1:{reward/risk:.1f}"
                                )
                            }
    
    # --- СЦЕНАРИЙ 2: SHORT ---
    # Условие: Последний свинг был Low (цена сломала структуру вниз)
    else:
        for i in range(len(df) - 2, max(0, len(df) - 30), -1):
            if df['close'].iloc[i] > df['open'].iloc[i]:
                if df['low'].iloc[i+1] < df['low'].iloc[i]:
                    ob_high = df['high'].iloc[i]
                    ob_low = df['low'].iloc[i]
                    
                    fvg = find_fvg(df, i, bullish=False)
                    zone_high = fvg["top"] if fvg else ob_high
                    zone_low = fvg["bottom"] if fvg else ob_low
                    
                    if recent['high'].max() >= zone_low and current_price < zone_high:
                        sl = zone_high * 1.005 # Стоп за зону
                        
                        # Тейк: Предыдущий минимум (ликвидность)
                        tp = prev_swing_low['price']
                        if tp >= current_price: 
                            tp = df['low'].iloc[-50:].min()
                            
                        risk = sl - current_price
                        reward = current_price - tp
                        
                        if risk > 0 and (reward / risk) >= P["min_rr"]:
                            fvg_text = f" + FVG ({zone_low:.4g} - {zone_high:.4g})" if fvg else ""
                            return {
                                "symbol": symbol.split("/")[0],
                                "side": "SHORT",
                                "entry": current_price,
                                "tp": tp,
                                "sl": sl,
                                "analysis": (
                                    f"🔹 **Структура:** BOS Down (слом минимума) на {last_swing_low['price']:.4g}\n"
                                    f"🔹 **Зона входа:** Медвежий OB {ob_low:.4g} - {ob_high:.4g}{fvg_text}\n"
                                    f"🔹 **Цель:** Ликвидность на минимуме {tp:.4g}\n"
                                    f"🔹 **Потенциал:** {((current_price/tp)-1)*100:.1f}% (без плеча)\n"
                                    f"🔹 **Риск/Прибыль:** 1:{reward/risk:.1f}"
                                )
                            }
    return None

# ---------- Сканирование ----------
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
    universe = fetch_universe(primary, P["min_turnover_usd"])
    if FIXED_COINS: universe = [s for s in universe if s.split("/")[0] in FIXED_COINS]

    def work(sym):
        for ex in exchanges:
            if sym in ex.symbols:
                try:
                    sig = analyze_smc(sym, ex, P)
                    if sig: return sig
                except Exception: continue
        return None

    with ThreadPoolExecutor(max_workers=P["workers"]) as pool:
        results = list(pool.map(work, universe))
    signals = [s for s in results if s]
    return signals[:P["max_alerts"]]

# ---------- Отправка ----------
def build_message(sig):
    arrow = "🟢 LONG" if sig["side"] == "LONG" else "🔴 SHORT"
    return (
        f"💎 **SMC СИГНАЛ (1H)**\n\n"
        f"Монета: **{sig['symbol']}USDT** | {arrow}\n"
        f"Вход: `{sig['entry']:.6g}`\n"
        f"TP: `{sig['tp']:.6g}`\n"
        f"SL: `{sig['sl']:.6g}`\n\n"
        f"📊 **РАЗБОР СИТУАЦИИ:**\n"
        f"{sig['analysis']}"
    )

def send_telegram(token, chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=15) as r:
        if not json.loads(r.read().decode()).get("ok"): raise RuntimeError("telegram error")

def load_state(path):
    try:
        with open(path) as f: return json.load(f)
    except: return {"sent": {}}

def save_state(path, state):
    with open(path, "w") as f: json.dump(state, f, indent=1)

# ---------- Main ----------
def main(argv):
    P = load_params()
    dry = "--dry" in argv
    test = "--test" in argv
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if test:
        msg = "✅ SMC-Бот (1H) запущен. Ищу имбалансы и сломы структуры."
        if not dry and token and chat_id: send_telegram(token, chat_id, msg)
        print(msg)
        return 0

    signals = scan_all(P)
    if not signals:
        print("SMC-сигналов не найдено.")
        return 0

    state = load_state(STATE_FILE)
    now = time.time()
    for sig in signals:
        key = f"{sig['symbol']}|{sig['side']}"
        if now - state["sent"].get(key, 0) < P["cooldown_min"] * 60: continue
        msg = build_message(sig)
        if not dry and token and chat_id:
            send_telegram(token, chat_id, msg)
            print(f"Отправлен SMC-сигнал: {sig['symbol']} {sig['side']}")
        else:
            print(msg + "\n---")
        state["sent"][key] = now
    
    if not dry: save_state(STATE_FILE, state)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

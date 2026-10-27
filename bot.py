import os, json, time, urllib.request, urllib.parse
from datetime import datetime

SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','ADAUSDT',
           'AVAXUSDT','LINKUSDT','DOTUSDT','MATICUSDT','LTCUSDT','ATOMUSDT',
           'NEARUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT',
           'TIAUSDT','DOGEUSDT']
TOP_N = 6
MIN_RR = 2.0 # Жесткий фильтр: ТП должен быть минимум в 2 раза больше СЛ

def fetch(symbol):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            return [[float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in data]
    except Exception:
        return None

def calc_indicators(k):
    closes = [x[3] for x in k]
    highs = [x[1] for x in k]
    lows = [x[2] for x in k]
    vols = [x[4] for x in k]
    
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(k))]
    atr = sum(trs[-14:])/14 if len(trs) >= 14 else 0
    ema20 = sum(closes[-20:])/20
    ema50 = sum(closes[-50:])/50
    
    deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
    gains = sum([d for d in deltas[-14:] if d > 0])/14
    losses = sum([-d for d in deltas[-14:] if d < 0])/14
    rsi = 100 - (100 / (1 + (gains/losses if losses > 0 else 100)))
    
    sma20 = sum(closes[-20:])/20
    std20 = (sum([(c - sma20)**2 for c in closes[-20:]]) / 20) ** 0.5
    bb_low = sma20 - (2 * std20)
    bb_up = sma20 + (2 * std20)
    avg_vol = sum(vols[-20:])/20

    return {'close': closes[-1], 'ema20': ema20, 'ema50': ema50, 'rsi': rsi, 
            'atr': atr, 'bb_low': bb_low, 'bb_up': bb_up, 'vol': vols[-1], 'avg_vol': avg_vol,
            'highs': highs, 'lows': lows}

def analyze(symbol, k):
    if len(k) < 50: return None
    ind = calc_indicators(k)
    c, e20, e50, rsi, atr = ind['close'], ind['ema20'], ind['ema50'], ind['rsi'], ind['atr']
    if atr == 0: return None

    score = 0
    side = "LONG" if e20 > e50 else "SHORT"
    strat = "Trend"

    # Оценка
    if side == "LONG":
        if 40 < rsi < 55: score += 4; strat = "Momentum"
        elif rsi < 35: score += 3; strat = "MeanRev"
        if c <= ind['bb_low']: score += 2; strat = "Bollinger"
    else:
        if 45 < rsi < 60: score += 4; strat = "Momentum"
        elif rsi > 65: score += 3; strat = "MeanRev"
        if c >= ind['bb_up']: score += 2; strat = "Bollinger"

    if ind['vol'] > 1.5 * ind['avg_vol']: 
        score += 2
        strat += "+Vol"
    score += 1

    # --- УМНЫЙ РАСЧЕТ SL и TP ---
    if side == 'LONG':
        # SL ставим под недавний минимум (последние 5 свечей) минус буфер ATR
        local_low = min(ind['lows'][-5:])
        sl = min(local_low - 0.5*atr, c - 1.0*atr)
        
        # TP ставим на недавний максимум (последние 30 свечей)
        recent_highs = [h for h in ind['highs'][-30:] if h > c + (c - sl) * 1.5]
        if recent_highs:
            tp = min(recent_highs) # Ближайшее сопротивление
        else:
            tp = c + (c - sl) * 2.5 # Если максимумов нет, ставим по Фибо (1:2.5)
            
    else: # SHORT
        local_high = max(ind['highs'][-5:])
        sl = max(local_high + 0.5*atr, c + 1.0*atr)
        
        recent_lows = [l for l in ind['lows'][-30:] if l < c - (sl - c) * 1.5]
        if recent_lows:
            tp = max(recent_lows)
        else:
            tp = c - (sl - c) * 2.5

    # Проверка R:R
    rr = abs(tp - c) / abs(c - sl) if abs(c - sl) > 0 else 0
    if rr < MIN_RR:
        return None # Если ТП короткий — выкидываем нахуй этот сигнал

    return {'sym': symbol, 'side': side, 'strat': strat, 'entry': c, 
            'tp': tp, 'sl': sl, 'score': score, 'rr': round(rr, 2)}

def send_tg(text):
    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id: return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except: pass

if __name__ == "__main__":
    all_signals = []
    for sym in SYMBOLS:
        klines = fetch(sym)
        if klines:
            sig = analyze(sym, klines)
            if sig: all_signals.append(sig)
    
    if not all_signals:
        print("Нет сигналов, проходящих по R:R.")
        exit()

    all_signals.sort(key=lambda x: x['score'], reverse=True)
    top = all_signals[:TOP_N]

    msg = "📡 Топ сигналов (R:R >= 2.0):\n\n"
    for s in top:
        emoji = "🟢" if s['side'] == 'LONG' else "🔴"
        pct = abs(s['tp'] - s['entry']) / s['entry'] * 100
        msg += f"{emoji} {s['sym']} | {s['side']} (Балл: {s['score']})\n"
        msg += f"Вход: {s['entry']:.4f}\n"
        msg += f"TP: {s['tp']:.4f} (+{pct:.2f}%)\n"
        msg += f"SL: {s['sl']:.4f}\n"
        msg += f"R:R: {s['rr']} | {s['strat']}\n\n"

    print(msg)
    send_tg(msg)

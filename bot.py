import os, json, time, urllib.request, urllib.parse
from datetime import datetime, timezone

SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','ADAUSDT',
           'AVAXUSDT','LINKUSDT','DOTUSDT','MATICUSDT','LTCUSDT','ATOMUSDT',
           'NEARUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','SUIUSDT',
           'TIAUSDT','DOGEUSDT']
TOP_N = 6
MIN_RR = 1.5

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
    
    # ATR
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(k))]
    atr = sum(trs[-14:])/14 if len(trs) >= 14 else 0
    
    # EMA 20/50
    ema20 = sum(closes[-20:])/20
    ema50 = sum(closes[-50:])/50
    
    # RSI
    deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
    gains = sum([d for d in deltas[-14:] if d > 0])/14
    losses = sum([-d for d in deltas[-14:] if d < 0])/14
    rsi = 100 - (100 / (1 + (gains/losses if losses > 0 else 100)))
    
    # Bollinger
    sma20 = sum(closes[-20:])/20
    std20 = (sum([(c - sma20)**2 for c in closes[-20:]]) / 20) ** 0.5
    bb_low = sma20 - (2 * std20)
    
    # Объем
    avg_vol = sum(vols[-20:])/20

    return {'close': closes[-1], 'ema20': ema20, 'ema50': ema50, 'rsi': rsi, 
            'atr': atr, 'bb_low': bb_low, 'vol': vols[-1], 'avg_vol': avg_vol}

def analyze(symbol, k):
    if len(k) < 50: return None
    ind = calc_indicators(k)
    c, e20, e50, rsi, atr = ind['close'], ind['ema20'], ind['ema50'], ind['rsi'], ind['atr']
    if atr == 0: return None

    # Стратегия 1: Тренд (Momentum)
    if e20 > e50 and 40 < rsi < 55:
        return {'sym': symbol, 'side': 'LONG', 'strat': 'Momentum', 'entry': c, 'tp': c + 2*atr, 'sl': c - 1*atr, 'rr': 2.0}
    if e20 < e50 and 45 < rsi < 60:
        return {'sym': symbol, 'side': 'SHORT', 'strat': 'Momentum', 'entry': c, 'tp': c - 2*atr, 'sl': c + 1*atr, 'rr': 2.0}

    # Стратегия 2: Откат (Mean Reversion)
    if c <= ind['bb_low'] and rsi < 30:
        return {'sym': symbol, 'side': 'LONG', 'strat': 'MeanRev', 'entry': c, 'tp': c + 1.5*atr, 'sl': c - 1*atr, 'rr': 1.5}
    
    # Стратегия 3: Пробой (Breakout)
    if c > max([x[1] for x in k[-21:-1]]) and ind['vol'] > 1.5*ind['avg_vol']:
         return {'sym': symbol, 'side': 'LONG', 'strat': 'Breakout', 'entry': c, 'tp': c + 2*atr, 'sl': c - 1*atr, 'rr': 2.0}
    if c < min([x[2] for x in k[-21:-1]]) and ind['vol'] > 1.5*ind['avg_vol']:
         return {'sym': symbol, 'side': 'SHORT', 'strat': 'Breakout', 'entry': c, 'tp': c - 2*atr, 'sl': c + 1*atr, 'rr': 2.0}

    return None

def send_tg(text):
    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id: 
        print("Нет токена ТГ")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        print("TG Error:", e)

if __name__ == "__main__":
    print("Сканируем...")
    signals = []
    for sym in SYMBOLS:
        klines = fetch(sym)
        if klines:
            sig = analyze(sym, klines)
            if sig: signals.append(sig)
    
    if not signals:
        print("Нет сигналов.")
        exit()

    signals.sort(key=lambda x: x['rr'], reverse=True)
    top = signals[:TOP_N]

    msg = "📡 Сигналы:\n\n"
    for s in top:
        emoji = "🟢" if s['side'] == 'LONG' else "🔴"
        msg += f"{emoji} {s['sym']} | {s['side']}\n"
        msg += f"Вход: {s['entry']:.4f} | TP: {s['tp']:.4f} | SL: {s['sl']:.4f}\n"
        msg += f"Стратегия: {s['strat']}\n\n"

    print(msg)
    send_tg(msg)

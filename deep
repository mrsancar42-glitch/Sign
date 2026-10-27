#!/usr/bin/env python3
"""
Adaptive Ensemble Signal Bot
- Regime-aware scoring (Trend / Range / Volatile)
- 3 independent strategies: Momentum, Mean-Reversion, Breakout
- Strict top-6 selection, no catch-up mode
- Pure standard library (urllib, json) for maximum portability
"""
import os, sys, json, time, urllib.request, urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

# ======================= КОНФИГУРАЦИЯ =======================
SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT',
    'AVAXUSDT', 'LINKUSDT', 'DOTUSDT', 'MATICUSDT', 'LTCUSDT', 'ATOMUSDT',
    'NEARUSDT', 'APTUSDT', 'ARBUSDT', 'OPUSDT', 'INJUSDT', 'SUIUSDT',
    'TIAUSDT', 'DOGEUSDT'
]
TIMEFRAME = '15m'
LIMIT = 200
TOP_N = 6
MIN_SCORE = 3.5          # Жесткий порог качества. Ниже — не берем.
FEE = 0.0005

# ======================= ДАННЫЕ =======================
def fetch_klines(symbol, interval=TIMEFRAME, limit=LIMIT):
    """Публичные свечи с Binance Vision (без ключей)."""
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            raw = json.loads(r.read().decode())
        return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in raw]
    except Exception as e:
        print(f"  [skip] {symbol}: {e}", file=sys.stderr)
        return None

# ======================= ИНДИКАТОРЫ =======================
def ema(values, period):
    if len(values) < period: return [sum(values)/len(values)] * len(values)
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out = [e] * (period - 1)
    for v in values[period-1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def rsi(closes, period=14):
    if len(closes) <= period: return [50.0] * len(closes)
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    rsis = [50.0] * (period + 1)
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period-1) + gains[i]) / period
        avg_l = (avg_l * (period-1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l > 0 else 100.0
        rsis.append(100 - 100/(1+rs))
    return rsis + [50.0] * (len(closes) - len(rsis))

def atr(klines, period=14):
    if len(klines) <= period: return [0.0] * len(klines)
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i][2], klines[i][3], klines[i-1][4]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    atrs = [sum(trs[:period]) / period] * (period + 1)
    for tr in trs[period:]:
        atrs.append((atrs[-1] * (period-1) + tr) / period)
    return [0.0] + atrs

def bollinger(closes, period=20, std_mult=2):
    if len(closes) < period: return [0.0]*len(closes), [0.0]*len(closes)
    mids = [sum(closes[i-period+1:i+1])/period for i in range(period-1, len(closes))]
    stds = [ (sum((x - mids[i])**2 for x in closes[i-period+1:i+1]) / period) ** 0.5 for i in range(len(mids)) ]
    upper = [mids[i] + std_mult * stds[i] for i in range(len(mids))]
    lower = [mids[i] - std_mult * stds[i] for i in range(len(mids))]
    pad = len(closes) - len(mids)
    return [0.0]*pad + upper, [0.0]*pad + lower

def donchian(klines, period=20):
    highs, lows = [], []
    for i in range(len(klines)):
        window = klines[max(0, i-period+1):i+1]
        highs.append(max(k[2] for k in window))
        lows.append(min(k[3] for k in window))
    return highs, lows

# ======================= РЕЖИМ РЫНКА (REGIME DETECTION) =======================
def detect_regime(klines):
    """
    Определяет режим: 'trend', 'range', 'volatile'.
    Использует ADX-подобную логику (сила тренда) и волатильность ATR.
    """
    closes = [k[4] for k in klines]
    if len(closes) < 50: return 'range'
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    atr_val = atr(klines, 14)[-1]
    avg_price = sum(closes[-20:]) / 20
    atr_pct = (atr_val / avg_price) * 100 if avg_price > 0 else 0

    trend_strength = abs(e20 - e50) / avg_price * 100 if avg_price > 0 else 0
    if atr_pct > 2.5: return 'volatile'
    if trend_strength > 0.8: return 'trend'
    return 'range'

# ======================= СТРАТЕГИИ =======================
def _make_signal(side, entry, atr_val, strategy):
    if atr_val <= 0: return None
    if side == 'LONG':
        sl = entry - 1.5 * atr_val
        tp = entry + 2.7 * atr_val
        rr = (tp - entry) / (entry - sl)
    else:
        sl = entry + 1.5 * atr_val
        tp = entry - 2.7 * atr_val
        rr = (entry - tp) / (sl - entry)
    if rr < 1.5: return None
    return {'side': side, 'entry': round(entry, 6), 'tp': round(tp, 6),
            'sl': round(sl, 6), 'rr': round(rr, 2), 'strategy': strategy, 'atr': atr_val}

def strategy_momentum(klines):
    if len(klines) < 60: return None
    closes = [k[4] for k in klines]
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    rsi_val = rsi(closes)[-1]
    atr_val = atr(klines)[-1]
    c = closes[-1]
    if e20 > e50 and 40 < rsi_val < 55 and c > e20:
        return _make_signal('LONG', c, atr_val, 'Momentum')
    if e20 < e50 and 45 < rsi_val < 60 and c < e20:
        return _make_signal('SHORT', c, atr_val, 'Momentum')
    return None

def strategy_mean_reversion(klines):
    if len(klines) < 50: return None
    closes = [k[4] for k in klines]
    upper, lower = bollinger(closes)[-1], bollinger(closes)[-1]
    rsi_val = rsi(closes)[-1]
    atr_val = atr(klines)[-1]
    c = closes[-1]
    if c <= lower * 1.002 and rsi_val < 30 and c > klines[-1][1]:
        return _make_signal('LONG', c, atr_val, 'MeanRev')
    if c >= upper * 0.998 and rsi_val > 70 and c < klines[-1][1]:
        return _make_signal('SHORT', c, atr_val, 'MeanRev')
    return None

def strategy_breakout(klines):
    if len(klines) < 30: return None
    highs, lows = donchian(klines, 20)
    closes = [k[4] for k in klines]
    vols = [k[5] for k in klines]
    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else 1
    atr_val = atr(klines)[-1]
    c = closes[-1]
    if c > highs[-2] and vols[-1] > 1.5 * avg_vol:
        return _make_signal('LONG', c, atr_val, 'Breakout')
    if c < lows[-2] and vols[-1] > 1.5 * avg_vol:
        return _make_signal('SHORT', c, atr_val, 'Breakout')
    return None

# ======================= СКОРИНГ КАЧЕСТВА =======================
def quality_score(klines, signal, regime):
    """Оценивает сигнал от 0 до 5+ с учетом рыночного режима."""
    if signal is None: return 0.0
    closes = [k[4] for k in klines]
    rsi_val = rsi(closes)[-1]
    atr_val = atr(klines)[-1]
    avg_atr = sum(atr(klines)[-50:]) / 50 if len(klines) >= 50 else atr_val
    vols = [k[5] for k in klines]
    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else 1
    score = 0.0

    # 1. Соответствие сигнала текущему режиму (главный бонус)
    if regime == 'trend' and signal['strategy'] == 'Momentum': score += 2.0
    if regime == 'range' and signal['strategy'] == 'MeanRev': score += 2.0
    if regime == 'volatile' and signal['strategy'] == 'Breakout': score += 2.0

    # 2. R:R
    if signal['rr'] >= 2.0: score += 1.0
    elif signal['rr'] >= 1.7: score += 0.5

    # 3. Волатильность (ATR выше среднего = хороший момент)
    if atr_val > avg_atr * 1.1: score += 0.8

    # 4. Объем
    if vols[-1] > avg_vol * 1.3: score += 0.7

    # 5. RSI-подтверждение
    if signal['side'] == 'LONG' and rsi_val < 50: score += 0.5
    if signal['side'] == 'SHORT' and rsi_val > 50: score += 0.5

    return round(score, 2)

# ======================= ОСНОВНОЙ ЦИКЛ =======================
def scan_all():
    candidates = []
    for sym in SYMBOLS:
        klines = fetch_klines(sym)
        if not klines or len(klines) < 60:
            continue
        regime = detect_regime(klines)
        for fn in (strategy_momentum, strategy_mean_reversion, strategy_breakout):
            sig = fn(klines)
            if sig:
                sig['symbol'] = sym
                sig['regime'] = regime
                sig['score'] = quality_score(klines, sig, regime)
                candidates.append(sig)
        time.sleep(0.1)
    return candidates

def pick_top(candidates, n=TOP_N, min_score=MIN_SCORE):
    filtered = [c for c in candidates if c['score'] >= min_score]
    filtered.sort(key=lambda x: x['score'], reverse=True)
    # Если после фильтра осталось меньше 6 — добираем лучшими из оставшихся,
    # НО помечаем их как 'weak' в сообщении. Мы не скрываем правду.
    if len(filtered) < n:
        rest = [c for c in candidates if c not in filtered]
        rest.sort(key=lambda x: x['score'], reverse=True)
        filtered += rest[:n - len(filtered)]
        for c in filtered[len(filtered) - len(rest[:n - len(filtered)]):]:
            c['weak'] = True
    return filtered[:n]

def format_signal(sig, idx):
    emoji = '🟢' if sig['side'] == 'LONG' else '🔴'
    weak_tag = " ⚠️ (слабый)" if sig.get('weak') else ""
    return (
        f"{idx}. {emoji} {sig['side']} | {sig['symbol']}\n"
        f"   Стратегия: {sig['strategy']} | Режим: {sig['regime']}\n"
        f"   Вход: {sig['entry']} | TP: {sig['tp']} | SL: {sig['sl']}\n"
        f"   R:R: {sig['rr']} | Score: {sig['score']}{weak_tag}"
    )

# ======================= ОТПРАВКА В TELEGRAM (ОПЦИОНАЛЬНО) =======================
def send_telegram(text):
    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print("[TG] Переменные TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не заданы. Вывод в консоль.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            resp = json.loads(r.read().decode())
            if not resp.get('ok'):
                print(f"[TG] Ошибка: {resp}", file=sys.stderr)
    except Exception as e:
        print(f"[TG] Ошибка отправки: {e}", file=sys.stderr)

# ======================= ТОЧКА ВХОДА =======================
if __name__ == '__main__':
    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"=== Adaptive Ensemble Scan | {now_utc} ===")

    candidates = scan_all()
    print(f"Всего кандидатов: {len(candidates)}")

    top = pick_top(candidates)
    if not top:
        msg = "📡 Adaptive Ensemble: сегодня нет сигналов, проходящих фильтр качества."
    else:
        lines = [f"📡 Adaptive Ensemble | {now_utc}", f"Топ-{len(top)} сигналов:", ""]
        for i, sig in enumerate(top, 1):
            lines.append(format_signal(sig, i))
        msg = "\n".join(lines)

    send_telegram(msg)
    print("\nГотово.")

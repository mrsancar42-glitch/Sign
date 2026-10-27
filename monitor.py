"""
Ensemble Signal Generator — 6 сигналов/день минимум
Стратегии: S1 trend-pullback, S2 mean-reversion, S3 breakout
Ранжирование по quality score. Бэктест каждой стратегии отдельно.
pip install ccxt pandas numpy
"""

import ccxt, pandas as pd, numpy as np
from datetime import datetime
import time

EX = ccxt.binance({'enableRateLimit': True})
TIMEFRAME = '15m'       # 15m = 96 баров/день -> много кандидатов
LIMIT = 300
TOP_N = 6               # сколько сигналов отдать
MIN_RR = 1.5
FEE = 0.0005

# 20 ликвидных пар — больше монет = больше кандидатов
SYMBOLS = ['BTC/USDT','ETH/USDT','SOL/USDT','BNB/USDT','XRP/USDT','ADA/USDT',
           'DOGE/USDT','AVAX/USDT','LINK/USDT','DOT/USDT','MATIC/USDT','LTC/USDT',
           'ATOM/USDT','NEAR/USDT','APT/USDT','ARB/USDT','OP/USDT','INJ/USDT',
           'SUI/USDT','TIA/USDT']

# ---------- 1. ДАННЫЕ ----------
def fetch(symbol):
    ohlcv = EX.fetch_ohlcv(symbol, TIMEFRAME, limit=LIMIT)
    df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','vol'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    return df

def indicators(df):
    c = df['close']
    df['ema20'] = c.ewm(span=20).mean()
    df['ema50'] = c.ewm(span=50).mean()
    # Bollinger
    df['bb_mid'] = c.rolling(20).mean()
    df['bb_std'] = c.rolling(20).std()
    df['bb_up']  = df['bb_mid'] + 2*df['bb_std']
    df['bb_dn']  = df['bb_mid'] - 2*df['bb_std']
    # RSI
    d = c.diff()
    g = d.clip(lower=0).rolling(14).mean()
    l = (-d.clip(upper=0)).rolling(14).mean()
    df['rsi'] = 100 - 100/(1 + g/l.replace(0,np.nan))
    # ATR
    tr = pd.concat([df['high']-df['low'],
                    (df['high']-c.shift()).abs(),
                    (df['low']-c.shift()).abs()], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    df['atr_ma'] = df['atr'].rolling(50).mean()
    # Donchian
    df['dc_hi'] = df['high'].rolling(20).max()
    df['dc_lo'] = df['low'].rolling(20).min()
    # объём
    df['vol_ma'] = df['vol'].rolling(20).mean()
    return df

# ---------- 2. СТРАТЕГИИ (возвращают dict или None) ----------
def _mk(side, entry, atr, strat, extra=0):
    if side == 'LONG':
        sl = entry - 1.2*atr; tp = entry + 2.2*atr
        rr = (tp-entry)/(entry-sl)
    else:
        sl = entry + 1.2*atr; tp = entry - 2.2*atr
        rr = (entry-tp)/(sl-entry)
    if rr < MIN_RR: return None
    return {'side':side,'entry':entry,'tp':tp,'sl':sl,'rr':round(rr,2),
            'strat':strat,'atr':atr,'extra':extra}

def s1_trend_pullback(df):
    r = df.iloc[-1]
    if pd.isna(r['ema50']) or pd.isna(r['rsi']): return None
    up = r['ema20'] > r['ema50']; dn = r['ema20'] < r['ema50']
    near_ema = abs(r['close'] - r['ema20'])/r['close'] < 0.008
    if up and r['rsi'] < 42 and near_ema:
        return _mk('LONG', r['close'], r['atr'], 'S1_trend')
    if dn and r['rsi'] > 58 and near_ema:
        return _mk('SHORT', r['close'], r['atr'], 'S1_trend')
    return None

def s2_mean_reversion(df):
    r = df.iloc[-1]
    if pd.isna(r['bb_dn']) or pd.isna(r['rsi']): return None
    prev = df.iloc[-2]
    # нижняя полоса + RSI<30 + отбой (свеча закрылась выше открытия)
    if r['close'] <= r['bb_dn']*1.002 and r['rsi'] < 30 and r['close'] > r['open']:
        return _mk('LONG', r['close'], r['atr'], 'S2_meanrev')
    if r['close'] >= r['bb_up']*0.998 and r['rsi'] > 70 and r['close'] < r['open']:
        return _mk('SHORT', r['close'], r['atr'], 'S2_meanrev')
    return None

def s3_breakout(df):
    r = df.iloc[-1]; prev = df.iloc[-2]
    if pd.isna(r['dc_hi']) or pd.isna(r['vol_ma']) or r['vol_ma']==0: return None
    vol_surge = r['vol'] > 1.5*r['vol_ma']
    if r['close'] > prev['dc_hi'] and vol_surge:
        return _mk('LONG', r['close'], r['atr'], 'S3_break')
    if r['close'] < prev['dc_lo'] and vol_surge:
        return _mk('SHORT', r['close'], r['atr'], 'S3_break')
    return None

# ---------- 3. СКОРИНГ ----------
def quality_score(df, sig):
    r = df.iloc[-1]; s = 0.0
    atr_ok = r['atr'] > r['atr_ma'] if not pd.isna(r['atr_ma']) else False
    vol_ok = r['vol'] > r['vol_ma']*1.2 if r['vol_ma']>0 else False
    rsi = r['rsi']

    if sig['side']=='LONG':
        if r['ema20'] > r['ema50']: s += 1.5            # тренд совпадает
        if 30 < rsi < 45: s += 1.0
        if vol_ok: s += 1.2
        if sig['rr'] >= 2: s += 0.8
        if atr_ok: s += 0.5
        if r['close'] > r['open']: s += 0.5
    else:
        if r['ema20'] < r['ema50']: s += 1.5
        if 55 < rsi < 70: s += 1.0
        if vol_ok: s += 1.2
        if sig['rr'] >= 2: s += 0.8
        if atr_ok: s += 0.5
        if r['close'] < r['open']: s += 0.5
    return round(s, 2)

# ---------- 4. БЭКТЕСТ ОДНОЙ СТРАТЕГИИ ----------
def backtest(df, fn, max_bars=40):
    trades = []
    for i in range(60, len(df)-1):
        w = df.iloc[:i+1]
        sig = fn(w)
        if not sig: continue
        entry, tp, sl, side = sig['entry'], sig['tp'], sig['sl'], sig['side']
        outcome = None
        for j in range(i+1, min(i+max_bars, len(df))):
            hi, lo = df['high'].iloc[j], df['low'].iloc[j]
            if side=='LONG':
                if lo <= sl: outcome=-1; break
                if hi >= tp: outcome=1; break
            else:
                if hi >= sl: outcome=-1; break
                if lo <= tp: outcome=1; break
        if outcome is None: continue
        pnl = (abs(tp-entry)/entry - 2*FEE) if outcome==1 else -(abs(sl-entry)/entry + 2*FEE)
        trades.append({'res':outcome,'pnl':pnl})
    if not trades: return None
    t = pd.DataFrame(trades)
    wr = (t['res']==1).mean()
    exp = t['pnl'].mean()
    eq = (1+t['pnl']).cumprod()
    dd = (eq/eq.cummax()-1).min()
    return {'n':len(t),'wr':round(wr,3),'exp_%':round(exp*100,4),
            'ret_%':round((eq.iloc[-1]-1)*100,1),'dd_%':round(dd*100,1)}

# ---------- 5. ГЛАВНЫЙ ПАЙПЛАЙН ----------
def collect_all_signals():
    """Обходит все символы, все 3 стратегии, возвращает список кандидатов с score."""
    candidates = []
    for sym in SYMBOLS:
        try:
            df = indicators(fetch(sym))
            for fn in (s1_trend_pullback, s2_mean_reversion, s3_breakout):
                sig = fn(df)
                if sig:
                    sig['symbol'] = sym
                    sig['score'] = quality_score(df, sig)
                    sig['ts'] = df['ts'].iloc[-1]
                    candidates.append(sig)
            time.sleep(0.15)
        except Exception as e:
            print(f"skip {sym}: {e}")
    return candidates

def pick_top(candidates, n=TOP_N, min_score=3.5):
    ok = [c for c in candidates if c['score'] >= min_score]
    ok.sort(key=lambda x: x['score'], reverse=True)
    if len(ok) < n:   # досыпаем более слабые, если не хватает
        extra = [c for c in candidates if c not in ok]
        extra.sort(key=lambda x: x['score'], reverse=True)
        ok += extra[:n-len(ok)]
    return ok[:n]

# ---------- MAIN ----------
if __name__ == '__main__':
    print(f"=== ENSEMBLE SIGNALS {datetime.utcnow():%Y-%m-%d %H:%M UTC} ===\n")

    # 1) Сигналы на сейчас
    cands = collect_all_signals()
    print(f"Кандидатов всего: {len(cands)}")
    top = pick_top(cands)

    print("\n--- ТОП-6 СИГНАЛОВ ---")
    for i, s in enumerate(top, 1):
        print(f"{i}. {s['symbol']:10s} {s['side']:5s} [{s['strat']:10s}] "
              f"entry={s['entry']:.4f} TP={s['tp']:.4f} SL={s['sl']:.4f} "
              f"RR={s['rr']} score={s['score']}")

    # 2) Бэктест — честная статистика на истории
    print("\n--- БЭКТЕСТ (реальная история, 300 баров 15m) ---")
    stats = {'S1':[], 'S2':[], 'S3':[]}
    for sym in SYMBOLS[:10]:   # 10 монет для скорости
        try:
            df = indicators(fetch(sym))
            for name, fn in [('S1',s1_trend_pullback),('S2',s2_mean_reversion),('S3',s3_breakout)]:
                r = backtest(df, fn)
                if r: stats[name].append(r)
            time.sleep(0.15)
        except Exception as e:
            print(f"bt skip {sym}: {e}")

    print(f"{'strat':6s} | {'trades':6s} | {'win_rate':8s} | {'exp_%':8s} | {'ret_%':8s} | {'dd_%':8s}")
    for name, lst in stats.items():
        if not lst: continue
        n = sum(x['n'] for x in lst)
        wr = np.mean([x['wr'] for x in lst])
        exp = np.mean([x['exp_%'] for x in lst])
        ret = np.mean([x['ret_%'] for x in lst])
        dd = min(x['dd_%'] for x in lst)
        print(f"{name:6s} | {n:6d} | {wr*100:7.1f}% | {exp:+7.3f}% | {ret:+7.1f}% | {dd:7.1f}%")

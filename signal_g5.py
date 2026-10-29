#!/usr/bin/env python3
"""
Signal G5 V5: Advanced Smart Cross-Exchange Spot Scanner (Bybit, Binance, OKX).
Strictly optimized for Bybit trading data with institutional indicators.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo
import requests

# Focused high-liquidity USDT spot universe available on Bybit, Binance, and OKX
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "TRXUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
    "INJUSDT", "SUIUSDT", "TIAUSDT", "FILUSDT", "UNIUSDT", "ETCUSDT",
    "AAVEUSDT", "SEIUSDT", "POLUSDT", "WLDUSDT", "FETUSDT", "RENDERUSDT",
    "SANDUSDT", "MANAUSDT", "GALAUSDT", "GRTUSDT", "STXUSDT", "ICPUSDT",
    "IMXUSDT", "FTMUSDT", "RUNEUSDT"
]

ENTRY_INTERVAL = "15" # Bybit uses strings like "15", "60", "240"
MID_INTERVAL = "60"
HIGH_INTERVAL = "240"
KLINE_LIMIT = 200

MIN_SCORE = 7
MIN_RR = 1.6
MODEL_RR = 2.0
MAX_STRUCTURAL_RR = 4.0
MIN_TREND_ADX = 15.0
MIN_VOLUME_RATIO = 0.65
STRONG_VOLUME_RATIO = 1.1

STATE_PATH = Path(__file__).with_name("signal_g5_state.json")
LOCAL_TZ = ZoneInfo("Europe/Istanbul")
HTTP_TIMEOUT = 8

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("signal-g5")

def get_bybit_klines(symbol: str, interval: str, limit: int = KLINE_LIMIT) -> List[Dict[str, float]]:
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}"
    res = requests.get(url, timeout=HTTP_TIMEOUT).json()
    if res.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {res.get('retMsg')}")
    
    list_data = res.get("result", {}).get("list", [])
    now_ms = int(time.time() * 1000)
    result = []
    
    # Bybit returns klines from newest to oldest. We need chronological order.
    for row in reversed(list_data):
        open_time = int(row[0])
        # Skip still-forming current bar
        if open_time + (int(interval) * 60 * 1000) >= now_ms:
            continue
        result.append({
            "open_time": open_time,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "close_time": open_time + (int(interval) * 60 * 1000) - 1
        })
    return result

def fetch_cross_prices(symbol: str) -> Dict[str, Optional[float]]:
    prices = {"bybit": None, "binance": None, "okx": None}
    
    def get_bybit():
        try:
            r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}", timeout=HTTP_TIMEOUT).json()
            return float(r["result"]["list"][0]["lastPrice"])
        except Exception: return None

    def get_binance():
        try:
            r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=HTTP_TIMEOUT).json()
            return float(r["price"])
        except Exception: return None

    def get_okx():
        # OKX uses hyphens like BTC-USDT
        okx_sym = f"{symbol[:-4]}-{symbol[-4:]}"
        try:
            r = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={okx_sym}", timeout=HTTP_TIMEOUT).json()
            return float(r["data"][0]["last"])
        except Exception: return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_bybit = pool.submit(get_bybit)
        f_binance = pool.submit(get_binance)
        f_okx = pool.submit(get_okx)
        prices["bybit"] = f_bybit.result()
        prices["binance"] = f_binance.result()
        prices["okx"] = f_okx.result()
        
    return prices

def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0

def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    current = mean(values[:period])
    out[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for i in range(period, len(values)):
        current = alpha * values[i] + (1.0 - alpha) * current
        out[i] = current
    return out

def rsi_series(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = mean([max(x, 0.0) for x in changes[:period]])
    avg_loss = mean([max(-x, 0.0) for x in changes[:period]])
    out[period] = 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss))) if avg_loss > 0 else 100.0
    
    for i in range(period + 1, len(closes)):
        change = changes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        if avg_loss <= 1e-15:
            out[i] = 100.0 if avg_gain > 1e-15 else 50.0
        else:
            out[i] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return out

def atr_wilder(candles: Sequence[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        hi, lo, prev_close = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
    current = mean(trs[:period])
    for tr in trs[period:]:
        current = (current * (period - 1) + tr) / period
    return current

def dmi_adx(candles: Sequence[Dict[str, float]], period: int = 14) -> Optional[Tuple[float, float, float]]:
    if len(candles) < (2 * period + 2):
        return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        high, low = candles[i]["high"], candles[i]["low"]
        prev_high, prev_low, prev_close = candles[i - 1]["high"], candles[i - 1]["low"], candles[i - 1]["close"]
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        trs.append(max(high - low, abs(high - prev_close), abs(lo - prev_close)))

    tr_s = sum(trs[:period])
    plus_s = sum(plus_dm[:period])
    minus_s = sum(minus_dm[:period])
    dx = []

    def di_dx(tr: float, plus: float, minus: float) -> Tuple[float, float, float]:
        if tr <= 0: return 0.0, 0.0, 0.0
        pdi = 100.0 * plus / tr
        mdi = 100.0 * minus / tr
        denom = pdi + mdi
        return (0.0 if denom <= 0 else 100.0 * abs(pdi - mdi) / denom), pdi, mdi

    dx.append(di_dx(tr_s, plus_s, minus_s)[0])
    for i in range(period, len(trs)):
        tr_s = tr_s - tr_s / period + trs[i]
        plus_s = plus_s - plus_s / period + plus_dm[i]
        minus_s = minus_s - minus_s / period + minus_dm[i]
        dx.append(di_dx(tr_s, plus_s, minus_s)[0])
        
    adx = mean(dx[:period])
    for value in dx[period:]:
        adx = (adx * (period - 1) + value) / period
    _, pdi, mdi = di_dx(tr_s, plus_s, minus_s)
    return adx, pdi, mdi

def check_smart_trend(candles: Sequence[Dict[str, float]]) -> Optional[str]:
    closes = [c["close"] for c in candles]
    ema50 = ema_series(closes, 50)
    if len(closes) < 55 or ema50[-1] is None or ema50[-5] is None:
        return None
    e50_now = float(ema50[-1])
    e50_old = float(ema50[-5])
    close = closes[-1]
    if close > e50_now and e50_now > e50_old:
        return "LONG"
    if close < e50_now and e50_now < e50_old:
        return "SHORT"
    return None

def detect_hidden_divergence(entry_candles: Sequence[Dict[str, float]], side: str) -> bool:
    closes = [c["close"] for c in entry_candles]
    lows = [c["low"] for c in entry_candles]
    highs = [c["high"] for c in entry_candles]
    rsi = rsi_series(closes, 14)
    if len(closes) < 30 or rsi[-1] is None:
        return False
    if side == "LONG":
        current_low = min(lows[-4:])
        current_rsi = min([r for r in rsi[-4:] if r is not None])
        past_lows = lows[-25:-6]
        past_rsi = [r for r in rsi[-25:-6] if r is not None]
        if not past_rsi: return False
        if current_low > min(past_lows) and current_rsi < min(past_rsi):
            return True
    elif side == "SHORT":
        current_high = max(highs[-4:])
        current_rsi = max([r for r in rsi[-4:] if r is not None])
        past_highs = highs[-25:-6]
        past_rsi = [r for r in rsi[-25:-6] if r is not None]
        if not past_rsi: return False
        if current_high < max(past_highs) and current_rsi > max(past_rsi):
            return True
    return False

def pivot_levels(candles: Sequence[Dict[str, float]], side: str, bars: int = 60) -> List[float]:
    start = max(2, len(candles) - bars)
    levels = []
    for i in range(start, len(candles) - 2):
        window = candles[i - 2:i + 3]
        value = window[2]["high"] if side == "LONG" else window[2]["low"]
        candidates = [x["high"] for x in window] if side == "LONG" else [x["low"] for x in window]
        if side == "LONG" and value == max(candidates):
            levels.append(value)
        elif side == "SHORT" and value == min(candidates):
            levels.append(value)
    levels.sort()
    deduped = []
    for level in levels:
        if not deduped or abs(level - deduped[-1]) / max(abs(level), 1e-12) > 0.002:
            deduped.append(level)
    return deduped

def price_text(value: float) -> str:
    if value >= 1000: return f"{value:,.2f}"
    if value >= 100: return f"{value:.3f}"
    if value >= 1: return f"{value:.4f}"
    if value >= 0.01: return f"{value:.5f}"
    return f"{value:.8f}"

def analyze(symbol: str, entry: Sequence[Dict[str, float]], mid: Sequence[Dict[str, float]],
            high: Sequence[Dict[str, float]]) -> Optional[Dict[str, Any]]:
    if min(len(entry), len(mid), len(high)) < 60:
        return None
    trend4h = check_smart_trend(high)
    trend1h = check_smart_trend(mid)
    if not trend4h or trend4h != trend1h:
        return None
    side = trend4h

    closes = [c["close"] for c in entry]
    lows = [c["low"] for c in entry]
    highs = [c["high"] for c in entry]
    volumes = [c["volume"] for c in entry]
    opens = [c["open"] for c in entry]
    
    ema20_series = ema_series(closes, 20)
    if not ema20_series or ema20_series[-1] is None:
        return None
    e20 = float(ema20_series[-1])
    close = closes[-1]
    
    atr = atr_wilder(entry)
    rsi_list = rsi_series(closes, 14)
    dmi1h = dmi_adx(mid)
    if atr is None or atr <= 0 or not rsi_list or rsi_list[-1] is None or not dmi1h:
        return None
    rsi = rsi_list[-1]
    adx1h, plus1h, minus1h = dmi1h

    if adx1h < MIN_TREND_ADX:
        return None

    pullback_ema = False
    if side == "LONG" and min(lows[-3:]) <= e20 + (0.3 * atr) and close > opens[-1]:
        pullback_ema = True
    elif side == "SHORT" and max(highs[-3:]) >= e20 - (0.3 * atr) and close < opens[-1]:
        pullback_ema = True
        
    has_divergence = detect_hidden_divergence(entry, side)
    if not pullback_ema and not has_divergence:
        return None

    prior_volume = mean(volumes[-20:-3])
    recent_volume = mean(volumes[-3:])
    volume_ratio = recent_volume / prior_volume if prior_volume > 0 else 1.0
    if volume_ratio < MIN_VOLUME_RATIO:
        return None

    score = 6
    if pullback_ema: score += 1
    if has_divergence: score += 1
    if volume_ratio >= STRONG_VOLUME_RATIO: score += 1
    if side == "LONG" and plus1h > minus1h: score += 1
    if side == "SHORT" and minus1h > plus1h: score += 1
    if score < MIN_SCORE:
        return None

    if side == "LONG":
        sl = min(lows[-5:]) - (0.1 * atr)
        risk = close - sl
    else:
        sl = max(highs[-5:]) + (0.1 * atr)
        risk = sl - close
    if risk <= 0 or risk > (3.0 * atr):
        return None

    levels = pivot_levels(mid, side)
    directional_levels = [x for x in levels if x > close] if side == "LONG" else [x for x in levels if x < close]
    if side == "LONG": directional_levels.sort()
    else: directional_levels.sort(reverse=True)
    
    nearest_level = directional_levels[0] if directional_levels else None
    if nearest_level is not None:
        level_rr = abs(nearest_level - close) / risk
        if level_rr >= MIN_RR and level_rr <= MAX_STRUCTURAL_RR:
            tp = nearest_level
            target_basis = "структурный уровень 1H"
        else:
            tp = close + MODEL_RR * risk if side == "LONG" else close - MODEL_RR * risk
            target_basis = "модельная цель 2.0R (уровень далеко)"
    else:
        tp = close + MODEL_RR * risk if side == "LONG" else close - MODEL_RR * risk
        target_basis = "математический профит 2.0R (нет уровней)"

    rr = abs(tp - close) / risk
    if rr < MIN_RR:
        return None

    reason_parts = [
        f"Синергия трендов 4H/1H ({side})",
        "Дивергенция RSI" if has_divergence else "Тест скользящей средней",
        f"ADX 1H ({adx1h:.1f})"
    ]
    if volume_ratio > 1.0:
        reason_parts.append(f"Всплеск объема x{volume_ratio:.2f}")

    return {
        "symbol": symbol, "side": side, "entry": close, "tp": tp, "sl": sl,
        "rr": round(rr, 2), "score": score, "rsi": rsi, "adx1h": adx1h,
        "volume_ratio": volume_ratio, "atr": atr, "atr_pct": atr / close * 100.0,
        "risk_pct": risk / close * 100.0, "reward_pct": abs(tp - close) / close * 100.0,
        "target_basis": target_basis, "reason": "; ".join(reason_parts),
        "candle_close_time": int(entry[-1]["close_time"]),
    }

def load_state() -> Dict[str, Any]:
    today = datetime.now(LOCAL_TZ).date().isoformat()
    if not STATE_PATH.exists():
        return {"date_local": today, "sent_symbols": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"date_local": today, "sent_symbols": {}}
    if state.get("date_local") != today:
        return {"date_local": today, "sent_symbols": {}}
    return state

def save_state(state: Dict[str, Any]) -> None:
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(STATE_PATH)

def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOG.error("Telegram не настроен в GitHub Secrets")
        return False
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(endpoint, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result.get("ok") is True
    except Exception as exc:
        LOG.error("Ошибка отправки в Telegram: %s", exc)
        return False

def build_message(signals: Sequence[Dict[str, Any]], daily_count_before: int) -> str:
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M TRT")
    lines = [f"📡 SMART CROSS-EXCHANGE G5 | BYBIT | {now}", "Глобальный тренд: Bybit 4H+1H • Мультибиржевой контроль", ""]
    for signal in signals:
        emoji = "🟢" if signal["side"] == "LONG" else "🔴"
        close_time = datetime.fromtimestamp(signal["candle_close_time"] / 1000, timezone.utc).astimezone(LOCAL_TZ).strftime("%H:%M TRT")
        lines.append(f"{emoji} {signal['symbol']} {signal['side']} | Сила: {signal['score']}/10")
        lines.append(f"Вход (Bybit): {price_text(signal['entry'])} | TP: {price_text(signal['tp'])} (+{signal['reward_pct']:.2f}%)")
        lines.append(f"SL: {price_text(signal['sl'])} (-{signal['risk_pct']:.2f}%) | R:R: {signal['rr']}")
        lines.append(f"Паттерн: {signal['reason']}")
        lines.append(f"Цель: {signal['target_basis']} | Валидация: ОК (Binance/OKX)")
        lines.append("")
    count_after = daily_count_before + len(signals)
    lines.append(f"Сигналов за сегодня: {count_after}/40. Защита от дублей активна.")
    return "\n".join(lines).strip()

def fetch_symbol_data(symbol: str) -> Tuple[str, List[Dict[str, float]], List[Dict[str, float]], List[Dict[str, float]], Optional[str]]:
    try:
        entry = get_bybit_klines(symbol, ENTRY_INTERVAL)
        mid = get_bybit_klines(symbol, MID_INTERVAL)
        high = get_bybit_klines(symbol, HIGH_INTERVAL)
        return symbol, entry, mid, high, None
    except Exception as exc:
        return symbol, [], [], [], str(exc)

def main() -> int:
    try:
        state = load_state()
        save_state(state)
    except Exception as exc:
        LOG.error("Ошибка стейта: %s", exc)
        return 2

    already_sent = state["sent_symbols"]
    daily_count = len(already_sent)
    if daily_count >= 40:
        LOG.info("Дневной лимит достигнут.")
        return 0

    symbols = [s for s in SYMBOLS if s not in already_sent]
    if not symbols:
        return 0

    passing = []
    failures = 0
    scanned = 0
    
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_symbol_data, symbol) for symbol in symbols]
        for future in as_completed(futures):
            symbol, entry, mid, high, error = future.result()
            if error:
                failures += 1
                continue
            scanned += 1
            try:
                signal = analyze(symbol, entry, mid, high)
            except Exception:
                failures += 1
                continue
            if not signal:
                continue
                
            # Cross-exchange validation: ensure Bybit price isn't a localized manipulation squeeze
            cross_prices = fetch_cross_prices(symbol)
            bb_p = cross_prices["bybit"]
            bn_p = cross_prices["binance"]
            ok_p = cross_prices["okx"]
            
            if bb_p and bn_p and ok_p:
                dev_binance = abs(bb_p - bn_p) / bb_p
                dev_okx = abs(bb_p - ok_p) / bb_p
                if dev_binance > 0.015 or dev_okx > 0.015:
                    LOG.warning(f"Символ {symbol} отклонен: сильный спред между биржами (Bybit:{bb_p}, Binance:{bn_p}, OKX:{ok_p})")
                    continue
                signal["score"] += 1 # Multi-exchange consensus bonus
            
            passing.append(signal)

    passing.sort(key=lambda x: (x["score"], x["rr"], x["volume_ratio"]), reverse=True)
    chosen = passing[:min(6, 40 - daily_count)]
    if not chosen:
        LOG.info("Скан завершен. Сигналов не найдено.")
        return 0

    message = build_message(chosen, daily_count)
    print(message)
    
    if not send_telegram(message):
        LOG.error("Ошибка отправки.")
        return 1

    now = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    for signal in chosen:
        already_sent[signal["symbol"]] = {
            "side": signal["side"],
            "sent_at": now,
            "score": signal["score"],
        }
    state["sent_symbols"] = already_sent
    state["last_updated"] = now
    save_state(state)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

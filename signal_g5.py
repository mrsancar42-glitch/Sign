#!/usr/bin/env python3
"""Signal G5 V3: strict Binance Spot scanner. Sends alerts only; never places orders."""
from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

BASE_URL = "https://data-api.binance.vision"
# A focused universe of liquid USDT spot pairs. Inactive pairs are removed using exchangeInfo.
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "TRXUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
    "INJUSDT", "SUIUSDT", "TIAUSDT", "FILUSDT", "UNIUSDT", "ETCUSDT",
    "AAVEUSDT", "SEIUSDT", "POLUSDT", "1000PEPEUSDT", "1000SHIBUSDT", "WLDUSDT",
]

ENTRY_INTERVAL = "15m"
MID_INTERVAL = "1h"
HIGH_INTERVAL = "4h"
KLINE_LIMIT = 250
MIN_SCORE = 9
MIN_RR = 2.0
MODEL_RR = 2.2
MAX_STRUCTURAL_RR = 3.5
MIN_15M_ADX = 16.0
MIN_1H_ADX = 20.0
MIN_4H_ADX = 14.0
MIN_VOLUME_RATIO = 0.75  # mean of last 3 x 15m volumes vs the previous 20-bar mean
STRONG_VOLUME_RATIO = 1.0
MAX_ENTRY_DISTANCE_ATR = 1.1
MIN_STOP_ATR = 1.0
MAX_STOP_ATR = 2.5
MAX_LIVE_DRIFT_ATR = 0.35
MAX_SIGNALS_PER_SCAN = 4
MAX_SIGNALS_PER_DAY = 20
STATE_PATH = Path(__file__).with_name("signal_g5_state.json")
LOCAL_TZ = ZoneInfo("Europe/Istanbul")
HTTP_TIMEOUT = 12

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("signal-g5")


def get_json(url: str, attempts: int = 3) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SignalG5/3.0)"},
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"API request failed after {attempts} attempts: {last_error}")


def fetch_active_symbols() -> set[str]:
    payload = get_json(f"{BASE_URL}/api/v3/exchangeInfo")
    return {
        item["symbol"] for item in payload.get("symbols", [])
        if item.get("status") == "TRADING"
        and item.get("quoteAsset") == "USDT"
        and item.get("isSpotTradingAllowed", True)
    }


def fetch_klines(symbol: str, interval: str, limit: int = KLINE_LIMIT) -> List[Dict[str, float]]:
    params = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    rows = get_json(f"{BASE_URL}/api/v3/klines?{params}")
    now_ms = int(time.time() * 1000)
    result: List[Dict[str, float]] = []
    for row in rows:
        close_time = int(row[6])
        # Never use the still-forming candle for indicator or entry calculations.
        if close_time >= now_ms:
            continue
        result.append({
            "open_time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "close_time": close_time,
        })
    return result


def fetch_last_price(symbol: str) -> float:
    params = urllib.parse.urlencode({"symbol": symbol})
    data = get_json(f"{BASE_URL}/api/v3/ticker/price?{params}")
    return float(data["price"])


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    current = mean(values[:period])
    out[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for i in range(period, len(values)):
        current = alpha * values[i] + (1.0 - alpha) * current
        out[i] = current
    return out


def rsi_wilder(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = mean([max(x, 0.0) for x in changes[:period]])
    avg_loss = mean([max(-x, 0.0) for x in changes[:period]])
    for change in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss <= 1e-15:
        return 100.0 if avg_gain > 1e-15 else 50.0
    if avg_gain <= 1e-15:
        return 0.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def atr_wilder(candles: Sequence[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs: List[float] = []
    for i in range(1, len(candles)):
        hi, lo, prev_close = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
    current = mean(trs[:period])
    for tr in trs[period:]:
        current = (current * (period - 1) + tr) / period
    return current


def macd_histogram_last_two(closes: Sequence[float]) -> Optional[Tuple[float, float]]:
    fast = ema_series(closes, 12)
    slow = ema_series(closes, 26)
    macd_values = [float(fast[i]) - float(slow[i]) for i in range(len(closes))
                   if fast[i] is not None and slow[i] is not None]
    if len(macd_values) < 12:
        return None
    signal = ema_series(macd_values, 9)
    hist = [macd_values[i] - float(signal[i]) for i in range(len(macd_values)) if signal[i] is not None]
    if len(hist) < 2:
        return None
    return hist[-2], hist[-1]


def dmi_adx(candles: Sequence[Dict[str, float]], period: int = 14) -> Optional[Tuple[float, float, float]]:
    """Return (ADX, +DI, -DI) using Wilder smoothing."""
    if len(candles) < (2 * period + 2):
        return None
    trs: List[float] = []
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    for i in range(1, len(candles)):
        high, low = candles[i]["high"], candles[i]["low"]
        prev_high, prev_low, prev_close = candles[i - 1]["high"], candles[i - 1]["low"], candles[i - 1]["close"]
        up_move, down_move = high - prev_high, prev_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    tr_s = sum(trs[:period])
    plus_s = sum(plus_dm[:period])
    minus_s = sum(minus_dm[:period])
    dx: List[float] = []

    def di_dx(tr: float, plus: float, minus: float) -> Tuple[float, float, float]:
        if tr <= 0:
            return 0.0, 0.0, 0.0
        pdi, mdi = 100.0 * plus / tr, 100.0 * minus / tr
        denom = pdi + mdi
        dx_val = 0.0 if denom <= 0 else 100.0 * abs(pdi - mdi) / denom
        return dx_val, pdi, mdi

    dx.append(di_dx(tr_s, plus_s, minus_s)[0])
    for i in range(period, len(trs)):
        tr_s = tr_s - tr_s / period + trs[i]
        plus_s = plus_s - plus_s / period + plus_dm[i]
        minus_s = minus_s - minus_s / period + minus_dm[i]
        dx.append(di_dx(tr_s, plus_s, minus_s)[0])
    if len(dx) < period:
        return None
    adx = mean(dx[:period])
    for value in dx[period:]:
        adx = (adx * (period - 1) + value) / period
    _, pdi, mdi = di_dx(tr_s, plus_s, minus_s)
    return adx, pdi, mdi


def candle_trend(candles: Sequence[Dict[str, float]]) -> Optional[str]:
    closes = [c["close"] for c in candles]
    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    if len(closes) < 55 or any(v is None for v in (ema20[-1], ema20[-4], ema50[-1])):
        return None
    e20, e20_old, e50 = float(ema20[-1]), float(ema20[-4]), float(ema50[-1])
    close = closes[-1]
    if e20 > e50 and close > e50 and e20 > e20_old:
        return "LONG"
    if e20 < e50 and close < e50 and e20 < e20_old:
        return "SHORT"
    return None


def pivot_levels(candles: Sequence[Dict[str, float]], side: str, bars: int = 80) -> List[float]:
    """Confirmed 1h swing levels; the last two bars are omitted so pivots are confirmed."""
    start = max(2, len(candles) - bars)
    levels: List[float] = []
    for i in range(start, len(candles) - 2):
        window = candles[i - 2:i + 3]
        value = candles[i]["high"] if side == "LONG" else candles[i]["low"]
        candidates = [x["high"] for x in window] if side == "LONG" else [x["low"] for x in window]
        if side == "LONG" and value == max(candidates) and candidates.count(value) == 1:
            levels.append(value)
        elif side == "SHORT" and value == min(candidates) and candidates.count(value) == 1:
            levels.append(value)
    # Cluster nearly-identical swing levels to avoid duplicate targets from one price zone.
    levels.sort()
    deduped: List[float] = []
    for level in levels:
        if not deduped or abs(level - deduped[-1]) / max(abs(level), 1e-12) > 0.0015:
            deduped.append(level)
    return deduped


def price_text(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 100:
        return f"{value:.3f}"
    if value >= 1:
        return f"{value:.4f}"
    if value >= 0.01:
        return f"{value:.5f}"
    return f"{value:.8f}"


def analyze(symbol: str, entry: Sequence[Dict[str, float]], mid: Sequence[Dict[str, float]],
            high: Sequence[Dict[str, float]]) -> Optional[Dict[str, Any]]:
    if min(len(entry), len(mid), len(high)) < 60:
        return None

    trend4h, trend1h, trend15m = candle_trend(high), candle_trend(mid), candle_trend(entry)
    if not trend4h or trend4h != trend1h or trend4h != trend15m:
        return None
    side = trend4h

    closes = [c["close"] for c in entry]
    highs = [c["high"] for c in entry]
    lows = [c["low"] for c in entry]
    volumes = [c["volume"] for c in entry]
    opens = [c["open"] for c in entry]
    e20_series = ema_series(closes, 20)
    e50_series = ema_series(closes, 50)
    if e20_series[-1] is None or e50_series[-1] is None:
        return None
    e20, e50 = float(e20_series[-1]), float(e50_series[-1])
    close = closes[-1]
    atr = atr_wilder(entry)
    rsi = rsi_wilder(closes)
    macd_pair = macd_histogram_last_two(closes)
    dmi1h = dmi_adx(mid)
    dmi4h = dmi_adx(high)
    dmi15m = dmi_adx(entry)
    if atr is None or atr <= 0 or rsi is None or macd_pair is None or not dmi1h or not dmi4h or not dmi15m:
        return None
    adx1h, plus1h, minus1h = dmi1h
    adx4h, plus4h, minus4h = dmi4h
    adx15m, plus15m, minus15m = dmi15m

    if adx1h < MIN_1H_ADX or adx4h < MIN_4H_ADX or adx15m < MIN_15M_ADX:
        return None
    if side == "LONG" and not (plus1h > minus1h and plus4h > minus4h and plus15m > minus15m):
        return None
    if side == "SHORT" and not (minus1h > plus1h and minus4h > plus4h and minus15m > plus15m):
        return None

    # Entry setup: a recent pullback into the 15m EMA20 zone, then a directional close.
    # Reject late entries that are already stretched from that mean.
    distance_atr = abs(close - e20) / atr
    if distance_atr > MAX_ENTRY_DISTANCE_ATR:
        return None
    recent_lows, recent_highs = lows[-4:], highs[-4:]
    if side == "LONG":
        pullback = min(recent_lows) <= e20 + 0.20 * atr and min(recent_lows) >= e50 - 0.65 * atr
        candle_confirm = close > opens[-1] and close > e20
        rsi_ok = 49.0 <= rsi <= 66.0
        macd_ok = macd_pair[1] > 0 and macd_pair[1] > macd_pair[0]
    else:
        pullback = max(recent_highs) >= e20 - 0.20 * atr and max(recent_highs) <= e50 + 0.65 * atr
        candle_confirm = close < opens[-1] and close < e20
        rsi_ok = 34.0 <= rsi <= 51.0
        macd_ok = macd_pair[1] < 0 and macd_pair[1] < macd_pair[0]
    if not pullback or not candle_confirm or not rsi_ok or not macd_ok:
        return None

    # A 3-candle volume average is more stable than judging only the last bar.
    prior_volume = mean(volumes[-23:-3])
    recent_volume = mean(volumes[-3:])
    volume_ratio = recent_volume / prior_volume if prior_volume > 0 else 0.0
    if volume_ratio < MIN_VOLUME_RATIO:
        return None

    # Score out of 10. The three timeframe alignment and setup are required, not decorative points.
    score = 5  # 4h + 1h + 15m alignment
    score += 1  # pullback and directional candle confirmed
    score += 1  # RSI in the directional momentum zone
    score += 1  # MACD histogram has directional momentum
    score += 1  # ADX and DMI are aligned on 1h/4h/15m
    if volume_ratio >= STRONG_VOLUME_RATIO:
        score += 1
    if score < MIN_SCORE:
        return None

    # Stop goes beyond recent 15m structure, with a volatility floor and cap.
    if side == "LONG":
        sl = min(min(lows[-5:]) - 0.15 * atr, close - MIN_STOP_ATR * atr)
        risk = close - sl
    else:
        sl = max(max(highs[-5:]) + 0.15 * atr, close + MIN_STOP_ATR * atr)
        risk = sl - close
    if risk <= 0 or risk > MAX_STOP_ATR * atr:
        return None

    # Use the nearest confirmed 1h structure as TP only if there is enough room.
    # A nearby level inside 2R blocks the trade instead of pretending it is not there.
    levels = pivot_levels(mid, side)
    directional_levels = sorted([x for x in levels if x > close], reverse=False) if side == "LONG" else sorted([x for x in levels if x < close], reverse=True)
    nearest_level = directional_levels[0] if directional_levels else None
    if nearest_level is not None:
        level_distance = abs(nearest_level - close)
        level_rr = level_distance / risk
        if level_rr < MIN_RR:
            return None
        if level_rr <= MAX_STRUCTURAL_RR:
            tp = nearest_level - 0.10 * atr if side == "LONG" else nearest_level + 0.10 * atr
            target_basis = "сопротивление 1h" if side == "LONG" else "поддержка 1h"
        else:
            tp = close + MODEL_RR * risk if side == "LONG" else close - MODEL_RR * risk
            target_basis = "модельная цель 2.2R; уровень дальше цели"
    else:
        tp = close + MODEL_RR * risk if side == "LONG" else close - MODEL_RR * risk
        target_basis = "модельная цель 2.2R; ближайший уровень не найден"

    rr = abs(tp - close) / risk
    if rr + 1e-9 < MIN_RR:
        return None

    risk_pct = risk / close * 100.0
    reward_pct = abs(tp - close) / close * 100.0
    reason_parts = [f"тренд 4H/1H/15M {('вверх' if side == 'LONG' else 'вниз')}",
                    "откат к EMA20 и свеча подтверждения",
                    "MACD подтверждает импульс",
                    f"RSI {rsi:.0f}", f"ADX 1H {adx1h:.0f}"]
    if volume_ratio >= STRONG_VOLUME_RATIO:
        reason_parts.append(f"объём x{volume_ratio:.2f}")

    return {
        "symbol": symbol, "side": side, "entry": close, "tp": tp, "sl": sl,
        "rr": round(rr, 2), "score": score, "rsi": rsi, "adx1h": adx1h,
        "volume_ratio": volume_ratio, "atr": atr, "atr_pct": atr / close * 100.0,
        "risk_pct": risk_pct, "reward_pct": reward_pct, "target_basis": target_basis,
        "reason": "; ".join(reason_parts), "candle_close_time": int(entry[-1]["close_time"]),
    }


def load_state() -> Dict[str, Any]:
    today = datetime.now(LOCAL_TZ).date().isoformat()
    if not STATE_PATH.exists():
        return {"date_local": today, "sent_symbols": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("sent_symbols", {}), dict):
            raise ValueError("unexpected state format")
    except Exception as exc:
        # Fail closed: do not silently erase dedup history and spam old coins again.
        raise RuntimeError(f"Cannot read {STATE_PATH.name}; refusing to send alerts to avoid duplicates: {exc}") from exc
    if state.get("date_local") != today:
        return {"date_local": today, "sent_symbols": {}}
    state.setdefault("sent_symbols", {})
    return state


def save_state(state: Dict[str, Any]) -> None:
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(STATE_PATH)


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOG.error("Telegram is not configured: add TELEGRAM_TOKEN and TELEGRAM_CHAT_ID to GitHub Secrets")
        return False
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(endpoint, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("ok") is not True:
            LOG.error("Telegram API rejected message: %s", result.get("description", "unknown error"))
            return False
        return True
    except Exception as exc:
        LOG.error("Telegram send failed: %s", exc)
        return False


def build_message(signals: Sequence[Dict[str, Any]], daily_count_before: int) -> str:
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M TRT")
    lines = [f"📡 SIGNAL G5 | SPOT | {now}", "Тренд: 4H + 1H • вход: 15M", ""]
    for signal in signals:
        emoji = "🟢" if signal["side"] == "LONG" else "🔴"
        close_time = datetime.fromtimestamp(signal["candle_close_time"] / 1000, timezone.utc).astimezone(LOCAL_TZ).strftime("%H:%M TRT")
        lines.append(f"{emoji} {signal['symbol']} {signal['side']} | Score {signal['score']}/10")
        lines.append(f"Вход: {price_text(signal['entry'])} | TP: {price_text(signal['tp'])} (+{signal['reward_pct']:.2f}%)")
        lines.append(f"SL: {price_text(signal['sl'])} (-{signal['risk_pct']:.2f}%) | R:R {signal['rr']}")
        lines.append(f"Причина: {signal['reason']}")
        lines.append(f"TP: {signal['target_basis']} | закрытие 15M: {close_time}")
        lines.append("")
    count_after = daily_count_before + len(signals)
    lines.append(f"Сегодня: {count_after}/{MAX_SIGNALS_PER_DAY} сигналов; повтор этой монеты сегодня отключён.")
    return "\n".join(lines).strip()


def fetch_symbol_data(symbol: str) -> Tuple[str, List[Dict[str, float]], List[Dict[str, float]], List[Dict[str, float]], Optional[str]]:
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            f15 = pool.submit(fetch_klines, symbol, ENTRY_INTERVAL)
            f1h = pool.submit(fetch_klines, symbol, MID_INTERVAL)
            f4h = pool.submit(fetch_klines, symbol, HIGH_INTERVAL)
            return symbol, f15.result(), f1h.result(), f4h.result(), None
    except Exception as exc:
        return symbol, [], [], [], str(exc)


def main() -> int:
    try:
        state = load_state()
        # Persist a daily rollover even when this scan produces no signals.
        save_state(state)
    except Exception as exc:
        LOG.error("State error: %s", exc)
        return 2

    already_sent: Dict[str, Any] = state["sent_symbols"]
    daily_count = len(already_sent)
    slots_today = max(0, MAX_SIGNALS_PER_DAY - daily_count)
    if slots_today == 0:
        LOG.info("Daily cap reached (%d/%d); no scan alerts will be sent today.", daily_count, MAX_SIGNALS_PER_DAY)
        return 0

    try:
        active = fetch_active_symbols()
    except Exception as exc:
        LOG.error("Cannot load Binance spot symbol list: %s", exc)
        return 1
    symbols = [s for s in SYMBOLS if s in active and s not in already_sent]
    if not symbols:
        LOG.info("No eligible unreported symbols remain in the configured universe today.")
        return 0

    passing: List[Dict[str, Any]] = []
    failures = 0
    scanned = 0
    LOG.info("Scanning %d not-yet-reported symbols across 15m/1h/4h.", len(symbols))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_symbol_data, symbol) for symbol in symbols]
        for future in as_completed(futures):
            symbol, entry, mid, high, error = future.result()
            if error:
                failures += 1
                LOG.warning("%s data fetch failed: %s", symbol, error)
                continue
            scanned += 1
            try:
                signal = analyze(symbol, entry, mid, high)
            except Exception as exc:
                failures += 1
                LOG.warning("Analysis failed for %s: %s", symbol, exc)
                continue
            if not signal:
                continue
            try:
                live_price = fetch_last_price(symbol)
            except Exception as exc:
                failures += 1
                LOG.warning("Cannot check live price for %s; skipping potentially stale alert: %s", symbol, exc)
                continue
            drift_atr = abs(live_price - signal["entry"]) / signal["atr"]
            if drift_atr > MAX_LIVE_DRIFT_ATR:
                LOG.info("%s filtered as stale: price drift %.2f ATR", symbol, drift_atr)
                continue
            if signal["side"] == "LONG" and not (signal["sl"] < live_price < signal["tp"]):
                LOG.info("%s filtered: live price is outside entry/TP/SL bounds", symbol)
                continue
            if signal["side"] == "SHORT" and not (signal["tp"] < live_price < signal["sl"]):
                LOG.info("%s filtered: live price is outside entry/TP/SL bounds", symbol)
                continue
            passing.append(signal)

    passing.sort(key=lambda x: (x["score"], x["rr"], x["volume_ratio"], -x["risk_pct"]), reverse=True)
    chosen = passing[:min(MAX_SIGNALS_PER_SCAN, slots_today)]
    LOG.info("Scan complete: scanned=%d fetch/analysis errors=%d passing=%d chosen=%d sent_today=%d/%d",
             scanned, failures, len(passing), len(chosen), daily_count, MAX_SIGNALS_PER_DAY)
    if not chosen:
        LOG.info("No new signals met every filter. No Telegram message sent.")
        return 0

    message = build_message(chosen, daily_count)
    print(message)
    if not send_telegram(message):
        LOG.error("Telegram delivery failed; dedup state was not updated for these symbols.")
        return 1

    now = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    for signal in chosen:
        already_sent[signal["symbol"]] = {
            "side": signal["side"],
            "candle_close_time": signal["candle_close_time"],
            "sent_at": now,
            "score": signal["score"],
        }
    state["sent_symbols"] = already_sent
    state["last_updated"] = now
    try:
        save_state(state)
    except Exception as exc:
        LOG.critical("Telegram was sent but dedup state could not be saved: %s", exc)
        return 2
    LOG.info("Telegram delivered. Marked %d symbols as sent for %s.", len(chosen), state["date_local"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

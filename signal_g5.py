#!/usr/bin/env python3
"""Educational crypto market scanner. Generates Telegram alerts; never places orders."""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

BASE_URL = "https://data-api.binance.vision"
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "POLUSDT", "LTCUSDT", "ATOMUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT",
    "TIAUSDT", "DOGEUSDT",
]
ENTRY_INTERVAL = "15m"
TREND_INTERVAL = "1h"
KLINE_LIMIT = 250
TOP_N = 6
MIN_RR = 2.0
MIN_SCORE = 6
MAX_STOP_ATR = 3.0
MIN_STOP_ATR = 1.2
VOLUME_MULTIPLIER = 1.2
HTTP_TIMEOUT = 12

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("signal-bot")


def get_json(url: str, attempts: int = 3) -> Any:
    """GET JSON with a small retry/backoff policy."""
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; EducationalSignalScanner/2.0)"},
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.7 * (attempt + 1))
    raise RuntimeError(f"GET failed after {attempts} attempts: {last_error}")


def fetch_active_symbols() -> Optional[set]:
    """Return active USDT spot symbols; None means exchangeInfo could not be read."""
    try:
        payload = get_json(f"{BASE_URL}/api/v3/exchangeInfo")
        return {
            item["symbol"] for item in payload.get("symbols", [])
            if item.get("status") == "TRADING"
            and item.get("quoteAsset") == "USDT"
            and item.get("isSpotTradingAllowed", True)
        }
    except Exception as exc:
        LOG.warning("Could not fetch exchangeInfo; will try configured symbols anyway: %s", exc)
        return None


def fetch_klines(symbol: str, interval: str, limit: int = KLINE_LIMIT) -> List[Dict[str, float]]:
    params = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    rows = get_json(f"{BASE_URL}/api/v3/klines?{params}")
    now_ms = int(time.time() * 1000)
    candles: List[Dict[str, float]] = []
    for row in rows:
        # Binance kline layout: open time, open, high, low, close, volume, close time, ...
        close_time = int(row[6])
        # Exclude the currently forming candle, even if the API already returned it.
        if close_time >= now_ms:
            continue
        candles.append({
            "open_time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "close_time": close_time,
        })
    return candles


def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    """True exponential moving average, seeded with the first-period SMA."""
    result: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        current = alpha * values[index] + (1.0 - alpha) * current
        result[index] = current
    return result


def rsi_wilder(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(max(change, 0.0) for change in changes[:period]) / period
    avg_loss = sum(max(-change, 0.0) for change in changes[:period]) / period
    for change in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr_wilder(candles: Sequence[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs: List[float] = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        previous_close = candles[i - 1]["close"]
        trs.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if len(trs) < period:
        return None
    current = sum(trs[:period]) / period
    for tr in trs[period:]:
        current = (current * (period - 1) + tr) / period
    return current


def macd_histogram(closes: Sequence[float]) -> Optional[float]:
    fast = ema_series(closes, 12)
    slow = ema_series(closes, 26)
    macd_values: List[float] = []
    for i in range(len(closes)):
        if fast[i] is not None and slow[i] is not None:
            macd_values.append(float(fast[i]) - float(slow[i]))
    if len(macd_values) < 9:
        return None
    signal = ema_series(macd_values, 9)
    if signal[-1] is None:
        return None
    return macd_values[-1] - float(signal[-1])


def adx_wilder(candles: Sequence[Dict[str, float]], period: int = 14) -> Optional[float]:
    """Wilder ADX; used as a trend-strength filter, not as a direction signal."""
    if len(candles) < 2 * period + 1:
        return None
    tr_values: List[float] = []
    plus_values: List[float] = []
    minus_values: List[float] = []
    for i in range(1, len(candles)):
        high, low = candles[i]["high"], candles[i]["low"]
        prev_high, prev_low = candles[i - 1]["high"], candles[i - 1]["low"]
        prev_close = candles[i - 1]["close"]
        up_move = high - prev_high
        down_move = prev_low - low
        plus_values.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_values.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        tr_values.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    tr_smoothed = sum(tr_values[:period])
    plus_smoothed = sum(plus_values[:period])
    minus_smoothed = sum(minus_values[:period])
    dx_values: List[float] = []

    def calc_dx(tr: float, plus: float, minus: float) -> float:
        if tr <= 0:
            return 0.0
        plus_di = 100.0 * plus / tr
        minus_di = 100.0 * minus / tr
        denom = plus_di + minus_di
        return 0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom

    dx_values.append(calc_dx(tr_smoothed, plus_smoothed, minus_smoothed))
    for i in range(period, len(tr_values)):
        tr_smoothed = tr_smoothed - tr_smoothed / period + tr_values[i]
        plus_smoothed = plus_smoothed - plus_smoothed / period + plus_values[i]
        minus_smoothed = minus_smoothed - minus_smoothed / period + minus_values[i]
        dx_values.append(calc_dx(tr_smoothed, plus_smoothed, minus_smoothed))
    if len(dx_values) < period:
        return None
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def price_text(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}"
    if value >= 0.01:
        return f"{value:.5f}"
    return f"{value:.8f}"


def analyze(symbol: str, entry_candles: Sequence[Dict[str, float]], trend_candles: Sequence[Dict[str, float]]) -> Optional[Dict[str, Any]]:
    if len(entry_candles) < 60 or len(trend_candles) < 60:
        return None

    closes = [c["close"] for c in entry_candles]
    highs = [c["high"] for c in entry_candles]
    lows = [c["low"] for c in entry_candles]
    volumes = [c["volume"] for c in entry_candles]
    entry_ema20 = ema_series(closes, 20)[-1]
    entry_ema50 = ema_series(closes, 50)[-1]
    trend_closes = [c["close"] for c in trend_candles]
    trend_ema20 = ema_series(trend_closes, 20)[-1]
    trend_ema50 = ema_series(trend_closes, 50)[-1]
    rsi = rsi_wilder(closes)
    atr = atr_wilder(entry_candles)
    macd = macd_histogram(closes)
    adx = adx_wilder(entry_candles)

    needed = [entry_ema20, entry_ema50, trend_ema20, trend_ema50, rsi, atr, macd, adx]
    if any(value is None for value in needed) or float(atr) <= 0:
        return None

    close = closes[-1]
    e20, e50 = float(entry_ema20), float(entry_ema50)
    htf20, htf50 = float(trend_ema20), float(trend_ema50)
    rsi_value, atr_value, macd_value, adx_value = float(rsi), float(atr), float(macd), float(adx)

    # Strong alignment required: both 1h and 15m trends must agree.
    if htf20 > htf50 and trend_closes[-1] > htf50:
        side = "LONG"
    elif htf20 < htf50 and trend_closes[-1] < htf50:
        side = "SHORT"
    else:
        return None

    if side == "LONG" and not (e20 > e50 and close > e50):
        return None
    if side == "SHORT" and not (e20 < e50 and close < e50):
        return None

    # Avoid chasing a move that is already far away from the entry-timeframe EMA20.
    distance_atr = abs(close - e20) / atr_value
    if distance_atr > 1.8:
        return None

    score = 3  # 1h direction and close agree
    reasons = ["тренд 1h подтверждён"]
    score += 2
    reasons.append("тренд 15m совпадает")

    if side == "LONG" and 47 <= rsi_value <= 68:
        score += 1
        reasons.append(f"RSI {rsi_value:.1f}")
    elif side == "SHORT" and 32 <= rsi_value <= 53:
        score += 1
        reasons.append(f"RSI {rsi_value:.1f}")

    if (side == "LONG" and macd_value > 0) or (side == "SHORT" and macd_value < 0):
        score += 1
        reasons.append("MACD подтверждает")

    avg_volume = sum(volumes[-21:-1]) / 20.0
    volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 0.0
    if volume_ratio >= VOLUME_MULTIPLIER:
        score += 1
        reasons.append(f"объём x{volume_ratio:.2f}")

    if adx_value >= 18:
        score += 1
        reasons.append(f"ADX {adx_value:.1f}")

    if distance_atr <= 1.0:
        score += 1
        reasons.append("цена не растянута от EMA20")

    if score < MIN_SCORE:
        return None

    # SL beyond recent structure, but at least 1.2 ATR away.
    if side == "LONG":
        recent_low = min(lows[-5:])
        sl = min(recent_low - 0.25 * atr_value, close - MIN_STOP_ATR * atr_value)
        risk = close - sl
        if risk <= 0 or risk > MAX_STOP_ATR * atr_value:
            return None
        # Prefer the nearest prior swing high that leaves at least the required R:R.
        prior_highs = highs[-31:-1]
        targets = [level for level in prior_highs if level >= close + MIN_RR * risk]
        if targets:
            tp = min(targets)
            target_basis = "ближайшее сопротивление"
        else:
            tp = close + 2.2 * risk
            target_basis = "модельная цель 2.2R; сопротивление не найдено"
    else:
        recent_high = max(highs[-5:])
        sl = max(recent_high + 0.25 * atr_value, close + MIN_STOP_ATR * atr_value)
        risk = sl - close
        if risk <= 0 or risk > MAX_STOP_ATR * atr_value:
            return None
        prior_lows = lows[-31:-1]
        targets = [level for level in prior_lows if level <= close - MIN_RR * risk]
        if targets:
            tp = max(targets)
            target_basis = "ближайшая поддержка"
        else:
            tp = close - 2.2 * risk
            target_basis = "модельная цель 2.2R; поддержка не найдена"

    rr = abs(tp - close) / abs(close - sl) if abs(close - sl) > 0 else 0.0
    if rr + 1e-9 < MIN_RR:
        return None

    risk_pct = abs(close - sl) / close * 100.0
    reward_pct = abs(tp - close) / close * 100.0
    return {
        "symbol": symbol,
        "side": side,
        "entry": close,
        "tp": tp,
        "sl": sl,
        "rr": round(rr, 2),
        "score": score,
        "max_score": 10,
        "rsi": rsi_value,
        "adx": adx_value,
        "volume_ratio": volume_ratio,
        "atr_pct": atr_value / close * 100.0,
        "risk_pct": risk_pct,
        "reward_pct": reward_pct,
        "target_basis": target_basis,
        "reasons": reasons,
        "candle_close_time": int(entry_candles[-1]["close_time"]),
    }


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOG.warning("Telegram is not configured. Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in GitHub Secrets.")
        return False
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    try:
        request = urllib.request.Request(endpoint, data=body, method="POST")
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            LOG.error("Telegram API rejected the message: %s", payload)
            return False
        return True
    except Exception as exc:
        LOG.error("Telegram send failed: %s", exc)
        return False


def build_message(signals: Sequence[Dict[str, Any]], scanned: int, failed: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "📡 CRYPTO SCANNER V2 | SPOT",
        f"Обновление: {now}",
        "Таймфреймы: вход 15m • фильтр тренда 1h",
        f"Проверено пар: {scanned} | ошибок загрузки: {failed}",
        f"Фильтры: score ≥ {MIN_SCORE}/10 • R:R ≥ {MIN_RR:.1f} • закрытые свечи",
        "",
    ]
    if not signals:
        lines.append("Сейчас нет сигналов, прошедших все фильтры. Это нормальный результат фильтрации.")
        return "\n".join(lines)

    for signal in signals:
        emoji = "🟢" if signal["side"] == "LONG" else "🔴"
        time_text = datetime.fromtimestamp(signal["candle_close_time"] / 1000, timezone.utc).strftime("%H:%M UTC")
        lines.append(f"{emoji} {signal['symbol']} | {signal['side']} | Score {signal['score']}/{signal['max_score']}")
        lines.append(f"Вход по закрытию 15m: {price_text(signal['entry'])}")
        lines.append(f"TP: {price_text(signal['tp'])} (+{signal['reward_pct']:.2f}%)")
        lines.append(f"SL: {price_text(signal['sl'])} (-{signal['risk_pct']:.2f}%)")
        lines.append(f"R:R {signal['rr']} | ATR {signal['atr_pct']:.2f}% | RSI {signal['rsi']:.1f} | ADX {signal['adx']:.1f}")
        lines.append(f"Объём: x{signal['volume_ratio']:.2f} | Свеча закрылась: {time_text}")
        lines.append(f"TP-логика: {signal['target_basis']}")
        lines.append("Факторы: " + ", ".join(signal["reasons"]))
        lines.append("")
    lines.append("Учебный сканер, не гарантия результата. Комиссии, проскальзывание и исполнение не учтены; сигналы не открывают сделки автоматически.")
    return "\n".join(lines)


def fetch_symbol_data(symbol: str) -> Tuple[str, List[Dict[str, float]], List[Dict[str, float]], Optional[str]]:
    try:
        entry = fetch_klines(symbol, ENTRY_INTERVAL)
        trend = fetch_klines(symbol, TREND_INTERVAL)
        return symbol, entry, trend, None
    except Exception as exc:
        return symbol, [], [], str(exc)


def main() -> int:
    active = fetch_active_symbols()
    symbols = [symbol for symbol in SYMBOLS if active is None or symbol in active]
    if not symbols:
        LOG.error("None of the configured symbols are currently tradable on Binance spot.")
        return 1

    all_signals: List[Dict[str, Any]] = []
    failed = 0
    fetched = 0
    LOG.info("Scanning %d symbols...", len(symbols))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_symbol_data, symbol) for symbol in symbols]
        for future in as_completed(futures):
            symbol, entry, trend, error = future.result()
            if error:
                failed += 1
                LOG.warning("%s data fetch failed: %s", symbol, error)
                continue
            fetched += 1
            signal = analyze(symbol, entry, trend)
            if signal:
                all_signals.append(signal)

    all_signals.sort(
        key=lambda item: (item["score"], item["rr"], item["volume_ratio"], -item["risk_pct"]),
        reverse=True,
    )
    top = all_signals[:TOP_N]
    message = build_message(top, fetched, failed)
    print(message)
    send_telegram(message)
    LOG.info("Scan complete: fetched=%d failed=%d passing=%d sent=%d", fetched, failed, len(all_signals), len(top))
    return 0 if fetched > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

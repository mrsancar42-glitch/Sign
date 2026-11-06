
import os
import json
import time
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import ccxt
import requests

# GPT 2.1 QUANT INTELLIGENCE
# Public market data only. No order execution.

TZ = ZoneInfo("Europe/Istanbul")
STATE_FILE = Path("gpt21_state.json")

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

EXCHANGES = ("okx", "bingx")
TIMEFRAME = "15m"
HTF = "1h"
MIN_SCORE = int(os.getenv("MIN_SCORE", "70"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

http = requests.Session()


def now():
    return datetime.now(TZ)


def quiet_hours():
    return now().hour < 7


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text())
        return {
            "sent": data.get("sent", []),
            "daily": data.get("daily", {}),
            "last_scan": data.get("last_scan", {})
        }
    except (OSError, ValueError):
        return {"sent": [], "daily": {}, "last_scan": {}}


def save_state(state):
    state["sent"] = state["sent"][-5000:]
    fd, name = tempfile.mkstemp(
        dir=".", prefix="gpt21_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, STATE_FILE)
    finally:
        if os.path.exists(name):
            os.remove(name)


def telegram(message):
    if quiet_hours():
        logging.info("Night quiet hours: message blocked")
        return False

    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets are missing")

    response = http.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=20
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")

    return True


def make_exchange(name):
    cls = getattr(ccxt, name)
    return cls({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "swap"}
    })


def eligible_markets(exchange):
    exchange.load_markets()
    result = []

    for market in exchange.markets.values():
        if market.get("active") is False:
            continue
        if not market.get("swap"):
            continue
        if market.get("linear") is not True:
            continue
        if market.get("quote") != "USDT":
            continue
        if market.get("settle") != "USDT":
            continue
        if not market.get("symbol"):
            continue
        if market.get("expiry") is not None:
            continue

        result.append(market["symbol"])

    return sorted(set(result))


def closed_candles(exchange, symbol, timeframe, limit=100):
    rows = exchange.fetch_ohlcv(
        symbol, timeframe=timeframe, limit=limit
    )

    duration = exchange.parse_timeframe(timeframe) * 1000
    current_ms = exchange.milliseconds()

    # Only fully closed candles.
    result = [
        row for row in rows
        if row[0] + duration <= current_ms
        and len(row) >= 6
        and all(v is not None for v in row[:6])
        and row[2] >= row[3]
    ]

    unique = {int(row[0]): row for row in result}
    return [unique[k] for k in sorted(unique)]


def ema(values, period):
    if len(values) < period:
        return None

    alpha = 2 / (period + 1)
    value = sum(values[:period]) / period

    for price in values[period:]:
        value = alpha * price + (1 - alpha) * value

    return value


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    ranges = []

    for i in range(len(candles) - period, len(candles)):
        c, p = candles[i], candles[i - 1]
        ranges.append(max(
            c[2] - c[3],
            abs(c[2] - p[4]),
            abs(c[3] - p[4])
        ))

    return sum(ranges) / len(ranges)


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(len(values) - period, len(values))
    ]

    gains = sum(max(x, 0) for x in changes) / period
    losses = sum(max(-x, 0) for x in changes) / period

    if losses == 0:
        return 100.0

    rs = gains / losses
    return 100 - 100 / (1 + rs)


def market_bias(candles):
    closes = [c[4] for c in candles]

    fast = ema(closes, 20)
    slow = ema(closes, 50)

    if fast is None or slow is None:
        return "NEUTRAL"

    if closes[-1] > fast > slow:
        return "BULLISH"

    if closes[-1] < fast < slow:
        return "BEARISH"

    return "NEUTRAL"


def htf_bias(exchange, symbol):
    try:
        candles = closed_candles(exchange, symbol, HTF, 100)
        if len(candles) < 55:
            return "NEUTRAL"
        return market_bias(candles)
    except Exception:
        logging.exception("HTF error %s", symbol)
        return "NEUTRAL"


def find_setup(candles, bias):
    """
    Hybrid trend + breakout/retest + momentum model.
    Uses only closed candles.
    """

    if len(candles) < 60:
        return None

    closes = [c[4] for c in candles]
    current = candles[-1]
    previous = candles[-2]

    current_atr = atr(candles)
    current_rsi = rsi(closes)

    fast = ema(closes, 20)
    slow = ema(closes, 50)

    if not all(x is not None for x in (
        current_atr, current_rsi, fast, slow
    )):
        return None

    if current_atr <= 0:
        return None

    open_price, high, low, close, volume = (
        current[1], current[2], current[3],
        current[4], current[5]
    )

    candle_range = max(high - low, 1e-12)
    body = abs(close - open_price)

    average_body = sum(
        abs(c[4] - c[1]) for c in candles[-21:-1]
    ) / 20

    average_volume = sum(
        c[5] for c in candles[-21:-1]
    ) / 20

    volume_ratio = (
        volume / average_volume
        if average_volume > 0 else 0
    )

    # Previous range excludes the current candle.
    resistance = max(c[2] for c in candles[-21:-1])
    support = min(c[3] for c in candles[-21:-1])

    bullish = (
        close > resistance
        and close > open_price
        and close > fast > slow
        and bias != "BEARISH"
        and 52 <= current_rsi <= 78
    )

    bearish = (
        close < support
        and close < open_price
        and close < fast < slow
        and bias != "BULLISH"
        and 22 <= current_rsi <= 48
    )

    if bullish == bearish:
        return None

    direction = "LONG" if bullish else "SHORT"

    # Require meaningful candle body and volume participation.
    if body < max(average_body * 1.15, current_atr * 0.30):
        return None

    if volume_ratio < 0.80:
        return None

    close_strength = (
        (close - low) / candle_range
        if direction == "LONG"
        else (high - close) / candle_range
    )

    if close_strength < 0.70:
        return None

    # Structural stop based on recent swing, with ATR buffer.
    recent = candles[-8:-1]

    if direction == "LONG":
        swing = min(c[3] for c in recent)
        stop = swing - current_atr * 0.20
        risk = close - stop
    else:
        swing = max(c[2] for c in recent)
        stop = swing + current_atr * 0.20
        risk = stop - close

    if risk <= 0:
        return None

    # Reject both noise-sized and excessively wide stops.
    if risk < current_atr * 0.60:
        return None

    if risk > current_atr * 3.0:
        return None

    # Score is a rule-based ranking, NOT win probability.
    score = 0
    score += 25  # Trend alignment
    score += 20  # Breakout
    score += 15  # Momentum
    score += 15  # Candle strength
    score += 10 if volume_ratio >= 1.20 else 5
    score += 10 if bias == (
        "BULLISH" if direction == "LONG" else "BEARISH"
    ) else 0

    if score < MIN_SCORE:
        return None

    # Three risk multiples. These are projected targets,
    # not guaranteed fills.
    multiples = (1.5, 2.5, 3.5)

    if direction == "LONG":
        targets = [close + risk * x for x in multiples]
    else:
        targets = [close - risk * x for x in multiples]

    return {
        "direction": direction,
        "entry": close,
        "stop": stop,
        "risk": risk,
        "targets": targets,
        "score": score,
        "atr": current_atr,
        "rsi": current_rsi,
        "volume_ratio": volume_ratio,
        "bias": bias,
        "candle_time": int(current[0])
    }


def fmt(value):
    return f"{value:.8g}"


def build_message(symbol, exchange_name, signal):
    local_time = datetime.fromtimestamp(
        signal["candle_time"] / 1000,
        timezone.utc
    ).astimezone(TZ)

    side = signal["direction"]
    icon = "🟢" if side == "LONG" else "🔴"

    return (
        "🤖 GPT 2.1 | QUANT INTELLIGENCE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{icon} {side} — {symbol}\n"
        f"Биржа: {exchange_name.upper()}\n"
        f"Таймфрейм: {TIMEFRAME} / {HTF}\n\n"
        f"Оценка модели: {signal['score']}/100\n"
        f"Рыночный уклон: {signal['bias']}\n"
        f"RSI: {signal['rsi']:.1f}\n"
        f"Объём / средний: {signal['volume_ratio']:.2f}x\n\n"
        f"ENTRY: {fmt(signal['entry'])}\n"
        f"STOP LOSS: {fmt(signal['stop'])}\n"
        f"Риск на единицу: {fmt(signal['risk'])}\n\n"
        f"TP1: {fmt(signal['targets'][0])}\n"
        f"TP2: {fmt(signal['targets'][1])}\n"
        f"TP3: {fmt(signal['targets'][2])}\n\n"
        "Цели: 1.5R / 2.5R / 3.5R\n"
        f"Закрытая свеча: {local_time:%d.%m.%Y %H:%M} Istanbul\n\n"
        "Оценка модели не является вероятностью прибыли.\n"
        "Аналитический сигнал. Не финансовая рекомендация."
    )


def scan_exchange(name, exchange, state):
    symbols = eligible_markets(exchange)

    logging.info(
        "%s: %d eligible USDT perpetual markets",
        name, len(symbols)
    )

    scanned = 0
    errors = 0
    sent = 0

    for symbol in symbols:
        if quiet_hours():
            logging.info("Night stop reached")
            break

        try:
            candles = closed_candles(
                exchange, symbol, TIMEFRAME
            )

            if len(candles) < 60:
                continue

            scanned += 1

            bias = htf_bias(exchange, symbol)
            signal = find_setup(candles, bias)

            if signal is None:
                continue

            key = (
                f"{name}|{symbol}|{signal['direction']}|"
                f"{signal['candle_time']}"
            )

            if key in state["sent"]:
                continue

            message = build_message(
                symbol, name, signal
            )

            if telegram(message):
                state["sent"].append(key)
                sent += 1
                save_state(state)

                logging.info("SIGNAL SENT %s", key)

        except ccxt.RateLimitExceeded:
            errors += 1
            logging.exception("Rate limit: %s %s", name, symbol)
            time.sleep(5)

        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable):
            errors += 1
            logging.exception("Network issue: %s %s", name, symbol)
            time.sleep(3)

        except Exception:
            errors += 1
            logging.exception("Scan error: %s %s", name, symbol)

    state["last_scan"][name] = {
        "time": now().isoformat(),
        "markets_found": len(symbols),
        "markets_scanned": scanned,
        "signals_sent": sent,
        "errors": errors
    }

    save_state(state)

    logging.info(
        "%s finished: found=%d scanned=%d sent=%d errors=%d",
        name, len(symbols), scanned, sent, errors
    )


def main():
    if quiet_hours():
        logging.info("Quiet hours: no scanning or Telegram messages")
        return

    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Missing Telegram secrets")

    state = load_state()

    for name in EXCHANGES:
        if quiet_hours():
            break

        exchange = None

        try:
            logging.info("Starting exchange %s", name)

            exchange = make_exchange(name)
            scan_exchange(name, exchange, state)

        except Exception:
            logging.exception("Exchange failure: %s", name)

        finally:
            if exchange is not None:
                try:
                    exchange.close()
                except Exception:
                    pass

            save_state(state)

    logging.info("GPT 2.1 scan completed")


if __name__ == "__main__":
    main()
  

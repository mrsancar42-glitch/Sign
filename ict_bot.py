
import os
import json
import time
import logging
import tempfile
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import ccxt
import requests

# ==================================================
# ICT FUTURES PRO
# Public market data only. No automatic trading.
# ==================================================

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TZ = ZoneInfo("Europe/Istanbul")

TIMEFRAME = "15m"
HTF = "1h"

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "40"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "72"))
MIN_RR = 2.0

STATE_FILE = Path("state.json")
BITUNIX_URL = "https://fapi.bitunix.com"

EXCHANGES = ("okx", "bingx", "bybit")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

http = requests.Session()
http.headers.update({"User-Agent": "ICT-Futures-Scanner/1.0"})


# ---------------- TIME ----------------

def is_quiet():
    hour = datetime.now(TZ).hour
    return hour < 6


# ---------------- STATE ----------------

def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {
            "cursors": data.get("cursors", {}),
            "sent": data.get("sent", [])
        }
    except (FileNotFoundError, json.JSONDecodeError):
        return {"cursors": {}, "sent": []}


def save_state(state):
    state["sent"] = state["sent"][-5000:]

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        dir=str(STATE_FILE.parent),
        prefix="state_"
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_name, STATE_FILE)

    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


# ---------------- TELEGRAM ----------------

def telegram(message):
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


# ---------------- EXCHANGES ----------------

def make_exchange(name):
    exchange_class = getattr(ccxt, name)

    return exchange_class({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "swap"}
    })


def ccxt_markets(exchange):
    markets = exchange.load_markets()
    result = {}

    for market in markets.values():
        if (
            market.get("active") is not False
            and market.get("swap") is True
            and market.get("linear") is True
            and market.get("quote") == "USDT"
            and market.get("settle") == "USDT"
        ):
            result[market["base"]] = market["symbol"]

    return result


# ---------------- BITUNIX ----------------

def bitunix_get(path, params=None):
    response = http.get(
        BITUNIX_URL + path,
        params=params or {},
        timeout=20
    )

    response.raise_for_status()
    result = response.json()

    if result.get("code") != 0:
        raise RuntimeError(f"Bitunix API error: {result}")

    return result.get("data", [])


def bitunix_markets():
    rows = bitunix_get(
        "/api/v1/futures/market/trading_pairs"
    )

    result = {}

    for row in rows:
        if (
            row.get("quote") == "USDT"
            and row.get("symbolStatus") == "OPEN"
        ):
            result[row["base"]] = row["symbol"]

    return result


def bitunix_candles(symbol):
    rows = bitunix_get(
        "/api/v1/futures/market/kline",
        {
            "symbol": symbol,
            "interval": TIMEFRAME,
            "limit": 100,
            "type": "LAST_PRICE"
        }
    )

    candles = []

    for row in rows:
        candles.append([
            int(row["time"]),
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row.get("baseVol", 0))
        ])

    return sorted(candles, key=lambda x: x[0])


# ---------------- INDICATORS ----------------

def true_range(current, previous):
    return max(
        current[2] - current[3],
        abs(current[2] - previous[4]),
        abs(current[3] - previous[4])
    )


def calculate_atr(candles, period=14):
    ranges = []

    for i in range(-period, 0):
        ranges.append(
            true_range(candles[i], candles[i - 1])
        )

    return sum(ranges) / len(ranges)


def get_htf_bias(exchange):
    try:
        candles = exchange.fetch_ohlcv(
            "BTC/USDT:USDT",
            timeframe=HTF,
            limit=40
        )

        closed = candles[:-1]

        if len(closed) < 25:
            return "NEUTRAL"

        closes = [x[4] for x in closed]
        sma = sum(closes[-20:]) / 20

        if closes[-1] > sma:
            return "BULLISH"

        if closes[-1] < sma:
            return "BEARISH"

    except Exception:
        logging.exception("BTC HTF analysis failed")

    return "NEUTRAL"


# ---------------- ICT ANALYSIS ----------------

def analyze_ict(candles, already_closed=False):
    if not candles or len(candles) < 35:
        return None

    # CCXT's latest candle can still be forming.
    # Bitunix endpoint returns closed candles only.
    data = candles if already_closed else candles[:-1]

    if len(data) < 30:
        return None

    current = data[-1]
    ts, open_price, high, low, close, volume = current

    lookback = data[-22:-2]

    if len(lookback) < 20:
        return None

    range_high = max(x[2] for x in lookback)
    range_low = min(x[3] for x in lookback)

    avg_body = sum(
        abs(x[4] - x[1]) for x in lookback
    ) / len(lookback)

    avg_volume = sum(
        x[5] for x in lookback
    ) / len(lookback)

    atr = calculate_atr(data)

    if atr <= 0 or avg_body <= 0:
        return None

    candle_body = abs(close - open_price)
    candle_range = max(high - low, 1e-12)

    # Liquidity sweep
    bull_sweep = low < range_low and close > range_low
    bear_sweep = high > range_high and close < range_high

    # Displacement
    displacement = candle_body >= avg_body * 1.35

    # Fair Value Gap
    bull_fvg = low > data[-3][2]
    bear_fvg = high < data[-3][3]

    # Market Structure Shift
    recent_high = max(x[2] for x in data[-7:-1])
    recent_low = min(x[3] for x in data[-7:-1])

    bull_mss = close > recent_high
    bear_mss = close < recent_low

    # Strong candle close
    bull_close = (
        close > open_price
        and (high - close) / candle_range < 0.30
    )

    bear_close = (
        close < open_price
        and (close - low) / candle_range < 0.30
    )

    # Relative volume
    volume_ok = (
        avg_volume > 0
        and volume >= avg_volume * 0.75
    )

    direction = None

    if (
        bull_sweep
        and displacement
        and bull_fvg
        and bull_mss
        and bull_close
    ):
        direction = "LONG"

    elif (
        bear_sweep
        and displacement
        and bear_fvg
        and bear_mss
        and bear_close
    ):
        direction = "SHORT"

    if direction is None:
        return None

    entry = close

    if direction == "LONG":
        sl = low - atr * 0.15
        risk = entry - sl

        if risk <= 0:
            return None

        targets = [
            entry + risk * 2,
            entry + risk * 3,
            entry + risk * 4
        ]

    else:
        sl = high + atr * 0.15
        risk = sl - entry

        if risk <= 0:
            return None

        targets = [
            entry - risk * 2,
            entry - risk * 3,
            entry - risk * 4
        ]

    # Transparent heuristic score, not win probability.
    score = 60

    if volume_ok:
        score += 10

    if displacement:
        score += 10

    if candle_range >= atr * 0.7:
        score += 5

    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "targets": targets,
        "score": min(score, 85),
        "timestamp": int(ts),
        "volume_ok": volume_ok,
        "atr": atr
    }


# ---------------- SIGNAL MESSAGE ----------------

def fmt(number):
    return f"{number:.8g}"


def make_message(base, source, signal, bias):
    direction = signal["direction"]

    emoji = "🟢" if direction == "LONG" else "🔴"

    timestamp = datetime.fromtimestamp(
        signal["timestamp"] / 1000,
        TZ
    ).strftime("%d.%m.%Y %H:%M")

    return (
        f"⚡ ICT FUTURES PRO\n\n"
        f"{emoji} {direction} | {base}/USDT\n"
        f"Source: {source}\n"
        f"Bybit perpetual: VERIFIED\n"
        f"Timeframe: {TIMEFRAME}\n"
        f"BTC 1H: {bias}\n\n"
        f"ICT SCORE: {signal['score']}/100\n\n"
        f"ENTRY: {fmt(signal['entry'])}\n"
        f"STOP LOSS: {fmt(signal['sl'])}\n\n"
        f"TP1: {fmt(signal['targets'][0])}\n"
        f"TP2: {fmt(signal['targets'][1])}\n"
        f"TP3: {fmt(signal['targets'][2])}\n\n"
        f"Risk/Reward: 1:2 / 1:3 / 1:4\n"
        f"Volume confirmation: "
        f"{'YES' if signal['volume_ok'] else 'NO'}\n\n"
        f"Conditions:\n"
        f"✓ Liquidity Sweep\n"
        f"✓ Displacement\n"
        f"✓ FVG\n"
        f"✓ MSS\n\n"
        f"Closed candle: {timestamp} Istanbul\n\n"
        f"⚠️ Analytical setup, not a profit guarantee."
    )


# ---------------- ROTATING SCANNER ----------------

def scan_source(
    name,
    markets,
    state,
    bybit_bases,
    btc_bias,
    exchange=None,
    bitunix=False
):
    bases = sorted(set(markets.keys()) & bybit_bases)

    if not bases:
        logging.warning("%s: no matching Bybit contracts", name)
        return

    cursor = int(state["cursors"].get(name, 0)) % len(bases)

    count = min(BATCH_SIZE, len(bases))

    batch = [
        bases[(cursor + i) % len(bases)]
        for i in range(count)
    ]

    state["cursors"][name] = (cursor + count) % len(bases)

    logging.info(
        "%s: %d eligible markets, checking %d",
        name, len(bases), len(batch)
    )

    for base in batch:
        try:
            if bitunix:
                candles = bitunix_candles(markets[base])
                signal = analyze_ict(
                    candles,
                    already_closed=True
                )
            else:
                candles = exchange.fetch_ohlcv(
                    markets[base],
                    timeframe=TIMEFRAME,
                    limit=100
                )
                signal = analyze_ict(candles)

            if not signal:
                continue

            # HTF BTC direction adjusts the score.
            if (
                btc_bias == "BULLISH"
                and signal["direction"] == "SHORT"
            ):
                signal["score"] -= 8

            elif (
                btc_bias == "BEARISH"
                and signal["direction"] == "LONG"
            ):
                signal["score"] -= 8

            if signal["score"] < MIN_SCORE:
                continue

            # One notification per coin/direction/candle,
            # even when several exchanges see the same setup.
            key = (
                f"{base}:{signal['direction']}:"
                f"{signal['timestamp']}"
            )

            if key in state["sent"]:
                continue

            message = make_message(
                base, name, signal, btc_bias
            )

            # Do not record a signal until Telegram confirms.
            telegram(message)

            state["sent"].append(key)
            save_state(state)

            logging.info("Signal sent: %s", key)

        except Exception as error:
            logging.warning(
                "%s / %s: %s",
                name, base, error
            )

        time.sleep(0.15)


# ---------------- MAIN ----------------

def main():
    state = load_state()

    # Quiet hours still preserve scanner progress.
    if is_quiet():
        logging.info("Quiet hours in Istanbul. No notifications.")
        return

    bybit = make_exchange("bybit")

    try:
        bybit_markets = ccxt_markets(bybit)
        bybit_bases = set(bybit_markets)

        logging.info(
            "Bybit USDT perpetual universe: %d",
            len(bybit_bases)
        )

        btc_bias = get_htf_bias(bybit)

        logging.info("BTC 1H bias: %s", btc_bias)

        for name in EXCHANGES:
            exchange = None

            try:
                exchange = (
                    bybit if name == "bybit"
                    else make_exchange(name)
                )

                markets = ccxt_markets(exchange)

                scan_source(
                    name.upper(),
                    markets,
                    state,
                    bybit_bases,
                    btc_bias,
                    exchange=exchange
                )

                save_state(state)

            except Exception:
                logging.exception("%s failed", name)

            finally:
                if exchange is not None and exchange is not bybit:
                    exchange.close()

        # Dedicated Bitunix public API adapter.
        try:
            markets = bitunix_markets()

            scan_source(
                "BITUNIX",
                markets,
                state,
                bybit_bases,
                btc_bias,
                bitunix=True
            )

        except Exception:
            logging.exception("Bitunix failed")

        save_state(state)

    finally:
        bybit.close()
        http.close()


if __name__ == "__main__":
    main()
  

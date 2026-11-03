
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

# =====================================================
# ICT FUTURES SCANNER PRO
# Exchanges: OKX / BingX / Bitunix
# Markets: USDT linear perpetual futures ONLY
# No spot. No Bybit. No automatic trading.
# =====================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TIMEZONE = ZoneInfo("Europe/Istanbul")

TIMEFRAME = "15m"
HTF_TIMEFRAME = "1h"

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "40"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "72"))

STATE_FILE = Path("state.json")
BITUNIX_URL = "https://fapi.bitunix.com"

EXCHANGES = ("okx", "bingx")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

http = requests.Session()
http.headers.update({
    "User-Agent": "ICT-Futures-Scanner/2.0"
})


# =====================================================
# TIME
# =====================================================

def is_quiet_hours():
    hour = datetime.now(TIMEZONE).hour
    return hour < 6


# =====================================================
# STATE
# =====================================================

def load_state():
    try:
        data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        return {
            "cursors": data.get("cursors", {}),
            "sent": data.get("sent", [])
        }

    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "cursors": {},
            "sent": []
        }


def save_state(state):
    state["sent"] = state["sent"][-5000:]

    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    fd, temp_name = tempfile.mkstemp(
        dir=str(STATE_FILE.parent),
        prefix="state_"
    )

    try:
        with os.fdopen(
            fd, "w", encoding="utf-8"
        ) as file:
            json.dump(
                state,
                file,
                ensure_ascii=False,
                indent=2
            )
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp_name, STATE_FILE)

    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


# =====================================================
# TELEGRAM
# =====================================================

def send_telegram(message):
    response = http.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )


# =====================================================
# CCXT EXCHANGES
# =====================================================

def create_exchange(exchange_name):
    exchange_class = getattr(ccxt, exchange_name)

    return exchange_class({
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {
            "defaultType": "swap"
        }
    })


def get_futures_markets(exchange):
    """
    Return ONLY active USDT linear perpetual markets.
    Spot, inverse futures and dated futures are excluded.
    """

    all_markets = exchange.load_markets()
    futures = {}

    for market in all_markets.values():

        if market.get("active") is False:
            continue

        if market.get("swap") is not True:
            continue

        if market.get("linear") is not True:
            continue

        if market.get("quote") != "USDT":
            continue

        if market.get("settle") != "USDT":
            continue

        if not market.get("base") or not market.get("symbol"):
            continue

        futures[market["base"]] = market["symbol"]

    return futures


# =====================================================
# BITUNIX PUBLIC FUTURES API
# =====================================================

def bitunix_get(path, params=None):

    response = http.get(
        BITUNIX_URL + path,
        params=params or {},
        timeout=25
    )

    response.raise_for_status()

    result = response.json()

    if result.get("code") != 0:
        raise RuntimeError(
            f"Bitunix API error: {result}"
        )

    return result.get("data", [])


def get_bitunix_futures_markets():

    rows = bitunix_get(
        "/api/v1/futures/market/trading_pairs"
    )

    markets = {}

    for row in rows:

        if row.get("quote") != "USDT":
            continue

        if row.get("symbolStatus") != "OPEN":
            continue

        base = row.get("base")
        symbol = row.get("symbol")

        if base and symbol:
            markets[base] = symbol

    return markets


def get_bitunix_candles(symbol):

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

    candles.sort(key=lambda candle: candle[0])

    # Official Bitunix endpoint returns closed candles.
    return candles


# =====================================================
# ATR
# =====================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return 0

    ranges = []

    for i in range(-period, 0):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current[2] - current[3],
            abs(current[2] - previous[4]),
            abs(current[3] - previous[4])
        )

        ranges.append(tr)

    return sum(ranges) / len(ranges)


# =====================================================
# BTC MARKET BIAS
# =====================================================

def get_btc_bias(exchange):

    try:

        symbol = "BTC/USDT:USDT"

        candles = exchange.fetch_ohlcv(
            symbol,
            timeframe=HTF_TIMEFRAME,
            limit=50
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
        logging.exception(
            "BTC bias unavailable"
        )

    return "NEUTRAL"


# =====================================================
# ICT SIGNAL ENGINE
# =====================================================

def analyze_ict(candles, already_closed=False):

    if not candles or len(candles) < 35:
        return None

    # Exclude currently forming candle on CCXT.
    # Bitunix returns closed candles already.
    data = (
        candles
        if already_closed
        else candles[:-1]
    )

    if len(data) < 30:
        return None

    current = data[-1]

    timestamp = current[0]
    open_price = current[1]
    high = current[2]
    low = current[3]
    close = current[4]
    volume = current[5]

    lookback = data[-22:-2]

    range_high = max(x[2] for x in lookback)
    range_low = min(x[3] for x in lookback)

    avg_body = sum(
        abs(x[4] - x[1])
        for x in lookback
    ) / len(lookback)

    avg_volume = sum(
        x[5] for x in lookback
    ) / len(lookback)

    atr = calculate_atr(data)

    if atr <= 0 or avg_body <= 0:
        return None

    body = abs(close - open_price)

    candle_range = max(
        high - low,
        1e-12
    )

    # 1. Liquidity Sweep
    bull_sweep = (
        low < range_low
        and close > range_low
    )

    bear_sweep = (
        high > range_high
        and close < range_high
    )

    # 2. Displacement
    displacement = body >= avg_body * 1.35

    # 3. Fair Value Gap
    bull_fvg = low > data[-3][2]
    bear_fvg = high < data[-3][3]

    # 4. Market Structure Shift
    local_high = max(
        x[2] for x in data[-7:-1]
    )

    local_low = min(
        x[3] for x in data[-7:-1]
    )

    bull_mss = close > local_high
    bear_mss = close < local_low

    # 5. Candle close strength
    bull_close = (
        close > open_price
        and (high - close) / candle_range < 0.30
    )

    bear_close = (
        close < open_price
        and (close - low) / candle_range < 0.30
    )

    # 6. Volume
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

    # Heuristic score, NOT win probability.
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
        "timestamp": int(timestamp),
        "volume_ok": volume_ok,
        "atr": atr
    }


# =====================================================
# SIGNAL MESSAGE
# =====================================================

def fmt(value):
    return f"{value:.8g}"


def create_signal_message(
    base,
    exchange_name,
    signal,
    btc_bias
):

    direction = signal["direction"]

    emoji = (
        "🟢" if direction == "LONG"
        else "🔴"
    )

    signal_time = datetime.fromtimestamp(
        signal["timestamp"] / 1000,
        TIMEZONE
    ).strftime("%d.%m.%Y %H:%M")

    return (
        f"⚡ ICT FUTURES PRO\n\n"
        f"{emoji} {direction} | {base}/USDT\n"
        f"Exchange: {exchange_name}\n"
        f"Market: USDT PERPETUAL\n"
        f"Timeframe: {TIMEFRAME}\n"
        f"BTC 1H Bias: {btc_bias}\n\n"
        f"⭐ ICT SCORE: {signal['score']}/100\n\n"
        f"ENTRY: {fmt(signal['entry'])}\n"
        f"STOP LOSS: {fmt(signal['sl'])}\n\n"
        f"TP1: {fmt(signal['targets'][0])}\n"
        f"TP2: {fmt(signal['targets'][1])}\n"
        f"TP3: {fmt(signal['targets'][2])}\n\n"
        f"Risk/Reward: 1:2 / 1:3 / 1:4\n"
        f"Volume: "
        f"{'CONFIRMED' if signal['volume_ok'] else 'WEAK'}\n\n"
        f"ICT CONDITIONS:\n"
        f"✓ Liquidity Sweep\n"
        f"✓ Displacement\n"
        f"✓ Fair Value Gap\n"
        f"✓ Market Structure Shift\n\n"
        f"Closed candle: {signal_time}\n"
        f"Time zone: Istanbul\n\n"
        f"⚠️ Analytical signal. "
        f"Not a guarantee of profit."
    )


# =====================================================
# ROTATING MARKET SCANNER
# =====================================================

def scan_exchange(
    exchange_name,
    markets,
    state,
    btc_bias,
    exchange=None,
    bitunix=False
):

    bases = sorted(markets.keys())

    if not bases:
        logging.warning(
            "%s: no eligible futures",
            exchange_name
        )
        return

    cursor = int(
        state["cursors"].get(exchange_name, 0)
    ) % len(bases)

    count = min(BATCH_SIZE, len(bases))

    batch = [
        bases[(cursor + i) % len(bases)]
        for i in range(count)
    ]

    state["cursors"][exchange_name] = (
        (cursor + count) % len(bases)
    )

    logging.info(
        "%s: %d futures, scanning %d",
        exchange_name,
        len(bases),
        len(batch)
    )

    for base in batch:

        try:

            if bitunix:

                candles = get_bitunix_candles(
                    markets[base]
                )

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

            if signal is None:
                continue

            # BTC bias is a soft filter.
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

            # Deduplication by coin, direction and candle.
            key = (
                f"{base}:"
                f"{signal['direction']}:"
                f"{signal['timestamp']}"
            )

            if key in state["sent"]:
                continue

            message = create_signal_message(
                base,
                exchange_name,
                signal,
                btc_bias
            )

            send_telegram(message)

            state["sent"].append(key)

            save_state(state)

            logging.info(
                "Telegram signal sent: %s",
                key
            )

        except Exception as error:

            logging.warning(
                "%s %s failed: %s",
                exchange_name,
                base,
                error
            )

        # Respect public API rate limits.
        time.sleep(0.20)


# =====================================================
# MAIN
# =====================================================

def main():

    state = load_state()

    if is_quiet_hours():

        logging.info(
            "Quiet hours: Telegram notifications disabled."
        )

        return

    # OKX provides the BTC reference trend.
    okx = None

    try:

        okx = create_exchange("okx")

        # Load only the exchange's available market metadata.
        okx_markets = get_futures_markets(okx)

        btc_bias = get_btc_bias(okx)

        logging.info(
            "OKX futures: %d",
            len(okx_markets)
        )

        logging.info(
            "BTC 1H bias: %s",
            btc_bias
        )

        # OKX
        scan_exchange(
            "OKX",
            okx_markets,
            state,
            btc_bias,
            exchange=okx
        )

        save_state(state)

    except Exception:

        logging.exception(
            "OKX scanner failed"
        )

        btc_bias = "NEUTRAL"

    # BingX
    bingx = None

    try:

        bingx = create_exchange("bingx")

        bingx_markets = get_futures_markets(bingx)

        logging.info(
            "BingX futures: %d",
            len(bingx_markets)
        )

        scan_exchange(
            "BINGX",
            bingx_markets,
            state,
            btc_bias,
            exchange=bingx
        )

        save_state(state)

    except Exception:

        logging.exception(
            "BingX scanner failed"
        )

    finally:

        if bingx is not None:
            bingx.close()

    # Bitunix
    try:

        bitunix_markets = get_bitunix_futures_markets()

        logging.info(
            "Bitunix futures: %d",
            len(bitunix_markets)
        )

        scan_exchange(
            "BITUNIX",
            bitunix_markets,
            state,
            btc_bias,
            bitunix=True
        )

        save_state(state)

    except Exception:

        logging.exception(
            "Bitunix scanner failed"
        )

    finally:

        if okx is not None:
            okx.close()

        http.close()


if __name__ == "__main__":
    main()

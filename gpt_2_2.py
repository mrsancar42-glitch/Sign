
import os
import json
import time
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import ccxt
import pandas as pd
import numpy as np

# ============================================================
# GPT 2.2 | QUANT INTELLIGENCE
# BingX + OKX | USDT Perpetual Futures
# Analysis only - no order execution
# ============================================================

VERSION = "2.2"
TZ = ZoneInfo("Europe/Istanbul")
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MIN_SCORE = int(os.getenv("MIN_SCORE", "78"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "60"))
MAX_DAILY_SIGNALS = int(os.getenv("MAX_DAILY_SIGNALS", "12"))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "2000000"))
SCAN_TIMEFRAME = "15m"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("GPT22")


def now():
    return datetime.now(TZ)


def quiet_hours():
    t = now()
    return t.hour < 7 or t.hour >= 24


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "signals": {},
            "daily": {},
            "news_hashes": [],
            "last_run": None
        }


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=20
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram error: %s", e)
        return False


def make_exchange(exchange_id):
    cls = getattr(ccxt, exchange_id)
    return cls({
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {"defaultType": "swap"}
    })


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev).abs(),
        (low - prev).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def prepare(rows):
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    if len(df) > 1:
        df = df.iloc[:-1].copy()  # closed candles only
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)
    df["rsi"] = rsi(df["close"])
    df["atr"] = atr(df)
    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, np.nan)
    df["macd"] = ema(df["close"], 12) - ema(df["close"], 26)
    df["macd_signal"] = ema(df["macd"], 9)
    df["high20"] = df["high"].shift(1).rolling(20).max()
    df["low20"] = df["low"].shift(1).rolling(20).min()
    return df.dropna()


def fetch_df(exchange, symbol, timeframe, limit=260):
    rows = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return prepare(rows)


def btc_regime(exchange):
    try:
        df = fetch_df(exchange, "BTC/USDT:USDT", "1h", 260)
        x = df.iloc[-1]
        if x.close > x.ema50 > x.ema200:
            return "BULLISH"
        if x.close < x.ema50 < x.ema200:
            return "BEARISH"
        return "NEUTRAL"
    except Exception as e:
        log.warning("BTC regime error: %s", e)
        return "UNKNOWN"


def market_symbols(exchange):
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()
    candidates = []

    for symbol, market in markets.items():
        if not market.get("active", True):
            continue
        if not market.get("swap") or not market.get("linear"):
            continue
        if market.get("quote") != "USDT":
            continue
        if market.get("settle") != "USDT":
            continue
        if not symbol.endswith(":USDT"):
            continue

        ticker = tickers.get(symbol, {})
        volume = ticker.get("quoteVolume") or 0
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            volume = 0

        if volume >= MIN_QUOTE_VOLUME:
            candidates.append((symbol, volume))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in candidates[:MAX_SYMBOLS]]


def get_news():
    feeds = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://cryptonews.com/news/feed/"
    ]
    items = []
    for url in feeds:
        try:
            response = requests.get(
                url,
                timeout=12,
                headers={"User-Agent": "GPT22-Research/1.0"}
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            for item in root.findall(".//item")[:20]:
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                if title:
                    items.append({"title": title.strip(), "link": link})
        except Exception as e:
            log.warning("News feed error: %s", e)
    return items


def news_analysis(news):
    negative = [
        "hack", "hacked", "exploit", "lawsuit", "ban", "crash",
        "liquidation", "bankruptcy", "outflow", "breach", "fraud",
        "санкц", "взлом", "запрет", "обвал"
    ]
    positive = [
        "approval", "approved", "inflow", "adoption", "partnership",
        "etf", "launch", "upgrade", "record high", "institutional",
        "одобр", "приток", "институцион"
    ]
    score = 0
    relevant = []
    for item in news:
        title = item["title"]
        low = title.lower()
        if any(k in low for k in negative):
            score -= 1
            relevant.append(item)
        elif any(k in low for k in positive):
            score += 1
            relevant.append(item)

    if score >= 2:
        mood = "POSITIVE"
    elif score <= -2:
        mood = "NEGATIVE"
    else:
        mood = "NEUTRAL"
    return mood, relevant[:5]


def funding_adjustment(exchange, symbol, side):
    try:
        if not exchange.has.get("fetchFundingRate"):
            return 0
        rate = exchange.fetch_funding_rate(symbol).get("fundingRate")
        if rate is None:
            return 0
        rate = float(rate)
        # Extremely crowded side receives a small penalty.
        if side == "LONG" and rate > 0.001:
            return -4
        if side == "SHORT" and rate < -0.001:
            return -4
    except Exception:
        pass
    return 0


def score_setup(df15, df1h, df4h, btc, news_mood, side):
    a = df15.iloc[-1]
    b = df1h.iloc[-1]
    c = df4h.iloc[-1]
    score = 0
    reasons = []

    if side == "LONG":
        if b.close > b.ema50 > b.ema200:
            score += 15
            reasons.append("1H bullish trend")
        if c.close > c.ema50:
            score += 10
            reasons.append("4H trend confirmation")
        if a.close > a.ema20 > a.ema50:
            score += 10
            reasons.append("15m EMA alignment")
        if 48 <= a.rsi <= 69:
            score += 10
            reasons.append("RSI momentum")
        if a.macd > a.macd_signal:
            score += 8
            reasons.append("MACD bullish")
        if a.vol_ratio >= 1.2:
            score += 10
            reasons.append("Volume expansion")
        if a.close > a.high20:
            score += 12
            reasons.append("Range breakout")
        elif a.low < a.low20 and a.close > a.low20:
            score += 12
            reasons.append("Liquidity sweep recovery")
        if btc == "BULLISH":
            score += 15
            reasons.append("BTC bullish regime")
        elif btc == "BEARISH":
            score -= 12
        if news_mood == "POSITIVE":
            score += 5
        elif news_mood == "NEGATIVE":
            score -= 8

    else:
        if b.close < b.ema50 < b.ema200:
            score += 15
            reasons.append("1H bearish trend")
        if c.close < c.ema50:
            score += 10
            reasons.append("4H trend confirmation")
        if a.close < a.ema20 < a.ema50:
            score += 10
            reasons.append("15m EMA alignment")
        if 31 <= a.rsi <= 52:
            score += 10
            reasons.append("RSI bearish momentum")
        if a.macd < a.macd_signal:
            score += 8
            reasons.append("MACD bearish")
        if a.vol_ratio >= 1.2:
            score += 10
            reasons.append("Volume expansion")
        if a.close < a.low20:
            score += 12
            reasons.append("Range breakdown")
        elif a.high > a.high20 and a.close < a.high20:
            score += 12
            reasons.append("Liquidity sweep rejection")
        if btc == "BEARISH":
            score += 15
            reasons.append("BTC bearish regime")
        elif btc == "BULLISH":
            score -= 12
        if news_mood == "NEGATIVE":
            score += 5
        elif news_mood == "POSITIVE":
            score -= 8

    return max(0, min(100, score)), reasons


def create_signal(exchange, symbol, side, score, reasons, df):
    x = df.iloc[-1]
    entry = float(x.close)
    volatility = float(x.atr)

    if not np.isfinite(volatility) or volatility <= 0:
        return None

    risk = max(volatility * 1.5, entry * 0.003)

    if side == "LONG":
        stop = entry - risk
        direction = 1
    else:
        stop = entry + risk
        direction = -1

    return {
        "id": f"{exchange.id}:{symbol}:{side}:{int(x.timestamp)}",
        "exchange": exchange.id.upper(),
        "symbol": symbol,
        "side": side,
        "score": int(score),
        "entry": entry,
        "stop": stop,
        "risk": risk,
        "tp1": entry + direction * risk * 1.5,
        "tp2": entry + direction * risk * 2.5,
        "tp3": entry + direction * risk * 3.5,
        "reasons": reasons,
        "created": now().isoformat(),
        "status": "OPEN",
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "stop_hit": False
    }


def fmt(value):
    return f"{value:.8g}"


def signal_message(s):
    return (
        f"🤖 GPT 2.2 | QUANT INTELLIGENCE\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{'🟢' if s['side'] == 'LONG' else '🔴'} "
        f"{s['side']} — {s['symbol']}\n"
        f"Биржа: {s['exchange']}\n"
        f"Таймфрейм: 15m / 1h / 4h\n\n"
        f"Оценка модели: {s['score']}/100\n"
        f"ENTRY: {fmt(s['entry'])}\n"
        f"STOP LOSS: {fmt(s['stop'])}\n"
        f"Риск на единицу: {fmt(s['risk'])}\n\n"
        f"TP1: {fmt(s['tp1'])} (1.5R)\n"
        f"TP2: {fmt(s['tp2'])} (2.5R)\n"
        f"TP3: {fmt(s['tp3'])} (3.5R)\n\n"
        f"Подтверждения:\n"
        + "\n".join(f"• {r}" for r in s["reasons"])
        + "\n\nАналитический сигнал. Не финансовая рекомендация."
    )


def monitor_signals(state, exchanges):
    for signal in state["signals"].values():
        if signal["status"] != "OPEN":
            continue
        try:
            exchange = exchanges[signal["exchange"].lower()]
            ticker = exchange.fetch_ticker(signal["symbol"])
            price = float(ticker["last"])
            long_side = signal["side"] == "LONG"

            def crossed(target):
                return price >= target if long_side else price <= target

            def stop_crossed():
                return price <= signal["stop"] if long_side else price >= signal["stop"]

            # Conservative ordering: stop is checked before new targets.
            if stop_crossed():
                signal["status"] = "STOPPED"
                signal["stop_hit"] = True
                telegram(
                    f"🛑 GPT 2.2 | STOP LOSS\n"
                    f"{signal['symbol']} | {signal['side']}\n"
                    f"Биржа: {signal['exchange']}\n"
                    f"Текущая цена: {fmt(price)}"
                )
                continue

            for number in (1, 2, 3):
                key = f"tp{number}"
                hit_key = f"tp{number}_hit"
                if not signal[hit_key] and crossed(signal[key]):
                    signal[hit_key] = True
                    telegram(
                        f"🎯 GPT 2.2 | TP{number} HIT\n"
                        f"{signal['symbol']} | {signal['side']}\n"
                        f"Цена: {fmt(price)}"
                    )
                    if number == 1:
                        # Protect remaining position at breakeven.
                        signal["stop"] = signal["entry"]

            if signal["tp3_hit"]:
                signal["status"] = "COMPLETED"

        except Exception as e:
            log.warning("Monitor error %s: %s", signal["symbol"], e)


def scan_exchange(exchange, state, btc, news_mood):
    log.info("Scanning %s", exchange.id)
    try:
        symbols = market_symbols(exchange)
    except Exception as e:
        log.error("Market loading failed: %s", e)
        return

    daily_key = now().strftime("%Y-%m-%d")
    daily_count = state["daily"].get(daily_key, 0)

    for symbol in symbols:
        if daily_count >= MAX_DAILY_SIGNALS:
            break
        try:
            df15 = fetch_df(exchange, symbol, "15m")
            df1h = fetch_df(exchange, symbol, "1h")
            df4h = fetch_df(exchange, symbol, "4h")

            if min(len(df15), len(df1h), len(df4h)) < 205:
                continue

            for side in ("LONG", "SHORT"):
                score, reasons = score_setup(
                    df15, df1h, df4h, btc, news_mood, side
                )
                score += funding_adjustment(exchange, symbol, side)
                score = max(0, min(100, score))

                if score < MIN_SCORE:
                    continue

                signal = create_signal(
                    exchange, symbol, side, score, reasons, df15
                )
                if not signal:
                    continue

                # Prevent duplicate signals and simultaneous opposite signals.
                duplicate = any(
                    x["symbol"] == symbol
                    and x["exchange"] == exchange.id.upper()
                    and x["status"] == "OPEN"
                    for x in state["signals"].values()
                )
                if duplicate or signal["id"] in state["signals"]:
                    continue

                if telegram(signal_message(signal)):
                    state["signals"][signal["id"]] = signal
                    daily_count += 1
                    state["daily"][daily_key] = daily_count
                    save_state(state)
                    log.info("Signal sent: %s %s %s", symbol, side, score)

        except Exception as e:
            log.warning("Symbol scan failed %s: %s", symbol, e)


def send_news(news, state):
    hashes = set(state.get("news_hashes", []))
    for item in news:
        key = str(abs(hash(item["title"])))
        if key in hashes:
            continue
        title = item["title"]
        if any(x in title.lower() for x in [
            "bitcoin", "btc", "ethereum", "crypto", "cryptocurrency",
            "etf", "sec", "exchange", "бтк", "биткоин", "крипто"
        ]):
            telegram(
                f"📰 CRYPTO MARKET NEWS\n\n{title}\n\n{item['link']}"
            )
            hashes.add(key)

    state["news_hashes"] = list(hashes)[-500:]


def main():
    if quiet_hours():
        log.info("Night pause: Istanbul 00:00-07:00")
        return

    state = load_state()

    exchanges = {}
    for name in ("bingx", "okx"):
        try:
            exchanges[name] = make_exchange(name)
        except Exception as e:
            log.error("Exchange init %s: %s", name, e)

    if not exchanges:
        raise RuntimeError("No exchanges available")

    # Manage existing signals before scanning new ones.
    monitor_signals(state, exchanges)

    news = get_news()
    mood, relevant = news_analysis(news)
    send_news(news, state)

    for exchange in exchanges.values():
        btc = btc_regime(exchange)
        scan_exchange(exchange, state, btc, mood)

    state["last_run"] = now().isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
  

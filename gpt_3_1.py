
import os
import json
import time
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import ccxt
import requests
import pandas as pd
import numpy as np

# ================= CONFIG =================

TZ = ZoneInfo("Europe/Istanbul")
STATE_FILE = "state.json"

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MIN_SCORE = int(os.getenv("MIN_SCORE", "78"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "40"))
MAX_DAILY = int(os.getenv("MAX_DAILY_SIGNALS", "8"))
MIN_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "2000000"))

EXCHANGES = ("bingx", "okx")
NEWS_URLS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://cryptonews.com/news/feed/"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("GPT31")


# ================= STATE =================

def now():
    return datetime.now(TZ)


def load_state():
    default = {
        "signals": {},
        "daily": {},
        "news_hashes": [],
        "last_run": None
    }
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        for k, v in default.items():
            if not isinstance(data.get(k), type(v)):
                data[k] = v
        data["signals"] = {
            k: v for k, v in data["signals"].items()
            if isinstance(v, dict) and "status" in v
        }
        return data
    except (OSError, ValueError, TypeError):
        return default


def save_state(state):
    with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


# ================= TELEGRAM =================

def telegram(message):
    if not TOKEN or not CHAT_ID:
        log.warning("Telegram credentials missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=20
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram: %s", e)
        return False


# ================= EXCHANGES =================

def make_exchange(name):
    return getattr(ccxt, name)({
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {"defaultType": "swap"}
    })


# ================= INDICATORS =================

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def prepare(rows):
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    if len(df) > 1:
        df = df.iloc[:-1].copy()

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["ema20"] = ema(df.close, 20)
    df["ema50"] = ema(df.close, 50)
    df["ema200"] = ema(df.close, 200)

    delta = df.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)

    tr = pd.concat([
        df.high - df.low,
        (df.high - df.close.shift()).abs(),
        (df.low - df.close.shift()).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    df["vol_avg"] = df.volume.rolling(20).mean()
    df["vol_ratio"] = df.volume / df.vol_avg.replace(0, np.nan)

    df["macd"] = ema(df.close, 12) - ema(df.close, 26)
    df["macd_signal"] = ema(df.macd, 9)

    df["high20"] = df.high.shift().rolling(20).max()
    df["low20"] = df.low.shift().rolling(20).min()

    return df.replace([np.inf, -np.inf], np.nan).dropna()


def fetch_df(exchange, symbol, timeframe):
    return prepare(exchange.fetch_ohlcv(
        symbol, timeframe, limit=260
    ))


# ================= NEWS INTELLIGENCE =================

POSITIVE = [
    "approval", "approved", "partnership", "adoption",
    "inflow", "upgrade", "launch", "bullish",
    "одобр", "партнер", "приток", "запуск"
]

NEGATIVE = [
    "hack", "exploit", "lawsuit", "ban", "fraud",
    "bankruptcy", "crash", "outflow", "liquidation",
    "взлом", "суд", "запрет", "банкрот", "обвал"
]

def clean_text(value):
    return re.sub(r"<[^>]+>", " ", value or "").strip()


def get_news():
    news = []
    for url in NEWS_URLS:
        try:
            r = requests.get(url, timeout=15, headers={
                "User-Agent": "GPT31-Research/1.0"
            })
            r.raise_for_status()
            root = ET.fromstring(r.content)

            for item in root.findall(".//item")[:30]:
                title = clean_text(item.findtext("title"))
                desc = clean_text(
                    item.findtext("description")
                )
                link = item.findtext("link") or ""

                if title:
                    news.append({
                        "title": title,
                        "text": title + " " + desc,
                        "link": link,
                        "source": url
                    })
        except Exception as e:
            log.warning("News error: %s", e)

    return news


def news_for_asset(news, base):
    aliases = {
        "BTC": ["bitcoin", "btc"],
        "ETH": ["ethereum", "eth"],
        "BNB": ["binance", "bnb"],
        "SOL": ["solana", "sol"],
        "XRP": ["ripple", "xrp"]
    }

    terms = aliases.get(base.upper(), [base.lower()])
    score = 0
    relevant = []

    for item in news:
        text = item["text"].lower()

        if not any(term in text for term in terms):
            continue

        positive = sum(k in text for k in POSITIVE)
        negative = sum(k in text for k in NEGATIVE)

        if positive == 0 and negative == 0:
            continue

        score += positive - negative
        relevant.append(item["title"])

    mood = (
        "POSITIVE" if score > 0
        else "NEGATIVE" if score < 0
        else "NEUTRAL"
    )

    return mood, relevant[:3]


# ================= BTC REGIME =================

def btc_regime(exchange):
    try:
        df = fetch_df(exchange, "BTC/USDT:USDT", "1h")
        if df.empty:
            return "UNKNOWN"

        x = df.iloc[-1]

        if x.close > x.ema50 > x.ema200:
            return "BULLISH"
        if x.close < x.ema50 < x.ema200:
            return "BEARISH"

        return "NEUTRAL"
    except Exception as e:
        log.warning("BTC regime: %s", e)
        return "UNKNOWN"


# ================= MARKET STRUCTURE =================

def swings(df, width=3):
    highs, lows = [], []

    for i in range(width, len(df) - width):
        h = df.high.iloc[i]
        l = df.low.iloc[i]

        if h >= df.high.iloc[i-width:i+width+1].max():
            highs.append(float(h))

        if l <= df.low.iloc[i-width:i+width+1].min():
            lows.append(float(l))

    return sorted(set(highs)), sorted(set(lows))


def structure_levels(df15, df1h, side):
    if len(df15) < 40 or len(df1h) < 40:
        return None

    entry = float(df15.close.iloc[-1])
    volatility = float(df15.atr.iloc[-1])

    if not np.isfinite(volatility) or volatility <= 0:
        return None

    highs15, lows15 = swings(df15.iloc[:-1])
    highs1h, lows1h = swings(df1h.iloc[:-1])

    if side == "LONG":
        supports = [x for x in lows15 if x < entry]
        targets = [x for x in highs1h if x > entry]

        if not supports:
            return None

        stop = max(supports) - volatility * 0.2
        risk = entry - stop
        targets.sort()

    else:
        supports = [x for x in highs15 if x > entry]
        targets = [x for x in lows1h if x < entry]

        if not supports:
            return None

        stop = min(supports) + volatility * 0.2
        risk = stop - entry
        targets.sort(reverse=True)

    if risk <= 0 or risk / entry > 0.03:
        return None

    # Structural targets must satisfy minimum RR.
    valid = [
        x for x in targets
        if abs(x - entry) / risk >= 1.2
    ]

    if not valid:
        return None

    selected = valid[:3]

    # If fewer structural targets exist, extend only beyond
    # the last structural target using explicit RR projections.
    minimum_rr = [1.2, 2.0, 3.0]

    while len(selected) < 3:
        rr = minimum_rr[len(selected)]
        projected = (
            entry + risk * rr
            if side == "LONG"
            else entry - risk * rr
        )

        if selected:
            last = selected[-1]
            projected = (
                max(projected, last + risk * 0.1)
                if side == "LONG"
                else min(projected, last - risk * 0.1)
            )

        selected.append(projected)

    tp1, tp2, tp3 = selected

    rr = [abs(x - entry) / risk for x in selected]

    if rr[0] < 1.2 or rr[1] < 2 or rr[2] < 3:
        return None

    if side == "LONG":
        if not entry < tp1 < tp2 < tp3:
            return None
    else:
        if not entry > tp1 > tp2 > tp3:
            return None

    return {
        "entry": entry,
        "stop": stop,
        "risk": risk,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr1": rr[0],
        "rr2": rr[1],
        "rr3": rr[2],
        "structure": "SWING_HIGH_LOW"
    }


# ================= LIQUIDITY =================

def liquidity_check(df, side):
    x = df.iloc[-1]

    if side == "LONG":
        sweep = (
            x.low < x.low20 and x.close > x.low20
        )
        breakout = x.close > x.high20
    else:
        sweep = (
            x.high > x.high20 and x.close < x.high20
        )
        breakout = x.close < x.low20

    return bool(sweep), bool(breakout)


# ================= DERIVATIVES =================

def funding_score(exchange, symbol, side):
    try:
        if not exchange.has.get("fetchFundingRate"):
            return 0

        rate = exchange.fetch_funding_rate(symbol).get(
            "fundingRate"
        )

        if rate is None:
            return 0

        rate = float(rate)

        if side == "LONG" and rate > 0.001:
            return -4
        if side == "SHORT" and rate < -0.001:
            return -4

    except Exception:
        pass

    return 0


# ================= DECISION ENGINE =================

def analyze(df15, df1h, df4h, btc, mood, exchange, symbol, side):
    a = df15.iloc[-1]
    b = df1h.iloc[-1]
    c = df4h.iloc[-1]

    score = 0
    reasons = []

    if side == "LONG":
        checks = [
            (b.close > b.ema50 > b.ema200, 15, "1H bullish trend"),
            (c.close > c.ema50, 10, "4H confirmation"),
            (a.close > a.ema20 > a.ema50, 10, "15M alignment"),
            (48 <= a.rsi <= 69, 10, "RSI momentum"),
            (a.macd > a.macd_signal, 8, "MACD bullish"),
            (a.vol_ratio >= 1.2, 10, "Volume expansion"),
            (btc == "BULLISH", 15, "BTC bullish regime")
        ]
    else:
        checks = [
            (b.close < b.ema50 < b.ema200, 15, "1H bearish trend"),
            (c.close < c.ema50, 10, "4H confirmation"),
            (a.close < a.ema20 < a.ema50, 10, "15M alignment"),
            (31 <= a.rsi <= 52, 10, "RSI momentum"),
            (a.macd < a.macd_signal, 8, "MACD bearish"),
            (a.vol_ratio >= 1.2, 10, "Volume expansion"),
            (btc == "BEARISH", 15, "BTC bearish regime")
        ]

    for passed, points, reason in checks:
        if passed:
            score += points
            reasons.append(reason)

    sweep, breakout = liquidity_check(df15, side)

    if sweep:
        score += 12
        reasons.append("Liquidity sweep")

    elif breakout:
        score += 8
        reasons.append("Structure breakout")

    if mood == "POSITIVE":
        score += 5 if side == "LONG" else -8
        reasons.append("Positive news" if side == "LONG" else "News against SHORT")

    elif mood == "NEGATIVE":
        score += 5 if side == "SHORT" else -8
        reasons.append("Negative news" if side == "SHORT" else "News against LONG")

    score += funding_score(exchange, symbol, side)
    score = max(0, min(100, score))

    return score, reasons


# ================= SIGNAL =================

def fmt(value):
    return f"{value:.8g}"


def create_signal(exchange, symbol, side, score, reasons, levels):
    return {
        "id": f"{exchange.id}:{symbol}:{side}:{int(time.time())}",
        "exchange": exchange.id.upper(),
        "symbol": symbol,
        "side": side,
        "score": int(score),
        **levels,
        "reasons": reasons,
        "created": now().isoformat(),
        "status": "OPEN",
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "stop_hit": False
    }


def signal_message(s, mood, headlines):
    return (
        f"🤖 GPT 3.1 | QUANT INTELLIGENCE\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{'🟢' if s['side'] == 'LONG' else '🔴'} "
        f"{s['side']} — {s['symbol']}\n"
        f"Биржа: {s['exchange']}\n\n"
        f"Оценка: {s['score']}/100\n"
        f"Новости: {mood}\n"
        f"Структура: {s['structure']}\n\n"
        f"ENTRY: {fmt(s['entry'])}\n"
        f"STOP LOSS: {fmt(s['stop'])}\n\n"
        f"TP1: {fmt(s['tp1'])} (RR {s['rr1']:.2f})\n"
        f"TP2: {fmt(s['tp2'])} (RR {s['rr2']:.2f})\n"
        f"TP3: {fmt(s['tp3'])} (RR {s['rr3']:.2f})\n\n"
        f"ПОДТВЕРЖДЕНИЯ:\n"
        + "\n".join(f"• {x}" for x in s["reasons"])
        + (
            "\n\nНОВОСТИ:\n"
            + "\n".join(f"• {x}" for x in headlines)
            if headlines else ""
        )
        + "\n\nАналитический сигнал. Не финансовая рекомендация."
    )


# ================= SIGNAL MONITOR =================

def monitor_signals(state, exchanges):
    for signal in list(state["signals"].values()):
        if signal.get("status") != "OPEN":
            continue

        try:
            exchange = exchanges.get(signal["exchange"].lower())
            if exchange is None:
                continue

            price = float(
                exchange.fetch_ticker(signal["symbol"])["last"]
            )

            long = signal["side"] == "LONG"

            stopped = (
                price <= signal["stop"]
                if long else price >= signal["stop"]
            )

            if stopped:
                signal["status"] = "STOPPED"
                signal["stop_hit"] = True
                telegram(
                    f"🛑 GPT 3.1 | STOP LOSS\n"
                    f"{signal['symbol']} | {fmt(price)}"
                )
                continue

            for n in (1, 2, 3):
                key = f"tp{n}"
                hit = f"tp{n}_hit"

                crossed = (
                    price >= signal[key]
                    if long else price <= signal[key]
                )

                if not signal.get(hit) and crossed:
                    signal[hit] = True
                    telegram(
                        f"🎯 GPT 3.1 | TP{n} HIT\n"
                        f"{signal['symbol']} | {fmt(price)}"
                    )

                    if n == 1:
                        signal["stop"] = signal["entry"]

            if signal.get("tp3_hit"):
                signal["status"] = "COMPLETED"

        except Exception as e:
            log.warning("Monitor error: %s", e)


# ================= SCANNER =================

def market_symbols(exchange):
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()
    result = []

    for symbol, market in markets.items():
        if not (
            market.get("active", True)
            and market.get("swap")
            and market.get("linear")
            and market.get("quote") == "USDT"
            and market.get("settle") == "USDT"
            and symbol.endswith(":USDT")
        ):
            continue

        volume = tickers.get(symbol, {}).get("quoteVolume") or 0

        try:
            volume = float(volume)
        except (ValueError, TypeError):
            continue

        if volume >= MIN_VOLUME:
            result.append((symbol, volume))

    result.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in result[:MAX_SYMBOLS]]


def scan_exchange(exchange, state, news, btc):
    log.info("Scanning %s", exchange.id)

    try:
        symbols = market_symbols(exchange)
    except Exception as e:
        log.error("Markets error: %s", e)
        return

    day = now().strftime("%Y-%m-%d")
    count = state["daily"].get(day, 0)

    for symbol in symbols:
        if count >= MAX_DAILY:
            break

        try:
            df15 = fetch_df(exchange, symbol, "15m")
            df1h = fetch_df(exchange, symbol, "1h")
            df4h = fetch_df(exchange, symbol, "4h")

            if min(len(df15), len(df1h), len(df4h)) < 205:
                continue

            base = exchange.market(symbol).get("base", "")
            mood, headlines = news_for_asset(news, base)

            for side in ("LONG", "SHORT"):
                score, reasons = analyze(
                    df15, df1h, df4h, btc, mood,
                    exchange, symbol, side
                )

                if score < MIN_SCORE:
                    continue

                levels = structure_levels(df15, df1h, side)
                if not levels:
                    continue

                duplicate = any(
                    x.get("symbol") == symbol
                    and x.get("exchange") == exchange.id.upper()
                    and x.get("status") == "OPEN"
                    for x in state["signals"].values()
                )

                if duplicate:
                    continue

                signal = create_signal(
                    exchange, symbol, side, score, reasons, levels
                )

                if telegram(signal_message(signal, mood, headlines)):
                    state["signals"][signal["id"]] = signal
                    count += 1
                    state["daily"][day] = count
                    save_state(state)
                    log.info("Signal: %s %s %s", symbol, side, score)

        except Exception as e:
            log.warning("Scan error %s: %s", symbol, e)


# ================= MAIN =================

def main():
    if now().hour < 7:
        log.info("Night pause: Istanbul 00:00-07:00")
        return

    state = load_state()
    exchanges = {}

    for name in EXCHANGES:
        try:
            exchanges[name] = make_exchange(name)
        except Exception as e:
            log.error("%s init: %s", name, e)

    if not exchanges:
        raise RuntimeError("No exchanges available")

    monitor_signals(state, exchanges)

    news = get_news()
    log.info("Loaded %s news articles", len(news))

    for exchange in exchanges.values():
        btc = btc_regime(exchange)
        scan_exchange(exchange, state, news, btc)

    state["last_run"] = now().isoformat()
    save_state(state)
    log.info("GPT 3.1 finished")


if __name__ == "__main__":
    main()

import os
import json
import logging
import hashlib
import requests
import xml.etree.ElementTree as ET

from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import ccxt
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup


# ============================================================
# GPT 3.0 | QUANT INTELLIGENCE
# FREE NEWS INTELLIGENCE
# STRUCTURE BASED RISK ENGINE
# BINGX + OKX | USDT PERPETUAL
# ANALYSIS ONLY - NO ORDER EXECUTION
# ============================================================

VERSION = "3.0"

TZ = ZoneInfo("Europe/Istanbul")
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MIN_SCORE = int(os.getenv("MIN_SCORE", "78"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "60"))
MAX_DAILY_SIGNALS = int(os.getenv("MAX_DAILY_SIGNALS", "12"))

MIN_QUOTE_VOLUME = float(
    os.getenv("MIN_QUOTE_VOLUME", "2000000")
)

MIN_RR_TP1 = float(os.getenv("MIN_RR_TP1", "1.2"))
MIN_RR_TP2 = float(os.getenv("MIN_RR_TP2", "2.0"))
MIN_RR_TP3 = float(os.getenv("MIN_RR_TP3", "3.0"))

MAX_STOP_ATR = float(os.getenv("MAX_STOP_ATR", "3.0"))

NEWS_MAX_AGE_HOURS = 36
NEWS_ARTICLES_PER_FEED = 12
ARTICLE_TEXT_LIMIT = 12000

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 GPT30-Research/3.0"
}

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://cryptonews.com/news/feed/"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("GPT30")


# ============================================================
# NEWS DICTIONARY
# ============================================================

NEGATIVE_WORDS = [
    "hack", "hacked", "exploit", "exploited",
    "lawsuit", "ban", "banned", "crash",
    "bankruptcy", "fraud", "breach",
    "withdrawals suspended", "withdrawal suspension",
    "insolvency", "investigation", "delisting",
    "liquidation", "outflow", "attack",
    "санкц", "взлом", "запрет", "обвал",
    "банкротство", "мошенничество", "взломали"
]

POSITIVE_WORDS = [
    "approval", "approved", "adoption",
    "partnership", "inflow", "etf approval",
    "institutional investment", "integration",
    "mainnet launch", "upgrade completed",
    "funding round", "record inflow",
    "одобрение", "партнерство", "приток",
    "интеграция", "запуск сети"
]

UNCERTAIN_WORDS = [
    "rumor", "rumour", "unconfirmed",
    "reportedly", "allegedly", "speculation",
    "may launch", "could launch",
    "слух", "неподтвержден"
]

MACRO_WORDS = [
    "federal reserve", "fed", "interest rate",
    "inflation", "cpi", "ppi", "fomc",
    "treasury", "sec", "regulation",
    "tariff", "recession", "jobs report",
    "central bank", "dollar index"
]

CRYPTO_GENERAL_WORDS = [
    "bitcoin", "btc", "ethereum", "crypto",
    "cryptocurrency", "digital assets",
    "crypto market", "stablecoin"
]

ASSET_ALIASES = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "BNB": ["bnb", "binance coin"],
    "SOL": ["solana", "sol"],
    "XRP": ["ripple", "xrp"],
    "DOGE": ["dogecoin", "doge"],
    "ADA": ["cardano", "ada"],
    "AVAX": ["avalanche", "avax"],
    "LINK": ["chainlink", "link"],
    "DOT": ["polkadot", "dot"],
    "LTC": ["litecoin", "ltc"],
    "TRX": ["tron", "trx"],
    "TON": ["toncoin", "ton"],
    "SUI": ["sui"],
    "NEAR": ["near protocol", "near"],
    "APT": ["aptos", "apt"],
    "ARB": ["arbitrum", "arb"],
    "OP": ["optimism", "op"],
    "SHIB": ["shiba inu", "shib"],
    "UNI": ["uniswap", "uni"],
    "AAVE": ["aave"],
    "PEPE": ["pepe"],
    "FIL": ["filecoin", "fil"],
    "ATOM": ["cosmos", "atom"],
    "INJ": ["injective", "inj"],
    "RENDER": ["render network", "render"],
    "FET": ["fetch.ai", "fet"],
    "WIF": ["dogwifhat", "wif"],
}


# ============================================================
# TIME
# ============================================================

def now():
    return datetime.now(TZ)


def quiet_hours():
    return now().hour < 7


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "signals": {},
        "daily": {},
        "news_hashes": [],
        "last_run": None
    }


def load_state():

    default = default_state()

    try:

        if not os.path.exists(STATE_FILE):
            return default

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        if not isinstance(state, dict):
            return default

        for key, value in default.items():

            if (
                key not in state
                or not isinstance(state[key], type(value))
            ):
                state[key] = value

        state["signals"] = {
            k: v
            for k, v in state["signals"].items()
            if isinstance(v, dict)
            and "symbol" in v
            and "status" in v
        }

        return state

    except Exception as e:

        log.error("State error: %s", e)

        return default


def save_state(state):

    tmp = STATE_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(tmp, STATE_FILE)


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials missing")
        return False

    try:

        response = requests.post(
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
            return False

        return True

    except Exception as e:

        log.error("Telegram error: %s", e)

        return False


# ============================================================
# EXCHANGES
# ============================================================

def make_exchange(exchange_id):

    cls = getattr(ccxt, exchange_id)

    return cls({
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {
            "defaultType": "swap"
        }
    })


def market_symbols(exchange):

    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()

    candidates = []

    for symbol, market in markets.items():

        if not market.get("active", True):
            continue

        if not market.get("swap"):
            continue

        if not market.get("linear"):
            continue

        if market.get("quote") != "USDT":
            continue

        if market.get("settle") != "USDT":
            continue

        if not symbol.endswith(":USDT"):
            continue

        ticker = tickers.get(symbol, {})

        try:
            volume = float(
                ticker.get("quoteVolume") or 0
            )
        except (ValueError, TypeError):
            volume = 0

        if volume >= MIN_QUOTE_VOLUME:
            candidates.append((symbol, volume))

    candidates.sort(
        key=lambda x: x[1],
        reverse=True
    )

    return [
        item[0]
        for item in candidates[:MAX_SYMBOLS]
    ]


# ============================================================
# TECHNICAL INDICATORS
# ============================================================

def ema(series, period):

    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    return 100 - 100 / (1 + rs)


def atr(df, period=14):

    prev = df["close"].shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs()
    ], axis=1).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


def prepare(rows):

    df = pd.DataFrame(
        rows,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    # Ignore currently forming candle.
    if len(df) > 1:
        df = df.iloc[:-1].copy()

    for column in [
        "open", "high", "low",
        "close", "volume"
    ]:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)

    df["rsi"] = rsi(df["close"])
    df["atr"] = atr(df)

    df["vol_avg"] = df["volume"].rolling(20).mean()

    df["vol_ratio"] = (
        df["volume"] /
        df["vol_avg"].replace(0, np.nan)
    )

    df["macd"] = (
        ema(df["close"], 12) -
        ema(df["close"], 26)
    )

    df["macd_signal"] = ema(df["macd"], 9)

    return df.replace(
        [np.inf, -np.inf],
        np.nan
    ).dropna().reset_index(drop=True)


def fetch_df(exchange, symbol, timeframe, limit=260):

    rows = exchange.fetch_ohlcv(
        symbol,
        timeframe,
        limit=limit
    )

    return prepare(rows)


# ============================================================
# FREE NEWS INTELLIGENCE
# ============================================================

def fetch_article_text(url):

    try:

        response = requests.get(
            url,
            headers=REQUEST_HEADERS,
            timeout=12
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        for tag in soup([
            "script",
            "style",
            "nav",
            "footer",
            "header",
            "aside",
            "form"
        ]):
            tag.decompose()

        article = soup.find("article")

        if article:
            container = article
        else:
            container = soup

        paragraphs = [
            p.get_text(" ", strip=True)
            for p in container.find_all("p")
        ]

        text = " ".join(paragraphs)

        return text[:ARTICLE_TEXT_LIMIT]

    except Exception as e:

        log.debug(
            "Article unavailable %s: %s",
            url,
            e
        )

        return ""


def parse_date(value):

    if not value:
        return None

    try:

        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)

        return dt.astimezone(TZ)

    except Exception:
        pass

    try:

        dt = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)

        return dt.astimezone(TZ)

    except Exception:
        return None


def get_news():

    articles = []
    seen = set()

    for feed_url in FEEDS:

        try:

            response = requests.get(
                feed_url,
                headers=REQUEST_HEADERS,
                timeout=15
            )

            response.raise_for_status()

            root = ET.fromstring(response.content)

            entries = root.findall(".//item")

            if not entries:
                entries = root.findall(
                    ".//{http://www.w3.org/2005/Atom}entry"
                )

            for item in entries[:NEWS_ARTICLES_PER_FEED]:

                def find_text(name):

                    value = (
                        item.findtext(name)
                        or item.findtext(
                            "{http://www.w3.org/2005/Atom}" + name
                        )
                    )

                    if value:
                        return value.strip()

                    return ""

                title = find_text("title")
                link = find_text("link")

                if not link:
                    link_node = item.find(
                        "{http://www.w3.org/2005/Atom}link"
                    )

                    if link_node is not None:
                        link = link_node.attrib.get("href", "")

                description = (
                    find_text("description")
                    or find_text("summary")
                )

                published = (
                    find_text("pubDate")
                    or find_text("published")
                    or find_text("updated")
                )

                if not title or not link:
                    continue

                digest = hashlib.sha256(
                    link.encode("utf-8")
                ).hexdigest()

                if digest in seen:
                    continue

                seen.add(digest)

                published_at = parse_date(published)

                if published_at:

                    age = (
                        now() - published_at
                    ).total_seconds() / 3600

                    if age < -2 or age > NEWS_MAX_AGE_HOURS:
                        continue

                article_text = fetch_article_text(link)

                content = (
                    title + " " +
                    description + " " +
                    article_text
                ).lower()

                source = urlparse(feed_url).netloc

                articles.append({
                    "title": title,
                    "link": link,
                    "source": source,
                    "published": published,
                    "content": content,
                    "hash": digest
                })

        except Exception as e:

            log.warning(
                "RSS error %s: %s",
                feed_url,
                e
            )

    log.info(
        "News intelligence: %s articles loaded",
        len(articles)
    )

    return articles


def word_count(text, words):

    return sum(
        text.count(word)
        for word in words
    )


def analyze_article(article):

    text = article["content"]

    negative = word_count(
        text,
        NEGATIVE_WORDS
    )

    positive = word_count(
        text,
        POSITIVE_WORDS
    )

    uncertain = word_count(
        text,
        UNCERTAIN_WORDS
    )

    macro = word_count(
        text,
        MACRO_WORDS
    )

    # Limit influence of repeated words.
    raw = max(
        -4,
        min(4, positive - negative)
    )

    confidence = 1.0

    if uncertain:
        confidence *= 0.5

    if len(text) < 250:
        confidence *= 0.5

    if not article.get("published"):
        confidence *= 0.8

    impact = raw * confidence

    if impact >= 1:
        sentiment = "POSITIVE"

    elif impact <= -1:
        sentiment = "NEGATIVE"

    else:
        sentiment = "NEUTRAL"

    if negative >= 3 or positive >= 3:
        severity = "HIGH"

    elif negative or positive:
        severity = "MEDIUM"

    else:
        severity = "LOW"

    return {
        "sentiment": sentiment,
        "impact": impact,
        "confidence": confidence,
        "severity": severity,
        "macro": macro > 0,
        "uncertain": uncertain > 0
    }


def symbol_news(symbol, articles):

    base = symbol.split("/")[0].upper()

    aliases = ASSET_ALIASES.get(
        base,
        [base.lower()]
    )

    relevant = []

    for article in articles:

        text = article["content"]

        asset_match = any(
            alias.lower() in text
            for alias in aliases
        )

        general_match = any(
            word in text
            for word in CRYPTO_GENERAL_WORDS
        )

        macro_match = any(
            word in text
            for word in MACRO_WORDS
        )

        # Avoid applying every general crypto story
        # to every altcoin.
        if asset_match:

            relevant.append(article)

        elif base == "BTC" and (
            general_match or macro_match
        ):

            relevant.append(article)

        elif macro_match and general_match:

            relevant.append(article)

    if not relevant:

        return {
            "mood": "NEUTRAL",
            "score": 0,
            "summary": "No relevant verified news found",
            "articles": []
        }

    total = 0
    summaries = []
    strong_conflict = False

    for article in relevant[:8]:

        analysis = analyze_article(article)

        total += analysis["impact"]

        if analysis["uncertain"]:
            strong_conflict = True

        summaries.append({
            "title": article["title"],
            "link": article["link"],
            **analysis
        })

    total = max(-10, min(10, total))

    if total >= 1.5:
        mood = "POSITIVE"

    elif total <= -1.5:
        mood = "NEGATIVE"

    else:
        mood = "NEUTRAL"

    titles = [
        item["title"]
        for item in summaries[:2]
    ]

    summary = " | ".join(titles)

    return {
        "mood": mood,
        "score": total,
        "summary": summary[:450],
        "articles": summaries,
        "uncertain": strong_conflict
    }


# ============================================================
# BTC REGIME
# ============================================================

def btc_regime(exchange):

    try:

        df = fetch_df(
            exchange,
            "BTC/USDT:USDT",
            "1h"
        )

        if len(df) < 205:
            return "UNKNOWN"

        x = df.iloc[-1]

        if x.close > x.ema50 > x.ema200:
            return "BULLISH"

        if x.close < x.ema50 < x.ema200:
            return "BEARISH"

        return "NEUTRAL"

    except Exception as e:

        log.warning("BTC regime error: %s", e)

        return "UNKNOWN"


# ============================================================
# STRUCTURE BASED STOP LOSS / TAKE PROFIT
# ============================================================

def find_swing_points(df, left=3, right=3):
    highs = []
    lows = []

    for i in range(left, len(df) - right):
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]

        window_high = df["high"].iloc[i-left:i+right+1]
        window_low = df["low"].iloc[i-left:i+right+1]

        if high >= window_high.max():
            highs.append((i, float(high)))

        if low <= window_low.min():
            lows.append((i, float(low)))

    return highs, lows


def pick_targets(levels, entry, risk, minimums):
    """Pick TP1..TP3 in order, each meeting its own minimum RR."""

    picked = []
    index = 0

    for minimum in minimums:

        while index < len(levels):

            level = levels[index]
            index += 1

            if abs(level - entry) / risk >= minimum:
                picked.append(level)
                break

        else:
            return None

    return picked


def calculate_structure_levels(
    df,
    side,
    entry=None,
    atr_multiplier=0.25,
    min_rr_tp1=MIN_RR_TP1,
    min_rr_tp2=MIN_RR_TP2,
    min_rr_tp3=MIN_RR_TP3
):
    if df is None or len(df) < 30:
        return None

    last = df.iloc[-1]

    entry = float(entry if entry is not None else last["close"])
    volatility = float(last["atr"])

    if not np.isfinite(volatility) or volatility <= 0:
        return None

    highs, lows = find_swing_points(df.iloc[:-1])

    if side == "LONG":

        candidates = [
            price for _, price in lows
            if price < entry
        ]

        if not candidates:
            return None

        structural_low = max(candidates)

        stop = structural_low - volatility * atr_multiplier
        risk = entry - stop

        if risk <= 0 or risk > volatility * MAX_STOP_ATR:
            return None

        resistance = sorted(set(
            price for _, price in highs
            if price > entry
        ))

        targets = pick_targets(
            resistance,
            entry,
            risk,
            (min_rr_tp1, min_rr_tp2, min_rr_tp3)
        )

        if targets is None:
            return None

        tp1, tp2, tp3 = targets

    elif side == "SHORT":

        candidates = [
            price for _, price in highs
            if price > entry
        ]

        if not candidates:
            return None

        structural_high = min(candidates)

        stop = structural_high + volatility * atr_multiplier
        risk = stop - entry

        if risk <= 0 or risk > volatility * MAX_STOP_ATR:
            return None

        support = sorted(
            set(
                price for _, price in lows
                if price < entry
            ),
            reverse=True
        )

        targets = pick_targets(
            support,
            entry,
            risk,
            (min_rr_tp1, min_rr_tp2, min_rr_tp3)
        )

        if targets is None:
            return None

        tp1, tp2, tp3 = targets

    else:
        return None

    rr1 = abs(tp1 - entry) / risk
    rr2 = abs(tp2 - entry) / risk
    rr3 = abs(tp3 - entry) / risk

    return {
        "entry": float(entry),
        "stop": float(stop),
        "risk": float(risk),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "tp3": float(tp3),
        "rr1": float(rr1),
        "rr2": float(rr2),
        "rr3": float(rr3),
        "structure": "SWING_HIGH_LOW"
    }


# ============================================================
# STRUCTURE VALIDATION
# ============================================================

def validate_structure(levels, side):

    if not levels:
        return False

    entry = levels["entry"]
    stop = levels["stop"]

    if side == "LONG" and stop >= entry:
        return False

    if side == "SHORT" and stop <= entry:
        return False

    if levels["rr1"] < MIN_RR_TP1:
        return False

    if levels["rr2"] < MIN_RR_TP2:
        return False

    if levels["rr3"] < MIN_RR_TP3:
        return False

    if side == "LONG":
        return (
            entry < levels["tp1"]
            < levels["tp2"]
            < levels["tp3"]
        )

    if side == "SHORT":
        return (
            entry > levels["tp1"]
            > levels["tp2"]
            > levels["tp3"]
        )

    return False


# ============================================================
# SIGNAL CREATION
# ============================================================

def create_signal(exchange, symbol, side, score, reasons, df, df_levels=None):

    if df is None or df.empty:
        return None

    entry = float(df.iloc[-1]["close"])

    levels = calculate_structure_levels(
        df=df_levels if df_levels is not None else df,
        side=side,
        entry=entry
    )

    if not validate_structure(levels, side):
        return None

    return {
        "id": (
            f"{exchange.id}:{symbol}:{side}:"
            f"{int(df.iloc[-1]['timestamp'])}"
        ),
        "exchange": exchange.id.upper(),
        "symbol": symbol,
        "side": side,
        "score": int(score),
        "entry": levels["entry"],
        "stop": levels["stop"],
        "risk": levels["risk"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "tp3": levels["tp3"],
        "rr1": levels["rr1"],
        "rr2": levels["rr2"],
        "rr3": levels["rr3"],
        "structure": levels["structure"],
        "reasons": reasons,
        "created": now().isoformat(),
        "status": "OPEN",
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "stop_hit": False
    }


# ============================================================
# FORMATTING
# ============================================================

def fmt(value):
    return f"{float(value):.8g}"


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def signal_message(signal, news_context=None):

    side_emoji = (
        "🟢" if signal["side"] == "LONG" else "🔴"
    )

    reasons = "\n".join(
        f"• {reason}"
        for reason in signal["reasons"]
    )

    return (
        "🤖 GPT 3.0 | QUANT INTELLIGENCE\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{side_emoji} {signal['side']} — {signal['symbol']}\n"
        f"Биржа: {signal['exchange']}\n\n"
        f"Оценка: {signal['score']}/100\n"
        f"Структура: {signal['structure']}\n\n"
        f"ENTRY: {fmt(signal['entry'])}\n"
        f"STOP LOSS: {fmt(signal['stop'])}\n\n"
        f"TP1: {fmt(signal['tp1'])} (RR {signal['rr1']:.2f})\n"
        f"TP2: {fmt(signal['tp2'])} (RR {signal['rr2']:.2f})\n"
        f"TP3: {fmt(signal['tp3'])} (RR {signal['rr3']:.2f})\n\n"
        "ПОДТВЕРЖДЕНИЯ:\n"
        f"{reasons}\n\n"
        "Аналитический сигнал. Не финансовая рекомендация."
    )


# ============================================================
# SIGNAL MONITOR
# ============================================================

def monitor_signals(state, exchanges):

    signals = state.get("signals", {})

    if not isinstance(signals, dict):
        state["signals"] = {}
        return

    for signal in signals.values():

        if not isinstance(signal, dict):
            continue

        if signal.get("status") != "OPEN":
            continue

        try:
            exchange_name = signal["exchange"].lower()
            exchange = exchanges.get(exchange_name)

            if exchange is None:
                continue

            ticker = exchange.fetch_ticker(signal["symbol"])
            price = float(ticker["last"])

            if not np.isfinite(price) or price <= 0:
                continue

            long_side = signal["side"] == "LONG"

            stop_hit = (
                price <= signal["stop"]
                if long_side
                else price >= signal["stop"]
            )

            if stop_hit:

                signal["status"] = "STOPPED"
                signal["stop_hit"] = True

                telegram(
                    "🛑 GPT 3.0 | STOP LOSS\n"
                    f"{signal['symbol']}\n"
                    f"Цена: {fmt(price)}"
                )

                continue

            for number in (1, 2, 3):

                target_key = f"tp{number}"
                hit_key = f"tp{number}_hit"

                if signal.get(hit_key, False):
                    continue

                target = float(signal[target_key])

                reached = (
                    price >= target
                    if long_side
                    else price <= target
                )

                if reached:

                    signal[hit_key] = True

                    telegram(
                        f"🎯 GPT 3.0 | TP{number} HIT\n"
                        f"{signal['symbol']}\n"
                        f"Цена: {fmt(price)}"
                    )

                    if number == 1:
                        signal["stop"] = signal["entry"]

            if signal.get("tp3_hit"):
                signal["status"] = "COMPLETED"

        except Exception as error:

            log.warning(
                "Monitor error %s: %s",
                signal.get("symbol", "UNKNOWN"),
                error
            )


# ============================================================
# NEWS CONTEXT
# ============================================================

def get_signal_news_context(news_mood, relevant_news):

    if not relevant_news:
        return f"Общий новостной фон: {news_mood}"

    headlines = []

    for item in relevant_news[:3]:
        headlines.append(
            f"• {item.get('title', '')}"
        )

    return (
        f"Общий фон: {news_mood}\n"
        + "\n".join(headlines)
    )


# ============================================================
# NEWS ANALYSIS (overall market mood)
# ============================================================

def news_analysis(articles):

    result = symbol_news("BTC/USDT:USDT", articles)

    return result["mood"], result["articles"]


# ============================================================
# SETUP SCORING
# ============================================================

def score_setup(df15, df1h, df4h, btc, news_mood, side):

    long_side = side == "LONG"

    score = 0
    reasons = []

    x4 = df4h.iloc[-1]
    x1 = df1h.iloc[-1]
    x15 = df15.iloc[-1]
    p15 = df15.iloc[-2]

    # 4H trend (22)
    if long_side:
        full = x4.close > x4.ema50 > x4.ema200
        part = x4.close > x4.ema200
    else:
        full = x4.close < x4.ema50 < x4.ema200
        part = x4.close < x4.ema200

    if full:
        score += 22
        reasons.append("4H тренд подтверждает направление")
    elif part:
        score += 10
        reasons.append("4H цена по правильную сторону EMA200")

    # 1H trend (18)
    if long_side:
        full = x1.close > x1.ema50 and x1.ema20 > x1.ema50
        part = x1.close > x1.ema50
    else:
        full = x1.close < x1.ema50 and x1.ema20 < x1.ema50
        part = x1.close < x1.ema50

    if full:
        score += 18
        reasons.append("1H EMA20/EMA50 в направлении сделки")
    elif part:
        score += 8
        reasons.append("1H цена по правильную сторону EMA50")

    # 15M structure (12)
    if long_side:
        full = x15.close > x15.ema20 > x15.ema50
        part = x15.close > x15.ema50
    else:
        full = x15.close < x15.ema20 < x15.ema50
        part = x15.close < x15.ema50

    if full:
        score += 12
        reasons.append("15M цена и EMA выстроены по тренду")
    elif part:
        score += 5

    # RSI (10)
    rsi_value = float(x15.rsi)

    if long_side and 45 <= rsi_value <= 68:
        score += 10
        reasons.append(f"RSI 15M в рабочей зоне ({rsi_value:.0f})")
    elif not long_side and 32 <= rsi_value <= 55:
        score += 10
        reasons.append(f"RSI 15M в рабочей зоне ({rsi_value:.0f})")

    # MACD (10)
    hist = float(x15.macd - x15.macd_signal)
    prev_hist = float(p15.macd - p15.macd_signal)

    if not long_side:
        hist, prev_hist = -hist, -prev_hist

    if hist > 0 and hist > prev_hist:
        score += 10
        reasons.append("MACD 15M усиливает импульс")
    elif hist > 0:
        score += 5

    # Volume (10)
    volume_ratio = float(x15.vol_ratio)

    if volume_ratio >= 1.2:
        score += 10
        reasons.append(f"Объём выше среднего (x{volume_ratio:.1f})")
    elif volume_ratio >= 0.9:
        score += 4

    # BTC regime (10)
    wanted = "BULLISH" if long_side else "BEARISH"
    opposite = "BEARISH" if long_side else "BULLISH"

    if btc == wanted:
        score += 10
        reasons.append(f"Режим BTC поддерживает сделку ({btc})")
    elif btc == opposite:
        score -= 8
    else:
        score += 4

    # News (8)
    wanted_news = "POSITIVE" if long_side else "NEGATIVE"
    opposite_news = "NEGATIVE" if long_side else "POSITIVE"

    if news_mood == wanted_news:
        score += 8
        reasons.append("Новостной фон в пользу сделки")
    elif news_mood == opposite_news:
        score -= 10
    else:
        score += 4

    return score, reasons


_FUNDING_CACHE = {}


def funding_adjustment(exchange, symbol, side):

    key = (exchange.id, symbol)

    if key not in _FUNDING_CACHE:

        try:

            data = exchange.fetch_funding_rate(symbol)
            rate = data.get("fundingRate")
            _FUNDING_CACHE[key] = (
                None if rate is None else float(rate)
            )

        except Exception as error:

            log.debug("Funding unavailable %s: %s", symbol, error)
            _FUNDING_CACHE[key] = None

    rate = _FUNDING_CACHE[key]

    if rate is None or not np.isfinite(rate):
        return 0

    crowded_long = rate >= 0.0005
    crowded_short = rate <= -0.0005

    if side == "LONG":
        if crowded_long:
            return -5
        if crowded_short:
            return 2

    if side == "SHORT":
        if crowded_short:
            return -5
        if crowded_long:
            return 2

    return 0


# ============================================================
# EXCHANGE SCANNER
# ============================================================

def scan_exchange(
    exchange,
    state,
    btc,
    news_mood,
    news_context
):

    log.info("Scanning %s", exchange.id)

    try:
        symbols = market_symbols(exchange)

    except Exception as error:
        log.error("Market loading failed: %s", error)
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
                    df15,
                    df1h,
                    df4h,
                    btc,
                    news_mood,
                    side
                )

                if score < MIN_SCORE - 2:
                    continue

                score += funding_adjustment(
                    exchange,
                    symbol,
                    side
                )

                score = max(0, min(100, score))

                if score < MIN_SCORE:
                    continue

                signal = create_signal(
                    exchange,
                    symbol,
                    side,
                    score,
                    reasons,
                    df15,
                    df1h
                )

                if signal is None:
                    continue

                duplicate = any(
                    item.get("symbol") == symbol
                    and item.get("exchange") == exchange.id.upper()
                    and item.get("status") == "OPEN"
                    for item in state["signals"].values()
                    if isinstance(item, dict)
                )

                if duplicate or signal["id"] in state["signals"]:
                    continue

                if telegram(
                    signal_message(signal, news_context)
                ):

                    state["signals"][signal["id"]] = signal

                    daily_count += 1
                    state["daily"][daily_key] = daily_count

                    save_state(state)

                    log.info(
                        "Signal sent: %s %s score=%s",
                        symbol,
                        side,
                        score
                    )

        except Exception as error:

            log.warning(
                "Symbol scan failed %s: %s",
                symbol,
                error
            )


# ============================================================
# MAIN
# ============================================================

def main():

    if quiet_hours():
        log.info("Night pause")
        return

    state = load_state()

    exchanges = {}

    for name in ("bingx", "okx"):

        try:
            exchanges[name] = make_exchange(name)

        except Exception as error:

            log.error(
                "Exchange initialization failed %s: %s",
                name,
                error
            )

    if not exchanges:
        raise RuntimeError("No exchanges available")

    # Monitor previously generated signals.
    monitor_signals(state, exchanges)
    save_state(state)

    # Read news.
    news = get_news()

    news_mood, relevant_news = news_analysis(news)

    news_context = get_signal_news_context(
        news_mood,
        relevant_news
    )

    log.info(
        "News mood: %s | relevant: %s",
        news_mood,
        len(relevant_news)
    )

    # Scan exchanges.
    for exchange in exchanges.values():

        btc = btc_regime(exchange)

        scan_exchange(
            exchange,
            state,
            btc,
            news_mood,
            news_context
        )

    state["last_run"] = now().isoformat()

    save_state(state)

    log.info("GPT 3.0 completed successfully")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()

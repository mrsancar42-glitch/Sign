import os
import json
import time
import logging
import hashlib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import ccxt
import pandas as pd
import numpy as np
import mplfinance as mpf

# ==========================================================
# GPT 5.0 HUNTER | ACTIVE RESEARCH
# BingX + OKX | Signals only | No order execution
# ==========================================================

TZ = ZoneInfo("Europe/Istanbul")
STATE_FILE = "hunter_state.json"
CHART_DIR = "charts"

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

MIN_SCORE = int(os.getenv("MIN_SCORE", "75"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "25"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "2000000"))
TIMEFRAMES = ("15m", "1h", "4h")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("HUNTER")


# ==========================================================
# TIME / STATE
# ==========================================================

def now():
    return datetime.now(TZ)


def today():
    return now().strftime("%Y-%m-%d")


def default_state():
    return {
        "signals": {},
        "daily": {},
        "last_report": None
    }


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return default_state()

        defaults = default_state()
        for key, value in defaults.items():
            if not isinstance(state.get(key), type(value)):
                state[key] = value

        return state

    except (OSError, ValueError, TypeError):
        return default_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def daily_stats(state):
    return state["daily"].setdefault(today(), {
        "sent": 0,
        "setups": 0,
        "rejected": 0,
        "no_structure": 0,
        "errors": 0,
        "news": "UNKNOWN",
        "reasons": {}
    })


def reason(state, message):
    stats = daily_stats(state)
    stats["reasons"][message] = (
        stats["reasons"].get(message, 0) + 1
    )


# ==========================================================
# TELEGRAM
# ==========================================================

def telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        log.warning("Telegram credentials missing")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
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


def send_photo(path, caption):
    if not TG_TOKEN or not TG_CHAT:
        return False

    try:
        with open(path, "rb") as photo:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                data={
                    "chat_id": TG_CHAT,
                    "caption": caption
                },
                files={"photo": photo},
                timeout=30
            )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram photo: %s", e)
        return False


# ==========================================================
# EXCHANGES
# ==========================================================

def make_exchange(name):
    cls = getattr(ccxt, name)
    return cls({
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {"defaultType": "swap"}
    })


def get_symbols(exchange):
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
        ):
            continue

        ticker = tickers.get(symbol, {})
        volume = ticker.get("quoteVolume") or 0

        try:
            volume = float(volume)
            if not volume and ticker.get("last"):
                volume = float(ticker.get("baseVolume") or 0) * float(ticker["last"])
        except (ValueError, TypeError):
            volume = 0

        if volume >= MIN_VOLUME:
            result.append((symbol, volume))

    result.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in result[:MAX_SYMBOLS]]


# ==========================================================
# INDICATORS
# ==========================================================

def ema(s, period):
    return s.ewm(span=period, adjust=False).mean()


def rsi(s, period=14):
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    up = gain.ewm(alpha=1 / period, adjust=False).mean()
    down = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = up / down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def prepare(rows):
    df = pd.DataFrame(rows, columns=[
        "timestamp", "open", "high", "low", "close", "volume"
    ])

    if len(df) > 1:
        df = df.iloc[:-1].copy()

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["ema20"] = ema(df.close, 20)
    df["ema50"] = ema(df.close, 50)
    df["ema200"] = ema(df.close, 200)
    df["rsi"] = rsi(df.close)

    previous = df.close.shift(1)
    tr = pd.concat([
        df.high - df.low,
        (df.high - previous).abs(),
        (df.low - previous).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["vol_avg"] = df.volume.rolling(20).mean()
    df["vol_ratio"] = df.volume / df.vol_avg.replace(0, np.nan)

    df["macd"] = ema(df.close, 12) - ema(df.close, 26)
    df["macd_signal"] = ema(df.macd, 9)

    df["high20"] = df.high.shift(1).rolling(20).max()
    df["low20"] = df.low.shift(1).rolling(20).min()

    return df.replace([np.inf, -np.inf], np.nan).dropna()


def fetch_df(exchange, symbol, timeframe):
    rows = exchange.fetch_ohlcv(
        symbol, timeframe, limit=260
    )
    return prepare(rows)


# ==========================================================
# FREE NEWS INTELLIGENCE
# ==========================================================

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://cryptonews.com/news/feed/"
]

NEGATIVE = [
    "hack", "hacked", "exploit", "lawsuit", "ban",
    "crash", "fraud", "bankruptcy", "liquidation",
    "breach", "санкц", "взлом", "запрет", "обвал"
]

POSITIVE = [
    "approval", "approved", "adoption", "partnership",
    "inflow", "upgrade", "launch", "etf",
    "institutional", "одобр", "приток"
]

ALIASES = {
    "BTC": ["bitcoin"],
    "ETH": ["ethereum"],
    "SOL": ["solana"],
    "BNB": ["binance coin"],
    "XRP": ["ripple"],
    "DOGE": ["dogecoin"],
    "ADA": ["cardano"],
    "AVAX": ["avalanche"]
}


def get_news():
    items = []

    for url in FEEDS:
        try:
            response = requests.get(
                url, timeout=12,
                headers={"User-Agent": "HunterResearch/5.0"}
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)

            for item in root.findall(".//item")[:25]:
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""

                if title:
                    items.append({
                        "title": title.strip(),
                        "link": link
                    })

        except Exception as e:
            log.warning("News source unavailable: %s", e)

    return items


def news_for_symbol(news, symbol):
    base = symbol.split("/")[0].upper()
    aliases = [base.lower()] + ALIASES.get(base, [])

    relevant = []
    for item in news:
        title = item["title"].lower()

        if any(word in title for word in aliases):
            relevant.append(item)

    negative = sum(
        any(word in x["title"].lower() for word in NEGATIVE)
        for x in relevant
    )
    positive = sum(
        any(word in x["title"].lower() for word in POSITIVE)
        for x in relevant
    )

    score = 0
    if negative:
        score -= min(15, negative * 8)
    if positive:
        score += min(5, positive * 2)

    if negative:
        mood = "NEGATIVE"
    elif positive:
        mood = "POSITIVE"
    else:
        mood = "NEUTRAL"

    return score, mood, relevant[:3]


# ==========================================================
# BTC MARKET REGIME
# ==========================================================

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


# ==========================================================
# TECHNICAL ANALYSIS
# ==========================================================

def analyse(df15, df1h, df4h, btc, news_score, side):
    a, b, c = df15.iloc[-1], df1h.iloc[-1], df4h.iloc[-1]
    score = 0
    reasons = []

    if side == "LONG":
        if c.close > c.ema20 > c.ema50:
            score += 15
            reasons.append("4H — восходящий тренд")

        if b.close > b.ema20 > b.ema50:
            score += 15
            reasons.append("1H — покупатели удерживают тренд")

        if a.close > a.ema20 > a.ema50:
            score += 10
            reasons.append("15M — бычье выравнивание EMA")

        if 45 <= a.rsi <= 68:
            score += 8
            reasons.append("RSI — покупательский импульс")

        if a.macd > a.macd_signal:
            score += 8
            reasons.append("MACD — бычье подтверждение")

        if a.vol_ratio >= 1.2:
            score += 10
            reasons.append("Объём — активность выше среднего")

        if a.close > a.high20:
            score += 12
            reasons.append("Пробой локального сопротивления")

        elif a.low < a.low20 and a.close > a.low20:
            score += 12
            reasons.append("Снятие ликвидности и возврат")

        if btc == "BULLISH":
            score += 10
            reasons.append("BTC поддерживает LONG")
        elif btc == "BEARISH":
            score -= 12

    else:
        if c.close < c.ema20 < c.ema50:
            score += 15
            reasons.append("4H — нисходящий тренд")

        if b.close < b.ema20 < b.ema50:
            score += 15
            reasons.append("1H — продавцы удерживают тренд")

        if a.close < a.ema20 < a.ema50:
            score += 10
            reasons.append("15M — медвежье выравнивание EMA")

        if 32 <= a.rsi <= 55:
            score += 8
            reasons.append("RSI — продавательский импульс")

        if a.macd < a.macd_signal:
            score += 8
            reasons.append("MACD — медвежье подтверждение")

        if a.vol_ratio >= 1.2:
            score += 10
            reasons.append("Объём — активность выше среднего")

        if a.close < a.low20:
            score += 12
            reasons.append("Пробой локальной поддержки")

        elif a.high > a.high20 and a.close < a.high20:
            score += 12
            reasons.append("Снятие ликвидности и отказ от роста")

        if btc == "BEARISH":
            score += 10
            reasons.append("BTC поддерживает SHORT")
        elif btc == "BULLISH":
            score -= 12

    score += news_score
    score = max(0, min(100, score))

    return score, reasons


# ==========================================================
# STRUCTURE BASED STOP LOSS / TAKE PROFIT
# ==========================================================

def swing_points(df, left=3, right=3):
    highs, lows = [], []

    for i in range(left, len(df) - right):
        high = df.high.iloc[i]
        low = df.low.iloc[i]

        if high >= df.high.iloc[i-left:i+right+1].max():
            highs.append(float(high))

        if low <= df.low.iloc[i-left:i+right+1].min():
            lows.append(float(low))

    return highs, lows


def structure_levels(df, side):
    if len(df) < 40:
        return None

    entry = float(df.close.iloc[-1])
    volatility = float(df.atr.iloc[-1])

    if not np.isfinite(volatility) or volatility <= 0:
        return None

    highs, lows = swing_points(df.iloc[-100:-1])

    if side == "LONG":
        supports = [x for x in lows if x < entry]
        if not supports:
            return None

        stop = max(supports) - volatility * 0.25
        risk = entry - stop

        if risk <= 0:
            return None

        targets = sorted(
            x for x in set(highs)
            if x > entry and (x-entry)/risk >= 1.0
        )

        fallback = [
            entry + risk * 1.5,
            entry + risk * 2.5,
            entry + risk * 3.5
        ]

    elif side == "SHORT":
        resistances = [x for x in highs if x > entry]
        if not resistances:
            return None

        stop = min(resistances) + volatility * 0.25
        risk = stop - entry

        if risk <= 0:
            return None

        targets = sorted(
            (
                x for x in set(lows)
                if x < entry and (entry-x)/risk >= 1.0
            ),
            reverse=True
        )

        fallback = [
            entry - risk * 1.5,
            entry - risk * 2.5,
            entry - risk * 3.5
        ]

    else:
        return None

    if risk / entry > 0.03:
        return None

    selected = []
    for target in targets:
        if not selected:
            selected.append(target)
        elif side == "LONG" and target > selected[-1]:
            selected.append(target)
        elif side == "SHORT" and target < selected[-1]:
            selected.append(target)

        if len(selected) == 3:
            break

    for target in fallback:
        if len(selected) == 3:
            break

        if not selected:
            selected.append(target)
        elif side == "LONG" and target > selected[-1]:
            selected.append(target)
        elif side == "SHORT" and target < selected[-1]:
            selected.append(target)

    if len(selected) != 3:
        return None

    rr = [abs(x-entry)/risk for x in selected]

    if min(rr) < 1.0:
        return None

    return {
        "entry": entry,
        "stop": stop,
        "risk": risk,
        "tp1": selected[0],
        "tp2": selected[1],
        "tp3": selected[2],
        "rr": rr
    }


def audit(levels, side):
    if not levels:
        return False

    e, s = levels["entry"], levels["stop"]

    if side == "LONG":
        valid = (
            s < e < levels["tp1"]
            < levels["tp2"] < levels["tp3"]
        )
    else:
        valid = (
            s > e > levels["tp1"]
            > levels["tp2"] > levels["tp3"]
        )

    if not valid:
        return False

    if min(levels["rr"]) < 1.0:
        return False

    return all(np.isfinite(v) for v in levels.values()
               if isinstance(v, (int, float)))


# ==========================================================
# GRAPH GENERATION
# ==========================================================

def make_chart(df, signal):
    os.makedirs(CHART_DIR, exist_ok=True)

    path = os.path.join(
        CHART_DIR,
        hashlib.md5(signal["id"].encode()).hexdigest() + ".png"
    )

    chart = df.tail(100).copy()
    chart["datetime"] = pd.to_datetime(
        chart.timestamp, unit="ms", utc=True
    )
    chart.set_index("datetime", inplace=True)

    levels = [
        signal["entry"],
        signal["stop"],
        signal["tp1"],
        signal["tp2"],
        signal["tp3"]
    ]

    colors = ["#FFD54F", "#EF5350", "#66BB6A",
              "#43A047", "#2E7D32"]

    fig, axes = mpf.plot(
        chart,
        type="candle",
        style="charles",
        volume=True,
        mav=(20, 50),
        hlines=dict(
            hlines=levels,
            colors=colors,
            linestyle="--",
            linewidths=1
        ),
        title=f"{signal['symbol']} | {signal['side']} | HUNTER 5.0",
        figsize=(13, 8),
        returnfig=True
    )

    ax = axes[0]
    labels = ["ENTRY", "STOP", "TP1", "TP2", "TP3"]

    for level, label, color in zip(levels, labels, colors):
        ax.text(
            0.99, level, f" {label}: {level:.6g}",
            color=color,
            transform=ax.get_yaxis_transform(),
            ha="right",
            va="bottom",
            fontsize=8
        )

    fig.savefig(path, dpi=130, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)

    return path


# ==========================================================
# SIGNAL MESSAGE
# ==========================================================

def fmt(value):
    return f"{value:.8g}"


def make_message(s):
    reasons = "
".join("• " + x for x in s["reasons"])

    return (
        f"🤖 GPT 5.0 HUNTER

"
        f"{'🟢' if s['side'] == 'LONG' else '🔴'} "
        f"{s['side']} — {s['symbol']}
"
        f"Биржа: {s['exchange']}

"
        f"ENTRY: {fmt(s['entry'])}
"
        f"TP1: {fmt(s['tp1'])}
"
        f"TP2: {fmt(s['tp2'])}
"
        f"TP3: {fmt(s['tp3'])}
"
        f"STOP LOSS: {fmt(s['stop'])}

"
        f"📊 ТЕХНИЧЕСКИЙ АНАЛИЗ
{reasons}

"
        f"🎯 ПРИЧИНА ВХОДА:
{s['explanation']}

"
        f"⭐ ОЦЕНКА: {s['score']}/100"
    )


# ==========================================================
# SIGNAL MONITOR
# ==========================================================

def monitor_signals(state, exchanges):
    for signal in list(state["signals"].values()):
        if signal.get("status") != "OPEN":
            continue

        try:
            exchange = exchanges.get(
                signal["exchange"].lower()
            )
            if not exchange:
                continue

            ticker = exchange.fetch_ticker(signal["symbol"])
            price = float(ticker["last"])

            long_side = signal["side"] == "LONG"

            stop_hit = (
                price <= signal["stop"]
                if long_side else price >= signal["stop"]
            )

            if stop_hit:
                signal["status"] = "STOPPED"
                signal["stop_hit"] = True
                telegram(
                    f"🛑 HUNTER | STOP LOSS
"
                    f"{signal['symbol']} | {signal['side']}
"
                    f"Цена: {fmt(price)}"
                )
                continue

            for n in (1, 2, 3):
                target = signal[f"tp{n}"]
                key = f"tp{n}_hit"

                crossed = (
                    price >= target
                    if long_side else price <= target
                )

                if not signal.get(key) and crossed:
                    signal[key] = True

                    telegram(
                        f"🎯 HUNTER | TP{n} HIT
"
                        f"{signal['symbol']}
"
                        f"Цена: {fmt(price)}"
                    )

                    if n == 1:
                        signal["stop"] = signal["entry"]

            if signal.get("tp3_hit"):
                signal["status"] = "COMPLETED"

        except Exception as e:
            log.warning(
                "Monitor %s: %s",
                signal.get("symbol", "UNKNOWN"),
                e
            )

# ============================================================
# SIGNAL SCANNER
# ============================================================

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

                levels = calculate_structure_levels(
                    df15, side
                )

                if not validate_structure(levels, side):
                    continue

                signal = create_signal(
                    exchange, symbol, side,
                    score, reasons, df15, levels
                )

                if not signal:
                    continue

                duplicate = any(
                    s.get("symbol") == symbol
                    and s.get("exchange") == exchange.id.upper()
                    and s.get("status") == "OPEN"
                    for s in state["signals"].values()
                )

                if duplicate:
                    continue

                if telegram(signal_message(signal)):
                    state["signals"][signal["id"]] = signal
                    state["daily"][daily_key] = daily_count + 1
                    daily_count += 1
                    save_state(state)

        except Exception as e:
            log.warning("Scan error %s: %s", symbol, e)

# ============================================================
# DAILY REPORT
# ============================================================

def daily_report(state):
    today = now().strftime("%Y-%m-%d")
    signals = list(state.get("signals", {}).values())

    today_signals = [
        s for s in signals
        if s.get("created", "").startswith(today)
    ]

    opened = sum(
        s.get("status") == "OPEN"
        for s in today_signals
    )

    completed = sum(
        s.get("status") == "COMPLETED"
        for s in today_signals
    )

    stopped = sum(
        s.get("status") == "STOPPED"
        for s in today_signals
    )

    tp1 = sum(s.get("tp1_hit", False) for s in today_signals)
    tp2 = sum(s.get("tp2_hit", False) for s in today_signals)
    tp3 = sum(s.get("tp3_hit", False) for s in today_signals)

    count = len(today_signals)

    message = (
        "🤖 GPT 5.0 HUNTER | DAILY REPORT
"
        "━━━━━━━━━━━━━━━━━━━━
"
        f"Дата: {today}

"
        f"Всего сигналов: {count}
"
        f"В обработке: {opened}
"
        f"Завершено: {completed}
"
        f"STOP LOSS: {stopped}

"
        f"TP1 достигнут: {tp1}
"
        f"TP2 достигнут: {tp2}
"
        f"TP3 достигнут: {tp3}
"
    )

    if count == 0:
        message += (
            "
Сегодня сигналов не было.
"
            "Причина: ни одна комбинация условий "
            "не прошла фильтры модели."
        )

    telegram(message)


# ============================================================
# MAIN
# ============================================================

def main():
    state = load_state()

    # Night hours: no trading signals.
    if quiet_hours():
        log.info("Sleep mode: 00:00-07:00 Istanbul")
        return

    exchanges = {}

    for name in ("bingx", "okx"):
        try:
            exchanges[name] = make_exchange(name)
        except Exception as e:
            log.error("Exchange init %s: %s", name, e)

    if not exchanges:
        log.error("No exchanges available")
        return

    monitor_signals(state, exchanges)

    news = get_news()
    news_mood, relevant_news = news_analysis(news)

    for exchange in exchanges.values():
        btc = btc_regime(exchange)
        scan_exchange(
            exchange,
            state,
            btc,
            news_mood
        )

    state["last_run"] = now().isoformat()
    save_state(state)


if __name__ == "__main__":
    main()

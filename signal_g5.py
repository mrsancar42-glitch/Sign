import os
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd

# SIGNAL G5 V5 — OKX SPOT market scanner.
# Sends educational PAPER setups only. It does not place orders or manage money.

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "signal_g5_state.json"
OKX_BASE = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

REQUEST_TIMEOUT = 15
KLINE_LIMIT = 100
REQUEST_PAUSE = 0.10
ALERT_COOLDOWN_HOURS = float(os.getenv("ALERT_COOLDOWN_HOURS", "8"))
MAX_SIGNALS_PER_RUN = int(os.getenv("MAX_SIGNALS_PER_RUN", "8"))
HEARTBEAT_HOURS = 4

# Strategy parameters; tune only after paper-testing and reviewing logged outcomes.
RSI_PERIOD = 14
ATR_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 50
VOLUME_LOOKBACK = 20
MIN_VOLUME_RATIO = 0.80
MIN_SCORE = 6
MIN_STOP_PCT = 0.0035       # 0.35%
MAX_STOP_ATR = 2.2
STOP_ATR_MULTIPLIER = 1.5
REWARD_RISK = 2.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s UTC | %(levelname)s | %(message)s")
log = logging.getLogger("signal-g5-v5")


def utc_now():
    return datetime.now(timezone.utc)


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state root must be an object")
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        state = {}
    state.setdefault("sent_signals", {})
    state.setdefault("last_heartbeat", None)
    return state


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def okx_get(path, params=None):
    response = requests.get(
        f"{OKX_BASE}{path}", params=params, timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "SignalG5-PaperScanner/5.0"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise RuntimeError(f"OKX API error: {payload.get('code', '?')} {payload.get('msg', '')}")
    return payload.get("data", [])


def fetch_active_symbols():
    rows = okx_get("/api/v5/public/instruments", {"instType": "SPOT"})
    return sorted({
        row["instId"] for row in rows
        if row.get("quoteCcy") == "USDT"
        and row.get("state") == "live"
        and row.get("instType") == "SPOT"
        and row.get("instId", "").endswith("-USDT")
    })


def get_klines(symbol, interval):
    bars = {"15m": "15m", "1h": "1H", "4h": "4H"}
    if interval not in bars:
        raise ValueError(f"Unsupported interval: {interval}")
    rows = okx_get("/api/v5/market/candles", {
        "instId": symbol, "bar": bars[interval], "limit": str(KLINE_LIMIT),
    })
    # OKX candle fields: ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm.
    closed = [r for r in rows if len(r) >= 9 and r[8] == "1"]
    closed.sort(key=lambda r: int(r[0]))
    if len(closed) < 60:
        raise RuntimeError(f"Only {len(closed)} closed candles for {symbol} {interval}")
    df = pd.DataFrame(closed, columns=[
        "ts", "open", "high", "low", "close", "volume", "vol_ccy", "vol_quote", "confirm"
    ])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
    if len(df) < 60 or (df["close"] <= 0).any():
        raise RuntimeError(f"Invalid candle data for {symbol} {interval}")
    return df


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=RSI_PERIOD):
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    out = 100 - (100 / (1 + rs))
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100)
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0)
    return out


def atr(df, period=ATR_PERIOD):
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def trend(df):
    close = df["close"]
    e20 = ema(close, EMA_FAST)
    e50 = ema(close, EMA_SLOW)
    if close.iloc[-1] > e20.iloc[-1] > e50.iloc[-1] and e20.iloc[-1] > e20.iloc[-4]:
        return "LONG"
    if close.iloc[-1] < e20.iloc[-1] < e50.iloc[-1] and e20.iloc[-1] < e20.iloc[-4]:
        return "SHORT"
    return None


def analyze(symbol):
    df15 = get_klines(symbol, "15m")
    time.sleep(REQUEST_PAUSE)
    df1h = get_klines(symbol, "1h")
    time.sleep(REQUEST_PAUSE)
    df4h = get_klines(symbol, "4h")

    side4h, side1h = trend(df4h), trend(df1h)
    if not side4h or side4h != side1h:
        return None
    side = side4h

    close = float(df15["close"].iloc[-1])
    prev_close = float(df15["close"].iloc[-2])
    high = float(df15["high"].iloc[-1])
    low = float(df15["low"].iloc[-1])
    open_price = float(df15["open"].iloc[-1])
    e20_15 = float(ema(df15["close"], EMA_FAST).iloc[-1])
    rsi_now = float(rsi(df15["close"]).iloc[-1])
    atr_now = float(atr(df15).iloc[-1])
    vol_base = float(df15["volume"].iloc[-VOLUME_LOOKBACK-1:-1].mean())
    vol_ratio = float(df15["volume"].iloc[-1] / vol_base) if vol_base > 0 else 0.0
    if not all(pd.notna(x) for x in (rsi_now, atr_now)) or atr_now <= 0:
        return None

    # 15m trigger: close is on the trend side of EMA20 and has progressed
    # beyond the previous close; the candle body must agree with the setup.
    if side == "LONG":
        trigger = close > e20_15 and close > prev_close and close > open_price
        rsi_ok = 50 <= rsi_now <= 68
    else:
        trigger = close < e20_15 and close < prev_close and close < open_price
        rsi_ok = 32 <= rsi_now <= 50
    if not trigger or not rsi_ok or vol_ratio < MIN_VOLUME_RATIO:
        return None

    # Avoid chasing an already stretched 15m move.
    if abs(close - e20_15) > 1.8 * atr_now:
        return None

    # Confluence score: aligned 4h + 1h trend (4 pts), 15m EMA (1),
    # RSI zone (1), volume (1), candle direction (1). Mandatory filters above.
    score = 4
    score += int((close > e20_15) if side == "LONG" else (close < e20_15))
    score += int(rsi_ok)
    score += int(vol_ratio >= 1.0)
    score += int((close > open_price) if side == "LONG" else (close < open_price))
    if score < MIN_SCORE:
        return None

    raw_risk = max(STOP_ATR_MULTIPLIER * atr_now, close * MIN_STOP_PCT)
    risk = raw_risk
    if risk > MAX_STOP_ATR * atr_now:
        return None
    entry = close
    if side == "LONG":
        sl = entry - risk
        tp = entry + risk * REWARD_RISK
    else:
        sl = entry + risk
        tp = entry - risk * REWARD_RISK

    return {
        "symbol": symbol, "side": side, "entry": entry, "tp": tp, "sl": sl,
        "rsi": rsi_now, "atr_pct": atr_now / close * 100,
        "volume_ratio": vol_ratio, "score": score,
        "trend1h": side1h, "trend4h": side4h,
        "created_at": utc_now().isoformat(),
    }


def price_text(value):
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.5f}".rstrip("0").rstrip(".")
    if value >= 0.01:
        return f"{value:.7f}".rstrip("0").rstrip(".")
    return f"{value:.10f}".rstrip("0").rstrip(".")


def format_signal(item):
    side_label = "LONG" if item["side"] == "LONG" else "SHORT (учебный сценарий)"
    return (
        "🎯 SIGNAL G5 V5 — PAPER SETUP\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Монета: {item['symbol']}\n"
        f"Направление: {side_label}\n"
        f"Вход для наблюдения: {price_text(item['entry'])}\n"
        f"TP: {price_text(item['tp'])}\n"
        f"SL: {price_text(item['sl'])}\n\n"
        "📊 ПОДТВЕРЖДЕНИЯ\n"
        f"Тренд 4h / 1h: {item['trend4h']} / {item['trend1h']}\n"
        f"RSI 15m: {item['rsi']:.1f}\n"
        f"Объём к среднему: {item['volume_ratio']:.2f}×\n"
        f"Оценка условий: {item['score']}/8\n"
        f"Расчётное соотношение риск/потенциал: 1:{REWARD_RISK:.1f}\n\n"
        "Это учебный сигнал для paper trading. Цены могут измениться; уровни не гарантируют результат. "
        "Бот не открывает сделки и не является рекомендацией торговать реальными деньгами."
    )


def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in GitHub Secrets")
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def signal_key(item):
    return f"{item['symbol']}:{item['side']}"


def on_cooldown(state, item, now):
    stamp = state["sent_signals"].get(signal_key(item))
    if not stamp:
        return False
    try:
        then = datetime.fromisoformat(stamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (now - then).total_seconds() < ALERT_COOLDOWN_HOURS * 3600
    except (ValueError, TypeError):
        return False


def main():
    now = utc_now()
    state = load_state()
    symbols = fetch_active_symbols()
    if not symbols:
        raise RuntimeError("OKX returned no active USDT spot pairs")
    log.info("Source: OKX SPOT; loaded %d active USDT pairs", len(symbols))

    candidates, errors = [], []
    for idx, symbol in enumerate(symbols, 1):
        try:
            item = analyze(symbol)
            if item:
                candidates.append(item)
                log.info("[%d/%d] PAPER SETUP %s %s score=%d/8", idx, len(symbols), symbol, item["side"], item["score"])
        except Exception as exc:
            errors.append((symbol, str(exc)))
            log.warning("[%d/%d] %s failed: %s", idx, len(symbols), symbol, exc)
        time.sleep(REQUEST_PAUSE)

    candidates.sort(key=lambda x: (x["score"], x["volume_ratio"]), reverse=True)
    fresh = [x for x in candidates if not on_cooldown(state, x, now)]
    sent = 0
    for item in fresh[:MAX_SIGNALS_PER_RUN]:
        try:
            telegram_send(format_signal(item))
            state["sent_signals"][signal_key(item)] = now.isoformat()
            sent += 1
            time.sleep(0.25)
        except Exception:
            log.exception("Telegram delivery failed for %s", item["symbol"])

    last_hb = state.get("last_heartbeat")
    heartbeat_due = True
    if last_hb:
        try:
            previous = datetime.fromisoformat(last_hb)
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            heartbeat_due = (now - previous).total_seconds() >= HEARTBEAT_HOURS * 3600
        except (ValueError, TypeError):
            heartbeat_due = True
    if heartbeat_due:
        hb = (
            "🛰 SIGNAL G5 V5 — СТАТУС\n"
            f"Время UTC: {now:%Y-%m-%d %H:%M}\n"
            f"Проверено пар OKX SPOT: {len(symbols)}\n"
            f"Кандидатов по стратегии: {len(candidates)}\n"
            f"Новых paper-сигналов: {sent}\n"
            f"Ошибок отдельных пар: {len(errors)}\n\n"
            "Режим: анализ и paper trading; реальные сделки не открываются."
        )
        try:
            telegram_send(hb)
            state["last_heartbeat"] = now.isoformat()
        except Exception:
            log.exception("Heartbeat delivery failed")

    save_state(state)
    log.info("Finished: symbols=%d candidates=%d sent=%d errors=%d", len(symbols), len(candidates), sent, len(errors))
    if not candidates and len(errors) == len(symbols):
        raise RuntimeError("All OKX instrument analyses failed; inspect API/network errors")


if __name__ == "__main__":
    main()

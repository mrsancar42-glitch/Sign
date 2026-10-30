import os
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd

# Signal G5 V4 — market observation monitor.
# Informational alerts only: no entry prices, stop-loss, take-profit, or trade execution.

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "signal_g5_state.json"
OKX_BASE = os.getenv("OKX_BASE_URL", "https://www.okx.com")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Dynamically loaded from OKX: all active USDT-quoted spot instruments.
SYMBOLS = []

# Observation thresholds, not trading recommendations.
MIN_1H_MOVE_PCT = 1.0
MIN_15M_MOVE_PCT = 0.65
MIN_VOLUME_RATIO = 1.25
ALERT_COOLDOWN_HOURS = 4
HEARTBEAT_HOURS = 4
REQUEST_TIMEOUT = 12
KLINE_LIMIT = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC | %(levelname)s | %(message)s",
)
log = logging.getLogger("signal-g5")


def utc_now():
    return datetime.now(timezone.utc)


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state root is not an object")
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        state = {}
    state.setdefault("sent_alerts", {})
    state.setdefault("last_heartbeat", None)
    state.setdefault("last_run", None)
    return state


def save_state(state):
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def get_json(path, params=None):
    response = requests.get(
        f"{OKX_BASE}{path}",
        params=params,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "SignalG5-MarketMonitor/4.0"},
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("code") != "0":
        raise RuntimeError(f"OKX API error: {data.get('code') if isinstance(data, dict) else 'bad response'} "
                           f"{data.get('msg', '') if isinstance(data, dict) else ''}")
    return data


def fetch_active_symbols():
    """All active OKX spot instruments quoted in USDT, sorted by instrument ID."""
    data = get_json("/api/v5/public/instruments", {"instType": "SPOT"})
    symbols = []
    for item in data.get("data", []):
        if (item.get("quoteCcy") == "USDT"
                and item.get("state") == "live"
                and item.get("instType") == "SPOT"):
            symbols.append(item["instId"])
    return sorted(set(symbols))


def get_klines(symbol, interval):
    bar_map = {"15m": "15m", "1h": "1H", "4h": "4H"}
    if interval not in bar_map:
        raise ValueError(f"Неподдерживаемый таймфрейм: {interval}")
    payload = get_json(
        "/api/v5/market/candles",
        {"instId": symbol, "bar": bar_map[interval], "limit": str(KLINE_LIMIT)},
    )
    rows = payload.get("data", [])
    if not rows:
        raise RuntimeError(f"OKX не вернула свечи для {symbol} {interval}")
    # OKX returns newest candles first. Columns: ts, o, h, l, c, vol, ... , confirm.
    rows = [row for row in rows if len(row) >= 9 and row[8] == "1"]
    rows.sort(key=lambda row: int(row[0]))
    if len(rows) < 60:
        raise RuntimeError(f"Недостаточно закрытых свечей для {symbol} {interval}: {len(rows)}")
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "volume_currency", "quote_volume", "confirm"
    ])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"])
    if len(df) < 60:
        raise RuntimeError(f"Некачественные данные для {symbol} {interval}")
    return df


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def analyze(symbol):
    # Fetch sequentially to keep request load modest.
    df15 = get_klines(symbol, "15m")
    time.sleep(0.08)
    df1h = get_klines(symbol, "1h")
    time.sleep(0.08)
    df4h = get_klines(symbol, "4h")

    # get_klines returns only fully closed candles (OKX confirm=1).
    if min(len(df15), len(df1h), len(df4h)) < 50:
        raise RuntimeError(f"Недостаточно закрытых свечей для {symbol}")

    last15, prev15 = df15.iloc[-1], df15.iloc[-2]
    last1h, prev1h = df1h.iloc[-1], df1h.iloc[-2]
    last4h = df4h.iloc[-1]

    move15 = (last15["close"] / prev15["close"] - 1) * 100
    move1h = (last1h["close"] / df1h.iloc[-5]["close"] - 1) * 100
    move4h = (last4h["close"] / df4h.iloc[-4]["close"] - 1) * 100

    vol_baseline = df15["volume"].iloc[-21:-1].mean()
    vol_ratio = float(last15["volume"] / vol_baseline) if vol_baseline > 0 else 0.0

    ema20_4h = float(ema(df4h["close"], 20).iloc[-1])
    ema50_4h = float(ema(df4h["close"], 50).iloc[-1])
    if last4h["close"] > ema20_4h and ema20_4h > ema50_4h:
        context = "восходящий"
    elif last4h["close"] < ema20_4h and ema20_4h < ema50_4h:
        context = "нисходящий"
    else:
        context = "смешанный"

    # Simple range-based volatility estimate on the latest closed 15m candle.
    range_pct = float((last15["high"] - last15["low"]) / last15["close"] * 100)

    reasons = []
    if abs(move1h) >= MIN_1H_MOVE_PCT:
        reasons.append(f"движение за 4 закрытых 1h-свечи: {move1h:+.2f}%")
    if abs(move15) >= MIN_15M_MOVE_PCT:
        reasons.append(f"последняя 15m-свеча: {move15:+.2f}%")
    if vol_ratio >= MIN_VOLUME_RATIO:
        reasons.append(f"объём 15m: {vol_ratio:.2f}× среднего")
    if range_pct >= 0.7:
        reasons.append(f"диапазон последней 15m-свечи: {range_pct:.2f}%")

    # Alerts describe observed market movement, not a buy/sell instruction.
    qualifies = (
        abs(move1h) >= MIN_1H_MOVE_PCT
        or (abs(move15) >= MIN_15M_MOVE_PCT and vol_ratio >= MIN_VOLUME_RATIO)
        or (range_pct >= 0.9 and vol_ratio >= 1.15)
    )
    direction = "вверх" if (move1h if abs(move1h) >= abs(move15) else move15) > 0 else "вниз"
    return {
        "symbol": symbol,
        "qualifies": qualifies,
        "direction": direction,
        "move15": float(move15),
        "move1h": float(move1h),
        "move4h": float(move4h),
        "vol_ratio": float(vol_ratio),
        "range_pct": float(range_pct),
        "context": context,
        "reasons": reasons,
    }


def telegram_send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Не заданы TELEGRAM_TOKEN или TELEGRAM_CHAT_ID в GitHub Secrets")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API вернул ошибку: {payload}")
    return True


def alert_key(item):
    return f'{item["symbol"]}:{item["direction"]}'


def is_cooled_down(state, item, now):
    sent = state["sent_alerts"].get(alert_key(item))
    if not sent:
        return False
    try:
        then = datetime.fromisoformat(sent)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (now - then).total_seconds() < ALERT_COOLDOWN_HOURS * 3600
    except (ValueError, TypeError):
        return False


def format_alert(item):
    reasons = item["reasons"] or ["обнаружено заметное движение цены"]
    return (
        f"📡 SIGNAL G5 V4 — НАБЛЮДЕНИЕ\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Пара: {item['symbol']}\n"
        f"Импульс: {item['direction']}\n"
        f"Контекст 4h: {item['context']}\n\n"
        f"📊 ИЗМЕРЕНИЯ\n"
        f"15m: {item['move15']:+.2f}%\n"
        f"1h (4 закрытые свечи): {item['move1h']:+.2f}%\n"
        f"4h (3 закрытые свечи): {item['move4h']:+.2f}%\n"
        f"Объём 15m к среднему: {item['vol_ratio']:.2f}×\n"
        f"Диапазон последней 15m-свечи: {item['range_pct']:.2f}%\n\n"
        f"Почему отмечено:\n• " + "\n• ".join(reasons) +
        "\n\nЭто автоматическое наблюдение за рынком, не рекомендация совершать сделку."
    )


def main():
    now = utc_now()
    state = load_state()
    state["last_run"] = now.isoformat()
    results = []
    errors = []

    try:
        symbols = fetch_active_symbols()
    except Exception as exc:
        log.exception("Не удалось загрузить список инструментов OKX")
        raise RuntimeError(f"Не удалось получить список монет с OKX: {exc}") from exc
    if not symbols:
        raise RuntimeError("OKX не вернула активных спотовых USDT-пар")
    log.info("Источник: OKX SPOT. Загружено %d активных USDT-пар; интервалы 15m/1h/4h", len(symbols))
    for index, symbol in enumerate(symbols, start=1):
        try:
            item = analyze(symbol)
            results.append(item)
            log.info(
                "[%d/%d] %s: 15m=%+.2f%% 1h=%+.2f%% 4h=%+.2f%% vol=%.2fx context=%s alert=%s",
                index, len(symbols), symbol, item["move15"], item["move1h"],
                item["move4h"], item["vol_ratio"], item["context"], item["qualifies"]
            )
        except Exception as exc:
            errors.append((symbol, str(exc)))
            log.exception("[%d/%d] Ошибка анализа %s", index, len(symbols), symbol)

    fresh_alerts = [
        item for item in results
        if item["qualifies"] and not is_cooled_down(state, item, now)
    ]
    sent_count = 0
    for item in fresh_alerts:
        try:
            telegram_send(format_alert(item))
            state["sent_alerts"][alert_key(item)] = now.isoformat()
            sent_count += 1
            log.info("Telegram: отправлено наблюдение по %s", item["symbol"])
            time.sleep(0.25)
        except Exception:
            log.exception("Не удалось отправить Telegram-сообщение по %s", item["symbol"])

    # Heartbeat ensures the bot does not remain silent all day.
    last_heartbeat = state.get("last_heartbeat")
    heartbeat_due = True
    if last_heartbeat:
        try:
            prev = datetime.fromisoformat(last_heartbeat)
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=timezone.utc)
            heartbeat_due = (now - prev).total_seconds() >= HEARTBEAT_HOURS * 3600
        except (ValueError, TypeError):
            heartbeat_due = True

    if heartbeat_due:
        qualifying_count = sum(1 for item in results if item["qualifies"])
        error_lines = "\n".join(f"• {s}: {e[:100]}" for s, e in errors[:5]) or "Ошибок загрузки данных нет."
        heartbeat = (
            f"🛰 SIGNAL G5 V4 — СТАТУС МОНИТОРА\n"
            f"Время UTC: {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"Проверено пар: {len(results)}/{len(symbols)}\n"
            f"Событий по порогам: {qualifying_count}\n"
            f"Новых сообщений отправлено: {sent_count}\n"
            f"Ошибок данных: {len(errors)}\n"
            f"Последние ошибки:\n{error_lines}\n\n"
            f"Режим: наблюдение и аналитика, без исполнения сделок."
        )
        try:
            telegram_send(heartbeat)
            state["last_heartbeat"] = now.isoformat()
            log.info("Telegram: отправлен статус монитора")
        except Exception:
            log.exception("Не удалось отправить статус монитора")

    save_state(state)
    log.info(
        "Итог: checked=%d/%d qualifying=%d sent=%d errors=%d",
        len(results), len(symbols),
        sum(1 for item in results if item["qualifies"]), sent_count, len(errors)
    )

    # Fail the workflow if every market request failed, so GitHub Actions visibly flags it.
    if not results:
        raise RuntimeError("Не удалось получить данные ни по одной паре; проверьте доступность OKX API и журналы.")


if __name__ == "__main__":
    main()

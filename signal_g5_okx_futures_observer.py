#!/usr/bin/env python3
"""
Signal G5 — OKX Futures Market Observer
Educational market-monitoring only:
- scans active OKX USDT perpetual swaps (SWAP instruments)
- ranks markets by recent activity and volatility
- sends at most ONE market-observation message to Telegram per run
- does NOT produce LONG/SHORT trade recommendations, entry prices, TP/SL,
  leverage advice, or place orders.

Environment variables:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID

Optional:
  MIN_24H_QUOTE_VOLUME_USDT (default: 5000000)
  MIN_15M_RANGE_PCT        (default: 0.8)
  COOLDOWN_MINUTES         (default: 60)
  STATE_FILE               (default: signal_g5_okx_state.json)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://www.okx.com"
TIMEOUT = 15
MIN_VOLUME = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "5000000"))
MIN_RANGE = float(os.getenv("MIN_15M_RANGE_PCT", "0.8"))
COOLDOWN = int(os.getenv("COOLDOWN_MINUTES", "60"))
STATE_FILE = os.getenv("STATE_FILE", "signal_g5_okx_state.json")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def get_json(path, params=None):
    response = requests.get(
        BASE_URL + path, params=params, timeout=TIMEOUT,
        headers={"User-Agent": "SignalG5-OKX-Market-Observer/1.0"}
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX API error: {payload.get('msg', 'unknown error')}")
    return payload.get("data", [])


def get_instruments():
    rows = get_json("/api/v5/public/instruments", {"instType": "SWAP"})
    return [
        row["instId"] for row in rows
        if row.get("settleCcy") == "USDT"
        and row.get("state") == "live"
        and row.get("instId", "").endswith("-USDT-SWAP")
    ]


def get_tickers():
    rows = get_json("/api/v5/market/tickers", {"instType": "SWAP"})
    return {row["instId"]: row for row in rows}


def get_candles(inst_id):
    rows = get_json(
        "/api/v5/market/candles",
        {"instId": inst_id, "bar": "15m", "limit": "5"}
    )
    # OKX candle order: newest first; [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    closed = [r for r in rows if len(r) > 8 and r[8] == "1"]
    if len(closed) < 2:
        return None
    current = closed[0]
    previous = closed[1]
    high = float(current[2])
    low = float(current[3])
    close = float(current[4])
    if close <= 0:
        return None
    range_pct = (high - low) / close * 100.0
    quote_volume = float(current[7]) if current[7] else 0.0
    prev_close = float(previous[4])
    change_pct = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
    return {
        "range_pct": range_pct,
        "quote_volume": quote_volume,
        "change_pct": change_pct,
        "close": close,
    }


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp, STATE_FILE)


def telegram_send(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID environment variables.")
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": TG_CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")


def scan():
    instruments = get_instruments()
    tickers = get_tickers()
    candidates = []

    for inst_id in instruments:
        ticker = tickers.get(inst_id)
        if not ticker:
            continue
        try:
            day_quote = float(ticker.get("volCcy24h") or 0.0)
            if day_quote < MIN_VOLUME:
                continue
            candle = get_candles(inst_id)
            if not candle or candle["range_pct"] < MIN_RANGE:
                continue

            # Ranking is an activity/volatility score, not a trade-direction score.
            score = (
                min(day_quote / MIN_VOLUME, 20.0) * 0.35
                + min(candle["range_pct"] / MIN_RANGE, 8.0) * 0.65
            )
            candidates.append((score, inst_id, day_quote, candle))
            time.sleep(0.06)  # keep request rate modest
        except (ValueError, KeyError, requests.RequestException):
            continue

    if not candidates:
        print("No market met the configured activity filters.")
        return

    candidates.sort(key=lambda item: item[0], reverse=True)
    score, inst_id, day_quote, candle = candidates[0]

    state = load_state()
    now = int(time.time())
    last_sent = int(state.get("last_sent_ts", 0))
    last_symbol = state.get("last_symbol", "")

    if inst_id == last_symbol and now - last_sent < COOLDOWN * 60:
        print(f"Top market unchanged ({inst_id}); cooldown is active.")
        return

    direction_word = (
        "рост за последнюю закрытую 15m-свечу"
        if candle["change_pct"] > 0 else
        "снижение за последнюю закрытую 15m-свечу"
        if candle["change_pct"] < 0 else
        "изменение около нуля за последнюю закрытую 15m-свечу"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = (
        "📊 SIGNAL G5 — OKX FUTURES MARKET WATCH\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Инструмент: {inst_id}\n"
        f"Активность: повышенная\n"
        f"15m диапазон: {candle['range_pct']:.2f}%\n"
        f"Изменение закрытой 15m-свечи: {candle['change_pct']:+.2f}% ({direction_word})\n"
        f"Объём за 24ч: {day_quote:,.0f} USDT\n"
        f"Кандидатов после фильтра: {len(candidates)}\n"
        f"Время: {stamp}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Это наблюдение за рынком, не торговый сигнал. "
        "Направление сделки, вход, TP и SL не формируются."
    )
    telegram_send(message)
    state.update({"last_sent_ts": now, "last_symbol": inst_id})
    save_state(state)
    print(f"Sent one market observation: {inst_id}")


if __name__ == "__main__":
    try:
        scan()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

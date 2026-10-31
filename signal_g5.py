#!/usr/bin/env python3
"""
Signal G5 — OKX Futures Momentum Scanner
-----------------------------------------
Логика сканера (моё видение):
  1. Из ~200 USDT-перпетуалов отбираем ликвидные:
     - 24h объём >= MIN_VOLUME
     - спред <= MAX_SPREAD_PCT (нет "мёртвых" стаканов)
     - дневной диапазон >= MIN_DAILY_RANGE (рынок вообще двигается)
  2. Для выживших тянем 1H (100 свечей) и 15m (50 свечей) и считаем:
     - тренд 1H: close > EMA20 > EMA50  → LONG-контекст
     - тренд 1H: close < EMA20 < EMA50  → SHORT-контекст
     - 15m: подтверждающая свеча в сторону тренда (или пробой/ретест EMA20)
     - всплеск объёма: последняя 15m свеча >= MIN_VOL_SURGE × среднего
     - ATR(14) 15m в "сладком" диапазоне [MIN_ATR_PCT..MAX_ATR_PCT]
       (не мёртвый рынок и не безумный памп)
  3. SL = свинг последних 10×15m ± 0.3×ATR, TP = SL × RR_MIN
  4. Отбрасываем, если SL% вне [MIN_SL_PCT..MAX_SL_PCT]
  5. Ранжируем по score (объём + всплеск + сила тренда + ATR + R:R)
  6. Отправляем ОДИН лучший сигнал в ТГ. Не повторяем монету N часов.

Env:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Optional:
  MIN_24H_QUOTE_VOLUME_USDT=5000000
  MAX_SPREAD_PCT=0.15
  MIN_DAILY_RANGE_PCT=2.0
  MIN_ATR_PCT=0.30
  MAX_ATR_PCT=3.00
  MIN_VOL_SURGE=1.30
  MIN_SL_PCT=0.40
  MAX_SL_PCT=3.00
  RR_MIN=1.80
  MAX_CANDIDATES=25          # сколько монет глубоко анализируем за прогон
  SYMBOL_COOLDOWN_MIN=240    # не повторять ту же монету N минут
  STATE_FILE=signal_g5_okx_state.json

⚠️  Это эвристика, не торговая система. Проверяй сигналы руками.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ------------------------------------------------------------------ CONFIG
BASE_URL = "https://www.okx.com"
TIMEOUT = 15

MIN_VOLUME        = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "5000000"))
MAX_SPREAD_PCT    = float(os.getenv("MAX_SPREAD_PCT", "0.15"))
MIN_DAILY_RANGE   = float(os.getenv("MIN_DAILY_RANGE_PCT", "2.0"))
MIN_ATR_PCT       = float(os.getenv("MIN_ATR_PCT", "0.30"))
MAX_ATR_PCT       = float(os.getenv("MAX_ATR_PCT", "3.00"))
MIN_VOL_SURGE     = float(os.getenv("MIN_VOL_SURGE", "1.30"))
MIN_SL_PCT        = float(os.getenv("MIN_SL_PCT", "0.40"))
MAX_SL_PCT        = float(os.getenv("MAX_SL_PCT", "3.00"))
RR_MIN            = float(os.getenv("RR_MIN", "1.80"))
MAX_CANDIDATES    = int(os.getenv("MAX_CANDIDATES", "25"))
SYMBOL_COOLDOWN   = int(os.getenv("SYMBOL_COOLDOWN_MIN", "240"))
STATE_FILE        = os.getenv("STATE_FILE", "signal_g5_okx_state.json")

TG_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CANDLES_1H  = 100
CANDLES_15M = 50

# ------------------------------------------------------------------ HTTP
def get_json(path, params=None, retries=2):
    for attempt in range(retries + 1):
        try:
            r = requests.get(
                BASE_URL + path, params=params, timeout=TIMEOUT,
                headers={"User-Agent": "SignalG5-OKX-Scanner/3.0"},
            )
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            payload = r.json()
            if payload.get("code") != "0":
                raise RuntimeError(f"OKX API error: {payload.get('msg', 'unknown')}")
            return payload.get("data", [])
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep(1.0)
    return []


def get_instruments():
    rows = get_json("/api/v5/public/instruments", {"instType": "SWAP"})
    return [
        r["instId"] for r in rows
        if r.get("settleCcy") == "USDT"
        and r.get("state") == "live"
        and r.get("instId", "").endswith("-USDT-SWAP")
    ]


def get_tickers():
    rows = get_json("/api/v5/market/tickers", {"instType": "SWAP"})
    return {r["instId"]: r for r in rows}


def fetch_candles(inst_id, bar, limit):
    rows = get_json(
        "/api/v5/market/candles",
        {"instId": inst_id, "bar": bar, "limit": str(limit)},
    )
    # OKX: newest first. Reverse → chronological. Keep only closed candles.
    rows = [r for r in rows if len(r) > 8 and r[8] == "1"]
    rows.reverse()
    out = []
    for r in rows:
        try:
            out.append({
                "ts":           int(r[0]),
                "open":         float(r[1]),
                "high":         float(r[2]),
                "low":          float(r[3]),
                "close":        float(r[4]),
                "volume":       float(r[5]),
                "quote_volume": float(r[7]) if r[7] else 0.0,
            })
        except (ValueError, IndexError):
            continue
    return out

# ------------------------------------------------------------------ INDICATORS
def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

# ------------------------------------------------------------------ PREFILTER
def prefilter(tickers, instruments):
    kept = []
    for inst_id in instruments:
        t = tickers.get(inst_id)
        if not t:
            continue
        try:
            vol   = float(t.get("volCcy24h") or 0)
            last  = float(t.get("last") or 0)
            ask   = float(t.get("askPx") or 0)
            bid   = float(t.get("bidPx") or 0)
            high  = float(t.get("high24h") or 0)
            low   = float(t.get("low24h") or 0)
        except ValueError:
            continue

        if vol < MIN_VOLUME or last <= 0 or ask <= 0 or bid <= 0:
            continue
        mid = (ask + bid) / 2.0
        if (ask - bid) / mid * 100.0 > MAX_SPREAD_PCT:
            continue
        if high <= 0 or low <= 0 or (high - low) / last * 100.0 < MIN_DAILY_RANGE:
            continue

        kept.append({"inst_id": inst_id, "volume_24h": vol, "ticker": t})

    kept.sort(key=lambda x: x["volume_24h"], reverse=True)
    return kept[:MAX_CANDIDATES]

# ------------------------------------------------------------------ ANALYSIS
def analyze(cand):
    inst_id = cand["inst_id"]
    h1  = fetch_candles(inst_id, "1H",  CANDLES_1H)
    m15 = fetch_candles(inst_id, "15m", CANDLES_15M)
    if len(h1) < 60 or len(m15) < 25:
        return None

    # --- 1H trend context ---
    closes_1h = [c["close"] for c in h1]
    ema20_1h  = ema(closes_1h, 20)
    ema50_1h  = ema(closes_1h, 50)
    if ema20_1h is None or ema50_1h is None:
        return None
    last_1h = closes_1h[-1]

    if last_1h > ema20_1h > ema50_1h:
        direction = "LONG"
    elif last_1h < ema20_1h < ema50_1h:
        direction = "SHORT"
    else:
        return None  # нет чёткого тренда на 1H — пропускаем

    # --- 15m entry confirmation ---
    last15 = m15[-1]
    closes_15 = [c["close"] for c in m15]
    ema20_15  = ema(closes_15, 20)
    if ema20_15 is None:
        return None

    a = atr(m15, 14)
    if not a or a <= 0:
        return None
    atr_pct = a / last15["close"] * 100.0
    if not (MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT):
        return None

    # Объёмный всплеск: последняя свеча vs среднее за 5 предыдущих
    prev_vols = [c["volume"] for c in m15[-6:-1]]
    avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 0.0
    if avg_vol <= 0:
        return None
    vol_surge = last15["volume"] / avg_vol
    if vol_surge < MIN_VOL_SURGE:
        return None

    entry = last15["close"]

    if direction == "LONG":
        bullish = last15["close"] > last15["open"]
        recent_high = max(c["high"] for c in m15[-15:-1])
        breakout = last15["close"] > recent_high
        reclaim  = last15["close"] > ema20_15 and last15["low"] < ema20_15
        if not bullish or not (breakout or reclaim):
            return None
        swing_low = min(c["low"] for c in m15[-10:])
        sl = swing_low - 0.3 * a
        sl_dist = entry - sl
        if sl_dist <= 0:
            return None
        tp = entry + sl_dist * RR_MIN
    else:  # SHORT
        bearish = last15["close"] < last15["open"]
        recent_low = min(c["low"] for c in m15[-15:-1])
        breakdown = last15["close"] < recent_low
        reclaim   = last15["close"] < ema20_15 and last15["high"] > ema20_15
        if not bearish or not (breakdown or reclaim):
            return None
        swing_high = max(c["high"] for c in m15[-10:])
        sl = swing_high + 0.3 * a
        sl_dist = sl - entry
        if sl_dist <= 0:
            return None
        tp = entry - sl_dist * RR_MIN

    sl_pct = sl_dist / entry * 100.0
    if not (MIN_SL_PCT <= sl_pct <= MAX_SL_PCT):
        return None

    tp_dist = abs(tp - entry)
    rr = tp_dist / sl_dist if sl_dist else 0.0

    # --- score ---
    trend_strength = abs(last_1h - ema50_1h) / ema50_1h * 100.0
    score = (
        2.0 * min(vol_surge, 5.0)
        + 1.5 * min(atr_pct / max(MIN_ATR_PCT, 1e-9), 4.0)
        + 1.2 * min(trend_strength, 5.0)
        + 1.0 * min(rr, 3.0)
        + 0.5 * min(cand["volume_24h"] / max(MIN_VOLUME, 1), 20.0) ** 0.5
    )

    return {
        "inst_id":     inst_id,
        "direction":   direction,
        "entry":       entry,
        "tp":          tp,
        "sl":          sl,
        "sl_pct":      sl_pct,
        "tp_pct":      tp_dist / entry * 100.0,
        "rr":          rr,
        "atr_pct":     atr_pct,
        "vol_surge":   vol_surge,
        "trend_str":   trend_strength,
        "volume_24h":  cand["volume_24h"],
        "score":       score,
        "breakout":    bool(breakout if direction == "LONG" else breakdown),
        "reclaim":     bool(reclaim),
    }

# ------------------------------------------------------------------ STATE
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

# ------------------------------------------------------------------ TELEGRAM
def telegram_send(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN и TELEGRAM_CHAT_ID обязательны.")
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")


def fmt_volume(v):
    if v >= 1e9: return f"{v/1e9:.1f}B"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return f"{v:.0f}"


def build_message(sig):
    arrow = "↑" if sig["direction"] == "LONG" else "↓"
    reasons = [f"1H тренд {arrow} (close > EMA20 > EMA50)"
               if sig["direction"] == "LONG"
               else f"1H тренд {arrow} (close < EMA20 < EMA50)"]
    if sig["breakout"]:
        reasons.append("15m пробой локального экстремума"
                       if sig["direction"] == "LONG"
                       else "15m пробой локального минимума")
    if sig["reclaim"]:
        reasons.append("15m ретест/возврат к EMA20")
    reasons.append(f"Всплеск объёма ×{sig['vol_surge']:.1f}")
    reasons.append(f"ATR {sig['atr_pct']:.2f}% — волатильность в норме")
    reasons.append(f"24ч объём: {fmt_volume(sig['volume_24h'])} USDT")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"🎯 SIGNAL G5 · {sig['inst_id']}\n"
        f"Направление: {sig['direction']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📍 Вход:  {sig['entry']:.6g}\n"
        f"🎯 TP:    {sig['tp']:.6g}  (+{sig['tp_pct']:.2f}%)\n"
        f"🛑 SL:    {sig['sl']:.6g}  (-{sig['sl_pct']:.2f}%)\n"
        f"⚖️ R:R    1:{sig['rr']:.2f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Почему:\n" + "\n".join(f"• {r}" for r in reasons) + "\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {stamp}\n"
        "⚠️ Авто-сигнал, не инвест-рекомендация. Соблюдай риск-менеджмент."
    )

# ------------------------------------------------------------------ MAIN
def scan():
    print("→ Загружаем инструменты и тикеры…")
    instruments = get_instruments()
    tickers = get_tickers()
    print(f"  USDT-перпетуалов live: {len(instruments)}")

    shortlist = prefilter(tickers, instruments)
    print(f"  После префильтра (ликвидность/спред/диапазон): {len(shortlist)}")

    if not shortlist:
        print("Нет монет, прошедших базовые фильтры.")
        return

    analyzed = []
    for i, cand in enumerate(shortlist, 1):
        try:
            sig = analyze(cand)
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(f"  [{i}/{len(shortlist)}] {cand['inst_id']}: ошибка {exc}")
            continue
        if sig:
            analyzed.append(sig)
            print(f"  [{i}/{len(shortlist)}] {sig['inst_id']} {sig['direction']} "
                  f"score={sig['score']:.2f} R:R=1:{sig['rr']:.2f}")
        else:
            print(f"  [{i}/{len(shortlist)}] {cand['inst_id']}: не прошёл анализ")
        time.sleep(0.08)

    if not analyzed:
        print("Итог: ни одна монета не дала сигнал по текущим фильтрам.")
        return

    analyzed.sort(key=lambda s: s["score"], reverse=True)
    state = load_state()
    history = state.get("history", {})
    now = int(time.time())

    # Отбрасываем недавно отправленные
    fresh = [s for s in analyzed
             if now - int(history.get(s["inst_id"], 0)) > SYMBOL_COOLDOWN * 60]
    if not fresh:
        print("Все лучшие кандидаты в cooldown — пропускаем.")
        return

    best = fresh[0]
    telegram_send(build_message(best))

    history[best["inst_id"]] = now
    # чистим историю старше 7 дней
    history = {k: v for k, v in history.items() if now - v < 7 * 86400}
    state.update({"last_sent_ts": now, "last_symbol": best["inst_id"],
                  "history": history})
    save_state(state)
    print(f"✅ Отправлен сигнал: {best['inst_id']} {best['direction']} "
          f"score={best['score']:.2f}")


if __name__ == "__main__":
    try:
        scan()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

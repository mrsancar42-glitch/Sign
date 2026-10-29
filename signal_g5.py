#!/usr/bin/env python3
"""
Signal G5 V5: Multi-Exchange Smart Scanner (Binance, Bybit, OKX).
Ориентирован на торговлю на Bybit. Внедрены скрытые дивергенции RSI, адаптивные тренды и мультибиржевой анализ.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

# Базовые URL публичных API для сбора данных по спотовым рынкам
BINANCE_URL = "https://api.binance.com"
BYBIT_URL = "https://api.bybit.com"
OKX_URL = "https://www.okx.com"

# Расширенный список ликвидных активов (базовые монеты). Торгуем их парами к USDT на Bybit.
COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", 
    "LTC", "BCH", "TRX", "ATOM", "NEAR", "APT", "ARB", "OP", "INJ", "SUI", 
    "TIA", "FIL", "UNI", "ETC", "AAVE", "SEI", "POL", "WLD", "FET", "RENDER",
    "SAND", "MANA", "GALA", "GRT", "STX", "ICP", "IMX", "FLOW", "EGLD", "THETA", 
    "FTM", "RUNE", "PEPE", "SHIB", "BONK", "WIF", "JUP", "PYTH", "TIA", "LDO"
]

ENTRY_INTERVAL = "15m"
MID_INTERVAL = "1h"
HIGH_INTERVAL = "4h"
KLINE_LIMIT = 250

MIN_SCORE = 7
MIN_RR = 1.6
MODEL_RR = 2.0
MAX_STRUCTURAL_RR = 4.0
MIN_TREND_ADX = 15.0
MIN_VOLUME_RATIO = 0.65
STRONG_VOLUME_RATIO = 1.1

STATE_PATH = Path(__file__).with_name("signal_g5_state.json")
LOCAL_TZ = ZoneInfo("Europe/Istanbul")
HTTP_TIMEOUT = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("signal-g5-multi")


def get_json(url: str, attempts: int = 2) -> Any:
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; MultiSignalG5/5.0)"},
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            if attempt + 1 < attempts:
                time.sleep(0.5)
    return None


def fetch_bybit_active_symbols() -> set[str]:
    """Получает список торгуемых USDT пар на Bybit"""
    url = f"{BYBIT_URL}/v5/market/instruments-info?category=spot"
    data = get_json(url)
    if not data or data.get("retCode") != 0:
        return set()
    return {
        item["symbol"] for item in data.get("result", {}).get("list", [])
        if item.get("status") == "Trading" and item.get("quoteCoin") == "USDT"
    }


def fetch_bybit_klines(symbol: str, interval: str) -> List[Dict[str, float]]:
    """Загружает исторические свечи с Bybit и форматирует их"""
    mapping = {"15m": "15", "1h": "60", "4h": "240"}
    bybit_interval = mapping.get(interval, "15")
    url = f"{BYBIT_URL}/v5/market/kline?category=spot&symbol={symbol}&interval={bybit_interval}&limit={KLINE_LIMIT}"
    data = get_json(url)
    if not data or data.get("retCode") != 0:
        return []
    
    rows = data.get("result", {}).get("list", [])
    now_ms = int(time.time() * 1000)
    result: List[Dict[str, float]] = []
    
    # Bybit возвращает данные от новых к старым, переворачиваем список
    for row in reversed(rows):
        open_time = int(row[0])
        # Приблизительное время закрытия свечи
        close_time = open_time + (int(bybit_interval) * 60 * 1000) - 1
        if close_time >= now_ms:
            continue
        result.append({
            "open_time": open_time,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "close_time": close_time,
        })
    return result


def fetch_bybit_price(symbol: str) -> float:
    url = f"{BYBIT_URL}/v5/market/tickers?category=spot&symbol={symbol}"
    data = get_json(url)
    if data and data.get("retCode") == 0:
        rows = data.get("result", {}).get("list", [])
        if rows:
            return float(rows[0]["lastPrice"])
    return 0.0


def fetch_cross_exchange_prices(coin: str) -> Dict[str, float]:
    """
    Параллельно собирает цену актива на Binance, Bybit и OKX для оценки арбитражного спреда
    и подтверждения истинности ценового движения (аномалии или общий рыночный тренд).
    """
    prices = {"binance": 0.0, "bybit": 0.0, "okx": 0.0}
    
    def get_binance():
        res = get_json(f"{BINANCE_URL}/api/v3/ticker/price?symbol={coin}USDT")
        return float(res["price"]) if res and "price" in res else 0.0

    def get_bybit():
        res = get_json(f"{BYBIT_URL}/v5/market/tickers?category=spot&symbol={coin}USDT")
        if res and res.get("retCode") == 0:
            list_ = res.get("result", {}).get("list", [])
            if list_: return float(list_[0]["lastPrice"])
        return 0.0

    def get_okx():
        res = get_json(f"{OKX_URL}/api/v5/market/ticker?instId={coin}-USDT")
        if res and res.get("code") == "0":
            data_ = res.get("data", [])
            if data_: return float(data_[0]["last"])
        return 0.0

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_bin = pool.submit(get_binance)
        f_byb = pool.submit(get_bybit)
        f_okx = pool.submit(get_okx)
        
        prices["binance"] = f_bin.result()
        prices["bybit"] = f_byb.result()
        prices["okx"] = f_okx.result()
        
    return prices


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    current = mean(values[:period])
    out[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for i in range(period, len(values)):
        current = alpha * values[i] + (1.0 - alpha) * current
        out[i] = current
    return out


def rsi_series(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = mean([max(x, 0.0) for x in changes[:period]])
    avg_loss = mean([max(-x, 0.0) for x in changes[:period]])
    
    out[period] = 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss))) if avg_loss > 0 else 100.0
    for i in range(period + 1, len(closes)):
        change = changes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        if avg_loss <= 1e-15:
            out[i] = 100.0 if avg_gain > 1e-15 else 50.0
        else:
            out[i] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return out


def atr_wilder(candles: Sequence[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs: List[float] = []
    for i in range(1, len(candles)):
        hi, lo, prev_close = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
    current = mean(trs[:period])
    for tr in trs[period:]:
        current = (current * (period - 1) + tr) / period
    return current


def dmi_adx(candles: Sequence[Dict[str, float]], period: int = 14) -> Optional[Tuple[float, float, float]]:
    if len(candles) < (2 * period + 2):
        return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        high, low = candles[i]["high"], candles[i]["low"]
        prev_high, prev_low, prev_close = candles[i - 1]["high"], candles[i - 1]["low"], candles[i - 1]["close"]
        up_move, down_move = high - prev_high, prev_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        trs.append(max(high - low, abs(high - prev_close), abs(lo - prev_close)))

    tr_s, plus_s, minus_s = sum(trs[:period]), sum(plus_dm[:period]), sum(minus_dm[:period])
    dx: List[float] = []

    def di_dx(tr: float, plus: float, minus: float) -> Tuple[float, float, float]:
        if tr <= 0: return 0.0, 0.0, 0.0
        pdi, mdi = 100.0 * plus / tr, 100.0 * minus / tr
        denom = pdi + mdi
        return (0.0 if denom <= 0 else 100.0 * abs(pdi - mdi) / denom), pdi, mdi

    dx.append(di_dx(tr_s, plus_s, minus_s))
    for i in range(period, len(trs)):
        tr_s = tr_s - tr_s / period + trs[i]
        plus_s = plus_s - plus_s / period + plus_dm[i]
        minus_s = minus_s - minus_s / period + minus_dm[i]
        dx.append(di_dx(tr_s, plus_s, minus_s))
        
    adx = mean(dx[:period])
    for value in dx[period:]:
        adx = (adx * (period - 1) + value) / period
    _, pdi, mdi = di_dx(tr_s, plus_s, minus_s)
    return adx, pdi, mdi


def check_smart_trend(candles: Sequence[Dict[str, float]]) -> Optional[str]:
    closes = [c["close"] for c in candles]
    ema50 = ema_series(closes, 50)
    if len(closes) < 55 or ema50[-1] is None or ema50[-5] is None:
        return None
    e50_now, e50_old = float(ema50[-1]), float(ema50[-5])
    close = closes[-1]
    if close > e50_now and e50_now > e50_old:
        return "LONG"
    if close < e50_now and e50_now < e50_old:
        return "SHORT"
    return None


def detect_hidden_divergence(entry_candles: Sequence[Dict[str, float]], side: str) -> bool:
    closes = [c["close"] for c in entry_candles]
    lows = [c["low"] for c in entry_candles]
    highs = [c["high"] for c in entry_candles]
    rsi = rsi_series(closes, 14)
    if len(closes) < 30 or rsi[-1] is None:
        return False
        
    if side == "LONG":
        current_low = min(lows[-4:])
        current_rsi = min([r for r in rsi[-4:] if r is not None])
        past_lows = lows[-25:-6]
        past_rsi = [r for r in rsi[-25:-6] if r is not None]
        if not past_rsi: return False
        if current_low > min(past_lows) and current_rsi < min(past_rsi):
            return True
    elif side == "SHORT":
        current_high = max(highs[-4:])
        current_rsi = max([r for r in rsi[-4:] if r is not None])
        past_highs = highs[-25:-6]
        past_rsi = [r for r in rsi[-25:-6] if r is not None]
        if not past_rsi: return False
        if current_high < max(past_highs) and current_rsi > max(past_rsi):
            return True
    return False


def pivot_levels(candles: Sequence[Dict[str, float]], side: str, bars: int = 60) -> List[float]:
    start = max(2, len(candles) - bars)
    levels: List[float] = []
    for i in range(start, len(candles) - 2):
        window = candles[i - 2:i + 3]
        value = window["high"] if side == "LONG" else window["low"]
        candidates = [x["high"] for x in window] if side == "LONG" else [x["low"] for x in window]
        if side == "LONG" and value == max(candidates): levels.append(value)
        elif side == "SHORT" and value == min(candidates): levels.append(value)
            
    levels.sort()
    deduped: List[float] = []
    for level in levels:
        if not deduped or abs(level - deduped[-1]) / max(abs(level), 1e-12) > 0.002:
            deduped.append(level)
    return deduped


def price_text(value: float) -> str:
    if value >= 1000: return f"{value:,.2f}"
    if value >= 100: return f"{value:.3f}"
    if value >= 1: return f"{value:.4f}"
    if value >= 0.01: return f"{value:.5f}"
    return f"{value:.8f}"


def analyze(symbol: str, entry: Sequence[Dict[str, float]], mid: Sequence[Dict[str, float]],
            high: Sequence[Dict[str, float]], cross_prices: Dict[str, float]) -> Optional[Dict[str, Any]]:
    if min(len(entry), len(mid), len(high)) < 60:
        return None

    trend4h = check_smart_trend(high)
    trend1h = check_smart_trend(mid)
    if not trend4h or trend4h != trend1h:
        return None
    side = trend4h

    closes = [c["close"] for c in entry]
    lows = [c["low"] for c in entry]
    highs = [c["high"] for c in entry]
    volumes = [c["volume"] for c in entry]
    opens = [c["open"] for c in entry]
    
    ema20_series = ema_series(closes, 20)
    if not ema20_series or ema20_series[-1] is None:
        return None
    e20 = float(ema20_series[-1])
    close = closes[-1]
    
    atr = atr_wilder(entry)
    rsi_list = rsi_series(closes, 14)
    dmi1h = dmi_adx(mid)
    
    if atr is None or atr <= 0 or not rsi_list or rsi_list[-1] is None or not dmi1h:
        return None
        
    rsi = rsi_list[-1]
    adx1h, plus1h, minus1h = dmi1h
    if adx1h < MIN_TREND_ADX:
        return None

    # Паттерны входа
    pullback_ema = False
    if side == "LONG" and min(lows[-3:]) <= e20 + (0.3 * atr) and close > opens[-1]: pullback_ema = True
    elif side == "SHORT" and max(highs[-3:]) >= e20 - (0.3 * atr) and close < opens[-1]: pullback_ema = True
        
    has_divergence = detect_hidden_divergence(entry, side)
    if not pullback_ema and not has_divergence:
        return None

    prior_volume = mean(volumes[-20:-3])
    recent_volume = mean(volumes[-3:])
    volume_ratio = recent_volume / prior_volume if prior_volume > 0 else 1.0
    if volume_ratio < MIN_VOLUME_RATIO:
        return None

    # Мультибиржевая валидация спреда (убираем фейковые прострелы на одной бирже)
    bybit_p = cross_prices.get("bybit", 0.0)
    binance_p = cross_prices.get("binance", 0.0)
    okx_p = cross_prices.get("okx", 0.0)
    
    cross_score = 0
    if bybit_p > 0 and binance_p > 0 and okx_p > 0:
        spread_bin = abs(bybit_p - binance_p) / bybit_p * 100
        spread_okx = abs(bybit_p - okx_p) / bybit_p * 100
        # Если спред между биржами аномально огромен (> 0.5%), отсекаем из-за неликвидности или бага
        if spread_bin > 0.5 or spread_okx > 0.5:
            return None
        cross_score = 1

    score = 6 + cross_score
    if pullback_ema: score += 1
    if has_divergence: score += 1
    if volume_ratio >= STRONG_VOLUME_RATIO: score += 1
    
    if score < MIN_SCORE:
        return None

    if side == "LONG":
        sl = min(lows[-5:]) - (0.1 * atr)
        risk = close - sl
    else:
        sl = max(highs[-5:]) + (0.1 * atr)
        risk = sl - close
        
    if risk <= 0 or risk > (3.0 * atr):
        return None

    levels = pivot_levels(mid, side)
    directional_levels = [x for x in levels if x > close] if side == "LONG" else [x for x in levels if x < close]
    if side == "LONG": directional_levels.sort()
    else: directional_levels.sort(reverse=True)
        
    nearest_level = directional_levels if directional_levels else None
    if nearest_level is not None:
        level_rr = abs(nearest_level - close) / risk
        if level_rr >= MIN_RR and level_rr <= MAX_STRUCTURAL_RR:
            tp = nearest_level
            target_basis = "уровень Bybit 1H"
        else:
            tp = close + MODEL_RR * risk if side == "LONG" else close - MODEL_RR * risk
            target_basis = "модельный 2.0R (цель ближе уровня)"
    else:
        tp = close + MODEL_RR * risk if side == "LONG" else close - MODEL_RR * risk
        target_basis = "математический 2.0R (нет уровней)"

    rr = abs(tp - close) / risk
    if rr < MIN_RR:
        return None

    # Формируем список подтверждений от бирж
    exch_status = f"Bybit: {price_text(bybit_p)} | Bin: {price_text(binance_p)} | OKX: {price_text(okx_p)}"

    return {
        "symbol": symbol, "side": side, "entry": close, "tp": tp, "sl": sl,
        "rr": round(rr, 2), "score": score, "rsi": rsi, "adx1h": adx1h,
        "volume_ratio": volume_ratio, "atr": atr, "risk_pct": risk / close * 100.0,
        "reward_pct": abs(tp - close) / close * 100.0, "target_basis": target_basis, 
        "reason": f"{side} тренд; " + ("Дивергенция RSI" if has_divergence else "Тест EMA20") + f"; {exch_status}",
        "candle_close_time": int(entry[-1]["close_time"]),
    }


def load_state() -> Dict[str, Any]:
    today = datetime.now(LOCAL_TZ).date().isoformat()
    if not STATE_PATH.exists():
        return {"date_local": today, "sent_symbols": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"date_local": today, "sent_symbols": {}}
    if state.get("date_local") != today:
        return {"date_local": today, "sent_symbols": {}}
    return state


def save_state(state: Dict[str, Any]) -> None:
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "
", encoding="utf-8")
    temp_path.replace(STATE_PATH)


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOG.error("Telegram токен не найден в GitHub Secrets")
        return False
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(endpoint, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result.get("ok") is True
    except Exception as exc:
        LOG.error("Ошибка отправки: %s", exc)
        return False


def build_message(signals: Sequence[Dict[str, Any]], daily_count_before: int) -> str:
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M TRT")
    lines = [f"📡 MULTI-EXCHANGE SIGNAL G5 | BYBIT SPOT | {now}", "Анализ: Bybit + Binance + OKX • Вход: 15M", ""]
    for signal in signals:
        emoji = "🟢" if signal["side"] == "LONG" else "🔴"
        close_time = datetime.fromtimestamp(signal["candle_close_time"] / 1000, timezone.utc).astimezone(LOCAL_TZ).strftime("%H:%M TRT")
        lines.append(f"{emoji} {signal['symbol']} {signal['side']} | Мульти-Скор: {signal['score']}/10")
        lines.append(f"Вход (Bybit): {price_text(signal['entry'])} | TP: {price_text(signal['tp'])} (+{signal['reward_pct']:.2f}%)")
        lines.append(f"SL: {price_text(signal['sl'])} (-{signal['risk_pct']:.2f}%) | R:R: {signal['rr']}")
        lines.append(f"Сверка данных: {signal['reason']}")
        lines.append(f"Обоснование ТР: {signal['target_basis']} | Клоуз 15M: {close_time}")
        lines.append("")
    count_after = daily_count_before + len(signals)
    lines.append(f"Сигналов за сегодня: {count_after}/40. Защита от дублирования активна.")
    return "\n".join(lines).strip()


def fetch_coin_data(coin: str) -> Tuple[str, List[Dict[str, float]], List[Dict[str, float]], List[Dict[str, float]], Dict[str, float], Optional[str]]:
    symbol = f"{coin}USDT"
    try:
        # Получаем срезы стаканов/цен со всех трех бирж
        cross_prices = fetch_cross_exchange_prices(coin)
        
        # Исторические данные для индикаторов берем напрямую с целевой биржи исполнения (Bybit)
        with ThreadPoolExecutor(max_workers=3) as pool:
            f15 = pool.submit(fetch_bybit_klines, symbol, ENTRY_INTERVAL)
            f1h = pool.submit(fetch_bybit_klines, symbol, MID_INTERVAL)
            f4h = pool.submit(fetch_bybit_klines, symbol, HIGH_INTERVAL)
            return symbol, f15.result(), f1h.result(), f4h.result(), cross_prices, None
    except Exception as exc:
        return symbol, [], [], [], {}, str(exc)


def main() -> int:
    try:
        state = load_state()
        save_state(state)
    except Exception as exc:
        LOG.error("Ошибка инициализации стейта: %s", exc)
        return 2

    already_sent: Dict[str, Any] = state["sent_symbols"]
    daily_count = len(already_sent)
    if daily_count >= 40:
        LOG.info("Дневной лимит (40 сигналов) достигнут.")
        return 0

    try:
        bybit_active = fetch_bybit_active_symbols()
    except Exception as exc:
        LOG.error("Не удалось получить рынки Bybit: %s", exc)
        return 1
        
    # Фильтруем монеты, которые торгуются на Bybit и еще не отправлялись сегодня
    eligible_coins = [c for c in COINS if f"{c}USDT" in bybit_active and f"{c}USDT" not in already_sent]
    if not eligible_coins:
        return 0

    passing: List[Dict[str, Any]] = []
    
    # Сканируем пулы данных
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fetch_coin_data, coin) for coin in eligible_coins]
        for future in as_completed(futures):
            symbol, entry, mid, high, cross_prices, error = future.result()
            if error or not entry or not mid or not high:
                continue
            try:
                signal = analyze(symbol, entry, mid, high, cross_prices)
            except Exception:
                continue
            if signal:
                passing.append(signal)

    passing.sort(key=lambda x: (x["score"], x["rr"], x["volume_ratio"]), reverse=True)
    chosen = passing[:min(6, 40 - daily_count)]
    if not chosen:
        return 0

    message = build_message(chosen, daily_count)
    print(message)
    
    if not send_telegram(message):
        return 1

    now = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    for signal in chosen:
        already_sent[signal["symbol"]] = {
            "side": signal["side"],
            "sent_at": now,
            "score": signal["score"],
        }
    state["sent_symbols"] = already_sent
    state["last_updated"] = now
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Signal G7 — OKX Swing Scanner: ICT + трендовый гибрид (среднесрок: дни–недели)

Идея: старший таймфрейм (1D) задаёт ТОЛЬКО направление (тренд монеты + режим BTC +
относительная сила), а вход ищется двумя способами:

  1) ICT-вход на 4H (лонг; шорт — зеркально):
       свип ликвидности (прокол недавнего swing-low с закрытием обратно выше)
       -> MSS/смещение (закрытие выше внутреннего максимума, импульсная свеча)
       -> первый откат в FVG (или order block) в зоне DISCOUNT (ниже 50% диапазона).
     Стоп — за экстремум свипа, цель — ближайшая внешняя ликвидность (swing-high)
     с RR не меньше ICT_MIN_RR.
  2) Трендовый вход на 1D (как в G6): пробой 20д-канала с объёмом
     или откат к EMA20 внутри тренда.

Режимы (--mode=trend|ict|hybrid, по умолчанию hybrid):
  trend  — только дневные трендовые сетапы (базовая версия G6)
  ict    — только ICT-входы под дневным фильтром
  hybrid — оба источника, ранжирование по силе и RR

ВАЖНО: ICT-правила субъективны, строгих исследований на крипте нет. Здесь они
формализованы жёстко и без подгонки, но ценность проверяется ТОЛЬКО бэктестом
(--backtest сравнивает trend / ict / hybrid на одной выборке с комиссиями).

Скрипт ничего не торгует и не размещает ордера.

Запуск (cron каждые 4 часа через 5 минут после закрытия 4H-свечи, UTC):
  5 0,4,8,12,16,20 * * *  /usr/bin/python3 /path/signal_g7.py

Аргументы:
  --dry          пробный прогон: ВЕСЬ вывод и найденные сигналы уходят в Telegram
                 с пометкой DRY-RUN; состояние (позиции, пауза) НЕ сохраняется
  --mode=...     trend | ict | hybrid
  --backtest     сравнить режимы на истории OKX (BT_DAYS, BT_TOP, BT_INSTS)
  --stats        статистика закрытых сигналов из state-файла
  --force        искать сигналы, даже если эта 4H-свеча уже обработана

Переменные окружения:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  MODE                 trend|ict|hybrid                          (hybrid)
  EQUITY_USDT          размер счёта                              (1000)
  RISK_PCT             риск на сделку, % счёта                   (1.0)
  MAX_LEVERAGE         потолок плеча                             (3)
  MAX_OPEN             макс. одновременных сигналов              (4)
  MAX_NEW_PER_RUN      макс. новых сигналов за запуск            (2)
  TOP_N                сколько самых ликвидных монет             (40)
  MIN_TURNOVER_USDT    мин. суточный оборот                      (20000000)
  ALLOW_SHORT          1 = шорты при медвежьем BTC               (0)
  ATR_STOP_MULT        дневной стоп в ATR (trend-сетапы)         (2.5)
  MAX_STOP_PCT         макс. ширина стопа, %                     (18)
  ICT_MIN_RR           мин. RR до цели-ликвидности               (2.0)
  MAX_FUNDING_PCT      макс. фандинг против позиции, %/период    (0.05)
  SYMBOL_COOLDOWN_DAYS пауза по монете после сигнала             (5)
  MAX_HOLD_DAYS        обзор позиции без TP1 через N дней        (30)
  STATE_FILE           (signal_g7_state.json)
"""

import html
import json
import math
import os
import sys
import time
from bisect import bisect_right
from copy import deepcopy

import requests

# ----------------------------------------------------------------- настройки
BASE_URL = "https://www.okx.com"
TIMEOUT = 20
DAY = 86_400_000
H4 = 4 * 3_600_000


def _f(name, default):
    return float(os.getenv(name, str(default)))


EQUITY = _f("EQUITY_USDT", 1000)
RISK_PCT = _f("RISK_PCT", 1.0)
MAX_LEV = _f("MAX_LEVERAGE", 3)
MAX_OPEN = int(_f("MAX_OPEN", 4))
MAX_NEW = int(_f("MAX_NEW_PER_RUN", 2))
TOP_N = int(_f("TOP_N", 40))
MIN_TURNOVER = _f("MIN_TURNOVER_USDT", 20_000_000)
ALLOW_SHORT = os.getenv("ALLOW_SHORT", "0") == "1"
ATR_STOP = _f("ATR_STOP_MULT", 2.5)
MAX_STOP_PCT = _f("MAX_STOP_PCT", 18)
ICT_MIN_RR = _f("ICT_MIN_RR", 2.0)
MAX_FUNDING = _f("MAX_FUNDING_PCT", 0.05)
COOLDOWN_DAYS = _f("SYMBOL_COOLDOWN_DAYS", 5)
MAX_HOLD_DAYS = _f("MAX_HOLD_DAYS", 30)
STATE_FILE = os.getenv("STATE_FILE", "signal_g7_state.json")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TP1_R = 1.5          # первая цель в R (50% позиции, стоп в безубыток)
TP2_R = 3.0          # минимальная вторая цель в R
TRAIL_ATR = 3.0      # chandelier-трейлинг остатка (дневной ATR)
DON_N = 20           # канал пробоя (1D)
RS_N = 21            # окно относительной силы (3 недели)
FEE_ROUNDTRIP_PCT = 0.12   # комиссии + проскальзывание туда-обратно (оценка)
STABLES = {"USDC", "DAI", "TUSD", "FDUSD", "USDE", "USDG"}
MIN_BARS = 110       # прогрев дневных индикаторов

# --- ICT (4H)
PIV_K = 3                 # фрактал: 3 свечи слева и справа
ICT_LOOKBACK = 60         # swing-low не старше N 4H-свечей
ICT_MAX_SWEEP_AGE = 40    # свип не старше N свечей
ICT_MAX_AGE = 12          # откат не позже N свечей после MSS (2 суток)
ICT_TARGET_LOOKBACK = 150 # где ищем внешнюю ликвидность (25 суток)
ICT_MIN_RISK_ATR = 0.8    # стоп не уже 0.8 ATR(4H) — иначе это шум
ICT_MAX_RISK_ATR = 6.0    # и не шире 6 ATR(4H)
ICT_DISPLACEMENT = 0.8    # тело импульса >= 0.8 ATR
MODES = ("trend", "ict", "hybrid")

DRY = "--dry" in sys.argv
MODE = os.getenv("MODE", "hybrid").strip().lower()
for _a in sys.argv:
    if _a.startswith("--mode="):
        MODE = _a.split("=", 1)[1].strip().lower()
if MODE not in MODES:
    print(f"Неизвестный режим {MODE!r}; использую hybrid")
    MODE = "hybrid"

LOG = []


def log(*parts):
    line = " ".join(str(p) for p in parts)
    print(line)
    LOG.append(line)


# ----------------------------------------------------------------- OKX API
def get_json(path, params=None, retries=4):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(
                BASE_URL + path, params=params, timeout=TIMEOUT,
                headers={"User-Agent": "SignalG7-Swing-Scanner/1.0"},
            )
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            payload = r.json()
            code = payload.get("code")
            if code == "50011":            # rate limit
                time.sleep(1.5 * (attempt + 1))
                continue
            if code != "0":
                raise RuntimeError(f"OKX {path}: {payload.get('msg')} ({code})")
            return payload.get("data", [])
        except (requests.RequestException, ValueError) as exc:
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"OKX request failed: {path}: {last}")


def get_instruments():
    rows = get_json("/api/v5/public/instruments", {"instType": "SWAP"})
    out = {}
    for r in rows:
        iid = r.get("instId", "")
        if (r.get("settleCcy") == "USDT" and r.get("state") == "live"
                and iid.endswith("-USDT-SWAP")):
            if iid.split("-")[0] in STABLES:
                continue
            out[iid] = {
                "ctVal": float(r["ctVal"]), "ctMult": float(r.get("ctMult") or 1),
                "lotSz": float(r["lotSz"]), "minSz": float(r["minSz"]),
            }
    return out


def get_universe(instruments):
    """Топ-N по суточному обороту. Оборот считаем сами: контракты*ctVal*ctMult*цена."""
    rows = get_json("/api/v5/market/tickers", {"instType": "SWAP"})
    ranked = []
    for t in rows:
        iid = t.get("instId")
        ins = instruments.get(iid)
        if not ins:
            continue
        try:
            turnover = float(t["vol24h"]) * ins["ctVal"] * ins["ctMult"] * float(t["last"])
        except (KeyError, ValueError):
            continue
        if turnover >= MIN_TURNOVER:
            ranked.append((turnover, iid, float(t["last"])))
    ranked.sort(reverse=True)
    return ranked[:TOP_N]


def parse_candles(rows):
    """OKX: новые первыми; [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]. Берём закрытые."""
    out = []
    for r in rows:
        if len(r) > 8 and r[8] == "1":
            out.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                        float(r[4]), float(r[5] or 0)))
    out.sort()
    return out


def get_candles(inst, bar, limit=300):
    rows = get_json("/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": str(limit)})
    return parse_candles(rows)


def get_history(inst, bar, total):
    """Постраничная история для бэктеста (history-candles, по 100)."""
    got, after = {}, None
    while len(got) < total:
        params = {"instId": inst, "bar": bar, "limit": "100"}
        if after:
            params["after"] = str(after)
        rows = get_json("/api/v5/market/history-candles", params)
        if not rows:
            break
        for c in parse_candles(rows):
            got[c[0]] = c
        oldest = min(int(r[0]) for r in rows)
        if after is not None and oldest >= after:
            break
        after = oldest
        time.sleep(0.15)
    return [got[k] for k in sorted(got)]


def get_funding(inst):
    try:
        rows = get_json("/api/v5/public/funding-rate", {"instId": inst})
        return float(rows[0]["fundingRate"]) * 100.0   # % за период
    except Exception:
        return None


# ----------------------------------------------------------------- индикаторы
def ema(values, n):
    k = 2.0 / (n + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def atr_series(candles, n=14):
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c[2] - c[3])
        else:
            pc = candles[i - 1][4]
            trs.append(max(c[2] - c[3], abs(c[2] - pc), abs(c[3] - pc)))
    out, prev = [], None
    for i, tr in enumerate(trs):
        if i < n:
            prev = sum(trs[:i + 1]) / (i + 1)
        else:
            prev = (prev * (n - 1) + tr) / n
        out.append(prev)
    return out


def build_indicators(candles):
    """Дневные индикаторы (трендовая часть)."""
    o = [c[1] for c in candles]
    h = [c[2] for c in candles]
    l = [c[3] for c in candles]
    cl = [c[4] for c in candles]
    v = [c[5] for c in candles]
    n = len(candles)
    don_hi = [None] * n
    don_lo = [None] * n
    vol_ma = [None] * n
    for i in range(n):
        if i >= DON_N:
            don_hi[i] = max(h[i - DON_N:i])
            don_lo[i] = min(l[i - DON_N:i])
        if i >= 20:
            vol_ma[i] = sum(v[i - 20:i]) / 20
    return {
        "ts": [c[0] for c in candles], "o": o, "h": h, "l": l, "c": cl, "v": v,
        "ema20": ema(cl, 20), "ema50": ema(cl, 50), "ema100": ema(cl, 100),
        "atr": atr_series(candles, 14),
        "don_hi": don_hi, "don_lo": don_lo, "vol_ma": vol_ma,
    }


def btc_regime(ind, i=None):
    """bull / bear / neutral по дневному BTC."""
    if i is None:
        i = len(ind["c"]) - 1
    if i < MIN_BARS:
        return "neutral"
    c, e50, e100 = ind["c"][i], ind["ema50"][i], ind["ema100"][i]
    if c > e50 and e50 > e100:
        return "bull"
    if c < e50 and e50 < e100:
        return "bear"
    return "neutral"


def trend_gate(ind, i, side, regime):
    """Дневной фильтр направления: тренд монеты + режим BTC."""
    if i < MIN_BARS or i >= len(ind["c"]):
        return False
    c, e50, e100 = ind["c"][i], ind["ema50"][i], ind["ema100"][i]
    if side == "long":
        return regime != "bear" and c > e50 and e50 > e100 and e50 > ind["ema50"][i - 5]
    return ALLOW_SHORT and regime == "bear" and c < e50 and e50 < e100 and e50 < ind["ema50"][i - 5]


# ----------------------------------------------------------------- сетап 1: дневной тренд
def signal_at(ind, i, regime, side):
    """Дневной трендовый сетап на закрытой свече i (пробой / откат к EMA20)."""
    if i < MIN_BARS or ind["don_hi"][i] is None or ind["vol_ma"][i] is None:
        return None
    if not trend_gate(ind, i, side, regime):
        return None
    c, o, h, l = ind["c"][i], ind["o"][i], ind["h"][i], ind["l"][i]
    e20 = ind["ema20"][i]
    a = ind["atr"][i]
    if a <= 0 or c <= 0:
        return None
    ext = (c - e20) / a if side == "long" else (e20 - c) / a   # насколько цена «убежала»
    if ext > 3.0:
        return None

    if side == "long":
        breakout = c > ind["don_hi"][i] and ind["v"][i] >= ind["vol_ma"][i]
        had_break = any(
            ind["don_hi"][j] is not None and ind["c"][j] > ind["don_hi"][j]
            for j in range(i - 15, i)
        )
        pullback = had_break and l <= e20 + 0.25 * a and c > e20 and c > o and not breakout
    else:
        breakout = c < ind["don_lo"][i] and ind["v"][i] >= ind["vol_ma"][i]
        had_break = any(
            ind["don_lo"][j] is not None and ind["c"][j] < ind["don_lo"][j]
            for j in range(i - 15, i)
        )
        pullback = had_break and h >= e20 - 0.25 * a and c < e20 and c < o and not breakout

    if not (breakout or pullback):
        return None
    setup = "Пробой 20д-канала" if breakout else "Откат к EMA20 в тренде"
    entry = c
    risk = ATR_STOP * a
    if side == "long":
        stop = entry - risk
        tp1, tp2 = entry + TP1_R * risk, entry + TP2_R * risk
    else:
        stop = entry + risk
        tp1, tp2 = entry - TP1_R * risk, entry - TP2_R * risk
    stop_pct = risk / entry * 100
    if stop <= 0 or stop_pct > MAX_STOP_PCT:
        return None
    return {
        "src": "trend", "side": side, "setup": setup, "entry": entry, "stop": stop,
        "tp1": tp1, "tp2": tp2, "risk": risk, "stop_pct": stop_pct,
        "atr": a, "ts": ind["ts"][i],
    }


# ----------------------------------------------------------------- сетап 2: ICT на 4H
def build_core(candles):
    """Массивы + фракталы для ICT-логики (работаем в «лонговом» пространстве)."""
    n = len(candles)
    o = [c[1] for c in candles]
    h = [c[2] for c in candles]
    l = [c[3] for c in candles]
    cl = [c[4] for c in candles]
    pl, ph = [], []
    for j in range(PIV_K, n - PIV_K):
        if l[j] == min(l[j - PIV_K:j + PIV_K + 1]) and l[j] < min(l[j - PIV_K:j]):
            pl.append(j)
        if h[j] == max(h[j - PIV_K:j + PIV_K + 1]) and h[j] > max(h[j - PIV_K:j]):
            ph.append(j)
    return {"ts": [c[0] for c in candles], "o": o, "h": h, "l": l, "c": cl,
            "atr": atr_series(candles, 14), "pl": pl, "ph": ph}


def build_ict(candles):
    """L — реальные цены (лонг). S — зеркало (-цена): шорт ищется той же логикой лонга."""
    neg = [(c[0], -c[1], -c[3], -c[2], -c[4], c[5]) for c in candles]
    return {"L": build_core(candles), "S": build_core(neg)}


def ict_core(x, i):
    """Лонговый ICT-сетап на закрытой 4H-свече i: свип -> MSS -> откат в FVG/OB (discount)."""
    if i < 40 or i >= len(x["c"]):
        return None
    o, h, l, c, atr = x["o"], x["h"], x["l"], x["c"], x["atr"]
    a = atr[i]
    if a <= 0:
        return None

    for p in reversed(x["pl"]):
        if p > i - PIV_K:                    # фрактал ещё не подтверждён
            continue
        if p < i - ICT_LOOKBACK:
            break
        ref = l[p]                           # ликвидность под минимумом

        # 1) свип: первый прокол ref с закрытием обратно выше
        s = None
        for j in range(p + 1, i - 1):
            if l[j] < ref:
                s = j
                break
        if s is None or c[s] <= ref or i - s > ICT_MAX_SWEEP_AGE:
            continue
        sweep_low = l[s]
        if min(l[s + 1:i + 1]) <= sweep_low:  # экстремум свипа обновили — идея сломана
            continue

        # 2) MSS: импульсное закрытие выше внутреннего максимума перед свипом
        h_struct = max(h[p:s])
        m = None
        for j in range(s + 1, i):
            if c[j] > h_struct and (c[j] - o[j]) >= ICT_DISPLACEMENT * atr[j]:
                m = j
                break
        if m is None or i - m > ICT_MAX_AGE:
            continue

        # 3) зона входа: FVG импульса (приоритет) или order block (тело последней красной)
        zone = None
        for t in range(min(m + 1, i - 1), m - 1, -1):
            if t - 2 >= s and l[t] > h[t - 2] and (l[t] - h[t - 2]) >= 0.1 * atr[t]:
                zone = ("FVG", h[t - 2], l[t], t)
                break
        if zone is None:
            for j in range(m - 1, s - 1, -1):
                if c[j] < o[j]:
                    zone = ("OB", c[j], o[j], m)
                    break
        if zone is None:
            continue
        ztype, zlo, zhi, f = zone

        # 4) первое касание зоны: между формированием и i цена её не трогала
        if any(l[j] <= zhi for j in range(f + 1, i)):
            continue
        if not (i > f and l[i] <= zhi and c[i] >= zlo and l[i] > sweep_low):
            continue

        # 5) premium/discount: зона ниже 50% диапазона [sweep_low .. максимум]
        rng_hi = max(h[max(0, s - 40):i + 1])
        if (zlo + zhi) / 2.0 > (sweep_low + rng_hi) / 2.0:
            continue

        # 6) стоп за свипом, цель — ближайшая внешняя ликвидность с RR >= ICT_MIN_RR
        entry = c[i]
        stop = sweep_low - 0.25 * a
        risk = entry - stop
        if risk < ICT_MIN_RISK_ATR * a or risk > ICT_MAX_RISK_ATR * a:
            continue
        highs = sorted(h[q] for q in x["ph"]
                       if i - ICT_TARGET_LOOKBACK <= q <= i - PIV_K and h[q] > entry)
        liq = next((hv for hv in highs if (hv - entry) / risk >= ICT_MIN_RR), None)
        if liq is None:
            continue
        return {
            "entry": entry, "stop": stop, "risk": risk, "atr": a,
            "tp1": entry + TP1_R * risk, "tp2": max(liq, entry + TP2_R * risk),
            "liq": liq, "rr_liq": (liq - entry) / risk,
            "ref": ref, "sweep": sweep_low, "ztype": ztype, "zlo": zlo, "zhi": zhi,
            "ts": x["ts"][i],
        }
    return None


def ict_signal(ict, i, side):
    """Обёртка: лонг на реальных ценах, шорт — на зеркале с обратным преобразованием."""
    r = ict_core(ict["L"] if side == "long" else ict["S"], i)
    if not r:
        return None
    k = 1.0 if side == "long" else -1.0
    entry, stop = k * r["entry"], k * r["stop"]
    if entry <= 0 or stop <= 0:
        return None
    stop_pct = r["risk"] / entry * 100.0
    if stop_pct > MAX_STOP_PCT:
        return None
    zlo, zhi = k * r["zlo"], k * r["zhi"]
    if side == "short":
        zlo, zhi = zhi, zlo
    return {
        "src": "ict", "side": side,
        "setup": f"ICT: свип ликвидности + MSS + {r['ztype']} (4H)",
        "entry": entry, "stop": stop, "tp1": k * r["tp1"], "tp2": k * r["tp2"],
        "risk": r["risk"], "stop_pct": stop_pct, "atr": r["atr"], "ts": r["ts"],
        "info": {"ref": k * r["ref"], "sweep": k * r["sweep"], "ztype": r["ztype"],
                 "zlo": zlo, "zhi": zhi, "liq": k * r["liq"], "rr_liq": r["rr_liq"]},
    }


# ----------------------------------------------------------------- размер позиции
def size_position(price, stop_pct, ins):
    risk_usdt = EQUITY * RISK_PCT / 100.0
    notional = risk_usdt / (stop_pct / 100.0)
    lev = notional / EQUITY
    capped = False
    if lev > MAX_LEV:
        notional, lev, capped = EQUITY * MAX_LEV, MAX_LEV, True
    unit = price * ins["ctVal"] * ins["ctMult"]
    contracts = math.floor(notional / unit / ins["lotSz"] + 1e-9) * ins["lotSz"]
    if contracts < ins["minSz"]:
        return None
    notional = contracts * unit
    real_risk = notional * stop_pct / 100.0
    return {
        "contracts": contracts, "notional": notional, "lev": notional / EQUITY,
        "risk_usdt": real_risk, "risk_pct": real_risk / EQUITY * 100, "capped": capped,
    }


# ----------------------------------------------------------------- состояние / Telegram
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError):
        st = {}
    st.setdefault("open", {})
    st.setdefault("closed", [])
    st.setdefault("cooldown", {})
    st.setdefault("last_scan_bar", 0)
    return st


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _plain(text):
    for t in ("<b>", "</b>", "<code>", "</code>"):
        text = text.replace(t, "")
    return html.unescape(text)


def _chunks(text, limit=3800):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def tg_send(text):
    """Отправка в Telegram (HTML). В dry-режиме добавляет пометку DRY-RUN."""
    if DRY:
        text = "🧪 <b>DRY-RUN</b> · позиции не записываются\n" + text
    if not TG_TOKEN or not TG_CHAT:
        print(_plain(text))
        print("-" * 40)
        print("(Telegram не настроен: задайте TELEGRAM_TOKEN и TELEGRAM_CHAT_ID)")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for chunk in _chunks(text):
        r = requests.post(url, json={"chat_id": TG_CHAT, "text": chunk, "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=TIMEOUT)
        r.raise_for_status()
        time.sleep(1.1)


def fmt(p):
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.6f}".rstrip("0").rstrip(".")


def name(inst):
    return inst.replace("-USDT-SWAP", "/USDT")


def fmt_signal(inst, sig, size, rs_pct, regime, funding):
    long_ = sig["side"] == "long"
    arrow = "🟢 LONG" if long_ else "🔴 SHORT"
    lines = [
        f"{arrow} <b>{name(inst)}</b> — среднесрок (дни–недели)",
        f"Сетап: {sig['setup']}",
        f"Режим BTC: {regime} · RS 21д: {'топ' if long_ else 'низ'} {rs_pct:.0f}%",
    ]
    info = sig.get("info")
    if info:
        zone_side = "discount" if long_ else "premium"
        lines += [
            f"Свип: снят уровень <code>{fmt(info['ref'])}</code>, экстремум <code>{fmt(info['sweep'])}</code>",
            f"Зона {info['ztype']}: <code>{fmt(info['zlo'])}</code> – <code>{fmt(info['zhi'])}</code> ({zone_side})",
            f"Цель-ликвидность: <code>{fmt(info['liq'])}</code> (RR {info['rr_liq']:.1f})",
        ]
    lines += [
        "",
        f"Вход: <code>{fmt(sig['entry'])}</code> (не гнаться, если цена ушла дальше 0.5 ATR)",
        f"Стоп: <code>{fmt(sig['stop'])}</code> ({sig['stop_pct']:.1f}%)",
        f"TP1: <code>{fmt(sig['tp1'])}</code> ({TP1_R:g}R) — закрыть 50%, стоп в безубыток",
        f"TP2: <code>{fmt(sig['tp2'])}</code> — остаток по трейлингу {TRAIL_ATR:g} ATR",
        "",
        f"Размер: {size['contracts']:g} контр. ≈ {size['notional']:,.0f} USDT, плечо ≈ {size['lev']:.1f}x",
        f"Риск: {size['risk_usdt']:.1f} USDT ({size['risk_pct']:.2f}% счёта)"
        + (" — урезан лимитом плеча" if size["capped"] else ""),
        "Маржа: isolated. Ликвидация должна быть заметно дальше стопа.",
    ]
    if funding is not None:
        lines.append(f"Фандинг: {funding:+.4f}% за период")
    lines += ["", "Идея для ручной проверки, не гарантия результата. Прибыль системы "
                  "даёт хвост крупных выигрышей, серии стопов — норма."]
    return "\n".join(lines)


# ----------------------------------------------------------------- сканирование
def rs_return(ind):
    c = ind["c"]
    if len(c) <= RS_N:
        return None
    return c[-1] / c[-1 - RS_N] - 1.0


def scan_new(state, instruments, universe, force=False):
    btc4 = get_candles("BTC-USDT-SWAP", "4H", 20)
    if not btc4:
        raise RuntimeError("Нет 4H-данных BTC")
    bar_ts = btc4[-1][0]
    if not force and state.get("last_scan_bar") == bar_ts:
        log("Эта 4H-свеча уже обработана; пропускаю поиск новых сигналов.")
        return []

    btc = get_candles("BTC-USDT-SWAP", "1Dutc", 300)
    if len(btc) < MIN_BARS:
        raise RuntimeError("Недостаточно дневных данных BTC")
    regime = btc_regime(build_indicators(btc))
    log(f"Режим: {MODE} · режим BTC: {regime} · монет в списке: {len(universe)}")

    daily, h4 = {}, {}
    for _, inst, _ in universe:
        try:
            cs = get_candles(inst, "1Dutc", 300)
            if len(cs) >= MIN_BARS:
                daily[inst] = build_indicators(cs)
                if MODE in ("ict", "hybrid"):
                    c4 = get_candles(inst, "4H", 300)
                    if len(c4) >= 80:
                        h4[inst] = build_ict(c4)
        except Exception as exc:
            log(f"пропуск {inst}: {exc}")
        time.sleep(0.12)

    rets = {i: rs_return(ind) for i, ind in daily.items()}
    rets = {i: r for i, r in rets.items() if r is not None}
    ordered = sorted(rets, key=lambda k: rets[k])           # от слабых к сильным
    pct = {k: (idx + 1) / len(ordered) * 100 for idx, k in enumerate(ordered)}

    now_ms = time.time() * 1000
    gated = ict_n = trend_n = 0
    cands = []
    for inst, ind in daily.items():
        if inst in state["open"]:
            continue
        if time.time() - state["cooldown"].get(inst, 0) < COOLDOWN_DAYS * 86400:
            continue
        di = len(ind["c"]) - 1
        p = pct.get(inst, 50)
        for side in ("long", "short"):
            if not trend_gate(ind, di, side, regime):
                continue
            if side == "long" and p < 60:          # только верхние 40% по силе
                continue
            if side == "short" and p > 40:
                continue
            gated += 1
            strength = p if side == "long" else 100 - p
            found = []
            if MODE in ("ict", "hybrid") and inst in h4:
                s_ict = ict_signal(h4[inst], len(h4[inst]["L"]["c"]) - 1, side)
                if s_ict:
                    ict_n += 1
                    found.append((strength + min(s_ict["info"]["rr_liq"], 6.0) * 3.0, s_ict))
            fresh = 0 <= now_ms - (ind["ts"][di] + DAY) < 2 * H4   # дневная свеча только что закрылась
            if MODE in ("trend", "hybrid") and fresh:
                s_tr = signal_at(ind, di, regime, side)
                if s_tr:
                    trend_n += 1
                    found.append((strength, s_tr))
            if found:
                found.sort(key=lambda z: z[0], reverse=True)
                score, sig = found[0]
                cands.append((score, inst, sig, p))
    cands.sort(key=lambda z: z[0], reverse=True)
    log(f"Прошли дневной фильтр направления: {gated} · ICT-кандидатов: {ict_n} · "
        f"дневных трендовых: {trend_n}")

    out = []
    free = MAX_OPEN - len(state["open"])
    for _, inst, sig, p in cands:
        if len(out) >= min(MAX_NEW, free):
            break
        funding = get_funding(inst)
        if funding is not None:
            against = funding if sig["side"] == "long" else -funding
            if against > MAX_FUNDING:
                log(f"пропуск {inst}: фандинг против позиции {funding:+.4f}%")
                continue
        size = size_position(sig["entry"], sig["stop_pct"], instruments[inst])
        if not size:
            log(f"пропуск {inst}: размер меньше минимального лота")
            continue
        out.append((inst, sig, size, p, funding))

    for inst, sig, size, p, funding in out:
        rs_pct = (100 - p) if sig["side"] == "long" else p
        tg_send(fmt_signal(inst, sig, size, rs_pct, regime, funding))
        now_ms_i = int(time.time() * 1000)
        state["open"][inst] = {
            "side": sig["side"], "entry": sig["entry"], "stop": sig["stop"],
            "stop0": sig["stop"], "tp1": sig["tp1"], "tp2": sig["tp2"],
            "risk": sig["risk"], "atr": sig["atr"], "opened_ts": now_ms_i,
            "last_ts": now_ms_i, "status": "open", "setup": sig["setup"],
            "src": sig["src"], "risk_usdt": size["risk_usdt"],
        }
        state["cooldown"][inst] = time.time()
        log(f"СИГНАЛ: {sig['side'].upper()} {name(inst)} · {sig['setup']}")

    state["last_scan_bar"] = bar_ts
    if not out:
        log("Новых сигналов нет.")
    return out


# ----------------------------------------------------------------- сопровождение
def r_of(pos, price):
    d = (price - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - price)
    return d / pos["risk"]


def close_pos(state, inst, pos, exit_price, reason, msgs):
    rem_r = r_of(pos, exit_price)
    total = pos.get("realized_r", 0.0) + rem_r * pos.get("remaining", 1.0)
    state["closed"].append({
        "inst": inst, "side": pos["side"], "setup": pos["setup"], "src": pos.get("src", "trend"),
        "r": round(total, 2), "reason": reason, "opened_ts": pos["opened_ts"],
        "closed_ts": int(time.time() * 1000),
    })
    del state["open"][inst]
    icon = "✅" if total > 0 else "🛑"
    msgs.append(f"{icon} <b>{name(inst)}</b> закрыт: {reason}\n"
                f"Выход ≈ <code>{fmt(exit_price)}</code> · результат {total:+.2f}R")


def manage_open(state):
    msgs = []
    for inst in list(state["open"].keys()):
        pos = state["open"][inst]
        try:
            h4 = get_candles(inst, "4H", 100)
            d1 = get_candles(inst, "1Dutc", 150)
        except Exception as exc:
            log(f"сопровождение {inst}: {exc}")
            continue
        time.sleep(0.12)
        long_ = pos["side"] == "long"

        for ts, o, h, l, c, v in h4:
            if ts + H4 <= pos["last_ts"]:
                continue
            hit_stop = l <= pos["stop"] if long_ else h >= pos["stop"]
            hit_tp1 = h >= pos["tp1"] if long_ else l <= pos["tp1"]
            if hit_stop:                                   # при неоднозначности — стоп первым
                reason = "стоп" if pos["status"] == "open" else "трейлинг/безубыток"
                close_pos(state, inst, pos, pos["stop"], reason, msgs)
                break
            if pos["status"] == "open" and hit_tp1:
                pos["status"] = "tp1"
                pos["realized_r"] = 0.5 * TP1_R
                pos["remaining"] = 0.5
                pos["stop"] = pos["entry"]                 # безубыток
                msgs.append(f"🎯 <b>{name(inst)}</b> достиг TP1 ({fmt(pos['tp1'])}, {TP1_R:g}R)\n"
                            f"Закрыть 50%, стоп остатка → вход <code>{fmt(pos['entry'])}</code>. "
                            f"Дальше трейлинг {TRAIL_ATR:g} ATR.")
        else:
            pos["last_ts"] = int(time.time() * 1000)

        if inst not in state["open"]:
            continue

        if pos["status"] == "tp1" and len(d1) > 20:        # трейлинг по закрытым дневным свечам
            ind = build_indicators(d1)
            since = [i for i, t in enumerate(ind["ts"]) if t >= pos["opened_ts"] - DAY]
            if since:
                a = ind["atr"][-1]
                if long_:
                    new_stop = max(ind["h"][i] for i in since) - TRAIL_ATR * a
                    better = new_stop > pos["stop"]
                else:
                    new_stop = min(ind["l"][i] for i in since) + TRAIL_ATR * a
                    better = new_stop < pos["stop"]
                if better:
                    pos["stop"] = new_stop
                    msgs.append(f"🔼 <b>{name(inst)}</b> трейлинг: новый стоп "
                                f"<code>{fmt(new_stop)}</code> ({r_of(pos, new_stop):+.2f}R от входа)")

        age_days = (time.time() * 1000 - pos["opened_ts"]) / DAY
        if pos["status"] == "open" and age_days > MAX_HOLD_DAYS and not pos.get("timeout_sent"):
            pos["timeout_sent"] = True
            msgs.append(f"⏳ <b>{name(inst)}</b> в позиции {age_days:.0f} дн. без TP1 — "
                        f"пересмотрите идею (тайм-аут).")
    return msgs


# ----------------------------------------------------------------- статистика
def summarize(rs):
    if not rs:
        return "сделок нет"
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gp, gl = sum(wins), -sum(losses)
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    pf = (gp / gl) if gl > 0 else float("inf")
    return (f"сделок {len(rs)} · винрейт {len(wins) / len(rs) * 100:.0f}% · "
            f"средний R {sum(rs) / len(rs):+.2f} · PF {pf:.2f} · "
            f"сумма {sum(rs):+.1f}R · макс.просадка {dd:.1f}R")


def cmd_stats():
    st = load_state()
    closed = st["closed"]
    log("Все: " + summarize([c["r"] for c in closed]))
    for src in ("ict", "trend"):
        sub = [c["r"] for c in closed if c.get("src", "trend") == src]
        if sub:
            log(f"  {src}: " + summarize(sub))
    log(f"Открыто сейчас: {len(st['open'])}")


# ----------------------------------------------------------------- бэктест
def simulate(d_ind, ict, regime_map, side, mode, start_ts):
    """Симуляция на 4H-свечах. Решение — по закрытой свече i, вход — по открытию i+1.
    Стоп первым при неоднозначности; 50% на TP1 (1.5R) + стоп в БУ; остаток — chandelier
    3 дневных ATR. Комиссии учтены. Возвращает [(R, ts_выхода), ...]."""
    x = ict["L"]
    n = len(x["c"])
    ts4 = x["ts"]
    dts = d_ind["ts"]
    dclose = [t + DAY for t in dts]
    long_ = side == "long"
    trades = []
    i = 0
    while i < n and ts4[i] < start_ts:
        i += 1
    i = max(i, 40)
    while i < n - 1:
        close_t = ts4[i] + H4
        di = bisect_right(dclose, close_t) - 1
        if di < MIN_BARS:
            i += 1
            continue
        regime = regime_map.get(dts[di], "neutral")
        sig = None
        if mode in ("ict", "hybrid") and trend_gate(d_ind, di, side, regime):
            sig = ict_signal(ict, i, side)
        if sig is None and mode in ("trend", "hybrid") and close_t == dclose[di]:
            sig = signal_at(d_ind, di, regime, side)
        if not sig:
            i += 1
            continue

        entry = x["o"][i + 1]
        if sig["src"] == "ict":
            stop = sig["stop"]
            risk = (entry - stop) if long_ else (stop - entry)
            if risk < 0.3 * sig["atr"]:                  # гэп съел идею
                i += 1
                continue
        else:
            risk = sig["risk"]
            stop = entry - risk if long_ else entry + risk
        tp1 = entry + TP1_R * risk if long_ else entry - TP1_R * risk
        cost_r = (FEE_ROUNDTRIP_PCT / 100.0 * entry) / risk
        status, realized, remaining = "open", 0.0, 1.0
        extreme = entry
        res, exit_j = None, n - 1
        for j in range(i + 1, n):
            h, l = x["h"][j], x["l"][j]
            if long_:
                extreme = max(extreme, h)
                hit_stop, hit_tp = l <= stop, h >= tp1
            else:
                extreme = min(extreme, l)
                hit_stop, hit_tp = h >= stop, l <= tp1
            if hit_stop:
                rr = ((stop - entry) if long_ else (entry - stop)) / risk
                res, exit_j = realized + rr * remaining, j
                break
            if status == "open" and hit_tp:
                status, realized, remaining = "tp1", 0.5 * TP1_R, 0.5
                stop = entry
            if status == "tp1":
                dj = bisect_right(dclose, ts4[j] + H4) - 1
                if dj >= 0:
                    a = d_ind["atr"][dj]
                    tr = extreme - TRAIL_ATR * a if long_ else extreme + TRAIL_ATR * a
                    stop = max(stop, tr) if long_ else min(stop, tr)
        if res is None:                                   # конец данных — по последней цене
            cl = x["c"][-1]
            rr = ((cl - entry) if long_ else (entry - cl)) / risk
            res = realized + rr * remaining
        trades.append((res - cost_r, ts4[exit_j]))
        i = exit_j + 1                                    # одна позиция на монету
    return trades


def _halves(trades):
    """Делит сделки по времени выхода пополам — проверка стабильности."""
    if len(trades) < 8:
        return None
    ts_sorted = sorted(t[1] for t in trades)
    mid = ts_sorted[len(ts_sorted) // 2]
    a = [t[0] for t in trades if t[1] < mid]
    b = [t[0] for t in trades if t[1] >= mid]
    return a, b


def cmd_backtest():
    global ALLOW_SHORT
    days = int(_f("BT_DAYS", 365))
    insts = os.getenv("BT_INSTS", "")
    instruments = get_instruments()
    if insts:
        names = [x.strip() for x in insts.split(",") if x.strip()]
    else:
        names = [i for _, i, _ in get_universe(instruments)][:int(_f("BT_TOP", 12))]
    log(f"БЭКТЕСТ: {len(names)} монет · ~{days} дней 4H · комиссии+проскальзывание "
        f"{FEE_ROUNDTRIP_PCT}% за круг · ICT_MIN_RR={ICT_MIN_RR:g}")
    btc = get_history("BTC-USDT-SWAP", "1Dutc", days + MIN_BARS + 40)
    bi = build_indicators(btc)
    regime = {bi["ts"][k]: btc_regime(bi, k) for k in range(len(bi["ts"]))}
    ALLOW_SHORT = True                                     # шорты проверяем отдельно
    agg = {(m, s): [] for m in MODES for s in ("long", "short")}
    for inst in names:
        try:
            dcs = get_history(inst, "1Dutc", days + MIN_BARS + 40)
            hcs = get_history(inst, "4H", days * 6 + 60)
        except Exception as exc:
            log(f"пропуск {inst}: {exc}")
            continue
        if len(dcs) < MIN_BARS + 30 or len(hcs) < 200:
            continue
        d_ind = build_indicators(dcs)
        ict = build_ict(hcs)
        start_ts = d_ind["ts"][MIN_BARS] + DAY
        parts = []
        for mode in MODES:
            tr = simulate(d_ind, ict, regime, "long", mode, start_ts)
            agg[(mode, "long")] += tr
            for t in simulate(d_ind, ict, regime, "short", mode, start_ts):
                agg[(mode, "short")].append(t)
            parts.append(f"{mode} {len(tr)}")
        log(f"{name(inst):12s} лонг-сделок: " + " · ".join(parts))

    log("")
    for side in ("long", "short"):
        label = "ЛОНГ" if side == "long" else "ШОРТ (только при медвежьем BTC)"
        log(f"=== {label} ===")
        for mode in MODES:
            tr = agg[(mode, side)]
            log(f"{mode:7s}: " + summarize([t[0] for t in tr]))
            hv = _halves(tr)
            if hv:
                log(f"         1-я половина: {summarize(hv[0])}")
                log(f"         2-я половина: {summarize(hv[1])}")
    log("\nКак читать: выбирайте режим по профит-фактору и просадке на ОБЕИХ половинах "
        "выборки, а не по лучшей сумме. Мало сделок (<30) — вывода нет. Без фильтров "
        "RS/фандинга, параметры не оптимизировались; прошлое не гарантирует будущее.")


# ----------------------------------------------------------------- main
def run():
    state = load_state()
    if DRY:
        state = deepcopy(state)                           # dry ничего не сохраняет
    msgs = manage_open(state)
    if msgs:
        tg_send("📌 SIGNAL G7 — обновления\n\n" + "\n\n".join(msgs))
    instruments = get_instruments()
    universe = get_universe(instruments)
    if len(state["open"]) < MAX_OPEN:
        scan_new(state, instruments, universe, force=("--force" in sys.argv) or DRY)
    else:
        log("Достигнут MAX_OPEN; новые сигналы не ищу.")
    if not DRY:
        save_state(state)


def send_dry_report():
    body = "\n".join(LOG).strip() or "(пустой вывод)"
    parts = _chunks(body, 3300)            # каждая часть в своём <pre>, чтобы HTML не ломался
    for k, part in enumerate(parts, 1):
        title = "📋 <b>SIGNAL G7 — отчёт запуска</b>" + (f" ({k}/{len(parts)})" if len(parts) > 1 else "")
        tg_send(title + "\n<pre>" + html.escape(part.strip()) + "</pre>")


if __name__ == "__main__":
    code = 0
    try:
        if "--backtest" in sys.argv:
            cmd_backtest()
        elif "--stats" in sys.argv:
            cmd_stats()
        else:
            run()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        LOG.append(f"ERROR: {exc}")
        code = 1
    if DRY:
        try:
            send_dry_report()
        except Exception as exc:
            print(f"Не удалось отправить отчёт в Telegram: {exc}", file=sys.stderr)
            code = code or 1
    sys.exit(code)

#!/usr/bin/env python3
"""
Signal G6 — OKX Swing Scanner (среднесрок: дни–недели, свечи 1D + 4H)

Что делает:
  * берёт ликвидные USDT-перпетуалы OKX;
  * ищет трендовые входы на ДНЕВНЫХ свечах: пробой 20-дневного канала (Donchian)
    и откат к EMA20 внутри тренда;
  * фильтры: тренд монеты (EMA50/EMA100), режим BTC, относительная сила
    (21-дневная доходность), стоимость фандинга, запрет «догонять» цену;
  * считает вход, стоп (2.5 ATR), TP1 (1.5R), TP2 (3R), трейлинг после TP1
    и размер позиции от риска на сделку;
  * ведёт открытые сигналы в state-файле и присылает обновления:
    TP1, подтяжка стопа, стоп, тайм-аут;
  * режим --backtest проверяет те же правила на истории OKX.

Скрипт НИЧЕГО не торгует и не размещает ордера. Это инструмент отбора идей.
Трендследование выигрывает редко (обычно 35–45% сделок) и платит за счёт
крупных плюсовых сделок; серии стопов — нормальная часть системы.

Запуск (cron каждые 4 часа через 5 минут после закрытия свечи, UTC):
  5 0,4,8,12,16,20 * * *  /usr/bin/python3 /path/signal_g6.py

Аргументы:
  --dry        ничего не отправлять в Telegram, печатать в консоль
  --backtest   прогнать тест на истории (см. BT_* переменные)
  --stats      показать статистику закрытых сигналов из state-файла
  --force      сканировать новые сигналы, даже если дневная свеча уже обработана

Переменные окружения:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  EQUITY_USDT          размер счёта для расчёта позиции        (1000)
  RISK_PCT             риск на сделку, % от счёта               (1.0)
  MAX_LEVERAGE         потолок плеча                            (3)
  MAX_OPEN             макс. одновременных сигналов             (4)
  MAX_NEW_PER_RUN      макс. новых сигналов за запуск           (2)
  TOP_N                сколько самых ликвидных монет сканировать (40)
  MIN_TURNOVER_USDT    мин. суточный оборот                     (20000000)
  ALLOW_SHORT          1 = разрешить шорты в режиме медвежьего BTC (0)
  ATR_STOP_MULT        стоп в ATR                               (2.5)
  MAX_STOP_PCT         макс. ширина стопа, %                    (18)
  MAX_FUNDING_PCT      макс. фандинг против позиции за период, % (0.05)
  SYMBOL_COOLDOWN_DAYS пауза по монете после сигнала            (5)
  MAX_HOLD_DAYS        обзор позиции без TP1 через N дней       (30)
  STATE_FILE           (signal_g6_state.json)
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------- настройки
BASE_URL = "https://www.okx.com"
TIMEOUT = 20


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
MAX_FUNDING = _f("MAX_FUNDING_PCT", 0.05)
COOLDOWN_DAYS = _f("SYMBOL_COOLDOWN_DAYS", 5)
MAX_HOLD_DAYS = _f("MAX_HOLD_DAYS", 30)
STATE_FILE = os.getenv("STATE_FILE", "signal_g6_state.json")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TP1_R = 1.5          # первая цель в R
TP2_R = 3.0          # вторая (ориентир) в R
TRAIL_ATR = 3.0      # chandelier-трейлинг после TP1
DON_N = 20           # канал пробоя
RS_N = 21            # окно относительной силы (3 недели)
FEE_ROUNDTRIP_PCT = 0.12   # комиссии taker туда-обратно + проскальзывание (оценка)
STABLES = {"USDC", "DAI", "TUSD", "FDUSD", "USDE", "USDG"}

DRY = "--dry" in sys.argv


# ----------------------------------------------------------------- OKX API
def get_json(path, params=None, retries=4):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(
                BASE_URL + path, params=params, timeout=TIMEOUT,
                headers={"User-Agent": "SignalG6-Swing-Scanner/1.0"},
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
            base = iid.split("-")[0]
            if base in STABLES:
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
        time.sleep(0.12)
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
    o = [c[1] for c in candles]
    h = [c[2] for c in candles]
    l = [c[3] for c in candles]
    cl = [c[4] for c in candles]
    v = [c[5] for c in candles]
    n = len(candles)
    don_hi = [None] * n
    don_lo = [None] * n
    exit_lo = [None] * n
    exit_hi = [None] * n
    vol_ma = [None] * n
    for i in range(n):
        if i >= DON_N:
            don_hi[i] = max(h[i - DON_N:i])
            don_lo[i] = min(l[i - DON_N:i])
        if i >= 10:
            exit_lo[i] = min(l[i - 10:i])
            exit_hi[i] = max(h[i - 10:i])
        if i >= 20:
            vol_ma[i] = sum(v[i - 20:i]) / 20
    return {
        "ts": [c[0] for c in candles], "o": o, "h": h, "l": l, "c": cl, "v": v,
        "ema20": ema(cl, 20), "ema50": ema(cl, 50), "ema100": ema(cl, 100),
        "atr": atr_series(candles, 14),
        "don_hi": don_hi, "don_lo": don_lo, "exit_lo": exit_lo, "exit_hi": exit_hi,
        "vol_ma": vol_ma,
    }


MIN_BARS = 110


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


def signal_at(ind, i, regime, side):
    """Проверка сетапа на закрытой дневной свече i. Возвращает dict или None."""
    if i < MIN_BARS or ind["don_hi"][i] is None or ind["vol_ma"][i] is None:
        return None
    c, o, h, l = ind["c"][i], ind["o"][i], ind["h"][i], ind["l"][i]
    e20, e50, e100 = ind["ema20"][i], ind["ema50"][i], ind["ema100"][i]
    a = ind["atr"][i]
    if a <= 0 or c <= 0:
        return None
    ext = (c - e20) / a if side == "long" else (e20 - c) / a   # насколько цена «убежала»
    if ext > 3.0:
        return None

    if side == "long":
        if regime == "bear":
            return None
        if not (c > e50 and e50 > e100 and e50 > ind["ema50"][i - 5]):
            return None
        breakout = c > ind["don_hi"][i] and ind["v"][i] >= ind["vol_ma"][i]
        had_break = any(
            ind["don_hi"][j] is not None and ind["c"][j] > ind["don_hi"][j]
            for j in range(i - 15, i)
        )
        pullback = (had_break and l <= e20 + 0.25 * a and c > e20 and c > o and not breakout)
    else:
        if regime != "bear" or not ALLOW_SHORT:
            return None
        if not (c < e50 and e50 < e100 and e50 < ind["ema50"][i - 5]):
            return None
        breakout = c < ind["don_lo"][i] and ind["v"][i] >= ind["vol_ma"][i]
        had_break = any(
            ind["don_lo"][j] is not None and ind["c"][j] < ind["don_lo"][j]
            for j in range(i - 15, i)
        )
        pullback = (had_break and h >= e20 - 0.25 * a and c < e20 and c < o and not breakout)

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
        "side": side, "setup": setup, "entry": entry, "stop": stop,
        "tp1": tp1, "tp2": tp2, "risk": risk, "stop_pct": stop_pct,
        "atr": a, "ts": ind["ts"][i],
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
    liq_dist = max(100.0 / max(lev, 1e-9) - 0.5, 0)
    return {
        "contracts": contracts, "notional": notional, "lev": notional / EQUITY,
        "risk_usdt": real_risk, "risk_pct": real_risk / EQUITY * 100,
        "capped": capped, "liq_dist": liq_dist,
    }


# ----------------------------------------------------------------- состояние/Telegram
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError):
        st = {}
    st.setdefault("open", {})
    st.setdefault("closed", [])
    st.setdefault("cooldown", {})
    st.setdefault("last_scan_day", 0)
    return st


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def tg_send(text):
    if DRY or not TG_TOKEN or not TG_CHAT:
        print(text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
        print("-" * 40)
        if not DRY and (not TG_TOKEN or not TG_CHAT):
            print("(Telegram не настроен: задайте TELEGRAM_TOKEN и TELEGRAM_CHAT_ID)")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
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
    arrow = "🟢 LONG" if sig["side"] == "long" else "🔴 SHORT"
    lines = [
        f"{arrow} <b>{name(inst)}</b> — среднесрок (дни–недели)",
        f"Сетап: {sig['setup']}",
        f"Режим BTC: {regime} · RS 21д: топ {rs_pct:.0f}%" if sig["side"] == "long"
        else f"Режим BTC: {regime} · RS 21д: низ {rs_pct:.0f}%",
        "",
        f"Вход: <code>{fmt(sig['entry'])}</code> (зона ±0.25 ATR; не гнаться, если ушла дальше 0.5 ATR)",
        f"Стоп: <code>{fmt(sig['stop'])}</code> ({sig['stop_pct']:.1f}%, {ATR_STOP:g} ATR 1D)",
        f"TP1: <code>{fmt(sig['tp1'])}</code> ({TP1_R:g}R) — закрыть 50%, стоп в безубыток",
        f"TP2: <code>{fmt(sig['tp2'])}</code> ({TP2_R:g}R) — остаток по трейлингу {TRAIL_ATR:g} ATR",
        "",
        f"Размер: {size['contracts']:g} контр. ≈ {size['notional']:,.0f} USDT, плечо ≈ {size['lev']:.1f}x",
        f"Риск: {size['risk_usdt']:.1f} USDT ({size['risk_pct']:.2f}% счёта)"
        + (" — урезан лимитом плеча" if size["capped"] else ""),
        "Маржа: isolated. Ликвидация должна быть заметно дальше стопа.",
    ]
    if funding is not None:
        lines.append(f"Фандинг: {funding:+.4f}% за период")
    lines += ["", "Это идея для ручной проверки, не гарантия результата. "
                  "Сделки по трендовой системе чаще убыточны; прибыль даёт хвост крупных выигрышей."]
    return "\n".join(lines)


# ----------------------------------------------------------------- сканирование
def rs_return(ind):
    c = ind["c"]
    if len(c) <= RS_N:
        return None
    return c[-1] / c[-1 - RS_N] - 1.0


def scan_new(state, instruments, universe, force=False):
    today = int(time.time() // 86400)
    if not force and state.get("last_scan_day") == today:
        print("Дневная свеча уже обработана; пропускаю поиск новых сигналов.")
        return []

    # BTC режим
    btc = get_candles("BTC-USDT-SWAP", "1Dutc", 300)
    if len(btc) < MIN_BARS:
        raise RuntimeError("Недостаточно дневных данных BTC")
    regime = btc_regime(build_indicators(btc))
    print(f"Режим BTC: {regime}")

    data = {}
    for turnover, inst, last in universe:
        try:
            cs = get_candles(inst, "1Dutc", 300)
            if len(cs) >= MIN_BARS:
                data[inst] = build_indicators(cs)
        except Exception as exc:
            print(f"skip {inst}: {exc}")
        time.sleep(0.12)

    rets = {i: rs_return(ind) for i, ind in data.items()}
    rets = {i: r for i, r in rets.items() if r is not None}
    ordered = sorted(rets, key=lambda k: rets[k])           # от слабых к сильным
    pct = {k: (idx + 1) / len(ordered) * 100 for idx, k in enumerate(ordered)}

    now = time.time()
    cands = []
    for inst, ind in data.items():
        if inst in state["open"]:
            continue
        if now - state["cooldown"].get(inst, 0) < COOLDOWN_DAYS * 86400:
            continue
        i = len(ind["c"]) - 1
        for side in ("long", "short"):
            sig = signal_at(ind, i, regime, side)
            if not sig:
                continue
            p = pct.get(inst, 50)
            if side == "long" and p < 60:          # только верхние 40% по силе
                continue
            if side == "short" and p > 40:
                continue
            strength = p if side == "long" else 100 - p
            cands.append((strength, inst, sig, strength))
    cands.sort(key=lambda x: x[0], reverse=True)

    out = []
    free = MAX_OPEN - len(state["open"])
    for _, inst, sig, strength in cands:
        if len(out) >= min(MAX_NEW, free):
            break
        funding = get_funding(inst)
        if funding is not None:
            against = funding if sig["side"] == "long" else -funding
            if against > MAX_FUNDING:
                print(f"skip {inst}: фандинг против позиции {funding:+.4f}%")
                continue
        size = size_position(sig["entry"], sig["stop_pct"], instruments[inst])
        if not size:
            print(f"skip {inst}: размер меньше минимального лота")
            continue
        out.append((inst, sig, size, strength, funding))

    for inst, sig, size, strength, funding in out:
        tg_send(fmt_signal(inst, sig, size, 100 - strength if sig["side"] == "long" else strength,
                           regime, funding)
                if False else fmt_signal(inst, sig, size, 100 - strength + 0.0, regime, funding))
        state["open"][inst] = {
            "side": sig["side"], "entry": sig["entry"], "stop": sig["stop"],
            "stop0": sig["stop"], "tp1": sig["tp1"], "tp2": sig["tp2"],
            "risk": sig["risk"], "atr": sig["atr"], "opened_ts": int(time.time() * 1000),
            "last_ts": int(time.time() * 1000), "status": "open", "setup": sig["setup"],
            "risk_usdt": size["risk_usdt"],
        }
        state["cooldown"][inst] = time.time()

    state["last_scan_day"] = today
    if not out:
        print("Новых сигналов нет.")
    return out


# ----------------------------------------------------------------- сопровождение
def r_of(pos, price):
    d = (price - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - price)
    return d / pos["risk"]


def close_pos(state, inst, pos, exit_price, reason, msgs):
    # половина закрыта на TP1 (если был) — R считаем взвешенно
    rem_r = r_of(pos, exit_price)
    total = pos.get("realized_r", 0.0) + rem_r * pos.get("remaining", 1.0)
    state["closed"].append({
        "inst": inst, "side": pos["side"], "setup": pos["setup"], "r": round(total, 2),
        "reason": reason, "opened_ts": pos["opened_ts"], "closed_ts": int(time.time() * 1000),
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
            print(f"manage {inst}: {exc}")
            continue
        time.sleep(0.12)
        long_ = pos["side"] == "long"

        for ts, o, h, l, c, v in h4:
            if ts + 4 * 3600 * 1000 <= pos["last_ts"]:
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

        # трейлинг по закрытым дневным свечам после TP1
        if pos["status"] == "tp1" and len(d1) > 20:
            ind = build_indicators(d1)
            since = [i for i, t in enumerate(ind["ts"]) if t >= pos["opened_ts"] - 86400000]
            if since:
                a = ind["atr"][-1]
                if long_:
                    extreme = max(ind["h"][i] for i in since)
                    new_stop = extreme - TRAIL_ATR * a
                    better = new_stop > pos["stop"]
                else:
                    extreme = min(ind["l"][i] for i in since)
                    new_stop = extreme + TRAIL_ATR * a
                    better = new_stop < pos["stop"]
                if better:
                    pos["stop"] = new_stop
                    msgs.append(f"🔼 <b>{name(inst)}</b> трейлинг: новый стоп "
                                f"<code>{fmt(new_stop)}</code> ({r_of(pos, new_stop):+.2f}R от входа)")

        age_days = (time.time() * 1000 - pos["opened_ts"]) / 86400000
        if pos["status"] == "open" and age_days > MAX_HOLD_DAYS and not pos.get("timeout_sent"):
            pos["timeout_sent"] = True
            msgs.append(f"⏳ <b>{name(inst)}</b> в позиции {age_days:.0f} дн. без TP1 — "
                        f"пересмотрите идею (тайм-аут).")
    return msgs


# ----------------------------------------------------------------- статистика
def summarize(rs):
    if not rs:
        return "Закрытых сделок пока нет."
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gp, gl = sum(wins), -sum(losses)
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    pf = (gp / gl) if gl > 0 else float("inf")
    return (f"Сделок: {len(rs)} · винрейт {len(wins) / len(rs) * 100:.0f}% · "
            f"средний R {sum(rs) / len(rs):+.2f} · профит-фактор {pf:.2f} · "
            f"суммарно {sum(rs):+.1f}R · макс. просадка {dd:.1f}R")


def cmd_stats():
    st = load_state()
    print(summarize([c["r"] for c in st["closed"]]))
    print(f"Открыто сейчас: {len(st['open'])}")


# ----------------------------------------------------------------- бэктест
def simulate(ind, regime_by_ts, side="long"):
    """Дневной бэктест: вход на открытии следующей свечи, стоп 2.5 ATR,
    50% на TP1 (1.5R) + стоп в БУ, остаток — chandelier 3 ATR. Результат в R."""
    trades = []
    n = len(ind["c"])
    i = MIN_BARS
    while i < n - 1:
        regime = regime_by_ts.get(ind["ts"][i], "neutral")
        sig = signal_at(ind, i, regime, side)
        if not sig:
            i += 1
            continue
        entry = ind["o"][i + 1]
        risk = sig["risk"]
        long_ = side == "long"
        stop = entry - risk if long_ else entry + risk
        tp1 = entry + TP1_R * risk if long_ else entry - TP1_R * risk
        cost_r = (FEE_ROUNDTRIP_PCT / 100 * entry) / risk
        status, realized, remaining = "open", 0.0, 1.0
        extreme = entry
        exit_i, res = None, None
        for j in range(i + 1, n):
            h, l, c = ind["h"][j], ind["l"][j], ind["c"][j]
            if long_:
                extreme = max(extreme, h)
                hit_stop, hit_tp = l <= stop, h >= tp1
            else:
                extreme = min(extreme, l)
                hit_stop, hit_tp = h >= stop, l <= tp1
            if hit_stop:
                rr = ((stop - entry) if long_ else (entry - stop)) / risk
                res = realized + rr * remaining
                exit_i = j
                break
            if status == "open" and hit_tp:
                status, realized, remaining = "tp1", 0.5 * TP1_R, 0.5
                stop = entry
            if status == "tp1":
                a = ind["atr"][j]
                tr = extreme - TRAIL_ATR * a if long_ else extreme + TRAIL_ATR * a
                stop = max(stop, tr) if long_ else min(stop, tr)
        if res is None:                                   # конец данных
            c = ind["c"][-1]
            rr = ((c - entry) if long_ else (entry - c)) / risk
            res, exit_i = realized + rr * remaining, n - 1
        trades.append(res - cost_r)
        i = exit_i + 1                                    # одна позиция на монету
    return trades


def cmd_backtest():
    days = int(_f("BT_DAYS", 900))
    insts = os.getenv("BT_INSTS", "")
    instruments = get_instruments()
    if insts:
        names = [x.strip() for x in insts.split(",") if x.strip()]
    else:
        names = [i for _, i, _ in get_universe(instruments)][:int(_f("BT_TOP", 15))]
    print(f"Бэктест: {len(names)} монет, до {days} дневных свечей, "
          f"комиссия+проскальзывание {FEE_ROUNDTRIP_PCT}% за круг")
    btc = get_history("BTC-USDT-SWAP", "1Dutc", days)
    bi = build_indicators(btc)
    regime = {bi["ts"][k]: btc_regime(bi, k) for k in range(len(bi["ts"]))}
    all_tr = {"long": [], "short": []}
    global ALLOW_SHORT
    for inst in names:
        try:
            cs = get_history(inst, "1Dutc", days)
        except Exception as exc:
            print(f"skip {inst}: {exc}")
            continue
        if len(cs) < MIN_BARS + 30:
            continue
        ind = build_indicators(cs)
        for side in ("long", "short"):
            if side == "short":
                ALLOW_SHORT = True
            tr = simulate(ind, regime, side)
            all_tr[side] += tr
        print(f"{name(inst):12s} лонг: {summarize(simulate(ind, regime, 'long'))}")
    print("\n=== ИТОГО ЛОНГ ===\n" + summarize(all_tr["long"]))
    print("=== ИТОГО ШОРТ (режим bear BTC) ===\n" + summarize(all_tr["short"]))
    print("\nОговорки: без фильтров RS и фандинга, вход по открытию следующего дня, "
          "параметры не оптимизированы; результат прошлого не гарантирует будущий. "
          "Сравнивайте периоды (до/после середины выборки) и смотрите на просадку.")


# ----------------------------------------------------------------- main
def run():
    state = load_state()
    msgs = manage_open(state)
    if msgs:
        tg_send("📌 SIGNAL G6 — обновления\n\n" + "\n\n".join(msgs))
    instruments = get_instruments()
    universe = get_universe(instruments)
    if len(state["open"]) < MAX_OPEN:
        scan_new(state, instruments, universe, force="--force" in sys.argv)
    else:
        print("Достигнут MAX_OPEN; новые сигналы не ищу.")
    save_state(state)


if __name__ == "__main__":
    try:
        if "--backtest" in sys.argv:
            cmd_backtest()
        elif "--stats" in sys.argv:
            cmd_stats()
        else:
            run()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

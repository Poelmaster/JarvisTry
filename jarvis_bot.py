"""
Jarvis BTC-setupbot — liquidity sweep + fib (strategie uit Gilles' trading journal)

Wat hij doet (elke run, ~elke 5 min via GitHub Actions):
 1. Haalt BTC-candles op (MEXC futures, met OKX als reserve).
 2. Zoekt een LIQUIDITY SWEEP: een swing-high/low wordt met een wick genomen,
    maar de candle sluit terug binnen de range (geen echte doorbraak).
 3. Tekent de fib zoals in het journal: 0 = sweep-extreem, 1 = de reactie erna.
      entry = oranje 0,382 | stop = net voorbij de 0 | TP = blauwe 1,618
 4. Checkt de confluence: trend hogere timeframe, 200 MA, RSI-divergentie, volume.
 5. Stuurt een Telegram-melding. Hij plaatst NOOIT zelf trades — jij beslist.

Instellingen staan bovenaan (CONFIG) en kunnen ook via environment variables.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("JARVIS_TZ", "Europe/Warsaw"))
except Exception:  # pragma: no cover
    TZ = timezone.utc

# ----------------------------------------------------------------------------
# CONFIG — pas hier aan wat je wil
# ----------------------------------------------------------------------------
CONFIG = {
    "timeframes": os.getenv("JARVIS_TIMEFRAMES", "5m,15m").split(","),
    "risk_usd": float(os.getenv("JARVIS_RISK_USD", "20")),   # je max risico per trade
    "min_score": int(os.getenv("JARVIS_MIN_SCORE", "1")),     # min. aantal confluences (0-4)
    "pivot_left": 3,           # candles links/rechts om een swing-high/low te zijn
    "pivot_right": 3,
    "pivot_lookback": 72,      # hoe ver terug een swing mag liggen (in candles)
    "sweep_lookback": 6,       # sweep moet in de laatste X gesloten candles gebeurd zijn
    "min_range_pct": 0.0008,   # fib-range min 0,08% van de prijs (anders te klein)
    "max_range_pct": 0.012,    # en max 1,2%
    "stop_buffer_pct": 0.0001, # stop 0,01% voorbij de sweep (± 8 punten)
    "fib_entry": 0.382,        # oranje lijn
    "fib_tp": 1.618,           # blauwe lijn
    "ma_len": 200,
    "rsi_len": 14,
    "vol_mult": 1.5,           # sweep-candle volume >= 1,5x gemiddelde = ✓
}

STATE_FILE = os.getenv("JARVIS_STATE", "state.json")
UA = {"User-Agent": "jarvis-bot/1.0"}


# ----------------------------------------------------------------------------
# Data ophalen
# ----------------------------------------------------------------------------
def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
MEXC_TF = {"1m": "Min1", "5m": "Min5", "15m": "Min15", "30m": "Min30", "1h": "Min60", "4h": "Hour4"}
OKX_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H"}


def fetch_mexc(tf, limit=300):
    now = int(time.time())
    start = now - TF_SECONDS[tf] * (limit + 2)
    url = ("https://contract.mexc.com/api/v1/contract/kline/BTC_USDT?"
           + urllib.parse.urlencode({"interval": MEXC_TF[tf], "start": start, "end": now}))
    d = _get_json(url)
    if not d.get("success"):
        raise RuntimeError(f"MEXC fout: {d}")
    k = d["data"]
    return [
        {"t": int(k["time"][i]), "o": float(k["open"][i]), "h": float(k["high"][i]),
         "l": float(k["low"][i]), "c": float(k["close"][i]), "v": float(k["vol"][i])}
        for i in range(len(k["time"]))
    ]


def fetch_okx(tf, limit=300):
    out, after = [], None
    while len(out) < limit:
        q = {"instId": "BTC-USDT-SWAP", "bar": OKX_TF[tf], "limit": 100}
        if after:
            q["after"] = after
        d = _get_json("https://www.okx.com/api/v5/market/history-candles?" + urllib.parse.urlencode(q))
        rows = d.get("data") or []
        if not rows:
            break
        out.extend(rows)
        after = rows[-1][0]
    out = sorted(out, key=lambda r: int(r[0]))
    return [
        {"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
         "c": float(r[4]), "v": float(r[5])}
        for r in out
    ]


def fetch_candles(tf, limit=300):
    """Gesloten candles, oudste eerst. De candle die nog loopt valt weg."""
    errors = []
    for name, fn in (("MEXC", fetch_mexc), ("OKX", fetch_okx)):
        try:
            c = fn(tf, limit)
            if len(c) < 60:
                raise RuntimeError(f"te weinig candles ({len(c)})")
            now = time.time()
            c = [x for x in c if x["t"] + TF_SECONDS[tf] <= now]  # enkel gesloten
            return c[-limit:], name
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    raise RuntimeError("Geen data: " + " | ".join(errors))


# ----------------------------------------------------------------------------
# Indicatoren
# ----------------------------------------------------------------------------
def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals, n):
    out, k, e = [None] * len(vals), 2 / (n + 1), None
    for i, v in enumerate(vals):
        e = v if e is None else v * k + e * (1 - k)
        out[i] = e if i >= n - 1 else None
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def pivots(c, side, left, right):
    """Indexen van swing-highs (side='high') of swing-lows (side='low')."""
    key = "h" if side == "high" else "l"
    res = []
    for i in range(left, len(c) - right):
        v = c[i][key]
        win_l = [c[j][key] for j in range(i - left, i)]
        win_r = [c[j][key] for j in range(i + 1, i + right + 1)]
        if side == "high" and v > max(win_l) and v >= max(win_r):
            res.append(i)
        if side == "low" and v < min(win_l) and v <= min(win_r):
            res.append(i)
    return res


# ----------------------------------------------------------------------------
# Setup-detectie
# ----------------------------------------------------------------------------
def find_setup(c, direction, htf_bias):
    """
    direction 'short' = sweep van een high, 'long' = sweep van een low.
    Geeft een dict met de setup terug, of None.
    """
    cfg = CONFIG
    n = len(c)
    short = direction == "short"
    ext = "h" if short else "l"           # sweep-kant
    opp = "l" if short else "h"           # reactie-kant
    better = (lambda a, b: a > b) if short else (lambda a, b: a < b)

    piv = pivots(c, "high" if short else "low", cfg["pivot_left"], cfg["pivot_right"])
    closes = [x["c"] for x in c]
    ma = sma(closes, cfg["ma_len"])
    rs = rsi(closes, cfg["rsi_len"])

    best = None
    for s in range(max(n - cfg["sweep_lookback"], 1), n):
        for p in piv:
            if p + cfg["pivot_right"] >= s or s - p > cfg["pivot_lookback"]:
                continue
            level = c[p][ext]
            # level mag tussen pivot en sweep nog niet genomen zijn
            between = [c[j][ext] for j in range(p + 1, s)]
            if between and (max(between) > level if short else min(between) < level):
                continue
            # de sweep: wick erdoor, close terug erbinnen
            if not better(c[s][ext], level):
                continue
            if (c[s]["c"] >= level) if short else (c[s]["c"] <= level):
                continue
            # na de sweep geen close voorbij het level (anders echte doorbraak)
            if any(((c[j]["c"] > level) if short else (c[j]["c"] < level)) for j in range(s, n)):
                continue
            # kies het meest significante level
            if best is None or better(level, best["level"]):
                best = {"p": p, "s": s, "level": level}
    if not best:
        return None

    p, s, level = best["p"], best["s"], best["level"]
    # fib 0 = uiterste punt vanaf de sweep
    h_idx = max(range(s, n), key=lambda j: c[j][ext]) if short else min(range(s, n), key=lambda j: c[j][ext])
    fib0 = c[h_idx][ext]
    # fib 1 = reactie na dat punt
    if h_idx >= n - 1:
        return None
    l_idx = (min if short else max)(range(h_idx + 1, n), key=lambda j: c[j][opp])
    fib1 = c[l_idx][opp]
    if l_idx >= n - 1:
        return None  # reactie loopt nog; fib 1 ligt nog niet vast → wachten op pullback
    rng = abs(fib0 - fib1)
    price = c[-1]["c"]
    if not (cfg["min_range_pct"] * price <= rng <= cfg["max_range_pct"] * price):
        return None

    sign = -1 if short else 1          # short: levels onder fib0
    entry = fib0 + sign * cfg["fib_entry"] * rng
    tp = fib0 + sign * cfg["fib_tp"] * rng
    stop = fib0 - sign * cfg["stop_buffer_pct"] * price
    # entry al geraakt sinds de reactie? dan is de melding te laat
    after = [c[j][ext] for j in range(l_idx + 1, n)]
    if after and ((max(after) >= entry) if short else (min(after) <= entry)):
        return None

    # --- confluence ---
    checks = []
    trend_ok = htf_bias == ("bearish" if short else "bullish")
    checks.append((trend_ok, f"trend 1h {htf_bias}"))
    m = ma[-1]
    if m is not None:
        near = abs(m - entry) <= max(0.35 * rng, 0.0008 * price)
        side_ok = (m >= entry) if short else (m <= entry)
        checks.append((near or (side_ok and abs(m - entry) <= 0.002 * price), f"200 MA ({fmt(m)})"))
    else:
        checks.append((False, "200 MA (te weinig data)"))
    if rs[p] is not None and rs[h_idx] is not None:
        div = rs[h_idx] < rs[p] if short else rs[h_idx] > rs[p]
        checks.append((div, f"RSI-divergentie ({rs[p]:.0f} → {rs[h_idx]:.0f})"))
    else:
        checks.append((False, "RSI-divergentie (te weinig data)"))
    vols = [c[j]["v"] for j in range(max(0, s - 20), s)]
    vol_ok = bool(vols) and c[s]["v"] >= CONFIG["vol_mult"] * (sum(vols) / len(vols))
    checks.append((vol_ok, "volume op de sweep"))
    score = sum(1 for ok, _ in checks if ok)

    return {
        "direction": direction, "level": level, "pivot_t": c[p]["t"], "sweep_t": c[s]["t"],
        "fib0": fib0, "fib1": fib1, "entry": entry, "stop": stop, "tp": tp,
        "price": price, "checks": checks, "score": score,
        "rr": abs(tp - entry) / abs(stop - entry) if stop != entry else 0,
    }


def htf_bias():
    """Trendrichting op 1h: prijs t.o.v. EMA50 en helling van de EMA50."""
    try:
        c, _ = fetch_candles("1h", 120)
        e = ema([x["c"] for x in c], 50)
        if e[-1] is None or e[-6] is None:
            return "neutraal"
        if c[-1]["c"] < e[-1] and e[-1] < e[-6]:
            return "bearish"
        if c[-1]["c"] > e[-1] and e[-1] > e[-6]:
            return "bullish"
        return "neutraal"
    except Exception:  # noqa: BLE001
        return "onbekend"


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def fmt(x):
    return f"{x:,.0f}".replace(",", ".")


def hhmm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


def tg(token, method, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def discover_chat_id(token):
    """Zoekt je chat-id via de laatste berichten aan de bot (na /start)."""
    try:
        res = tg(token, "getUpdates", {"limit": 50})
    except Exception as e:  # noqa: BLE001
        print("getUpdates mislukt:", e)
        return None
    ids = []
    for u in res.get("result", []):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private" and chat.get("id"):
            ids.append(chat["id"])
    return ids[-1] if ids else None


def message(setup, tf, source):
    s = setup
    short = s["direction"] == "short"
    risk_pts = abs(s["stop"] - s["entry"])
    size_btc = CONFIG["risk_usd"] / risk_pts if risk_pts else 0
    lines = [
        f"{'🔻' if short else '🟢'} <b>{'SHORT' if short else 'LONG'}-setup BTC ({tf})</b>",
        f"Sweep van {'high' if short else 'low'} {fmt(s['level'])} (swing {hhmm(s['pivot_t'])}) "
        f"om {hhmm(s['sweep_t'])}, close terug {'eronder' if short else 'erboven'}.",
        "",
        f"Fib: 0 = {fmt(s['fib0'])} | 1 = {fmt(s['fib1'])}",
        f"🟠 Entry 0,382: <b>{fmt(s['entry'])}</b> (limit)",
        f"🛑 Stop: <b>{fmt(s['stop'])}</b> ({risk_pts:.0f} punten)",
        f"🔵 TP 1,618: <b>{fmt(s['tp'])}</b>",
        f"RR ≈ {s['rr']:.1f}".replace(".", ",") + f" : 1 · prijs nu {fmt(s['price'])}",
        f"Risico ${CONFIG['risk_usd']:.0f} → positie ≈ {size_btc:.4f} BTC (≈ ${size_btc * s['entry']:,.0f})".replace(",", "."),
        "",
        "Checklist:",
    ]
    lines += [f"{'✅' if ok else '❌'} {txt}" for ok, txt in s["checks"]]
    lines += [
        f"Score {s['score']}/4",
        "",
        "Jij beslist. Limit-order, geen bijkopen, stop niet verzetten.",
        f"<i>data: {source}</i>",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(st):
    st["sent"] = st.get("sent", [])[-300:]
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def main():
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    manual = os.getenv("JARVIS_MANUAL", "").lower() in ("1", "true", "yes")
    dry = not token
    st = load_state()

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or st.get("chat_id")
    if not dry and not chat_id:
        chat_id = discover_chat_id(token)
        if chat_id:
            st["chat_id"] = chat_id
            tg(token, "sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "text":
               f"🤖 <b>Jarvis is verbonden.</b>\nJe chat-id is <code>{chat_id}</code>.\n"
               "Ik meld me zodra er een sweep-setup verschijnt."})
        else:
            print("Geen chat-id gevonden. Stuur /start naar je bot in Telegram en draai opnieuw.")
            save_state(st)
            return 1

    bias = htf_bias()
    found, status = [], []
    for tf in [t.strip() for t in CONFIG["timeframes"] if t.strip()]:
        try:
            c, source = fetch_candles(tf)
        except Exception as e:  # noqa: BLE001
            print(f"[{tf}] {e}")
            status.append(f"{tf}: geen data")
            continue
        status.append(f"{tf}: {fmt(c[-1]['c'])}")
        for d in ("short", "long"):
            s = find_setup(c, d, bias)
            if not s or s["score"] < CONFIG["min_score"]:
                continue
            key = f"{tf}-{d}-{s['sweep_t']}-{s['level']}"
            if key in st.get("sent", []):
                continue
            found.append((key, s, tf, source))

    for key, s, tf, source in found:
        text = message(s, tf, source)
        print(text.replace("<b>", "").replace("</b>", ""), "\n")
        if not dry:
            tg(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        st.setdefault("sent", []).append(key)

    if manual and not found and not dry:
        tg(token, "sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "text":
           "🤖 <b>Jarvis draait.</b>\nBTC " + " · ".join(status) +
           f"\nTrend 1h: {bias}\nGeen nieuwe sweep-setup op dit moment."})

    print(f"Klaar — {len(found)} nieuwe setup(s). Trend 1h: {bias}. " + " | ".join(status))
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())

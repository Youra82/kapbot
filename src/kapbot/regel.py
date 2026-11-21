"""kapbot-Regel (Claude, 21.11.2026): EINE Regel für Live und Backtest, reines Python (Mini-PC).

Forschung: botprojekte/forschung/2026-11-20-btc-abverkaeufe (PRAEREGISTRIERUNG_puzzle*.md, _kapitulation_max.md).
Eingefroren, bestanden in Prüfung C (10 neue Coins +1,44 %/Trade, p 0,0002) und den Tests ab 07/2024 (+2,13 %) bzw.
10 weiteren neuen Coins (+1,65 %); 30 Coins 2020–2026: 289 Trades/J, +1,41 %/Trade netto, jedes Jahr positiv.

Signal am Schluss einer Binance-USDT-M-5m-Kerze t (alle Bedingungen):
1. Flush: r15 = ln(c_t / c_{t−3}); z = r15 / Std(r15 der letzten 8640 Kerzen inkl. t, ddof 1, min. 2000) ≤ −4.
2. Hebel rausgespült: OI_t / max(OI der letzten 72 Kerzen inkl. t, min. 36 Werte) − 1 ≤ −3 %.
3. Kleinanleger geben auf: Anteil Konten long R = g/(1+g) (g = globalLongShortAccountRatio) ist jetzt KLEINER als vor 72 h.
4. Abwärtstrend: c_t / max(Schlusskurse der letzten 25 920 Kerzen = 90 T inkl. t, min. 17 280) − 1 ≤ −20 %.
Handel: Long zur Eröffnung der Folgekerze, Ausstieg zum Schluss der Kerze t+288 (24 h), je Coin keine Überlappung.
Konto (robust über alle Tests): 10 % des Kontowerts je Trade, Hebel 1, höchstens 10 gleichzeitig.
"""
from __future__ import annotations
import math

COINS = ("BTC ETH SOL XRP BNB DOGE ADA LINK AVAX LTC DOT ATOM NEAR FIL UNI ETC BCH TRX AAVE ALGO "
         "XLM VET THETA SAND MANA AXS CRV ZEC HBAR ICP").split()
BAR_MS = 300_000
Z_THR, OI_DROP, DD_THR = -4.0, -0.03, -0.20
SIG_N, SIG_MIN = 8640, 2000
OI_N, OI_MIN = 72, 36
R_LAG_BARS = 864                      # 72 h
DD_N, DD_MIN = 25_920, 17_280         # 90 / 60 Tage
HOLD_BARS = 288                       # 24 h
NEED_CLOSES = DD_N                    # Kerzen, die der Live-Dienst vorhalten muss (≥ SIG_N + 3)


def z_flush(closes) -> float | None:
    """z der 15-min-Rendite der letzten Kerze. closes = aufeinanderfolgende 5m-Schlüsse, letzter = Kerze t."""
    c = closes[-(SIG_N + 3):]
    r = [math.log(c[i] / c[i - 3]) for i in range(3, len(c))]
    if len(r) < SIG_MIN:
        return None
    n = len(r); m = sum(r) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in r) / (n - 1))
    return r[-1] / sd if sd > 0 else None


def dd90(closes) -> float | None:
    c = closes[-DD_N:]
    if len(c) < DD_MIN:
        return None
    return c[-1] / max(c) - 1


def oi_drop(ois) -> float | None:
    """ois = OI-Werte der letzten 72 Kerzen (None = fehlt), letzter = Kerze t."""
    w = ois[-OI_N:]
    vals = [v for v in w if v is not None]
    if w[-1] is None or len(vals) < OI_MIN:
        return None
    mx = max(vals)
    return w[-1] / mx - 1 if mx > 0 else None


def retail_falling(g_now, g_72h_ago) -> bool:
    if g_now is None or g_72h_ago is None:
        return False
    return g_now / (1 + g_now) < g_72h_ago / (1 + g_72h_ago)


def price_ok(closes) -> tuple[bool, float | None, float | None]:
    """Nur Kursbedingungen (1 und 4) – der Live-Dienst lädt OI/Long-Short nur, wenn diese schon erfüllt sind."""
    z = z_flush(closes)
    d = dd90(closes)
    return (z is not None and z <= Z_THR and d is not None and d <= DD_THR), z, d


# ---------- Liquidationszonen (Positionsgröße, Forschung PRAEREGISTRIERUNG_maximum*.md) ----------
# Modell-Karte aus Open Interest (in Coins): Zuwachs je Stunde je zur Hälfte Long/Short, verteilt auf Hebel 5/10/20/25/50/100×;
# OI-Abbau verkleinert alle Bänder anteilig; ein Band verschwindet, sobald das Stundentief es kreuzt. LL3 = verbleibende Long-Liquidationen
# im Bereich [Einstieg·0,97; Einstieg) / aktuelles OI-Nominal. 90 Tage Vorlauf ergeben dieselbe Einstufung wie die volle Geschichte (400/400).
LIQ_LEV = (5, 10, 20, 25, 50, 100)
LIQ_W = (0.10, 0.25, 0.20, 0.20, 0.15, 0.10)
LIQ_MM, LIQ_STEP = 0.005, 0.002
LL3_SCHWELLE = 0.004591825032372134       # Median der 30 Forschungs-Coins
LL3_VORLAUF_H = 90 * 24


def ll3(stunden, e, low_seit_stunde) -> float | None:
    """stunden = [(close, high, low, oi)] aufeinanderfolgender voller Stunden bis zur Stunde VOR der Signalstunde (älteste zuerst,
    oi None = fehlt → letzter Wert gilt). e = Schluss der Signal-5m-Kerze, low_seit_stunde = tiefstes 5m-Tief seit Beginn der Signalstunde."""
    if len(stunden) < 48:
        return None
    lows = [s[2] for s in stunden if s[2] and s[2] > 0] + [low_seit_stunde]
    lo_all = math.log(min(lows) * 0.3)
    nb = int((math.log(max(s[1] for s in stunden) * 3) - lo_all) / LIQ_STEP) + 2
    def b(p):
        return min(nb - 1, max(0, int((math.log(p) - lo_all) / LIQ_STEP)))
    L = [0.0] * nb
    top = -1                                   # höchster belegter Index (Nullsetzen nur bis dorthin)
    oi_prev = None; oi_last = None; c_last = None
    for c, hi, lo, oi in stunden:
        oi = oi if oi is not None else oi_last
        if oi is not None and oi_prev is not None:
            d = oi - oi_prev
            if d > 0:
                for lev, w in zip(LIQ_LEV, LIQ_W):
                    i = b(c * (1 - 1 / lev + LIQ_MM)); L[i] += d * 0.5 * w * c; top = max(top, i)
            elif d < 0 and oi_prev > 0:
                f = max(0.0, 1 + d / oi_prev)
                for i in range(top + 1):
                    if L[i]: L[i] *= f
        for i in range(b(lo), top + 1):
            L[i] = 0.0
        if oi is not None:
            oi_prev = oi_last = oi
        c_last = c
    for i in range(b(low_seit_stunde), top + 1):
        L[i] = 0.0
    if not oi_last or not c_last:
        return None
    return sum(L[b(e * 0.97):b(e)]) / (oi_last * c_last)


def einsatz(ll3_wert, wenig=0.10, viel=0.05) -> float:
    """Positionsgröße: wenig Liquidationen unter dem Kurs → größer. Unbekannt → kleiner Einsatz."""
    return wenig if (ll3_wert is not None and ll3_wert <= LL3_SCHWELLE) else viel


def signal(closes, ois, g_now, g_72h_ago) -> tuple[bool, dict]:
    """Vollständige Entscheidung am Schluss von Kerze t. Rückgabe (Kaufen?, Merkmale)."""
    ok, z, d = price_ok(closes)
    o = oi_drop(ois) if ok else None
    rf = retail_falling(g_now, g_72h_ago) if ok else False
    buy = bool(ok and o is not None and o <= OI_DROP and rf)
    return buy, {'z': z, 'dd': d, 'oi_drop': o, 'retail_falling': rf}

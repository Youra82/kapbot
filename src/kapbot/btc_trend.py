"""BTC-Trend BEIDSEITIG (RESEARCH_KANDIDATEN §42/§43/§53/§57) – Claude, 12./13.11.2026.

Freies Kapital arbeitet mit dem BTC-Trend:
  Long  mit Nominal = faktor × Kontowert, solange BTC am Vortag über SMA200 UND SMA50 schloss (Hebel lev, Standard 5x),
  Short mit Nominal = short_faktor × Kontowert, solange BTC am Vortag unter SMA200 UND SMA50 schloss (Hebel short_lev,
  §57: 1,0× bei 3x), sonst flach. Entscheidung einmal je UTC-Tag; Seitenwechsel = schließen, dann neu eröffnen.
Vorrang der Arten (§53): Braucht eine Art mehr Kasse als frei ist, gibt der BTC-Trend sofort anteilig Margin ab
(free_margin); am nächsten Tag stockt step wieder auf das Ziel auf.
Papier: Bitget-Kurse mit Spread und Taker-Gebühr; Funding pauschal 10 %/J (Long) bzw. 5 %/J (Short) auf das Nominal;
Liquidation über die 1h-Tiefs (Long) bzw. 1h-Hochs (Short) seit Einstieg. Echtgeld über kapbot.bitget (Hedge-Modus, BTCUSDT;
die Arten handeln kein BTC). Gleiche Regeln wie tools/gesamt_ereignis2.simulate.
"""
from __future__ import annotations
import json, urllib.request

LEV = 5
SHORT_LEV = 3
FUNDING_JAHR = 0.10
FUNDING_SHORT = 0.05
SPREAD, FEE = 0.0005, 0.0006
LIQ_BUFFER = 0.005
AUFSTOCKEN_AB = 0.05             # Lücke zum Ziel ≥ 5 % → aufstocken
DAY = 86_400_000
SYM = 'BTCUSDT'


def regime(daily_closes):
    """True, wenn der letzte Tagesschluss (Vortag) über SMA200 und SMA50 liegt. daily_closes bis einschließlich Vortag."""
    d = daily_closes
    if len(d) < 200:
        return False
    return d[-1] > sum(d[-200:]) / 200 and d[-1] > sum(d[-50:]) / 50


def regime_short(daily_closes):
    """True, wenn der letzte Tagesschluss (Vortag) unter SMA200 und SMA50 liegt."""
    d = daily_closes
    if len(d) < 200:
        return False
    return d[-1] < sum(d[-200:]) / 200 and d[-1] < sum(d[-50:]) / 50


def _candles_1h():
    d = json.loads(urllib.request.urlopen('https://api.bitget.com/api/v2/mix/market/candles?symbol=BTCUSDT'
                                          '&productType=USDT-FUTURES&granularity=1H&limit=1000', timeout=20).read())
    return d.get('data') or []


def btc_price():
    d = json.loads(urllib.request.urlopen('https://api.bitget.com/api/v2/mix/market/ticker?symbol=BTCUSDT'
                                          '&productType=USDT-FUTURES', timeout=20).read())
    return float(d['data'][0]['lastPr'])


def btc_low_since(t_ms):
    """Tiefster 1h-Kurs (Bitget) seit t_ms; None, wenn keine Daten."""
    lows = [float(c[3]) for c in _candles_1h() if int(c[0]) + 3_600_000 > t_ms]
    return min(lows) if lows else None


def btc_high_since(t_ms):
    """Höchster 1h-Kurs (Bitget) seit t_ms; None, wenn keine Daten."""
    highs = [float(c[2]) for c in _candles_1h() if int(c[0]) + 3_600_000 > t_ms]
    return max(highs) if highs else None


def side_of(pos):
    return pos.get('side', 1)


def pnl(pos, px_exit, now_ms, frac=1.0):
    """Netto-Ergebnis in USDT für den Anteil frac einer Papier-Position (Long oder Short)."""
    days = max(0.0, (now_ms - pos['t_ms']) / DAY)
    n = pos['notional'] * frac
    if side_of(pos) == 1:
        gross = n * (px_exit * (1 - SPREAD) / (pos['entry_px'] * (1 + SPREAD)) - 1); fund = FUNDING_JAHR
    else:
        gross = n * (1 - px_exit * (1 + SPREAD) / (pos['entry_px'] * (1 - SPREAD))); fund = FUNDING_SHORT
    return gross - n * FEE * 2 - n * fund * days / 365


def _book_close(k, st, pos, res, frac, now_ms, log, grund, exit_px=None):
    m = pos['margin'] * frac
    k.s['cash'] += max(0.0, m + res)
    k.s.setdefault('closed', []).append({'art': 'btc_trend' if side_of(pos) == 1 else 'btc_trend_short', 'coin': 'BTC',
                                         'stake': m, 'lev': pos.get('lev', LEV), 'r': res / (pos['notional'] * frac),
                                         'entry_ms': pos['t_ms'], 'closed_ms': now_ms, 'grund': grund,
                                         'entry_px': pos['entry_px'], 'exit_px': exit_px})
    if frac >= 1.0 - 1e-9:
        st['pos'] = None
    else:
        for f in ('margin', 'notional', 'size', 'fee_in'):
            if f in pos:
                pos[f] *= 1 - frac


def _hold(pos):
    return 'long' if side_of(pos) == 1 else 'short'


def _close(k, st, pos, frac, now_ms, log, live, price_fn, grund):
    """Anteil frac schließen (Papier oder Echtgeld). False, wenn Echtgeld-Ausstieg unbestätigt."""
    if live is not None:
        from . import bitget as BG
        c = live.contract(SYM)
        size = pos['size'] if frac >= 1.0 - 1e-9 else BG.size_for(pos['margin'] * frac, pos.get('lev', LEV), live.last_price(SYM), c)
        if not size:
            size, frac = pos['size'], 1.0                       # Teil unter Mindestmenge → ganz schließen
        frac = min(1.0, size / pos['size'])
        f = BG.fill(live.market(SYM, _hold(pos), size, BG.client_oid('btc_trend', 'BTC', now_ms, f'close{frac:.4f}'), close=True))
        if f is None:
            log.append('BTC-Trend: Ausstieg nicht bestätigt – nächster Lauf prüft erneut'); return False
        res = side_of(pos) * (f[0] - pos['entry_px']) * f[1] - pos.get('fee_in', 0) * frac - f[2]; x = f[0]
    else:
        x = price_fn(); res = pnl(pos, x, now_ms, frac)
    _book_close(k, st, pos, res, frac, now_ms, log, grund, x)
    log.append(f"BTC-Trend {'Long' if side_of(pos) == 1 else 'Short'} "
               f"{'geschlossen' if frac >= 1 - 1e-9 else f'{100 * frac:.0f} % abgegeben'} ({grund}): {res:+.2f} USDT")
    return True


def liquidation_check(k, now_ms, log, live=None, low_fn=btc_low_since, high_fn=btc_high_since):
    st = k.s.get('btc_trend') or {}
    pos = st.get('pos')
    if not pos:
        return
    lev = pos.get('lev', LEV)
    if live is not None:
        gone = live.positions().get((SYM, _hold(pos)), 0.0) + 1e-9 < pos.get('size', 0)
    else:
        since = pos.get('liq_checked_ms', pos['t_ms'])
        if side_of(pos) == 1:
            lo = low_fn(since); gone = lo is not None and lo <= pos['entry_px'] * (1 - 1 / lev + LIQ_BUFFER)
        else:
            hi = high_fn(since); gone = hi is not None and hi >= pos['entry_px'] * (1 + 1 / lev - LIQ_BUFFER)
        pos['liq_checked_ms'] = now_ms - 3_600_000
    if gone:
        _book_close(k, st, pos, -pos['margin'], 1.0, now_ms, log, 'LIQUIDIERT',
                    pos['entry_px'] * (1 + (1 / lev - LIQ_BUFFER) * (-1 if side_of(pos) == 1 else 1)))
        log.append('BTC-Trend LIQUIDIERT – Margin verloren')


def free_margin(k, need, now_ms, log, live=None, price_fn=btc_price):
    """Vorrang der Arten: bis zu 'need' USDT Margin freigeben, indem die BTC-Position anteilig geschlossen wird."""
    st = k.s.get('btc_trend') or {}
    pos = st.get('pos')
    if not pos or need <= 0:
        return 0.0
    frac = min(1.0, need / pos['margin'])
    before = k.s['cash']
    _close(k, st, pos, frac, now_ms, log, live, price_fn, 'Vorrang Arten')
    return k.s['cash'] - before


def step(k, daily, now_ms, log, faktor, live=None, price_fn=btc_price, lev=LEV, low_fn=btc_low_since,
         short_faktor=0.0, short_lev=SHORT_LEV, high_fn=btc_high_since):
    """Stündlich aufrufbar: Liquidation prüfen; einmal je UTC-Tag Regime prüfen, öffnen/schließen/wechseln/aufstocken."""
    st = k.s.setdefault('btc_trend', {'pos': None, 'tag': None})
    liquidation_check(k, now_ms, log, live, low_fn, high_fn)
    tag = now_ms // DAY
    if st['tag'] == tag:
        return
    closes = [c for t, c in daily if t < tag * DAY]
    want = 1 if (faktor > 0 and regime(closes)) else (-1 if (short_faktor > 0 and regime_short(closes)) else 0)
    pos = st['pos']
    if pos and side_of(pos) != want:
        if not _close(k, st, pos, 1.0, now_ms, log, live, price_fn, 'Regime aus' if want == 0 else 'Seitenwechsel'):
            return
        pos = None
    if want != 0:
        fk, Lv = (faktor, lev) if want == 1 else (short_faktor, short_lev)
        eq = k.equity(); target = fk * eq / Lv
        have = pos['margin'] if pos else 0.0
        if target - have > (AUFSTOCKEN_AB * target if pos else 0.0):
            margin = min(k.s['cash'], target - have)
            if margin * Lv >= 5:
                px = price_fn(); extra = {}
                hold = 'long' if want == 1 else 'short'
                if live is not None:
                    from . import bitget as BG
                    c = live.contract(SYM); size = BG.size_for(margin, Lv, px, c) if c else None
                    if not size:
                        log.append('BTC-Trend: Menge unter Minimum'); st['tag'] = tag; return
                    live.prepare(SYM, Lv)
                    f = BG.fill(live.market(SYM, hold, size, BG.client_oid('btc_trend', 'BTC', now_ms, f'open-{hold}')))
                    if f is None:
                        log.append('BTC-Trend: Einstieg nicht bestätigt'); return
                    px, q, fee = f; margin = px * q / Lv; extra = {'size': q, 'fee_in': fee}
                k.s['cash'] -= margin
                n = margin * Lv
                if pos:                                          # aufstocken: Ø-Einstand, Zeit gewichtet
                    tot = pos['notional'] + n
                    pos['entry_px'] = tot / (pos['notional'] / pos['entry_px'] + n / px)
                    pos['t_ms'] = int((pos['t_ms'] * pos['notional'] + now_ms * n) / tot)
                    pos['notional'] = tot; pos['margin'] += margin
                    for f_ in ('size', 'fee_in'):
                        if f_ in extra: pos[f_] = pos.get(f_, 0) + extra[f_]
                    log.append(f"BTC-Trend {hold} aufgestockt: +{n:.2f} USDT Nominal zu {px:.1f}")
                else:
                    st['pos'] = {'t_ms': now_ms, 'entry_px': px, 'notional': n, 'margin': margin, 'lev': Lv, 'side': want, **extra}
                    log.append(f"BTC-Trend {hold} eröffnet: {n:.2f} USDT Nominal ({fk:g}× Konto, {Lv}x) zu {px:.1f}")
    st['tag'] = tag


def margin_open(k):
    pos = (k.s.get('btc_trend') or {}).get('pos')
    return pos['margin'] if pos else 0.0

"""BTC-Trend BEIDSEITIG (RESEARCH_KANDIDATEN §42/§43/§53/§57) – Claude, 12./13.11.2026; Echtgeld-Absicherung 21.11.2026 (Codex-Review).

Freies Kapital arbeitet mit dem BTC-Trend:
  Long  mit Nominal = faktor × Kontowert, solange BTC am Vortag über SMA200 UND SMA50 schloss (Hebel lev, Standard 5x),
  Short mit Nominal = short_faktor × Kontowert, solange BTC am Vortag unter SMA200 UND SMA50 schloss (Hebel short_lev,
  §57: 1,0× bei 3x), sonst flach. Entscheidung einmal je UTC-Tag; Seitenwechsel = schließen, dann neu eröffnen.
Vorrang der Arten (§53): Braucht eine Art mehr Kasse als frei ist, gibt der BTC-Trend sofort anteilig Margin ab
(free_margin); am nächsten Tag stockt step wieder auf das Ziel auf.
Papier: Bitget-Kurse mit Spread und Taker-Gebühr; Funding pauschal 10 %/J (Long) bzw. 5 %/J (Short) auf das Nominal;
Liquidation über die 5m-Tiefs (Long) bzw. 5m-Hochs (Short) seit der letzten Prüfung. Echtgeld über kapbot.bitget (Hedge-Modus, BTCUSDT).

Echtgeld-Absicherung: Jede Order bekommt eine fortlaufende, im Konto gespeicherte clientOid („Absicht“), die VOR dem Senden
gesichert wird. Ist das Ergebnis unklar (Netzfehler, Order noch offen, Absturz), schlägt der nächste Lauf genau diese Order nach
und bucht die tatsächlich gefüllte Menge – nie eine zweite Order für dieselbe Absicht. Liquidation wird erst geprüft, wenn keine
Absicht offen ist. BTC gehört immer nur einer Strategie: solange eine Kapitulations-Position auf BTC offen/vorgemerkt ist,
eröffnet oder vergrößert der Trend nicht.
"""
from __future__ import annotations
import json, math, urllib.request

LEV = 5
SHORT_LEV = 3
FUNDING_JAHR = 0.10
FUNDING_SHORT = 0.05
SPREAD, FEE = 0.0005, 0.0006
LIQ_BUFFER = 0.005
AUFSTOCKEN_AB = 0.05             # Lücke zum Ziel ≥ 5 % → aufstocken
DAY = 86_400_000
SYM = 'BTCUSDT'
INTENT_VERFALL_MS = 15 * 60_000  # Absicht ohne auffindbare Order frühestens nach 15 min UND 3 leeren Abfragen verwerfen
ABDECKUNG_MS = 15 * 60_000       # Papier-Liquidation: neueste Kerze darf höchstens so alt sein


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


def _candles(granularity, limit):
    d = json.loads(urllib.request.urlopen('https://api.bitget.com/api/v2/mix/market/candles?symbol=BTCUSDT'
                                          f'&productType=USDT-FUTURES&granularity={granularity}&limit={limit}', timeout=20).read())
    return d.get('data') or []


def btc_price():
    d = json.loads(urllib.request.urlopen('https://api.bitget.com/api/v2/mix/market/ticker?symbol=BTCUSDT'
                                          '&productType=USDT-FUTURES', timeout=20).read())
    return float(d['data'][0]['lastPr'])


def _extreme_since(t_ms, col, fn):
    """5m-Kerzen (≈ 3,5 Tage) ab der Kerze, die t_ms enthält; älter → 1h-Kerzen (≈ 41 Tage). None, wenn keine Daten."""
    import time
    now = int(time.time() * 1000)
    rows, step = (_candles('5m', 1000), 300_000) if now - t_ms < 80 * 3_600_000 else (_candles('1H', 1000), 3_600_000)
    if not rows or max(int(c[0]) for c in rows) + step < now - ABDECKUNG_MS:
        return None                                          # Daten veraltet/leer → keine Aussage, Prüfzeitpunkt bleibt stehen
    if min(int(c[0]) for c in rows) > t_ms:
        return None                                          # Fenster nicht vollständig abgedeckt
    vals = [float(c[col]) for c in rows if int(c[0]) + step > t_ms]
    return fn(vals) if vals else None


def btc_low_since(t_ms):
    return _extreme_since(t_ms, 3, min)


def btc_high_since(t_ms):
    return _extreme_since(t_ms, 2, max)


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
                                         'stake': m, 'lev': pos.get('lev', LEV), 'r': max(res, -m) / (pos['notional'] * frac),
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


# ---------- Echtgeld: Order-Absichten ----------
def _neue_absicht(k, st, art, hold, size, close, meta, now_ms):
    from . import bitget as BG
    st['seq'] = st.get('seq', 0) + 1
    oid = BG.client_oid('btc_trend', 'BTC', f"{k.s.get('live_since', 0)}-{st['seq']}", art)
    st['intent'] = {'oid': oid, 'art': art, 'hold': hold, 'size': size, 'close': close, 'meta': meta, 't': now_ms}
    k.save()                                                # VOR dem Senden sichern
    return oid


def _menge_erklaert(k, st, live):
    """Stimmt die Börsenmenge BTC mit dem überein, was der Bot kennt (Trend + Kapitulation)? Sonst Absicht nicht verwerfen."""
    if live is None:
        return True
    try:
        pos = live.positions()
    except Exception:
        return False
    tp = st.get('pos') or {}
    own_l = (tp.get('size', 0) if side_of(tp) == 1 else 0) + sum(p.get('size', 0) for p in k.s.get('positions', [])
                                                                 if p.get('coin') == 'BTC' and p.get('live'))
    own_s = tp.get('size', 0) if tp and side_of(tp) == -1 else 0
    return pos.get((SYM, 'long'), 0) <= own_l + 1e-9 and pos.get((SYM, 'short'), 0) <= own_s + 1e-9


def _abrechnen(k, st, o, now_ms, log, live=None):
    """Order-Zustand einer Absicht buchen. True = gebucht/erledigt, None = noch unklar (Absicht bleibt)."""
    from . import bitget as BG
    it = st['intent']
    state = (o or {}).get('state')
    if o is None or state not in ('filled', 'canceled', 'cancelled'):
        if o is None:
            it['leer'] = it.get('leer', 0) + 1
            if it['leer'] >= 3 and now_ms - it['t'] >= INTENT_VERFALL_MS and _menge_erklaert(k, st, live):
                log.append('BTC-Trend: Order nie bei Bitget angekommen – Absicht verworfen'); st['intent'] = None; k.save(); return True
            k.save()
        log.append('BTC-Trend: Order-Ergebnis noch unklar – nächster Lauf prüft erneut'); return None
    px, q, fee = BG.partial(o)
    st['intent'] = None
    if q <= 0:
        log.append(f"BTC-Trend: {'Ausstieg' if it['close'] else 'Einstieg'} nicht gefüllt"); return True
    m = it['meta']
    if it['close']:
        pos = st['pos']
        if not pos:
            log.append('BTC-Trend: Ausstieg gefüllt, Position schon ausgebucht'); return True
        frac = min(1.0, q / pos['size'])
        if pos['size'] - q < 1e-12:
            frac = 1.0
        res = side_of(pos) * (px - pos['entry_px']) * q - pos.get('fee_in', 0) * frac - fee
        try:                                                 # echtes Funding auf die geschlossene Menge
            from .markt import bitget_funding_anteil
            res += bitget_funding_anteil(SYM, pos['t_ms'], now_ms, side_of(pos)) * pos['entry_px'] * q
        except Exception:
            pass
        _book_close(k, st, pos, res, frac, now_ms, log, m['grund'], px)
        log.append(f"BTC-Trend {'Long' if side_of(pos) == 1 else 'Short'} "
                   f"{'geschlossen' if frac >= 1 - 1e-9 else f'{100 * frac:.0f} % abgegeben'} ({m['grund']}): {res:+.2f} USDT")
    else:
        Lv = m['lev']; margin = px * q / Lv
        _buche_einstieg(k, st, px, margin, Lv, m['side'], now_ms, log, {'size': q, 'fee_in': fee}, m.get('fk', 0))
    return True


def _absicht_aufloesen(k, st, live, now_ms, log):
    it = st.get('intent')
    if not it:
        return True
    try:
        o = live.order(SYM, it['oid'])
    except Exception as e:
        log.append(f'BTC-Trend: Order-Abfrage {type(e).__name__}'); return None
    return _abrechnen(k, st, o, now_ms, log, live)


def _senden(k, st, live, art, hold, size, close, meta, now_ms, log):
    oid = _neue_absicht(k, st, art, hold, size, close, meta, now_ms)
    try:
        o = live.market(SYM, hold, size, oid, close=close)
    except Exception as e:
        log.append(f'BTC-Trend: Order {type(e).__name__} – Absicht bleibt, nächster Lauf prüft'); return None
    return _abrechnen(k, st, o, now_ms, log, live)


def _teilmenge(pos, frac, c):
    """Close-Menge aus der EIGENEN Stückzahl: frac × size, auf den Kontraktschritt abgerundet, höchstens die ganze Position."""
    if frac >= 1.0 - 1e-9:
        return pos['size']
    step = float(c['sizeMultiplier']); places = int(c['volumePlace'])
    q = round(math.floor(pos['size'] * frac / step + 1e-9) * step, places)
    q = min(q, pos['size'])
    if q < float(c['minTradeNum']):
        return None
    if pos['size'] - q < float(c['minTradeNum']):          # Rest wäre unter Mindestmenge → ganz schließen
        return pos['size']
    return q


def _close(k, st, pos, frac, now_ms, log, live, price_fn, grund):
    """Anteil frac schließen (Papier oder Echtgeld). False, wenn Echtgeld-Ausstieg unbestätigt oder nicht möglich."""
    if live is not None:
        c = live.contract(SYM)
        size = _teilmenge(pos, frac, c) if c else None
        if not size:
            log.append(f'BTC-Trend: Teilabgabe {100 * frac:.0f} % unter Mindestmenge – nicht abgegeben'); return False
        return bool(_senden(k, st, live, 'close', _hold(pos), size, True, {'grund': grund}, now_ms, log))
    x = price_fn(); res = pnl(pos, x, now_ms, frac)
    _book_close(k, st, pos, res, frac, now_ms, log, grund, x)
    log.append(f"BTC-Trend {'Long' if side_of(pos) == 1 else 'Short'} "
               f"{'geschlossen' if frac >= 1 - 1e-9 else f'{100 * frac:.0f} % abgegeben'} ({grund}): {res:+.2f} USDT")
    return True


def liquidation_check(k, now_ms, log, live=None, low_fn=btc_low_since, high_fn=btc_high_since):
    st = k.s.get('btc_trend') or {}
    pos = st.get('pos')
    if not pos or st.get('intent'):
        return                                               # offene Absicht zuerst klären (sonst falsche Liquidation)
    lev = pos.get('lev', LEV)
    if live is not None:
        gone = live.positions().get((SYM, _hold(pos)), 0.0) + 1e-9 < pos.get('size', 0)
    else:
        since = pos.get('liq_checked_ms', pos['t_ms'])
        if side_of(pos) == 1:
            ex = low_fn(since); gone = ex is not None and ex <= pos['entry_px'] * (1 - 1 / lev + LIQ_BUFFER)
        else:
            ex = high_fn(since); gone = ex is not None and ex >= pos['entry_px'] * (1 + 1 / lev - LIQ_BUFFER)
        if ex is not None:
            pos['liq_checked_ms'] = max(since, now_ms - 3_600_000)   # nur weiterrücken, wenn Daten da waren
    if gone:
        _book_close(k, st, pos, -pos['margin'], 1.0, now_ms, log, 'LIQUIDIERT',
                    pos['entry_px'] * (1 + (1 / lev - LIQ_BUFFER) * (-1 if side_of(pos) == 1 else 1)))
        log.append('BTC-Trend LIQUIDIERT – Margin verloren')


def free_margin(k, need, now_ms, log, live=None, price_fn=btc_price):
    """Vorrang der Arten: bis zu 'need' USDT in die Kasse holen, indem die BTC-Position anteilig geschlossen wird.
    Anteil nach geschätztem Nettoerlös (Margin + anteiliger Gewinn/Verlust), nicht nach Margin allein."""
    st = k.s.get('btc_trend') or {}
    pos = st.get('pos')
    if not pos or need <= 0 or st.get('intent'):
        return 0.0
    try:
        if live is not None and pos.get('size'):             # Echtgeld: echte Menge, Einstiegsgebühr, Ausstiegsgebühr
            px = live.last_price(SYM) or price_fn()
            est = pos['margin'] + side_of(pos) * (px - pos['entry_px']) * pos['size'] - pos.get('fee_in', 0) - px * pos['size'] * FEE
        else:
            est = pos['margin'] + pnl(pos, price_fn(), now_ms, 1.0)
    except Exception:
        est = pos['margin']
    frac = 1.0 if est <= 0 else min(1.0, need / est)
    before = k.s['cash']
    _close(k, st, pos, frac, now_ms, log, live, price_fn, 'Vorrang Arten')
    return k.s['cash'] - before


def _buche_einstieg(k, st, px, margin, Lv, want, now_ms, log, extra, fk):
    k.s['cash'] -= margin
    n = margin * Lv; pos = st.get('pos'); hold = 'long' if want == 1 else 'short'
    if pos:                                                  # aufstocken: Ø-Einstand, Zeit gewichtet
        tot = pos['notional'] + n
        pos['entry_px'] = tot / (pos['notional'] / pos['entry_px'] + n / px)
        pos['t_ms'] = int((pos['t_ms'] * pos['notional'] + now_ms * n) / tot)
        pos['notional'] = tot; pos['margin'] += margin
        pos['liq_checked_ms'] = now_ms                       # ältere Tiefs galten dem alten Einstand
        for f_ in ('size', 'fee_in'):
            if f_ in extra: pos[f_] = pos.get(f_, 0) + extra[f_]
        log.append(f"BTC-Trend {hold} aufgestockt: +{n:.2f} USDT Nominal zu {px:.1f}")
    else:
        st['pos'] = {'t_ms': now_ms, 'entry_px': px, 'notional': n, 'margin': margin, 'lev': Lv, 'side': want, **extra}
        log.append(f"BTC-Trend {hold} eröffnet: {n:.2f} USDT Nominal ({fk:g}× Konto, {Lv}x) zu {px:.1f}")


def _btc_belegt(k):
    """Kapitulations-Position (oder vorgemerkter Einstieg) auf BTC → BTC gehört gerade der Kapitulation."""
    return (any(p.get('coin') == 'BTC' for p in k.s.get('positions', []))
            or any(v.get('coin') == 'BTC' for v in k.s.get('pending_live', [])))


def step(k, daily, now_ms, log, faktor, live=None, price_fn=btc_price, lev=LEV, low_fn=btc_low_since,
         short_faktor=0.0, short_lev=SHORT_LEV, high_fn=btc_high_since):
    """Je Lauf: offene Absicht klären, Liquidation prüfen, bei Faktor 0 sofort schließen; einmal je UTC-Tag Regime prüfen,
    öffnen/schließen/wechseln/aufstocken. daily = None (Tagesdaten nicht ladbar) → keine Regime-Entscheidung in diesem Lauf."""
    st = k.s.setdefault('btc_trend', {'pos': None, 'tag': None})
    if live is not None and _absicht_aufloesen(k, st, live, now_ms, log) is None:
        return
    try:
        liquidation_check(k, now_ms, log, live, low_fn, high_fn)
    except Exception as e:                                   # Abfragefehler darf Abschalten/Regime nicht blockieren
        log.append(f'BTC-Trend Liquidationsprüfung: {type(e).__name__}')
    pos = st['pos']
    if pos and ((side_of(pos) == 1 and faktor <= 0) or (side_of(pos) == -1 and short_faktor <= 0)):
        _close(k, st, pos, 1.0, now_ms, log, live, price_fn, 'ausgeschaltet')
        return
    tag = now_ms // DAY
    if st['tag'] == tag or daily is None:
        return
    closes = [c for t, c in daily if t < tag * DAY]
    if len(closes) < 200:
        log.append(f'BTC-Trend: nur {len(closes)} Tagesschlüsse – keine Regime-Entscheidung'); return
    want = 1 if (faktor > 0 and regime(closes)) else (-1 if (short_faktor > 0 and regime_short(closes)) else 0)
    if pos and side_of(pos) != want:
        if not _close(k, st, pos, 1.0, now_ms, log, live, price_fn, 'Regime aus' if want == 0 else 'Seitenwechsel'):
            return
        if st.get('pos'):                                    # Echtgeld: nur teilweise gefüllt → nächster Lauf
            return
        pos = None
    if want != 0:
        if _btc_belegt(k):
            log.append('BTC-Trend: BTC hat gerade eine Kapitulations-Position – kein Einstieg/Aufstocken'); return
        fk, Lv = (faktor, lev) if want == 1 else (short_faktor, short_lev)
        if pos and pos.get('lev', Lv) != Lv:
            Lv = pos['lev']                                  # Hebel einer offenen Position bleibt, bis sie geschlossen ist
        eq = k.equity(); target = fk * eq / Lv
        have = pos['margin'] if pos else 0.0
        if target - have > (AUFSTOCKEN_AB * target if pos else 0.0):
            margin = min(k.s['cash'], target - have)
            if margin * Lv >= 5:
                hold = 'long' if want == 1 else 'short'
                if live is not None:
                    from . import bitget as BG
                    px = live.last_price(SYM) or price_fn()
                    c = live.contract(SYM); size = BG.size_for(margin, Lv, px, c) if c else None
                    if not size:
                        log.append('BTC-Trend: Menge unter Minimum'); st['tag'] = tag; return
                    live.prepare(SYM, Lv)
                    if _senden(k, st, live, 'open', hold, size, False, {'lev': Lv, 'side': want, 'fk': fk}, now_ms, log) is None:
                        return                               # unklar → Absicht bleibt, Tag erst nach Klärung
                else:
                    px = price_fn()
                    _buche_einstieg(k, st, px, margin, Lv, want, now_ms, log, {}, fk)
    st['tag'] = tag


def margin_open(k):
    pos = (k.s.get('btc_trend') or {}).get('pos')
    return pos['margin'] if pos else 0.0

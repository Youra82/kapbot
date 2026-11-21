"""Marktdaten (öffentlich), Papierkonto und Echtgeld-Hilfen für kapbot (Claude, 21.11.2026).
Übernommen aus dem evobot (arten.py, Stand e114a73) und auf kapbot reduziert; kapbot importiert nichts aus dem evobot."""
from __future__ import annotations
import json, os, time, urllib.request
from pathlib import Path

from . import btc_trend as BT

SPREAD = 0.0005                  # je Seite (Papier)
FEE = 0.0006                     # Bitget-Taker-Gebühr je Seite
LIQ_BUFFER = 0.01                # Liquidation, wenn das Tief ≤ −(1/Hebel − 1 %) unter dem Einstieg liegt
MIN_NOTIONAL = 5.0
FUNDING_MAX_VERSUCHE = 6         # Papier-Ausstieg wartet so viele Läufe auf die Funding-Sätze, danach ohne
BITGET_SYMBOL = {'1000PEPE': 'PEPE', '1000SHIB': 'SHIB'}


def _get(url, tries=4):
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(url, timeout=20).read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + 2 * i)


def binance_closes(sym, interval, limit):
    """Abgeschlossene Binance-USDT-M-Kerzen: [(open_ms, close)]."""
    rows = _get(f'https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT&interval={interval}&limit={limit}')
    now = int(time.time() * 1000)
    return [(int(r[0]), float(r[4])) for r in rows if int(r[6]) < now]


def bitget_open_at(coin, t_ms):
    """Eröffnungskurs der Bitget-1m-Kerze, die bei t_ms beginnt (oder der nächsten verfügbaren innerhalb 5 min)."""
    sym = BITGET_SYMBOL.get(coin, coin) + 'USDT'
    d = _get('https://api.bitget.com/api/v2/mix/market/history-candles?symbol=' + sym +
             f'&productType=USDT-FUTURES&granularity=1m&startTime={t_ms - 60_000}&endTime={t_ms + 5 * 60_000}&limit=10')
    rows = sorted((int(r[0]), float(r[1])) for r in (d.get('data') or []))
    for ts, op in rows:
        if t_ms <= ts <= t_ms + 5 * 60_000:
            return op
    return None


def bitget_min_low(coin, t_from, t_to):
    """Tiefster Kurs der Bitget-1h-Kerzen zwischen t_from und t_to (Liquidationsprüfung). None bei fehlenden Daten."""
    sym = BITGET_SYMBOL.get(coin, coin) + 'USDT'
    d = _get('https://api.bitget.com/api/v2/mix/market/history-candles?symbol=' + sym +
             f'&productType=USDT-FUTURES&granularity=1H&startTime={t_from}&endTime={t_to}&limit=48')
    lows = [float(r[3]) for r in (d.get('data') or []) if t_from <= int(r[0]) < t_to]
    return min(lows) if lows else None


def bitget_funding_anteil(symbol, von_ms, bis_ms, side=1):
    """Funding zwischen Einstieg und Ausstieg als Anteil am Positionswert: Summe der echten Bitget-Sätze in (von_ms, bis_ms].
    Satz > 0: Long zahlt. side=1 Long (Rückgabe −Summe), side=−1 Short (+Summe)."""
    saetze = {}
    for n in range(1, 11):
        d = _get('https://api.bitget.com/api/v2/mix/market/history-fund-rate?symbol=' + symbol +
                 f'&productType=USDT-FUTURES&pageSize=100&pageNo={n}')
        if str(d.get('code')) != '00000':
            raise RuntimeError(f"Bitget Funding {symbol}: {d.get('msg')}")
        rows = d.get('data') or []
        saetze.update((int(x['fundingTime']), float(x['fundingRate'])) for x in rows)
        if len(rows) < 100 or min(int(x['fundingTime']) for x in rows) <= von_ms:
            break
    return -side * sum(r for t, r in saetze.items() if von_ms < t <= bis_ms)


class Konto:
    """Papier- bzw. Echtgeld-Buchführung (JSON). Format wie das evobot-Konto, damit das Cockpit es als 'evo' liest."""
    def __init__(self, path: Path):
        self.path = path
        self.s = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'start': 100.0, 'cash': 100.0, 'positions': [], 'closed': [], 'busy': {}}

    def save(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.s, indent=1), encoding='utf-8')
        os.replace(tmp, self.path)

    def units(self):
        return self.s.setdefault('units', self.s['start'])

    def einzahlung(self, amount, t_ms=None):
        """Ein- (+) oder Auszahlung (−) zum aktuellen Anteilswert (einzahlungsneutral)."""
        self.s['units'] = self.units() + amount / (self.equity() / self.units())
        self.s['cash'] += amount
        self.s.setdefault('einzahlungen', []).append({'t_ms': t_ms or int(time.time() * 1000), 'betrag': amount})

    def equity(self):
        return self.s['cash'] + sum(p['stake'] for p in self.s['positions']) + BT.margin_open(self)


# ---------- Echtgeld (nur mit Bitget-Zugang aus secret.json['kapbot']) ----------
def live_sync(k, live, now_ms, log):
    """Start aus dem Bitget-Guthaben, danach jede Ein-/Auszahlung als Konto.einzahlung."""
    if 'live_since' not in k.s:
        if k.s['positions'] or k.s['closed']:
            raise SystemExit('Echtgeld braucht ein eigenes, leeres Konto (data/echtgeld)')
        bal = float(live.account().get('available') or 0)
        if bal < MIN_NOTIONAL:
            raise SystemExit(f'Bitget-Futures-Guthaben {bal:.2f} USDT zu klein')
        k.s.update(start=bal, cash=bal, units=bal, live_since=now_ms, seen_bills=[])
        log.append(f'Echtgeld-Start mit {bal:.2f} USDT')
        return
    seen = set(k.s.setdefault('seen_bills', []))
    for bid, t, amt in live.transfers(k.s['live_since'], now_ms):
        if bid in seen:
            continue
        k.einzahlung(amt, t)
        k.s['seen_bills'].append(bid)
        log.append(f"{'Einzahlung' if amt > 0 else 'Auszahlung'} {amt:+.2f} USDT gebucht")


def live_entry(k, live, art, coin, margin, lev, t_in, log):
    """Echtgeld-Einstieg Long per Markt (isoliert, Hedge-Modus). Rückgabe (Kurs, Zusatzfelder) oder None."""
    from . import bitget as BG
    sym = BG.symbol(coin)
    c = live.contract(sym)
    if not c or c.get('symbolStatus', 'normal') != 'normal':
        log.append(f'{sym} auf Bitget nicht handelbar')
        return None
    ref = live.last_price(sym)
    size = BG.size_for(margin, lev, ref or 0, c)
    if size is None:
        log.append(f'{sym}: Margin {margin:.2f} zu klein für Mindestmenge')
        return None
    prep = k.s.setdefault('prepared', {})
    if prep.get(sym) != lev:
        live.prepare(sym, lev)
        prep[sym] = lev
    f = BG.fill(live.market(sym, 'long', size, BG.client_oid(art, coin, t_in, 'open')))
    if f is None:
        log.append(f'Einstieg {sym} nicht gefüllt/unklar – kein Konto-Eintrag')
        return None
    px, qty, fee = f
    return px, {'size': qty, 'fee_in': fee, 'live': True}


def live_exit(k, live, p, log):
    """Eigene Menge per Markt schließen. Liegt auf der Börse weniger Long-Menge als alle eigenen offenen Long-Positionen
    dieses Coins, wurde liquidiert → als liquidiert buchen. Rückgabe (fill, liquidiert) bzw. ('offen', False)."""
    from . import bitget as BG
    sym = BG.symbol(p['coin'])
    have = live.positions().get((sym, 'long'), 0.0)
    own = sum(q['size'] for q in k.s['positions'] if q.get('size') and BG.symbol(q['coin']) == sym)
    if have + 1e-9 < own:
        return None, True
    f = BG.fill(live.market(sym, 'long', p['size'], BG.client_oid(p.get('genom', p['art']), p['coin'], p['entry_ms'], 'close'), close=True))
    if f is None:
        log.append(f"Ausstieg {p['coin']} nicht bestätigt – nächster Lauf prüft erneut")
        return 'offen', False
    return f, False

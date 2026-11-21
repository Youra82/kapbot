"""kapbot (Mini-PC, alle 5 Minuten): BTC-Trend + „Kapitulation kaufen“ aus einer Kasse (Claude, 21.11.2026).

Kapitulation: Binance-USDT-M-5m-Schlüsse + Open Interest + globalLongShortAccountRatio (öffentlich, nur lesen); Regel kapbot.regel,
Parität mit der Forschung 1 789/1 789 (tools/parity_signale.py). Einsatz nach Liquidationszone (Modell-Karte aus 90 Tagen OI,
tools/parity_ll3.py). BTC-Trend: kapbot.btc_trend (aus dem evobot übernommen), long solange BTC über SMA200 und SMA50.
Papier/Echtgeld wie beim jaegerbot über settings.json echtgeld {"aktiv", "papier_startkapital"}, je Lauf neu gelesen:
- Papier: data/papier/konto.json, Bitget-Kurse der Ausführungsminute, 0,05 % Spread + 0,06 % Gebühr je Seite, echtes Funding;
  Telegram mit „📝 PAPIER“.
- Echtgeld: data/echtgeld/konto.json, Bitget-Zugang secret.json['kapbot']. Fehlt er: keine Trades, einmalige Telegram-Meldung.
- Wechsel wird einmal gemeldet; fällige Ausstiege laufen in BEIDEN Konten weiter (Echtgeld nur mit Zugang).

Aufruf:  python -m kapbot run     [--data data] [--telegram]
         python -m kapbot bericht [--data data] [--modus papier|echtgeld]
Speicher: Kurs-Puffer 30 × 25 920 Kerzen (≈ 12 MB Platte), OI-Zwischenspeicher ≈ 2 MB; erster Lauf ≈ 10 min Nachladen, danach Sekunden.
"""
from __future__ import annotations
import argparse, json, os, time, urllib.request
from array import array
from datetime import datetime, timezone
from pathlib import Path

from . import regel as R
from . import settings as ST
from . import markt as A
from . import btc_trend as BT

BAR = R.BAR_MS
MAX_CATCHUP_BARS = 12            # Papier: bis 1 h nachholen
LIVE_MAX_DELAY_MS = 4 * 60_000   # Echtgeld: nur die gerade geschlossene Kerze (≤ 4 min alt)
PENDING_MAX_MS = 12 * 60_000     # fehlen OI/Long-Short für Kerze t noch, bis zu 12 min später erneut versuchen
KLINE_PAUSE_S = 0.35             # Gewichtsgrenze Binance beim Nachladen


# ---------- Binance (öffentlich) ----------
def klines_since(sym, start_ms, now_ms):
    """Abgeschlossene 5m-Kerzen ab start_ms: [(open_ms, close)]."""
    out = []
    while True:
        rows = A._get(f'https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT&interval=5m&startTime={start_ms}&limit=1500')
        rows = [(int(r[0]), float(r[4])) for r in rows if int(r[6]) < now_ms]
        out += rows
        if len(rows) < 1499:
            return out
        start_ms = rows[-1][0] + BAR
        time.sleep(KLINE_PAUSE_S)


def oi_window(sym, t_ms):
    """OI der 72 Kerzen bis t (None = fehlt). None insgesamt, wenn der Wert für t noch nicht veröffentlicht ist."""
    rows = A._get(f'https://fapi.binance.com/futures/data/openInterestHist?symbol={sym}USDT&period=5m'
                  f'&startTime={t_ms - (R.OI_N - 1) * BAR}&endTime={t_ms}&limit={R.OI_N}')
    m = {int(r['timestamp']): float(r['sumOpenInterest']) for r in rows}
    if t_ms not in m:
        return None
    return [m.get(t_ms - (R.OI_N - 1 - j) * BAR) for j in range(R.OI_N)]


def ratio_at(sym, t_ms):
    rows = A._get(f'https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={sym}USDT&period=5m'
                  f'&startTime={t_ms}&endTime={t_ms}&limit=1')
    for r in rows:
        if int(r['timestamp']) == t_ms:
            return float(r['longShortRatio'])
    return None



# ---------- Liquidationszonen live: stündliches Open Interest (Zwischenspeicher) + Karte beim Signal ----------
HOUR = 3_600_000


def _oi_datei(folder: Path, coin: str) -> Path:
    return folder / f'{coin}.json'


def _portal_tag(coin, tag):
    """Binance-Datenportal, Tages-Metrics (5m): {Stunde_ms: OI des letzten 5m-Werts der Stunde}."""
    import csv, io, zipfile
    url = f'https://data.binance.vision/data/futures/um/daily/metrics/{coin}USDT/{coin}USDT-metrics-{tag}.zip'
    try:
        raw_ = urllib.request.urlopen(url, timeout=60).read()
    except Exception:
        return {}
    z = zipfile.ZipFile(io.BytesIO(raw_)); txt = z.read(z.namelist()[0]).decode()
    out = {}
    for r in csv.DictReader(io.StringIO(txt)):
        try:
            t = int(datetime.strptime(r['create_time'][:16], '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp() * 1000)
            v = float(r['sum_open_interest'])
        except (KeyError, ValueError):
            continue
        h = t // HOUR * HOUR
        if v > 0 and (h not in out or t >= out[h][0]):
            out[h] = (t, v)
    return {h: v for h, (_, v) in out.items()}


def oi_update(folder: Path, coin: str, now_ms: int):
    """Hält das stündliche OI der letzten 92 Tage aktuell: Lücken > 40 h aus dem Datenportal (Tagesdateien), den Rest aus der API (5m)."""
    folder.mkdir(parents=True, exist_ok=True)
    f = _oi_datei(folder, coin)
    try:
        d = {int(k): v for k, v in json.loads(f.read_text(encoding='utf-8')).items()}
    except (OSError, ValueError):
        d = {}
    last_full = now_ms // HOUR * HOUR - HOUR
    if d and max(d) >= last_full:
        return
    start = now_ms // HOUR * HOUR - 92 * 24 * HOUR
    have = max(d) if d else start - HOUR
    if have < last_full - 40 * HOUR:                         # Datenportal für ganze Tage bis vorgestern
        from concurrent.futures import ThreadPoolExecutor
        t0 = max(start, have + HOUR) // 86_400_000 * 86_400_000
        tage = [datetime.fromtimestamp(t / 1000, timezone.utc).strftime('%Y-%m-%d')
                for t in range(t0, (now_ms // 86_400_000 - 1) * 86_400_000, 86_400_000)]
        with ThreadPoolExecutor(6) as ex:
            for part in ex.map(lambda tg: _portal_tag(coin, tg), tage):
                d.update(part)
    rows = A._get(f'https://fapi.binance.com/futures/data/openInterestHist?symbol={coin}USDT&period=5m&limit=500')
    best = {}
    for r in rows:
        t = int(r['timestamp']); h = t // HOUR * HOUR
        if h <= last_full and (h not in best or t >= best[h][0]):
            best[h] = (t, float(r['sumOpenInterest']))
    d.update({h: v for h, (_, v) in best.items()})
    d = {h: v for h, v in d.items() if h >= start}
    tmp = f.with_suffix('.tmp'); tmp.write_text(json.dumps(d), encoding='utf-8'); os.replace(tmp, f)


def stunden_kerzen(coin, von_ms, bis_ms):
    """Binance-1h-Kerzen [(open_ms, high, low, close)] von von_ms bis bis_ms (Eröffnungszeiten)."""
    out = []; s = von_ms
    while s <= bis_ms:
        rows = A._get(f'https://fapi.binance.com/fapi/v1/klines?symbol={coin}USDT&interval=1h&startTime={s}&endTime={bis_ms}&limit=1500')
        rows = [(int(r[0]), float(r[2]), float(r[3]), float(r[4])) for r in rows]
        out += rows
        if len(rows) < 1500:
            break
        s = rows[-1][0] + HOUR
    return out


def ll3_live(folder: Path, coin: str, t_bar: int, e: float):
    """LL3 für die Signal-5m-Kerze t_bar (wie Forschung, 90 Tage Vorlauf). None bei fehlenden Daten."""
    h0 = t_bar // HOUR * HOUR
    try:
        oi = {int(k): v for k, v in json.loads(_oi_datei(folder, coin).read_text(encoding='utf-8')).items()}
    except (OSError, ValueError):
        return None
    kz = stunden_kerzen(coin, h0 - R.LL3_VORLAUF_H * HOUR, h0 - HOUR)
    st = [(c, hi, lo, oi.get(t)) for t, hi, lo, c in kz]
    rows = A._get(f'https://fapi.binance.com/fapi/v1/klines?symbol={coin}USDT&interval=5m&startTime={h0}&endTime={t_bar}&limit=13')
    lows = [float(r[3]) for r in rows if int(r[0]) <= t_bar]
    if not lows or not st:
        return None
    return R.ll3(st, e, min(lows))


# ---------- Kurs-Puffer (Binärdateien, nur anhängen) ----------
class Puffer:
    def __init__(self, folder: Path, coin: str):
        self.ft, self.fc = folder / f'{coin}.t', folder / f'{coin}.c'
        self.t, self.c = array('q'), array('d')
        if self.ft.exists() and self.fc.exists():
            self.t.frombytes(self.ft.read_bytes()); self.c.frombytes(self.fc.read_bytes())
            n = min(len(self.t), len(self.c))
            if len(self.t) != len(self.c):                   # nach Absturz zwischen den zwei Schreibvorgängen: auf Platte angleichen
                del self.t[n:]; del self.c[n:]
                self.ft.write_bytes(self.t.tobytes()); self.fc.write_bytes(self.c.tobytes())

    def last(self):
        return self.t[-1] if self.t else None

    def append(self, rows):
        rows = [(t, c) for t, c in rows if not self.t or t > self.t[-1]]
        if not rows:
            return
        nt, nc = array('q', [t for t, _ in rows]), array('d', [c for _, c in rows])
        self.t.extend(nt); self.c.extend(nc)
        if len(self.t) > 2 * R.NEED_CLOSES:                 # gelegentlich kürzen und neu schreiben
            del self.t[:-R.NEED_CLOSES - 600]; del self.c[:-R.NEED_CLOSES - 600]
            self.ft.write_bytes(self.t.tobytes()); self.fc.write_bytes(self.c.tobytes())
        else:
            with open(self.ft, 'ab') as f: f.write(nt.tobytes())
            with open(self.fc, 'ab') as f: f.write(nc.tobytes())

    def window(self, t_ms):
        """Schlüsse bis einschließlich Kerze t (Liste), oder None, wenn t nicht im Puffer liegt."""
        i = len(self.t) - 1
        while i >= 0 and self.t[i] > t_ms:
            i -= 1
        if i < 0 or self.t[i] != t_ms:
            return None
        return self.c[max(0, i - R.NEED_CLOSES + 1):i + 1].tolist()


# ---------- Ablauf ----------
def _btc_price():
    return BT.btc_price()


def _btc_low(t_ms):
    return BT.btc_low_since(t_ms)


def _btc_daily():
    return A.binance_closes('BTC', '1d', 260)


def trend(k, live, faktor, hebel, now_ms, log):
    """BTC-Trend-Anteil der gelben Linie (kapbot.btc_trend, nur Long): Nominal = faktor × Kontowert bei Hebel hebel, solange BTC am
    Vortag über SMA200 und SMA50 schloss; sonst (oder faktor 0) geschlossen. Einmal je UTC-Tag, Liquidation jeden Lauf."""
    st = k.s.get('btc_trend') or {}
    if faktor <= 0 and not st.get('pos') and not st.get('intent'):
        return
    try:
        daily = _btc_daily() if faktor > 0 else None          # Fehler hier blockiert nicht Liquidationsprüfung/Schließen
    except Exception as e:
        daily = None; log.append(f'BTC-Trend Tagesdaten: {type(e).__name__}')
    try:
        BT.step(k, daily, now_ms, log, faktor, live, price_fn=_btc_price, lev=hebel, low_fn=_btc_low, short_faktor=0.0)
    except Exception as e:
        log.append(f'BTC-Trend Fehler: {type(e).__name__}')


def _paper_px(coin, t_ms):
    return A.bitget_open_at(coin, t_ms)


def exits(k, live, now_ms, log):
    for p in list(k.s['positions']):
        if p['exit_ms'] > now_ms - 60_000:
            continue
        lev = p.get('lev', 1)
        if p.get('live'):                                  # Echtgeld-Trade (auch mit Restmenge 0 nach Teil-Closes)
            if live is None:
                continue
            try:
                f, liquidated = A.live_exit(k, live, p, now_ms, log)
            except Exception as e:
                log.append(f"Ausstieg {p['coin']}: Fehler {type(e).__name__} – nächster Lauf versucht erneut"); continue
            if f == 'offen':
                continue
            if liquidated:
                px, r = None, A.live_liquidation_r(p)       # realisierte Teil-Closes zählen, Rest verliert die Margin
            else:
                px, qty, fee_out = f
                r = ((px - p['entry_px']) * qty - p.get('fee_in', 0.0) - fee_out) / (p['stake'] * lev)
                try:                                       # Funding auf die echte Menge (Anteil am Positionswert × Wert / Einsatz)
                    fa = A.bitget_funding_anteil(A.BITGET_SYMBOL.get(p['coin'], p['coin']) + 'USDT', p['entry_ms'], p['exit_ms'], 1)
                    p['funding_r'] = fa * p['entry_px'] * qty / (p['stake'] * lev)
                except Exception:
                    p['funding_r'], p['funding_unvollstaendig'] = 0.0, True
                r += p['funding_r']
        else:
            px = _paper_px(p['coin'], p['exit_ms'])
            if px is None:
                continue
            r = px * (1 - A.SPREAD) * (1 - A.FEE) / p['entry_eff'] - 1
            low = A.bitget_min_low(p['coin'], p['entry_ms'], p['exit_ms']) if lev > 1 else None
            liquidated = low is not None and low / p['entry_px'] - 1 <= -(1 / lev - A.LIQ_BUFFER)
            if liquidated:
                r = -1 / lev
            else:
                try:
                    p['funding_r'] = A.bitget_funding_anteil(A.BITGET_SYMBOL.get(p['coin'], p['coin']) + 'USDT',
                                                             p['entry_ms'], p['exit_ms'], 1)
                except Exception:
                    p['funding_versuche'] = p.get('funding_versuche', 0) + 1
                    if p['funding_versuche'] < A.FUNDING_MAX_VERSUCHE:
                        continue
                    p['funding_r'], p['funding_unvollstaendig'] = 0.0, True
                r += p['funding_r']
        k.s['cash'] += p['stake'] * max(0.0, 1 + lev * r)
        k.s['positions'].remove(p)
        k.s['closed'].append({**p, 'exit_px': px, 'r': r, 'liquidated': liquidated, 'closed_ms': now_ms})
        log.append(f"AUSSTIEG {p['coin']}: Kurs {100 * r:+.2f} %, Gewinn {p['stake'] * lev * r:+.2f} USDT"
                   + (' (LIQUIDIERT)' if liquidated else ''))


def pruefbar(k, coin, t):
    """Darf Kerze t dieses Coins überhaupt ein Signal werden? (Live und Nachspiel identisch.)"""
    if coin == 'BTC' and ((k.s.get('btc_trend') or {}).get('pos') or (k.s.get('btc_trend') or {}).get('intent')):
        return False                                   # BTC-Anteil offen: gleiches Symbol, anderer Hebel → kein kapbot-BTC
    if any(p['coin'] == coin for p in k.s['positions']) or any(v['coin'] == coin for v in k.s.get('pending_live', [])):
        return False                                   # alter Trade noch nicht geschlossen (z. B. Ausstieg fehlgeschlagen)
    return k.s['busy'].get(coin, 0) <= t


def kauf(k, live, sw, coin, t, feat, e_close, now_ms, log, l3_fn):
    """Kauf-Entscheidung und Buchung nach einem Signal in Kerze t (Live und Nachspiel identisch):
    Coin belegen, Höchstzahl, Einsatz nach Liquidationszone (l3_fn), Vorrang vor dem BTC-Anteil, Einstieg, Buchung."""
    k.s['busy'][coin] = t + R.HOLD_BARS * BAR          # wie Forschung: neues Signal ab der Kerze, in der der alte Trade endet
    belegt = len(k.s['positions']) + len(k.s.get('pending_live', []))
    if belegt >= sw['kap_max_offen']:
        log.append(f'{coin}: schon {belegt} Positionen offen – übersprungen'); return
    t_in = now_ms // 60_000 * 60_000
    if live is not None and t_in - (t + BAR) > LIVE_MAX_DELAY_MS:
        log.append(f'{coin}: Signal zu alt für Echtgeld'); return
    if live is not None and 'live_since' not in k.s:
        log.append('Echtgeld-Konto noch nicht initialisiert – kein Einstieg'); return
    lev = sw['kap_hebel']
    try:
        l3 = l3_fn()
    except Exception as e:
        l3 = None; log.append(f'Liquidationskarte {coin}: {type(e).__name__}')
    frac = R.einsatz(l3, sw['kap_einsatz'], sw['kap_einsatz_viel'])
    want = frac * k.equity()
    if want > k.s['cash'] and (k.s.get('btc_trend') or {}).get('pos'):
        try:                                       # Vorrang kapbot: BTC-Anteil gibt anteilig Margin ab
            BT.free_margin(k, want - k.s['cash'], now_ms, log, live, price_fn=_btc_price)
        except Exception as e:
            log.append(f'BTC-Trend Margin-Abgabe Fehler: {type(e).__name__}')
    stake = min(k.s['cash'], want)
    if stake * lev < A.MIN_NOTIONAL:
        log.append(f'{coin}: Einsatz {stake:.2f} USDT zu klein'); return
    meta = {'art': 'kapbot', 'genom': 'kapbot', 'exit_ms': t + (R.HOLD_BARS + 1) * BAR,
            'z': round(feat['z'], 2), 'dd': round(feat['dd'], 4), 'oi_drop': round(feat['oi_drop'], 4),
            'll3': None if l3 is None else round(l3, 6), 'einsatz_anteil': frac}
    if live is not None:
        pos = A.live_kauf(k, live, 'kapbot', coin, t, t_in, stake, lev, meta, log)
        if not isinstance(pos, dict):
            return
        px, stake = pos['entry_px'], pos['stake']
    else:
        px = _paper_px(coin, t_in)
        if px is None:
            log.append(f'kein Bitget-Kurs {coin}'); return
        k.s['cash'] -= stake
        k.s['positions'].append({**meta, 'coin': coin, 'signal_ms': t, 'entry_ms': t_in, 'entry_px': px,
                                 'entry_eff': px * (1 + A.SPREAD) * (1 + A.FEE), 'stake': stake, 'lev': lev})
    log.append(f"EINSTIEG {coin} Long {lev}x zu {px}, Einsatz {stake:.2f} USDT ({100 * frac:.0f} %, "
               f"{'wenig' if frac == sw['kap_einsatz'] else 'viel/unbekannt'} Liquidationen darunter)" + (' [ECHT]' if live is not None else ''))


def resolve_pending(k, live, log, now_ms):
    """Echtgeld-Einstiege mit unklarem Ergebnis per clientOid nachschlagen und buchen (markt.resolve_pending)."""
    for p in A.resolve_pending(k, live, now_ms, log):
        p['nachgebucht'] = True; k.save()
        log.append(f"EINSTIEG {p['coin']} nachgebucht (Order bestätigt) zu {p['entry_px']} [ECHT]")


def fremde_positionen(k, live, now_ms, log):
    """Mehr Long-Menge auf der Börse als eigene Positionen → ein anderer Bot (z. B. kapbot) handelt auf demselben Konto.
    Ausstiege schließen nur die eigene Menge, aber Liquidationen und Margin sind dann nicht mehr sauber zuzuordnen."""
    from . import bitget as BG
    try:
        have = live.positions()
    except Exception:
        return
    own = {}
    for p in k.s['positions']:
        if p.get('size'):
            own[BG.symbol(p['coin'])] = own.get(BG.symbol(p['coin']), 0.0) + p['size']
    tp = (k.s.get('btc_trend') or {}).get('pos')
    if tp and tp.get('size') and tp.get('side', 1) == 1:          # eigener BTC-Trend-Anteil
        own['BTCUSDT'] = own.get('BTCUSDT', 0.0) + tp['size']
    day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime('%Y-%m-%d')
    seen = k.s.setdefault('warn_fremd', {})
    for (sym, hold), q in have.items():
        if hold == 'long' and q > own.get(sym, 0.0) + 1e-9 and seen.get(sym) != day:
            seen[sym] = day
            log.append(f'WARNUNG {sym}: {q} long auf Bitget, eigene {own.get(sym, 0.0)} – fremde Position auf diesem Konto? '
                       f'Eigenes Unterkonto verwenden.')


def entries(k, live, sw, now_ms, log, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    pend = k.s.setdefault('pending', {})
    last_bar = k.s.setdefault('last_bar', {})
    newest = (now_ms // BAR) * BAR - BAR                 # Eröffnungszeit der zuletzt geschlossenen Kerze
    for coin in R.COINS:
        try:
            pf = Puffer(folder, coin)
            start = (pf.last() + BAR) if pf.last() else newest - (R.NEED_CLOSES + 10) * BAR
            if start <= newest:
                pf.append(klines_since(coin, start, now_ms))
        except Exception as e:
            log.append(f'Datenfehler {coin}: {type(e).__name__}'); continue
        try:
            oi_update(folder.parent / 'oi', coin, now_ms)
        except Exception as e:
            log.append(f'OI-Speicher {coin}: {type(e).__name__}')
        lb = last_bar.get(coin)
        first = max(lb + BAR if lb else newest, newest - (MAX_CATCHUP_BARS - 1) * BAR)
        bars = sorted({b for b in range(first, newest + BAR, BAR)} | {int(b) for b in pend.get(coin, [])})
        keep = []
        for t in bars:
            if not pruefbar(k, coin, t):
                continue
            w = pf.window(t)
            if w is None:
                continue
            ok, z, d = R.price_ok(w)
            if not ok:
                continue
            try:
                ois = oi_window(coin, t)
                g_now, g_lag = ratio_at(coin, t), ratio_at(coin, t - R.R_LAG_BARS * BAR)
            except Exception as e:
                log.append(f'OI/Long-Short-Fehler {coin}: {type(e).__name__}'); ois = g_now = None
            if ois is None or g_now is None or g_lag is None:
                if now_ms - t < PENDING_MAX_MS:
                    keep.append(t)                         # Werte noch nicht veröffentlicht → nächster Takt
                continue
            buy, feat = R.signal(w, ois, g_now, g_lag)
            log.append(f"Flush {coin} {datetime.fromtimestamp(t / 1000, timezone.utc):%d.%m. %H:%M}: z {feat['z']:+.1f}, "
                       f"90T {100 * feat['dd']:+.0f} %, OI {100 * (feat['oi_drop'] or 0):+.1f} %, Kleinanleger "
                       f"{'fallend' if feat['retail_falling'] else 'nicht fallend'} → {'KAUF' if buy else 'kein Kauf'}")
            if not buy:
                continue
            kauf(k, live, sw, coin, t, feat, w[-1], now_ms, log,
                 lambda: ll3_live(folder.parent / 'oi', coin, t, w[-1]))
        pend[coin] = keep
        last_bar[coin] = newest


MODI = {'papier': 'papier', 'echtgeld': 'echtgeld'}     # Unterordner von data/


def melde_umschaltung(base: Path, papier: bool, hook, log):
    """Modus merken; bei einem Wechsel einmal melden (wie jaegerbot master_runner)."""
    pfad = base / 'modus.json'
    neu = 'papier' if papier else 'echtgeld'
    try:
        alt = json.loads(pfad.read_text(encoding='utf-8')).get('modus')
    except (OSError, ValueError):
        alt = None
    if alt == neu:
        return
    pfad.write_text(json.dumps({'modus': neu}), encoding='utf-8')
    if alt is None:
        return
    text = ('kapbot umgeschaltet auf ECHTGELD: ab jetzt echte Orders auf Bitget.' if not papier else
            'kapbot umgeschaltet auf PAPIER: keine neuen echten Orders mehr. Offene Echtgeld-Positionen werden '
            'trotzdem nach 24 h regulär geschlossen.')
    log.append(text)
    if hook:
        try: hook(text)
        except Exception: pass


def konto(base: Path, modus: str, start: float):
    d = base / MODI[modus]; d.mkdir(parents=True, exist_ok=True)
    k = A.Konto(d / 'konto.json')
    if not (d / 'konto.json').exists() and modus == 'papier':
        k.s.update(start=start, cash=start, units=start)
    return k


def run(base: Path, telegram=False, now_ms=None, live=None, schalter=None, hook=None):
    """Ein Takt. base = data/ des kapbot. Papier/Echtgeld nach settings.json echtgeld.aktiv (je Lauf neu gelesen):
    neue Trades nur im eingestellten Modus, fällige Ausstiege in BEIDEN Konten (Echtgeld nur mit Zugang)."""
    sw = schalter or ST.load()
    papier = not sw['kap_echtgeld']
    now_ms = now_ms or int(time.time() * 1000)
    base.mkdir(parents=True, exist_ok=True)
    if telegram and hook is None:
        from .telegram import TelegramHook
        hook = TelegramHook(ST.ROOT / 'secret.json')
    log = []
    melde_umschaltung(base, papier, hook, log)
    if not papier and live is None:
        msg = "kapbot: echtgeld.aktiv = true, aber secret.json hat keinen Bitget-Zugang 'kapbot' – keine Trades."
        log.append(msg)
        if hook and not (base / 'ohne_zugang.flag').exists():
            try:
                hook(msg); (base / 'ohne_zugang.flag').write_text('1', encoding='utf-8')
            except Exception:
                pass
    else:
        (base / 'ohne_zugang.flag').unlink(missing_ok=True)
    for modus in ('papier', 'echtgeld'):
        aktiv = (modus == 'papier') == papier
        lv = live if modus == 'echtgeld' else None
        d = base / MODI[modus]
        if not aktiv and not (d / 'konto.json').exists():
            continue
        k = konto(base, modus, sw.get('kap_papier_start', 100.0))
        if (not aktiv and not k.s['positions'] and not (k.s.get('btc_trend') or {}).get('pos') and not k.s.get('pending_live')
                and not (k.s.get('btc_trend') or {}).get('intent')):
            continue
        if modus == 'echtgeld' and lv is None:
            log.append(f"{len(k.s['positions'])} offene Echtgeld-Position(en), aber kein Zugang – Ausstieg nicht möglich")
            continue
        sub = []
        if lv is not None:
            try:
                A.live_sync(k, lv, now_ms, sub); k.save()
            except SystemExit:
                raise
            except Exception as e:
                sub.append(f'Kontoabgleich: {type(e).__name__} – Ausstiege laufen trotzdem')
                if 'live_since' not in k.s:
                    sub.append('Echtgeld-Konto nicht initialisiert – keine Ausstiege/Einstiege möglich'); log += sub; continue
            resolve_pending(k, lv, sub, now_ms); k.save()
            fremde_positionen(k, lv, now_ms, sub)
        exits(k, lv, now_ms, sub)
        k.save()
        tf = sw['kap_trend_faktor'] if (aktiv and sw['kapbot'] and sw['kap_trend']) else 0.0
        trend(k, lv, tf, sw['kap_trend_hebel'], now_ms, sub)
        k.save()
        if aktiv and sw['kapbot']:
            entries(k, lv, sw, now_ms, sub, base / 'kurse')
            k.save()
        elif aktiv:
            sub.append('ausgeschaltet: keine neuen Trades, offene laufen regulär aus')
        prefix = '📝 PAPIER · ' if modus == 'papier' else ''
        log += [prefix + x for x in sub]
        if hook:
            for line in sub:
                if line.startswith(('EINSTIEG', 'AUSSTIEG', 'BTC-Trend', 'WARNUNG')):
                    try: hook(prefix + 'kapbot: ' + line)
                    except Exception: pass
            day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime('%Y-%m-%d')
            if aktiv and k.s.get('reported_day') != day and datetime.fromtimestamp(now_ms / 1000, timezone.utc).hour >= 19:
                try: hook(prefix + bericht_text(k)); k.s['reported_day'] = day; k.save()
                except Exception: pass
    return log


def bericht(k):
    cl = k.s['closed']
    rs = [c['r'] for c in cl]
    return {'konto_usdt': round(k.equity(), 2), 'start_usdt': k.s['start'], 'kasse': round(k.s['cash'], 2),
            'offen': [{'coin': p['coin'], 'einsatz': round(p['stake'], 2), 'einstieg': p['entry_px'],
                       'ausstieg_utc': datetime.fromtimestamp(p['exit_ms'] / 1000, timezone.utc).strftime('%d.%m. %H:%M')}
                      for p in k.s['positions']],
            'btc_trend': (lambda tp: None if not tp else {'margin': round(tp['margin'], 2), 'nominal': round(tp['notional'], 2),
                                                          'einstand': tp['entry_px'], 'hebel': tp.get('lev')})((k.s.get('btc_trend') or {}).get('pos')),
            'trades': len(cl), 'treffer_%': round(100 * sum(r > 0 for r in rs) / len(rs), 1) if rs else None,
            'mittel_%': round(100 * sum(rs) / len(rs), 2) if rs else None,
            'gewinn_usdt': round(sum(max(-c['stake'], c['stake'] * c.get('lev', 1) * c['r']) for c in cl), 2)}


def bericht_text(k):
    b = bericht(k)
    z = [f"kapbot (Kapitulation kaufen) – Konto {b['konto_usdt']:.2f} USDT (Start {b['start_usdt']:.2f})",
         f"Trades {b['trades']}" + (f", Treffer {b['treffer_%']:.0f} %, Ø {b['mittel_%']:+.2f} %, Gewinn {b['gewinn_usdt']:+.2f} USDT" if b['trades'] else '')]
    for p in b['offen']:
        z.append(f"offen {p['coin']}: Einsatz {p['einsatz']:.2f} USDT, Ausstieg {p['ausstieg_utc']} UTC")
    if b.get('btc_trend'):
        t = b['btc_trend']; z.append(f"BTC-Trend offen: Margin {t['margin']:.2f} USDT, Nominal {t['nominal']:.2f} USDT ({t['hebel']}x)")
    return '\n'.join(z)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='kapbot')
    ap.add_argument('befehl', choices=['run', 'bericht'])
    ap.add_argument('--data', type=Path, default=Path('data'), help='Datenordner (Unterordner papier, echtgeld, kurse, oi)')
    ap.add_argument('--telegram', action='store_true')
    ap.add_argument('--secret', type=Path, default=Path('secret.json'))
    ap.add_argument('--modus', choices=['papier', 'echtgeld'], help='bericht: Konto wählen (Standard: eingestellter Modus)')
    a = ap.parse_args(argv)
    sw = ST.load()
    if a.befehl == 'bericht':
        modus = a.modus or ('echtgeld' if sw['kap_echtgeld'] else 'papier')
        print(json.dumps(bericht(A.Konto(a.data / MODI[modus] / 'konto.json')), indent=1, ensure_ascii=False)); return
    sec = a.secret if a.secret.is_absolute() or a.secret.exists() else ST.ROOT / a.secret
    live = None
    try:
        cred = (json.loads(sec.read_text(encoding='utf-8-sig')).get('kapbot') or [None])[0]
    except (OSError, ValueError):
        cred = None
    eg = a.data / MODI['echtgeld'] / 'konto.json'
    egk = A.Konto(eg) if eg.exists() else None
    eg_offen = bool(egk and (egk.s['positions'] or egk.s.get('pending_live') or (egk.s.get('btc_trend') or {}).get('pos')
                             or (egk.s.get('btc_trend') or {}).get('intent')))
    if cred and cred.get('apiKey') and cred.get('secret') and cred.get('password') and (sw['kap_echtgeld'] or eg_offen):
        from . import bitget as BG
        live = BG.Bitget(cred, enabled=True); live.ensure_hedge_mode()
    for line in run(a.data, telegram=a.telegram, live=live, schalter=sw):
        print(line)


if __name__ == '__main__':
    main()

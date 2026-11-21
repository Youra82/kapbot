"""Nachspiel: kapbot mit SEINEM EIGENEN Code über die Geschichte laufen lassen (Desktop, braucht pandas + Forschungsdaten).

Aufgerufen werden genau die Bot-Funktionen bot.pruefbar, bot.kauf, bot.exits, bot.trend (→ btc_trend.step / free_margin)
mit dem Konto markt.Konto, in der Reihenfolge eines echten Laufs (Ausstiege → Trend → Einstiege). Ersetzt werden nur die
Datenquellen: Kurse/Funding/Open Interest aus den Binance-Daten der Forschung statt live von Bitget/Binance.
- Signale: regel.signal auf jeder Kandidaten-Kerze (wie tools/parity_signale.py), Bearbeitung zur Laufzeit t + 6 min.
- Einsatz: regel.ll3 mit 90 Tagen Stundendaten (wie live).
- Papier-Einstieg/-Ausstieg zum Eröffnungskurs der 5m-Kerze der Ausführungszeit, Funding = echte Binance-Sätze (live: Bitget).
- BTC-Anteil: btc_trend.step einmal je Stunde (Entscheidung einmal je UTC-Tag), Funding im Papier pauschal wie im Bot.
Marktbewertung des Kontos stündlich. Ergebnis: data/nachspiel.json (Kurve, Kennzahlen, Trades).

Aufruf:  python tools/nachspiel.py [--forschung D:/botprojekte/forschung/2026-11-20-btc-abverkaeufe]
"""
from __future__ import annotations
import argparse, heapq, json, math, sys, tempfile, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from kapbot import regel as R, bot as K, settings as ST, markt as A, btc_trend as BT

BAR, HOUR, DAY = R.BAR_MS, 3_600_000, 86_400_000


def load(fdir: Path, coin: str):
    sym = coin + 'USDT'
    if coin == 'BTC':
        b = pd.read_parquet(fdir / 'data/btc5m.parquet')
        x = b[['fopen', 'fhigh', 'flow', 'fclose']].astype(float).dropna(); x.columns = ['open', 'high', 'low', 'close']
        x['oi'] = b.oi.astype(float).where(lambda v: v > 1000); x['ga'] = b.glob_acc.astype(float)
    else:
        x = pd.read_parquet(fdir / f'data/fut5_{sym}.parquet')[['open', 'high', 'low', 'close']]
        m = pd.read_parquet(fdir / f'data/metrics_{sym}.parquet'); m.index = m.index.floor('5min'); m = m[~m.index.duplicated()]
        x = x.join(m[['oi', 'glob_acc']], how='left').rename(columns={'glob_acc': 'ga'}); x['oi'] = x.oi.where(x.oi > 0)
    fr = pd.read_parquet(fdir / f'data/funding_{sym}.parquet').fr.sort_index()
    return x, fr[~fr.index.duplicated()]


def signale(x: pd.DataFrame, t0: pd.Timestamp):
    """Alle Kerzen, auf denen regel.signal kauft (ohne Belegung – die prüft der Bot selbst)."""
    c = x.close.values.astype(float); n = len(c); oi = x.oi.values.astype(float); ga = x.ga.values.astype(float)
    lc = np.log(c); r15 = np.full(n, np.nan); r15[3:] = lc[3:] - lc[:-3]
    z = r15 / pd.Series(r15).rolling(R.SIG_N, min_periods=R.SIG_MIN).std().values
    dd = c / pd.Series(c).rolling(R.DD_N, min_periods=R.DD_MIN).max().values - 1
    out = []
    for i in np.where((z <= -3.9) & (dd <= -0.19))[0]:
        if x.index[i] < t0 or i + R.HOLD_BARS >= n:
            continue
        ois = [None if math.isnan(v) else float(v) for v in oi[max(0, i - R.OI_N + 1):i + 1]]
        g_now = None if math.isnan(ga[i]) else float(ga[i])
        g_lag = None if i < R.R_LAG_BARS or math.isnan(ga[i - R.R_LAG_BARS]) else float(ga[i - R.R_LAG_BARS])
        buy, feat = R.signal(c[max(0, i - R.DD_N + 1):i + 1].tolist(), ois, g_now, g_lag)
        if buy:
            out.append((int(x.index[i].value // 1_000_000), feat, float(c[i])))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--forschung', type=Path, default=Path('D:/botprojekte/forschung/2026-11-20-btc-abverkaeufe'))
    ap.add_argument('--start', default='2020-09-01')
    a = ap.parse_args(argv)
    sw = ST.load(Path(__file__).resolve().parents[1] / 'settings.json')
    assert not sw['kap_echtgeld'], 'Nachspiel nur im Papiermodus'
    t0 = pd.Timestamp(a.start)
    D, F, H, ev = {}, {}, {}, []
    tic = time.time()
    for coin in R.COINS:
        x, fr = load(a.forschung, coin)
        D[coin] = (x.index.values.astype('datetime64[ms]').astype(np.int64), x.open.values, x.low.values, x.close.values)
        F[coin] = (fr.index.values.astype('datetime64[ms]').astype(np.int64), np.cumsum(fr.values))
        H[coin] = x.resample('1h').agg({'high': 'max', 'low': 'min', 'close': 'last', 'oi': 'last'}).dropna(subset=['close'])
        for t, feat, e in signale(x, t0):
            ev.append((t + BAR + 60_000, R.COINS.index(coin), coin, t, feat, e))
        print(f'{coin:6s} Signale bis jetzt {len(ev)}  ({time.time() - tic:.0f} s)', flush=True)
    ev.sort()

    def bar_idx(coin, t_ms):
        ts = D[coin][0]; i = int(np.searchsorted(ts, t_ms, side='right')) - 1
        return max(0, i)
    def px_open(coin, t_ms):                        # Eröffnungskurs der 5m-Kerze, in der t_ms liegt
        return float(D[coin][1][bar_idx(coin, t_ms)])
    def px_last(coin, now):                         # Schluss der zuletzt geschlossenen 5m-Kerze
        return float(D[coin][3][bar_idx(coin, now - BAR)])
    def funding(symbol, von, bis, side=1):
        coin = symbol[:-4]; ts, cs = F[coin]
        i, j = np.searchsorted(ts, von, side='right'), np.searchsorted(ts, bis, side='right')
        s = (cs[j - 1] if j > 0 else 0.0) - (cs[i - 1] if i > 0 else 0.0)
        return -side * float(s)
    btc_day = H['BTC'].close.resample('1D').last().dropna()
    btc_day_list = [(int(t.value // 1_000_000), float(v)) for t, v in btc_day.items()]
    state = {'now': 0}
    def btc_low(since):
        ts, _, lo, _ = D['BTC']; i, j = np.searchsorted(ts, since), np.searchsorted(ts, state['now'] - BAR, side='right')
        return float(lo[i:j].min()) if j > i else None
    def l3_fn(coin, t, e):
        h0 = pd.Timestamp(t // HOUR * HOUR, unit='ms'); hh = H[coin].loc[h0 - pd.Timedelta(hours=R.LL3_VORLAUF_H):h0 - pd.Timedelta(hours=1)]
        st = [(c, hi, lo, None if not np.isfinite(o) else float(o)) for c, hi, lo, o in zip(hh.close, hh.high, hh.low, hh.oi)]
        ts, _, lo, _ = D[coin]; i, j = np.searchsorted(ts, h0.value // 1_000_000), np.searchsorted(ts, t, side='right')
        return R.ll3(st, e, float(lo[i:j].min()))

    K._paper_px = px_open
    K._btc_price = lambda: px_last('BTC', state['now'])
    K._btc_low = btc_low
    K._btc_daily = lambda: [(t, c) for t, c in btc_day_list if t + DAY <= state['now']]
    A.bitget_funding_anteil = funding

    tmp = Path(tempfile.mkdtemp()); k = K.konto(tmp, 'papier', sw['kap_papier_start'])
    end = int(pd.Timestamp('2026-11-19 23:00').value // 1_000_000)
    hourly = list(range(int(t0.value // 1_000_000) + 60_000, end, HOUR))
    curve, log_all = [], []
    ei = 0
    def mark(now):
        v = k.s['cash']
        for p in k.s['positions']:
            v += p['stake'] * max(0.0, 1 + p['lev'] * (px_last(p['coin'], now) * (1 - A.SPREAD) * (1 - A.FEE) / p['entry_eff'] - 1))
        pos = (k.s.get('btc_trend') or {}).get('pos')
        if pos:
            v += pos['margin'] + BT.pnl(pos, px_last('BTC', now), now)
        return v
    def takt(now, kaeufe):
        state['now'] = now; log = []
        K.exits(k, None, now, log)
        tf = sw['kap_trend_faktor'] if (sw['kapbot'] and sw['kap_trend']) else 0.0
        K.trend(k, None, tf, sw['kap_trend_hebel'], now, log)
        for _, _, coin, t, feat, e in kaeufe:
            if K.pruefbar(k, coin, t):
                K.kauf(k, None, sw, coin, t, feat, e, now, log, lambda c=coin, tt=t, ee=e: l3_fn(c, tt, ee))
        log_all.extend((now, x_) for x_ in log)
    for h in hourly:
        # alle Einstiegs-/Ausstiegszeitpunkte vor dieser Stunde in Reihenfolge abarbeiten
        while True:
            nxt_e = ev[ei][0] if ei < len(ev) else None
            nxt_x = min((p['exit_ms'] + 60_000 for p in k.s['positions']), default=None)
            cand = [x_ for x_ in (nxt_e, nxt_x) if x_ is not None and x_ < h]
            if not cand:
                break
            now = min(cand); batch = []
            while ei < len(ev) and ev[ei][0] == now:
                batch.append(ev[ei]); ei += 1
            takt(now, batch)
        takt(h, [])
        curve.append((h, mark(h)))
    t_ = np.array([c[0] for c in curve]); e_ = np.array([c[1] for c in curve])
    yrs = (t_[-1] - t_[0]) / (365 * DAY)
    dd = e_ / np.maximum.accumulate(e_) - 1
    def window(a_, b_):
        i, j = np.searchsorted(t_, a_), np.searchsorted(t_, b_) - 1
        e = e_[i:j + 1]; y = (t_[j] - t_[i]) / (365 * DAY)
        return dict(pro_jahr=round(((e[-1] / e[0]) ** (1 / y) - 1) * 100, 1), maxdd=round(float((e / np.maximum.accumulate(e) - 1).min()) * 100, 1))
    ms = lambda s_: int(pd.Timestamp(s_).value // 1_000_000)
    cl = k.s['closed']
    res = dict(ende=round(float(e_[-1]), 1), pro_jahr=round(((e_[-1] / e_[0]) ** (1 / yrs) - 1) * 100, 1), maxdd=round(float(dd.min()) * 100, 1),
               haelfte1=window(t_[0], ms('2023-10-01')), haelfte2=window(ms('2023-10-01'), t_[-1] + 1), pruefung_2024_26=window(ms('2024-01-01'), t_[-1] + 1),
               kapbot_trades=sum(1 for c in cl if c.get('art') == 'kapbot'), btc_trend_buchungen=sum(1 for c in cl if c.get('art') == 'btc_trend'),
               btc_liquidationen=sum(1 for c in cl if c.get('grund') == 'LIQUIDIERT'),
               einsatz_10=sum(1 for c in cl if c.get('einsatz_anteil') == sw['kap_einsatz']), einsatz_5=sum(1 for c in cl if c.get('einsatz_anteil') == sw['kap_einsatz_viel']),
               signale=len(ev), uebersprungen_max=sum(1 for _, x_ in log_all if 'Positionen offen' in x_),
               btc_ruht=None)
    out = dict(kennzahlen=res, kurve=[[int(t), round(float(v), 2)] for t, v in curve[::24]], trades=cl)
    Path(__file__).resolve().parents[1].joinpath('data').mkdir(exist_ok=True)
    json.dump(out, open(Path(__file__).resolve().parents[1] / 'data' / 'nachspiel.json', 'w'), default=float)
    np.save(Path(__file__).resolve().parents[1] / 'data' / 'nachspiel_kurve.npy', np.array(curve))
    print(json.dumps(res, indent=1))


if __name__ == '__main__':
    main()

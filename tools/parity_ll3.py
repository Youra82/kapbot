"""Parität kapbot-Positionsgröße: kapbot.regel.ll3 (reines Python, 90 Tage Vorlauf, wie live) gegen die Forschungswerte
(forschung/2026-11-20-btc-abverkaeufe/data/base_trades_ll3.pkl, volle Geschichte). Gemessen: gleiche Einstufung (≤ Schwelle) und Abweichung.
Aufruf (Desktop, braucht pandas):  python tools/parity_ll3.py [--forschung D:/botprojekte/forschung/2026-11-20-btc-abverkaeufe]"""
from __future__ import annotations
import argparse, math, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from kapbot import regel as R


def raw(fdir: Path, sym: str) -> pd.DataFrame:
    if sym == 'BTCUSDT':
        b = pd.read_parquet(fdir / 'data/btc5m.parquet'); x = b[['fhigh', 'flow', 'fclose']].astype(float).dropna(); x.columns = ['high', 'low', 'close']
        x['oi'] = b.oi.astype(float).where(lambda v: v > 1000); return x
    x = pd.read_parquet(fdir / f'data/fut5_{sym}.parquet')[['high', 'low', 'close']]
    m = pd.read_parquet(fdir / f'data/metrics_{sym}.parquet'); m.index = m.index.floor('5min'); m = m[~m.index.duplicated()]
    x = x.join(m[['oi']], how='left'); x['oi'] = x.oi.where(x.oi > 0); return x


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--forschung', type=Path, default=Path('D:/botprojekte/forschung/2026-11-20-btc-abverkaeufe'))
    a = ap.parse_args(argv)
    ref = pd.read_pickle(a.forschung / 'data/base_trades_ll3.pkl').dropna(subset=['ll3'])
    gleich = n = 0; abw = []
    for sym, g in ref.groupby('sym'):
        x = raw(a.forschung, sym)
        h = x.resample('1h').agg({'high': 'max', 'low': 'min', 'close': 'last', 'oi': 'last'}).dropna(subset=['close'])
        for r in g.itertuples():
            ts = r.sig; h0 = ts.floor('1h')
            w = h.loc[h0 - pd.Timedelta(hours=R.LL3_VORLAUF_H):h0 - pd.Timedelta(hours=1)]
            st = [(c, hi, lo, None if not np.isfinite(o) else float(o)) for c, hi, lo, o in zip(w.close, w.high, w.low, w.oi)]
            seg = x.loc[h0:ts]
            v = R.ll3(st, float(x.close.asof(ts)), float(seg.low.min()))
            n += 1
            if v is not None:
                gleich += (v <= R.LL3_SCHWELLE) == (r.ll3 <= R.LL3_SCHWELLE); abw.append(abs(v - r.ll3))
        print(f'{sym:10s} {len(g):4d} Trades', flush=True)
    print(f'GESAMT: gleiche Einstufung {gleich}/{n} = {100 * gleich / n:.1f} %, mittlere Abweichung {np.mean(abw):.6f} (Schwelle {R.LL3_SCHWELLE:.6f})')
    return 0 if gleich / n >= 0.97 else 1


if __name__ == '__main__':
    raise SystemExit(main())

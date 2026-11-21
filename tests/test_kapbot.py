"""kapbot (Kapitulation kaufen): Regel gegen die pandas-Definition der Forschung und Dienstablauf offline (keine Netz- oder Order-Aufrufe)."""
import math, random, tempfile, unittest
from pathlib import Path
from unittest import mock

from kapbot import regel as R, bot as K


def walk(n, seed, vol=0.002, drift=0.0):
    rng = random.Random(seed); c = [100.0]
    for _ in range(n - 1):
        c.append(c[-1] * math.exp(drift + rng.gauss(0, vol)))
    return c


class RuleTests(unittest.TestCase):
    def test_z_matches_pandas(self):
        import numpy as np, pandas as pd
        c = walk(9000, 1)
        lc = np.log(np.array(c)); r = np.full(len(c), np.nan); r[3:] = lc[3:] - lc[:-3]
        ref = (r / pd.Series(r).rolling(R.SIG_N, min_periods=R.SIG_MIN).std().values)[-1]
        self.assertAlmostEqual(R.z_flush(c), ref, places=9)

    def test_z_needs_min_window(self):
        self.assertIsNone(R.z_flush(walk(R.SIG_MIN + 2, 2)))
        self.assertIsNotNone(R.z_flush(walk(R.SIG_MIN + 3, 2)))

    def test_dd_and_oi(self):
        c = [100.0] * R.DD_MIN + [79.0]
        self.assertAlmostEqual(R.dd90(c), -0.21)
        self.assertIsNone(R.dd90(c[-100:]))
        ois = [100.0] * 71 + [96.0]
        self.assertAlmostEqual(R.oi_drop(ois), -0.04)
        self.assertIsNone(R.oi_drop([100.0] * 71 + [None]))
        self.assertIsNone(R.oi_drop([None] * 40 + [100.0] * 32))     # nur 32 Werte < 36

    def test_retail(self):
        self.assertTrue(R.retail_falling(2.0, 2.5))
        self.assertFalse(R.retail_falling(2.5, 2.0))
        self.assertFalse(R.retail_falling(None, 2.0))

    def _crash_series(self):
        c = [200.0] * 9000 + walk(18000, 3, vol=0.002, drift=-0.00004)      # langer Abwärtstrend
        c[-1] = c[-4] * math.exp(-0.06)                                      # 15-min-Flush −6 %
        return c

    def test_signal_all_parts_needed(self):
        c = self._crash_series()
        ok, f = R.signal(c, [100.0] * 71 + [95.0], 2.0, 2.4)
        self.assertTrue(ok, f)
        self.assertFalse(R.signal(c, [100.0] * 71 + [98.0], 2.0, 2.4)[0])   # OI nur −2 %
        self.assertFalse(R.signal(c, [100.0] * 71 + [95.0], 2.4, 2.0)[0])   # Kleinanleger steigen
        flat = c[:-1] + [c[-4] * math.exp(-0.001)]
        self.assertFalse(R.signal(flat, [100.0] * 71 + [95.0], 2.0, 2.4)[0])  # kein Flush


class LiqKarteTests(unittest.TestCase):
    def _stunden(self):
        st = [(100.0, 100.5, 99.5, 1000.0)] * 60                           # ruhig, OI konstant
        st += [(100.0, 100.5, 99.5, 1100.0)]                                # +100 Coins OI bei 100
        return st

    def test_bands_below_price_count(self):
        v = R.ll3(self._stunden(), 96.0, 95.9)                               # 20x-Band bei 95,5 liegt in [93,12; 96)
        self.assertGreater(v, 0)

    def test_band_crossed_by_low_is_gone(self):
        st = self._stunden() + [(97.0, 97.5, 95.0, 1100.0)]                   # Stundentief 95 kreuzt das 20x-Band (95,5)
        v = R.ll3(st, 96.0, 95.9)
        self.assertLess(v, R.ll3(self._stunden(), 96.0, 95.9))

    def test_oi_reduction_shrinks_bands(self):
        st = self._stunden() + [(100.0, 100.5, 99.5, 1050.0)]
        self.assertAlmostEqual(R.ll3(st, 96.0, 95.9) / R.ll3(self._stunden(), 96.0, 95.9), (1050 / 1100) * (1100 / 1050), places=6)

    def test_einsatz(self):
        self.assertEqual(R.einsatz(0.001), 0.10); self.assertEqual(R.einsatz(0.02), 0.05); self.assertEqual(R.einsatz(None), 0.05)

    def test_needs_history(self):
        self.assertIsNone(R.ll3(self._stunden()[:10], 96.0, 95.9))


class FakeLive:
    """Simulierte Börse (keine Netz- oder Order-Aufrufe), wie evobot tests/test_arten_live.py."""
    C = {'symbol': 'SOLUSDT', 'sizeMultiplier': '0.1', 'volumePlace': '1', 'minTradeNum': '0.1', 'minTradeUSDT': '5',
         'symbolStatus': 'normal'}

    def __init__(self, bal=200.0, price=50.0):
        self.bal, self.price, self.pos, self.orders = bal, price, {}, {}

    def account(self): return {'available': str(self.bal), 'posMode': 'hedge_mode'}
    def transfers(self, since, until): return []
    def contract(self, sym): return dict(self.C, symbol=sym)
    def last_price(self, sym): return self.price
    def prepare(self, sym, lev): pass
    def positions(self): return dict(self.pos)
    def order(self, sym, oid): return self.orders.get(oid)

    def market(self, sym, hold, size, oid, close=False):
        if oid in self.orders:
            return self.orders[oid]
        self.pos[(sym, hold)] = self.pos.get((sym, hold), 0.0) + (-size if close else size)
        o = {'state': 'filled', 'priceAvg': str(self.price), 'baseVolume': str(size), 'fee': str(-size * self.price * 0.0006)}
        self.orders[oid] = o
        return o


class RunTests(unittest.TestCase):
    """Signal, Einstieg, Ausstieg 24 h später; Papier/Echtgeld-Schalter – mit ersetzten Datenquellen."""
    def setUp(self):
        self.l3 = 0.001                                   # wenig Liquidationen darunter → voller Einsatz
        c = RuleTests()._crash_series()
        self.bar = R.BAR_MS; self.t_sig = 1_800_000_000_000 // self.bar * self.bar
        rows = [(self.t_sig - (len(c) - 1 - i) * self.bar, v) for i, v in enumerate(c)]
        t_sig = self.t_sig
        self.patches = [mock.patch.object(R, 'COINS', ['SOL']),
                        mock.patch.object(K, 'klines_since', lambda coin, s, now: [r for r in rows if r[0] >= s]),
                        mock.patch.object(K, 'oi_window', lambda coin, t: [100.0] * 71 + [95.0]),
                        mock.patch.object(K, 'ratio_at', lambda coin, t: 2.0 if t == t_sig else 2.4),
                        mock.patch.object(K, '_paper_px', lambda coin, t: 50.0),
                        mock.patch.object(K, 'oi_update', lambda *a, **k: None),
                        mock.patch.object(K, 'll3_live', lambda *a, **k: self.l3),
                        mock.patch.object(K.A, 'bitget_funding_anteil', lambda *a, **k: 0.0)]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    @staticmethod
    def sw(echtgeld=False, aktiv=True, start=100.0, trend=False, tf=1.0, th=5):
        return {'kapbot': aktiv, 'kap_einsatz': 0.10, 'kap_einsatz_viel': 0.05, 'kap_hebel': 1, 'kap_max_offen': 10,
                'kap_echtgeld': echtgeld, 'kap_papier_start': start, 'kap_trend': trend, 'kap_trend_faktor': tf, 'kap_trend_hebel': th}

    def test_paper_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); msgs = []
            now = self.t_sig + self.bar + 60_000
            log = K.run(base, now_ms=now, schalter=self.sw(start=200.0), hook=msgs.append)
            k = K.A.Konto(base / 'papier' / 'konto.json')
            self.assertEqual(len(k.s['positions']), 1, log)
            self.assertEqual(k.s['start'], 200.0)
            p = k.s['positions'][0]
            self.assertAlmostEqual(p['stake'], 20.0)                              # 10 % von 200
            self.assertEqual(p['exit_ms'], self.t_sig + (R.HOLD_BARS + 1) * self.bar)
            self.assertTrue(any(m.startswith('📝 PAPIER · kapbot: EINSTIEG SOL') for m in msgs), msgs)
            K.run(base, now_ms=now + self.bar, schalter=self.sw(start=200.0))     # kein zweiter Einstieg (Coin belegt)
            self.assertEqual(len(K.A.Konto(base / 'papier' / 'konto.json').s['positions']), 1)
            K.run(base, now_ms=p['exit_ms'] + 120_000, schalter=self.sw(start=200.0))
            k = K.A.Konto(base / 'papier' / 'konto.json')
            self.assertEqual(len(k.s['positions']), 0)
            r = k.s['closed'][0]['r']
            self.assertAlmostEqual(r, (1 - 0.0005) * (1 - 0.0006) / ((1 + 0.0005) * (1 + 0.0006)) - 1, places=9)
            self.assertAlmostEqual(k.s['cash'], 180.0 + 20.0 * (1 + r), places=9)

    def test_many_liquidations_below_half_stake(self):
        self.l3 = 0.02
        with tempfile.TemporaryDirectory() as d:
            K.run(Path(d), now_ms=self.t_sig + self.bar + 60_000, schalter=self.sw())
            p = K.A.Konto(Path(d) / 'papier' / 'konto.json').s['positions'][0]
            self.assertAlmostEqual(p['stake'], 5.0)
            self.assertEqual(p['einsatz_anteil'], 0.05)

    def test_switched_off_no_entries(self):
        with tempfile.TemporaryDirectory() as d:
            log = K.run(Path(d), now_ms=self.t_sig + self.bar + 60_000, schalter=self.sw(aktiv=False))
            self.assertTrue(any('ausgeschaltet' in x for x in log), log)
            self.assertEqual(K.A.Konto(Path(d) / 'papier' / 'konto.json').s['positions'], [])

    def test_live_without_access_does_nothing_and_reports_once(self):
        with tempfile.TemporaryDirectory() as d:
            msgs = []
            K.run(Path(d), now_ms=self.t_sig + self.bar + 60_000, schalter=self.sw(echtgeld=True), hook=msgs.append)
            K.run(Path(d), now_ms=self.t_sig + 2 * self.bar + 60_000, schalter=self.sw(echtgeld=True), hook=msgs.append)
            self.assertEqual(sum('keinen Bitget-Zugang' in m for m in msgs), 1, msgs)
            self.assertFalse((Path(d) / 'echtgeld' / 'konto.json').exists())

    def test_live_entry_then_switch_to_paper_still_exits_live(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); lv = FakeLive(); msgs = []
            K.run(base, now_ms=1_000, schalter=self.sw(echtgeld=False), hook=msgs.append)          # Modus merken: Papier
            K.run(base, now_ms=self.t_sig + self.bar + 60_000, live=lv, schalter=self.sw(echtgeld=True), hook=msgs.append)
            self.assertTrue(any('umgeschaltet auf ECHTGELD' in m for m in msgs), msgs)
            k = K.A.Konto(base / 'echtgeld' / 'konto.json')
            self.assertEqual(len(k.s['positions']), 1)
            self.assertEqual(k.s['start'], 200.0)                                  # Echtgeld-Start = Bitget-Guthaben
            self.assertAlmostEqual(lv.pos[('SOLUSDT', 'long')], 0.4)               # 20 USDT / 50
            exit_ms = k.s['positions'][0]['exit_ms']
            K.run(base, now_ms=exit_ms + 120_000, live=lv, schalter=self.sw(echtgeld=False), hook=msgs.append)
            self.assertTrue(any('umgeschaltet auf PAPIER' in m for m in msgs), msgs)
            k = K.A.Konto(base / 'echtgeld' / 'konto.json')
            self.assertEqual(k.s['positions'], [])                                 # trotz Papiermodus regulär geschlossen
            self.assertAlmostEqual(lv.pos[('SOLUSDT', 'long')], 0.0)

    def test_open_position_blocks_new_entry(self):
        """Ausstieg fehlgeschlagen (kein Kurs) → Coin-Sperre abgelaufen, aber kein zweiter Trade auf dem offenen Coin."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); now = self.t_sig + self.bar + 60_000
            K.run(base, now_ms=now, schalter=self.sw(start=200.0))
            k = K.A.Konto(base / 'papier' / 'konto.json'); k.s['busy'] = {}; k.save()
            self.assertFalse(K.pruefbar(k, 'SOL', self.t_sig + 10 * self.bar))

    def test_no_access_still_closes_paper(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); msgs = []
            K.run(base, now_ms=self.t_sig + self.bar + 60_000, schalter=self.sw(start=200.0))
            p = K.A.Konto(base / 'papier' / 'konto.json').s['positions'][0]
            K.run(base, now_ms=p['exit_ms'] + 120_000, schalter=self.sw(echtgeld=True, start=200.0), hook=msgs.append)
            self.assertEqual(K.A.Konto(base / 'papier' / 'konto.json').s['positions'], [])
            self.assertTrue(any('keinen Bitget-Zugang' in m for m in msgs))

    def test_flag_only_after_successful_message(self):
        with tempfile.TemporaryDirectory() as d:
            def broken(_): raise OSError('Telegram weg')
            K.run(Path(d), now_ms=self.t_sig + self.bar + 60_000, schalter=self.sw(echtgeld=True), hook=broken)
            self.assertFalse((Path(d) / 'ohne_zugang.flag').exists())

    def test_missing_lag_ratio_is_retried(self):
        t_sig = self.t_sig
        with tempfile.TemporaryDirectory() as d, mock.patch.object(K, 'ratio_at', lambda coin, t: 2.0 if t == t_sig else None):
            K.run(Path(d), now_ms=self.t_sig + self.bar + 60_000, schalter=self.sw(start=200.0))
            k = K.A.Konto(Path(d) / 'papier' / 'konto.json')
            self.assertEqual(k.s['positions'], []); self.assertEqual(k.s['pending']['SOL'], [self.t_sig])

    def test_foreign_position_warns_once(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); lv = FakeLive(); msgs = []
            K.run(base, now_ms=1_000, schalter=self.sw(echtgeld=False))
            K.run(base, now_ms=self.t_sig + self.bar + 60_000, live=lv, schalter=self.sw(echtgeld=True), hook=msgs.append)
            lv.pos[('ETHUSDT', 'long')] = 1.0                                     # Fremdposition (anderer Bot)
            for i in (2, 3):
                K.run(base, now_ms=self.t_sig + i * self.bar + 60_000, live=lv, schalter=self.sw(echtgeld=True), hook=msgs.append)
            self.assertEqual(sum('WARNUNG ETHUSDT' in m for m in msgs), 1, msgs)

    def _live(self, lv, base):
        K.run(base, now_ms=1_000, schalter=self.sw(echtgeld=False))
        K.run(base, now_ms=self.t_sig + self.bar + 60_000, live=lv, schalter=self.sw(echtgeld=True))
        return K.A.Konto(base / 'echtgeld' / 'konto.json')

    def test_crash_right_after_send_keeps_vormerkung(self):
        """Absturz nach dem Senden: Vormerkung + Reservierung liegen schon auf Platte, nächster Lauf bucht die Order."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); lv = FakeLive(); real = lv.market
            def crash(sym, hold, size, oid, close=False):
                if close: return real(sym, hold, size, oid, close=close)
                real(sym, hold, size, oid); raise KeyboardInterrupt
            lv.market = crash
            K.run(base, now_ms=1_000, schalter=self.sw(echtgeld=False))
            with self.assertRaises(KeyboardInterrupt):
                K.run(base, now_ms=self.t_sig + self.bar + 60_000, live=lv, schalter=self.sw(echtgeld=True))
            k = K.A.Konto(base / 'echtgeld' / 'konto.json')
            self.assertEqual(len(k.s['pending_live']), 1); self.assertLess(k.s['cash'], 200.0)
            lv.market = real
            K.run(base, now_ms=self.t_sig + 2 * self.bar + 60_000, live=lv, schalter=self.sw(echtgeld=True))
            k = K.A.Konto(base / 'echtgeld' / 'konto.json')
            self.assertEqual(len(k.s['positions']), 1); self.assertEqual(k.s['pending_live'], [])
            self.assertAlmostEqual(k.equity(), 200.0, places=6)

    def test_never_arrived_order_released_after_checks(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); lv = FakeLive(); real = lv.market
            def lost(sym, hold, size, oid, close=False):
                if close: return real(sym, hold, size, oid, close=close)
                raise OSError('Netz weg vor Bitget')
            lv.market = lost
            k = self._live(lv, base)
            self.assertEqual(len(k.s['pending_live']), 1)
            for i in range(2, 6):                                                  # 4 weitere Läufe, > 15 min
                K.run(base, now_ms=self.t_sig + self.bar + 60_000 + i * 5 * 60_000, live=lv, schalter=self.sw(echtgeld=True))
            k = K.A.Konto(base / 'echtgeld' / 'konto.json')
            self.assertEqual(k.s['pending_live'], []); self.assertAlmostEqual(k.s['cash'], 200.0)

    def test_partial_close_then_rest_liquidated_keeps_partial_profit(self):
        from kapbot import bitget as BG
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); lv = FakeLive(); k = self._live(lv, base)
            p = k.s['positions'][0]; q0 = p['size']; oid = BG.client_oid(p['genom'], 'SOL', p['entry_ms'], 'close')
            lv.orders[oid] = {'state': 'canceled', 'priceAvg': '60', 'baseVolume': str(q0 / 2), 'fee': '0'}
            lv.pos[('SOLUSDT', 'long')] = 0.0                                     # Rest liquidiert
            ex = p['exit_ms']
            for dt in (120_000, 120_000 + 5 * 60_000, 120_000 + 20 * 60_000):
                K.run(base, now_ms=ex + dt, live=lv, schalter=self.sw(echtgeld=True))
            c = K.A.Konto(base / 'echtgeld' / 'konto.json').s['closed'][0]
            self.assertTrue(c['liquidated'])
            want = ((60 - 50) * q0 / 2 - p['fee_in'] * 0.5 - p['stake'] * 0.5) / (p['stake'] * p['lev'])
            self.assertAlmostEqual(c['r'], want, places=9)
            self.assertGreater(c['r'], -1.0)

    def test_same_order_id_not_booked_twice(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); lv = FakeLive(); k = self._live(lv, base)
            ex = k.s['positions'][0]['exit_ms']
            K.run(base, now_ms=ex + 120_000, live=lv, schalter=self.sw(echtgeld=True))
            k = K.A.Konto(base / 'echtgeld' / 'konto.json'); k.s['busy'] = {}; k.s['last_bar'] = {}; k.save()
            log = K.run(base, now_ms=self.t_sig + self.bar + 60_000, live=lv, schalter=self.sw(echtgeld=True))
            k = K.A.Konto(base / 'echtgeld' / 'konto.json')
            self.assertEqual(k.s['positions'], []); self.assertTrue(any('schon gebucht' in x for x in log), log)

    def test_no_live_entry_without_initialisation(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); lv = FakeLive()
            def broken(): raise OSError('Bitget nicht erreichbar')
            lv.account = broken
            K.run(base, now_ms=1_000, schalter=self.sw(echtgeld=False))
            log = K.run(base, now_ms=self.t_sig + self.bar + 60_000, live=lv, schalter=self.sw(echtgeld=True))
            self.assertEqual(lv.orders, {}, log)

class TrendTests(unittest.TestCase):
    """Gelbe Linie: BTC-Trend-Anteil im kapbot-Konto (kapbot.btc_trend) mit Vorrang für kapbot – offline."""
    DAY = 86_400_000

    def setUp(self):
        self.l3 = 0.001
        c = RuleTests()._crash_series()
        self.bar = R.BAR_MS; self.t_sig = 1_800_000_000_000 // self.DAY * self.DAY + 10 * self.bar   # kurz nach 00:00 UTC
        rows = [(self.t_sig - (len(c) - 1 - i) * self.bar, v) for i, v in enumerate(c)]
        t_sig = self.t_sig; day0 = t_sig // self.DAY * self.DAY
        self.bull = [(day0 - (260 - i) * self.DAY, 100.0 + i) for i in range(260)]     # steigend → Regime an
        self.daily = self.bull
        self.patches = [mock.patch.object(K, 'klines_since', lambda coin, s, now: [r for r in rows if r[0] >= s]),
                        mock.patch.object(K, 'oi_window', lambda coin, t: [100.0] * 71 + [95.0]),
                        mock.patch.object(K, 'ratio_at', lambda coin, t: 2.0 if t == t_sig else 2.4),
                        mock.patch.object(K, '_paper_px', lambda coin, t: 50.0),
                        mock.patch.object(K, 'oi_update', lambda *a, **k: None),
                        mock.patch.object(K, 'll3_live', lambda *a, **k: self.l3),
                        mock.patch.object(K, '_btc_price', lambda: 30_000.0),
                        mock.patch.object(K, '_btc_low', lambda t: None),
                        mock.patch.object(K, '_btc_daily', lambda: self.daily),
                        mock.patch.object(K.A, 'bitget_funding_anteil', lambda *a, **k: 0.0)]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_trend_opens_with_5x_margin(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(R, 'COINS', ['SOL']):
            K.run(Path(d), now_ms=self.t_sig + self.bar + 60_000, schalter=RunTests.sw(trend=True))
            k = K.A.Konto(Path(d) / 'papier' / 'konto.json')
            pos = k.s['btc_trend']['pos']
            self.assertAlmostEqual(pos['margin'], 20.0)                      # 1,0 × 100 / 5
            self.assertAlmostEqual(pos['notional'], 100.0)
            self.assertEqual(len(k.s['positions']), 1)                       # kapbot-Trade zusätzlich (Kasse reicht)
            self.assertAlmostEqual(k.s['positions'][0]['stake'], 10.0)

    def test_kapbot_has_priority_over_trend_margin(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(R, 'COINS', ['SOL']):
            K.run(Path(d), now_ms=self.t_sig + self.bar + 60_000, schalter=RunTests.sw(trend=True, th=1))   # Margin = ganzes Konto
            k = K.A.Konto(Path(d) / 'papier' / 'konto.json')
            self.assertEqual(len(k.s['positions']), 1)
            self.assertAlmostEqual(k.s['positions'][0]['stake'], 10.0, places=1)
            self.assertLess(k.s['btc_trend']['pos']['margin'], 91.0)          # Margin abgegeben

    def test_btc_signal_skipped_while_trend_open(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(R, 'COINS', ['BTC']):
            K.run(Path(d), now_ms=self.t_sig + self.bar + 60_000, schalter=RunTests.sw(trend=True))
            k = K.A.Konto(Path(d) / 'papier' / 'konto.json')
            self.assertIsNotNone(k.s['btc_trend']['pos'])
            self.assertEqual(k.s['positions'], [])

    def test_trend_off_closes_position(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(R, 'COINS', ['SOL']):
            K.run(Path(d), now_ms=self.t_sig + self.bar + 60_000, schalter=RunTests.sw(trend=True))
            K.run(Path(d), now_ms=self.t_sig + self.DAY + 60_000, schalter=RunTests.sw(trend=False))   # nächster Tag, aus
            k = K.A.Konto(Path(d) / 'papier' / 'konto.json')
            self.assertIsNone(k.s['btc_trend']['pos'])
            self.assertTrue(any(c.get('art') == 'btc_trend' for c in k.s['closed']))

class TrendLiveTests(unittest.TestCase):
    """BTC-Trend Echtgeld-Absicherung (Codex-Review 21.11.2026) mit simulierter Börse – keine Netz- oder Order-Aufrufe."""
    DAY = 86_400_000

    class Live(FakeLive):
        C = {'symbol': 'BTCUSDT', 'sizeMultiplier': '0.001', 'volumePlace': '3', 'minTradeNum': '0.001', 'minTradeUSDT': '5',
             'symbolStatus': 'normal'}

        def __init__(self, price=30_000.0):
            super().__init__(bal=1000.0, price=price); self.sent = 0; self.hang = False

        def market(self, sym, hold, size, oid, close=False):
            if oid in self.orders:
                return self.orders[oid]
            self.sent += 1
            if self.hang:                                   # Antwort geht verloren, Order liegt aber schon auf der Börse
                FakeLive.market(self, sym, hold, size, oid, close=close); return None
            return FakeLive.market(self, sym, hold, size, oid, close=close)

    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.addCleanup(self.d.cleanup)
        self.k = K.A.Konto(Path(self.d.name) / 'konto.json')
        self.k.s.update(start=1000.0, cash=1000.0, units=1000.0, live_since=1)
        self.now = 1_800_000_000_000 // self.DAY * self.DAY + 3_600_000
        self.bull = [(self.now - (260 - i) * self.DAY, 100.0 + i) for i in range(260)]

    def step(self, lv, now=None, faktor=1.0, daily='bull', log=None):
        log = [] if log is None else log
        K.BT.step(self.k, self.bull if daily == 'bull' else daily, now or self.now, log, faktor, lv, price_fn=lambda: lv.price, lev=5)
        return log

    def test_open_unclear_then_resolved_no_second_order(self):
        lv = self.Live(); lv.hang = True
        self.step(lv)
        st = self.k.s['btc_trend']
        self.assertIsNone(st['pos']); self.assertIsNotNone(st['intent']); self.assertEqual(lv.sent, 1)
        lv.hang = False
        self.step(lv, now=self.now + 300_000)
        st = self.k.s['btc_trend']
        self.assertIsNone(st['intent']); self.assertAlmostEqual(st['pos']['size'], 0.033)
        self.assertEqual(lv.sent, 1)                         # keine zweite Order
        self.assertAlmostEqual(self.k.s['cash'], 1000.0 - 30_000 * 0.033 / 5)

    def test_close_filled_but_unanswered_is_not_liquidation(self):
        lv = self.Live(); self.step(lv)
        lv.hang = True; lv.price = 33_000.0
        self.step(lv, now=self.now + 300_000, faktor=0.0)    # ausschalten → Close, Antwort verloren
        self.assertIsNotNone(self.k.s['btc_trend']['intent'])
        lv.hang = False
        self.step(lv, now=self.now + 600_000, faktor=0.0)
        cl = [c for c in self.k.s['closed'] if c['art'] == 'btc_trend']
        self.assertEqual(len(cl), 1); self.assertNotEqual(cl[0]['grund'], 'LIQUIDIERT')
        self.assertGreater(cl[0]['r'], 0.09)
        self.assertIsNone(self.k.s['btc_trend']['pos'])

    def test_partial_release_never_exceeds_own_size(self):
        lv = self.Live(); self.step(lv)
        own = self.k.s['btc_trend']['pos']['size']
        lv.price = 27_000.0                                   # Kurs unter Einstand
        K.BT.free_margin(self.k, 190.0, self.now + 300_000, [], lv, price_fn=lambda: lv.price)
        self.assertLessEqual(own - lv.pos.get(('BTCUSDT', 'long'), 0.0), own + 1e-12)
        self.assertGreaterEqual(lv.pos.get(('BTCUSDT', 'long'), 0.0), -1e-12)
        self.assertGreater(self.k.s['cash'], 800.0)

    def test_btc_capitulation_blocks_trend(self):
        lv = self.Live(); self.k.s['positions'] = [{'coin': 'BTC', 'stake': 50.0}]
        log = self.step(lv)
        self.assertIsNone(self.k.s['btc_trend']['pos']); self.assertEqual(lv.sent, 0)
        self.assertTrue(any('Kapitulations-Position' in x for x in log), log)

    def test_trend_open_blocks_btc_capitulation(self):
        self.k.s['btc_trend'] = {'pos': None, 'tag': None, 'intent': {'oid': 'x'}}
        self.k.s['busy'] = {}
        self.assertFalse(K.pruefbar(self.k, 'BTC', self.now))

    def test_switch_off_closes_immediately_same_day(self):
        lv = self.Live(); self.step(lv)
        self.step(lv, now=self.now + 300_000, faktor=0.0)
        self.assertIsNone(self.k.s['btc_trend']['pos'])

    def test_missing_daily_no_decision_but_liquidation_runs(self):
        lv = self.Live(); self.step(lv)
        lv.pos[('BTCUSDT', 'long')] = 0.0                    # Börse hat liquidiert
        self.step(lv, now=self.now + self.DAY, daily=None)
        self.assertEqual(self.k.s['closed'][-1]['grund'], 'LIQUIDIERT')

    def test_short_daily_history_no_close(self):
        lv = self.Live(); self.step(lv)
        log = self.step(lv, now=self.now + self.DAY, daily=self.bull[:50])
        self.assertIsNotNone(self.k.s['btc_trend']['pos'])
        self.assertTrue(any('keine Regime-Entscheidung' in x for x in log), log)

    def test_order_ids_unique_per_intent(self):
        lv = self.Live(); self.step(lv)
        K.BT.free_margin(self.k, 20.0, self.now + 300_000, [], lv, price_fn=lambda: lv.price)
        K.BT.free_margin(self.k, 20.0, self.now + 300_000, [], lv, price_fn=lambda: lv.price)
        self.assertEqual(lv.sent, 3)                         # Einstieg + zwei eigene Abgaben, keine wiederverwendete ID


class TrendLiveTests2(TrendLiveTests):
    """Weitere Absicherungen (Codex-Gegenprüfung 21.11.2026)."""

    def test_lost_intent_released_only_after_three_checks_and_time(self):
        lv = self.Live(); real = lv.market
        def lost(*a, **kw): raise OSError('weg')
        lv.market = lost
        self.step(lv)
        self.assertIsNotNone(self.k.s['btc_trend']['intent'])
        lv.market = real
        self.step(lv, now=self.now + 5 * 60_000); self.step(lv, now=self.now + 10 * 60_000)
        self.assertIsNotNone(self.k.s['btc_trend']['intent'])               # 2 leere Abfragen, < 15 min
        self.step(lv, now=self.now + 16 * 60_000)
        self.assertIsNone(self.k.s['btc_trend']['intent'])

    def test_lost_intent_kept_when_exchange_has_unexplained_btc(self):
        lv = self.Live()
        def lost(*a, **kw): raise OSError('weg')
        lv.market = lost
        self.step(lv)
        lv.pos[('BTCUSDT', 'long')] = 0.033                                  # Order kam doch an, Detailabfrage findet sie nicht
        for m in (5, 10, 16, 21):
            self.step(lv, now=self.now + m * 60_000)
        self.assertIsNotNone(self.k.s['btc_trend']['intent'])

    def test_switch_off_works_when_liquidation_check_fails(self):
        lv = self.Live(); self.step(lv)
        def broken(): raise OSError('positions weg')
        lv.positions = broken
        log = self.step(lv, now=self.now + 300_000, faktor=0.0)
        self.assertIsNone(self.k.s['btc_trend']['pos'], log)


if __name__ == '__main__':
    unittest.main()

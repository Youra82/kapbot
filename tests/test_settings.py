import json, tempfile, unittest
from pathlib import Path
from kapbot import settings as ST


class SettingsTest(unittest.TestCase):
    def test_defaults_when_missing_or_broken(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ST.load(Path(d) / 'fehlt.json'), ST.DEFAULTS)
            p = Path(d) / 's.json'; p.write_text('{kaputt', encoding='utf-8')
            self.assertEqual(ST.load(p), ST.DEFAULTS)
            self.assertFalse(ST.DEFAULTS['kap_echtgeld'])                  # ohne settings.json nie Echtgeld
            self.assertFalse(ST.DEFAULTS['kapbot'])

    def test_repo_settings(self):
        s = ST.load(ST.ROOT / 'settings.json')
        self.assertEqual(s, dict(kapbot=True, kap_einsatz=0.10, kap_einsatz_viel=0.05, kap_hebel=1, kap_max_offen=20,
                                 kap_echtgeld=False, kap_papier_start=100.0, kap_trend=True, kap_trend_faktor=1.0, kap_trend_hebel=5))

    def test_invalid_values_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 's.json'
            p.write_text(json.dumps({'kapbot': {'aktiv': True, 'einsatz': 3, 'hebel': 50, 'max_offen': 0},
                                     'echtgeld': {'aktiv': 'ja'}, 'btc_trend': {'aktiv': True, 'faktor': 9}}), encoding='utf-8')
            s = ST.load(p)
            self.assertEqual((s['kap_einsatz'], s['kap_hebel'], s['kap_max_offen'], s['kap_echtgeld'], s['kap_trend_faktor']),
                             (0.10, 1, 20, False, 1.0))


if __name__ == '__main__':
    unittest.main()

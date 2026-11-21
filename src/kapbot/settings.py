"""kapbot-Schalter aus settings.json (Projektwurzel). Fehlt die Datei oder ein Eintrag, gelten sichere Standardwerte (Papier)."""
from __future__ import annotations
import json, os
from pathlib import Path


def _root() -> Path:
    """Projektordner mit settings.json/data/secret.json: KAPBOT_ROOT (Dienstdatei) → Arbeitsverzeichnis → Quellbaum."""
    env = os.environ.get('KAPBOT_ROOT')
    if env and (Path(env) / 'settings.json').exists():
        return Path(env)
    if (Path.cwd() / 'settings.json').exists():
        return Path.cwd()
    return Path(__file__).resolve().parents[2]


ROOT = _root()
DEFAULTS = dict(kapbot=False, kap_einsatz=0.10, kap_einsatz_viel=0.05, kap_hebel=1, kap_max_offen=20,
                kap_echtgeld=False, kap_papier_start=100.0, kap_trend=False, kap_trend_faktor=1.0, kap_trend_hebel=5)


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def load(path: Path | None = None) -> dict:
    out = dict(DEFAULTS)
    try:
        data = json.loads((path or ROOT / 'settings.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return out
    kp = data.get('kapbot') if isinstance(data.get('kapbot'), dict) else {}
    out['kapbot'] = kp.get('aktiv') is True
    ke, kv, kh, km = kp.get('einsatz'), kp.get('einsatz_viel_liq'), kp.get('hebel'), kp.get('max_offen')
    if _num(ke) and 0 < ke <= 0.5: out['kap_einsatz'] = float(ke)
    if _num(kv) and 0 < kv <= 0.5: out['kap_einsatz_viel'] = float(kv)
    if _num(kh) and int(kh) == kh and 1 <= kh <= 5: out['kap_hebel'] = int(kh)
    if _num(km) and int(km) == km and 1 <= km <= 30: out['kap_max_offen'] = int(km)
    eg = data.get('echtgeld') if isinstance(data.get('echtgeld'), dict) else {}      # wie jaegerbot
    out['kap_echtgeld'] = eg.get('aktiv') is True
    ps = eg.get('papier_startkapital')
    if _num(ps) and 5 <= ps <= 1e7: out['kap_papier_start'] = float(ps)
    bt = data.get('btc_trend') if isinstance(data.get('btc_trend'), dict) else {}
    out['kap_trend'] = bt.get('aktiv') is True
    tf, th = bt.get('faktor'), bt.get('hebel')
    if _num(tf) and 0 < tf <= 2: out['kap_trend_faktor'] = float(tf)
    if _num(th) and int(th) == th and 1 <= th <= 10: out['kap_trend_hebel'] = int(th)
    return out

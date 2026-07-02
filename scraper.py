#!/usr/bin/env python3
"""
Czech Extraliga stats scraper
=============================
Haalt individuele spelersstatistieken (batting/pitching/fielding) op via de
JSON-API van stats.baseball.cz en schrijft ze weg als:

    data/stats.json   → { batting: {data, headers}, pitching: {...}, fielding: {...} }
    data/splits.json  → {} (placeholder, PHP laadt dit bestand wel)
    data/meta.json    → { last_updated, event, round, counts }

Dit formaat sluit 1-op-1 aan op de [extraliga_stats] WordPress-shortcode.

Let op: de URL wordt opgebouwd via een params-dict, dus GEEN dubbele
team=/round=/split= parameters (de bug die eerder in de request-URL's zat).
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------

EVENT      = "extraliga-2026"
ROUND      = "6792"          # ronde-id uit de request-URL's
LANGUAGE   = "en"
BASE_URL   = f"https://stats.baseball.cz/api/v1/stats/events/{EVENT}/index"
OUTPUT_DIR = Path(__file__).parent / "data"

SECTIES = ["batting", "pitching", "fielding"]

# Kwalificatiedrempels voor de "Top 10" sortering (overzicht-tab in de PHP).
# Gekwalificeerde spelers komen bovenaan, de rest daaronder.
MIN_AB = 40    # batting: minimaal aantal at-bats
MIN_IP = 15.0  # pitching: minimaal aantal innings pitched
MIN_C  = 20    # fielding: minimaal aantal chances

HEADERS_HTTP = {
    "User-Agent": "Mozilla/5.0 (compatible; ExtraligaStatsBot/1.0; +https://worldbaseballnews.org)",
    "Accept": "application/json",
    "Referer": f"https://stats.baseball.cz/en/events/{EVENT}/stats",
}

TIMEOUT = 30

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NAAM_RE = re.compile(
    r'<span class="lastname">(?P<last>.*?)</span>\s*(?:<br\s*/?>)?\s*'
    r'<span class="firstname">(?P<first>.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)


def schoon_naam(raw: str) -> str:
    """
    Zet '<span class="lastname">ALVAREZ</span><br><span class="firstname">Roberto</span>'
    om naar 'Roberto Alvarez'. Valt terug op het strippen van alle HTML.
    """
    if not raw:
        return ""
    m = _NAAM_RE.search(raw)
    if m:
        last = m.group("last").strip()
        first = m.group("first").strip()
        # Achternaam komt in CAPS binnen → nette titlecase (werkt ook met č/ř/á etc.)
        last = " ".join(w.capitalize() if w.isupper() else w for w in last.split())
        first = " ".join(w.capitalize() if w.islower() or w.isupper() else w for w in first.split())
        return f"{first} {last}".strip()
    # Fallback: strip alle tags
    txt = re.sub(r"<br\s*/?>", " ", raw)
    txt = re.sub(r"<[^>]+>", "", txt)
    return " ".join(txt.split())


def naar_float(val):
    """Zet '3.00', '15.2' (IP), 982 etc. veilig om naar float; None bij mislukking."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def haal_sectie(sessie: requests.Session, sectie: str) -> dict:
    """
    Haalt één stats-sectie op. Params via dict → gegarandeerd geen duplicaten.
    Retourneert {"data": [...], "headers": [...]}.
    """
    params = {
        "section": "players",
        "stats-section": sectie,
        "team": "",
        "round": ROUND,
        "language": LANGUAGE,
    }
    # 'split' hoort alleen bij batting/pitching (fielding-URL heeft die param niet)
    if sectie in ("batting", "pitching"):
        params["split"] = ""

    resp = sessie.get(BASE_URL, params=params, headers=HEADERS_HTTP, timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    data = payload.get("data") or []
    headers = payload.get("headers") or []

    # Namen opschonen zodat de PHP ze direct kan tonen
    for rij in data:
        if "name" in rij:
            rij["name"] = schoon_naam(str(rij["name"]))

    return {"data": data, "headers": headers}


# ---------------------------------------------------------------------------
# Sortering voor de "Top 10" op de overzicht-tab
# ---------------------------------------------------------------------------

def sorteer_batting(rijen: list) -> list:
    def key(r):
        ab = naar_float(r.get("ab")) or 0
        avg = naar_float(r.get("avg")) or 0
        gekwalificeerd = ab >= MIN_AB
        return (not gekwalificeerd, -avg, -ab)
    return sorted(rijen, key=key)


def sorteer_pitching(rijen: list) -> list:
    def key(r):
        ip = naar_float(r.get("ip")) or 0
        era = naar_float(r.get("era"))
        era = era if era is not None else 999.0
        gekwalificeerd = ip >= MIN_IP
        return (not gekwalificeerd, era, -ip)
    return sorted(rijen, key=key)


def sorteer_fielding(rijen: list) -> list:
    def key(r):
        c = naar_float(r.get("field_c")) or 0
        fldp = naar_float(r.get("fldp")) or 0
        gekwalificeerd = c >= MIN_C
        return (not gekwalificeerd, -fldp, -c)
    return sorted(rijen, key=key)


SORTEERDERS = {
    "batting": sorteer_batting,
    "pitching": sorteer_pitching,
    "fielding": sorteer_fielding,
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sessie = requests.Session()

    stats = {}
    fouten = []

    for sectie in SECTIES:
        try:
            print(f"→ Ophalen: {sectie} …", flush=True)
            resultaat = haal_sectie(sessie, sectie)
            resultaat["data"] = SORTEERDERS[sectie](resultaat["data"])
            stats[sectie] = resultaat
            print(f"  ✓ {len(resultaat['data'])} spelers, {len(resultaat['headers'])} kolommen")
        except Exception as e:  # noqa: BLE001
            fouten.append(f"{sectie}: {e}")
            print(f"  ✗ Fout bij {sectie}: {e}", file=sys.stderr)

    if len(fouten) == len(SECTIES):
        print("Alle secties mislukt — bestaande data blijft ongewijzigd.", file=sys.stderr)
        return 1

    # Bij een deels mislukte run: vul aan vanuit bestaande stats.json zodat
    # de widget nooit een lege sectie krijgt.
    stats_pad = OUTPUT_DIR / "stats.json"
    if fouten and stats_pad.exists():
        try:
            oud = json.loads(stats_pad.read_text(encoding="utf-8"))
            for sectie in SECTIES:
                if sectie not in stats and sectie in oud:
                    stats[sectie] = oud[sectie]
                    print(f"  ↺ {sectie}: oude data hergebruikt")
        except Exception:
            pass

    nu = datetime.now(timezone.utc).isoformat()

    meta = {
        "last_updated": nu,
        "event": EVENT,
        "round": ROUND,
        "source": f"https://stats.baseball.cz/en/events/{EVENT}/stats",
        "counts": {s: len(stats.get(s, {}).get("data", [])) for s in SECTIES},
        "errors": fouten,
    }

    # splits.json is (nog) een placeholder; de PHP laadt het bestand wel,
    # dus het moet bestaan en geldige JSON bevatten.
    splits = {}

    (OUTPUT_DIR / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (OUTPUT_DIR / "splits.json").write_text(
        json.dumps(splits, ensure_ascii=False), encoding="utf-8"
    )
    (OUTPUT_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"✓ Klaar. Bestanden weggeschreven naar {OUTPUT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

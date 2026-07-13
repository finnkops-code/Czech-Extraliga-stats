#!/usr/bin/env python3
"""
Czech Extraliga stats scraper
=============================
Haalt individuele spelersstatistieken (batting/pitching/fielding) op via de
JSON-API van stats.baseball.cz en schrijft ze weg als:
    data/stats.json   → { batting: {data, headers}, pitching: {...}, fielding: {...} }
    data/splits.json  → {} (placeholder, PHP laadt dit bestand wel)
    data/meta.json    → { last_checked, last_updated, data_changed_this_run, event, round, counts }
Strategie (i.v.m. 403 bot-detectie op GitHub Actions runners):
  1. Eerst een snelle poging met `requests` + volledige browser-headers.
  2. Bij 403: Playwright-fallback. De stats-pagina wordt in headless Chromium
     geladen (lost eventuele Cloudflare/JS-challenge op) en de API-calls
     worden daarna VANUIT de browsercontext gedaan via window.fetch — dus met
     echte browser-TLS-fingerprint, cookies en headers.
De URL wordt opgebouwd via een params-dict → geen dubbele parameters.

Belangrijk: "last_checked" wordt ELKE run bijgewerkt (zodat je in meta.json
altijd kunt zien wanneer de scraper voor het laatst succesvol heeft gedraaid),
terwijl "last_updated" alleen verandert als de batting/pitching/fielding-data
inhoudelijk anders is dan de vorige run. Zo kun je op de site onderscheid
maken tussen "gecontroleerd, geen nieuwe data" en "daadwerkelijk bijgewerkt".
"""
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
import requests
# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------
EVENT      = "extraliga-2026"
ROUND      = "6792"          # ronde-id uit de request-URL's (seizoensgebonden!)
LANGUAGE   = "en"
BASE_URL   = f"https://stats.baseball.cz/api/v1/stats/events/{EVENT}/index"
STATS_PAGE = f"https://stats.baseball.cz/en/events/{EVENT}/stats"
OUTPUT_DIR = Path(__file__).parent / "data"
SECTIES = ["batting", "pitching", "fielding"]
# Kwalificatiedrempels voor de "Top 10" sortering (overzicht-tab in de PHP).
MIN_AB = 40    # batting: minimaal aantal at-bats
MIN_IP = 15.0  # pitching: minimaal aantal innings pitched
MIN_C  = 20    # fielding: minimaal aantal chances
# Volledige browser-headers — geen bot-UA, die triggerde de 403
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": STATS_PAGE,
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
TIMEOUT = 30
# ---------------------------------------------------------------------------
# URL-opbouw (schoon, zonder duplicaten)
# ---------------------------------------------------------------------------
def bouw_url(sectie: str) -> str:
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
    return BASE_URL + "?" + urllib.parse.urlencode(params)
# ---------------------------------------------------------------------------
# Naam opschonen
# ---------------------------------------------------------------------------
_NAAM_RE = re.compile(
    r'<span class="lastname">(?P<last>.*?)</span>\s*(?:<br\s*/?>)?\s*'
    r'<span class="firstname">(?P<first>.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
def schoon_naam(raw: str) -> str:
    """
    '<span class="lastname">ALVAREZ</span><br><span class="firstname">Roberto</span>'
    → 'Roberto Alvarez'. Valt terug op het strippen van alle HTML.
    """
    if not raw:
        return ""
    m = _NAAM_RE.search(raw)
    if m:
        last = m.group("last").strip()
        first = m.group("first").strip()
        last = " ".join(w.capitalize() if w.isupper() else w for w in last.split())
        first = " ".join(w.capitalize() if w.islower() or w.isupper() else w for w in first.split())
        return f"{first} {last}".strip()
    txt = re.sub(r"<br\s*/?>", " ", raw)
    txt = re.sub(r"<[^>]+>", "", txt)
    return " ".join(txt.split())
def naar_float(val):
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
def verwerk_payload(payload: dict) -> dict:
    """Normaliseert een API-antwoord naar {"data": [...], "headers": [...]}."""
    data = payload.get("data") or []
    headers = payload.get("headers") or []
    for rij in data:
        if "name" in rij:
            rij["name"] = schoon_naam(str(rij["name"]))
    return {"data": data, "headers": headers}
# ---------------------------------------------------------------------------
# Strategie 1: requests met browser-headers
# ---------------------------------------------------------------------------
def haal_via_requests(sessie: requests.Session, sectie: str) -> dict:
    resp = sessie.get(bouw_url(sectie), headers=BROWSER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return verwerk_payload(resp.json())
# ---------------------------------------------------------------------------
# Strategie 2: Playwright-fallback (fetch vanuit de browsercontext)
# ---------------------------------------------------------------------------
def haal_alles_via_playwright() -> dict:
    """
    Laadt de stats-pagina in headless Chromium en haalt daarna alle secties
    op via window.fetch binnen de pagina. Retourneert {sectie: {data, headers}}.
    """
    from playwright.sync_api import sync_playwright
    resultaat = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=BROWSER_HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        print("  Playwright: stats-pagina laden (JS-challenge)…", flush=True)
        page.goto(STATS_PAGE, wait_until="domcontentloaded", timeout=60_000)
        # Even wachten zodat een eventuele challenge/cookies kan afronden
        page.wait_for_timeout(4_000)
        for sectie in SECTIES:
            url = bouw_url(sectie)
            print(f"  Playwright fetch: {sectie} …", flush=True)
            payload = page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {
                        headers: { 'Accept': 'application/json',
                                   'X-Requested-With': 'XMLHttpRequest' },
                        credentials: 'same-origin'
                    });
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return await r.json();
                }""",
                url,
            )
            resultaat[sectie] = verwerk_payload(payload)
            print(f"  ✓ {sectie}: {len(resultaat[sectie]['data'])} spelers")
        browser.close()
    return resultaat
# ---------------------------------------------------------------------------
# Sortering voor de "Top 10" op de overzicht-tab
# ---------------------------------------------------------------------------
def sorteer_batting(rijen: list) -> list:
    def key(r):
        ab = naar_float(r.get("ab")) or 0
        avg = naar_float(r.get("avg")) or 0
        return (not ab >= MIN_AB, -avg, -ab)
    return sorted(rijen, key=key)
def sorteer_pitching(rijen: list) -> list:
    def key(r):
        ip = naar_float(r.get("ip")) or 0
        era = naar_float(r.get("era"))
        era = era if era is not None else 999.0
        return (not ip >= MIN_IP, era, -ip)
    return sorted(rijen, key=key)
def sorteer_fielding(rijen: list) -> list:
    def key(r):
        c = naar_float(r.get("field_c")) or 0
        fldp = naar_float(r.get("fldp")) or 0
        return (not c >= MIN_C, -fldp, -c)
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
    stats = {}
    fouten = []
    geblokkeerd = False
    # --- Poging 1: requests ------------------------------------------------
    sessie = requests.Session()
    for sectie in SECTIES:
        try:
            print(f"→ Ophalen (requests): {sectie} …", flush=True)
            stats[sectie] = haal_via_requests(sessie, sectie)
            print(f"  ✓ {len(stats[sectie]['data'])} spelers")
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            print(f"  ✗ HTTP {code} bij {sectie}", file=sys.stderr)
            if code in (403, 429, 503):
                geblokkeerd = True
                break  # geen zin de andere secties ook te proberen
            fouten.append(f"{sectie}: {e}")
        except Exception as e:  # noqa: BLE001
            fouten.append(f"{sectie}: {e}")
            print(f"  ✗ Fout bij {sectie}: {e}", file=sys.stderr)
    # --- Poging 2: Playwright-fallback -------------------------------------
    if geblokkeerd or len(stats) < len(SECTIES):
        print("→ Fallback naar Playwright (browsercontext)…", flush=True)
        try:
            stats = haal_alles_via_playwright()
            fouten = []
        except Exception as e:  # noqa: BLE001
            fouten.append(f"playwright: {e}")
            print(f"  ✗ Playwright-fallback mislukt: {e}", file=sys.stderr)
    if not stats:
        print("Alle strategieën mislukt — bestaande data blijft ongewijzigd.", file=sys.stderr)
        return 1
    # Sorteren
    for sectie, resultaat in stats.items():
        resultaat["data"] = SORTEERDERS[sectie](resultaat["data"])
    # Bestaande stats.json inlezen (voor failsafe én voor wijzigingsdetectie)
    stats_pad = OUTPUT_DIR / "stats.json"
    oude_stats = None
    if stats_pad.exists():
        try:
            oude_stats = json.loads(stats_pad.read_text(encoding="utf-8"))
        except Exception:
            oude_stats = None
    # Ontbrekende secties aanvullen vanuit bestaande stats.json (failsafe)
    ontbrekend = [s for s in SECTIES if s not in stats]
    if ontbrekend and oude_stats:
        for sectie in ontbrekend:
            if sectie in oude_stats:
                stats[sectie] = oude_stats[sectie]
                print(f"  ↺ {sectie}: oude data hergebruikt")
    # Bepalen of de spelersdata inhoudelijk is gewijzigd t.o.v. de vorige run
    # (alleen "data" vergelijken; headers/tooltips veranderen praktisch nooit
    # en zouden anders elke run als "wijziging" tellen).
    data_gewijzigd = True
    if oude_stats is not None:
        nieuw_vergelijk = {s: stats.get(s, {}).get("data") for s in SECTIES}
        oud_vergelijk = {s: oude_stats.get(s, {}).get("data") for s in SECTIES}
        data_gewijzigd = nieuw_vergelijk != oud_vergelijk
    # Oude meta.json inlezen zodat "last_updated" behouden blijft als er
    # niets is veranderd.
    meta_pad = OUTPUT_DIR / "meta.json"
    oude_last_updated = None
    if meta_pad.exists():
        try:
            oude_meta = json.loads(meta_pad.read_text(encoding="utf-8"))
            oude_last_updated = oude_meta.get("last_updated")
        except Exception:
            pass
    nu = datetime.now(timezone.utc).isoformat()
    meta = {
        "last_checked": nu,
        "last_updated": nu if (data_gewijzigd or not oude_last_updated) else oude_last_updated,
        "data_changed_this_run": data_gewijzigd,
        "event": EVENT,
        "round": ROUND,
        "source": STATS_PAGE,
        "counts": {s: len(stats.get(s, {}).get("data", [])) for s in SECTIES},
        "errors": fouten,
    }
    # splits.json is (nog) een placeholder; de PHP laadt het bestand wel.
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
    if data_gewijzigd:
        print(f"✓ Klaar — nieuwe data. Bestanden weggeschreven naar {OUTPUT_DIR}/")
    else:
        print(f"✓ Klaar — geen inhoudelijke wijzigingen t.o.v. vorige run. meta.json bijgewerkt in {OUTPUT_DIR}/")
    return 0
if __name__ == "__main__":
    sys.exit(main())

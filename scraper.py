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
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
import requests
# ---------------------------------------------------------------------------
# Proxy (optioneel)
# ---------------------------------------------------------------------------
# stats.baseball.cz blokkeert GitHub Actions-runners op WAF/CDN-niveau (een
# AWS CloudFront 403 "The request could not be satisfied", die al optreedt
# vóórdat er ook maar JavaScript draait) — dat is een IP-reputatie-blokkade,
# geen headless-browser-detectie. Headers, stealth-scripts of retries lossen
# dat niet op. De enige echte oplossing is de requests via een IP routeren
# dat niet als datacenter/cloud-IP bekend staat, bijvoorbeeld via een
# residential-proxy-dienst (ScraperAPI, Bright Data, ZenRows, etc.).
#
# Zet de GitHub Actions secret PROXY_URL (bijv. http://user:pass@proxy-host:poort)
# om dit te activeren — zonder die secret verandert er niets aan het gedrag.
PROXY_URL = os.environ.get("PROXY_URL", "").strip() or None
REQUEST_PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
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
# Verbergt de meest voorkomende automation-fingerprints van headless Chromium
# (navigator.webdriver, ontbrekende plugins/languages, ontbrekend window.chrome).
# Dit alleen is geen garantie tegen geavanceerde bot-detectie, maar haalt wel
# de "makkelijke" signalen weg waar veel WAF's/anti-bot-systemen op filteren.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
    window.navigator.permissions.query = (params) => (
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(params)
    );
}
"""
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
    resp = sessie.get(
        bouw_url(sectie), headers=BROWSER_HEADERS, timeout=TIMEOUT, proxies=REQUEST_PROXIES
    )
    resp.raise_for_status()
    return verwerk_payload(resp.json())
# ---------------------------------------------------------------------------
# Strategie 2: Playwright-fallback (fetch vanuit de browsercontext)
# ---------------------------------------------------------------------------
def _playwright_proxy_config():
    """Zet PROXY_URL (bv. http://user:pass@host:poort) om naar het dict-formaat
    dat Playwright's chromium.launch(proxy=...) verwacht. Geeft None terug als
    er geen PROXY_URL is ingesteld."""
    if not PROXY_URL:
        return None
    parsed = urllib.parse.urlsplit(PROXY_URL)
    server = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    config = {"server": server}
    if parsed.username:
        config["username"] = urllib.parse.unquote(parsed.username)
    if parsed.password:
        config["password"] = urllib.parse.unquote(parsed.password)
    return config
def _fetch_sectie_met_retries(page, sectie: str, pogingen: int = 3):
    """Haalt één sectie op via window.fetch, met een paar herhaalpogingen
    (met oplopende wachttijd) voordat we het opgeven — transiënte 403's van
    WAF's lossen zich soms na een paar seconden vanzelf op."""
    laatste_fout = None
    for poging in range(1, pogingen + 1):
        url = bouw_url(sectie)
        try:
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
            return verwerk_payload(payload)
        except Exception as e:  # noqa: BLE001
            laatste_fout = e
            print(f"    ✗ poging {poging}/{pogingen} voor {sectie} mislukt: {e}", file=sys.stderr)
            if poging < pogingen:
                page.wait_for_timeout(3_000 * poging)
    raise laatste_fout
def haal_alles_via_playwright() -> dict:
    """
    Laadt de stats-pagina in headless Chromium en haalt daarna alle secties
    op via window.fetch binnen de pagina. Retourneert {sectie: {data, headers}}.
    Probeert de hele operatie (paginalading + fetches) tot 2 keer met een
    verse browser-context, met wat stealth-maatregelen tegen headless-detectie.
    """
    from playwright.sync_api import sync_playwright
    max_pogingen = 2
    laatste_fout = None
    for poging in range(1, max_pogingen + 1):
        try:
            with sync_playwright() as p:
                proxy_config = _playwright_proxy_config()
                if proxy_config:
                    print(f"  → gebruik proxy: {proxy_config['server']}", flush=True)
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ],
                    proxy=proxy_config,
                )
                context = browser.new_context(
                    user_agent=BROWSER_HEADERS["User-Agent"],
                    locale="en-US",
                    viewport={"width": 1366, "height": 900},
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                context.add_init_script(STEALTH_INIT_SCRIPT)
                page = context.new_page()
                print(f"  Playwright (poging {poging}/{max_pogingen}): stats-pagina laden…", flush=True)
                resp = page.goto(STATS_PAGE, wait_until="domcontentloaded", timeout=60_000)
                status = resp.status if resp else None
                print(f"  → paginastatus: {status}", flush=True)
                if status and status >= 400:
                    fragment = page.content()[:300].replace("\n", " ")
                    print(f"  ⚠ Pagina gaf status {status}. Fragment: {fragment}", file=sys.stderr)
                # Wachten zodat een eventuele JS-challenge/cookies kan afronden
                page.wait_for_timeout(6_000)
                resultaat = {}
                for sectie in SECTIES:
                    print(f"  Playwright fetch: {sectie} …", flush=True)
                    resultaat[sectie] = _fetch_sectie_met_retries(page, sectie)
                    print(f"  ✓ {sectie}: {len(resultaat[sectie]['data'])} spelers")
                browser.close()
                return resultaat
        except Exception as e:  # noqa: BLE001
            laatste_fout = e
            print(f"  ✗ Playwright-poging {poging}/{max_pogingen} volledig mislukt: {e}", file=sys.stderr)
            if poging < max_pogingen:
                print("  → nieuwe poging over 10s met verse browser-context…", flush=True)
                import time
                time.sleep(10)
    raise laatste_fout
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
    # "last_updated" wordt bewust ELKE run bijgewerkt (ook zonder inhoudelijke
    # wijziging) — dit veld voedt de "Bijgewerkt"-datum op de site, en die
    # moet voor SEO-doeleinden dagelijks verschuiven. "data_changed_this_run"
    # blijft beschikbaar als intern diagnoseveld om te zien of er die dag ook
    # daadwerkelijk nieuwe spelersdata binnenkwam.
    nu = datetime.now(timezone.utc).isoformat()
    meta = {
        "last_checked": nu,
        "last_updated": nu,
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

"""
Czech Extraliga Schedule Scraper
Bron: extraliga.baseball.cz/rozpis-vysledky (statische HTML, geen JS/Playwright nodig)
"""

import json
import re
import urllib.request
from datetime import datetime, timezone

URL       = "https://extraliga.baseball.cz/rozpis-vysledky"
JSON_FILE = "schedule_extraliga.json"

DAG_NL   = ["maandag","dinsdag","woensdag","donderdag","vrijdag","zaterdag","zondag"]
MAAND_NL = {1:"januari",2:"februari",3:"maart",4:"april",5:"mei",6:"juni",
            7:"juli",8:"augustus",9:"september",10:"oktober",11:"november",12:"december"}

TEAM_NAMEN = {
    "DRA": "Draci",
    "KOT": "Kotlářka",
    "SOK": "Hluboká",
    "HLU": "Hluboká",
    "ARR": "Arrows",
    "NUC": "Nuclears",
    "EAG": "Eagles",
    "HRO": "Hroši",
    "SAB": "SaBaT",
}

CODE_CANONICAL = {"SOK": "HLU"}  # Sokol Hluboká → HLU


def fetch_html(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def strip_tags(h):
    return re.sub(r'<[^>]+>', '', h).strip()


def parse_datum(s):
    m = re.search(r'(\d+)\.\s*(\d+)\.\s*(\d{4})', s)
    if not m:
        return None
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"


def format_dag(datum_str):
    d = datetime.strptime(datum_str, "%Y-%m-%d")
    return f"{DAG_NL[d.weekday()]} {d.day} {MAAND_NL[d.month]}"


def extract_code(raw_cell):
    """
    Haal teamcode op uit cel-inhoud.
    Formaat: 'Draci BrnoDraciDRA', 'SaBaT PrahaSaBaTSAB'
    De 3-letter code staat altijd aan het einde.
    """
    # Probeer eerst de laatste 3 hoofdletters
    m = re.search(r'([A-Z]{3})$', raw_cell.strip())
    if m and m.group(1) in TEAM_NAMEN:
        code = m.group(1)
        return CODE_CANONICAL.get(code, code)
    # Fallback: zoek alle exacte 3-letter codes
    for code in re.findall(r'[A-Z]{3}', raw_cell):
        if code in TEAM_NAMEN:
            return CODE_CANONICAL.get(code, code)
    return None


def parse_table(table_html):
    games = []
    current_datum = None
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)

    for row in rows:
        if '<th' in row:
            continue

        cells_raw = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if not cells_raw:
            continue

        cells = [strip_tags(c).strip() for c in cells_raw]
        if len(cells) < 7:
            continue

        # Update datum als cel 0 een datum bevat
        if re.search(r'\d+\.\s*\d+\.\s*\d{4}', cells[0]):
            current_datum = parse_datum(cells[0])

        if not current_datum:
            continue

        # Tijdstip: cel 3
        tijdstip = None
        m = re.match(r'^(\d{1,2}):(\d{2})$', cells[3])
        if m and int(m.group(1)) <= 23:
            tijdstip = f"{int(m.group(1)):02d}:{m.group(2)}"

        # Teamcodes uit raw HTML (vóór tag-stripping), cel 4 = thuis, cel 5 = uit
        thuis_code = extract_code(cells_raw[4])
        uit_code   = extract_code(cells_raw[5])

        if not thuis_code or not uit_code:
            continue

        # Score: cel 6, formaat "13:6" of "-:-"
        score_thuis = score_uit = None
        gespeeld = False
        m = re.match(r'^(\d+):(\d+)$', cells[6])
        if m:
            score_thuis = int(m.group(1))
            score_uit   = int(m.group(2))
            gespeeld    = True

        games.append({
            "datum":       current_datum,
            "tijdstip":    tijdstip,
            "thuis":       TEAM_NAMEN[thuis_code],
            "thuis_code":  thuis_code,
            "uit":         TEAM_NAMEN[uit_code],
            "uit_code":    uit_code,
            "score_thuis": score_thuis,
            "score_uit":   score_uit,
            "gespeeld":    gespeeld,
        })

    return games


def main():
    print(f"Ophalen van {URL}...")
    html = fetch_html(URL)
    print(f"Ontvangen: {len(html)} bytes")

    tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    print(f"Tabellen gevonden: {len(tables)}")

    all_games = []
    for i, table in enumerate(tables):
        games = parse_table(table)
        print(f"  Tabel {i+1}: {len(games)} wedstrijden")
        all_games.extend(games)

    # Dedupliceer op (datum, thuis_code, uit_code)
    seen = set()
    unique = []
    for g in all_games:
        key = (g["datum"], g["thuis_code"], g["uit_code"])
        if key not in seen:
            seen.add(key)
            unique.append(g)

    # Voeg dag, ID en lege velden toe
    for i, g in enumerate(unique):
        g["id"]            = i + 1
        g["dag"]           = format_dag(g["datum"])
        g["thuis_innings"] = []
        g["uit_innings"]   = []
        g["innings"]       = None
        g["gamestatus"]    = "F" if g["gespeeld"] else ""
        g["locatie"]       = None
        g["stadion"]       = None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    uitslagen = [g for g in unique if g["gespeeld"]]
    programma = [g for g in unique if not g["gespeeld"] and g["datum"] >= today]

    uitslagen.sort(key=lambda g: (g["datum"], g["tijdstip"] or ""), reverse=True)
    programma.sort(key=lambda g: (g["datum"], g["tijdstip"] or ""))
    programma = programma[:10]

    print(f"\nUitslagen ({len(uitslagen)}):")
    for u in uitslagen[:10]:
        print(f"  {u['datum']} {u['tijdstip']}  {u['thuis_code']} {u['score_thuis']}-{u['score_uit']} {u['uit_code']}")

    print(f"\nProgramma ({len(programma)}):")
    for p in programma:
        print(f"  {p['datum']} {p['tijdstip']}  {p['thuis_code']} @ {p['uit_code']}")

    output = {
        "bijgewerkt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bron":       URL,
        "uitslagen":  uitslagen,
        "programma":  programma,
    }

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ {JSON_FILE}: {len(uitslagen)} uitslagen, {len(programma)} programma")


if __name__ == "__main__":
    main()

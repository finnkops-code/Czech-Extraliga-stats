"""
Czech Extraliga Stats Scraper — API versie
Roept de JSON API rechtstreeks aan, geen browser nodig.
Resultaten worden opgeslagen in /data als JSON.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
import urllib.request
import urllib.parse

BASE = "https://stats.baseball.cz/api/v1/stats/events/extraliga-2026"
DATA = "data"
os.makedirs(DATA, exist_ok=True)

HEADERS_REQ = {
    "Accept":     "application/json",
    "Referer":    "https://stats.baseball.cz/en/events/extraliga-2026/stats",
    "User-Agent": "Mozilla/5.0 (compatible; ExtraligaBot/2.0)",
}

TEAMS = {
    "Hroši":    "43158",
    "Kotlářka": "43154",
    "Draci":    "43156",
    "Hluboká":  "43155",
    "Nuclears": "43153",
    "Eagles":   "43159",
    "Arrows":   "43160",
    "SaBaT":    "43157",
}

# round= parameter weglaten → haalt volledige seizoensdata op
ROUND = ""

STAT_SECTIONS = ["batting", "pitching", "fielding"]


def clean_name(html: str) -> str:
    """
    Strip HTML-tags uit naamveld.
    API geeft nu: <span class="lastname">ALVAREZ</span><br><span class="firstname">Roberto</span>
    Resultaat: "ALVAREZ Roberto"
    """
    # Haal lastname en firstname op
    last  = re.search(r'class="lastname"[^>]*>(.*?)</span>', html)
    first = re.search(r'class="firstname"[^>]*>(.*?)</span>', html)
    if last and first:
        return f"{last.group(1)} {first.group(1)}"
    # Fallback: strip alle tags
    text = re.sub(r'<[^>]+>', ' ', html)
    return ' '.join(text.split()).strip()


def fetch(url: str, params: dict = None) -> dict | None:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS_REQ)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  ⚠ Poging {attempt+1} mislukt ({url}): {e}")
            time.sleep(2 ** attempt)
    return None


def clean_players(data: list) -> list:
    out = []
    for row in data:
        row = dict(row)
        row["name"] = clean_name(row.get("name", ""))
        link = row.get("link", "")
        m = re.search(r"/players/(\d+)$", link)
        row["player_id"] = m.group(1) if m else None
        out.append(row)
    return out


def annotate_headers(headers: list) -> list:
    for h in headers:
        if h.get("format"):
            h["format_type"] = "baseball_pct"
    return headers


def scrape_section(section: str, team: str = "", round_: str = ROUND) -> dict | None:
    params = {
        "section":       "players",
        "stats-section": section,
        "team":          team,
        "round":         round_,
        "split":         "",
        "language":      "en",
    }
    result = fetch(f"{BASE}/index", params)
    if not result:
        return None
    return {
        "data":    clean_players(result.get("data", [])),
        "headers": annotate_headers(result.get("headers", [])),
    }


def scrape_all_stats():
    print("📊 Scraping stats (alle secties)…")
    all_stats = {}
    for section in STAT_SECTIONS:
        print(f"  ↳ {section}…")
        result = scrape_section(section)
        if result:
            all_stats[section] = result
            print(f"     {len(result['data'])} spelers")
        time.sleep(0.5)

    with open(f"{DATA}/stats.json", "w", encoding="utf-8") as f:
        json.dump(all_stats, f, ensure_ascii=False, indent=2)
    print(f"  ✅ stats.json")
    return all_stats


def scrape_per_team():
    print("👕 Scraping per team…")
    os.makedirs(f"{DATA}/teams", exist_ok=True)
    team_index = []

    for name, team_id in TEAMS.items():
        safe = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        team_data = {"name": name, "id": team_id, "sections": {}}

        for section in STAT_SECTIONS:
            result = scrape_section(section, team=team_id)
            if result:
                team_data["sections"][section] = result
            time.sleep(0.3)

        path = f"{DATA}/teams/{safe}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(team_data, f, ensure_ascii=False, indent=2)
        team_index.append({"name": name, "id": team_id, "file": f"teams/{safe}.json"})
        print(f"  ✅ {name}")

    with open(f"{DATA}/teams/index.json", "w", encoding="utf-8") as f:
        json.dump(team_index, f, ensure_ascii=False, indent=2)


def scrape_standings():
    print("🏆 Scraping standings…")
    url = f"https://stats.baseball.cz/api/v1/events/extraliga-2026/standings"
    result = fetch(url)
    if result:
        with open(f"{DATA}/standings.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print("  ✅ standings.json")
    else:
        with open(f"{DATA}/standings.json", "w") as f:
            json.dump({}, f)


def write_meta(stats: dict):
    meta = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "source":       f"{BASE}/index",
        "season":       "Extraliga 2026",
        "round":        ROUND or "alle ronden",
        "player_counts": {s: len(v["data"]) for s, v in stats.items()},
        "api_params": {
            "stat_sections": STAT_SECTIONS,
            "teams":         TEAMS,
        },
    }
    with open(f"{DATA}/meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("  ✅ meta.json")


def main():
    print(f"\n🚀 Czech Extraliga Stats Scraper — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n")
    stats = scrape_all_stats()
    scrape_per_team()
    scrape_standings()
    # Schrijf lege splits (API niet toegankelijk)
    import os
    with open(f"{DATA}/splits.json", "w") as f:
        import json as _j; _j.dump({}, f)
    write_meta(stats)
    print("\n✅ Klaar! Alle data staat in /data/\n")


if __name__ == "__main__":
    main()

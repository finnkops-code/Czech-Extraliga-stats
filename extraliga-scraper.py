"""
Czech Extraliga Stats Scraper
Gebruikt de exacte API endpoints zoals opgegeven.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
import urllib.request

DATA = "data"
os.makedirs(DATA, exist_ok=True)

HEADERS_REQ = {
    "Accept":     "application/json",
    "Referer":    "https://stats.baseball.cz/en/events/extraliga-2026/stats",
    "User-Agent": "Mozilla/5.0 (compatible; ExtraligaBot/2.0)",
}

# Exacte URLs zoals opgegeven — stats-section staat in de URL zelf hardcoded
ENDPOINTS = {
    "batting":  "https://stats.baseball.cz/api/v1/stats/events/extraliga-2026/index?section=players&stats-section=batting&team=&round=6792&split=&team=&round=6792&split=&language=en",
    "pitching": "https://stats.baseball.cz/api/v1/stats/events/extraliga-2026/index?section=players&stats-section=pitching&team=&round=6792&split=&team=&round=6792&split=&language=en",
    "fielding": "https://stats.baseball.cz/api/v1/stats/events/extraliga-2026/index?section=players&stats-section=fielding&team=&round=6792&team=&round=6792&language=en",
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

STAT_SECTIONS = ["batting", "pitching", "fielding"]


def clean_name(html: str) -> str:
    last  = re.search(r'class="lastname"[^>]*>(.*?)</span>',  html)
    first = re.search(r'class="firstname"[^>]*>(.*?)</span>', html)
    if last and first:
        return f"{last.group(1)} {first.group(1)}"
    return " ".join(re.sub(r"<[^>]+>", " ", html).split()).strip()


def fetch(url: str) -> dict | None:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS_REQ)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  ⚠ Poging {attempt+1} mislukt: {e}")
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


def scrape_all_stats():
    print("📊 Scraping stats (alle secties)…")
    all_stats = {}

    for section, url in ENDPOINTS.items():
        print(f"  ↳ {section}…")
        result = fetch(url)
        if result:
            data = {
                "data":    clean_players(result.get("data", [])),
                "headers": annotate_headers(result.get("headers", [])),
            }
            all_stats[section] = data
            print(f"     {len(data['data'])} spelers")
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

        for section, base_url in ENDPOINTS.items():
            # Vervang team= door het juiste team_id
            url = re.sub(r'team=(?=&|$)', f'team={team_id}', base_url, count=1)
            result = fetch(url)
            if result:
                team_data["sections"][section] = {
                    "data":    clean_players(result.get("data", [])),
                    "headers": annotate_headers(result.get("headers", [])),
                }
            time.sleep(0.3)

        path = f"{DATA}/teams/{safe}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(team_data, f, ensure_ascii=False, indent=2)
        team_index.append({"name": name, "id": team_id, "file": f"teams/{safe}.json"})
        print(f"  ✅ {name}")

    with open(f"{DATA}/teams/index.json", "w", encoding="utf-8") as f:
        json.dump(team_index, f, ensure_ascii=False, indent=2)


def write_meta(stats: dict):
    meta = {
        "last_updated":  datetime.now(timezone.utc).isoformat(),
        "source":        "https://stats.baseball.cz/api/v1/stats/events/extraliga-2026/index",
        "season":        "Extraliga 2026",
        "player_counts": {s: len(v["data"]) for s, v in stats.items()},
        "teams":         TEAMS,
    }
    with open(f"{DATA}/meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("  ✅ meta.json")


def main():
    print(f"\n🚀 Czech Extraliga Stats Scraper — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n")
    stats = scrape_all_stats()
    scrape_per_team()
    write_meta(stats)
    print("\n✅ Klaar!\n")


if __name__ == "__main__":
    main()

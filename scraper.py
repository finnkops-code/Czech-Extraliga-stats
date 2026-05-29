"""
Czech Baseball Extraliga Stats Scraper
Identieke aanpak als de KNBSB Hoofdklasse scraper.
Draait via GitHub Actions, slaat resultaten op in data/stats.json
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import requests

BASE = "https://stats.baseball.cz/api/v1/stats/events/extraliga-2026"
DATA = "data"
os.makedirs(DATA, exist_ok=True)

HEADERS = {
    "Accept": "application/json",
    "Referer": "https://stats.baseball.cz/en/events/extraliga-2026/stats",
    "User-Agent": "Mozilla/5.0 (compatible; ExtraligaBot/2.0)",
}

STAT_SECTIONS = ["batting", "pitching", "fielding"]


def clean_name(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return " ".join(text.split()).strip()


def fetch(url: str, params: dict = None) -> dict | None:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  ⚠ Poging {attempt+1} mislukt ({url}): {e}")
            time.sleep(2 ** attempt)
    return None


def clean_players(data: list) -> list:
    """
    Maak namen leesbaar en normaliseer veldnamen zodat de shortcode
    dezelfde kolomnamen verwacht als de DBL shortcode:
    name → Player, team → Teamname
    """
    out = []
    for row in data:
        row = dict(row)
        # Naam opschonen
        row["name"] = clean_name(row.get("name", ""))
        # Normaliseer naar Player / Teamname zodat de shortcode werkt
        row["Player"]   = row.get("name", "")
        row["Teamname"] = row.get("team", "")
        # Spelerlink
        link = row.get("link", "")
        m = re.search(r"/players/(\d+)$", link)
        row["player_id"] = m.group(1) if m else None
        out.append(row)
    return out


def annotate_headers(headers: list) -> list:
    for h in headers:
        if h.get("format"):
            h["format_type"] = "baseball_pct"
        # Normaliseer ook de column-naam in de headers
        if h.get("column") == "name":
            h["column"] = "Player"
        if h.get("column") == "team":
            h["column"] = "Teamname"
    return headers


def scrape_section(section: str) -> dict | None:
    params = {
        "section": "players",
        "stats-section": section,
        "language": "en",
    }
    result = fetch(f"{BASE}/index", params)
    if not result:
        return None
    return {
        "data":    clean_players(result.get("data", [])),
        "headers": annotate_headers(result.get("headers", [])),
    }


def main():
    print(f"\nCzech Extraliga Scraper — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n")

    all_stats = {}
    for section in STAT_SECTIONS:
        print(f"  {section}…")
        result = scrape_section(section)
        if result:
            all_stats[section] = result
            print(f"    {len(result['data'])} rijen")
        else:
            print(f"    ⚠ Geen data")
        time.sleep(0.5)

    # Meta
    meta = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "source": BASE,
        "season": "Czech Baseball Extraliga 2026",
        "player_counts": {s: len(v["data"]) for s, v in all_stats.items()},
    }

    os.makedirs(DATA, exist_ok=True)
    with open(f"{DATA}/stats.json", "w", encoding="utf-8") as f:
        json.dump(all_stats, f, ensure_ascii=False, indent=2)
    with open(f"{DATA}/meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    total = sum(len(v["data"]) for v in all_stats.values())
    print(f"\n✅ stats.json geschreven ({total} totaal rijen)\n")


if __name__ == "__main__":
    main()

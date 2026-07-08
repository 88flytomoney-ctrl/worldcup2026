#!/usr/bin/env python3
"""
build_index.py
Builds index.html from template + data/matches.json + data/lineups.json + data/data_overrides.json.
Run after every scrape or AI re-prediction to refresh the static page for GitHub Pages.
"""
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
MATCHES_FILE = DATA / "matches.json"
OVERRIDES_FILE = DATA / "data_overrides.json"
LINEUPS_FILE = DATA / "lineups.json"
TEMPLATE_FILE = BASE / "templates" / "index.template.html"
INDEX_FILE = BASE / "index.html"

HKT = timezone(timedelta(hours=8))


def load_data():
    matches = json.loads(MATCHES_FILE.read_text())
    overrides = {"overrides": []}
    if OVERRIDES_FILE.exists():
        overrides = json.loads(OVERRIDES_FILE.read_text())
    lineups = {}
    if LINEUPS_FILE.exists():
        lineups = json.loads(LINEUPS_FILE.read_text())
    return matches, overrides, lineups


def apply_overrides(matches_db, overrides):
    """Apply user overrides on top of scraped data."""
    matches = matches_db["matches"]
    applied = 0
    for ov in overrides.get("overrides", []):
        pair = ov.get("match", "")
        if " vs " not in pair:
            continue
        t1, t2 = [x.strip() for x in pair.split(" vs ", 1)]
        field = ov.get("field")
        value = ov.get("value")
        for m in matches:
            if (m["team1"] == t1 and m["team2"] == t2) or (m["team1"] == t2 and m["team2"] == t1):
                m[field] = value
                if field == "datetime":
                    try:
                        dt = datetime.fromisoformat(value)
                        m["date"] = dt.strftime("%Y-%m-%d")
                        m["kickoff_time"] = dt.strftime("%H:%M")
                    except Exception:
                        pass
                applied += 1
                print(f"   ✓ Override applied: {pair} {field}={value}")
    print(f"📌 Applied {applied} overrides")
    return matches_db


def main():
    matches_db, overrides, lineups = load_data()
    matches_db = apply_overrides(matches_db, overrides)

    # Mark is_past based on actual current time vs match datetime.
    # Rule: a match is "past" when its scheduled datetime has passed,
    # regardless of whether Fox Sports has marked it "final" yet.
    # Stale pregame matches (synthetic entries never updated by Fox)
    # should still be greyed out when their datetime is in the past.
    now = datetime.now(HKT)
    for m in matches_db["matches"]:
        try:
            dt_str = m.get("datetime", "")
            if "T" in dt_str:
                mdt = datetime.fromisoformat(dt_str)
            else:
                # Date-only → end of day so matches don't grey out before they start
                mdt = datetime.fromisoformat(dt_str + "T23:59:59+08:00")
            if mdt.tzinfo is None:
                mdt = mdt.replace(tzinfo=HKT)
            m["is_past"] = mdt < now
        except Exception:
            # Fall back to status-based logic if datetime parsing fails
            m["is_past"] = m.get("status") == "final"

    # Inject JSON into template
    template = TEMPLATE_FILE.read_text()
    matches_json = json.dumps(matches_db, ensure_ascii=False)
    lineups_json = json.dumps(lineups, ensure_ascii=False)
    last_updated_hkt = datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT")

    output = template.replace("__MATCHES_JSON__", matches_json)
    output = output.replace("__LINEUPS_JSON__", lineups_json)
    output = output.replace("__LAST_UPDATED__", last_updated_hkt)

    INDEX_FILE.write_text(output, encoding="utf-8")
    print(f"✅ Built {INDEX_FILE} ({len(output):,} chars, {matches_db['matchCount']} matches)")


if __name__ == "__main__":
    main()

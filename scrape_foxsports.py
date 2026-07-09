#!/usr/bin/env python3
"""
scrape_foxsports.py
Scrapes FIFA World Cup 2026 scores from Fox Sports and produces/updates matches.json.
Fox Sports is the single source of truth for teams, groups, dates, and real scores.
Applies data_overrides.json on top for manual corrections.
"""

import os
import re
import json
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MATCHES_FILE = DATA_DIR / "matches.json"
OVERRIDES_FILE = DATA_DIR / "data_overrides.json"

# ── Blacklist ───────────────────────────────────────────────────────────────
# Fox Sports sometimes shows wrong synthetic matches (e.g., Spain vs Belgium
# on 07-01 as R32 when they actually meet in QF on 07-12).
# These IDs are always removed.
BLACKLIST_IDS = {
    "ESP-BEL-2026-07-01",  # Fake R32 — Spain & Belgium meet in QF, not R32
}
TEAM_MAP_FILE = DATA_DIR / "team_map.json"

FOX_URL = "https://www.foxsports.com/soccer/fifa-world-cup/scores"
HKT = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def load_json(path, default=None):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else {}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_fox_html():
    """Fetch the Fox Sports World Cup scores page."""
    req = urllib.request.Request(FOX_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_fox_matches(html):
    """Parse Fox Sports HTML into structured match data."""
    matches = []
    seen_pair_dates = set()  # Dedupe: same team pair on same date

    # Split HTML by score-chip blocks
    chip_blocks = re.findall(
        r'<a href="([^"]*fifa-world-cup-men[^"]*)"[^>]*class="score-chip (final|pregame)">(.*?)</a>',
        html, re.DOTALL
    )

    for href, status, block in chip_blocks:
        # Group name
        group_m = re.search(r'<span>(GROUP [A-L])</span>', block)
        group = group_m.group(1) if group_m else ""

        # Team names (full English)
        teams = re.findall(r'score-team-name team[^>]*>[^<]*<span[^>]*title="([^"]+)"', block)
        # Abbreviations
        abbrs = re.findall(r'score-team-name abbreviation[^>]*>[^<]*<span[^>]*title="([^"]+)"', block)

        # Scores - format: <span class="scores-text"><!--[--><!----> <!----> 2<!--]--></span>
        scores = []
        score_spans = re.findall(r'score-team-score[^>]*>(.*?)</div>', block, re.DOTALL)
        for span in score_spans:
            # Extract trailing number before <!--]-->
            num_m = re.search(r'(\d+)\s*<!--\]', span)
            if num_m:
                scores.append(num_m.group(1))

        # Determine winner/loser for final matches
        has_loser = 'is-loser score-team-row' in block

        # Game date from href
        date_m = re.search(r'-(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-(\d+)-(\d{4})', href, re.IGNORECASE)
        game_date = ""
        if date_m:
            month_str = date_m.group(1).capitalize()
            day = int(date_m.group(2))
            year = int(date_m.group(3))
            try:
                month_num = {
                    'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                    'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12
                }[month_str]
                game_date = f"{year}-{month_num:02d}-{day:02d}"
            except KeyError:
                pass

        # For pregame: try to extract kickoff time
        kickoff_time = ""
        time_m = re.search(r'score-team-pregame-info[^>]*>.*?<span[^>]*>([^<]+)</span>', block, re.DOTALL)
        if time_m:
            t = time_m.group(1).strip()
            if t and t != "-":
                kickoff_time = t

        if len(teams) >= 2:
            # Dedupe: skip if same team pair already seen
            pair_key = tuple(sorted([teams[0], teams[1]]))
            if pair_key in seen_pair_dates:
                continue
            seen_pair_dates.add(pair_key)
            
            match_data = {
                "id": f"{abbrs[0] if len(abbrs) > 0 else teams[0][:3]}-{abbrs[1] if len(abbrs) > 1 else teams[1][:3]}-{game_date}",
                "team1": teams[0],
                "team2": teams[1],
                "abbr1": abbrs[0] if len(abbrs) > 0 else "",
                "abbr2": abbrs[1] if len(abbrs) > 1 else "",
                "group": group,
                "date": game_date,
                "kickoff_time": kickoff_time,
                "status": status,  # "final" or "pregame"
                "score1": int(scores[0]) if len(scores) > 0 else None,
                "score2": int(scores[1]) if len(scores) > 1 else None,
                "winner": "",
            }

            # Determine winner
            if status == "final" and has_loser and len(scores) >= 2:
                if int(scores[0]) > int(scores[1]):
                    match_data["winner"] = "team1"
                elif int(scores[1]) > int(scores[0]):
                    match_data["winner"] = "team2"
                else:
                    match_data["winner"] = "draw"

            matches.append(match_data)

    return matches


def generate_group_schedule(teams_by_group):
    """
    Given a dict of {group_name: [team1, team2, team3, team4]},
    generate the full round-robin match schedule (6 matches per group).
    Matchday 1&2 known from Fox; Matchday 3&4&5&6 are generated.
    """
    # Standard WC 4-team round-robin pairing order:
    # MD1: 1v2, 3v4
    # MD2: 1v3, 2v4
    # MD3: 1v4, 2v3
    pairings = [
        (0, 1), (2, 3),  # Matchday 1
        (0, 2), (1, 3),  # Matchday 2
        (0, 3), (1, 2),  # Matchday 3
    ]
    return pairings


def build_full_schedule(fox_matches):
    """
    Build a complete tournament schedule from Fox Sports data.
    If Fox only shows partial data, fill in missing group matches.
    """
    # Group teams from Fox data
    teams_by_group = {}
    existing_match_ids = set()
    
    for m in fox_matches:
        if m["group"]:
            if m["group"] not in teams_by_group:
                teams_by_group[m["group"]] = []
            for t in [m["team1"], m["team2"]]:
                if t not in teams_by_group[m["group"]]:
                    teams_by_group[m["group"]].append(t)
        existing_match_ids.add(m["id"])
    
    all_matches = list(fox_matches)
    
    # For each group, generate missing round-robin matches
    pairings = generate_group_schedule(None)
    
    # Build a set of team-pairs already covered by Fox (regardless of date)
    existing_match_keys = set()
    for m in fox_matches:
        # Pair-only key (the dedup logic; we trust Fox to have the right date)
        pair_only = tuple(sorted([m["team1"], m["team2"]]))
        existing_match_keys.add(pair_only)
    
    # WC 2026 group stage date windows (approximate)
    # MD1: Jun 11-14, MD2: Jun 17-20, MD3: Jun 23-26
    matchday_dates = {
        0: {"start": "2026-06-11", "end": "2026-06-14"},  # MD1
        1: {"start": "2026-06-17", "end": "2026-06-20"},  # MD2
        2: {"start": "2026-06-23", "end": "2026-06-26"},  # MD3
    }
    
    # Group date offsets (each group starts on different days)
    group_date_offset = {
        "GROUP A": 0, "GROUP B": 1, "GROUP C": 2, "GROUP D": 2,
        "GROUP E": 3, "GROUP F": 3, "GROUP G": 4, "GROUP H": 4,
        "GROUP I": 5, "GROUP J": 5, "GROUP K": 6, "GROUP L": 6,
    }
    
    # Standard kick-off times (HKT) for 2026 WC: 03:00, 06:00, 09:00, 12:00
    kickoff_slots = ["03:00", "06:00", "09:00", "12:00"]
    
    for group, teams in teams_by_group.items():
        if len(teams) != 4:
            continue
        
        offset = group_date_offset.get(group, 0)
        
        for pair_idx, (i, j) in enumerate(pairings):
            matchday = pair_idx // 2
            slot = pair_idx % 2
            match_idx_in_day = slot + (0 if group in ["GROUP A", "GROUP C", "GROUP E", "GROUP G", "GROUP I", "GROUP K"] else 2)
            
            # Calculate match date
            base_date = datetime(2026, 6, 11) + timedelta(days=offset + matchday * 6)
            match_date = base_date.strftime("%Y-%m-%d")
            kickoff = kickoff_slots[match_idx_in_day % len(kickoff_slots)]
            
            t1 = teams[i]
            t2 = teams[j]
            
            # Check for duplicates using team pair only (date-independent)
            dedup_key = tuple(sorted([t1, t2]))
            if dedup_key in existing_match_keys:
                continue
            
            # Try to get proper abbreviations from Fox's existing match data
            abbr1 = ""
            abbr2 = ""
            for fm in fox_matches:
                if fm["group"] == group:
                    if fm["team1"] == t1:
                        abbr1 = abbr1 or fm["abbr1"]
                    if fm["team2"] == t1:
                        abbr1 = abbr1 or fm["abbr2"]
                    if fm["team1"] == t2:
                        abbr2 = abbr2 or fm["abbr1"]
                    if fm["team2"] == t2:
                        abbr2 = abbr2 or fm["abbr2"]
            # Fallback abbreviations from common FIFA codes
            if not abbr1:
                abbr1 = t1[:3].upper()
            if not abbr2:
                abbr2 = t2[:3].upper()
            
            match_id = f"{abbr1}-{abbr2}-{match_date}"
            
            all_matches.append({
                "id": match_id,
                "team1": t1,
                "team2": t2,
                "abbr1": abbr1,
                "abbr2": abbr2,
                "group": group,
                "date": match_date,
                "kickoff_time": kickoff,
                "status": "pregame",
                "score1": None,
                "score2": None,
                "winner": "",
            })
            existing_match_ids.add(match_id)
            existing_match_keys.add(dedup_key)
    
    return all_matches


def apply_overrides(matches, overrides_data):
    """Apply data_overrides.json corrections to match data."""
    overrides = overrides_data.get("overrides", [])
    applied = 0
    for ov in overrides:
        match_desc = ov.get("match", "")
        field = ov.get("field", "")
        value = ov.get("value", "")
        
        # Parse "Team1 vs Team2" format
        parts = match_desc.split(" vs ")
        if len(parts) != 2:
            continue
        t1_search, t2_search = parts[0].strip().lower(), parts[1].strip().lower()
        
        for m in matches:
            mt1 = m["team1"].lower()
            mt2 = m["team2"].lower()
            # Match if search terms appear in team names (handles "Czech" matching "Czechia")
            if (t1_search in mt1 and t2_search in mt2) or \
               (t1_search in mt2 and t2_search in mt1):
                # Apply the override
                old_val = m.get(field, "")
                m[field] = value
                applied += 1
                print(f"  ✅ Override: {match_desc} → {field}: {old_val} → {value}")
                # If datetime was overridden, also update date and kickoff_time fields
                if field == "datetime" and "T" in value:
                    try:
                        dt_part, time_part = value.split("T", 1)
                        time_only = time_part.split("+")[0].split("-")[0][:5]  # HH:MM
                        m["date"] = dt_part
                        m["kickoff_time"] = time_only
                        print(f"     → date={dt_part}, kickoff_time={time_only}")
                    except Exception as e:
                        print(f"     ⚠️ Could not parse datetime: {e}")
                break  # Only apply to first matching match
    
    return matches, applied


def enrich_with_translations(matches, team_map):
    """Add Chinese names and flag emojis from team_map.json."""
    for m in matches:
        for prefix in ["team1", "team2"]:
            name = m[prefix]
            info = team_map.get(name, {})
            if not info:
                # Try alternative names
                for alt_name, alt_info in team_map.items():
                    if alt_name.startswith("_"):
                        continue
                    if name.lower() in alt_name.lower() or alt_name.lower() in name.lower():
                        info = alt_info
                        break
            m[f"{prefix}_zh"] = info.get("zh", name)
            m[f"{prefix}_flag"] = info.get("flag", "🏳️")
    return matches


def dedupe_matches(matches):
    """
    Remove duplicate matches by sorted team-pair.
    When the same team-pair appears multiple times:
    - Prefer 'final' status over 'pregame' (real result over synthetic)
    - If same status, prefer the one with an earlier date (actual match date)
    - Merge AI predictions from the loser into the winner
    """
    by_pair = {}
    for m in matches:
        pair_key = tuple(sorted([m["team1"], m["team2"]]))
        if pair_key not in by_pair:
            by_pair[pair_key] = m
            continue
        
        existing = by_pair[pair_key]
        # Prefer final over pregame
        if m["status"] == "final" and existing["status"] != "final":
            # Merge predictions from the old (likely synthetic pregame) entry
            for pred_key in ["predicted_score", "predicted_first_scorer",
                            "predicted_first_scorer_team", "predicted_confidence", "predicted_at"]:
                if pred_key in existing and pred_key not in m:
                    m[pred_key] = existing[pred_key]
            by_pair[pair_key] = m
        elif m["status"] == existing["status"]:
            # Same status: prefer earlier date (actual match date, not synthetic)
            if m.get("date", "9999") < existing.get("date", "9999"):
                by_pair[pair_key] = m
        # else: keep existing (it's final, new one is pregame)
    
    return list(by_pair.values())


def merge_with_existing(new_matches, existing_data):
    """
    Merge newly scraped data with existing matches.json.
    - Keep AI predictions from existing data
    - Update scores for completed matches
    - Add new matches from Fox
    """
    existing_by_id = {}
    if existing_data and "matches" in existing_data:
        for m in existing_data["matches"]:
            existing_by_id[m.get("id", "")] = m
    
    merged = []
    seen_ids = set()
    
    # Start with new/updated matches
    for m in new_matches:
        mid = m.get("id", "")
        seen_ids.add(mid)
        
        if mid in existing_by_id:
            old = existing_by_id[mid]
            # Preserve AI prediction fields if they exist
            for pred_key in ["predicted_score", "predicted_first_scorer",
                            "predicted_first_scorer_team", "predicted_confidence", "predicted_at",
                            "pred_score1", "pred_score2", "pred_scorer", "pred_scorer_team"]:
                if pred_key in old and m.get("status") == "pregame":
                    m[pred_key] = old[pred_key]
            # Preserve datetime if it was set (from overrides or previous runs)
            if "datetime" in old and old["datetime"]:
                m["datetime"] = old["datetime"]
            # Preserve prediction timestamp
            if "predicted_at" in old:
                m["predicted_at"] = old["predicted_at"]
        
        merged.append(m)
    
    # Keep any existing matches not in Fox data (e.g. knockout bracket predictions)
    for mid, old in existing_by_id.items():
        if mid not in seen_ids:
            merged.append(old)
            seen_ids.add(mid)
    
    return merged


def main():
    print("⚽ Fox Sports World Cup 2026 Scraper")
    print("=" * 50)
    
    # Load team map
    team_map = load_json(TEAM_MAP_FILE, {})
    print(f"📋 Team map: {len([k for k in team_map if not k.startswith('_')])} teams")
    
    # Load overrides
    overrides = load_json(OVERRIDES_FILE, {"overrides": []})
    print(f"🔧 Overrides: {len(overrides.get('overrides', []))} rules")
    
    # Load existing data
    existing = load_json(MATCHES_FILE, {})
    existing_match_count = len(existing.get("matches", []))
    print(f"📦 Existing matches: {existing_match_count}")
    
    # Fetch and parse Fox Sports
    print(f"\n🌐 Fetching {FOX_URL} ...")
    try:
        html = fetch_fox_html()
        print(f"   Got {len(html)} bytes")
    except Exception as e:
        print(f"❌ Failed to fetch Fox Sports: {e}")
        print("   Using existing data only (if any)")
        if existing_match_count == 0:
            raise SystemExit(1)
        html = ""
    
    fox_matches = []
    if html:
        fox_matches = parse_fox_matches(html)
        # Remove blacklisted matches (fake/wrong from Fox)
        if BLACKLIST_IDS:
            before_bl = len(fox_matches)
            fox_matches = [m for m in fox_matches if m.get("id", "") not in BLACKLIST_IDS]
            bl_removed = before_bl - len(fox_matches)
            if bl_removed:
                print(f"🚫 Blacklist: removed {bl_removed} fake match(es)")
        print(f"   Parsed {len(fox_matches)} matches from Fox")
        final_count = sum(1 for m in fox_matches if m["status"] == "final")
        pregame_count = sum(1 for m in fox_matches if m["status"] == "pregame")
        print(f"   Final: {final_count}, Pregame: {pregame_count}")
    
    # Build full schedule (fill in missing group matches)
    if fox_matches:
        all_matches = build_full_schedule(fox_matches)
        print(f"\n📅 Full schedule: {len(all_matches)} matches (group stage)")
        # Dedupe: remove synthetic matches that have real Fox results
        before = len(all_matches)
        all_matches = dedupe_matches(all_matches)
        removed = before - len(all_matches)
        if removed:
            print(f"🧹 Deduped {removed} stale synthetic matches (real Fox results exist)")
    else:
        all_matches = existing.get("matches", [])
    
    # Enrich with Chinese translations + flags
    all_matches = enrich_with_translations(all_matches, team_map)
    
    # Merge with existing data (preserves AI predictions)
    all_matches = merge_with_existing(all_matches, existing)
    
    # Remove blacklisted matches from merged data too
    if BLACKLIST_IDS:
        before_bl = len(all_matches)
        all_matches = [m for m in all_matches if m.get("id", "") not in BLACKLIST_IDS]
        bl_removed = before_bl - len(all_matches)
        if bl_removed:
            print(f"🚫 Blacklist (post-merge): removed {bl_removed} fake match(es)")
    
    # Final dedupe pass on merged data
    before_merge = len(all_matches)
    all_matches = dedupe_matches(all_matches)
    final_removed = before_merge - len(all_matches)
    if final_removed:
        print(f"🧹 Final dedupe: removed {final_removed} duplicates from merged data")
    
    # Apply overrides
    all_matches, n_applied = apply_overrides(all_matches, overrides)
    print(f"\n🔧 Overrides applied: {n_applied}")
    
    # Build datetime fields from date + kickoff_time
    now = datetime.now(HKT)
    for m in all_matches:
        if "datetime" not in m or not m["datetime"]:
            if m.get("date") and m.get("kickoff_time"):
                try:
                    dt = datetime.strptime(f"{m['date']} {m['kickoff_time']}", "%Y-%m-%d %H:%M")
                    m["datetime"] = dt.replace(tzinfo=HKT).isoformat()
                except ValueError:
                    m["datetime"] = m["date"]
            elif m.get("date"):
                m["datetime"] = m["date"]
        
        # Determine if match is in the past
        if m.get("datetime"):
            try:
                match_dt = datetime.fromisoformat(m["datetime"])
                if match_dt.tzinfo is None:
                    match_dt = match_dt.replace(tzinfo=HKT)
                # Mark as past if the match time has passed — regardless of status
                # This catches both final matches AND stale pregame matches
                m["is_past"] = match_dt < now
            except (ValueError, TypeError):
                m["is_past"] = False
        else:
            m["is_past"] = False
    
    # Sort by date
    all_matches.sort(key=lambda m: m.get("datetime", "") or m.get("date", ""))
    
    # Save
    output = {
        "lastUpdated": now.isoformat(),
        "source": "foxsports",
        "matchCount": len(all_matches),
        "matches": all_matches,
    }
    save_json(MATCHES_FILE, output)
    print(f"\n✅ Saved {len(all_matches)} matches to {MATCHES_FILE}")
    
    # Summary
    groups = set(m.get("group", "") for m in all_matches if m.get("group"))
    print(f"   Groups: {sorted(groups)}")
    final = sum(1 for m in all_matches if m.get("status") == "final")
    pregame = sum(1 for m in all_matches if m.get("status") == "pregame")
    has_pred = sum(1 for m in all_matches if m.get("predicted_score") or m.get("pred_score1") is not None)
    print(f"   Final: {final}, Pregame: {pregame}, With AI prediction: {has_pred}")


if __name__ == "__main__":
    main()

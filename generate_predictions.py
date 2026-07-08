#!/usr/bin/env python3
"""
generate_predictions.py
Calls OpenRouter LLM to predict scores + first scorer for upcoming WC 2026 matches.
Updates matches.json in place. Only re-predicts matches without an existing prediction
(unless --force is passed to re-predict all pregame matches).
"""

import os
import sys
import json
import time
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

from openai import OpenAI

# ── Config ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MATCHES_FILE = DATA_DIR / "matches.json"
LINEUPS_FILE = DATA_DIR / "lineups.json"

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
AI_MODEL_ID = os.environ.get("AI_MODEL_ID", "poolside/laguna-xs-2.1:free")

HKT = timezone(timedelta(hours=8))


def get_client():
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set in environment")
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
    )


def _team_zh_key(match, side):
    """Return the Chinese team name used as key in lineups.json (team1_zh / team2_zh)."""
    return match.get(f"{side}_zh") or match.get(side, "")


def _load_lineups():
    if LINEUPS_FILE.exists():
        try:
            return json.loads(LINEUPS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def predict_match(client, match, lineups=None):
    """Ask the LLM to predict score + first scorer for one match.

    The LLM is *constrained* to pick the first scorer from the supplied
    squad lineup (lineups.json). This prevents hallucinated players such
    as retired internationals (Shaqiri 沙基利/沙奇里) appearing as scorers.
    """
    t1 = match["team1"]
    t2 = match["team2"]
    group = match.get("group", match.get("stage", ""))
    lineups = lineups or {}

    t1_key = _team_zh_key(match, "team1")
    t2_key = _team_zh_key(match, "team2")
    t1_lineup = lineups.get(t1_key, [])
    t2_lineup = lineups.get(t2_key, [])

    # Extract just the player names (strip "門將: ", "後衛: " etc.)
    def _names(lineup):
        out = []
        for entry in lineup:
            if ":" in entry:
                out.append(entry.split(":", 1)[1].strip())
            else:
                out.append(entry.strip())
        return out

    t1_names = _names(t1_lineup)
    t2_names = _names(t2_lineup)

    if t1_names and t2_names:
        squad_block = (
            f"\nVerified 2026 World Cup squad starting XI (use ONLY these names — do NOT invent players):\n"
            f"  {t1} ({t1_key}): {', '.join(t1_names)}\n"
            f"  {t2} ({t2_key}): {', '.join(t2_names)}\n\n"
            f"CRITICAL: first_scorer MUST be one of the names listed above. "
            f"Do NOT use retired players. Do NOT use players from previous tournaments. "
            f"Do NOT list any name more than once across both squads.\n"
        )
    else:
        squad_block = ""

    prompt = (
        f"You are a football statistics engine predicting FIFA World Cup 2026 results.\n"
        f"Predict the score and first goal scorer for this match:\n\n"
        f"  {t1} vs {t2} ({group})\n"
        f"{squad_block}\n"
        f"Return ONLY a valid JSON object — no markdown, no explanations:\n"
        f'{{"score1": <int>, "score2": <int>, "first_scorer": "<scorer name in Traditional Chinese — MUST be from the squad list above>", "scorer_team": "<{t1} or {t2}>", "confidence": <0.0-1.0>}}\n\n'
        f"Output JSON:"
    )
    
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=AI_MODEL_ID,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=1000,
                timeout=30,
                extra_body={"reasoning": {"enabled": False}},
            )
            msg = response.choices[0].message
            raw = msg.content
            if not raw:
                raw = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
            if not raw:
                raise ValueError("Model returned empty content")
            raw = raw.strip()
            
            # Strip markdown if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw
                if raw.endswith("```"):
                    raw = raw.rsplit("\n", 1)[0] if "\n" in raw else raw
                if raw.startswith("json"):
                    raw = raw[4:].lstrip()
            raw = raw.strip()
            
            # Try to find JSON object boundaries
            if not raw.startswith("{"):
                start = raw.find("{")
                end = raw.rfind("}")
                if start >= 0 and end > start:
                    raw = raw[start:end+1]
            
            parsed = json.loads(raw)
            score1 = int(parsed.get("score1", 1))
            score2 = int(parsed.get("score2", 1))
            scorer = str(parsed.get("first_scorer", "未知")).strip()
            scorer_team = str(parsed.get("scorer_team", t1)).strip()

            # Squad validation: if a lineup is available, the scorer MUST be in it.
            # Otherwise fall back to "首位破門球員" (TBD) instead of hallucinating.
            if t1_names or t2_names:
                allowed = set(t1_names) | set(t2_names)
                if scorer not in allowed:
                    # Try a soft match (substring) before giving up
                    soft = next((n for n in allowed if scorer and (scorer in n or n in scorer)), None)
                    if soft:
                        scorer = soft
                    else:
                        print(f"   ⚠️  Scorer '{scorer}' not in squad — replacing with TBD")
                        scorer = "首位破門球員待定"
                        scorer_team = t1 if score1 >= score2 else t2

            return {
                "predicted_score": f"{score1}-{score2}",
                "predicted_first_scorer": scorer,
                "predicted_first_scorer_team": scorer_team,
                "predicted_confidence": float(parsed.get("confidence", 0.5)),
                "predicted_at": datetime.now(HKT).isoformat(),
            }
        except Exception as e:
            if attempt < 2:
                print(f"   ⚠️ Attempt {attempt+1} failed: {e}, retrying...")
                time.sleep(2)
            else:
                print(f"   ❌ Prediction failed for {t1} vs {t2}: {e}")
                return None


def main():
    force = "--force" in sys.argv
    
    print("🤖 AI Match Predictor (OpenRouter)")
    print("=" * 50)
    print(f"   Model: {AI_MODEL_ID}")
    print(f"   Force mode: {force}")
    
    if not MATCHES_FILE.exists():
        print(f"❌ {MATCHES_FILE} not found. Run scrape_foxsports.py first.")
        sys.exit(1)
    
    with open(MATCHES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    matches = data.get("matches", [])
    print(f"📦 Loaded {len(matches)} matches")
    
    if not OPENROUTER_API_KEY:
        print("⚠️ OPENROUTER_API_KEY not set — skipping predictions.")
        sys.exit(0)
    
    client = get_client()
    lineups = _load_lineups()
    print(f"📋 Loaded squad lineups for {len(lineups)} teams")
    
    # Find pregame matches needing prediction
    to_predict = []
    for m in matches:
        # Skip already-played (final) matches
        if m.get("status") == "final":
            continue
        # Skip matches that already have a prediction, unless --force
        if not force and m.get("predicted_score"):
            continue
        # On --force, clear old prediction so we overwrite
        if force and m.get("predicted_score"):
            for key in ["predicted_score", "predicted_first_scorer",
                        "predicted_first_scorer_team", "predicted_confidence", "predicted_at",
                        "pred_score1", "pred_score2", "pred_scorer", "pred_scorer_team", "pred_confidence"]:
                m.pop(key, None)
        to_predict.append(m)
    
    print(f"🎯 Matches to predict: {len(to_predict)}")
    
    if not to_predict:
        print("✅ Nothing to do.")
        return
    
    success = 0
    fail = 0
    
    for i, m in enumerate(to_predict, 1):
        t1, t2 = m.get("team1", "?"), m.get("team2", "?")
        group = m.get("group", m.get("stage", ""))
        print(f"\n[{i}/{len(to_predict)}] {t1} vs {t2} ({group})")
        pred = predict_match(client, m, lineups=lineups)
        if pred:
            m.update(pred)
            print(f"   ✅ {pred['predicted_score']} | first scorer: {pred['predicted_first_scorer']} ({pred['predicted_first_scorer_team']})")
            success += 1
        else:
            fail += 1
        # Rate limit: 0.5s between requests
        time.sleep(0.5)
    
    # Save back
    data["lastPredictedAt"] = datetime.now(HKT).isoformat()
    with open(MATCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ Done. Success: {success}, Failed: {fail}")
    print(f"   Saved to {MATCHES_FILE}")


if __name__ == "__main__":
    main()

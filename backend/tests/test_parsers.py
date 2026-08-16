"""Golden-file parser tests against captured real API responses (15e)."""
import json
from pathlib import Path

from backend.sources import draftkings as dk
from backend.sources import fantasypros as fp
from backend.sources import odds as oddsrc
from backend.sources.imports import (export_dkentries, parse_dkentries,
                                     parse_standings, parse_standings_lineup)

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def test_main_slate_identification():
    groups = dk.find_main_slate_groups(load("contests_groups_only.json"))
    assert groups, "should find at least one main-slate group"
    for g in groups:
        assert g["ContestTypeId"] == 21
        assert g.get("ContestStartTimeSuffix") in (None, "")
        assert g["GameCount"] >= 8


def test_parse_draftables_collapses_slots():
    parsed = dk.parse_draftables(load("dkdraftables_fixture_small.json"))
    players = parsed["players"]
    assert players
    flex = [p for p in players if p["position"] in ("RB", "WR", "TE")]
    for p in flex:
        ids = p["draftable_ids"]
        base_slot = {"RB": "67", "WR": "68", "TE": "69"}[p["position"]]
        if base_slot in ids and "70" in ids:
            assert ids["70"] == ids[base_slot] + 1  # verified +1 FLEX rule


def test_parse_contest_detail_payout_curve():
    d = dk.parse_contest_detail(load("contestdetail.json"))
    assert d["payout_curve"], "payout curve should be present"
    assert d["field_size"]
    first = d["payout_curve"][0]
    assert first["min_position"] == 1


def test_fp_projections_positions():
    players = fp.parse_projections(load("fp_projections.json"))
    positions = {p["position"] for p in players}
    assert positions == {"QB", "RB", "WR", "TE", "DST"}  # K filtered out
    hurts = next(p for p in players if p["name"] == "Jalen Hurts")
    assert hurts["stats"]["pass_yds"] > 100


def test_odds_implied_totals():
    lines = oddsrc.parse_game_lines(load("odds_bulk_small.json"))
    assert lines
    for ln in lines:
        assert abs((ln["home_implied"] + ln["away_implied"]) - ln["total"]) < 0.01
        assert ln["home"] and ln["away"]


def test_dkentries_roundtrip():
    text = (FIX / "dkentries_filled_small.csv").read_text()
    entries = parse_dkentries(text)
    assert entries
    e = entries[0]
    assert e["entry_id"].isdigit()
    assert len(e["slots"]) == 9
    assert e["slots"][0]["slot"] == "QB"
    assert e["slots"][0]["draftable_id"]
    out = export_dkentries(entries)
    reparsed = parse_dkentries(out)
    assert [x["entry_id"] for x in reparsed] == [x["entry_id"] for x in entries]
    assert reparsed[0]["slots"][0]["draftable_id"] == e["slots"][0]["draftable_id"]


def test_standings_parses_entries_and_ownership():
    text = (FIX / "standings_small.csv").read_text()
    parsed = parse_standings(text)
    assert parsed["entries"] and parsed["ownership"]
    assert parsed["entries"][0]["rank"] == 1
    own = parsed["ownership"][0]
    assert own["drafted_pct"] is not None
    slots = parse_standings_lineup(parsed["entries"][0]["lineup"])
    assert len(slots) == 9

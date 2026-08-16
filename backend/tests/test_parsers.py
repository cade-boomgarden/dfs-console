"""Golden-file parser tests against captured real API responses (15e)."""
import json

import pytest
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


def test_dk_status_none_string_is_normalised():
    """DK sends the literal string "None" for a healthy player, not null."""
    parsed = dk.parse_draftables(load("dkdraftables_fixture_small.json"))
    statuses = {p["status"] for p in parsed["players"]}
    assert "None" not in statuses, 'the string "None" must not survive parsing'
    assert None in statuses                      # healthy players
    assert statuses & {"Q", "OUT", "IR"}         # real designations preserved


class _StubClient:
    """Captures the request FantasyPros would receive."""
    def __init__(self, payload):
        self.payload, self.params = payload, None

    def get(self, url, params=None, headers=None):
        self.params = params
        body = self.payload

        class R:
            status_code = 200
            @staticmethod
            def raise_for_status(): pass
            @staticmethod
            def json(): return body
        return R()


def test_fp_fetch_requests_all_positions_and_a_limit():
    """Without `limit` the API returns only its default first page (~100
    players) -- a well-formed response covering a seventh of the slate."""
    payload = load("fp_projections.json")
    stub = _StubClient(payload)
    fp.fetch(2026, 1, "k", client=stub)
    assert stub.params["week"] == 1
    assert stub.params["position"] == "ALL"
    assert stub.params["limit"] >= 1000


def test_fp_fetch_rejects_a_truncated_response():
    payload = dict(load("fp_projections.json"))
    payload["count"] = "5000"                      # body has far fewer
    with pytest.raises(ValueError, match="truncated"):
        fp.fetch(2026, 1, "k", client=_StubClient(payload))


def test_fp_fetch_rejects_season_long_totals():
    payload = dict(load("fp_projections.json"))
    payload["week"] = "0"
    with pytest.raises(ValueError, match="season-long"):
        fp.fetch(2026, 1, "k", client=_StubClient(payload))


def test_fixture_projections_align_with_the_draftables_fixture():
    """Both fixtures must be the same season/week or the join is meaningless."""
    fpx = load("fp_projections.json")
    assert fpx["season"] == "2026" and fpx["week"] == "1"
    assert int(fpx["count"]) == len(fpx["players"]) > 600

"""Known-hard identity cases: suffixes, nicknames, DSTs, JAX/JAC (15e)."""
from backend.identity.resolver import Candidate, resolve_name
from backend.identity.rules import norm_name, norm_team


def test_norm_rules():
    assert norm_team("JAC") == "JAX"
    assert norm_team("LA") == "LAR"
    assert norm_name("Kenneth Walker III") == norm_name("Kenneth Walker")
    assert norm_name("Hollywood Brown") == norm_name("Marquise Brown")
    assert norm_name("Ja'Marr Chase") == norm_name("JaMarr Chase")


CANDS = [
    Candidate("1", "Kenneth Walker III", "SEA", "RB"),
    Candidate("2", "Marquise Brown", "KC", "WR"),
    Candidate("3", "Michael Williams", "LAC", "WR"),
    Candidate("4", "Seahawks", "SEA", "DST"),
    Candidate("5", "Travis Etienne Jr.", "JAX", "RB"),
]


def test_suffix_and_nickname_matching():
    r = resolve_name("Kenneth Walker", "SEA", "RB", CANDS)
    assert r.player_id == "1" and r.confidence == 1.0
    r = resolve_name("Hollywood Brown", "KC", "WR", CANDS)
    assert r.player_id == "2"


def test_nickname_map_promotes_to_exact():
    r = resolve_name("Mike Williams", "LAC", "WR", CANDS)
    assert r.player_id == "3" and r.confidence == 1.0


def test_first_initial_fallback():
    cands = CANDS + [Candidate("6", "Christopher Olave", "NO", "WR")]
    r = resolve_name("Chris Olave", "NO", "WR", cands)
    assert r.player_id == "6" and r.confidence < 0.85  # goes to review


def test_dst_by_team():
    r = resolve_name("Seattle Seahawks", "SEA", "DST", CANDS)
    assert r.player_id == "4" and r.method == "dst_team"


def test_jax_jac():
    r = resolve_name("Travis Etienne Jr.", "JAC", "RB", CANDS)
    assert r.player_id == "5" and r.confidence == 1.0


def test_unmatched_fails_loudly():
    r = resolve_name("Total Unknown", "ZZZ", "WR", CANDS)
    assert r.player_id is None and r.method == "unmatched"

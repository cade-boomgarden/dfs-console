"""Contest results import: classification, matching, top block, and the
large-field MME ownership on the player detail page."""
import os
import tempfile

os.environ["DFS_DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")
os.environ["DFS_BLOB_DIR"] = tempfile.mkdtemp()

from backend.api.players import player_detail                  # noqa: E402
from backend.api.results import lineups, list_results, observations  # noqa: E402
from backend.jobs import results as R                          # noqa: E402
from backend.models.db import Base, SessionLocal, engine       # noqa: E402
from backend.models.models import (Contest, ContestEntry, ContestLineup,  # noqa: E402
                                   OwnershipObservation, PlayerCanonical,
                                   SalaryHistory)

DG = 990001
HEAD = "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS\n"
L1 = ("DST Buccaneers  FLEX Kyren Williams QB Joe Burrow RB Bucky Irving RB Tony Pollard "
      "TE George Kittle WR Ja'Marr Chase WR Tee Higgins WR Zay Flowers")
L2 = ("DST Buccaneers  FLEX Tee Higgins QB Joe Burrow RB Bucky Irving RB Kyren Williams "
      "TE George Kittle WR Ja'Marr Chase WR Zay Flowers WR Puka Nacua")
PLAYERS = [("Joe Burrow", "QB", "CIN"), ("Bucky Irving", "RB", "TB"),
           ("Tony Pollard", "RB", "TEN"), ("Kyren Williams", "RB", "LAR"),
           ("George Kittle", "TE", "SF"), ("Ja'Marr Chase", "WR", "CIN"),
           ("Tee Higgins", "WR", "CIN"), ("Zay Flowers", "WR", "BAL"),
           ("Puka Nacua", "WR", "LAR"), ("Buccaneers", "DST", "TB"),
           ("Benchwarmer Guy", "WR", "NYJ")]
OWN = [("Joe Burrow", "QB", "40.10%", "30"), ("Buccaneers", "DST", "22.00%", "12"),
       ("Kyren Williams", "RB", "31.50%", "20"), ("Bucky Irving", "RB", "28.00%", "18"),
       ("Tony Pollard", "RB", "25.00%", "15"), ("George Kittle", "TE", "22.30%", "14"),
       ("Ja'Marr Chase", "WR", "35.00%", "33"), ("Tee Higgins", "WR", "27.00%", "25"),
       ("Zay Flowers", "WR", "18.00%", "4"), ("Puka Nacua", "WR", "20.00%", "19")]


def _csv(rows, own_scale=1.0):
    lines = []
    for i, (rank, eid, name, pts, lu) in enumerate(rows):
        o = OWN[i] if i < len(OWN) else None
        right = ""
        if o:
            pct = f"{float(o[2][:-1]) * own_scale:.2f}%"
            right = f",{o[0]},{o[1]},{pct},{o[3]}"
        else:
            right = ",,,,"
        lines.append(f"{rank},{eid},{name},0,{pts},{lu},{right}")
    # remaining ownership rows (no entry columns)
    for o in OWN[len(rows):]:
        pct = f"{float(o[2][:-1]) * own_scale:.2f}%"
        lines.append(f",,,,,,,{o[0]},{o[1]},{pct},{o[3]}")
    return HEAD + "\n".join(lines) + "\n"


# big 20-max contest: 3 top rows, then cb1991 far down
MME = _csv([(1, "e1", "alpha (3/20)", 210.1, L1), (2, "e2", "beta (1/2)", 205.0, L2),
            (3, "e3", "alpha (3/20)", 200.0, L1), (14000, "e4", "cb1991 (2/20)", 140.0, L2)])
SINGLE = _csv([(1, "s1", "gamma", 220.0, L2), (2, "s2", "delta", 190.0, L1)], own_scale=1.5)
SUMMARY = ("contestKey,name,draftGroupId,totalEntries,rowsCaptured,lineupsCaptured,lineupsFailed,"
           "minCashRank,playersCaptured,capturedOwnershipPct,uncapturedOwnershipPct,error\n"
           f"5550001,NFL $500K Play-Action [20 Entry Max],{DG},198176,4,4,0,46301,10,268.9,631.1,\n"
           f"5550002,$2K Takeover,{DG},50412,2,2,0,245,10,403,497,\n")


def setup_module():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    for i, (n, pos, team) in enumerate(PLAYERS):
        c = PlayerCanonical(name=n, position=pos, team=team, player_dk_id=800000 + i)
        db.add(c)
        db.flush()
        db.add(SalaryHistory(player_id=c.id, player_dk_id=c.player_dk_id, draft_group_id=DG,
                             season=2026, week=11, slate_date="2026-11-15",
                             salary=5000 + 100 * i, position=pos, team=team,
                             opponent="OPP", source="backfill"))
    db.commit()
    db.close()


def _pid(db, name):
    return db.query(PlayerCanonical).filter_by(name=name).one().id


def test_helpers():
    assert R.max_entries_from_name("NFL $500K Play-Action [20 Entry Max]") == 20
    assert R.max_entries_from_name("NFL $10K Daily Dollar [Single Entry]") == 1
    assert R.max_entries_from_name("NFL $1 Contest") is None
    assert R.username_of("beng4800 (17/17)") == "beng4800"
    assert R.top_block([1, 2, 2, 4, 13473, 48540]) == 4
    assert R.contest_key_from_filename("contest-standings-197411850.csv") == "197411850"


def test_import_batch_classifies_and_matches():
    db = SessionLocal()
    out = R.import_batch(db, [("pull_summary (1).csv", SUMMARY),
                              ("contest-standings-5550001.csv", MME),
                              ("contest-standings-5550002.csv", SINGLE)], fetch_dk=False)
    assert not out["errors"]
    by = {r["contest_key"]: r for r in out["imported"]}
    mme, single = by["5550001"], by["5550002"]
    assert mme["lf_mme"] and mme["max_entries_source"] == "name"
    assert not single["lf_mme"] and single["large_field"]
    assert mme["unmatched"] == [] and single["unmatched"] == []
    assert mme["top_n"] == 3 and mme["lineups"] == 4

    c = db.query(Contest).filter_by(contest_key="5550001").one()
    assert c.slate_id is None and c.draft_group_id == DG and c.slate_type == "classic"
    # DST nickname resolved through the team
    rows = db.query(ContestLineup).filter_by(contest_id=c.id).order_by(ContestLineup.rank).all()
    assert rows[0].slots[0] == "DST" and rows[0].player_ids[0] == _pid(db, "Buccaneers")
    assert all(rows[0].player_ids)
    assert rows[-1].is_user and not rows[-1].top_block
    # only the user's entry becomes a ContestEntry
    assert [e.dk_entry_id for e in db.query(ContestEntry).filter_by(contest_id=c.id)] == ["e4"]
    # Puka is only in L2 (rank 2 in the top block; the user row is excluded)
    puka = db.query(OwnershipObservation).filter_by(contest_id=c.id, player_name="Puka Nacua").one()
    assert puka.lineup_count == 1
    db.close()


def test_reimport_replaces():
    db = SessionLocal()
    R.import_batch(db, [("pull_summary.csv", SUMMARY), ("contest-standings-5550001.csv", MME)],
                   fetch_dk=False)
    c = db.query(Contest).filter_by(contest_key="5550001").one()
    assert db.query(ContestLineup).filter_by(contest_id=c.id).count() == 4
    assert db.query(Contest).filter_by(contest_key="5550001").count() == 1
    db.close()


def test_player_detail_uses_lf_mme_only():
    db = SessionLocal()
    out = player_detail(_pid(db, "Joe Burrow"), db=db, user=None)
    wk = [w for w in out["weeks"] if w["draft_group_id"] == DG][0]
    assert wk["actual_own"] == 40.1                 # MME, not the 1.5x single-entry value
    assert wk["own_status"] == "observed" and wk["lf_contests"] == 1
    assert {x["contest_key"] for x in wk["contests"]} == {"5550001", "5550002"}
    assert wk["top_rate"] == 100.0
    bench = player_detail(_pid(db, "Benchwarmer Guy"), db=db, user=None)
    bw = [w for w in bench["weeks"] if w["draft_group_id"] == DG][0]
    assert bw["actual_own"] is None and bw["own_status"] == "not_captured"
    db.close()


def test_results_endpoints():
    db = SessionLocal()
    lst = list_results(db=db, user=None)
    assert len(lst["contests"]) == 2 and lst["contests"][0]["week"] == 11
    obs = observations(lf_mme_only=True, db=db, user=None)
    assert obs["n"] == len(OWN)
    row = [r for r in obs["rows"] if r["player"] == "Zay Flowers"][0]
    assert row["salary"] and row["team"] == "BAL" and row["top_rate"] == 100.0
    c = db.query(Contest).filter_by(contest_key="5550001").one()
    lu = lineups(c.id, top_only=True, db=db, user=None)
    assert len(lu["lineups"]) == 3 and lu["lineups"][0]["players"][0]["name"] == "Buccaneers"
    db.close()

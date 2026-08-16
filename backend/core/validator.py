"""Live legality checks for the hand-builder (section 12).

A validator, not the solver: cheap, synchronous, runs on every keystroke.
Shares RosterRules with solver.py so the two cannot diverge. Advisory, not
blocking -- illegal lineups may be saved as drafts.
"""
from __future__ import annotations

from .solver import Player, Position, RosterRules


def validate(
    players: list[Player | None],
    rules: RosterRules,
    no_opposing_dst: bool = True,
) -> list[str]:
    """Return a list of human-readable violations for a (possibly partial)
    slot assignment. Empty list == legal so far."""
    issues: list[str] = []
    filled = [(rules.slots[i], p) for i, p in enumerate(players) if p is not None]

    # slot eligibility
    for slot, p in filled:
        if p.position not in slot.positions:
            issues.append(f"{p.name} is not eligible for {slot.name}")

    # duplicates
    ids = [p.id for _, p in filled]
    if len(ids) != len(set(ids)):
        issues.append("Duplicate player")

    # salary
    salary = sum(p.salary for _, p in filled)
    if salary > rules.salary_cap:
        issues.append(f"Over cap by ${salary - rules.salary_cap:,}")

    # min games -- only meaningful once complete
    if len(filled) == rules.size:
        games = {p.game_id for _, p in filled}
        if len(games) < rules.min_games:
            issues.append(f"Needs players from at least {rules.min_games} games")
        if rules.min_salary and salary < rules.min_salary:
            issues.append(f"Below salary floor ${rules.min_salary:,}")

    if rules.max_per_team is not None:
        counts: dict[str, int] = {}
        for _, p in filled:
            counts[p.team] = counts.get(p.team, 0) + 1
        for team, n in counts.items():
            if n > rules.max_per_team:
                issues.append(f"More than {rules.max_per_team} from {team}")

    if no_opposing_dst:
        dsts = [p for _, p in filled if p.position is Position.DST]
        for dst in dsts:
            for _, p in filled:
                if p.team == dst.opponent and p.position in (Position.QB, Position.RB):
                    issues.append(f"{dst.name} opposes {p.name}")

    return issues

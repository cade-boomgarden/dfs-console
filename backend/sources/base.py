"""Source adapter protocols. One adapter per source, one protocol per role,
so a fallback (e.g. the unwired Sleeper adapter) is a config change, not a
rewrite (section 3b)."""
from __future__ import annotations

from typing import Any, Protocol


class ProjectionSource(Protocol):
    name: str
    def fetch(self, season: int, week: int) -> dict[str, Any]: ...
    def parse(self, payload: dict[str, Any]) -> list[dict[str, Any]]: ...


class SalarySource(Protocol):
    name: str
    def fetch_draftables(self, draft_group_id: int) -> dict[str, Any]: ...


class OddsSource(Protocol):
    name: str
    def fetch_game_lines(self) -> list[dict[str, Any]]: ...

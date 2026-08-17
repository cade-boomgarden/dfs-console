"""Usage shares + dispersion from profiles (build items 12/13, section 14).

Maps a `PlayerProfile` onto the two things downstream models consume:

* `Dispersion` for `variance.py` -- fitted per-position, adjusted by role
  features so a player's *shape* follows his profile, not a position cliff.
* `AllocationShares` for the hierarchical sim (item 14) -- usage shares plus
  the two precisions that govern how they vary:

    - `posterior_n`: evidence behind the share estimate, in denominator
      units. Parameter uncertainty (1f) draws the player's *true* share from
      Beta(mean=share, precision=posterior_n) -- thin history, wide draw.
    - `weekly_phi`: fitted week-to-week precision of the *realised* share
      around the true share. Irreducible game-script variation; does not
      shrink with sample size.

Coefficients are fitted offline (`scripts/fit_allocation.py`) and shipped as
data in `core/data/allocation_coeffs.json` -- the running app never touches
pbp. `AllocationCoeffs.load()` falls back to conservative defaults when the
fitted file is absent, so core stays importable in a bare sandbox.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .profiles import PlayerProfile
from .variance import Dispersion

_DATA_FILE = Path(__file__).parent / "data" / "allocation_coeffs.json"

# Conservative defaults, used only when no fitted file is shipped.
_DEFAULT_DISPERSION: dict[str, dict[str, float]] = {
    "QB": {"att_k": 12.0, "tgt_k": 6.0, "ypc_cv": 0.55, "ypr_cv": 0.65,
           "ypa_cv": 0.30, "cmp_k": 25.0, "td_k": 8.0},
    "RB": {"att_k": 12.0, "tgt_k": 6.0, "ypc_cv": 0.55, "ypr_cv": 0.65,
           "ypa_cv": 0.30, "cmp_k": 25.0, "td_k": 8.0},
    "WR": {"att_k": 12.0, "tgt_k": 6.0, "ypc_cv": 0.55, "ypr_cv": 0.65,
           "ypa_cv": 0.30, "cmp_k": 25.0, "td_k": 8.0},
    "TE": {"att_k": 12.0, "tgt_k": 6.0, "ypc_cv": 0.55, "ypr_cv": 0.65,
           "ypa_cv": 0.30, "cmp_k": 25.0, "td_k": 8.0},
}
_DEFAULT_WEEKLY_PHI: dict[str, dict[str, float]] = {
    "RB": {"carry_share": 12.0, "target_share": 25.0, "gl_carry_share": 3.0},
    "WR": {"target_share": 30.0, "air_yards_share": 20.0, "ez_target_share": 5.0},
    "TE": {"target_share": 30.0, "air_yards_share": 20.0, "ez_target_share": 5.0},
    "QB": {},
}


@dataclass(frozen=True)
class AllocationCoeffs:
    """Fitted coefficients, shipped as data (item 13 output)."""
    dispersion: dict          # position -> Dispersion field values
    weekly_phi: dict          # position -> share name -> Beta precision
    priors: dict              # position -> feature -> prior mean (overrides POSITION_PRIORS)
    meta: dict

    @classmethod
    def load(cls, path: Path | None = None) -> "AllocationCoeffs":
        p = path or _DATA_FILE
        if p.exists():
            blob = json.loads(p.read_text())
            return cls(
                dispersion=blob.get("dispersion", _DEFAULT_DISPERSION),
                weekly_phi=blob.get("weekly_phi", _DEFAULT_WEEKLY_PHI),
                priors=blob.get("priors", {}),
                meta=blob.get("meta", {}),
            )
        return cls(dispersion=_DEFAULT_DISPERSION, weekly_phi=_DEFAULT_WEEKLY_PHI,
                   priors={}, meta={"fitted": False})


def dispersion_for(
    position: str,
    coeffs: AllocationCoeffs,
    profile: PlayerProfile | None = None,
    variance_scale: float = 1.0,
) -> Dispersion:
    """Per-player Dispersion: fitted position base, adjusted by profile.

    Adjustments are deliberately few and monotone -- each one encodes a
    mechanism, not a curve-fit:

    * Committee backs (low carry share) have more overdispersed carry counts
      than bellcows: their workload swings with game script.
    * Thin-target players (low targets_per_dropback) have more overdispersed
      reception counts than high-participation targets.
    * Deep-aDOT receivers have higher per-catch variance (ypr_cv).

    `variance_scale` is the existing per-player override (questionable tag);
    it widens efficiency CVs and count overdispersion together.
    """
    # fitted files carry only the fields relevant to each position (a QB has
    # no fitted tgt_k); everything else falls back to the defaults.
    defaults = _DEFAULT_DISPERSION.get(position, _DEFAULT_DISPERSION["RB"])
    base = {**defaults, **(coeffs.dispersion.get(position) or {})}
    att_k = base["att_k"]
    tgt_k = base["tgt_k"]
    ypc_cv = base["ypc_cv"]
    ypr_cv = base["ypr_cv"]
    ypa_cv = base["ypa_cv"]
    cmp_k = base["cmp_k"]
    td_k = base.get("td_k", 8.0)

    if profile is not None:
        f = profile.features
        if position == "RB":
            # carry_share 0.7 -> ~1.5x k (steadier); 0.2 -> ~0.6x k (swingier)
            cs = f.get("carry_share", 0.35)
            att_k *= 0.45 + 1.5 * cs
        if position in ("RB", "WR", "TE"):
            tpd = f.get("targets_per_dropback", 0.08)
            tgt_k *= 0.55 + 3.5 * tpd
            adot = f.get("rec_adot", 8.0)
            # aDOT 15 -> ~1.2x ypr_cv; aDOT 3 -> ~0.9x
            ypr_cv *= 0.83 + 0.025 * max(adot, 0.0)

    if variance_scale != 1.0:
        s = max(variance_scale, 0.1)
        ypc_cv *= s
        ypr_cv *= s
        ypa_cv *= s
        att_k /= s
        tgt_k /= s
        td_k /= s

    return Dispersion(att_k=att_k, tgt_k=tgt_k, ypc_cv=ypc_cv, ypr_cv=ypr_cv,
                      ypa_cv=ypa_cv, cmp_k=cmp_k, td_k=td_k)


@dataclass(frozen=True)
class AllocationShares:
    """Usage shares + precisions for the hierarchical sim (item 14)."""
    carry_share: float = 0.0
    target_share: float = 0.0
    targets_per_dropback: float = 0.0
    gl_carry_share: float = 0.0
    ez_target_share: float = 0.0
    air_yards_share: float = 0.0
    # precision of the *estimate* (parameter uncertainty, 1f) per share
    posterior_n: dict | None = None
    # fitted week-to-week precision of the *realised* share per share name
    weekly_phi: dict | None = None


def shares_for(profile: PlayerProfile, coeffs: AllocationCoeffs) -> AllocationShares:
    f = profile.features
    opp = profile.opportunities
    phi = coeffs.weekly_phi.get(profile.position, {})

    def post_n(feature: str, k: float) -> float:
        return opp.get(feature, 0.0) + k

    return AllocationShares(
        carry_share=f.get("carry_share", 0.0),
        target_share=f.get("target_share", 0.0),
        targets_per_dropback=f.get("targets_per_dropback", 0.0),
        gl_carry_share=f.get("gl_carry_share", 0.0),
        ez_target_share=f.get("ez_target_share", 0.0),
        air_yards_share=f.get("air_yards_share", 0.0),
        posterior_n={
            "carry_share": post_n("carry_share", 30),
            "target_share": post_n("target_share", 40),
            "gl_carry_share": post_n("gl_carry_share", 8),
            "ez_target_share": post_n("ez_target_share", 5),
        },
        weekly_phi=dict(phi),
    )

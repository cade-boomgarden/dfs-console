"""Player profile system (build item 12, requirements section 14).

Profiles feed two models:

* Variance (`variance.py`) -- per-player dispersion parameters
* Correlation/allocation (`allocation.py`, sim item 14) -- usage shares and
  their variability

Design rules honoured here:

* **Continuous features, discrete labels (14b).** Models consume continuous
  features; archetype labels are computed separately and are display-only.
* **Exponentially weighted (14g).** Features are EW ratios with a ~4-game
  half-life, computed as ratio-of-EW-sums (stabler than mean-of-ratios).
* **Shrinkage by opportunity count (14e).** Empirical Bayes toward a
  position prior, weighted by the feature's own denominator count (carries,
  targets, dropbacks) -- never games played.
* **Cold start (14f).** Projection-derived proxies, draft capital, depth
  chart. Blended through the same shrinkage machinery via pseudo-counts.

Pure core module: dataclasses in, dataclasses out. No I/O, no database, no
HTTP. The offline scripts (`scripts/build_usage.py`, `scripts/fit_*.py`)
compute the raw usage rows from nflverse parquet and fit the coefficients;
this module only defines the math.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Raw observations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageGame:
    """One player-game of raw usage counts, all tier-1 (live) sources.

    Every field is a count or a sum -- features are ratios of EW sums of
    these. `team_*` fields are that player's team totals in the same game so
    share features need no second lookup.
    """
    season: int
    week: int
    team: str = ""
    opponent: str = ""

    # participation
    snaps: float = 0.0
    team_snaps: float = 0.0

    # passing (QB)
    dropbacks: float = 0.0          # player's own dropbacks
    pass_att: float = 0.0
    completions: float = 0.0
    pass_yds: float = 0.0
    pass_tds: float = 0.0
    ints: float = 0.0
    sacks: float = 0.0
    scrambles: float = 0.0
    designed_rush: float = 0.0      # QB designed carries (non-scramble)
    pass_air_yards: float = 0.0     # sum of air yards thrown
    deep_att: float = 0.0           # throws with air_yards >= 20

    # rushing
    carries: float = 0.0
    rush_yds: float = 0.0
    rush_tds: float = 0.0
    gl_carries: float = 0.0         # carries from inside the 5
    team_rb_carries: float = 0.0    # team carries by RBs
    team_gl_carries: float = 0.0    # team carries inside the 5 (all positions)

    # receiving
    targets: float = 0.0
    receptions: float = 0.0
    rec_yds: float = 0.0
    rec_tds: float = 0.0
    rec_air_yards: float = 0.0      # sum of air yards on this player's targets
    deep_targets: float = 0.0       # targets with air_yards >= 20
    ez_targets: float = 0.0         # targets thrown into the end zone
    yac: float = 0.0

    # team context
    team_dropbacks: float = 0.0
    team_targets: float = 0.0
    team_air_yards: float = 0.0
    team_ez_targets: float = 0.0


# --------------------------------------------------------------------------
# Feature registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureSpec:
    """A profile feature: EW(num) / EW(den), shrunk toward a prior with
    pseudo-count `k` expressed in denominator units."""
    name: str
    num: str                        # UsageGame field for the numerator
    den: str                        # UsageGame field for the denominator
    k: float                        # shrinkage pseudo-count (denominator units)
    positions: tuple[str, ...]


FEATURES: tuple[FeatureSpec, ...] = (
    # --- QB ---
    FeatureSpec("rush_att_per_dropback", "qb_rushes", "dropbacks", 60, ("QB",)),
    FeatureSpec("designed_rush_rate", "designed_rush", "dropbacks", 60, ("QB",)),
    FeatureSpec("scramble_rate", "scrambles", "dropbacks", 60, ("QB",)),
    FeatureSpec("adot", "pass_air_yards", "pass_att", 50, ("QB",)),
    FeatureSpec("deep_rate", "deep_att", "pass_att", 50, ("QB",)),
    FeatureSpec("sack_rate", "sacks", "dropbacks", 60, ("QB",)),
    FeatureSpec("int_rate", "ints", "pass_att", 120, ("QB",)),
    FeatureSpec("ypa", "pass_yds", "pass_att", 50, ("QB",)),
    # --- RB ---
    FeatureSpec("carry_share", "carries", "team_rb_carries", 30, ("RB",)),
    FeatureSpec("gl_carry_share", "gl_carries", "team_gl_carries", 8, ("RB",)),
    FeatureSpec("targets_per_dropback", "targets", "team_dropbacks", 60, ("RB", "TE", "WR")),
    FeatureSpec("ypc", "rush_yds", "carries", 40, ("RB", "QB")),
    # --- WR / TE / RB receiving ---
    FeatureSpec("target_share", "targets", "team_targets", 40, ("RB", "WR", "TE")),
    FeatureSpec("air_yards_share", "rec_air_yards", "team_air_yards", 300, ("WR", "TE")),
    FeatureSpec("rec_adot", "rec_air_yards", "targets", 12, ("RB", "WR", "TE")),
    FeatureSpec("deep_target_rate", "deep_targets", "targets", 15, ("WR", "TE")),
    FeatureSpec("ez_target_share", "ez_targets", "team_ez_targets", 5, ("WR", "TE", "RB")),
    FeatureSpec("ypr", "rec_yds", "receptions", 15, ("RB", "WR", "TE")),
    FeatureSpec("yac_per_rec", "yac", "receptions", 15, ("RB", "WR", "TE")),
    # --- participation ---
    FeatureSpec("snap_share", "snaps", "team_snaps", 120, ("QB", "RB", "WR", "TE")),
)

# Derived features computed from base features rather than num/den pairs.
# WOPR = 1.5 * target_share + 0.7 * air_yards_share (14d).
DERIVED = ("wopr",)


def _usage_value(g: UsageGame, name: str) -> float:
    if name == "qb_rushes":                      # designed + scrambles
        return g.designed_rush + g.scrambles
    return float(getattr(g, name))


# --------------------------------------------------------------------------
# Exponential weighting
# --------------------------------------------------------------------------

DEFAULT_HALF_LIFE = 4.0


def ew_weight(games_ago: int, half_life: float = DEFAULT_HALF_LIFE) -> float:
    """Weight for an observation `games_ago` games before the as-of point.
    Most recent game has weight 1."""
    return 0.5 ** (games_ago / half_life)


def ew_sums(
    games: list[UsageGame], name: str, half_life: float = DEFAULT_HALF_LIFE
) -> float:
    """EW sum of one usage field over games ordered oldest -> newest."""
    n = len(games)
    return sum(
        ew_weight(n - 1 - i, half_life) * _usage_value(g, name)
        for i, g in enumerate(games)
    )


# --------------------------------------------------------------------------
# Position priors (defaults; the fitted coefficients file overrides these)
# --------------------------------------------------------------------------

POSITION_PRIORS: dict[str, dict[str, float]] = {
    "QB": {
        "rush_att_per_dropback": 0.10, "designed_rush_rate": 0.04,
        "scramble_rate": 0.06, "adot": 7.8, "deep_rate": 0.11,
        "sack_rate": 0.065, "int_rate": 0.023, "ypa": 7.0,
        "ypc": 5.5, "snap_share": 1.0,
    },
    "RB": {
        "carry_share": 0.35, "gl_carry_share": 0.20,
        "targets_per_dropback": 0.07, "ypc": 4.3, "target_share": 0.08,
        "rec_adot": -0.5, "ez_target_share": 0.04, "ypr": 7.5,
        "yac_per_rec": 8.0, "snap_share": 0.45,
    },
    "WR": {
        "targets_per_dropback": 0.12, "target_share": 0.14,
        "air_yards_share": 0.16, "rec_adot": 10.5, "deep_target_rate": 0.18,
        "ez_target_share": 0.09, "ypr": 12.0, "yac_per_rec": 4.8,
        "snap_share": 0.65,
    },
    "TE": {
        "targets_per_dropback": 0.09, "target_share": 0.11,
        "air_yards_share": 0.10, "rec_adot": 7.0, "deep_target_rate": 0.08,
        "ez_target_share": 0.08, "ypr": 10.0, "yac_per_rec": 5.0,
        "snap_share": 0.60,
    },
}


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


@dataclass
class PlayerProfile:
    """Continuous features + the opportunity counts behind them.

    `features` are shrunk values ready for model consumption.
    `raw` and `opportunities` are kept for the correlation inspector and for
    debugging shrinkage behaviour. `label` is display-only (14b).
    """
    gsis_id: str
    name: str
    position: str
    team: str
    season: int
    week: int                        # as-of week (features use games strictly before it)
    features: dict[str, float] = field(default_factory=dict)
    raw: dict[str, float | None] = field(default_factory=dict)
    opportunities: dict[str, float] = field(default_factory=dict)
    games_observed: int = 0
    label: str = ""


def shrink(raw: float | None, eff_n: float, prior: float, k: float) -> float:
    """Empirical-Bayes shrinkage: (n*raw + k*prior) / (n + k).

    `eff_n` is the EW opportunity count in denominator units. With no
    observations the prior comes back exactly; with heavy usage the raw value
    dominates. Never weighted by games played (14e).
    """
    if raw is None or eff_n <= 0:
        return prior
    return (eff_n * raw + k * prior) / (eff_n + k)


def compute_profile(
    gsis_id: str,
    name: str,
    position: str,
    team: str,
    season: int,
    week: int,
    games: list[UsageGame],
    priors: dict[str, dict[str, float]] | None = None,
    half_life: float = DEFAULT_HALF_LIFE,
    cold_prior: dict[str, float] | None = None,
    cold_weight: float = 0.0,
) -> PlayerProfile:
    """Build a profile from usage history (games ordered oldest -> newest,
    all strictly before the as-of week).

    `cold_prior` + `cold_weight`: cold-start features (14f) blended in as
    `cold_weight` pseudo-observations of each feature's denominator. As real
    opportunities accumulate the cold prior washes out automatically.
    """
    priors = priors or POSITION_PRIORS
    pos_prior = priors.get(position, {})
    prof = PlayerProfile(gsis_id=gsis_id, name=name, position=position,
                         team=team, season=season, week=week,
                         games_observed=len(games))

    for spec in FEATURES:
        if position not in spec.positions:
            continue
        num = ew_sums(games, spec.num, half_life)
        den = ew_sums(games, spec.den, half_life)
        raw = (num / den) if den > 0 else None
        prior = pos_prior.get(spec.name, 0.0)
        # cold-start blending: treat the cold value as `cold_weight` extra
        # denominator units of evidence at the cold value.
        if cold_prior is not None and spec.name in cold_prior and cold_weight > 0:
            prior = cold_prior[spec.name]
            k = cold_weight
        else:
            k = spec.k
        prof.raw[spec.name] = raw
        prof.opportunities[spec.name] = den
        prof.features[spec.name] = shrink(raw, den, prior, k)

    # derived
    if position in ("WR", "TE", "RB"):
        ts = prof.features.get("target_share", 0.0)
        ays = prof.features.get("air_yards_share", 0.0)
        prof.features["wopr"] = 1.5 * ts + 0.7 * ays

    prof.label = archetype_label(position, prof.features)
    return prof


# --------------------------------------------------------------------------
# Cold start (14f)
# --------------------------------------------------------------------------

# League-average per-game team volumes used to turn a projected stat line
# into share proxies. Coarse by design -- these only matter until real
# opportunities accumulate.
LEAGUE_AVG = {
    "team_dropbacks": 36.0,
    "team_targets": 34.0,
    "team_rb_carries": 24.0,
    "team_air_yards": 270.0,
    "team_gl_carries": 1.8,
    "team_ez_targets": 2.4,
    "catch_rate": 0.66,
}

# Draft-capital multiplier on projected usage shares for players with no
# usage history: high picks are given their projection at face value or
# better; late picks and UDFAs are haircut. Strongest cold-start signal (14f).
def draft_capital_factor(overall_pick: int | None) -> float:
    if overall_pick is None:
        return 0.85
    if overall_pick <= 15:
        return 1.10
    if overall_pick <= 32:
        return 1.05
    if overall_pick <= 64:
        return 1.00
    if overall_pick <= 105:
        return 0.95
    return 0.88


def cold_start_features(
    position: str,
    proj: dict[str, float],
    overall_pick: int | None = None,
    depth_rank: int | None = None,
) -> dict[str, float]:
    """Projection-derived feature proxies for a player with no usage history.

    `proj` is a per-game projected stat line (FantasyPros field names:
    pass_att, pass_yds, rush_att, rush_yds, rec_rec/rec, rec_yds, ...).
    Draft capital scales usage shares; depth rank beyond 2 haircuts further.
    """
    g = lambda k, alt=None: float(proj.get(k, proj.get(alt, 0.0) if alt else 0.0) or 0.0)
    f = draft_capital_factor(overall_pick)
    if depth_rank is not None and depth_rank >= 3:
        f *= 0.8

    rec = g("rec_rec", "rec")
    targets = rec / LEAGUE_AVG["catch_rate"] if rec > 0 else 0.0
    rec_yds = g("rec_yds")
    ypr = rec_yds / rec if rec > 0 else 0.0
    # aDOT proxy: yards per reception minus a typical YAC allowance
    adot_proxy = max(ypr - 5.0, 0.5) if rec > 0 else 0.0

    out: dict[str, float] = {}
    if position == "QB":
        pa = max(g("pass_att"), 1e-9)
        dropbacks = pa * 1.07                      # attempts + sacks/scrambles
        out["rush_att_per_dropback"] = g("rush_att") / dropbacks
        out["scramble_rate"] = 0.6 * out["rush_att_per_dropback"]
        out["designed_rush_rate"] = 0.4 * out["rush_att_per_dropback"]
        out["ypa"] = g("pass_yds") / pa
        out["ypc"] = (g("rush_yds") / g("rush_att")) if g("rush_att") > 0 else 5.5
    else:
        out["carry_share"] = min(f * g("rush_att") / LEAGUE_AVG["team_rb_carries"], 0.85)
        out["gl_carry_share"] = out["carry_share"]  # no better signal pre-debut
        out["target_share"] = min(f * targets / LEAGUE_AVG["team_targets"], 0.32)
        out["targets_per_dropback"] = min(f * targets / LEAGUE_AVG["team_dropbacks"], 0.28)
        if rec > 0:
            out["rec_adot"] = adot_proxy
            out["air_yards_share"] = min(
                f * targets * adot_proxy / LEAGUE_AVG["team_air_yards"], 0.40)
            out["ypr"] = ypr
    return out


# --------------------------------------------------------------------------
# Archetype labels -- display only (14b). Never used by models.
# --------------------------------------------------------------------------


def archetype_label(position: str, feats: dict[str, float]) -> str:
    g = lambda k: feats.get(k, 0.0)
    if position == "QB":
        r = g("rush_att_per_dropback")
        if r >= 0.16:
            return "Dual-Threat"
        if r >= 0.09:
            return "Mobile"
        return "Pocket Passer"
    if position == "RB":
        bell = g("carry_share") >= 0.55
        recv = g("targets_per_dropback") >= 0.11
        if bell and recv:
            return "Three-Down Back"
        if bell:
            return "Early-Down Hammer"
        if recv:
            return "Receiving Back"
        if g("gl_carry_share") >= 0.45:
            return "Goal-Line Back"
        return "Committee Back"
    if position == "WR":
        if g("target_share") >= 0.24 and g("air_yards_share") >= 0.30:
            return "Alpha"
        if g("rec_adot") >= 14.0:
            return "Deep Threat"
        if g("rec_adot") <= 8.0 and g("target_share") >= 0.16:
            return "Underneath / Slot"
        if g("ez_target_share") >= 0.20:
            return "Red-Zone Specialist"
        if g("target_share") >= 0.16:
            return "Primary"
        return "Rotational"
    if position == "TE":
        if g("targets_per_dropback") >= 0.14:
            return "Move TE"
        if g("target_share") >= 0.12:
            return "Primary TE"
        return "Inline TE"
    return ""

"""Strategy registry + signal generation.

Every strategy is a data-defined hypothesis with explicit entry rules, sizing,
and as-of data requirements. The SAME evaluate() code path is used for
backtesting (historical prices) and forward paper trading (captured prices),
so a backtested rule is exactly the rule traded in the competition.

No strategy result is ever hard-coded. Signals depend only on information
timestamped at or before the decision time (enforced by the engine).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import util


@dataclass
class Signal:
    strategy_id: str
    game_id: str
    market: str                 # kalshi:winner | ml | total | 1h | prop:*
    selection: str              # which team / over-under
    side: str                   # yes/no/over/under
    price: float                # cents (kalshi) or american odds
    price_format: str
    source: str
    model_prob: float
    market_prob: float
    trigger: str
    game_label: str = ""
    tipoff_utc: str | None = None
    source_ts: str | None = None
    # decision-state extras frozen into the bet row (2026-09-21, prop/1h
    # settlement needs the market identity + strike at decision time):
    strike: float | None = None
    market_ticker: str | None = None
    prop_player: str | None = None

    @property
    def edge(self) -> float:
        return self.model_prob - self.market_prob


STRATEGIES: dict[str, dict] = {}


def register(sid: str, **meta):
    meta.setdefault("history", [])
    STRATEGIES[sid] = meta
    return meta


#: 2026-09-21 model/engine changes that alter what a strategy would have
#: traded. Version history is part of the audit trail: the earlier rule text is
#: never rewritten, and the version recorded on old bet rows stays as it was.
_UPGRADES: dict[str, tuple[str, str]] = {
    "NBA-001": ("1.1.0", "2026-09-21 (a) Elo margin-of-victory multiplier corrected to "
                         "the published FiveThirtyEight form; (b) rolling state restricted "
                         "to the current season and refused when older than 14 days; "
                         "(c) central probability calibration + tier gating. Rule text "
                         "unchanged. Expected-edge note restated from our own measurement: "
                         "B2B costs -2.11 net points (t=-4.24, n=1321 team-games)."),
    "NBA-002": ("1.1.0", "2026-09-21 (a) same-season, freshness-gated rolling state; "
                         "(b) totals priced from an observed ESPN over/under price when "
                         "captured instead of a blind -110; (c) calibration/tier gating. "
                         "Evidence note: this rule's own outcome-only validation is WORSE "
                         "than the naive league-mean baseline (MAE 17.04 vs 16.69) and its "
                         "directional hit rate is 49.2% on 535 firings, so the rule is "
                         "PARKED by its own evidence (tier: failed) pending new evidence."),
    "NBA-003": ("1.1.0", "2026-09-21 Elo margin-of-victory multiplier corrected (the "
                         "previous implementation dropped the rating-gap denominator). "
                         "Rule text unchanged; ratings differ, so the version is bumped."),
    "NBA-004": ("1.2.0", "2026-09-21 central calibration/tier policy applied to sizing "
                         "(v1.1.0 fixed the identically-zero Kelly stake). Signal rule "
                         "unchanged."),
    "NBA-005": ("1.1.0", "2026-09-21 Elo MOV correction + central calibration/tier policy."),
    "NBA-006": ("1.1.0", "2026-09-21 Elo MOV correction, same-season freshness gate, "
                         "central calibration/tier policy."),
    "NBA-007": ("1.1.0", "2026-09-21 Elo MOV correction, same-season freshness gate, "
                         "central calibration/tier policy. Measured note: road-trip "
                         "finales (5+ away games) hit 61.8% vs a 54.9% base rate on 76 "
                         "firings (z=1.22) — promising, underpowered."),
    "NBA-008": ("1.1.0", "2026-09-21 central calibration/tier policy (props are "
                         "forward-only: no free historical prop prices exist)."),
    "NBA-009": ("1.1.0", "2026-09-21 central calibration/tier policy (forward-only)."),
    "NBA-010": ("1.1.0", "2026-09-21 observed-price totals pricing + calibration/tier "
                         "policy; same-season freshness gate."),
    "NBA-011": ("1.1.0", "2026-09-21 observed-price totals pricing + calibration/tier "
                         "policy; same-season freshness gate."),
    "NBA-012": ("1.1.0", "2026-09-21 calibration/tier policy. Note: no ESPN moneyline "
                         "snapshot has been captured yet, so this cross-market rule has "
                         "never been able to fire."),
    "NBA-013": ("1.2.0", "2026-09-21 calibration/tier policy applied to 1H bets (the "
                         "traded probability is now shrunk toward the market ask); rule "
                         "text unchanged."),
    "NBA-014": ("1.1.0", "2026-09-21 calibration/tier policy (forward-only)."),
    "NBA-015": ("1.1.0", "2026-09-21 calibration/tier policy + observed-price totals path."),
    "NBA-016": ("1.1.0", "2026-09-21 calibration/tier policy + observed-price totals path."),
    "NBA-017": ("1.1.0", "2026-09-21 calibration/tier policy + observed-price totals path."),
    "NBA-018": ("1.1.0", "2026-09-21 central probability calibration + tier gating "
                         "applied to every placement path; rule text unchanged. "
                         "Unproven: this rule has recorded no firings, so it trades "
                         "only at the 'forward_only' (weak) stake scale."),
    "NBA-019": ("1.1.0", "2026-09-21 Elo MOV correction + calibration/tier policy."),
    "NBA-020": ("1.1.0", "2026-09-21 Elo MOV correction + calibration/tier policy. "
                         "Evidence note: the rule's own sweep result is strongly NEGATIVE "
                         "(41.0% hit on 1,043 firings vs a 49.9% base rate, z=-5.75), so "
                         "it is PARKED by its own evidence (tier: failed)."),
    "NBA-021": ("1.1.0", "2026-09-21 Elo MOV correction + calibration/tier policy. "
                         "Evidence note: fading 4+ game streaks hit 37.8% on 777 firings "
                         "vs a 49.8% base rate (z=-6.69); the rule is PARKED (tier: "
                         "failed) and NBA-024 trades the measured opposite direction."),
    "NBA-022": ("1.1.0", "2026-09-21 rest-asymmetry adjustment changed from +4.0 to the "
                         "MEASURED +1.82 points (n=289 asymmetric-rest games vs 3,318 "
                         "symmetric, Welch t=1.38 — not statistically significant), plus "
                         "observed-price totals pricing and calibration/tier policy."),
}


def _apply_version_history():
    """Append the previous version + reason to each strategy's history list."""
    for sid, (new_version, note) in _UPGRADES.items():
        m = STRATEGIES.get(sid)
        if not m:
            continue
        old_version = m.get("version", "unknown")
        m.setdefault("history", [])
        m["history"].append({"version": old_version, "superseded_by": new_version,
                             "changed_utc": "2026-09-21", "note": note})
        m["version"] = new_version


# ---------------------------------------------------------------- registry

register("NBA-001", version="1.0.0", name="Rest Edge Raven", username="RestEdgeRaven",
         category="Rest & Scheduling",
         thesis=("Teams on the second night of a back-to-back underperform relative "
                 "to market prices when facing a rested opponent; markets underreact "
                 "to rest differentials."),
         description=("Bets against a team playing its second game in two nights on the "
                      "road against an opponent with 2+ rest days, using game-winner "
                      "contracts when the model edge exceeds threshold."),
         market_types=["kalshi:winner", "ml"],
         data_sources=["espn:scoreboard", "kalshi:markets", "kalshi:candles", "nba:teamgamelogs"],
         entry_rules=["home team rested >= 2 days", "away team on 2nd night of B2B (rest_days==0)",
                      "model_prob - market_prob >= 0.04"],
         exit_rules=["settle at game conclusion via verified final score or Kalshi result"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="seasons with collected Kalshi price history",
         expected_edge="Literature prior: B2B cost ~1.5-3.0 pts; exploiting only when market gap >= 4pp",
         failure_modes=["market already prices B2B", "small sample", "rest policies change by team"],
         data_limitations=["priced evidence exists only for the SBR archive window (Oct-Dec of 2013-14..2022-23, moneylines only; see research.html)", "Kalshi candles cover only the forward window"],
         lookahead_controls=["rest computed only from games strictly before decision date",
                             "price must be timestamped at or before decision"])

register("NBA-002", version="1.0.0", name="Pace Pulse Pete", username="PacePulsePete",
         category="Pace & Totals",
         thesis=("Game totals can be modeled from rolling possession pace and rolling "
                 "offensive/defensive efficiency; the market is slow to update when a "
                 "team's pace or efficiency regime shifts."),
         description=("Computes expected total from last-15-games rolling pace and ORtg/DRtg "
                      "and bets over/under when the model differs from the market line by >= 8 points."),
         market_types=["kalshi:total", "total"],
         data_sources=["nba:teamgamelogs", "espn:scoreboard", "kalshi:markets"],
         entry_rules=["both teams >= 5 prior games with box data", "|model_total - market_total| >= 8"],
         exit_rules=["settle on final score (over/under vs market total line)"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="same as price history availability",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["injury shifts pace", "garbage time inflates totals", "rolling window lag"],
         data_limitations=["team gamelogs lack opponent shooting splits"],
         lookahead_controls=["rolling windows strictly exclude the current game"])

register("NBA-003", version="1.0.0", name="Elo Oracle", username="EloOracle",
         category="Team Ratings / Moneyline",
         thesis=("A margin-of-victory-adjusted Elo model devigged from game results "
                 "finds moneyline mispricing, especially early season and after roster changes."),
         description=("Updates Elo chronologically (K=20, MOV multiplier, season carryover "
                      "regression). Bets game-winner side when Elo probability beats the "
                      "devigged market probability by >= 4 percentage points."),
         market_types=["kalshi:winner", "ml"],
         data_sources=["espn:scoreboard", "nba:teamgamelogs", "kalshi:candles"],
         entry_rules=["edge >= 0.04", "price available at decision time"],
         exit_rules=["settle at game conclusion"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="seasons with collected prices",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["Elo lags roster news", "regression to market efficiency"],
         data_limitations=["no lineup-level ratings yet"],
         lookahead_controls=["Elo updated only with games before decision"])

register("NBA-004", version="1.1.0", name="Line Move Tracker", username="LineMoveTracker",
         category="Market Movement",
         thesis=("Sustained pre-game moves in the game-winner market contain information "
                 "not yet fully incorporated at the decision time (follow, not fade)."),
         description=("Compares the Kalshi winner price ~48h before tipoff with the price "
                      "~2h before tipoff; follows moves of >= 8 cents in the moved direction. "
                      "v1.1.0 (2026-09-21): the v1.0.0 sizing was a Kelly fraction computed "
                      "with model_prob = market_prob, which is mathematically ZERO for every "
                      "price — the strategy could never place a bet (found in adversarial "
                      "review). It now trades a FIXED 1% of bankroll per signal: this is a "
                      "directional-information bet with no probability edge claimed, so "
                      "Kelly sizing (which requires an independent probability estimate) "
                      "does not apply. The bet row stores model_prob = market probability "
                      "at entry and edge = 0, labeled 'directional move-follow'."),
         market_types=["kalshi:winner"],
         data_sources=["kalshi:candles", "kalshi:orderbooks"],
         entry_rules=["|price(T-2h) - price(T-48h)| >= 8 cents", "both prices exist",
                      "fixed 1% stake (directional rule; no probability edge claimed)"],
         exit_rules=["settle at game conclusion"],
         sizing_rules="FIXED 1% of current bankroll (v1.1.0; Kelly with model_prob=market "
                      "price is identically zero — the v1.0.0 rule could never trade)",
         historical_window="same as price history availability",
         expected_edge="no assumed edge; movement is treated as informative ONLY by test result",
         failure_modes=["movement already complete", "noise at low liquidity", "one-sided flow",
                        "fixed stake overbets strong moves and underbets weak ones"],
         data_limitations=["candles are last-trade based; historical book depth unknown"],
         lookahead_controls=["both price points strictly before decision"],
         lineage="NBA-004 v1.0.0 (sizing fix only; signal rule unchanged)")

register("NBA-005", version="1.0.0", name="Injury IQ Ivan", username="InjuryIQIvan",
         category="Injuries",
         thesis=("Late availability news (Out/Doubtful for a top-usage player) moves true "
                 "win probability more than the market's immediate reaction."),
         description=("If, at decision time, a team lists a top-2-by-minutes player as Out or "
                      "Doubtful for today's game, adjust that team's Elo win probability "
                      "downward (starter 2.5 pts, max one adjustment per team) and bet the "
                      "opponent when edge >= 4pp."),
         market_types=["kalshi:winner", "ml"],
         data_sources=["espn:injuries", "nba:playergamelogs", "kalshi:markets"],
         entry_rules=["injury listing published before decision time", "edge >= 0.04"],
         exit_rules=["settle at game conclusion"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="forward-first: no free historical injury archive is available "
                           "(documented limitation)",
         expected_edge="Literature prior: starter absence ~2.0-4.5 pts depending on usage",
         failure_modes=["market reacts faster than our collection cadence",
                        "injury listing lag", "next-man-up variance"],
         data_limitations=["ESPN injury board is not an official league confirmation",
                           "historical backtest impossible without dated injury history"],
         lookahead_controls=["only listings with published_utc <= decision are used"])

register("NBA-006", version="1.0.0", name="Home Court Hana", username="HomeCourtHana",
         category="Home/Away",
         thesis=("Team-specific home/away efficiency splits deviate from the generic "
                 "home advantage the market prices."),
         description=("Bets home-team winner contracts when the home team's rolling home "
                      "net efficiency beats the visitor's rolling road net efficiency by "
                      "a margin that the Elo model converts to >= 4pp edge."),
         market_types=["kalshi:winner", "ml"],
         data_sources=["nba:teamgamelogs", "kalshi:candles"],
         entry_rules=["home rolling home-game net rating - away rolling road-game net rating > 4.0",
                      "edge >= 0.04"],
         exit_rules=["settle at game conclusion"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="same as price history availability",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["splits are noisy at small n", "travel/schedule confounds"],
         data_limitations=["needs >= 8 home games / 8 road games"],
         lookahead_controls=["rolling splits exclude current game"])

register("NBA-007", version="1.0.0", name="Road Warrior Rex", username="RoadWarriorRex",
         category="Travel & Schedule Spots",
         thesis=("Road-trip finales (5+ consecutive road games) combined with a >= 2 time-zone "
                 "return home depress road teams more than markets price."),
         description=("When the AWAY team is on its 5th+ consecutive road game and its last game "
                      "was >= 2 time zones from its home city, bet the home team when edge >= 4pp."),
         market_types=["kalshi:winner", "ml"],
         data_sources=["espn:scoreboard", "kalshi:candles"],
         entry_rules=["away road_trip_len >= 5", "away tz_shift >= 2", "edge >= 0.04"],
         exit_rules=["settle at game conclusion"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="same as price history availability",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["charters erase travel cost", "rare spot -> small sample"],
         data_limitations=["great-circle city distances, not charter flight data"],
         lookahead_controls=["trip length computed from strictly prior games"])

register("NBA-008", version="1.0.0", name="Glass Guru", username="GlassGuru",
         category="Player Props (Rebounds)",
         thesis=("Rebound opportunity (rolling REB chance proxies: minutes, team missed-shot "
                 "volume faced, OREB rates) predicts player rebounds better than market prop lines."),
         description=("Forward-test-first. When a player's rolling 5-game REB/game exceeds a listed "
                      "prop line by >= 1.5 and minutes role is stable, bet over at the listed price. "
                      "Activates only when a verifiable rebound-prop market exists in collected data."),
         market_types=["kalshi:prop:rebounds"],
         data_sources=["nba:playergamelogs", "kalshi:markets"],
         entry_rules=["rolling5 REB - line >= 1.5", "minutes rolling5 >= 20", "market exists"],
         exit_rules=["settle from verified box score / Kalshi result"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="none (no free historical prop prices) - documented",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["role changes", "garbage time", "prop lines already sharp"],
         data_limitations=["requires Kalshi rebound-prop series; auto-activates when discovered"],
         lookahead_controls=["props priced only from snapshots before decision"])

register("NBA-009", version="1.0.0", name="Dime Doc", username="DimeDoc",
         category="Player Props (Assists)",
         thesis=("Assist opportunity (rolling AST + team pace + teammate shooting) predicts "
                 "player assists vs market lines."),
         description=("Forward-test-first mirror of NBA-008 for assists: rolling5 AST vs listed "
                      "prop line, threshold >= 1.2. Activates when an assist-prop market exists."),
         market_types=["kalshi:prop:assists"],
         data_sources=["nba:playergamelogs", "kalshi:markets"],
         entry_rules=["rolling5 AST - line >= 1.2", "minutes rolling5 >= 20", "market exists"],
         exit_rules=["settle from verified box score / Kalshi result"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="none (no free historical prop prices) - documented",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["teammate shooting variance", "lineup changes"],
         data_limitations=["requires prop series discovery"],
         lookahead_controls=["props priced only from snapshots before decision"])

register("NBA-010", version="1.0.0", name="Regime Ranger", username="RegimeRanger",
         category="Three-Point Regression",
         thesis=("Team three-point percentage extremes regress toward season baseline; "
                 "totals markets overweight recent hot/cold shooting."),
         description=("When a team's rolling-10 3P% deviates from its as-of season 3P% by more "
                      "than 3.5 points AND it attempts >= 35 threes per game, bet the game total "
                      "toward regression (hot -> under, cold -> over) when priced."),
         market_types=["kalshi:total", "total"],
         data_sources=["nba:teamgamelogs", "kalshi:markets"],
         entry_rules=["rolling10 3P% - season 3P% > +0.035 and 3PA/game >= 35 -> under",
                      "season 3P% - rolling10 3P% > 0.035 and 3PA/game >= 35 -> over",
                      "edge vs line-implied prob >= 0.03"],
         exit_rules=["settle on final score"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="same as price history availability",
         expected_edge="no assumed edge; regression hypothesis under test",
         failure_modes=["real offensive regime changes", "small samples", "both-teams-hot seasons"],
         data_limitations=["team logs do not include opponent 3P defense"],
         lookahead_controls=["season-to-date stats end strictly before current game"])

register("NBA-011", version="1.0.0", name="Profile Sage", username="ProfileSage",
         category="Shot Profile / Offensive Rebounding",
         thesis=("High combined offensive-rebound-rate matchups shorten possessions but add "
                 "second chances; totals models built only on pace misprice these games."),
         description=("Computes each team's rolling OREB rate (OREB per missed shot). When the "
                      "sum exceeds a league-relative threshold and the pace model disagrees with "
                      "the market line, bet the direction of the pace model with boosted threshold."),
         market_types=["kalshi:total", "total"],
         data_sources=["nba:teamgamelogs", "kalshi:markets"],
         entry_rules=["team OREB rates >= league 60th pct (rolling)", "|model-market| >= 6"],
         exit_rules=["settle on final score"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="same as price history availability",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["lineup size changes", "rebounding is noisy"],
         data_limitations=["no opponent shot-profile splits in team logs"],
         lookahead_controls=["rolling windows exclude current game"])

register("NBA-012", version="1.0.0", name="Market Mirror Mia", username="MarketMirrorMia",
         category="Cross-Market Divergence",
         thesis=("Kalshi game-winner prices and sportsbook moneylines occasionally diverge by "
                 "more than transaction costs; buying the cheap side captures the convergence."),
         description=("Compares devigged ESPN moneyline probability with the Kalshi winner price "
                      "at the same decision time; buys the cheaper side when gap >= 4pp "
                      "(net of Kalshi fees and 1-tick slippage)."),
         market_types=["kalshi:winner"],
         data_sources=["espn:scoreboard", "kalshi:markets", "kalshi:orderbooks"],
         entry_rules=["|kalshi_prob - devigged_ml_prob| >= 0.04 at decision time"],
         exit_rules=["settle at game conclusion"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="forward-first (needs concurrent ESPN+Kalshi snapshots, collected nightly); "
                           "partially backtestable where both exist historically",
         expected_edge="bounded by execution costs; measured net",
         failure_modes=["divergence = real information one venue has", "fee drag"],
         data_limitations=["ESPN odds snapshots only exist from collection start forward"],
         lookahead_controls=["both prices timestamped at decision time"])

register("NBA-013", version="1.1.0", name="Quarter Quest", username="QuarterQuest",
         category="Quarter / Half Markets",
         thesis=("The team likely to lead at halftime is predictable from the full-game "
                 "rating differential; first-half winner contracts (KXNBA1H) may lag the "
                 "full-game price because less attention is paid to them."),
         description=("v1.1.0 (2026-09-21): the KXNBA1H series is confirmed to exist "
                      "(runtime discovery) but had no live events yet, so no rule existed. "
                      "Now: when a game's 1H market has a live orderbook ask, compute the "
                      "model probability that HOME leads at halftime "
                      "(P = normal_cdf(elo_margin / 8.0), 8.0 = prior SD of NBA half "
                      "margins, documented) and buy the side (home YES / away NO) when "
                      "model - market >= 4pp. Settlement: Kalshi's recorded result for "
                      "the 1H market when captured; otherwise the captured in-game "
                      "quarter scores (Q1+Q2 leader) from ESPN scoreboard snapshots; "
                      "when neither exists the bet is marked void with the stake "
                      "returned (no P&L) after 48h — a settlement-data gap is never "
                      "resolved by guessing. No historical 1H prices exist (forward "
                      "only)."),
         market_types=["kalshi:1h"],
         data_sources=["kalshi:series-discovery", "kalshi:orderbooks", "espn:scoreboard (in-game quarters)"],
         entry_rules=["game has a mapped KXNBA1H market with a live ask",
                      "|model_half_prob - market_prob| >= 0.04"],
         exit_rules=["settle from Kalshi 1H result, or captured Q1+Q2 scores, else "
                     "void (stake returned) 48h after the game is final"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="forward only (no free historical 1H prices; KXNBA1H candles "
                           "accumulate from listing forward)",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["1H markets thin/illiquid", "half margin SD prior wrong",
                        "settlement data gaps (quarter snapshots depend on 6h cadence)"],
         data_limitations=["quarter scores only where in-game scoreboard snapshots "
                           "were captured; 1H history starts at first listing"],
         lookahead_controls=["model from strictly prior games; price = observed ask at "
                             "decision; settlement only from observed half data"],
         lineage="NBA-013 v1.0.0 (rule activation; the v1.0.0 placeholder had no executable rule)")

register("NBA-014", version="1.0.0", name="Blowout Blair", username="BlowoutBlair",
         category="Game Script / Garbage Time",
         thesis=("In high-probability blowouts, starter minutes shrink; starter-over props "
                 "are systematically overpriced."),
         description=("Forward-test-first. When Elo margin expectation exceeds 13 points, bet "
                      "UNDER on listed player-prop lines of the favorite's top-minutes players "
                      "if a prop market exists; otherwise records the signal as an observed "
                      "unbettable edge."),
         market_types=["kalshi:prop:points"],
         data_sources=["nba:playergamelogs", "kalshi:markets"],
         entry_rules=["|elo_margin| >= 13", "prop line listed for favorite's top-3 minutes players",
                      "rolling5 line gap >= 1.0 toward under"],
         exit_rules=["settle from verified box score / Kalshi result"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="none (no free historical prop prices) - documented",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["blowouts by bench-heavy leads still run", "coach rotation choices"],
         data_limitations=["requires prop series discovery"],
         lookahead_controls=["props priced only from snapshots before decision"])

register("NBA-015", version="1.0.0", name="DefRtg Lena", username="DefRtgLena",
         category="Defensive Matchup",
         thesis=("When a top-5 ranked defense (by DRtg) hosts a bottom-5 offense (by ORtg), "
                 "the market underprices the spread/total interaction."),
         description=("Computes each team's rolling-15 defensive rating from points allowed per "
                      "possession. When home_DRtg <= league 5th pct AND away_ORtg >= league "
                      "80th pct, bet UNDER on the game total (price assumed -110)."),
         market_types=["kalshi:total", "total"],
         data_sources=["nba:teamgamelogs", "espn:scoreboard", "kalshi:markets"],
         entry_rules=["home_team_rolling games >= 10", "away_team_rolling games >= 10",
                      "home_DRtg <= 110", "away_ORtg >= 115", "model total <= line - 4"],
         exit_rules=["settle on final score"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="same as price history availability",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["rate stats noisy early season", "matchup effect already in price"],
         data_limitations=["DRtg/ORtg are estimated from team gamelogs (no opponent shot splits)"],
         lookahead_controls=["rolling windows strictly exclude the current game"])

register("NBA-016", version="1.0.0", name="FoulTone Fern", username="FoulToneFern",
         category="Foul Rate / Free Throws",
         thesis=("Matchups involving teams with very different foul tendencies (high FTA rate vs "
                 "low FTA rate allowed) move totals more than the market initially prices."),
         description=("Identifies game where one team's rolling FTA/game >= 26 AND opponent's "
                      "rolling opp_FTA/game <= 19. Uses the totals model; bets UNDER the line "
                      "when the model is below the line and the gap exceeds 5 points."),
         market_types=["kalshi:total", "total"],
         data_sources=["nba:teamgamelogs", "espn:scoreboard"],
         entry_rules=["both rolling-10 games >= 5",
                      "(home_fta - away_opp_fta >= 7) OR (away_fta - home_opp_fta >= 7)",
                      "model total <= line - 5"],
         exit_rules=["settle on final score"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="same as price history availability",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["referees swap trends across seasons", "small samples"],
         data_limitations=["FT attempts only - free throw rate approximated"],
         lookahead_controls=["rolling windows strictly exclude the current game"])

register("NBA-017", version="1.0.0", name="PaceMatch Quincy", username="PaceMatchQuincy",
         category="Pace Mismatch",
         thesis=("Large pace mismatches (>= 5 possessions) systematically produce higher "
                 "variance in game totals than the market's implied variance."),
         description=("Computes a pace mismatch index = abs(home_pace - away_pace). When index >= 5 "
                      "and the model total disagrees with the line by >= 5 points, bet the model's "
                      "direction (no clear over/under bias)."),
         market_types=["kalshi:total", "total"],
         data_sources=["nba:teamgamelogs", "espn:scoreboard"],
         entry_rules=["rolling-15 games >= 5 each",
                      "abs(home_pace - away_pace) >= 5",
                      "|model_total - line| >= 5"],
         exit_rules=["settle on final score"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="same as price history availability",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["fast team forced into slow game by coach", "small samples"],
         data_limitations=["no opponent-pace interaction in current rolling formulation"],
         lookahead_controls=["rolling windows strictly exclude the current game"])

register("NBA-018", version="1.0.0", name="Overtone Olive", username="OvertoneOlive",
         category="Overtone Watcher",
         thesis=("Final-score markets may settle differently from regulation markets; a few "
                 "games each season go to overtime, which is included in the 'final' metric "
                 "and which can swing the total outcome by 5-12 points."),
         description=("STATUS: monitored-only. Whenever a game goes to OT (visible from the "
                      "box score's `OT` period in the summary feed), the bet log records the "
                      "outcome vs the strategy's signal and updates a separate OT-incidence "
                      "counter. No OT betting line is currently offered by Kalshi on regular-"
                      "season markets (documented)."),
         market_types=["final-score watcher"],
         data_sources=["espn:summary"],
         entry_rules=["ot_period observed in box score"],
         exit_rules=["increment ot_counter"],
         sizing_rules="no stake (observer strategy; logged for future OT market discovery)",
         historical_window="monitored going forward",
         expected_edge="unknown - no OT market currently available",
         failure_modes=["no executable signal"],
         data_limitations=["no Kalshi OT-specific market",
                            "regulation-only MML/total markets may settle differently"],
         lookahead_controls=["OT detection strictly after game status = final"])

register("NBA-019", version="1.0.0", name="LineupSpot Larry", username="LineupSpotLarry",
         category="Starting Lineup",
         thesis=("Confirmed bench scorers moving into the starting lineup modestly raise team "
                 "early-game scoring rates, which the full-game price has not always absorbed."),
         description=("STATUS: awaiting free starting-lineup data. NBA.com official starting "
                      "lineups are posted ~30 min before tipoff; our collector records them when "
                      "reachable (stats.nba.com is currently blocked from CI). Activates and "
                      "tracks actual vs expected rolling min for the promoted starter."),
         market_types=["1h (KXNBA1H) when available", "first-quarter"],
         data_sources=["nba:starters (status blocked in current env)", "espn:summary"],
         entry_rules=["promoted-starter minutes-rolling >= 22", "edge vs market >= 0.04",
                      "starting-lineup feed verifiable"],
         exit_rules=["settle at quarter / half / game conclusion"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="forward-only until lineup source is verified",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["limited lineup historical archive", "announcement timing"],
         data_limitations=["no free historical starting-lineup archive was found"],
         lookahead_controls=["only lineups published strictly before decision"])


register("NBA-020", version="1.0.0", name="Blowout Bounce", username="BlowoutBounce",
         category="Game Script / Motivation",
         thesis=("Teams that lose by 18+ points slightly overperform in their next game "
                 "(rotation shakeup, intensity reset) more than the market prices from "
                 "ratings alone."),
         description=("When one of today's teams lost its IMMEDIATELY PREVIOUS game by "
                      ">= 18 points, bet that team's winner contract when the Elo model "
                      "plus a +2pp bounce adjustment clears the market by >= 4pp. Uses "
                      "only verified final scores from the previous game (strictly prior "
                      "by construction). Signal-validation backtest: next-game win rate "
                      "after blowout losses vs base rate, per season."),
         market_types=["kalshi:winner", "ml"],
         data_sources=["espn:scoreboard", "kalshi:markets", "kalshi:candles"],
         entry_rules=["team lost previous game by >= 18",
                      "(elo_prob + 0.02) - market_prob >= 0.04"],
         exit_rules=["settle at game conclusion"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="all seasons with verified finals (signal validation); "
                           "market P&L only where prices exist",
         expected_edge="no assumed edge; the +2pp bounce is the hypothesis itself",
         failure_modes=["market already adjusts for blowout losses", "one-off event "
                        "(lockout, mass DNP)", "small sample"],
         data_limitations=["'previous game' = chronological predecessor in the games "
                           "table; no in-between-game events (trade deadline etc.)"],
         lookahead_controls=["previous game must be final before the decision; "
                             "price at or before decision"])

register("NBA-021", version="1.0.0", name="Streak Skeptic", username="StreakSkeptic",
         category="Streak Persistence / Market Overreaction",
         thesis=("The market overweights recent streaks: it over-fades 4+ game losing "
                 "streaks and over-pays for 4+ game winning streaks. Fading the market's "
                 "streak premium is a value bet."),
         description=("Two-sided rule on the game-winner market: if a team is on a 4+ game "
                      "WINNING streak, bet AGAINST it (elo_prob - 0.03 adjustment, market "
                      "assumed to overvalue the hot team); if on a 4+ game LOSING streak, "
                      "bet ON it (elo_prob + 0.03, market assumed to overfade the cold "
                      "team). Bets when adjusted model - market >= 4pp. Streaks are "
                      "computed from the chronological final-score history only. "
                      "Signal-validation backtest: next-game win rate after 4+ streaks, "
                      "split by streak sign, per season."),
         market_types=["kalshi:winner", "ml"],
         data_sources=["espn:scoreboard", "kalshi:markets", "kalshi:candles"],
         entry_rules=["|streak| >= 4 games", "sign-adjusted (elo_prob +/- 0.03) - "
                      "market_prob >= 0.04"],
         exit_rules=["settle at game conclusion"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="all seasons with verified finals (signal validation); "
                           "market P&L only where prices exist",
         expected_edge="no assumed edge; the +/-3pp overreaction is the hypothesis",
         failure_modes=["streaks are (near-)random by construction — the edge, if any, "
                        "lives in the market's reaction, not the team", "small sample",
                        "streak definition disputes (does an OT loss count? yes, final "
                        "score only)"],
         data_limitations=["streak length limited to collected seasons"],
         lookahead_controls=["streak from strictly prior finals; price at or before "
                             "decision"])

register("NBA-022", version="1.0.0", name="Rest Rigidity", username="RestRigidity",
         category="Rest & Scheduling (Totals)",
         thesis=("Mixing a fully rested team (3+ rest days) with a back-to-back team "
                 "raises game totals: the tired team chases the lead with threes and "
                 "plays faster. The market's total line is set from both teams' rolling "
                 "pace and does not carry a rest-asymmetry adjustment."),
         description=("Model-total adjustment: +4 points when (max(team rest days) >= 3 "
                      "AND min(team rest days) <= 1). When an observed total line exists "
                      "(ESPN snapshot or KXNBATOTAL strike) and (model_total + 4) - line "
                      ">= 6, bet OVER at -110 (documented PRICED-ASSUMPTION where the "
                      "line is not an observed market price). Rest days come from the "
                      "strictly-prior schedule history. Signal validation: totals "
                      "distribution in rest-asymmetric games vs symmetric games."),
         market_types=["kalshi:total", "total"],
         data_sources=["espn:scoreboard", "espn:scoreboard-odds", "kalshi:markets"],
         entry_rules=["max rest >= 3 and min rest <= 1", "rest_asymmetry_total_model - "
                      "line >= 6", "observed total line exists"],
         exit_rules=["settle on final score vs the line stored at decision"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="forward-first (lines exist only from collection start); "
                           "signal validation on all seasons",
         expected_edge="no assumed edge; the +4pt rest asymmetry is the hypothesis",
         failure_modes=["charters erase the B2B cost", "coaches slow the game in "
                        "blowouts (garbage time)", "small sample"],
         data_limitations=["rest measured in calendar days, not minutes of travel"],
         lookahead_controls=["rest from strictly prior games; line observed at or "
                             "before decision; -110 fill labeled PRICED-ASSUMPTION"])

# ---------------------------------------------------- new strategies (2026-09-21)
# Both were registered only AFTER their hypotheses were measured on verified
# results (data/research_scan.json). Neither is claimed to be profitable: with
# no free historical price series, the price test can only run forward.

register("NBA-023", version="1.0.0", name="Playoff Grind", username="PlayoffGrindGus",
         category="Season Phase / Totals",
         thesis=("Postseason basketball is lower-scoring than the regular season "
                 "(shorter rotations, half-court possessions, defensive game planning) "
                 "and a model fitted on regular-season form overstates playoff totals."),
         description=("MEASURED (verified results, 3 seasons, 469 games outside the regular-"
                      "season window vs 3,704 inside): average total 219.0 vs 228.4 — a "
                      "9.4-point drop. Rule: in games the schedule labels postseason, take "
                      "the UNDER when the model total minus 9.4 still sits below the "
                      "observed line; price = observed ESPN over/under price when captured, "
                      "else -110 labelled PRICED-ASSUMPTION. Refuses to fire on regular-"
                      "season games and on unlabelled rows."),
         market_types=["total"],
         data_sources=["espn:scoreboard", "nba:teamgamelogs"],
         entry_rules=["game season_type == 'postseason' (observed label, never inferred)",
                      "rolling state fresh (<= 14 days old, same season)",
                      "model_total - 9.4 <= observed_line - 3.0"],
         exit_rules=["settle on verified final score vs the decision-time line"],
         sizing_rules="Fractional Kelly (25%) x tier stake scale, capped at 3% of bankroll",
         historical_window="postseason games present in the database",
         expected_edge="measured -9.4 pts season-phase effect (in-sample); price edge UNKNOWN",
         failure_modes=["the market already prices the playoff slowdown",
                        "playoff pace varies by series style",
                        "in-sample effect may not repeat"],
         data_limitations=["season_type is NULL for games collected before 2026-09-21 "
                           "(never guessed retroactively) so the historical sample is "
                           "window-based, not label-based"],
         lookahead_controls=["rolling state excludes the current game and any other season",
                             "line and price observed at or before the decision timestamp"])

register("NBA-025", version="1.0.0", name="Longshot Fade", username="LongshotLarry",
         category="Academic anomaly / Favorite-longshot bias",
         thesis=("The favorite-longshot bias documented in betting markets "
                 "(favorites underbet, longshots overbet) appears in NBA moneylines; "
                 "fading sides priced +200 or longer captures that bias."),
         description=("When the observed moneyline (SBR archive historically, Kalshi "
                      "winner ask forward) implies the side is a longshot at +200 or "
                      "longer (implied p <= 1/3), bet the OTHER side at the archive's "
                      "own price. This is an academic-anomaly test, not a model-edge "
                      "claim: model_prob is the market probability of the favorite, "
                      "and the hypothesis is that the favorite still wins often enough "
                      "to overcome the vig. Priced on real SBR moneylines in hist_backtest."),
         market_types=["kalshi:winner", "ml"],
         data_sources=["sbr:nba-odds", "kalshi:markets"],
         entry_rules=["one side's implied win probability <= 1/3 (American +200 or longer)",
                      "bet the favorite at the observed price"],
         exit_rules=["settle at game conclusion from verified score / Kalshi result"],
         sizing_rules="Fractional Kelly (25%) x tier stake scale, capped at 3% of bankroll",
         historical_window="SBR archive moneylines 2013-14..2022-23 (Oct-Dec)",
         expected_edge="academic prior (FLB); no assumed NBA-specific edge",
         failure_modes=["NBA moneylines may already be efficient vs horse-racing FLB",
                        "vig on heavy favorites can erase the bias",
                        "small sample of +200 dogs"],
         data_limitations=["forward Kalshi prices are 1-99 cents so a true +200 dog is rare; "
                           "the priced test is the SBR archive"],
         lookahead_controls=["price is the pre-game archive/ask; result unused until settlement"])

register("NBA-026", version="1.0.0", name="Spread Sage", username="SpreadSageSam",
         category="Spreads / Against the spread",
         thesis=("Elo expected margin disagrees with the captured closing spread by "
                 "enough points that covering is more likely than the vig-implied 52.4%."),
         description=("FORWARD-ONLY. When an ESPN or Kalshi spread line is captured "
                      "before tipoff, convert Elo margin to expected cover vs that "
                      "line. Bet ATS when |Elo margin + home_spread| >= 3.5 points. "
                      "No free per-side historical spread PRICE exists (SBR prints "
                      "the line but not -110/-110), so this rule is never given a "
                      "fabricated historical P&L: it paper-trades at observed ESPN "
                      "spread odds when present, else -110 labelled PRICED-ASSUMPTION."),
         market_types=["spread", "kalshi:spread"],
         data_sources=["espn:scoreboard", "kalshi:markets"],
         entry_rules=["observed spread line exists at decision",
                      "|elo_margin + home_spread| >= 3.5"],
         exit_rules=["settle on verified final margin vs the frozen spread"],
         sizing_rules="Fractional Kelly (25%) x tier stake scale, capped at 3% of bankroll",
         historical_window="forward-only (no free historical ATS prices)",
         expected_edge="no assumed edge; hypothesis under test",
         failure_modes=["spreads are the sharpest NBA market", "Elo lags roster news"],
         data_limitations=["SBR archive has spread LINES but not per-side prices; "
                           "ATS P&L is never invented for history"],
         lookahead_controls=["line captured at or before decision; Elo from prior games only"])

register("NBA-027", version="1.0.0", name="Team Total Tess", username="TeamTotalTess",
         category="Team totals",
         thesis=("A team's rolling scoring rate plus opponent rolling points-allowed "
                 "disagrees with the implied team total constructed from "
                 "(game total − home spread) / 2, a no-arbitrage identity."),
         description=("FORWARD-ONLY. Implied home team total = (total_line − home_spread) / 2 "
                      "when BOTH lines are observed (never guessed). Bet the home score "
                      "OVER/UNDER that implied team total when rolling home pts vs "
                      "rolling away pts-allowed differs from the implied line by >= 6. "
                      "This is a team-total market proxy: Kalshi team-total series were "
                      "not confirmed as of 2026-09-22 (UNVERIFIED/UNAVAILABLE). "
                      "Settlement is the verified home score vs the frozen implied line. "
                      "Priced at observed ESPN team-total odds if captured, else -110 labelled."),
         market_types=["team_total"],
         data_sources=["espn:scoreboard"],
         entry_rules=["both total and spread lines observed at decision",
                      "|rolling_home_pts - implied_home_tt| >= 6"],
         exit_rules=["settle verified home_score vs frozen implied team total"],
         sizing_rules="Fractional Kelly (25%) x tier stake scale, capped at 3% of bankroll",
         historical_window="forward-only",
         expected_edge="no assumed edge; identity is exact, mispricing is the hypothesis",
         failure_modes=["implied TT already equals the market team total",
                        "garbage time inflates home scoring"],
         data_limitations=["no confirmed free Kalshi team-total series; public betting % "
                           "UNVERIFIED/UNAVAILABLE (no keyless source found)"],
         lookahead_controls=["both lines observed at decision; rolling windows exclude current game"])

register("NBA-024", version="1.0.0", name="Momentum Witness", username="MomentumWitness",
         category="Streaks (counter-hypothesis to NBA-021)",
         thesis=("Streak information is NOT fully faded: teams on a 4+ game winning streak "
                 "keep winning at a higher rate than the unconditional base rate, and teams "
                 "on a 4+ game losing streak keep losing."),
         description=("MEASURED on verified results: backing the team on a 4+ win streak "
                      "hit 62.2% (fade of it hit 37.8%; z = -6.7 for the fade) and backing "
                      "the OPPONENT of a 4+ game losing-streak team hit 67.6% (backing the "
                      "cold team hit 32.4%, z = -10.2). This strategy trades the measured "
                      "direction: back the streak. It is the explicit counter-hypothesis to "
                      "NBA-021 StreakSkeptic, whose fade rule the same data contradicts."),
         market_types=["kalshi:winner", "ml"],
         data_sources=["espn:scoreboard", "kalshi:markets"],
         entry_rules=["one team carries a 4+ game win streak OR the opponent carries a "
                      "4+ game losing streak (streaks from strictly prior finals, current "
                      "season only)", "fresh state", "model edge >= 0.04 after calibration"],
         exit_rules=["settle at game conclusion from the verified result"],
         sizing_rules="Fractional Kelly (25%) x tier stake scale, capped at 3% of bankroll",
         historical_window="all seasons in the database",
         expected_edge="measured hit-rate lift vs base rate; price edge UNKNOWN",
         failure_modes=["streaks proxy team quality, which the market already prices",
                        "selection effects in the streak sample",
                        "no price-verified backtest is possible from free data"],
         data_limitations=["moneylines are priced from the SBR archive (Oct-Dec of each season); the measured hit rate alone is not a P&L — the priced simulation on research.html is"],
         lookahead_controls=["streaks computed from strictly prior games in the same season",
                             "prices timestamped at or before the decision"])

# ---------------------------------------------------------------------------
# Stated, falsifiable hypothesis per strategy. The user-facing requirement is
# that every strategy declares what it believes and how that belief would be
# refuted; the registry enforces that each registered strategy has one.
# "Refuted by" names the measurement that would (or did) park the strategy.
HYPOTHESES: dict[str, str] = {
    "NBA-001": "H: a team on the second night of a back-to-back loses more often than the market's price implies when the opponent has rested. Refuted by: B2B net-point cost measured at ~0 (ours: -2.11 net pts, t=-4.24, so the effect is real) AND a hit rate at or below the base rate on rule firings (ours: 65.0% vs 54.9%, n=123).",
    "NBA-002": "H: a totals line differs from (rolling pace x rolling efficiency) by more than noise. Refuted by: model MAE worse than the naive league-mean MAE on the same games. Refuted for v1.0/v1.1: MAE 17.04 vs 16.69 baseline and 49.2% directional hits on 535 firings -> parked.",
    "NBA-003": "H: a margin-of-victory Elo converted to a win probability disagrees with the priced probability often enough to be profitable after the vig. Refuted by: hit rate at or below the base rate (ours: 65.4% vs 51.9% on 2,727 firings) or a price-verified backtest showing negative ROI.",
    "NBA-004": "H: a sustained pre-game move in the winner market continues (momentum) instead of mean-reverting. Refuted by: a measured negative hit rate on line-move firings, or by moves that already reversed before we could act (execution lag).",
    "NBA-005": "H: late 'Out/Doubtful' news on a top-usage player moves true win probability more than the market reprices in the minutes after the report. Refuted by: no measurable hit-rate lift on injury firings, or by the injury feed arriving after we cannot act.",
    "NBA-006": "H: team-specific home/away efficiency splits are larger than the generic league home advantage the market charges. Refuted by: hit rate at or below base rate on split firings (no firings recorded yet on the current data).",
    "NBA-007": "H: a team playing the last game of a 5+ game road trip, or returning across 2+ time zones, underperforms its price. Refuted by: hit rate at or below base rate (ours: 61.8% vs 54.9% on 76 firings, z=1.22 - promising but underpowered).",
    "NBA-008": "H: a player's rebound rate is predictable from rolling minutes and team missed-shot volume, and Kalshi's rebound prop line lags that estimate. Refuted by: no forward-tested price edge once prop markets are captured (no free historical prop prices exist, so it cannot be backtested).",
    "NBA-009": "H: assist props are predictable from rolling assists, team pace and teammate shooting, and the Kalshi line lags. Refuted by: forward-tested price edge absent (forward-only; no historical prop prices).",
    "NBA-010": "H: extremes in team 3-point shooting percentage regress to the mean, and totals markets overreact to hot/cold shooting weeks. Refuted by: model total MAE worse than the naive baseline (the current evidence for NBA-022-style totals rules).",
    "NBA-011": "H: high combined offensive-rebound rates raise total possessions and therefore totals; the market under-adjusts. Refuted by: total MAE worse than baseline, or the effect already being inside the priced total.",
    "NBA-012": "H: Kalshi winner prices and sportsbook moneylines diverge by more than the cost of trading both. Refuted by: divergence never exceeding costs on captured snapshots (no ESPN moneyline snapshot captured yet, so it has never fired).",
    "NBA-013": "H: the first-half winner is more predictable than the full-game winner because the full-game line already contains late-game information (fouls, garbage time). Refuted by: 1H hit rate at or below the 1H base rate.",
    "NBA-014": "H: in expected blowouts, starter minutes shrink, so starter-over props are overpriced. Refuted by: no forward-tested edge on prop markets, or by blowout probability being too uncertain to trade.",
    "NBA-015": "H: an elite defense hosting a bottom-tier offense produces a lower total than the market's line. Refuted by: total MAE worse than the naive league-mean baseline.",
    "NBA-016": "H: teams with extreme free-throw-attempt tendencies push totals away from the market line (more shooting fouls stop the clock and add points). Refuted by: total MAE worse than baseline on firings.",
    "NBA-017": "H: large pace mismatches make totals more volatile than the market prices, so the market's own line is beatable in either direction. Refuted by: no directional edge measured on pace-mismatch games.",
    "NBA-018": "H: overtime risk is mispriced in totals: the market prices an average OT rate, but OT probability varies with pace and closeness. Refuted by: no measurable MAE improvement over baseline on firings.",
    "NBA-019": "H: confirmed lineup changes (bench scorer to starter) raise early-game scoring, so first-half and quarter markets lag the news. Refuted by: no dependable free, timestamped lineup feed (our collection has captured zero lineup rows).",
    "NBA-020": "H: teams that lost by 18+ points bounce back against the market in their next game. Refuted by: back-post-blowout-loss firings hitting 41.0% vs a 49.9% base rate (n=1,043, z=-5.75) -> the market over-fades them; parked.",
    "NBA-021": "H: the market overprices streaks, so fading a 4+ game win streak is profitable. Refuted by: fade-4+win-streak firing at 37.8% vs 49.8% base (n=777, z=-6.69) -> the market does NOT over-fade; parked.",
    "NBA-022": "H: mixing a rested team with a back-to-back team raises the total because the tired team's defense drops more than its offense. Refuted by: the measured rest-asymmetry total difference being inside noise (ours: +1.82 pts, t=1.38) or model MAE worse than baseline (MAE 18.06 vs 16.69) -> parked.",
    "NBA-023": "H: postseason games are lower-scoring than regular-season games by more than the market's postseason adjustment. Refuted by: the measured postseason shift being inside noise (ours: -9.40 pts on 469 games) or the under-side hit rate at or below 50% in forward play.",
    "NBA-024": "H: teams on a 4+ game winning streak keep winning at a higher rate than the market's price implies (streaks are not fully faded). Refuted by: hit rate at or below the base rate (ours: 64.5% vs 50.1% on 1,531 firings, +14.4%) or negative forward P&L.",
    "NBA-025": "H: NBA moneylines exhibit favorite-longshot bias — sides priced +200 or longer are overbet. Refuted by: fading those longshots losing money at the archive's own prices.",
    "NBA-026": "H: Elo expected margin disagrees with the captured spread by >= 3.5 points often enough to cover after -110. Refuted by: ATS hit rate <= 52.4% in forward play.",
    "NBA-027": "H: implied team totals from (total - spread)/2 disagree with rolling scoring rates. Refuted by: team-total hit rate <= 52.4% in forward play. Public betting % and coaching effects remain UNVERIFIED/UNAVAILABLE.",
}


def _assert_hypotheses_complete():
    missing = [sid for sid in STRATEGIES if sid not in HYPOTHESES]
    if missing:
        raise AssertionError(
            f"strategy registry incomplete: no stated hypothesis for {missing}")
    for sid, hyp in HYPOTHESES.items():
        if sid in STRATEGIES:
            STRATEGIES[sid]["hypothesis"] = hyp


COMPETITION_START = "2026-09-20"
COMPETITION_END = "2027-09-19"
STARTING_BANKROLL = 1000.0

KELLY_FRACTION = 0.25
MAX_STAKE_PCT = 0.03
MIN_STAKE = 5.0
# NBA-004 (line-move) is a directional rule: it has no independent probability
# estimate, so Kelly is undefined for it (model_prob = market_prob -> f = 0).
# It trades a fixed fraction of bankroll instead (documented on the strategy).
LINE_MOVE_STAKE_PCT = 0.01


# Every strategy is registered by now: attach the stated hypotheses and apply
# the recorded version history (all strategies must be registered first).
_assert_hypotheses_complete()
_apply_version_history()


def stake_for(bankroll: float, model_prob: float, decimal_odds: float) -> float:
    f = util.kelly_fraction(model_prob, decimal_odds, KELLY_FRACTION)
    s = f * bankroll
    s = min(s, bankroll * MAX_STAKE_PCT)
    if s < MIN_STAKE:
        return 0.0
    return round(s, 2)

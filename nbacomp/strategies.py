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
    market: str                 # kalshi:winner | ml | total | prop:*
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

    @property
    def edge(self) -> float:
        return self.model_prob - self.market_prob


STRATEGIES: dict[str, dict] = {}


def register(sid: str, **meta):
    STRATEGIES[sid] = meta
    return meta


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
         data_limitations=["historical prices only where Kalshi candles exist"],
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

register("NBA-004", version="1.0.0", name="Line Move Tracker", username="LineMoveTracker",
         category="Market Movement",
         thesis=("Sustained pre-game moves in the game-winner market contain information "
                 "not yet fully incorporated at the decision time (follow, not fade)."),
         description=("Compares the Kalshi winner price ~48h before tipoff with the price "
                      "~2h before tipoff; follows moves of >= 8 cents in the moved direction."),
         market_types=["kalshi:winner"],
         data_sources=["kalshi:candles"],
         entry_rules=["|price(T-2h) - price(T-48h)| >= 8 cents", "both prices exist"],
         exit_rules=["settle at game conclusion"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="same as price history availability",
         expected_edge="no assumed edge; movement is treated as informative ONLY by test result",
         failure_modes=["movement already complete", "noise at low liquidity", "one-sided flow"],
         data_limitations=["candles are last-trade based; historical book depth unknown"],
         lookahead_controls=["both price points strictly before decision"])

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

register("NBA-013", version="1.0.0", name="Quarter Quest", username="QuarterQuest",
         category="Quarter / Half Markets",
         thesis=("First-quarter scoring environments differ by matchup pace and starting-unit "
                 "minutes; quarter markets may lag full-game signals."),
         description=("STATUS: awaiting verified quarter/half market discovery in collected "
                      "Kalshi data. Rules activate only when a Q1/1H series is confirmed; "
                      "no signals are generated until then."),
         market_types=["kalshi:q1", "kalshi:1h"],
         data_sources=["kalshi:series-discovery"],
         entry_rules=["pending market discovery"],
         exit_rules=["pending"],
         sizing_rules="Fractional Kelly (25%), capped at 3% of bankroll, min $5",
         historical_window="pending",
         expected_edge="unknown - pending data",
         failure_modes=["no such series exists publicly", "illiquidity"],
         data_limitations=["series existence auto-probed nightly"],
         lookahead_controls=["n/a until active"])

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


COMPETITION_START = "2026-09-20"
COMPETITION_END = "2027-09-19"
STARTING_BANKROLL = 1000.0

KELLY_FRACTION = 0.25
MAX_STAKE_PCT = 0.03
MIN_STAKE = 5.0


def stake_for(bankroll: float, model_prob: float, decimal_odds: float) -> float:
    f = util.kelly_fraction(model_prob, decimal_odds, KELLY_FRACTION)
    s = f * bankroll
    s = min(s, bankroll * MAX_STAKE_PCT)
    if s < MIN_STAKE:
        return 0.0
    return round(s, 2)

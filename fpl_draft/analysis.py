from collections import defaultdict
from . import api

POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def players():
    bs = api.bootstrap()
    teams = {t["id"]: t["short_name"] for t in bs["teams"]}
    out = {}
    for e in bs["elements"]:
        out[e["id"]] = dict(
            id=e["id"], name=e["web_name"], pos=POS[e["element_type"]], team_id=e["team"],
            team=teams[e["team"]], pts=e["total_points"], form=float(e["form"]),
            ppg=float(e["points_per_game"]), ep_next=float(e["ep_next"] or 0),
            minutes=e["minutes"], starts=e.get("starts", 0), status=e["status"],
            news=e["news"], xgi=float(e.get("expected_goal_involvements") or 0),
            chance=e["chance_of_playing_next_round"],
        )
    return out


def _fixtures():
    """Full-season fixtures (main FPL API, includes official FDR) — draft API only has a few GWs."""
    return api.fpl_fixtures()


def team_ratings():
    """Rate teams from season player stats (draft API only lists unplayed fixtures).
    att = team xG per game; dfn = goals conceded per game (max over a team's players ~ starting GK)."""
    bs = api.bootstrap()
    games = max(bs["events"]["current"], 1)
    xg, ga = defaultdict(float), defaultdict(int)
    for e in bs["elements"]:
        xg[e["team"]] += float(e.get("expected_goals") or 0)
        ga[e["team"]] = max(ga[e["team"]], e["goals_conceded"])
    return {t["id"]: dict(att=xg[t["id"]] / games, dfn=ga[t["id"]] / games) for t in bs["teams"]}


def upcoming(team_id, gw, k=5):
    """Next k fixtures for a team as (gw, opp_short, home, difficulty).
    Difficulty = opponent's attack (for defenders/GK) blended with its defensive weakness (for attackers); 1-5 scale."""
    bs = api.bootstrap()
    short = {t["id"]: t["short_name"] for t in bs["teams"]}
    rt = team_ratings()
    avg_att = sum(r["att"] for r in rt.values()) / len(rt)
    res = []
    for f in sorted(_fixtures(), key=lambda f: (f["event"] or 99)):
        if f.get("finished") or not f["event"] or f["event"] < gw:
            continue
        if team_id not in (f["team_h"], f["team_a"]):
            continue
        home = f["team_h"] == team_id
        opp = f["team_a"] if home else f["team_h"]
        r = rt.get(opp, dict(att=avg_att, dfn=avg_att))
        # attacker difficulty: tougher if opp concedes few; defender difficulty: tougher if opp scores a lot
        att_diff = max(1, min(5, round(5 - (r["dfn"] - 0.8) * 3.3)))
        def_diff = max(1, min(5, round(1 + (r["att"] - 0.8) * 3.3)))
        shift = -0.5 if home else 0.5
        fdr = f["team_h_difficulty"] if home else f["team_a_difficulty"]  # official FDR for this team
        res.append((f["event"], short[opp], home, round(att_diff + shift, 1), round(def_diff + shift, 1), fdr))
        if len(res) >= k:
            break
    return res

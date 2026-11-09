from . import api, analysis
from .config import *

MIN = {"GK": 1, "DEF": 3, "MID": 2, "FWD": 1}
MAX = {"GK": 1, "DEF": 5, "MID": 5, "FWD": 3}


def next_gw():
    return api.current_gw() + 1


def my_squad_ids():
    """Last finished GW's picks, then apply accepted waivers/trades processed since (picks endpoint lags)."""
    gw = api.current_gw()
    ids = [p["element"] for p in api.entry_picks(MY_ENTRY_ID, gw)["picks"]]
    tx = [t for t in api.transactions(LEAGUE_ID)["transactions"]
          if t["entry"] == MY_ENTRY_ID and t["result"] == "a" and t["event"] > gw]
    for t in sorted(tx, key=lambda t: t["added"]):
        if t["element_out"] in ids:
            ids[ids.index(t["element_out"])] = t["element_in"]
    return ids


def owners():
    """element id -> owner entry_id, or None if truly available. Locked players (status 'l': dropped in the
    last 24h, can't be signed yet) are returned as -1 so callers treat them as not free."""
    return {x["element"]: (-1 if x["status"] == "l" else x["owner"]) for x in api.element_status(LEAGUE_ID)["element_status"]}


def entry_names():
    d = api.league(LEAGUE_ID)
    return {e["entry_id"]: f'{e["entry_name"]} ({e["player_first_name"]})' for e in d["league_entries"] if e["entry_id"]}


def recent(pid, n=4):
    """(avg minutes, avg points per GW) over the last n gameweeks (blank GWs count as 0 minutes)."""
    gw = api.current_gw()
    by = {}
    for h in api.element_history(pid):
        e = by.setdefault(h["event"], [0, 0]); e[0] += h["total_points"]; e[1] += h["minutes"]
    gws = range(max(1, gw - n + 1), gw + 1)
    mins = sum(by.get(g, [0, 0])[1] for g in gws) / len(gws)
    pts = sum(by.get(g, [0, 0])[0] for g in gws) / len(gws)
    return mins, pts


def score(p, k=3):
    """Expected-value proxy for the next few gameweeks (higher = better).
    Blend of recent points/game, form, season ppg; nudged by fixture ease; scaled by availability and
    recent playing time (last 4 GWs) so rotation/bench players are not overrated."""
    gw = next_gw()
    fx = analysis.upcoming(p["team_id"], gw, k)
    if not fx:
        return 0  # blank gameweek
    idx = 3 if p["pos"] in ("GK", "DEF") else 4
    ease = (3 - sum(f[idx] for f in fx) / len(fx)) * 0.5
    mins, rpts = recent(p["id"])
    base = 0.35 * p["form"] + 0.3 * p["ppg"] + 0.35 * rpts + ease
    avail = {"a": 1.0, "d": (p["chance"] if p["chance"] is not None else 50) / 100, "i": 0, "s": 0, "u": 0, "n": 0}.get(p["status"], 0.5)
    playtime = min(mins / 70, 1.0)  # regular starter ~ 70+ avg minutes over last 4 GWs
    return round(max(base, 0) * avail * playtime, 2)


def best_xi(ps):
    """ps: list of player dicts with 'score'. Returns (xi, bench) honoring formation limits."""
    ps = sorted(ps, key=lambda p: -p["score"])
    xi = []
    cnt = {k: 0 for k in MIN}
    for pos in MIN:  # fill minimums first
        for p in [q for q in ps if q["pos"] == pos][: MIN[pos]]:
            xi.append(p); cnt[pos] += 1
    for p in ps:  # then best remaining within max, until 11
        if len(xi) >= 11: break
        if p not in xi and cnt[p["pos"]] < MAX[p["pos"]]:
            xi.append(p); cnt[p["pos"]] += 1
    bench = [p for p in ps if p not in xi]
    return xi, bench

"""Strength-of-schedule-only test (no home/away, no official FDR): does opponent strength add to a player's recent average?
Usage: python3 sos_test.py [first_test_gw] [last_test_gw]  (run WITHOUT -I; needs pandas/sklearn)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd, model as M
from sklearn.linear_model import LinearRegression
from fpl_draft import api

df = M.build(); d = df[df.n_prior >= 2].copy()
# opponent strength by (team, event), using only games BEFORE that event
blend = lambda a, b: 0.5 * a + 0.5 * b
S = df.groupby(["team", "event"]).first()[["gf5", "gf_all", "ga5", "ga_all", "xg_for5", "xg_for_all", "xg_against5", "xg_against_all"]]
S = pd.DataFrame({"att": blend(S.gf5, S.gf_all), "dfn": blend(S.ga5, S.ga_all), "xatt": blend(S.xg_for5, S.xg_for_all), "xdfn": blend(S.xg_against5, S.xg_against_all)})
teams = sorted(df.team.unique()); evs = sorted(df.event.unique())
S = S.reindex(pd.MultiIndex.from_product([teams, evs], names=["team", "event"])).groupby(level=0).ffill()
lg = S.att.groupby(level=1).mean()
# each team's upcoming opponents (fixture list is known in advance)
fx = [f for f in api.fpl_fixtures() if f["event"]]
sched = {}
for f in sorted(fx, key=lambda f: (f["event"], f["id"])):
    sched.setdefault(f["team_h"], []).append((f["event"], f["team_a"])); sched.setdefault(f["team_a"], []).append((f["event"], f["team_h"]))
def n3(team, ev, col):
    opps = [o for e, o in sched[team] if e >= ev][:3]
    v = [S[col].get((o, ev), np.nan) for o in opps]; return np.nanmean(v) if v else np.nan
d["o_att"] = [S.att.get((o, e), np.nan) for o, e in zip(d.opp, d.event)]
d["o_dfn"] = [S.dfn.get((o, e), np.nan) for o, e in zip(d.opp, d.event)]
d["o_xatt"] = [S.xatt.get((o, e), np.nan) for o, e in zip(d.opp, d.event)]
d["o_xdfn"] = [S.xdfn.get((o, e), np.nan) for o, e in zip(d.opp, d.event)]
d["n3_att"] = [n3(t, e, "att") for t, e in zip(d.team, d.event)]
d["n3_dfn"] = [n3(t, e, "dfn") for t, e in zip(d.team, d.event)]
# position-relevant opponent strength: defenders/GKs care about opp attack; attackers about opp defence
att_side = d.pos.isin(["GK", "DEF"])
d["opp_relevant"] = np.where(att_side, d.o_att, -d.o_dfn)          # higher = tougher for this player
d["n3_relevant"] = np.where(att_side, d.n3_att, -d.n3_dfn)
sets = {"1: last-6 avg + recent minutes (no schedule)": ["pts_6", "minutes_6"],
        "2: + this opponent's attack & defence": ["pts_6", "minutes_6", "o_att", "o_dfn", "o_xatt", "o_xdfn"],
        "3: + what this opponent allows to the position": ["pts_6", "minutes_6", "opp_pa"],
        "4: + next-3 opponents' strength": ["pts_6", "minutes_6", "n3_att", "n3_dfn"],
        "5: + all schedule (2+3+4)": ["pts_6", "minutes_6", "o_att", "o_dfn", "o_xatt", "o_xdfn", "opp_pa", "n3_att", "n3_dfn"],
        "6: + one 'tougher for this position' score": ["pts_6", "minutes_6", "opp_relevant", "n3_relevant"]}
a = sys.argv[1:]; first = int(a[0]) if a else 6; last = int(a[1]) if len(a) > 1 else api.current_gw()
out = []
for T in range(first, last + 1):
    tr, te = d[d.event < T].copy(), d[d.event == T].copy()
    for lab, F in sets.items():
        med = tr[F].median(); m = LinearRegression().fit(tr[F].fillna(med), tr.pts); te[lab] = m.predict(te[F].fillna(med))
    te["T"] = T; out.append(te)
r = pd.concat(out); Ts = sorted(r["T"].unique())
print(f"{len(r)} player-games, GW {first}-{last}. Linear models, walk-forward, NO home/away and NO official FDR.\n")
print(f"{'model':50}{'corr':>7}{'MAE':>7}{'RMSE':>7}   MAE by GW " + " ".join(str(t) for t in Ts))
for lab in sets:
    e = (r[lab] - r.pts).abs(); print(f"{lab:50}{np.corrcoef(r[lab], r.pts)[0,1]:>7.3f}{e.mean():>7.3f}{np.sqrt(((r[lab]-r.pts)**2).mean()):>7.3f}   " + " ".join(f"{e[r['T']==t].mean():.2f}" for t in Ts))
print("\nCorrelation by position:")
for pos in ("GK", "DEF", "MID", "FWD"):
    q = r[r.pos == pos]; print(f"  {pos:4} n={len(q):>4} " + "  ".join(f"{l[0]}: {np.corrcoef(q[l], q.pts)[0,1]:.3f}" for l in sets))
# does the model's schedule coefficient make sense?
F = sets["6: + one 'tougher for this position' score"]; tr = d[d.event < last + 1]; m = LinearRegression().fit(tr[F].fillna(tr[F].median()), tr.pts)
print("\nModel 6 coefficients (points per unit):", dict(zip(F, np.round(m.coef_, 3))))
print("  (negative opp_relevant = tougher opponent lowers expected points, as it should)")

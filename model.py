"""ML experiment: predict a player's points in a gameweek from info available BEFORE that gameweek.
Uses EVERY per-gameweek stat in the Draft API (goals, assists, xG/xA, ICT, BPS, saves, tackles, recoveries, defensive contribution...),
team form/xG, opponent form, FDR, home/away, and what opponents allow. Walk-forward evaluation (train on earlier GWs, test on GW T).
Usage: python3 model.py [first_test_gw] [last_test_gw]   (needs numpy/pandas/scikit-learn, so run WITHOUT -I)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from concurrent.futures import ThreadPoolExecutor
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from fpl_draft import analysis, api

# every numeric per-gameweek stat the API returns for a player
STATS = ["minutes", "starts", "goals_scored", "assists", "clean_sheets", "goals_conceded", "own_goals", "penalties_saved",
         "penalties_missed", "yellow_cards", "red_cards", "saves", "bonus", "bps", "influence", "creativity", "threat", "ict_index",
         "expected_goals", "expected_assists", "expected_goal_involvements", "expected_goals_conceded",
         "clearances_blocks_interceptions", "recoveries", "tackles", "defensive_contribution"]

def build():
    bs = api.bootstrap(); ps = analysis.players(); el = {e["id"]: e for e in bs["elements"]}
    pool = [p for p in ps.values() if p["minutes"] > 0]
    with ThreadPoolExecutor(8) as ex:
        hist = list(ex.map(lambda p: (p, api.element_history(p["id"])), pool))
    rows = []
    for p, h in hist:
        e = el[p["id"]]
        for x in h:
            r = dict(pid=p["id"], name=p["name"], pos=p["pos"], team=p["team_id"], event=x["event"], fixture=x["fixture"],
                     opp=x["opponent_team"], home=int("(H)" in x["detail"]), pts=x["total_points"],
                     # long-standing role info (penalty / free-kick / corner order); static, tiny leakage risk
                     pen_order=e.get("penalties_order") or 9, fk_order=e.get("direct_freekicks_order") or 9, corner_order=e.get("corners_and_indirect_freekicks_order") or 9)
            for s in STATS: r[s] = float(x.get(s) or 0)
            rows.append(r)
    df = pd.DataFrame(rows).sort_values(["pid", "event", "fixture"]).reset_index(drop=True)
    # team-level per-fixture: real goals + xG for/against (from player rows) + official FDR
    txg = df.groupby(["fixture", "team"]).expected_goals.sum().rename("xg_for").reset_index()
    fx = api.fpl_fixtures(); t = []
    for f in fx:
        if f["event"] is None: continue
        t.append(dict(event=f["event"], fixture=f["id"], team=f["team_h"], opp=f["team_a"], gf=f["team_h_score"], ga=f["team_a_score"], fdr=f["team_h_difficulty"]))
        t.append(dict(event=f["event"], fixture=f["id"], team=f["team_a"], opp=f["team_h"], gf=f["team_a_score"], ga=f["team_h_score"], fdr=f["team_a_difficulty"]))
    tf = pd.DataFrame(t).merge(txg, on=["fixture", "team"], how="left")
    tf = tf.merge(txg.rename(columns={"team": "opp", "xg_for": "xg_against"}), on=["fixture", "opp"], how="left")
    tf = tf.sort_values(["team", "event", "fixture"]).reset_index(drop=True)
    g = tf.groupby("team")
    for c in ("gf", "ga", "xg_for", "xg_against"):
        tf[c + "5"] = g[c].transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
    for c in ("gf", "ga", "xg_for", "xg_against"):          # whole-season-to-date strength (prior games only)
        tf[c + "_all"] = g[c].transform(lambda s: s.shift(1).expanding().mean())
    tf["fdr_next3"] = g["fdr"].transform(lambda s: s[::-1].rolling(3, min_periods=1).mean()[::-1])   # this + next 2 fixtures' FDR (known in advance)
    ev = tf.groupby("event").gf.mean(); lgp = ev.expanding().mean().shift(1)
    tf["lg"] = tf.event.map(lgp).fillna(1.35)
    cols = ["gf5", "ga5", "xg_for5", "xg_against5", "gf_all", "ga_all", "xg_for_all", "xg_against_all", "fdr_next3"]
    df = df.merge(tf[["fixture", "team", "fdr", "lg"] + cols], on=["fixture", "team"], how="left")
    df = df.merge(tf[["fixture", "team"] + cols].rename(columns={"team": "opp", **{c: "o_" + c for c in cols}}), on=["fixture", "opp"], how="left")
    # ---- strength-of-schedule model: expected goals for/against in THIS fixture from both teams' attack & defence ----
    att = lambda pre: 0.5 * df[pre + "gf5"] + 0.5 * df[pre + "gf_all"]
    dfn = lambda pre: 0.5 * df[pre + "ga5"] + 0.5 * df[pre + "ga_all"]
    xatt = lambda pre: 0.5 * df[pre + "xg_for5"] + 0.5 * df[pre + "xg_for_all"]
    xdfn = lambda pre: 0.5 * df[pre + "xg_against5"] + 0.5 * df[pre + "xg_against_all"]
    hf = np.where(df.home == 1, 1.1, 0.9)
    df["exp_team_goals"] = att("") * dfn("o_") / df.lg * hf
    df["exp_opp_goals"] = att("o_") * dfn("") / df.lg / hf
    df["exp_team_xg"] = xatt("") * xdfn("o_") / df.lg * hf
    df["exp_opp_xg"] = xatt("o_") * xdfn("") / df.lg / hf
    df["p_cs"] = np.exp(-df.exp_opp_goals)
    df["p_cs_xg"] = np.exp(-df.exp_opp_xg)
    df["opp_att_rank_gap"] = att("o_") - dfn("")             # opponent attack vs my defence
    df["my_att_vs_opp_def"] = att("") - dfn("o_")
    # player rolling features from PRIOR games only (every stat, 3- and 6-game windows) + per-game rates
    g = df.groupby("pid"); new = {}
    for c in STATS + ["pts"]:
        for w in (3, 6):
            new[f"{c}_{w}"] = g[c].transform(lambda s: s.shift(1).rolling(w, min_periods=1).mean())
    new["pts_all"] = g["pts"].transform(lambda s: s.shift(1).expanding().mean())
    new["min60_6"] = g["minutes"].transform(lambda s: (s.shift(1) >= 60).rolling(6, min_periods=1).mean())
    new["n_prior"] = g.cumcount()
    df = pd.concat([df, pd.DataFrame(new)], axis=1)
    # what the opponent has allowed to this position recently (players with 60+ min)
    pa = df[df.minutes >= 60].groupby(["opp", "pos", "event"]).pts.mean().reset_index().sort_values("event")
    pa["opp_pa"] = pa.groupby(["opp", "pos"]).pts.transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
    df = df.merge(pa[["opp", "pos", "event", "opp_pa"]], on=["opp", "pos", "event"], how="left")
    for k in ("GK", "DEF", "MID", "FWD"): df["pos_" + k] = (df.pos == k).astype(int)
    # matchup interactions: player's recent attacking/defensive output scaled by how this fixture should go
    df["xgi6_x_teamgoals"] = df.expected_goal_involvements_6 * df.exp_team_goals
    df["goals6_x_teamgoals"] = df.goals_scored_6 * df.exp_team_goals
    df["cs6_x_pcs"] = df.clean_sheets_6 * df.p_cs
    df["xgc6_x_oppgoals"] = df.expected_goals_conceded_6 * df.exp_opp_goals
    df["pts6_x_pcs"] = df.pts_6 * df.p_cs
    df["pts6_x_teamgoals"] = df.pts_6 * df.exp_team_goals
    return df

FEATS = (["home", "fdr", "pen_order", "fk_order", "corner_order", "opp_pa", "pts_all", "min60_6", "n_prior"]
         + ["gf5", "ga5", "xg_for5", "xg_against5", "o_gf5", "o_ga5", "o_xg_for5", "o_xg_against5"]
         + [f"{c}_{w}" for c in STATS + ["pts"] for w in (3, 6)] + ["pos_GK", "pos_DEF", "pos_MID", "pos_FWD"])

PLAYER = (["pen_order", "fk_order", "corner_order", "pts_all", "min60_6", "n_prior"]
          + [f"{c}_{w}" for c in STATS + ["pts"] for w in (3, 6)] + ["pos_GK", "pos_DEF", "pos_MID", "pos_FWD"])
SIMPLE = ["home", "fdr"]
SOS = ["home", "fdr", "fdr_next3", "opp_pa", "gf5", "ga5", "xg_for5", "xg_against5", "o_gf5", "o_ga5", "o_xg_for5", "o_xg_against5",
       "exp_team_goals", "exp_opp_goals", "exp_team_xg", "exp_opp_xg", "p_cs", "p_cs_xg", "opp_att_rank_gap", "my_att_vs_opp_def",
       "xgi6_x_teamgoals", "goals6_x_teamgoals", "cs6_x_pcs", "xgc6_x_oppgoals", "pts6_x_pcs", "pts6_x_teamgoals"]

def models():
    return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=30, l2_regularization=1.0, random_state=0)

def evaluate(df, first, last):
    df = df[df.n_prior >= 2].copy(); res = []
    for T in range(first, last + 1):
        tr, te = df[df.event < T], df[df.event == T]
        if len(tr) < 300 or len(te) < 30: continue
        gb = models().fit(tr[FEATS], tr.pts); med = tr[FEATS].median()
        rg = Ridge(alpha=50).fit(tr[FEATS].fillna(med), tr.pts)
        res.append(te.assign(gb=gb.predict(te[FEATS]), ridge=rg.predict(te[FEATS].fillna(med)), base6=te.pts_6.fillna(0), base_all=te.pts_all.fillna(0), T=T))
    return pd.concat(res)

def report(r):
    def sc(col): return np.corrcoef(r[col], r.pts)[0, 1], (r[col] - r.pts).abs().mean(), np.sqrt(((r[col] - r.pts) ** 2).mean())
    print(f"Test rows: {len(r)} player-games across GW {sorted(r['T'].unique())}   (avg actual pts {r.pts.mean():.2f})")
    print(f"{'model':28}{'corr':>7}{'MAE':>7}{'RMSE':>7}")
    for lab, col in (("Last-6-games average", "base6"), ("Season-to-date average", "base_all"), ("Ridge regression", "ridge"), ("Gradient boosting (ML)", "gb")):
        c, m, s = sc(col); print(f"{lab:28}{c:>7.2f}{m:>7.2f}{s:>7.2f}")
    print("\nBy position (corr with actual):")
    for pos in ("GK", "DEF", "MID", "FWD"):
        q = r[r.pos == pos]; print(f"  {pos:4} n={len(q):>4}  last-6 {np.corrcoef(q.base6,q.pts)[0,1]:.2f}  ML {np.corrcoef(q.gb,q.pts)[0,1]:.2f}")
    print("\nPick test: each GW take the top-N by predictor; avg ACTUAL points of those picks (higher = better):")
    for N in (10, 25, 50):
        out = {lab: np.mean([g.nlargest(N, col).pts.mean() for _, g in r.groupby("T")]) for lab, col in (("last-6", "base6"), ("ML", "gb"))}
        print(f"  top {N:>2}:  last-6 avg {out['last-6']:.2f}   ML {out['ML']:.2f}")
    print("\nWithin-position pick test (top 5 per position per GW, avg actual pts):")
    for pos in ("DEF", "MID", "FWD"):
        o = {lab: np.mean([g[g.pos == pos].nlargest(5, col).pts.mean() for _, g in r.groupby("T")]) for lab, col in (("last-6", "base6"), ("ML", "gb"))}
        print(f"  {pos}: last-6 {o['last-6']:.2f}   ML {o['ML']:.2f}")

def ablation(df, first, last):
    df = df[df.n_prior >= 2].copy(); sets = {"A: player stats only": PLAYER, "B: + home & official FDR": PLAYER + SIMPLE, "C: + full schedule model": PLAYER + SOS}
    out = []
    for T in range(first, last + 1):
        tr, te = df[df.event < T], df[df.event == T]
        if len(tr) < 300 or len(te) < 30: continue
        te = te.assign(T=T)
        for lab, F in sets.items():
            te[lab] = models().fit(tr[F], tr.pts).predict(te[F])
        te["base6"] = te.pts_6.fillna(0); out.append(te)
    r = pd.concat(out); labs = list(sets)
    print(f"{len(r)} player-games, GW {first}-{last}.  Avg actual pts {r.pts.mean():.2f}\n")
    print(f"{'model':30}{'corr':>7}{'MAE':>7}{'RMSE':>7}" + "".join(f"{'MAE GW'+str(t):>11}" for t in sorted(r['T'].unique())))
    for lab in ["base6"] + labs:
        e = (r[lab] - r.pts).abs(); per = "".join(f"{e[r['T']==t].mean():>11.2f}" for t in sorted(r['T'].unique()))
        print(f"{'last-6 average (baseline)' if lab=='base6' else lab:30}{np.corrcoef(r[lab], r.pts)[0,1]:>7.3f}{e.mean():>7.3f}{np.sqrt(((r[lab]-r.pts)**2).mean()):>7.3f}{per}")
    print("\nCorrelation by position:")
    for pos in ("GK", "DEF", "MID", "FWD"):
        q = r[r.pos == pos]; print(f"  {pos:4} n={len(q):>4} " + "  ".join(f"{l[0]}: {np.corrcoef(q[l], q.pts)[0,1]:.3f}" for l in ["base6"] + labs))
    print("\nTop-5-per-position pick test (avg ACTUAL pts of top 5 picks per GW):")
    for pos in ("DEF", "MID", "FWD"):
        print(f"  {pos}: " + "  ".join(f"{l[0]}: {np.mean([g[g.pos==pos].nlargest(5,l).pts.mean() for _,g in r.groupby('T')]):.2f}" for l in ["base6"] + labs))
    print("\nTop-25 overall:   " + "  ".join(f"{l[0]}: {np.mean([g.nlargest(25,l).pts.mean() for _,g in r.groupby('T')]):.2f}" for l in ["base6"] + labs))
    return r, sets

if __name__ == "__main__" and "--sos" in sys.argv:
    sys.argv.remove("--sos"); a = sys.argv[1:]; first = int(a[0]) if a else 6; last = int(a[1]) if len(a) > 1 else api.current_gw()
    df = build(); r, sets = ablation(df, first, last)
    from sklearn.inspection import permutation_importance
    F = sets["C: + full schedule model"]; d = df[df.n_prior >= 2]; tr, te = d[d.event < last], d[d.event == last]
    gb = models().fit(tr[F], tr.pts); pi = permutation_importance(gb, te[F], te.pts, n_repeats=10, random_state=0, scoring="neg_mean_absolute_error")
    print("\nMost useful features in model C:")
    for i in np.argsort(-pi.importances_mean)[:10]: print(f"  {F[i]:28}{pi.importances_mean[i]:+.3f}" + ("   <-- schedule" if F[i] in SOS else ""))
    sys.exit()

if __name__ == "__main__":
    a = sys.argv[1:]; first = int(a[0]) if a else 6; last = int(a[1]) if len(a) > 1 else api.current_gw()
    df = build(); print(f"Dataset: {len(df)} player-games, GW1-{df.event.max()}, {df.pid.nunique()} players, {len(FEATS)} features\n")
    r = evaluate(df, first, last); report(r)
    from sklearn.inspection import permutation_importance
    tr = df[(df.n_prior >= 2) & (df.event < last)]; te = df[(df.n_prior >= 2) & (df.event == last)]
    gb = models().fit(tr[FEATS], tr.pts)
    pi = permutation_importance(gb, te[FEATS], te.pts, n_repeats=8, random_state=0, scoring="neg_mean_absolute_error")
    print("\nMost useful features (permutation importance, last GW; positive = helps):")
    for i in np.argsort(-pi.importances_mean)[:10]: print(f"  {FEATS[i]:30} {pi.importances_mean[i]:+.3f}")

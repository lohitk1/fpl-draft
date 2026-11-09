#!/usr/bin/env python3
"""FPL Draft helper. Usage: ./fpl.py players|fixtures|squad|waivers ..."""
import argparse, sys
from fpl_draft import api, analysis, league as lg


def cmd_players(a):
    ps = [p for p in analysis.players().values() if (not a.pos or p["pos"] == a.pos.upper())]
    ps = [p for p in ps if p["minutes"] > a.min_minutes]
    ps.sort(key=lambda p: p[a.sort], reverse=True)
    print(f"{'Player':16}{'Pos':5}{'Team':5}{'Pts':>5}{'Form':>6}{'PPG':>6}{'EPnext':>7}{'Min':>6}  Status")
    for p in ps[: a.n]:
        print(f"{p['name']:16}{p['pos']:5}{p['team']:5}{p['pts']:>5}{p['form']:>6}{p['ppg']:>6}{p['ep_next']:>7}{p['minutes']:>6}  {p['status']}{' - ' + p['news'] if p['news'] else ''}")


def cmd_fixtures(a):
    gw = api.current_gw() + 1
    bs = api.bootstrap()
    rows = []
    for t in bs["teams"]:
        fx = analysis.upcoming(t["id"], gw, a.k)
        n = max(len(fx), 1)
        rows.append((sum(f[3] for f in fx) / n, sum(f[4] for f in fx) / n, sum(f[5] for f in fx) / n, t["short_name"], fx))
    i = {"att": 0, "def": 1, "fdr": 2}[a.kind]
    rows.sort(key=lambda r: r[i])
    label = {"att": "attackers (my xG-based rating)", "def": "defenders/GK (my xG-based rating)", "fdr": "official FPL FDR"}[a.kind]
    print(f"Next {a.k} fixtures from GW{gw}; lower = easier [{label}]")
    for r in rows:
        print(f"{r[3]:4} {r[i]:4.1f}  " + " ".join(f"{o}({'H' if h else 'A'}{f[5]})" for (_, o, h, *_), f in zip(r[4], r[4])))


def _row(p, extra=""):
    nxt = " ".join(f"{o}({'H' if h else 'A'})" for _, o, h, *_ in analysis.upcoming(p["team_id"], lg.next_gw(), 3)) or "BLANK"
    news = f"  [{p['status']}: {p['news']}]" if p["news"] else ""
    return f"{p['name']:14}{p['pos']:4}{p['team']:4} score {p['score']:5} form {p['form']:4} ppg {p['ppg']:4}  {nxt}{news}{extra}"


def _mine():
    ps = analysis.players()
    out = []
    for i in lg.my_squad_ids():
        p = dict(ps[i]); p["score"] = lg.score(p); out.append(p)
    return out


def cmd_squad(a):
    print(f"Your squad for GW{lg.next_gw()}:")
    for p in sorted(_mine(), key=lambda p: (list(lg.MIN).index(p["pos"]), -p["score"])):
        print(" ", _row(p))


def cmd_lineup(a):
    xi, bench = lg.best_xi(_mine())
    f = {k: sum(1 for p in xi if p["pos"] == k) for k in lg.MIN}
    print(f"Best XI for GW{lg.next_gw()}  ({f['DEF']}-{f['MID']}-{f['FWD']})  [no captains in this league]")
    for p in xi: print(" ", _row(p))
    print("Bench:")
    for p in bench: print(" ", _row(p))


def cmd_waivers(a):
    ps = analysis.players(); own = lg.owners()
    mine = _mine()
    free = []
    for p in ps.values():
        if own.get(p["id"]) is None and p["minutes"] >= a.min_minutes and (not a.pos or p["pos"] == a.pos.upper()):
            q = dict(p); q["score"] = lg.score(q); free.append(q)
    free.sort(key=lambda p: -p["score"])
    print(f"Top free agents (GW{lg.next_gw()}):")
    for p in free[: a.n]:
        worse = [m for m in mine if m["pos"] == p["pos"] and m["score"] < p["score"]]
        drop = min(worse, key=lambda m: m["score"]) if worse else None
        tag = f"  -> swap out {drop['name']} (+{p['score'] - drop['score']:.1f})" if drop else ""
        print(" ", _row(p, tag))
    print("\nYour weakest per position (drop candidates):")
    for pos in lg.MIN:
        w = sorted([m for m in mine if m["pos"] == pos], key=lambda m: m["score"])[:2]
        print("  ", pos, ", ".join(f"{m['name']} {m['score']}" for m in w))


def cmd_trades(a):
    """Compare what you give vs. get: ./fpl.py trades "Salah" "Palmer"  (give names, then --get)"""
    ps = analysis.players()
    def find(name):
        m = [p for p in ps.values() if name.lower() in p["name"].lower()]
        if not m: sys.exit(f"no player matching {name}")
        return max(m, key=lambda p: p["pts"])
    give = [find(n) for n in a.give]; get = [find(n) for n in a.get]
    for p in give + get: p["score"] = lg.score(p)
    print("GIVE:"); [print(" ", _row(p)) for p in give]
    print("GET:");  [print(" ", _row(p)) for p in get]
    d = sum(p["score"] for p in get) - sum(p["score"] for p in give)
    print(f"\nNet score change: {d:+.2f} ->", "good for you" if d > 0.5 else "roughly even" if d > -0.5 else "bad for you")
    print("(Check positions fit your squad: 2 GK / 5 DEF / 5 MID / 3 FWD.)")


def cmd_league(a):
    d = api.league(lg.LEAGUE_ID)
    names = {e["id"]: (e["entry_name"] or "AVERAGE") for e in d["league_entries"]}
    print("Standings:")
    for s in d["standings"]:
        print(f"  {s['rank']:>2}. {names[s['league_entry']]:20} {s['total']:>3} pts  {s['matches_won']}W-{s['matches_lost']}L  PF {s['points_for']}")


def cmd_weekly(a):
    """Everything for the week: standings, squad, lineup, waivers, fixture swings."""
    print(f"===== WEEKLY REPORT: GW{lg.next_gw()} =====\n")
    cmd_league(a); print()
    a.n, a.pos, a.min_minutes = 10, None, 180
    cmd_lineup(a); print()
    cmd_waivers(a); print()
    a.k, a.kind = 4, "att"; cmd_fixtures(a); print()
    a.kind = "def"; cmd_fixtures(a)


def cmd_history(a):
    """Points per gameweek (minutes in brackets). No names = your squad."""
    ps = analysis.players()
    if a.names:
        ids = []
        for n in a.names:
            m = [p for p in ps.values() if n.lower() in p["name"].lower()]
            if not m: sys.exit(f"no player matching {n}")
            ids.append(max(m, key=lambda p: p["pts"])["id"])
    else:
        ids = lg.my_squad_ids()
    gws = range(max(1, api.current_gw() - a.last + 1), api.current_gw() + 1)
    print(f"{'Player':13}{'Pos':4}" + "".join(f"{'GW'+str(g):>8}" for g in gws) + f"{'Total':>7}{'Avg':>6}")
    for i in ids:
        by = {}
        for h in api.element_history(i):
            e = by.setdefault(h["event"], [0, 0]); e[0] += h["total_points"]; e[1] += h["minutes"]
        cells = [by.get(g) for g in gws]
        tot = sum(c[0] for c in cells if c); played = [c for c in cells if c and c[1] > 0]
        avg = f"{sum(c[0] for c in played)/len(played):.1f}" if played else "-"
        row = "".join(f"{(str(c[0])+'('+str(c[1])+')') if c else '-':>8}" for c in cells)
        print(f"{ps[i]['name']:13}{ps[i]['pos']:4}{row}{tot:>7}{avg:>6}")


def main():
    p = argparse.ArgumentParser(); s = p.add_subparsers(dest="cmd", required=True)
    pl = s.add_parser("players"); pl.add_argument("--pos"); pl.add_argument("-n", type=int, default=25)
    pl.add_argument("--sort", default="form", choices=["pts", "form", "ppg", "ep_next", "xgi"]); pl.add_argument("--min-minutes", type=int, default=270)
    pl.set_defaults(f=cmd_players)
    fx = s.add_parser("fixtures"); fx.add_argument("-k", type=int, default=5); fx.add_argument("--kind", choices=["att", "def", "fdr"], default="fdr")
    fx.set_defaults(f=cmd_fixtures)
    s.add_parser("squad").set_defaults(f=cmd_squad)
    s.add_parser("lineup").set_defaults(f=cmd_lineup)
    s.add_parser("league").set_defaults(f=cmd_league)
    w = s.add_parser("waivers"); w.add_argument("--pos"); w.add_argument("-n", type=int, default=12); w.add_argument("--min-minutes", type=int, default=180)
    w.set_defaults(f=cmd_waivers)
    t = s.add_parser("trades"); t.add_argument("--give", nargs="+", required=True); t.add_argument("--get", nargs="+", required=True)
    t.set_defaults(f=cmd_trades)
    s.add_parser("weekly").set_defaults(f=cmd_weekly)
    h = s.add_parser("history"); h.add_argument("names", nargs="*"); h.add_argument("--last", type=int, default=9)
    h.set_defaults(f=cmd_history)
    a = p.parse_args(); a.f(a)


if __name__ == "__main__":
    main()

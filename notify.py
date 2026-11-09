#!/usr/bin/env python3
"""Sleeper-style live alerts for your FPL Draft squad.

  ./notify.py                 # poll every 60s ONLY while matches are live (or waivers/full-time are pending),
                              # otherwise sleep until the next reminder/kickoff
  ./notify.py --once          # single check (good for testing / cron)
  ./notify.py --replay 9      # replay a finished GW to see what alerts look like
  ./notify.py --league        # also alert on other teams' players owned in the league

Delivery (all optional, combine freely):
  * terminal always
  * Windows toast when running under WSL (automatic, disable with --no-toast)
  * phone push via ntfy.sh:  NTFY_TOPIC=some-long-secret-name ./notify.py
    (install the ntfy app, subscribe to that topic. The topic name is the only
    "password", and alert text goes through ntfy's server, so use a random name.)
"""
import argparse, json, os, socket, subprocess, sys, time, urllib.parse, urllib.request
socket.setdefaulttimeout(30)  # never hang forever on a dead connection
from pathlib import Path
from fpl_draft import api, config, league, analysis

_env = Path(__file__).parent / ".env"
if _env.exists():  # NTFY_TOPIC etc. live in a gitignored .env file
    for _l in _env.read_text().splitlines():
        if "=" in _l and not _l.startswith("#"):
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

STATE = Path(__file__).parent / ".cache" / "notify_state.json"
# stat -> (emoji, label, only-if-increase)
EVENTS = {
    "goals_scored": ("⚽", "GOAL"),
    "assists": ("🅰️", "ASSIST"),
    "penalties_saved": ("🧤", "PENALTY SAVED"),
    "penalties_missed": ("❌", "PENALTY MISSED"),
    "own_goals": ("😬", "OWN GOAL"),
    "red_cards": ("🟥", "RED CARD"),
    "yellow_cards": ("🟨", "YELLOW CARD"),
}



def send(title, msg, args):
    print(f"[{time.strftime('%H:%M:%S')}] {title}: {msg}", flush=True)
    if args.replay:
        return
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode(),
                                         headers={"Title": title.encode("utf-8"), "Priority": "high"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print("ntfy failed:", e, file=sys.stderr)
    hook = os.environ.get("DISCORD_WEBHOOK")
    if hook:
        try:
            body = json.dumps({"content": f"**{title}**\n{msg}"}).encode()
            urllib.request.urlopen(urllib.request.Request(hook, data=body, headers={
                "Content-Type": "application/json", "User-Agent": "fpl-notify"}), timeout=10)
        except Exception as e:
            print("discord failed:", e, file=sys.stderr)
    tg, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tg and chat:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n{msg}"}).encode()
            urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tg}/sendMessage", data=data), timeout=10)
        except Exception as e:
            print("telegram failed:", e, file=sys.stderr)
    tok, usr = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
    if tok and usr:
        try:
            data = urllib.parse.urlencode({"token": tok, "user": usr, "title": title, "message": msg}).encode()
            urllib.request.urlopen(urllib.request.Request("https://api.pushover.net/1/messages.json", data=data), timeout=10)
        except Exception as e:
            print("pushover failed:", e, file=sys.stderr)
    if not args.no_toast and os.path.exists("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"):
        ps = ("Add-Type -AssemblyName System.Windows.Forms;$n=New-Object System.Windows.Forms.NotifyIcon;"
              "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
              f"$n.ShowBalloonTip(8000,'{title}','{msg}'.Replace(\"'\",''),'Info');Start-Sleep 9;$n.Dispose()")
        try:
            subprocess.Popen(["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def my_match(gw):
    """(opponent label, opponent entry_id, match dict) for my head-to-head in this GW, or None."""
    d = api.league(config.LEAGUE_ID)
    ents = {e["id"]: e for e in d["league_entries"]}
    for m in d["matches"]:
        if m["event"] != gw:
            continue
        a, b = m["league_entry_1"], m["league_entry_2"]
        if config.MY_LEAGUE_ENTRY in (a, b):
            o = ents[b if a == config.MY_LEAGUE_ENTRY else a]
            if o.get("entry_id"):
                return (o["entry_name"], o["entry_id"], m)
    return None


def starting_xi(entry_id, gw):
    """Players in the starting XI (pick positions 1-11) of a manager for a GW; [] if unavailable."""
    try:
        return [p["element"] for p in api.entry_picks(entry_id, gw)["picks"] if p["position"] <= 11]
    except Exception:
        return []


def watched(args, gw=None):
    mine = set(league.my_squad_ids())
    if not args.league:
        out = {p: "you" for p in mine}
        return out
    own = league.owners()
    names = league.entry_names()
    out = {}
    for pid, o in own.items():
        if o and o != -1:
            out[pid] = "you" if pid in mine else names.get(o, "?")
    return out


def pts_by_stat(e):
    """{stat: (points, value)} summed over this GW's fixtures, from the live point breakdown."""
    out = {}
    for fx in e.get("explain", []):
        for row in fx[0]:
            p, v = out.get(row["stat"], (0, 0))
            out[row["stat"]] = (p + row["points"], v + row["value"])
    return out


LABEL = {
    "minutes": "minutes played", "goals_scored": "GOAL", "assists": "ASSIST", "clean_sheets": "CLEAN SHEET",
    "goals_conceded": "goals conceded", "own_goals": "OWN GOAL", "penalties_saved": "PENALTY SAVE",
    "penalties_missed": "PENALTY MISS", "yellow_cards": "YELLOW CARD", "red_cards": "RED CARD",
    "saves": "saves", "bonus": "BONUS", "defensive_contribution": "DEFENSIVE CONTRIBUTION",
}


def check(gw, args, state, pl, prev_status):
    live = api.get(f"event/{gw}/live", ttl=0)["elements"]
    alerts = []
    for pid, who in watched(args, gw).items():
        e, p = live.get(str(pid)), pl.get(pid)
        if not e or not p:
            continue
        cur = {k: list(v) for k, v in pts_by_stat(e).items()}
        old = state.get(str(pid), {})
        if str(pid) not in state and not args.replay:  # first time we see this player this GW: record silently, never replay history
            state[str(pid)] = {k: list(v) for k, v in pts_by_stat(e).items()}
            continue
        tag = p["name"] + ("" if who == "you" else f" ({who})")
        total = e["stats"]["total_points"]
        for k, (pt, val) in cur.items():
            opt, oval = old.get(k, (0, 0))
            d = pt - opt
            if d == 0:
                continue
            what = LABEL.get(k, k)
            if k == "minutes":
                what = "60+ minutes" if pt >= 2 else "playing"
                if opt == 0 and d > 0 and e["stats"].get("starts", 0) == 0:
                    what = "came off the bench"
            if k == "defensive_contribution":
                what = f"defensive contribution reached ({val})"
            if k == "goals_conceded":
                what = f"conceded ({val} so far)"
            if k == "bonus":
                what = f"bonus ({val})"
            sign = "+" if d > 0 else ""
            emo = "\U0001F7E2" if d > 0 else "\U0001F534"
            alerts.append((f"{emo} {sign}{d}  {tag}", f"{what}. {p['team']} {p['pos']}, now {total} pts"))
        # points that vanished entirely (e.g. clean sheet lost): stat present before, gone now
        for k, (opt, oval) in old.items():
            if k not in cur and opt:
                alerts.append((f"\U0001F534 {-opt}  {tag}", f"{LABEL.get(k, k)} lost, now {total} pts"))
        state[str(pid)] = cur
    for pid in watched(args, gw):
        p = pl.get(pid)
        if not p:
            continue
        c = (p["status"], p["news"])
        if pid in prev_status and prev_status[pid] != c:
            alerts.append((f"\U0001F3E5 NEWS: {p['name']}", p["news"] or f"status {p['status']}"))
        prev_status[pid] = c
    mm = my_match(gw)
    if mm and not args.league:
        tot = lambda ids: sum(live[str(i)]["stats"]["total_points"] for i in ids if str(i) in live)
        me, opp = tot(starting_xi(config.MY_ENTRY_ID, gw)), tot(starting_xi(mm[1], gw))
        diff = me - opp
        line = (f"You're winning {me}-{opp}" if diff > 0 else f"You're losing {me}-{opp}" if diff < 0 else f"Tied {me}-{opp}") + f" vs {mm[0]}"
        alerts = [(t, m + f"\n\U0001F19A {line}") if not t.startswith("\U0001F3E5") else (t, m) for t, m in alerts]
        state["_score"] = [me, opp]
        fkey = f"_final{gw}"
        if mm[2]["finished"] and fkey not in state:
            if "_score" in state and state.get("_seen_live"):
                res = "WIN \U0001F3C6" if me > opp else ("LOSS" if me < opp else "DRAW")
                alerts.append((f"\U0001F3C1 Full time: {res}", f"You {me} - {opp} {mm[0]}"))
            state[fkey] = True
        if not mm[2]["finished"]:
            state["_seen_live"] = True
    return alerts


def _ts(s):
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def last_waiver_event():
    """(event id, waivers_time ts) of the most recent unfinished gameweek whose waiver deadline has passed."""
    now = time.time()
    best = None
    for ev in api.bootstrap()["events"]["data"]:
        if ev.get("finished") or not ev.get("waivers_time"):
            continue
        ts = _ts(ev["waivers_time"])
        if ts <= now and (best is None or ts > best[1]):
            best = (ev["id"], ts)
    return best


def waiver_results(state, pl):
    """One summary message when YOUR waiver claims have been processed (won / lost and who beat you)."""
    rows = api.transactions(config.LEAGUE_ID)["transactions"]
    seen = state.get("_tx")
    if seen is None:  # first run ever: remember history silently
        state["_tx"] = [r["id"] for r in rows]
        return []
    game = api.get("game", ttl=60)
    if game.get("processing_status") != "n":  # still processing; wait so claims arrive as one batch
        return []
    new = [r for r in rows if r["id"] not in seen]
    state["_tx"] = seen + [r["id"] for r in new]
    if game.get("waivers_processed"):  # processing finished: stop the post-deadline waiver polling window
        ev = last_waiver_event()
        if ev and ev[0] not in state.setdefault("_wr", []):
            state["_wr"].append(ev[0])
    mine = sorted([r for r in new if r["kind"] == "w" and r["entry"] == config.MY_ENTRY_ID], key=lambda r: r.get("index") or 0)
    if not mine:
        return []
    names = league.entry_names()
    nm = lambda pid: pl[pid]["name"] if pid in pl else f"#{pid}"
    lines = []
    for r in mine:
        if r["result"] == "a":
            lines.append(f"\u2705 Got {nm(r['element_in'])} (dropped {nm(r['element_out'])})")
        else:
            winner = next((x for x in rows if x["element_in"] == r["element_in"] and x["result"] == "a" and x["kind"] == "w"), None)
            why = (f"taken by {names.get(winner['entry'], 'someone')}" if winner else "player taken") if r["result"] == "di" \
                else f"{nm(r['element_out'])} was already used in an earlier claim"
            lines.append(f"\u274c Lost {nm(r['element_in'])}: {why}")
    won = sum(r["result"] == "a" for r in mine)
    return [(f"\U0001F4CB Waivers done: {won}/{len(mine)} claims won", "\n".join(lines))]


LINEUP_CHECK_LEAD = 3600  # one check this many seconds before the lineup deadline


def injury_flags(pl, event_id):
    """Lines for squad players who are injured/doubtful or have no match in this gameweek."""
    playing = set()
    for f in api.fpl_fixtures():
        if f["event"] == event_id:
            playing |= {f["team_h"], f["team_a"]}
    flags = []
    for pid in league.my_squad_ids():
        p = pl.get(pid)
        if not p:
            continue
        if p["team_id"] not in playing:
            flags.append(f"\U0001F6AB {p['name']} ({p['team']}): NO MATCH this gameweek")
        elif p["status"] != "a" or (p.get("chance") is not None and p["chance"] < 100):
            chance = f", {p['chance']}% chance" if p.get("chance") is not None and "%" not in (p["news"] or "") else ""
            flags.append(f"⚠️ {p['name']} ({p['team']}): {p['news'] or 'status ' + p['status']}{chance}")
    return flags


def lineup_check(state, pl):
    """One message ~1h before the GW deadline: which of your players are injured/doubtful or have no match."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).timestamp()
    sent = state.setdefault("_lc", [])
    for ev in api.bootstrap()["events"]["data"]:
        if ev.get("finished"):
            continue
        ts = datetime.fromisoformat(ev["deadline_time"].replace("Z", "+00:00")).timestamp()
        left = ts - now
        if not (0 < left <= LINEUP_CHECK_LEAD) or ev["id"] in sent:
            return []
        sent.append(ev["id"])
        flags = injury_flags(pl, ev["id"])
        mins = round(left / 60)
        if flags:
            return [(f"\U0001F4CB Lineup check: {len(flags)} issue(s), deadline in {mins} min", "\n".join(flags))]
        return [(f"\u2705 Lineup check: no injury flags or blank games, deadline in {mins} min",
                 "Your whole squad is available. Just set your XI.")]
    return []


KICKOFF_LEAD = 5 * 60  # alert this many seconds before kickoff


def kickoffs(state, pl, args):
    """'Match starting soon' alerts listing your squad players in it. Your XI is private (403), so this lists
    everyone you OWN in the match, with a warning flag for doubtful/injured ones."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).timestamp()
    sent = state.setdefault("_ko", [])
    watch = watched(args)
    by_team = {}
    for pid, who in watch.items():
        p = pl.get(pid)
        if p:
            by_team.setdefault(p["team_id"], []).append((p, who))
    short = {p["team_id"]: p["team"] for p in pl.values()}
    out = []
    for fx in api.fpl_fixtures():
        if fx.get("finished") or not fx.get("kickoff_time") or fx.get("started"):
            continue
        ts = datetime.fromisoformat(fx["kickoff_time"].replace("Z", "+00:00")).timestamp()
        left = ts - now
        key = f"{fx['id']}"
        if not (0 < left <= KICKOFF_LEAD) or key in sent:
            continue
        sent.append(key)
        mine = by_team.get(fx["team_h"], []) + by_team.get(fx["team_a"], [])
        if not mine:
            continue
        xi = set(starting_xi(config.MY_ENTRY_ID, fx["event"])) if not args.league else set()
        fmt = lambda lst: ", ".join(p["name"] + ("" if who == "you" else f" ({who})") + (" ⚠️" if p["status"] != "a" else "")
                                    for p, who in lst)
        ko = datetime.fromtimestamp(ts).strftime("%H:%M")
        if xi:  # lineup is locked/visible (GW deadline has passed)
            start = [(p, w) for p, w in mine if p["id"] in xi]
            bench = [(p, w) for p, w in mine if p["id"] not in xi]
            if not start and not bench:
                continue
            msg = (f"In your 11: {fmt(start)}" if start else "None of your 11") + (f"\nBench: {fmt(bench)}" if bench else "")
        else:  # before the deadline the XI is private, so list everyone you own
            msg = f"You have: {fmt(mine)} (XI not visible until the deadline)"
        out.append((f"\u26bd {short.get(fx['team_h'], '?')} v {short.get(fx['team_a'], '?')} starts {ko}", msg))
    return out


REMIND_BEFORE = {"waivers_time": [(6 * 3600, "6h")], "deadline_time": [(3 * 3600, "3h")]}  # one reminder each


def reminders(state, pl):
    """Waiver + lineup deadline reminders, each with your injury report. Lineup/waivers are private so we can't
    tell if you already did them."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).timestamp()
    sent = state.setdefault("_rem", [])
    out = []
    for ev in api.bootstrap()["events"]["data"]:
        if ev.get("finished"):
            continue
        upcoming = False
        for key, what in (("waivers_time", "WAIVERS"), ("deadline_time", "SET YOUR LINEUP")):
            t = ev.get(key)
            if not t:
                continue
            ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            left = ts - now
            if left <= 0:
                continue
            upcoming = True
            local = datetime.fromtimestamp(ts).strftime("%a %H:%M")
            window = min([(secs, lab) for secs, lab in REMIND_BEFORE[key] if left <= secs], default=None)
            if window:  # only the tightest window we're inside of, once
                lab = window[1]
                rid = f"{ev['id']}-{key}-{lab}"
                if rid not in sent:
                    sent.append(rid)
                    msg = ("Submit your waiver claims (priority order matters)" if key == "waivers_time"
                           else "Set your starting XI/formation, check injuries first")
                    flags = injury_flags(pl, ev["id"])
                    report = "\n".join(flags) if flags else "\u2705 Whole squad available, no blank games"
                    out.append((f"\u23f0 {what} in {lab}: GW{ev['id']}", f"{msg}. Deadline {local} your time\n{report}"))
        if upcoming:
            break  # only the next gameweek that still has a deadline ahead of us
    return out


LIVE_AFTER = 3 * 3600     # keep polling this long after kickoff (match + stoppage + API lag), unless officially finished
SETTLE_POLL = 600         # after the last match: check this often until the full-time result has been sent
SETTLE_MAX = 12 * 3600    # ...but give up after this long
WAIVER_POLL = 300         # after the waiver deadline: check this often until processing is done
WAIVER_MAX = 3 * 3600
CLOUD_EXIT_IF_IDLE_OVER = 45 * 60  # --cloud: if the next event is further away than this, exit
CLOUD_MAX_RUN = 5 * 3600 + 30 * 60  # --cloud: hand over before GitHub's 6h job limit
IDLE_RECHECK = 6 * 3600   # when idle, re-read the schedule at least this often (kickoffs can be moved)


def next_wake(gw, state, live_interval):
    """Decide what to do after this run. Returns (interval, wake_ts, why):
    interval = seconds to sleep then poll again, when we are inside a live window (matches, waiver processing, settling);
    otherwise None and wake_ts = the next moment anything needs to happen (kickoff alert, reminder, ...). No polling in between."""
    now = time.time()
    events = []  # (ts, why) in the future
    poll = None  # (interval, why)
    last_end = 0
    for f in api.fpl_fixtures():
        if not f.get("kickoff_time"):
            continue
        ko = _ts(f["kickoff_time"])
        start, end = ko - KICKOFF_LEAD - 60, ko + LIVE_AFTER
        if f["event"] == gw:
            last_end = max(last_end, end)
        if f.get("finished"):
            continue
        if start <= now <= end:
            poll = (live_interval, "match in progress")
        elif start > now:
            events.append((start, "next kickoff alert"))
    if not poll and last_end and last_end <= now < last_end + SETTLE_MAX and f"_final{gw}" not in state:
        poll = (SETTLE_POLL, "waiting for full-time result")
    for ev in api.bootstrap()["events"]["data"]:
        if ev.get("finished"):
            continue
        for key, leads in (("waivers_time", [6 * 3600]), ("deadline_time", [3 * 3600, LINEUP_CHECK_LEAD])):
            if ev.get(key):
                for lead in leads:
                    if _ts(ev[key]) - lead > now:
                        events.append((_ts(ev[key]) - lead, f"GW{ev['id']} {key.split('_')[0]} reminder"))
        if ev.get("waivers_time"):
            w = _ts(ev["waivers_time"])
            if ev["id"] not in state.get("_wr", []):
                if w + WAIVER_POLL > now:
                    events.append((w + WAIVER_POLL, f"GW{ev['id']} waiver results"))
                elif now <= w + WAIVER_MAX and not poll:
                    poll = (WAIVER_POLL, "waiting for waivers to process")
    if poll:
        return poll[0], None, poll[1]
    events.append((now + IDLE_RECHECK, "schedule recheck"))
    ts, why = min(events)
    return None, ts, why


def idle_until(ts):
    """Sleep in short slices on the wall clock (no network), so a laptop suspend/resume wakes us on time."""
    while time.time() < ts:
        time.sleep(min(30, max(1, ts - time.time())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send a test push and exit")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--replay", type=int, help="replay a finished GW from scratch")
    ap.add_argument("--league", action="store_true")
    ap.add_argument("--no-toast", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--cloud", action="store_true", help="CI mode: exit instead of idling long; a scheduler restarts us")
    args = ap.parse_args()
    started = time.time()
    args.replay_all = args.replay is not None

    if args.test:
        args.replay = None
        send("✅ FPL alerts connected", "If you see this on your phone, push notifications work.", args)
        return
    gw = args.replay or api.current_gw()
    state = {} if args.replay else (json.loads(STATE.read_text()) if STATE.exists() else {})
    baseline = False
    if not args.replay and state.get("_gw") != gw:
        state = {"_gw": gw, "_rem": state.get("_rem", []), "_tx": state.get("_tx"), "_lc": state.get("_lc", []), "_wr": state.get("_wr", [])}
        baseline = True  # first run for this GW: record current points silently, alert only on changes
    prev_status = {}
    while True:
        try:
            if not args.replay:
                cur = api.current_gw()  # bootstrap is disk-cached (10 min), so this is not an extra call every minute
                if cur != gw:  # deadline passed, new gameweek is live: reset and baseline silently
                    gw, state, baseline = cur, {"_gw": cur, "_rem": state.get("_rem", []), "_tx": state.get("_tx"), "_lc": state.get("_lc", []), "_wr": state.get("_wr", [])}, True
            pl = analysis.players()
            for t, m in (reminders(state, pl) + kickoffs(state, pl, args) + waiver_results(state, pl) + lineup_check(state, pl) if not args.replay else []) + check(gw, args, state, pl, prev_status)[: (0 if baseline else None)]:
                send(t, m, args)
            if not args.replay:
                STATE.parent.mkdir(exist_ok=True)
                STATE.write_text(json.dumps(state))
            baseline = False
        except Exception as ex:
            if args.once or args.replay:
                raise
            print(f"[{time.strftime('%H:%M:%S')}] transient error, retrying: {ex}", file=sys.stderr, flush=True)
        if args.once or args.replay:
            break
        try:
            interval, wake, why = next_wake(gw, state, args.interval)
        except Exception as ex:
            print(f"[{time.strftime('%H:%M:%S')}] schedule error, polling in 5 min: {ex}", file=sys.stderr, flush=True)
            interval, wake, why = 300, None, "schedule error"
        if interval:
            time.sleep(interval)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] idle until {time.strftime('%a %H:%M', time.localtime(wake))} ({why})", flush=True)
            if args.cloud and wake - time.time() > CLOUD_EXIT_IF_IDLE_OVER:
                break  # nothing to do soon: end the job (free CI minutes); the next cron run picks it up
            idle_until(wake)
        if args.cloud and time.time() - started > CLOUD_MAX_RUN:
            break  # stay under the 6h job limit; the next scheduled run continues


if __name__ == "__main__":
    main()

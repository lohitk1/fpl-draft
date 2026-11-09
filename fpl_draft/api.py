"""Thin client for the (unauthenticated) FPL Draft API."""
import json, time, urllib.request
from pathlib import Path

BASE = "https://draft.premierleague.com/api"
CACHE = Path(__file__).resolve().parent.parent / ".cache"


def get(path, ttl=600):
    """GET a JSON endpoint with a small on-disk cache (ttl seconds)."""
    CACHE.mkdir(exist_ok=True)
    f = CACHE / (path.strip("/").replace("/", "_") + ".json")
    if f.exists() and time.time() - f.stat().st_mtime < ttl:
        return json.loads(f.read_text())
    req = urllib.request.Request(f"{BASE}/{path.lstrip('/')}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    f.write_text(json.dumps(data))
    return data


def bootstrap():
    return get("bootstrap-static")


def league(league_id):
    return get(f"league/{league_id}/details", ttl=300)


def entry_picks(entry_id, gw):
    return get(f"entry/{entry_id}/event/{gw}", ttl=300)


def transactions(league_id):
    return get(f"draft/league/{league_id}/transactions", ttl=120)


def element_status(league_id):
    """Which players are owned (and by whom) in the league."""
    return get(f"league/{league_id}/element-status", ttl=120)


def trades(league_id):
    return get(f"draft/league/{league_id}/trades", ttl=120)


def current_gw():
    ev = bootstrap()["events"]
    return ev["current"]


def element_history(pid):
    """Per-gameweek stats (points, minutes, goals...) for one player."""
    return get(f"element-summary/{pid}", ttl=3600)["history"]


def fpl_fixtures():
    """Full-season fixtures with official FDR from the MAIN FPL API (the draft API only lists ~3 GWs).
    Team ids are identical between the two APIs."""
    CACHE.mkdir(exist_ok=True)
    f = CACHE / "fpl_fixtures.json"
    if f.exists() and time.time() - f.stat().st_mtime < 3600:
        return json.loads(f.read_text())
    req = urllib.request.Request("https://fantasy.premierleague.com/api/fixtures/", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    f.write_text(json.dumps(data))
    return data

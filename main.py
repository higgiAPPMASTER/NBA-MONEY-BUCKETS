
# NBA Stats API blocks server IPs. ESPN gives schedule + rosters + player game logs free.

import asyncio, pathlib, time, random
import json
import math
import os
import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Any


def _nba_today() -> date:
    """NBA slate date in Eastern time. The server clock is UTC, which rolled
    the slate to tomorrow at 8 PM ET and hid that night's saved run."""
    try:
        from zoneinfo import ZoneInfo as _ZI
        return datetime.now(_ZI("America/New_York")).date()
    except Exception:
        return date.today()

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="Money Buckets")

# ─── Auth ─────────────────────────────────────────────────────────────────────
USERS_RAW = os.environ.get("USERS", "admin:buckets")
USERS: Dict[str, str] = {}
for _pair in USERS_RAW.split(","):
    if ":" in _pair.strip():
        _u, _p = _pair.strip().split(":", 1)
        USERS[_u.strip()] = _p.strip()

SECRET = os.environ.get("SECRET_KEY", "nba-money-buckets-2026")

def make_token(username: str) -> str:
    return hashlib.sha256(f"{username}:{SECRET}".encode()).hexdigest()

def get_user(request: Request) -> Optional[str]:
    from jose import jwt as _jose_jwt
    import os as _os
    _jwt_secret = _os.environ.get("JWT_SECRET", "")
    tok = request.query_params.get("_tok","") or request.cookies.get("__mpa_token","") or request.headers.get("Authorization","").replace("Bearer ","").strip()
    if not tok or len(tok.split(".")) != 3 or not _jwt_secret:
        return None
    try:
        _jose_jwt.decode(tok, _jwt_secret, algorithms=["HS256"])
        return tok
    except Exception:
        return None

_NBA_ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAIL", "higgi117711@gmail.com").split(",") if e.strip()}

def _token_email(token: str) -> str:
    """Return the email (sub) from a hub token, else ''.
    Primary path: verify the signature with JWT_SECRET (secure, used when the hub
    and this app share an identical secret on Render). Fallback: read the
    unverified claims so the owner still resolves as admin on login even if the
    two Render services don't have a matching JWT secret. The only thing this
    unlocks is the admin UI (Run/Force) for the configured ADMIN_EMAIL."""
    from jose import jwt as _jose_jwt
    import os as _os
    if not token or len(token.split(".")) != 3:
        return ""
    _secret = _os.environ.get("JWT_SECRET", "")
    if _secret:
        try:
            payload = _jose_jwt.decode(token, _secret, algorithms=["HS256"])
            return str(payload.get("sub", "")).strip().lower()
        except Exception:
            pass
    try:
        payload = _jose_jwt.get_unverified_claims(token)
        return str(payload.get("sub", "")).strip().lower()
    except Exception:
        return ""

def _is_admin_token(token: str) -> bool:
    return bool(_NBA_ADMIN_EMAILS) and _token_email(token) in _NBA_ADMIN_EMAILS

# ─── Stat Config ──────────────────────────────────────────────────────────────
# ESPN gamelog stats array order:
# [0]=MIN [1]=FG [2]=FG% [3]=3PT [4]=3P% [5]=FT [6]=FT% [7]=REB [8]=AST
# [9]=BLK [10]=STL [11]=PF [12]=TO [13]=PTS
STAT_CONFIG = {
    'PTS':  {'label': 'Points',     'emoji': '🏀', 'idx': 13, 'thresholds': list(range(45, 4, -1))},
    'REB':  {'label': 'Rebounds',   'emoji': '📊', 'idx': 7,  'thresholds': list(range(20, 1, -1))},
    'AST':  {'label': 'Assists',    'emoji': '🎯', 'idx': 8,  'thresholds': list(range(15, 1, -1))},
    'FG3M': {'label': '3-Pointers', 'emoji': '🔥', 'idx': 3,  'thresholds': list(range(8,  0, -1))},
    'PRA':  {'label': 'Pts+Reb+Ast','emoji': '🃏', 'idx': None, 'thresholds': list(range(60, 9, -1))},
    'PTS_REB': {'label': 'Pts+Reb', 'emoji': '💪', 'idx': None, 'thresholds': list(range(55, 6, -1))},
    'PTS_AST': {'label': 'Pts+Ast', 'emoji': '⚡', 'idx': None, 'thresholds': list(range(50, 6, -1))},
    'REB_AST': {'label': 'Reb+Ast', 'emoji': '🔗', 'idx': None, 'thresholds': list(range(30, 3, -1))},
    'BLK':  {'label': 'Blocks',     'emoji': '🛡️', 'idx': 9,  'thresholds': list(range(6, 0, -1))},
    'STL':  {'label': 'Steals',     'emoji': '🧤', 'idx': 10, 'thresholds': list(range(6, 0, -1))},
}

HIT_RATE_MIN  = 0.70
MIN_GAMES     = 2
MIN_MINUTES   = 10.0
ESPN_SEASONS  = [2026, 2025, 2024, 2023, 2022, 2021, 2020]
TOP_N         = 12

ODDS_API_BASE   = "https://api.the-odds-api.com/v4"
HISTORICAL_ODDS_MIN_DATE = "2023-05-03"
# Historical requests are intentionally one-region and standard-market only.
HISTORICAL_ODDS_REGION = "us"
LIVE_ODDS_REGIONS = "us,us2,ca"
# Same sportsbook order as the MLB/NFL apps. Live NBA props use the
# highest-priority book that posts the market; its exact line and both side
# prices stay together (never mixed with another book's line).
_NBA_PRIORITY_BOOKS = ("draftkings", "fanduel", "betmgm", "williamhill_us", "caesars",
                       "betrivers", "ballybet", "bet365", "espnbet",
                       "bet99", "thescore", "fliff", "mybookieag", "betonlineag", "bovada")
_NBA_BOOK_PRIORITY = {b: i for i, b in enumerate(_NBA_PRIORITY_BOOKS)}
# Odds pulled by a run that stopped on ESPN are reused by the automatic retry
# so a slow ESPN response never costs a second Odds API pull.
_NBA_RETRY_ODDS: dict = {}
_NBA_RETRY_ODDS_TTL = 900
HISTORICAL_ODDS_TIMEOUT = 55
HISTORICAL_ODDS_CONCURRENCY = 4
HISTORICAL_REPLAY_SCHEMA = 11
ODDS_MARKET_MAP = {
    "player_points":                    "PTS",
    "player_rebounds":                   "REB",
    "player_assists":                    "AST",
    "player_threes":                     "FG3M",
    "player_points_rebounds_assists":    "PRA",
    "player_points_rebounds":            "PTS_REB",
    "player_points_assists":             "PTS_AST",
    "player_rebounds_assists":           "REB_AST",
    "player_blocks":                     "BLK",
    "player_steals":                     "STL",
}
# The Odds API's documented NBA alternate player-prop market keys.  Keep this
# map separate from ODDS_MARKET_MAP: standard boards and their qualification
# inputs intentionally consume standard lines only.
ODDS_ALTERNATE_MARKET_MAP = {
    "player_points_alternate":                    "PTS",
    "player_rebounds_alternate":                  "REB",
    "player_assists_alternate":                   "AST",
    "player_threes_alternate":                    "FG3M",
    "player_points_rebounds_assists_alternate":   "PRA",
    "player_points_rebounds_alternate":           "PTS_REB",
    "player_points_assists_alternate":            "PTS_AST",
    "player_rebounds_assists_alternate":          "REB_AST",
    "player_blocks_alternate":                    "BLK",
    "player_steals_alternate":                    "STL",
}
MIN_GAMES     = 1
MIN_MINUTES   = 10.0
ESPN_SEASONS  = [2026, 2025, 2024, 2023, 2022, 2021, 2020]   # ESPN uses season END year — 7 seasons for full career H/A history


def _nba_current_espn_season(as_of=None) -> int:
    """ESPN labels an NBA season by its END year (Oct 2026 -> 2027)."""
    d = as_of or _nba_today()
    return d.year + 1 if d.month >= 10 else d.year


# The in-progress season must always be included, or this season's games never
# reach any recent-form or matchup history once the season has started.
if _nba_current_espn_season() not in ESPN_SEASONS:
    ESPN_SEASONS = [_nba_current_espn_season()] + ESPN_SEASONS
TOP_N         = 12

# ─── Cache ────────────────────────────────────────────────────────────────────
_cache: Dict[str, Any] = {}  # kept for compat
# ── File-based Picks Cache ────────────────────────────────────────────────────
import pathlib
_CACHE_DIR = pathlib.Path("/tmp/mpa_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_TTL = 6 * 3600  # 6 hours
_NBA_ALT_WARM_INFLIGHT: Dict[str, asyncio.Task] = {}
_NBA_GAMELOG_CACHE: Dict[tuple, List[Dict]] = {}
# Completed seasons never change, so their parsed logs are also kept on disk:
# a woken/restarted instance reuses them instead of re-downloading ~1 MB per
# player-season. The in-progress season stays memory-only with a short TTL.
_NBA_GAMELOG_DIR = _CACHE_DIR / "nba_gamelogs"
_NBA_GAMELOG_DIR.mkdir(parents=True, exist_ok=True)
_NBA_GAMELOG_CUR_TTL = 600
_NBA_GAMELOG_CUR_TS: Dict[tuple, float] = {}
_NBA_LOG_FETCH_TASKS: Dict[str, asyncio.Task] = {}
_NBA_HIST_INFLIGHT: Dict[str, asyncio.Task] = {}
_NBA_PLAYER_IDENTITY_CACHE: Dict[str, dict] = {}

# ── Bet Log ───────────────────────────────────────────────────────────────────
import threading as _nba_th
import uuid as _nba_uuid

# ── Supabase Track Record helpers ─────────────────────────────────────────────
import os as _nba_os
_NBA_SB_URL   = (_nba_os.getenv("SUPABASE_URL") or "").rstrip("/")
_NBA_SB_KEY   = _nba_os.getenv("SUPABASE_SERVICE_KEY") or ""
_NBA_SB_TBL   = "mpa_track_ledger"
_NBA_TRK_APP  = "nba"
_NBA_TRK_STAKE= 20.0
_NBA_TRK_TOP  = 10
_NBA_SNAP_CAT = "__picks__"
_NBA_TRK_LOCK = _nba_th.Lock()

def _nba_season_type(event, scoreboard=None):
    """Read explicit ESPN season metadata; never infer it from the date."""
    event = event if isinstance(event, dict) else {}
    scoreboard = scoreboard if isinstance(scoreboard, dict) else {}
    sources = [event.get("season"), event.get("seasonType"), scoreboard.get("season")]
    for league in scoreboard.get("leagues") or []:
        if isinstance(league, dict):
            sources.append(league.get("season"))
    for source in sources:
        value = source.get("type") if isinstance(source, dict) else source
        if isinstance(value, dict):
            value = value.get("id")
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value) if int(value) in (1, 2, 3) else None
        except (TypeError, ValueError):
            continue
    return None


def _nba_official_board(result):
    games = result.get("games") or [] if isinstance(result, dict) else []
    return bool(games) and all(
        isinstance(game, dict) and game.get("season_type") in (2, 3)
        for game in games)


def _nba_apply_season_scope(result):
    """Annotate display boards without changing any selected play."""
    out = dict(result)
    games = out.get("games") or []
    types = [g.get("season_type") for g in games if isinstance(g, dict)]
    out["preseason"] = 1 in types
    out["seasonScopeVerified"] = bool(games) and len(types) == len(games) and all(
        value in (1, 2, 3) for value in types)
    out["officialCaptureAllowed"] = _nba_official_board(out)
    return out


async def _nba_refresh_season_scope(date_str, result):
    """Give old cached boards verified labels, without regenerating picks."""
    out = dict(result)
    games = out.get("games") or []
    if games and any(g.get("season_type") not in (1, 2, 3) for g in games):
        try:
            schedule = await get_today_games(date_str)
            by_id = {g["event_id"]: g for g in schedule if g.get("event_id")}
            by_matchup = {(g.get("away"), g.get("home"), g.get("tipoff")): g
                          for g in schedule}
            out["games"] = [
                {**g, "season_type": (
                    by_id.get(g.get("event_id")) or
                    by_matchup.get((g.get("away"), g.get("home"), g.get("tipoff"))) or {}
                ).get("season_type")}
                for g in games]
        except Exception as exc:
            print(f"[nba_season] season metadata unavailable for {date_str}: {exc}")
    return _nba_apply_season_scope(out)


def _nba_sb_get(params: dict) -> list:
    if not _NBA_SB_URL: return []
    h = {"apikey":_NBA_SB_KEY,"Authorization":f"Bearer {_NBA_SB_KEY}","Accept":"application/json"}
    try:
        r = httpx.get(f"{_NBA_SB_URL}/rest/v1/{_NBA_SB_TBL}", headers=h, params=params, timeout=12)
        return r.json() if r.status_code==200 else []
    except Exception as e:
        print(f"[nba_track] sb_get: {e}"); return []

def _nba_sb_upsert(rows: list, on_conflict: str=None) -> bool:
    if not _NBA_SB_URL or not rows: return False
    url = f"{_NBA_SB_URL}/rest/v1/{_NBA_SB_TBL}"
    if on_conflict: url += f"?on_conflict={on_conflict}"
    h = {"apikey":_NBA_SB_KEY,"Authorization":f"Bearer {_NBA_SB_KEY}",
         "Content-Type":"application/json","Prefer":"resolution=merge-duplicates,return=minimal"}
    try:
        r = httpx.post(url, headers=h, json=rows, timeout=20)
        return r.status_code in (200,201,204)
    except Exception as e:
        print(f"[nba_track] sb_upsert: {e}"); return False

_NBA_LIVE_BOARD_APP = _NBA_TRK_APP + "_live_boards"


def _nba_save_live_board(date_str: str, result: dict) -> None:
    """Durable copy of a completed live run for Get Picks (Render /tmp is
    wiped on restart/redeploy). Compressed; kept out of every record."""
    if not isinstance(result, dict) or result.get("historical_replay"):
        return
    try:
        import base64, gzip
        raw = json.dumps(result, ensure_ascii=False, default=str,
                         separators=(",", ":")).encode("utf-8")
        packed = base64.b64encode(gzip.compress(raw, 6)).decode("ascii")
        ok = _nba_sb_upsert([{
            "app": _NBA_LIVE_BOARD_APP, "date": date_str, "category": "__board__",
            "side": "ALL", "wins": 0, "losses": 0, "locked": False,
            "detail": {"version": 1, "date": date_str, "board_gz": packed,
                       "saved_at": datetime.utcnow().isoformat() + "Z"},
        }], "app,date,category,side")
        print(f"[nba_board] {'saved' if ok else 'NOT saved'} live board {date_str} "
              f"({len(packed) // 1024} KB compressed)")
    except Exception as exc:
        print(f"[nba_board] live board NOT saved for {date_str}: {exc}")


def _nba_save_live_board_bg(date_str: str, result: dict) -> None:
    _nba_th.Thread(target=_nba_save_live_board, args=(date_str, result),
                   daemon=True).start()


def _nba_read_live_board(date_str: str):
    """Read the durable live board; None when absent or unreadable."""
    rows = _nba_sb_get({"app": f"eq.{_NBA_LIVE_BOARD_APP}", "date": f"eq.{date_str}",
                        "category": "eq.__board__", "select": "detail", "limit": "1"})
    detail = (rows[0].get("detail") if rows else None) or {}
    packed = detail.get("board_gz") if isinstance(detail, dict) else None
    if not packed:
        return None
    try:
        import base64, gzip
        board = json.loads(gzip.decompress(base64.b64decode(packed)).decode("utf-8"))
    except Exception as exc:
        print(f"[nba_board] saved board unreadable for {date_str}: {exc}")
        return None
    if (not isinstance(board, dict) or board.get("date") != date_str
            or board.get("stat_parser_version") != 3):
        return None
    return board


def _nba_main_board_ok(date_str: str, result: dict) -> bool:
    """A complete live board (the per-game tipoff check happens per row)."""
    if not isinstance(result, dict) or result.get("date") != date_str:
        return False
    if (not _nba_official_board(result) or result.get("historical_replay")
            or result.get("run_incomplete")):
        return False
    if not result.get("games") or not result.get("odds_loaded"):
        return False
    if result.get("log") and any(str(x).lower().startswith("error") for x in result["log"]):
        return False
    return True


def _nba_tip_started(tipoff, now_utc) -> bool:
    """Unknown tipoff counts as started, so it is never captured as pregame."""
    try:
        from datetime import timezone as _tz
        t = datetime.fromisoformat(str(tipoff).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=_tz.utc)
        return now_utc >= t
    except Exception:
        return True


def _nba_save_picks_snapshot(date_str: str, result: dict):
    """Save category/side Top 10 plus all remaining standard qualifying plays.
    Per game: rows for games not yet tipped come from the latest run; games
    that already started keep their saved pregame rows (never rewritten)."""
    if not _nba_main_board_ok(date_str, result):
        return
    from datetime import timezone as _tz
    now_utc = datetime.now(_tz.utc)
    # Never broaden/reclassify an older saved board or rewrite settled data.
    with _NBA_TRK_LOCK:
        existing_rows = _nba_hist_sb_get({"app":f"eq.{_NBA_TRK_APP}",
                                        "category":f"eq.{_NBA_SNAP_CAT}",
                                        "side":"eq.ALL","date":f"eq.{date_str}",
                                        "select":"detail","limit":"1"})
        existing = (existing_rows[0].get("detail") or []) if existing_rows else []
        if existing and any(p.get("capture_scope") != "category_side_top10" for p in existing):
            return
        locked = _nba_hist_sb_get({"app":f"eq.{_NBA_TRK_APP}",
                             "category":"eq.__ledger__","date":f"eq.{date_str}",
                             "locked":"eq.true","select":"date","limit":"1"})
        if locked:
            return
        fresh = [r for r in _nba_snapshot_standard_rows(result)
                 if not _nba_tip_started(r.get("tipoff"), now_utc)]
        if not fresh:
            return
        kept = [r for r in existing if _nba_tip_started(r.get("tipoff"), now_utc)]
        flat = kept + fresh
        ok = _nba_sb_upsert(
            [{"app":_NBA_TRK_APP,"date":date_str,"category":_NBA_SNAP_CAT,
              "side":"ALL","wins":0,"losses":0,"locked":False,"detail":flat}],
            "app,date,category,side")
    print(f"[nba_track] snapshot {'saved' if ok else 'FAILED'}: {len(fresh)} pregame + "
          f"{len(kept)} locked started-game picks -> {date_str}")


def _nba_snapshot_standard_rows(result: dict) -> list:
    """Use the category OVER/UNDER buttons' source, order and side verdict."""
    flat = []
    ranks, seen = {}, set()
    for p in result.get("all_picks") or []:
        sk   = p.get("stat","")
        if p.get("has_consistency"):
            side = "OVER"
        else:
            votes = [p.get(k) for k in ("line_rec", "streak_rec", "alt_rec")]
            over, under = votes.count("OVER"), votes.count("UNDER")
            side = "OVER" if over > under else "UNDER" if under > over else None
        if not sk or side is None:
            continue
        # Exact standard line and side price only; never borrow opposite odds
        # or an alternate recommendation's price.
        line = p.get("dk_line")
        odds = p.get("dk_over_odds" if side == "OVER" else "dk_under_odds")
        book = p.get("dk_book")
        if line is None:
            line = p.get("fd_line")
            odds = p.get("fd_over_odds" if side == "OVER" else "fd_under_odds")
            book = p.get("fd_book")
        key = (str(p.get("player","")).lower().strip(), p.get("team"), sk, side, str(line))
        if key in seen:
            continue
        seen.add(key)
        group = (sk, side)
        ranks[group] = ranks.get(group, 0) + 1
        rank = ranks[group]
        if not _nba_trk_priced(odds):
            odds = None
        flat.append({"name":p.get("player",""),"team":p.get("team",""),
                     "category":_NBA_STAT_LABEL.get(sk,sk),"stat_key":sk,
                     "side":side,"line":line,"odds":odds,"book":book or "",
                     "rank":rank,"is_overflow":rank>_NBA_TRK_TOP,
                     "capture_scope":"category_side_top10",
                     "tipoff":p.get("tipoff"),
                     "score":p.get("hit_rate") or 0})
    return flat


def _nba_trk_priced(odds) -> bool:
    try:
        value = float(str(odds).replace("+", ""))
        return math.isfinite(value) and (value > 0 or -1000 <= value <= -100)
    except (TypeError, ValueError):
        return False

def _nba_load_picks_snapshot(date_str: str) -> list:
    rows = _nba_sb_get({"app":f"eq.{_NBA_TRK_APP}","category":f"eq.{_NBA_SNAP_CAT}",
                        "side":"eq.ALL","date":f"eq.{date_str}","select":"detail","limit":"1"})
    if rows:
        d = rows[0].get("detail") or []
        return d if isinstance(d, list) else []
    return []

def _nba_list_snap_dates() -> list:
    rows = _nba_sb_get({"app":f"eq.{_NBA_TRK_APP}","category":f"eq.{_NBA_SNAP_CAT}",
                        "side":"eq.ALL","select":"date","limit":"365"})
    return sorted({r["date"] for r in rows if r.get("date")})

def _nba_american_profit_trk(odds, stake: float, result: str) -> float:
    try: o = float(str(odds).replace("+","")) if odds else 0
    except: o = 0
    if result=="PUSH": return 0.0
    if result!="WIN": return -stake
    if o>0: return round(stake*o/100,2)
    if o<0: return round(stake*100/abs(o),2)
    return round(stake*0.91,2)

def _nba_name_key(name) -> str:
    """Sportsbook vs ESPN names: accents, punctuation and Jr./Sr./II-IV."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z ]", " ", s.replace("'", "").replace("\u2019", "").replace(".", ""))
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s(jr|sr|ii|iii|iv|v)$", "", s).strip()
    return s

def _nba_grade_date(date_str: str, snap: list) -> dict:
    from collections import defaultdict as _dd
    box      = {name: stats for name, stats in _nba_box_lookup(date_str).items()
                if stats.get("season_type") in (2, 3)}
    any_game = bool(box)
    slate_ok = bool(globals().get("_NBA_BOX_COMPLETE_CACHE", {}).get(date_str))
    fetch_ok = bool(globals().get("_NBA_BOX_FETCH_OK", {}).get(date_str))
    all_final= any_game and slate_ok and all(v.get("final",False) for v in box.values())
    box_norm = {}
    for _n, _st in box.items():
        box_norm.setdefault(_nba_name_key(_n), _st)
    main_rows, ovf_rows = [], []
    for p in snap:
        name_key = (p.get("name") or "").lower().strip()
        pd       = box.get(name_key) or box_norm.get(_nba_name_key(name_key)) or {}
        sk       = p.get("stat_key","")
        line_raw = p.get("line")
        odds     = p.get("odds")
        side     = (p.get("side") or "OVER").upper()
        result_val = actual = profit = None
        if pd.get("final") and line_raw is not None and sk:
            actual = pd.get(sk)
            if actual is not None:
                try:
                    fl = float(line_raw)
                    if actual==fl: result_val="PUSH"
                    elif side=="OVER": result_val="WIN" if actual>fl else "LOSS"
                    else: result_val="WIN" if actual<fl else "LOSS"
                    if result_val and _nba_trk_priced(odds):
                        profit = _nba_american_profit_trk(odds, _NBA_TRK_STAKE, result_val)
                except Exception: pass
        row = {"name":p.get("name",""),"team":p.get("team",""),
               "category":p.get("category","?"),"side":side,"stat_key":sk,
               "line":line_raw,"odds":odds,"rank":p.get("rank",999),
               "book":p.get("book",""),"is_overflow":bool(p.get("is_overflow")),
               "capture_scope":p.get("capture_scope"),
               "result":result_val,"actual":actual,"profit":profit}
        if not p.get("is_overflow"):
            main_rows.append(row)
        else:
            ovf_rows.append({**row,"category":"NBA Overflow"})
    return {"any_game":any_game,"all_final":all_final,"fetch_ok":fetch_ok,
            "main":main_rows,"overflow":ovf_rows}

def _nba_aggregate_graded(graded: dict) -> dict:
    agg: dict = {}
    for row in graded.get("main",[]) + graded.get("overflow",[]):
        if row.get("result") not in ("WIN","LOSS") or row.get("odds") is None: continue
        rec = agg.setdefault(row["category"],{}).setdefault(row.get("side","OVER"),[0,0])
        if row["result"]=="WIN": rec[0]+=1
        else: rec[1]+=1
    return agg

def _nba_detail_graded(graded: dict) -> list:
    out = []
    for row in graded.get("main",[]) + graded.get("overflow",[]):
        if row.get("result") not in ("WIN","LOSS","PUSH"): continue
        out.append({k:row.get(k) for k in
                    ("name","team","category","side","stat_key","line","odds","book","rank",
                     "is_overflow","capture_scope","result","actual","profit")})
    return out

def _nba_update_track_ledger():
    today = _nba_today().isoformat()
    with _NBA_TRK_LOCK:
        locked = {r["date"] for r in (_nba_sb_get(
            {"app":f"eq.{_NBA_TRK_APP}","category":"eq.__ledger__",
             "locked":"eq.true","select":"date","limit":"500"}) or [])}
        upserts = []
        for d in _nba_list_snap_dates():
            if d >= today or d in locked: continue
            snap = _nba_load_picks_snapshot(d)
            if not snap: continue
            try: graded = _nba_grade_date(d, snap)
            except Exception as e:
                print(f"[nba_track] grade failed {d}: {e}"); continue
            if not graded.get("any_game"): continue
            try:
                from datetime import date as _dd2
                old_enough = (_nba_today()-_dd2.fromisoformat(d)).days>=2
            except Exception: old_enough=False
            # Old dates may lock with postponed games, but never on an ESPN
            # fetch failure (that would permanently drop picks from the record).
            if not graded.get("all_final") and not (old_enough and graded.get("fetch_ok")):
                continue
            agg = _nba_aggregate_graded(graded)
            det = _nba_detail_graded(graded)
            upserts += [
                {"app":_NBA_TRK_APP,"date":d,"category":"__ledger__","side":"ALL",
                 "wins":0,"losses":0,"locked":True,"detail":agg},
                {"app":_NBA_TRK_APP,"date":d,"category":"__detail__","side":"ALL",
                 "wins":0,"losses":0,"locked":True,"detail":det},
            ]
        if upserts:
            for i in range(0,len(upserts),10):
                _nba_sb_upsert(upserts[i:i+10],"app,date,category,side")
            print(f"[nba_track] locked {len(upserts)//2} dates")

def _nba_trk_bg(date_str: str, result: dict):
    if not _nba_official_board(result):
        return
    try:
        _nba_save_picks_snapshot(date_str, result)
    except Exception as exc:
        print(f"[nba_track] snapshot NOT saved for {date_str}: {exc}")
    try:
        _nba_save_hc_snapshot(date_str, result)
    except Exception as exc:
        print(f"[nba_hc_track] snapshot NOT saved for {date_str}: {exc}")
    _nba_th.Thread(target=_nba_update_all_ledgers, daemon=True).start()

_NBA_TRK_CACHED_TS: dict = {}

def _nba_trk_bg_throttled(date_str: str, result: dict):
    """Cached boards re-attempt the Track Record save at most every 10 min."""
    now = time.time()
    if now - _NBA_TRK_CACHED_TS.get(date_str, 0) < 600:
        return
    _NBA_TRK_CACHED_TS[date_str] = now
    _nba_th.Thread(target=_nba_trk_bg, args=(date_str, result), daemon=True).start()

# ─── NBA Hot / Cold form section (separately tracked spotlight record) ───────
# Mirrors the NHL Hot/Cold rules. These rows never touch the ordinary picks,
# boards or the Main/Overflow ledgers: they live in their own snapshot/ledger
# categories inside the same NBA app namespace.
_NBA_HC_SNAP_CAT   = "__hc_picks__"
_NBA_HC_DETAIL_CAT = "__hc_detail__"
_NBA_HC_LEDGER_CAT = "__hc_ledger__"
_NBA_HC_FORMS = (("hot", "Hot", "OVER"), ("cold", "Cold", "UNDER"),
                 ("double_cold", "Double Cold", "UNDER"))
_NBA_HC_TOP = 10       # Top 10 main per form x market
_NBA_HC_LIMIT = 20     # ranks 11-20 are the Hot/Cold overflow
_NBA_HC_KEYS = ("name","team","opp","category","market","stat_key","side","line","odds",
                "book","rank","is_overflow","form","form_key","edge","capture_scope")


def _nba_hot_cold_min_games(target_date):
    """October uses 2-5 current-season games; November onward requires five."""
    try:
        return 2 if date.fromisoformat(str(target_date)[:10]).month == 10 else 5
    except (TypeError, ValueError):
        return 5


def _nba_et_date(raw) -> str:
    """ESPN game timestamps are UTC; NBA slate dates are Eastern."""
    s = str(raw or "")
    if len(s) <= 10:
        return s[:10]
    try:
        from zoneinfo import ZoneInfo as _ZI
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return s[:10]
        return dt.astimezone(_ZI("America/New_York")).date().isoformat()
    except Exception:
        return s[:10]


def _nba_hc_price(raw):
    """Genuine side price only: +100 or longer, or -100 down to -1000."""
    try:
        v = float(str(raw).replace("+", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v < -1000 or -100 < v < 100:
        return None
    return int(v) if v == int(v) else v


def _nba_hot_cold_rows(today_str, games, rosters, logs_by_player, dk_lookup, props_rows):
    """Hot (OVER) / Cold (UNDER) / Double Cold form plays for every market with a
    genuine standard sportsbook line. Independent of the ordinary pick gates."""
    try:
        as_of = date.fromisoformat(str(today_str)[:10])
    except (TypeError, ValueError):
        return []
    season_start = date(as_of.year if as_of.month >= 10 else as_of.year - 1, 10, 1).isoformat()
    minimum = _nba_hot_cold_min_games(today_str)
    props_by_key = {}
    for r in props_rows or []:
        props_by_key.setdefault((str(r.get("player") or ""), r.get("stat")), r)
    candidates = []
    for game in games or []:
        for tid, team, opp, opp_name, venue in (
                (game.get("home_id"), game.get("home"), game.get("away"), game.get("away_name"), "HOME"),
                (game.get("away_id"), game.get("away"), game.get("home"), game.get("home_name"), "AWAY")):
            for player in (rosters or {}).get(tid, []) or []:
                pid, pname = player.get("id"), player.get("name") or ""
                if not pname or not any((_nn(pname), s) in dk_lookup for s in STAT_CONFIG):
                    continue
                history, seen = [], set()
                for g in logs_by_player.get(pid, []) or []:
                    d = _nba_et_date(g.get("date"))
                    if not d or d >= today_str or d in seen:
                        continue
                    seen.add(d)
                    history.append((d, g))
                history.sort(key=lambda x: x[0], reverse=True)
                # True chronological last five, current season only, all venues.
                recent = [(d, g) for d, g in history if d >= season_start][:5]
                if len(recent) < minimum:
                    continue
                versus = [(d, g) for d, g in history if g.get("opp") == opp][:5]
                for sk, sc in STAT_CONFIG.items():
                    quote = dk_lookup.get((_nn(pname), sk)) or {}
                    try:
                        line = float(quote.get("line"))
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(line):
                        continue
                    try:
                        vals = [float(g[sk]) for _, g in recent]
                        vs_vals = [float(g[sk]) for _, g in versus]
                    except (KeyError, TypeError, ValueError):
                        continue
                    avg = sum(vals) / len(vals)
                    for side, form in (("OVER", "HOT"), ("UNDER", "COLD")):
                        hits = sum(v > line if side == "OVER" else v < line for v in vals)
                        if hits * 5 < len(vals) * 4 or not (avg > line if side == "OVER" else avg < line):
                            continue
                        opp_hits = sum(v > line if side == "OVER" else v < line for v in vs_vals)
                        opp_rate = opp_hits / len(vs_vals) * 100 if vs_vals else None
                        supports = bool(len(vs_vals) >= 3 and opp_rate is not None and opp_rate >= 70)
                        odds = _nba_hc_price(quote.get("over_odds" if side == "OVER" else "under_odds"))
                        implied = _nba_coach_implied(odds) if odds is not None else None
                        # Edge reuses the app's own same-line estimate only.
                        model = None
                        prop = props_by_key.get((pname, sk)) or {}
                        try:
                            if prop and abs(float(prop.get("line")) - line) < 1e-9:
                                model, _ = _nba_coach_prob(prop, side, line)
                        except (TypeError, ValueError):
                            model = None
                        edge = (round((model - implied) * 100, 2)
                                if model is not None and implied is not None else None)
                        candidates.append({
                            "player": pname, "player_id": pid, "team": team or "",
                            "opp": opp or "", "opp_name": opp_name or "", "venue": venue,
                            "matchup": f"{game.get('away_name','')} @ {game.get('home_name','')}",
                            "tipoff": game.get("tipoff", ""), "stat": sk,
                            "stat_label": sc["label"], "emoji": sc["emoji"],
                            "pick": side, "form": form,
                            "double_cold": form == "COLD" and supports,
                            "matchup_supports": supports, "line": line, "odds": odds,
                            "book": ((quote.get("bookmaker_label") or quote.get("bookmaker") or "")
                                     if odds is not None else ""),
                            "recent_hits": hits, "recent_total": len(vals),
                            "recent_avg": round(avg, 2),
                            "recent_pushes": sum(v == line for v in vals),
                            "recent_games": [{"d": d, "opp": g.get("opp", ""),
                                              "loc": g.get("location", ""), "v": v}
                                             for (d, g), v in zip(recent, vals)],
                            "opp_hits": opp_hits, "opp_total": len(vs_vals),
                            "opp_rate": round(opp_rate, 1) if opp_rate is not None else None,
                            "opp_games": [{"d": d, "loc": g.get("location", ""), "v": v}
                                          for (d, g), v in zip(versus, vs_vals)],
                            "app_probability": round(model * 100, 1) if model is not None else None,
                            "implied_probability": round(implied * 100, 1) if implied is not None else None,
                            "edge": edge,
                        })

    def rank_key(r):
        margin = (r["recent_avg"] - r["line"]) if r["pick"] == "OVER" else (r["line"] - r["recent_avg"])
        return (-(r["recent_hits"] / r["recent_total"]), -r["recent_hits"],
                -bool(r["matchup_supports"]), r["odds"] is None,
                -(margin / max(abs(r["line"]), 0.5)),
                r["player"].casefold(), str(r["player_id"]))

    out = []
    for form_key, form_label, side in _NBA_HC_FORMS:
        for sk, sc in STAT_CONFIG.items():
            pool = [r for r in candidates if r["stat"] == sk and r["pick"] == side
                    and (form_key != "double_cold" or r["double_cold"])]
            pool.sort(key=rank_key)
            seen_players = set()
            rank = 0
            for r in pool:
                pkey = (r["player"].casefold(), r["team"])
                if pkey in seen_players:
                    continue
                seen_players.add(pkey)
                rank += 1
                if rank > _NBA_HC_LIMIT:
                    break
                out.append({**r, "form_key": form_key, "form_label": form_label,
                            "category": f"{form_label} {sc['label']}",
                            "hc_key": f"{form_key}|{sk}", "rank": rank,
                            "is_overflow": rank > _NBA_HC_TOP})
    return out


def _nba_hc_snapshot_rows(result: dict) -> list:
    flat = []
    for r in result.get("hot_cold") or []:
        sk = r.get("stat")
        if sk not in STAT_CONFIG or r.get("line") is None or r.get("pick") not in ("OVER", "UNDER"):
            continue
        odds = r.get("odds") if _nba_trk_priced(r.get("odds")) else None
        flat.append({"name": r.get("player", ""), "team": r.get("team", ""), "opp": r.get("opp", ""),
                     "category": r.get("category") or "", "market": _NBA_STAT_LABEL.get(sk, sk),
                     "stat_key": sk, "side": r.get("pick"), "line": r.get("line"),
                     "odds": odds, "book": (r.get("book") or "") if odds is not None else "",
                     "rank": r.get("rank"), "is_overflow": bool(r.get("is_overflow")),
                     "form": r.get("form"), "form_key": r.get("form_key"),
                     "edge": r.get("edge"), "capture_scope": "hot_cold",
                     "tipoff": r.get("tipoff")})
    return flat


def _nba_save_hc_snapshot(date_str: str, result: dict):
    """Per game: unstarted games take the latest run; started games keep their
    saved pregame Hot/Cold rows. Never rewrites a locked (graded) date."""
    if not _nba_main_board_ok(date_str, result) or not isinstance(result.get("hot_cold"), list):
        return
    from datetime import timezone as _tz
    now_utc = datetime.now(_tz.utc)
    with _NBA_TRK_LOCK:
        existing_rows = _nba_hist_sb_get({"app": f"eq.{_NBA_TRK_APP}",
                                          "category": f"eq.{_NBA_HC_SNAP_CAT}",
                                          "side": "eq.ALL", "date": f"eq.{date_str}",
                                          "select": "detail", "limit": "1"})
        existing = (existing_rows[0].get("detail") or []) if existing_rows else []
        locked = _nba_hist_sb_get({"app": f"eq.{_NBA_TRK_APP}",
                                   "category": f"eq.{_NBA_HC_LEDGER_CAT}", "date": f"eq.{date_str}",
                                   "locked": "eq.true", "select": "date", "limit": "1"})
        if locked:
            return
        fresh = [r for r in _nba_hc_snapshot_rows(result)
                 if not _nba_tip_started(r.get("tipoff"), now_utc)]
        kept = [r for r in existing if _nba_tip_started(r.get("tipoff"), now_utc)]
        if not fresh:
            return
        ok = _nba_sb_upsert(
            [{"app": _NBA_TRK_APP, "date": date_str, "category": _NBA_HC_SNAP_CAT,
              "side": "ALL", "wins": 0, "losses": 0, "locked": False, "detail": kept + fresh}],
            "app,date,category,side")
    print(f"[nba_hc_track] snapshot {'saved' if ok else 'FAILED'}: {len(fresh)} pregame + "
          f"{len(kept)} locked started-game Hot/Cold picks -> {date_str}")


def _nba_load_hc_snapshot(date_str: str) -> list:
    rows = _nba_sb_get({"app": f"eq.{_NBA_TRK_APP}", "category": f"eq.{_NBA_HC_SNAP_CAT}",
                        "side": "eq.ALL", "date": f"eq.{date_str}", "select": "detail", "limit": "1"})
    d = (rows[0].get("detail") or []) if rows else []
    return d if isinstance(d, list) else []


def _nba_update_hc_ledger():
    """Grade saved Hot/Cold snapshots with the same box-score grader as Main."""
    today = _nba_today().isoformat()
    with _NBA_TRK_LOCK:
        locked = {r["date"] for r in (_nba_sb_get(
            {"app": f"eq.{_NBA_TRK_APP}", "category": f"eq.{_NBA_HC_LEDGER_CAT}",
             "locked": "eq.true", "select": "date", "limit": "500"}) or [])}
        dates = sorted({r["date"] for r in (_nba_sb_get(
            {"app": f"eq.{_NBA_TRK_APP}", "category": f"eq.{_NBA_HC_SNAP_CAT}",
             "side": "eq.ALL", "select": "date", "limit": "365"}) or []) if r.get("date")})
        upserts = []
        for d in dates:
            if d >= today or d in locked:
                continue
            snap = _nba_load_hc_snapshot(d)
            if not snap:
                continue
            try:
                graded = _nba_grade_date(d, [{**r, "is_overflow": False} for r in snap])
            except Exception as e:
                print(f"[nba_hc_track] grade failed {d}: {e}")
                continue
            if not graded.get("any_game"):
                continue
            try:
                old_enough = (_nba_today() - date.fromisoformat(d)).days >= 2
            except Exception:
                old_enough = False
            if not graded.get("all_final") and not (old_enough and graded.get("fetch_ok")):
                continue
            det, agg = [], {}
            for src, row in zip(snap, graded.get("main", [])):
                if row.get("result") not in ("WIN", "LOSS", "PUSH"):
                    continue
                out = {k: src.get(k) for k in _NBA_HC_KEYS}
                out.update({"result": row.get("result"), "actual": row.get("actual"),
                            "profit": row.get("profit")})
                det.append(out)
                if out["result"] in ("WIN", "LOSS") and out.get("odds") is not None:
                    rec = agg.setdefault(out["category"], {}).setdefault(out.get("side", "OVER"), [0, 0])
                    rec[0 if out["result"] == "WIN" else 1] += 1
            upserts += [
                {"app": _NBA_TRK_APP, "date": d, "category": _NBA_HC_LEDGER_CAT, "side": "ALL",
                 "wins": 0, "losses": 0, "locked": True, "detail": agg},
                {"app": _NBA_TRK_APP, "date": d, "category": _NBA_HC_DETAIL_CAT, "side": "ALL",
                 "wins": 0, "losses": 0, "locked": True, "detail": det},
            ]
        if upserts:
            for i in range(0, len(upserts), 10):
                _nba_sb_upsert(upserts[i:i + 10], "app,date,category,side")
            print(f"[nba_hc_track] locked {len(upserts)//2} dates")


def _nba_hc_record_response() -> dict:
    """Hot/Cold spotlight record: graded rows plus PENDING saved pregame rows."""
    det_rows = _nba_hist_sb_get({"app": f"eq.{_NBA_TRK_APP}", "category": f"eq.{_NBA_HC_DETAIL_CAT}",
                                 "locked": "eq.true", "select": "date,detail", "limit": "365"})
    detail_by_date = {r["date"]: (r.get("detail") or []) for r in (det_rows or []) if r.get("date")}
    pending_dates = set()
    try:
        cutoff = (_nba_today() - timedelta(days=10)).isoformat()
        snap_dates = sorted({r["date"] for r in (_nba_hist_sb_get(
            {"app": f"eq.{_NBA_TRK_APP}", "category": f"eq.{_NBA_HC_SNAP_CAT}",
             "side": "eq.ALL", "select": "date", "limit": "365"}) or []) if r.get("date")})
        for d in snap_dates:
            if d in detail_by_date or d < cutoff:
                continue
            snap = _nba_load_hc_snapshot(d)
            if not snap:
                continue
            detail_by_date[d] = [{**{k: p.get(k) for k in _NBA_HC_KEYS},
                                  "result": "PENDING", "actual": None, "profit": None} for p in snap]
            pending_dates.add(d)
    except Exception as exc:
        print(f"[nba_hc_track] pending snapshot read failed: {exc}")
    out = []
    for d in sorted(detail_by_date, reverse=True):
        det = []
        for saved in detail_by_date[d]:
            row = dict(saved)
            row["is_overflow"] = bool(row.get("is_overflow"))
            if not _nba_trk_priced(row.get("odds")):
                row["odds"] = None
                row["profit"] = None
            row["stake"] = _NBA_TRK_STAKE if row.get("odds") is not None else None
            det.append(row)
        if det:
            out.append({"date": d, "pending": d in pending_dates, "detail": det})
    return {"dates": out, "stake": _NBA_TRK_STAKE, "record": "hot_cold"}


def _nba_update_all_ledgers():
    try:
        _nba_update_track_ledger()
    except Exception as exc:
        print(f"[nba_track] ledger update failed: {exc}")
    try:
        _nba_update_hc_ledger()
    except Exception as exc:
        print(f"[nba_hc_track] ledger update failed: {exc}")

_nba_th.Thread(target=_nba_update_all_ledgers, daemon=True).start()

_NBA_BET_LOG_PATH = str(_CACHE_DIR / "_nba_bet_log.json")
_NBA_BET_LOCK = _nba_th.Lock()
_NBA_BET_STAT_KEYS = ("PTS","REB","AST","FG3M","BLK","STL","PRA","PTS_REB","PTS_AST","REB_AST")
_NBA_STAT_LABEL = {"PTS":"Points","REB":"Rebounds","AST":"Assists","FG3M":"3-Pointers",
    "PRA":"Pts+Reb+Ast","PTS_REB":"Pts+Reb","PTS_AST":"Pts+Ast",
    "REB_AST":"Reb+Ast","BLK":"Blocks","STL":"Steals"}

def _nba_load_bets() -> dict:
    try:
        with open(_NBA_BET_LOG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _nba_save_bets(data: dict):
    try:
        tmp = _NBA_BET_LOG_PATH + f".{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _NBA_BET_LOG_PATH)
    except Exception as e:
        print(f"[nba_bet_log] save failed: {e}")

def _nba_bet_admin_ok(tok: str, admin: str) -> bool:
    return _is_admin_token(tok) or (
        bool(admin) and admin == os.environ.get("INTERNAL_API_TOKEN", "__none__"))

def _nba_bet_user_key(tok: str, admin: str) -> str:
    em = _token_email(tok) if tok else ""
    return em.lower().strip() if em else "__admin__"

def _nba_american_profit(odds, stake, result) -> float:
    try:
        stake = float(stake)
    except Exception:
        return 0.0
    if result == "WIN":
        try:
            o = float(odds)
        except Exception:
            return 0.0
        return stake * (o / 100.0) if o > 0 else stake * (100.0 / abs(o))
    if result == "LOSS":
        return -stake
    return 0.0

def _nba_am_to_dec(odds) -> float:
    try:
        o = float(odds)
    except Exception:
        return 1.0
    return round(1 + o / 100, 6) if o > 0 else round(1 + 100 / abs(o), 6)

def _nba_extract_stat(stats_arr: list, stat_key: str, labels: list = None):
    """Extract an NBA stat using ESPN's supplied labels, with legacy fallback."""
    legacy_idx = {"PTS": 13, "REB": 6, "AST": 7, "FG3M": 2, "BLK": 9, "STL": 8}
    aliases = {"PTS": ("PTS",), "REB": ("REB",), "AST": ("AST",),
               "FG3M": ("3PM", "FG3M", "3PT", "3FG", "3PM-A", "3PTM-A"),
               "BLK": ("BLK",), "STL": ("STL",)}
    label_idx = {str(label).upper(): i for i, label in enumerate(labels or [])}
    if stat_key == "FG3M":
        # ESPN has served game-log payloads where category labels and event values
        # are offset, making the apparent 3PT value a percentage (52.0, 43.8,
        # etc.). Made threes must be a small whole number. Prefer a labelled
        # made-attempt value, then use the second made-attempt pair in the row
        # (FG, 3PT, FT) rather than ever accepting a percentage.
        for label in aliases["FG3M"]:
            idx = label_idx.get(label)
            if idx is None or idx >= len(stats_arr):
                continue
            raw = stats_arr[idx]
            match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(raw))
            if match:
                return float(match.group(1))
            try:
                value = float(raw)
                if value.is_integer() and 0 <= value <= 20:
                    return value
            except Exception:
                pass
        made_attempt_pairs = []
        for raw in stats_arr:
            match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(raw))
            if match:
                made_attempt_pairs.append(float(match.group(1)))
        return made_attempt_pairs[1] if len(made_attempt_pairs) >= 2 else None
    idx = None
    for label in aliases.get(stat_key, ()):
        if label in label_idx:
            idx = label_idx[label]
            break
    if idx is None and not labels:
        idx = legacy_idx.get(stat_key)
    if idx is not None:
        try:
            raw = stats_arr[idx]
            if stat_key == "FG3M" and isinstance(raw, str) and "-" in raw:
                return float(raw.split("-")[0])
            value = float(raw)
            return value if math.isfinite(value) and value >= 0 and value.is_integer() else None
        except Exception:
            return None
    _base = {"PRA": ("PTS","REB","AST"), "PTS_REB": ("PTS","REB"),
             "PTS_AST": ("PTS","AST"), "REB_AST": ("REB","AST")}
    if stat_key in _base:
        vals = [_nba_extract_stat(stats_arr, k, labels) for k in _base[stat_key]]
        if all(v is not None for v in vals):
            return sum(vals)
    return None


def _nba_fg3m_logs_sane(result: dict) -> bool:
    """Reject cached boards containing percentages misread as made threes."""
    for group in ("picks", "all_picks", "props_picks", "props_nopick"):
        for row in result.get(group, []) or []:
            if str(row.get("stat") or row.get("stat_key") or "").upper() != "FG3M":
                continue
            for log_key in ("glog", "recent_glog"):
                for entry in row.get(log_key, []) or []:
                    raw = entry.get("v") if isinstance(entry, dict) else entry
                    try:
                        value = float(raw)
                    except Exception:
                        continue
                    if not value.is_integer() or value < 0 or value > 20:
                        return False
    return True

_NBA_BOX_CACHE: dict = {}
_NBA_BOX_COMPLETE_CACHE: dict = {}
_NBA_BOX_FETCH_OK: dict = {}
_NBA_BOX_LOCK = _nba_th.Lock()
_NBA_BOX_TTL = 120

def _nba_box_lookup(date_str: str) -> dict:
    """Cached wrapper: final dates cached permanently, in-progress dates for
    _NBA_BOX_TTL seconds. Prevents repeat ESPN scoreboard hits (HTTP 429) when
    My Bets / the hub fan-out settle the same date many times.
    Lock prevents concurrent misses from all hitting ESPN simultaneously."""
    import time as _t
    ent = _NBA_BOX_CACHE.get(date_str)
    now = _t.time()
    if ent and (ent["final"] or now - ent["ts"] < _NBA_BOX_TTL):
        return ent["data"]
    with _NBA_BOX_LOCK:
        ent = _NBA_BOX_CACHE.get(date_str)
        now = _t.time()
        if ent and (ent["final"] or now - ent["ts"] < _NBA_BOX_TTL):
            return ent["data"]
        res, complete = _nba_box_lookup_raw(date_str)
        allfinal = complete and bool(res)
        _NBA_BOX_CACHE[date_str] = {"ts": now, "final": allfinal, "data": res}
        # Keep compatibility with AST-isolated consumers that load the
        # historical box helper without the service module globals.
        globals().setdefault("_NBA_BOX_COMPLETE_CACHE", {})[date_str] = bool(allfinal)
        return res

def _nba_box_lookup_raw(date_str: str):
    """Return (results, complete). results = {lowername: {stat_key: float, 'final': bool}}.
    complete is True only when EVERY event for the date is final AND its box score was
    fetched successfully, so the wrapper marks the cache permanent only on fully-complete
    data (a failed summary fetch keeps the date on the short TTL so it retries)."""
    d = date_str.replace("-", "")
    results: dict = {}
    try:
        sb = httpx.get(
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}",
            timeout=15)
        sb.raise_for_status()
        scoreboard = sb.json()
        events = scoreboard.get("events", [])
    except Exception as e:
        # ESPN intermittently rejects a connection; retry with the same normal
        # client policy (never the Mozilla UA, which causes a known 403).
        try:
            sb = httpx.get(
                f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}",
                timeout=15)
            sb.raise_for_status()
            scoreboard = sb.json()
            events = scoreboard.get("events", [])
        except Exception as retry_error:
            print(f"[nba_box] scoreboard failed {date_str}: {retry_error}")
            globals().setdefault("_NBA_BOX_FETCH_OK", {})[date_str] = False
            return results, False
    complete = True
    fetch_ok = True
    for ev in events:
        season_type = _nba_season_type(ev, scoreboard)
        is_final = ev.get("status", {}).get("type", {}).get("completed", False)
        if not is_final:
            complete = False
        ev_id = ev.get("id")
        if not ev_id:
            complete = False
            continue
        boxscore = None
        for _try in range(3):
            try:
                bs = httpx.get(
                    f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={ev_id}",
                    timeout=15)
                if bs.status_code == 200:
                    boxscore = bs.json().get("boxscore", {})
                    break
            except Exception:
                pass
            time.sleep(0.7 * (_try + 1))
        if boxscore is None:
            complete = False
            fetch_ok = False
            continue
        for team in boxscore.get("players", []):
            team_meta = team.get("team", {}) or {}
            team_abbr = _norm_abbr(team_meta.get("abbreviation") or "")
            for grp in team.get("statistics", []):
                for ath in grp.get("athletes", []):
                    athlete = ath.get("athlete", {}) or {}
                    name = (athlete.get("displayName") or "").lower().strip()
                    stats_arr = ath.get("stats", [])
                    if not name or not stats_arr:
                        continue
                    headshot = athlete.get("headshot") or {}
                    ps: dict = {
                        "final": is_final,
                        "season_type": season_type,
                        "player_id": athlete.get("id"),
                        "headshot": headshot.get("href") if isinstance(headshot, dict) else "",
                        "team": team_abbr,
                    }
                    labels = grp.get("labels") or grp.get("names") or []
                    for sk in _NBA_BET_STAT_KEYS:
                        v = _nba_extract_stat(stats_arr, sk, labels)
                        if v is not None:
                            ps[sk] = v
                    results[name] = ps
    globals().setdefault("_NBA_BOX_FETCH_OK", {})[date_str] = fetch_ok
    return results, complete

def _nba_settle_cached(bet: dict, name_stats: dict) -> bool:
    if bet.get("result") in ("WIN", "LOSS", "PUSH"):
        return False
    st = name_stats.get((bet.get("name") or "").lower())
    if not st or not st.get("final"):
        return False
    stat_key = bet.get("stat_key")
    actual = st.get(stat_key)
    if actual is None:
        return False
    try:
        line = float(bet.get("line"))
    except Exception:
        return False
    side = bet.get("side", "OVER")
    if actual == line:
        res = "PUSH"
    elif side == "OVER":
        res = "WIN" if actual > line else "LOSS"
    else:
        res = "WIN" if actual < line else "LOSS"
    bet["result"] = res
    bet["actual"] = actual
    bet["profit"] = round(_nba_american_profit(bet.get("odds"), bet.get("stake"), res), 2)
    bet["settled_at"] = _nba_today().isoformat()
    return True

def _nba_settle_bet(bet: dict) -> bool:
    if bet.get("result") in ("WIN", "LOSS", "PUSH"):
        return False
    bdate = bet.get("date")
    if not bdate or bdate >= _nba_today().isoformat():
        return False
    try:
        ns = _nba_box_lookup(bdate)
    except Exception as e:
        print(f"[nba_bet_log] settle lookup failed {bdate}: {e}")
        return False
    return _nba_settle_cached(bet, ns)

def _nba_settle_batch(bets: list) -> bool:
    today = _nba_today().isoformat()
    dates_needed: set = set()
    for b in bets:
        if b.get("result") in ("WIN", "LOSS", "PUSH"):
            continue
        if b.get("date") and b["date"] < today:
            dates_needed.add(b["date"])
    if not dates_needed:
        return False
    ns_cache: dict = {}
    for d in sorted(dates_needed):
        try:
            ns_cache[d] = _nba_box_lookup(d)
        except Exception as e:
            print(f"[nba_bet_log] batch settle failed {d}: {e}")
    changed = False
    for b in bets:
        bdate = b.get("date")
        if bdate and bdate in ns_cache:
            if _nba_settle_cached(b, ns_cache[bdate]):
                changed = True
    return changed

_NBA_CAT_ORDER = ["Points","Rebounds","Assists","3-Pointers","Pts+Reb+Ast",
    "Pts+Reb","Pts+Ast","Reb+Ast","Blocks","Steals"]

def _nba_summarize_bets(bets: list) -> dict:
    cats: dict = {}
    tot_staked = tot_profit = 0.0
    w = l = pu = pend = 0
    for b in bets:
        res = b.get("result", "pending")
        try:
            stake = float(b.get("stake") or 0)
        except Exception:
            stake = 0.0
        c = cats.setdefault(b.get("category", "?"),
                            {"wins": 0, "losses": 0, "push": 0, "pending": 0,
                             "staked": 0.0, "profit": 0.0})
        if res == "WIN": w += 1; c["wins"] += 1
        elif res == "LOSS": l += 1; c["losses"] += 1
        elif res == "PUSH": pu += 1; c["push"] += 1
        else: pend += 1; c["pending"] += 1
        if res in ("WIN", "LOSS", "PUSH"):
            prof = float(b.get("profit") or 0)
            tot_staked += stake; c["staked"] += stake
            tot_profit += prof; c["profit"] += prof
    roi = (tot_profit / tot_staked * 100.0) if tot_staked > 0 else None
    ordered = _NBA_CAT_ORDER + [k for k in cats if k not in _NBA_CAT_ORDER]
    by_cat = []
    for cat in ordered:
        c = cats.get(cat)
        if not c:
            continue
        st = c["staked"]; pr = c["profit"]
        by_cat.append({"category": cat, "wins": c["wins"], "losses": c["losses"],
            "push": c["push"], "pending": c["pending"],
            "staked": round(st, 2), "profit": round(pr, 2),
            "roi": round(pr / st * 100, 1) if st > 0 else None})
    return {"wins": w, "losses": l, "push": pu, "pending": pend,
        "staked": round(tot_staked, 2), "profit": round(tot_profit, 2),
        "returned": round(tot_staked + tot_profit, 2),
        "roi": round(roi, 1) if roi is not None else None,
        "by_category": by_cat}

def _cache_path(app: str, date_key: str) -> pathlib.Path:
    return _CACHE_DIR / f"{app}_{date_key}.json"

def _cache_get(app: str, date_key: str, ignore_ttl: bool = False):
    p = _cache_path(app, date_key)
    try:
        # Historical NBA replay is immutable and must remain reusable.  In
        # particular, never make a past replay spend live Odds API quota again.
        # Get Picks (read-only) passes ignore_ttl: the 6h TTL only decides
        # when a new run refreshes lines, never whether a saved run exists.
        _permanent_nba_replay = app == "nba" and date_key < _nba_today().isoformat()
        if p.exists() and (ignore_ttl or _permanent_nba_replay or
                          (time.time() - p.stat().st_mtime) < _CACHE_TTL):
            data = json.loads(p.read_text(encoding="utf-8"))
            if app == "nba" and data.get("stat_parser_version") != 3:
                return None
            print(f"[Cache] FILE HIT {app}/{date_key}")
            return data
    except Exception as e:
        print(f"[Cache] Read error: {e}")
    return None

def _cache_set(app: str, date_key: str, result: dict):
    try:
        if app == "nba":
            result["stat_parser_version"] = 3
        _cache_path(app, date_key).write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8")
        print(f"[Cache] FILE SET {app}/{date_key}")
    except Exception as e:
        print(f"[Cache] Write error: {e}")

def _cache_clear(app: str = None):
    for p in _CACHE_DIR.glob("*.json"):
        if app is None or p.name.startswith(app + "_"):
            p.unlink(missing_ok=True)

def _schedule_nba_alternates_warm(date_str: str) -> None:
    """Warm only the separate alternate cache when a standard result is cached."""
    if date_str < _nba_today().isoformat() or _cache_get('nba_alternates', date_str):
        return
    task = _NBA_ALT_WARM_INFLIGHT.get(date_str)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(get_odds_lines(date_str, alternate_only=True))
    _NBA_ALT_WARM_INFLIGHT[date_str] = task

    def _clear(done_task):
        if _NBA_ALT_WARM_INFLIGHT.get(date_str) is done_task:
            _NBA_ALT_WARM_INFLIGHT.pop(date_str, None)
        # The alternate board is an independent cache and may finish after the
        # standard run.  Re-capture while still pregame so genuine exact
        # alternate lines can enter the Coach snapshot without touching NBA.
        try:
            _cached = _cache_get("nba", date_str)
            if _cached:
                _nba_coach_capture_pregame(date_str, _cached)
        except Exception as _ce:
            print(f"[nba_coach_track] alternate capture failed: {_ce}")
    task.add_done_callback(_clear)

async def get_today_games(date_str: str = None) -> List[Dict]:
    if date_str:
        today_fmt = datetime.strptime(date_str, '%Y-%m-%d').strftime('%Y%m%d')
    else:
        today_fmt = _nba_today().strftime('%Y%m%d')

    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={today_fmt}"
    last_error = None
    for _attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(url)
            if getattr(r, "status_code", 200) < 200 or getattr(r, "status_code", 200) >= 300:
                raise RuntimeError(f"ESPN schedule HTTP {getattr(r, 'status_code', 'unknown')}")
            data = r.json()
            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise RuntimeError("ESPN schedule response missing events list")
            break
        except Exception as exc:
            last_error = exc
            if _attempt:
                raise RuntimeError(f"ESPN schedule unavailable: {exc}") from exc
    else:
        raise RuntimeError(f"ESPN schedule unavailable: {last_error}")

    games = []
    for event in data.get('events', []):
        comps = event['competitions'][0]['competitors']
        home = next((c for c in comps if c['homeAway'] == 'home'), None)
        away = next((c for c in comps if c['homeAway'] == 'away'), None)
        if not home or not away:
            continue
        games.append({
            'event_id':   str(event.get('id', '')),
            'season_type': _nba_season_type(event, data),
            'home':      _norm_abbr(home['team']['abbreviation']),
            'away':      _norm_abbr(away['team']['abbreviation']),
            'home_id':   home['team']['id'],
            'away_id':   away['team']['id'],
            'home_name': home['team']['displayName'],
            'away_name': away['team']['displayName'],
            'tipoff':    event.get('date', ''),
        })
    return games


async def get_team_roster_espn(team_id: str) -> List[Dict]:
    """ESPN roster with retries. Raises when ESPN never answers, so the run
    can stop explicitly instead of silently dropping a whole team."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
    last_error = None
    for attempt in range(4):
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                await asyncio.sleep(0.1)
                r = await c.get(url)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            data = r.json()
            athletes = data.get('athletes', []) if isinstance(data, dict) else []
            if not athletes:
                raise RuntimeError("empty roster")
            return [{'id': p['id'], 'name': p.get('displayName', ''),
                     'jersey': p.get('jersey', ''),
                     'position': (p.get('position') or {}).get('abbreviation', '')}
                    for p in athletes]
        except Exception as e:
            last_error = e
            print(f"  Roster error {team_id} (attempt {attempt + 1}): {e}")
            await asyncio.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"ESPN roster {team_id} unavailable: {last_error}")


async def get_player_gamelogs_espn(player_id: str, season: int,
                                    sem: asyncio.Semaphore,
                                    client: httpx.AsyncClient = None) -> List[Dict]:
    """Fetch one player's game logs for one season from ESPN."""
    cache_key = (str(player_id), int(season))
    completed_season = int(season) < _nba_current_espn_season()
    cached = _NBA_GAMELOG_CACHE.get(cache_key)
    if cached is not None and (completed_season or
            time.time() - _NBA_GAMELOG_CUR_TS.get(cache_key, 0) < _NBA_GAMELOG_CUR_TTL):
        return cached
    disk_path = _NBA_GAMELOG_DIR / f"{re.sub(r'[^0-9A-Za-z]', '', str(player_id))}_{int(season)}.json"
    if completed_season:
        try:
            if disk_path.exists():
                games = json.loads(disk_path.read_text(encoding="utf-8"))
                if isinstance(games, list):
                    _NBA_GAMELOG_CACHE[cache_key] = games
                    return games
        except Exception:
            pass
    url = (f"https://site.web.api.espn.com/apis/common/v3/sports/"
           f"basketball/nba/athletes/{player_id}/gamelog")
    gl = None
    for attempt in range(3):
        retry = False
        async with sem:
            try:
                if client is None:
                    async with httpx.AsyncClient(timeout=8) as c:
                        r = await c.get(url, params={'season': season})
                else:
                    r = await client.get(url, params={'season': season})
                if r.status_code == 200:
                    gl = r.json()
                elif r.status_code == 429 or r.status_code >= 500:
                    retry = True
                else:
                    return []
            except Exception:
                retry = True
        if gl is not None or not retry:
            break
        # ESPN throttling/timeouts: back off and try again instead of
        # silently losing this player's season.
        await asyncio.sleep(0.6 * (attempt + 1) + random.random() * 0.4)
    if not isinstance(gl, dict):
        return []

    events = gl.get('events', {})

    # Build eventId → stats map from seasonTypes → categories → events
    stats_map: Dict[str, Dict] = {}
    for st in gl.get('seasonTypes', []):
        # WHITELIST: only count Regular Season + Postseason. Excludes preseason,
        # summer league, NBA Cup / In-Season Tournament, exhibitions, etc.
        st_name = (st.get('displayName') or st.get('name') or '').lower()
        if not ('regular' in st_name or 'post' in st_name or 'playoff' in st_name):
            continue
        for cat in st.get('categories', []):
            if cat is None:
                continue
            for ev in cat.get('events', []):
                eid = ev.get('eventId')
                if eid and ev.get('stats') and eid not in stats_map:
                    stats_map[eid] = {
                        "stats": ev["stats"],
                        "labels": (ev.get("labels") or cat.get("labels")
                                   or gl.get("labels") or cat.get("names")
                                   or gl.get("names") or []),
                    }

    games = []
    for eid, ev_info in events.items():
        if eid not in stats_map:
            continue
        stat_entry = stats_map[eid]
        stats = stat_entry["stats"]
        labels = stat_entry.get("labels") or []
        if not labels or len(stats) != len(labels):
            continue

        # Skip garbage time / DNP games
        min_idx = next((i for i, label in enumerate(labels)
                        if str(label).upper() == "MIN"), 0)
        if parse_min(stats[min_idx]) < MIN_MINUTES:
            continue
        # Missing/misaligned provider values are not zero-stat performances.
        base_stats = {key: _nba_extract_stat(stats, key, labels)
                      for key in ("PTS", "REB", "AST", "FG3M", "BLK", "STL")}
        if any(value is None for value in base_stats.values()):
            continue

        opp_info = ev_info.get('opponent', {})
        opp_abbr = _norm_abbr(opp_info.get('abbreviation', '') if isinstance(opp_info, dict) else '')
        location = 'Away' if ev_info.get('atVs', '') == '@' else 'Home'
        team_info = ev_info.get('team', {})
        player_team_abbr = _norm_abbr(team_info.get('abbreviation', '') if isinstance(team_info, dict) else '')

        games.append({
            'opp':         opp_abbr,
            'location':    location,
            'date':        ev_info.get('gameDate', ''),
            'player_team': player_team_abbr,
            'MIN':         parse_min(stats[min_idx]),
            'PTS':         _nba_extract_stat(stats, 'PTS', labels) or 0,
            'REB':         _nba_extract_stat(stats, 'REB', labels) or 0,
            'AST':         _nba_extract_stat(stats, 'AST', labels) or 0,
            'FG3M':        _nba_extract_stat(stats, 'FG3M', labels) or 0,
            'PRA':         sum((_nba_extract_stat(stats, k, labels) or 0) for k in ('PTS','REB','AST')),
            'PTS_REB':     sum((_nba_extract_stat(stats, k, labels) or 0) for k in ('PTS','REB')),
            'PTS_AST':     sum((_nba_extract_stat(stats, k, labels) or 0) for k in ('PTS','AST')),
            'REB_AST':     sum((_nba_extract_stat(stats, k, labels) or 0) for k in ('REB','AST')),
            'BLK':         _nba_extract_stat(stats, 'BLK', labels) or 0,
            'STL':         _nba_extract_stat(stats, 'STL', labels) or 0,
        })
    _NBA_GAMELOG_CACHE[cache_key] = games
    if completed_season:
        try:
            tmp = disk_path.with_name(disk_path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(games), encoding="utf-8")
            os.replace(tmp, disk_path)
        except Exception as exc:
            print(f"[nba_gamelog] disk cache write failed: {exc}")
    else:
        _NBA_GAMELOG_CUR_TS[cache_key] = time.time()
    return games

# ─── Analysis ─────────────────────────────────────────────────────────────────

def _nn(n):
    import unicodedata as ud, re
    s = ud.normalize('NFD', n).encode('ascii','ignore').decode().lower()
    return re.sub(r'[^a-z ]', '', s).strip()

def _nm(a, b):
    na, nb = _nn(a), _nn(b)
    if na == nb: return True
    pa, pb = na.split(), nb.split()
    return len(pa) >= 2 and len(pb) >= 2 and pa[0][0] == pb[0][0] and pa[-1] == pb[-1]


async def get_underdog_lines():
    """DEPRECATED — Underdog removed. Odds API is the sole line source."""
    return []

async def get_odds_lines(today_str, alternate_only: bool = False):
    api_key = os.environ.get('ODDS_API_KEY', '')
    if not api_key:
        return []
    # Alternates are deliberately live-only. Never let a caller turn a
    # historical/replay request into a new alternate-market Odds API request.
    if alternate_only and today_str < _nba_today().isoformat():
        return []
    props = []
    alternate_props = []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            # Event filter: keep an event iff its US-Eastern calendar date equals the
            # SELECTED date. Converting to ET first correctly handles a late ET tip that
            # rolls into the next UTC day. Unlike a now-relative rolling window, scoping to
            # the selected day lets admins pull lines for a game days out (the date picker is
            # unlocked for them) while keeping each player's props bound to the right game —
            # no cross-day overwrite from intermediate-day events. Non-admins are capped at
            # tomorrow by the picker, so they only ever see today's/tomorrow's slate.
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            try:
                from zoneinfo import ZoneInfo as _ZI
                _ET = _ZI("America/New_York")
            except Exception:
                _ET = _tz(-_td(hours=4))  # EDT fallback if the tz database is unavailable

            def _in_window(iso_ts: str) -> bool:
                try:
                    t = _dt.fromisoformat(iso_ts.replace('Z', '+00:00'))
                    return t.astimezone(_ET).strftime('%Y-%m-%d') == today_str
                except Exception:
                    return False

            events = []
            active_key = 'basketball_nba'
            for sport_key in ('basketball_nba', 'basketball_nba_championship'):
                r = await c.get(f"{ODDS_API_BASE}/sports/{sport_key}/events",
                                params={'apiKey': api_key, 'dateFormat': 'iso'})
                if r.status_code == 200:
                    raw = r.json()
                    found = [e for e in raw if _in_window(e.get('commence_time', ''))]
                    print(f'[OddsAPI] {sport_key}: {len(raw)} total events, {len(found)} in window')
                    if found:
                        events = found
                        active_key = sport_key
                        print(f'[OddsAPI] {len(events)} NBA events ({sport_key}) for {today_str}')
                        break
                else:
                    print(f'[OddsAPI] events {r.status_code} for {sport_key}: {r.text[:150]}')
            if not events:
                print(f'[OddsAPI] No NBA events found for {today_str}')
                return []
            # Do not add alternates to a past-date request. This endpoint is a
            # live ingestion path; standard-only handling for historical inputs
            # remains exactly as it was.
            is_live_run = today_str >= _nba_today().isoformat()
            requested_market_map = (
                dict(ODDS_ALTERNATE_MARKET_MAP) if alternate_only
                else dict(ODDS_MARKET_MAP))
            if is_live_run and not alternate_only:
                requested_market_map.update(ODDS_ALTERNATE_MARKET_MAP)
            markets = ','.join(requested_market_map.keys())
            for ev in events:
                r2 = await c.get(
                    f"{ODDS_API_BASE}/sports/{active_key}/events/{ev['id']}/odds",
                    params={'apiKey': api_key, 'regions': LIVE_ODDS_REGIONS,
                            'markets': markets, 'oddsFormat': 'american'})
                if r2.status_code != 200:
                    print(f'[OddsAPI] props {r2.status_code} for {ev.get("home_team","?")} game: {r2.text[:150]}')
                    continue
                data = r2.json()
                seen = set()
                seen_alternates = set()
                _books = sorted(data.get('bookmakers', []) or [],
                                key=lambda b: _NBA_BOOK_PRIORITY.get(b.get('key', ''), 999))
                for book in _books:
                    for mkt in book.get('markets', []):
                        market_key = mkt.get('key', '')
                        stat = requested_market_map.get(market_key)
                        if not stat:
                            continue
                        is_alternate = market_key in ODDS_ALTERNATE_MARKET_MAP
                        # Collect BOTH Over and Under prices per player for this market.
                        by_player = {}
                        for oc in mkt.get('outcomes', []):
                            nm = oc.get('name')
                            if nm not in ('Over', 'Under'):
                                continue
                            player = oc.get('description', '').strip()
                            line   = float(oc.get('point') or 0)
                            if not player or line <= 0:
                                continue
                            outcome_key = ((player, line) if is_alternate else player)
                            d = by_player.setdefault(
                                outcome_key, {'player': player, 'line': line})
                            if not is_alternate:
                                d['line'] = line
                            if nm == 'Over':
                                d['over_odds'] = str(oc.get('price', ''))
                            else:
                                d['under_odds'] = str(oc.get('price', ''))
                        for _, d in by_player.items():
                            player = d['player']
                            if 'over_odds' not in d:   # require an Over line to register (mirrors prior behavior)
                                continue
                            # Alternate ladders need their point in the identity;
                            # standard rows preserve the existing player/stat
                            # first-seen behavior used by the calibrated boards.
                            key = (f"{player}|{stat}|{d['line']}"
                                   if is_alternate else f"{player}|{stat}")
                            target = alternate_props if is_alternate else props
                            target_seen = seen_alternates if is_alternate else seen
                            if key not in target_seen:
                                target_seen.add(key)
                                target.append({
                                    'player': player, 'stat': stat, 'line': d['line'],
                                    'odds': d.get('over_odds', ''),
                                    'over_odds': d.get('over_odds', ''),
                                    'under_odds': d.get('under_odds', ''),
                                     'bookmaker': book.get('key', ''),
                                     'bookmaker_label': (
                                         book.get('title') or book.get('key', '')),
                                    'home': data.get('home_team', ''),
                                    'away': data.get('away_team', ''),
                                    'source_market': market_key,
                                    'is_alternate': is_alternate,
                                })
                    # check all bookmakers for best coverage
    except Exception as e:
        print(f'[OddsAPI] error: {e}')
    # Alternate rows are retained independently for a future alternate consumer.
    # They are deliberately not returned to the existing standard-board inputs.
    if alternate_props:
        _cache_set('nba_alternates', today_str, {'date': today_str,
                   'props': alternate_props})
    print(f'[OddsAPI] {len(props)} NBA prop lines fetched; {len(alternate_props)} alternates warmed')
    return alternate_props if alternate_only else props


def _nba_hist_file(kind: str, key: str) -> pathlib.Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return _CACHE_DIR / f"nba_historical_{kind}_{safe}.json"


def _nba_hist_read(kind: str, key: str):
    try:
        p = _nba_hist_file(kind, key)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[NBA historical] cache read failed: {e}")
    return None


def _nba_hist_write(kind: str, key: str, value):
    p = _nba_hist_file(kind, key)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        print(f"[NBA historical] cache write failed: {e}")
        try: tmp.unlink(missing_ok=True)
        except Exception: pass


def _nba_hist_pick_snapshot(games: list) -> Optional[str]:
    """Return an ISO instant strictly before the earliest ESPN scheduled tip."""
    tips = []
    for g in games:
        try:
            tips.append(datetime.fromisoformat(g.get("tipoff", "").replace("Z", "+00:00")))
        except Exception:
            pass
    if not tips:
        return None
    earliest = min(tips)
    if earliest.tzinfo is None:
        from datetime import timezone
        earliest = earliest.replace(tzinfo=timezone.utc)
    return (earliest - timedelta(seconds=1)).astimezone(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")


async def _nba_hist_events(date_str: str, snapshot: str, api_key: str):
    key = f"{date_str}_{snapshot}"
    cached = _nba_hist_read("events", key)
    if cached is not None:
        return cached.get("data", cached) if isinstance(cached, dict) else cached
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.get(f"{ODDS_API_BASE}/historical/sports/basketball_nba/events",
                        params={"apiKey": api_key, "date": snapshot,
                                "dateFormat": "iso"})
        if r.status_code != 200:
            raise RuntimeError(f"historical event list unavailable ({r.status_code})")
        payload = r.json()
    # Cache the complete immutable event list before any analysis.
    _nba_hist_write("events", key, payload)
    return payload.get("data", payload) if isinstance(payload, dict) else payload


async def _nba_hist_event_odds(event_id: str, snapshot: str, api_key: str,
                               market_family: str = "standard"):
    """Fetch one immutable archived event response, isolated by market family."""
    market_family = "alternate" if market_family == "alternate" else "standard"
    key = f"{market_family}_{event_id}_{snapshot}"
    cached = _nba_hist_read("odds", key)
    if cached is not None:
        return cached.get("data", cached) if isinstance(cached, dict) else cached
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{ODDS_API_BASE}/historical/sports/basketball_nba/events/{event_id}/odds",
                        params={"apiKey": api_key, "date": snapshot,
                                "regions": HISTORICAL_ODDS_REGION,
                                "markets": ",".join(
                                    ODDS_ALTERNATE_MARKET_MAP if market_family == "alternate"
                                    else ODDS_MARKET_MAP),
                                "oddsFormat": "american", "dateFormat": "iso"})
        if r.status_code != 200:
            raise RuntimeError(f"historical odds unavailable for event {event_id} ({r.status_code})")
        payload = r.json()
    # Full response is permanent: never make a second paid request for it.
    _nba_hist_write("odds", key, payload)
    return payload.get("data", payload) if isinstance(payload, dict) else payload


async def _nba_hist_event_odds_family(event_id: str, snapshot: str, api_key: str,
                                      market_family: str = "standard"):
    return await _nba_hist_event_odds(event_id, snapshot, api_key, market_family)


def _nba_hist_alternate_props(payload: dict, game: dict) -> list:
    """Retain every exact two-sided archived alternate quote independently."""
    rows = []
    for book in payload.get("bookmakers", []):
        for market in book.get("markets", []):
            stat = ODDS_ALTERNATE_MARKET_MAP.get(market.get("key"))
            if not stat:
                continue
            quotes = {}
            for outcome in market.get("outcomes", []):
                if outcome.get("name") not in ("Over", "Under") or not outcome.get("description"):
                    continue
                try:
                    line = float(outcome["point"])
                    price = outcome["price"]
                except Exception:
                    continue
                if line <= 0 or _nba_hist_price_probability(price) is None:
                    continue
                key = (outcome["description"].strip(), line)
                quotes.setdefault(key, {})[outcome["name"].lower()] = str(price)
            for (player, line), sides in quotes.items():
                if "over" not in sides or "under" not in sides:
                    continue
                rows.append({
                    "player": player, "stat": stat, "line": line,
                    "over_odds": sides["over"], "under_odds": sides["under"],
                    "odds": sides["over"], "side": "OVER",
                    "event_id": game.get("event_id"),
                    "home": game.get("home_name", ""), "away": game.get("away_name", ""),
                    "home_abbr": game.get("home", ""), "away_abbr": game.get("away", ""),
                    "book": book.get("title") or book.get("key") or "Archived sportsbook",
                    "book_key": book.get("key") or book.get("title") or "Archived sportsbook",
                    "quote_updated": market.get("last_update") or book.get("last_update"),
                    "source_market": market.get("key"), "historical_replay": True,
                    "is_alternate": True,
                })
    return rows


def _nba_hist_price_probability(price) -> Optional[float]:
    try:
        p = float(price)
        if p < -1000:
            return None
        return 100.0 / (p + 100.0) if p > 0 else abs(p) / (abs(p) + 100.0)
    except Exception:
        return None


def _nba_hist_team_key(name: str) -> str:
    key = _nn(name)
    # ESPN uses "LA Clippers"; The Odds API uses "Los Angeles Clippers".
    if key in ("la clippers", "los angeles clippers"):
        return "clippers"
    return key


def _nba_hist_props(payload: dict, game: dict) -> list:
    """Select a consensus standard line, retaining one exact book quote.

    Each book gets one vote: its most balanced two-sided standard quote.
    Consensus breaks first, then price balance; juice never earns preference.
    Alternate-market keys are deliberately absent from ODDS_MARKET_MAP.
    """
    candidates = {}
    for book in payload.get("bookmakers", []):
        for market in book.get("markets", []):
            stat = ODDS_MARKET_MAP.get(market.get("key"))
            if not stat:
                continue
            rows = {}
            for oc in market.get("outcomes", []):
                if oc.get("name") not in ("Over", "Under") or not oc.get("description"):
                    continue
                try: line = float(oc["point"])
                except Exception: continue
                price = oc.get("price")
                if _nba_hist_price_probability(price) is None:
                    continue
                d = rows.setdefault((oc["description"].strip(), line), {})
                d[oc["name"].lower()] = str(price)
            for (player, line), sides in rows.items():
                if "over" not in sides or "under" not in sides:
                    continue
                op = _nba_hist_price_probability(sides["over"])
                up = _nba_hist_price_probability(sides["under"])
                side = "OVER" if op >= up else "UNDER"
                price = sides["over"] if side == "OVER" else sides["under"]
                # One historical pick per player/category/game. Sportsbooks can
                # post different standard lines for the same market; treating
                # every line as another pick inflates one player into dozens of
                # duplicate rows and makes category boards unusable.
                ident = (_nn(player), stat, game.get("event_id"))
                candidate = {"player": player, "stat": stat, "line": line,
                            "over_odds": sides["over"], "under_odds": sides["under"],
                            "odds": price, "side": side, "event_id": game.get("event_id"),
                            "home": game.get("home_name", ""), "away": game.get("away_name", ""),
                             "home_abbr": game.get("home", ""), "away_abbr": game.get("away", ""),
                             "book": book.get("title") or book.get("key") or "Archived sportsbook",
                             "book_key": book.get("key") or book.get("title") or "Archived sportsbook",
                             "quote_updated": market.get("last_update") or book.get("last_update"),
                             "source_market": market.get("key"), "historical_replay": True}
                candidates.setdefault(ident, []).append(candidate)

    def balance_key(quote):
        over = _nba_hist_price_probability(quote["over_odds"])
        under = _nba_hist_price_probability(quote["under_odds"])
        # Normalize out the book's margin before comparing price balance.
        return (abs(over / (over + under) - 0.5),
                abs(over + under - 1), quote["book_key"], quote["line"])

    selected = []
    for quotes in candidates.values():
        per_book = {}
        for quote in quotes:
            old = per_book.get(quote["book_key"])
            if old is None or balance_key(quote) < balance_key(old):
                per_book[quote["book_key"]] = quote
        votes = {}
        for quote in per_book.values():
            votes[quote["line"]] = votes.get(quote["line"], 0) + 1
        chosen = min(per_book.values(), key=lambda q: (
            -votes[q["line"]], *balance_key(q)))
        chosen["line_selection"] = "standard_book_consensus"
        chosen["line_book_count"] = votes[chosen["line"]]
        chosen["available_book_count"] = len(per_book)
        selected.append(chosen)
    return selected


async def _nba_historical_odds_uncached(date_str: str, games: list):
    if date_str < HISTORICAL_ODDS_MIN_DATE:
        return {"ok": False, "error": "Historical Sportsbook Replay is supported from 2023-05-03 onward only.", "props": []}
    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "Historical Sportsbook Replay unavailable: Odds API key is not configured.", "props": []}
    snapshot = _nba_hist_pick_snapshot(games)
    if not snapshot:
        return {"ok": False, "error": "Historical Sportsbook Replay unavailable: ESPN tipoff timestamp missing.", "props": []}
    try:
        events = await asyncio.wait_for(_nba_hist_events(date_str, snapshot, api_key),
                                        timeout=HISTORICAL_ODDS_TIMEOUT)
        # Match archived events only to ESPN's selected slate; ESPN remains the
        # authority for tipoffs and event matching.
        # ESPN and Odds API use different event IDs.  Match only by the ESPN
        # schedule's home/away participants (and selected slate), then use the
        # archived Odds event ID for the paid odds request.
        wanted = {(_nba_hist_team_key(g.get("home_name", "")),
                   _nba_hist_team_key(g.get("away_name", ""))): g
                  for g in games}
        expected_games = len(wanted)
        matched = []
        for ev in events if isinstance(events, list) else []:
            pair = (_nba_hist_team_key(ev.get("home_team", "")),
                    _nba_hist_team_key(ev.get("away_team", "")))
            if pair in wanted: matched.append((wanted.pop(pair), ev))
        if wanted or len(matched) != expected_games:
            return {"ok": False, "error": "Historical Sportsbook Replay incomplete: archived event list did not cover the ESPN slate.", "props": [], "snapshot": snapshot}
        sem = asyncio.Semaphore(HISTORICAL_ODDS_CONCURRENCY)
        async def one(game, ev):
            async with sem:
                payload = await _nba_hist_event_odds(str(ev["id"]), snapshot, api_key)
                if not isinstance(payload, dict) or "bookmakers" not in payload:
                    raise RuntimeError(f"historical odds response incomplete for event {ev.get('id')}")
                return game, payload
        pairs = await asyncio.wait_for(asyncio.gather(*(one(g, e) for g, e in matched)),
                                       timeout=HISTORICAL_ODDS_TIMEOUT)
        props = []
        for game, payload in pairs:
            props.extend(_nba_hist_props(payload, game))
        if len(pairs) != expected_games:
            return {"ok": False, "error": "Historical Sportsbook Replay incomplete: one or more event responses failed.", "props": [], "snapshot": snapshot}
        return {"ok": True, "props": props, "snapshot": snapshot}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Historical Sportsbook Replay timed out before all archived event responses completed.", "props": [], "snapshot": snapshot}
    except Exception as e:
        return {"ok": False, "error": f"Historical Sportsbook Replay unavailable: {e}", "props": [], "snapshot": snapshot}


async def _nba_historical_odds(date_str: str, games: list):
    """Deduplicate concurrent clicks so an event response is purchased once."""
    key = date_str
    task = _NBA_HIST_INFLIGHT.get(key)
    if task is not None and not task.done():
        return await task
    task = asyncio.create_task(_nba_historical_odds_uncached(date_str, games))
    _NBA_HIST_INFLIGHT[key] = task
    try:
        return await task
    finally:
        if _NBA_HIST_INFLIGHT.get(key) is task:
            _NBA_HIST_INFLIGHT.pop(key, None)


async def _nba_historical_alternates_uncached(date_str: str, games: list):
    """Warm genuine archived alternate lines without touching the replay board."""
    if date_str < HISTORICAL_ODDS_MIN_DATE:
        return {"ok": False, "error": "Archived alternate lines are supported from 2023-05-03 onward.",
                "props": []}
    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "Archived alternate lines unavailable: Odds API key is not configured.",
                "props": []}
    snapshot = _nba_hist_pick_snapshot(games)
    if not snapshot:
        return {"ok": False, "error": "Archived alternate lines unavailable: ESPN tipoff timestamp missing.",
                "props": []}
    try:
        # Reuse the standard replay's immutable event-list request/cache.
        events = await asyncio.wait_for(_nba_hist_events(date_str, snapshot, api_key),
                                        timeout=HISTORICAL_ODDS_TIMEOUT)
        wanted = {(_nba_hist_team_key(g.get("home_name", "")),
                   _nba_hist_team_key(g.get("away_name", ""))): g for g in games}
        matched = []
        for ev in events if isinstance(events, list) else []:
            pair = (_nba_hist_team_key(ev.get("home_team", "")),
                    _nba_hist_team_key(ev.get("away_team", "")))
            if pair in wanted:
                matched.append((wanted.pop(pair), ev))
        if wanted or len(matched) != len(games):
            return {"ok": False, "error": "Archived alternate lines unavailable: archived event list did not cover the ESPN slate.",
                    "props": [], "snapshot": snapshot}
        sem = asyncio.Semaphore(HISTORICAL_ODDS_CONCURRENCY)
        async def one(game, ev):
            async with sem:
                payload = await _nba_hist_event_odds(
                    str(ev["id"]), snapshot, api_key, "alternate")
                if not isinstance(payload, dict) or "bookmakers" not in payload:
                    raise RuntimeError(f"archived alternate response incomplete for event {ev.get('id')}")
                return _nba_hist_alternate_props(payload, {**game, "event_id": ev.get("id")})
        result = await asyncio.wait_for(asyncio.gather(*(one(g, e) for g, e in matched)),
                                        timeout=HISTORICAL_ODDS_TIMEOUT)
        props = [row for group in result for row in group]
        doc = {"date": date_str, "props": props, "snapshot": snapshot,
               "ok": True, "historical_alternates": True}
        _cache_set("nba_alternates", date_str, doc)
        return doc
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Archived alternate lines timed out before all event responses completed.",
                "props": [], "snapshot": snapshot}
    except Exception as exc:
        return {"ok": False, "error": f"Archived alternate lines unavailable: {exc}",
                "props": [], "snapshot": snapshot}


async def _nba_historical_alternates(date_str: str, games: list):
    """Shared inflight dedup for on-demand historical Coach alternate warming."""
    key = date_str
    task = _NBA_ALT_WARM_INFLIGHT.get(key)
    if task is not None and not task.done():
        return await task
    cached = _cache_get("nba_alternates", date_str)
    if isinstance(cached, dict) and cached.get("historical_alternates"):
        return cached
    task = asyncio.create_task(_nba_historical_alternates_uncached(date_str, games))
    _NBA_ALT_WARM_INFLIGHT[key] = task
    try:
        return await task
    finally:
        if _NBA_ALT_WARM_INFLIGHT.get(key) is task:
            _NBA_ALT_WARM_INFLIGHT.pop(key, None)


async def _nba_espn_player_identity(player_name: str, client) -> dict:
    """Resolve an archived player when the selected box has no athlete row."""
    cache_key = _nn(player_name)
    if cache_key in _NBA_PLAYER_IDENTITY_CACHE:
        return _NBA_PLAYER_IDENTITY_CACHE[cache_key]
    identity = {}
    try:
        response = await client.get(
            "https://site.api.espn.com/apis/search/v2",
            params={"query": player_name, "limit": 10})
        if response.status_code == 200:
            payload = response.json()
            contents = [
                item
                for group in payload.get("results", [])
                if group.get("type") == "player"
                for item in group.get("contents", [])
            ]
            match = next((
                item for item in contents
                if str(item.get("description") or "").upper() == "NBA"
                and _nm(item.get("displayName", ""), player_name)
            ), None)
            if match:
                found = re.search(
                    r"(?:^|~)a:(\d+)", str(match.get("uid") or ""))
                image = match.get("image") or {}
                identity = {
                    "player_id": found.group(1) if found else None,
                    "headshot": (
                        image.get("default", "")
                        if isinstance(image, dict) else ""),
                    "team_name": match.get("subtitle") or "",
                }
    except Exception as exc:
        print(f"[nba_history] player identity failed for {player_name}: {exc}")
    _NBA_PLAYER_IDENTITY_CACHE[cache_key] = identity
    return identity


async def _nba_historical_board(date_str: str, games: list, log: list) -> dict:
    """Build a priced, immutable replay board; never enters live tracking."""
    if date_str < HISTORICAL_ODDS_MIN_DATE:
        msg = "Historical Sportsbook Replay unavailable before 2023-05-03 (no Odds API calls made)."
        return {"date": date_str, "picks": [], "all_picks": [], "games": games,
                "log": log + [msg], "total": 0, "odds_loaded": False,
                "historical_replay": True, "historical_unavailable": True,
                "historical_replay_schema": HISTORICAL_REPLAY_SCHEMA,
                "props_picks": [], "props_nopick": []}
    archived = await _nba_historical_odds(date_str, games)
    if not archived.get("ok"):
        return {"date": date_str, "picks": [], "all_picks": [], "games": games,
                "log": log + [archived.get("error", "Historical replay unavailable.")],
                "total": 0, "odds_loaded": False, "historical_replay": True,
                "historical_unavailable": True, "historical_snapshot": archived.get("snapshot"),
                "historical_replay_schema": HISTORICAL_REPLAY_SCHEMA,
                "props_picks": [], "props_nopick": []}
    box = _nba_box_lookup(date_str)
    unresolved_names = sorted({
        str(prop.get("player") or "").strip()
        for prop in archived["props"]
        if prop.get("player")
        and not any(_nm(name, prop.get("player", "")) for name in box)
    })
    if unresolved_names:
        identity_sem = asyncio.Semaphore(12)

        async def resolve_identity(player_name, client):
            async with identity_sem:
                return player_name, await _nba_espn_player_identity(
                    player_name, client)

        try:
            async with httpx.AsyncClient(timeout=8) as identity_client:
                resolved = await asyncio.wait_for(
                    asyncio.gather(*[
                        resolve_identity(name, identity_client)
                        for name in unresolved_names
                    ]), timeout=20)
            team_abbr_by_name = {}
            for game in games:
                team_abbr_by_name[_nba_hist_team_key(
                    game.get("home_name", ""))] = game.get("home", "")
                team_abbr_by_name[_nba_hist_team_key(
                    game.get("away_name", ""))] = game.get("away", "")
            for player_name, identity in resolved:
                if not identity.get("player_id"):
                    continue
                team_abbr = team_abbr_by_name.get(
                    _nba_hist_team_key(identity.get("team_name", "")), "")
                box.setdefault(player_name.lower().strip(), {
                    "player_id": identity["player_id"],
                    "headshot": identity.get("headshot") or "",
                    "team": team_abbr,
                })
            log.append(
                f"Resolved ESPN identities for "
                f"{sum(1 for _, item in resolved if item.get('player_id'))}/"
                f"{len(unresolved_names)} archived players missing from the box")
        except asyncio.TimeoutError:
            log.append(
                "Some archived player pictures could not be resolved before "
                "the ESPN identity deadline.")
    historical_logs = {}
    if box:
        # The archived sportsbook feed identifies players but has no statistical
        # history. Load only seasons that ended on/before the replay context and
        # later filter every game strictly before the selected date.
        replay_date = datetime.strptime(date_str, "%Y-%m-%d")
        replay_season = replay_date.year + (1 if replay_date.month >= 10 else 0)
        replay_seasons = [replay_season - offset for offset in range(7)]
        player_ids = sorted({
            str(data.get("player_id"))
            for data in box.values()
            if data.get("player_id")
        })
        sem = asyncio.Semaphore(36)

        async def one_player(pid, client):
            season_results = await asyncio.gather(*[
                get_player_gamelogs_espn(pid, season, sem, client)
                for season in replay_seasons
            ], return_exceptions=True)
            games_out = [
                game for result in season_results if isinstance(result, list)
                for game in result
                if str(game.get("date", ""))[:10] < date_str
            ]
            games_out.sort(key=lambda game: str(game.get("date", "")), reverse=True)
            return pid, games_out

        # Small quota-free test doubles may expose only synchronous ESPN get().
        # In that case retain the sportsbook rows and leave history unavailable.
        if hasattr(httpx, "AsyncClient"):
            limits = (httpx.Limits(max_connections=40, max_keepalive_connections=36)
                      if hasattr(httpx, "Limits") else None)
            client_kwargs = {"timeout": 8}
            if limits is not None:
                client_kwargs["limits"] = limits
            async with httpx.AsyncClient(**client_kwargs) as client:
                tasks = {
                    asyncio.create_task(one_player(pid, client)): pid
                    for pid in player_ids
                }
                done, pending = await asyncio.wait(tasks, timeout=40)
                for task in done:
                    try:
                        pid, games_out = task.result()
                        historical_logs[pid] = games_out
                    except Exception:
                        pass
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        log.append(
            f"Loaded pre-replay opponent history for "
            f"{len(historical_logs)}/{len(player_ids)} archived players")
        if len(historical_logs) < len(player_ids):
            log.append(
                "The ESPN history deadline was reached; completed player "
                "histories were kept and unfinished players were excluded "
                "from historical picks.")
    rows = []
    for prop in archived["props"]:
        player_name = prop.get("player") or ""
        player_box = box.get(player_name.lower().strip(), {}) if box else {}
        if not player_box and box:
            player_box = next((data for name, data in box.items()
                               if _nm(name, player_name)), {})
        actual = player_box.get(prop["stat"])
        result = None
        if actual is not None:
            result = "PUSH" if actual == prop["line"] else (
                "WIN" if ((prop["side"] == "OVER" and actual > prop["line"]) or
                          (prop["side"] == "UNDER" and actual < prop["line"])) else "LOSS")
        row = {**prop, "stat_label": STAT_CONFIG[prop["stat"]]["label"],
               "emoji": STAT_CONFIG[prop["stat"]]["emoji"], "line": prop["line"],
               "pick": prop["side"], "fd_line": prop["line"], "fd_odds": prop["odds"],
               "dk_line": prop["line"], "dk_over_odds": prop["over_odds"],
               "dk_under_odds": prop["under_odds"], "actual": actual,
               "player_id": player_box.get("player_id"),
               "headshot": player_box.get("headshot") or "",
               "team": player_box.get("team") or "",
               "result": result, "historical_snapshot": archived.get("snapshot"),
               "historical_replay": True, "model_edge": False,
               "matchup": f"{prop.get('away','')} @ {prop.get('home','')}"}
        if row["team"] == prop.get("home_abbr"):
            row["opp"] = prop.get("away_abbr") or prop.get("away", "")
            row["location"] = "Home"
        elif row["team"] == prop.get("away_abbr"):
            row["opp"] = prop.get("home_abbr") or prop.get("home", "")
            row["location"] = "Away"
        else:
            row["opp"] = ""
            row["location"] = ""
        player_logs = historical_logs.get(str(row.get("player_id")), [])
        recent_minutes = [
            float(game["MIN"]) for game in player_logs[:10]
            if game.get("MIN") is not None
        ]
        row["mpg"] = (round(sum(recent_minutes) / len(recent_minutes), 1)
                      if recent_minutes else None)
        row["rotation_priority"] = bool(
            row["mpg"] is not None and row["mpg"] >= 24)
        opponent_logs = [
            game for game in player_logs
            if game.get("opp") == row["opp"]
            and game.get("location") == row["location"]
            and game.get("player_team") == row["team"]
        ][:8]
        values = [float(game[prop["stat"]]) for game in opponent_logs]
        row["avg"] = round(sum(values) / len(values), 1) if values else None
        row["games"] = len(values)
        row["history"] = ",".join(
            str(int(value)) if value.is_integer() else str(value)
            for value in values)
        row["glog"] = [
            {"d": game.get("date", ""), "v": game[prop["stat"]]}
            for game in opponent_logs
        ]
        # Historical replay follows the same evidence rule as the sport apps:
        # a pick must come from pre-replay history versus this opponent at this
        # location. Sportsbook juice alone never chooses the displayed side.
        signal = best_bet_at_line(prop["line"], values) if values else None
        history_side = (
            signal.get("side")
            if signal and signal.get("conf") in ("STRONG", "LEAN")
            and signal.get("side") in ("OVER", "UNDER")
            else None
        )
        row["side"] = history_side
        row["pick"] = history_side
        row["pct"] = signal.get("pct") if history_side else None
        row["hits"] = signal.get("hits") if history_side else 0
        row["model_edge"] = bool(history_side)
        if history_side:
            row["fd_odds"] = (
                prop["over_odds"] if history_side == "OVER"
                else prop["under_odds"])
            actual = row.get("actual")
            row["result"] = (
                "PUSH" if actual == prop["line"] else
                ("WIN" if (
                    (history_side == "OVER" and actual > prop["line"]) or
                    (history_side == "UNDER" and actual < prop["line"])
                ) else "LOSS")
            ) if actual is not None else None
        else:
            row["fd_odds"] = None
            row["result"] = None
        rows.append(row)
    rows.sort(key=lambda x: (
        -int(x.get("rotation_priority", False)),
        -(x.get("pct") or 0), -(x.get("games") or 0),
        x.get("player", ""), x.get("stat", "")))
    qualified_rows = [row for row in rows if row.get("pick")]
    by_stat = {
        stat: [r for r in qualified_rows if r.get("stat") == stat]
        for stat in STAT_CONFIG}
    top = []
    top_players = set()
    depth = 0
    while len(top) < TOP_N:
        added = False
        for stat in STAT_CONFIG:
            pool = by_stat.get(stat) or []
            if depth < len(pool):
                added = True
                candidate = pool[depth]
                player_key = _nn(candidate.get("player", ""))
                if player_key in top_players:
                    continue
                top_players.add(player_key)
                top.append(candidate)
                if len(top) >= TOP_N:
                    break
        if not added:
            break
        depth += 1
    msg = (
        f"Historical Sportsbook Replay · archived snapshot "
        f"{archived.get('snapshot')} · {len(rows)} exact lines · "
        f"{len(qualified_rows)} opponent-history picks")
    if box:
        msg += " · final ESPN actuals attached where available"
    log = log + [msg,
        "Standard lines use book consensus with balanced-price tie-breaking; alternates stay out of the main boards.",
        "Starter/rotation priority uses 24+ average minutes in the last 10 pre-game appearances, then opponent/home-away hit rate and sample size; sportsbook juice does not rank picks.",
        "Historical replay uses archived sportsbook prices only; no pregame player-stat model edge is claimed."]
    return {"date": date_str, "picks": top, "all_picks": rows, "games": games,
            "log": log, "total": len(rows), "odds_loaded": bool(rows),
            "historical_replay": True, "historical_snapshot": archived.get("snapshot"),
            "historical_replay_schema": HISTORICAL_REPLAY_SCHEMA,
            "props_picks": qualified_rows,
            "props_nopick": [
                row for row in rows if not row.get("pick")]}


def parse_stat(val):
    s = str(val)
    if '-' in s: s = s.split('-')[0]
    try: return int(float(s))
    except: return 0

def parse_min(val):
    s = str(val)
    if ':' in s:
        p = s.split(':')
        try: return float(p[0]) + float(p[1])/60
        except: return 0.0
    try: return float(s)
    except: return 0.0

_ABBR_ALIASES = {
    'SA': 'SAS', 'SAS': 'SAS',
    'NO': 'NOP', 'NOP': 'NOP', 'NOH': 'NOP',
    'GS': 'GSW', 'GSW': 'GSW',
    'NY': 'NYK', 'NYK': 'NYK',
    'UTAH': 'UTA', 'UTA': 'UTA',
    'WSH': 'WAS', 'WAS': 'WAS',
    'PHX': 'PHX', 'PHO': 'PHX',
    'BKN': 'BKN', 'BRK': 'BKN',
    'CHA': 'CHA', 'CHO': 'CHA',
}
def _norm_abbr(a):
    """Normalize ESPN team abbreviation — scoreboard + gamelog APIs disagree
    on some teams (SA vs SAS, NO vs NOP, GS vs GSW, etc)."""
    if not a: return a
    return _ABBR_ALIASES.get(a.upper(), a.upper())


def _streak_pick(line, recent10, sk):
    """🔥 STREAK PICK: trailing consecutive games over the line.
    Returns ('OVER', n) if 3+ in a row over, ('UNDER', n) if 3+ in a row under, else (None, 0).
    recent10 is sorted newest-first."""
    if not line or not recent10:
        return None, 0
    over_streak = under_streak = 0
    for g in recent10:
        v = float(g[sk])
        if v > line:
            if under_streak: break
            over_streak += 1
        elif v < line:
            if over_streak: break
            under_streak += 1
        else:
            break
    if over_streak >= 3:
        return 'OVER', over_streak
    if under_streak >= 3:
        return 'UNDER', under_streak
    return None, 0


def _alt_pick(line, recent10, sk):
    """🔄 ALTERNATING PICK: on/off pattern. recent10 newest-first.
    If even-indexed games (0,2,4,6,8 = most recent + every other before)
    hit overs ≥4/5 and odd-indexed hit ≤1/5 (or vice versa), pattern is strong.
    Tonight is the NEXT game so its parity is OPPOSITE of index 0.
    Returns (rec, evens_hit_text, odds_hit_text) or (None, None, None)."""
    if not line or len(recent10) < 2:
        return None, None, None
    evens = recent10[0::2][:5]  # idx 0,2,4,6,8
    odds  = recent10[1::2][:5]  # idx 1,3,5,7,9
    e_hits = sum(1 for g in evens if float(g[sk]) > line)
    o_hits = sum(1 for g in odds  if float(g[sk]) > line)
    e_n, o_n = len(evens), len(odds)
    if e_n < 1 or o_n < 1:
        return None, None, None
    e_pct = e_hits / e_n
    o_pct = o_hits / o_n
    # Strong alternation = one side ≥70%, other ≤30%, and a clear gap
    # (catches usage/minute cycles books exploit — heavy night → light night)
    if e_pct >= 0.70 and o_pct <= 0.30:
        # Evens are HIGH cycle; tonight = odd cycle = LOW = UNDER
        return 'UNDER', f"{e_hits}/{e_n}", f"{o_hits}/{o_n}"
    if o_pct >= 0.70 and e_pct <= 0.30:
        # Odds are HIGH cycle; tonight = even cycle = LOW = UNDER... wait
        # Actually: most recent past game is idx 0 (even). Tonight = NEXT game.
        # If odds (idx 1,3,5...) are HIGH cycle and evens are LOW,
        # tonight follows the alternation → opposite of most recent = HIGH = OVER.
        return 'OVER', f"{e_hits}/{e_n}", f"{o_hits}/{o_n}"
    return None, None, None


def _line_pick(line, all_vals, last10, sk):
    """Recommend OVER/UNDER vs the sportsbook line based on last-10 hit rate.
    Returns (rec, pct, hits_text) or (None, None, None) if no line/data."""
    if not line or not last10:
        return None, None, None
    n = len(last10)
    over_hits = sum(1 for l in last10 if float(l[sk]) > line)
    under_hits = n - over_hits
    if over_hits == under_hits:
        return None, None, None
    # Require at least 70% on the dominant side to qualify
    if over_hits > under_hits:
        pct = over_hits / n
        if pct < 0.70:
            return None, None, None
        return 'OVER', round(pct * 100, 1), f"{over_hits}/{n}"
    pct = under_hits / n
    if pct < 0.70:
        return None, None, None
    return 'UNDER', round(pct * 100, 1), f"{under_hits}/{n}"


def find_best_threshold(values, thresholds):
    n = len(values)
    if n < MIN_GAMES: return None
    for t in thresholds:
        hits = sum(1 for v in values if v >= t)
        rate = hits / n
        if rate >= HIT_RATE_MIN:
            return {'threshold':t,'hits':hits,'games':n,'hit_rate':rate,'pct':round(rate*100,1)}
    return None


def pattern_at_line(values, line):
    """PATTERN consistency measured at the ACTUAL sportsbook line (OVER side).
    Returns the matchup/location over-rate, qualifying only when the player
    cleared the real betting line in >= HIT_RATE_MIN of those games. The
    'threshold' is the integer bar implied by the line (over 2.5 -> 3+), so the
    badge ("PATTERN 3/4 vs OPP") and the bet ("over 2.5") describe the same thing."""
    if line is None or not values:
        return None
    n = len(values)
    if n < MIN_GAMES:
        return None
    hits = sum(1 for v in values if v > line)
    rate = hits / n
    if rate < HIT_RATE_MIN:
        return None
    return {'threshold': int(line) + 1, 'hits': hits, 'games': n,
            'hit_rate': rate, 'pct': round(rate * 100, 1)}


def build_ladder(values, line, span=3):
    """A — Hit-rate ladder anchored to the sportsbook line. Sportsbooks only
    offer alt lines NEAR the posted number, so showing the full integer range
    (3,4,5,6...) is useless — you can never bet those. Instead we show `span`
    rungs below and above the actual book line in 1-point steps at the book's
    half-point (e.g. line 29.5 -> 26.5, 27.5, 28.5, 29.5, 30.5, 31.5, 32.5).
    Each rung = how often the player went OVER that alt line (v > rung) over the
    same opponent/location history. Returns [] when there is no book line."""
    if not values or line is None:
        return []
    n = len(values)
    out = []
    for off in range(-span, span + 1):
        rung = round(line + off, 1)
        if rung <= 0:
            continue
        hits = sum(1 for v in values if v > rung)
        out.append({'t': rung, 'hits': hits, 'games': n, 'pct': round(hits / n * 100)})
    return out


def best_bet_at_line(line, values):
    """B — Best bet at the sportsbook's ACTUAL line: over the same opponent/location
    history, the dominant side (over/under) vs that line plus its hit rate and a
    confidence label (STRONG >=70%, LEAN >=60%, else PASS)."""
    if line is None or not values:
        return None
    n = len(values)
    over = sum(1 for v in values if v > line)
    under = n - over
    if over == under:                      # even split = no edge, do not pick a side
        return {'side': 'PASS', 'line': line, 'hits': over, 'games': n, 'pct': 50, 'conf': 'PASS'}
    if over > under:
        side, hits = 'OVER', over
    else:
        side, hits = 'UNDER', under
    pct = round(hits / n * 100)
    conf = 'STRONG' if pct >= 70 else ('LEAN' if pct >= 60 else 'PASS')
    return {'side': side, 'line': line, 'hits': hits, 'games': n, 'pct': pct, 'conf': conf}


_NBA_RUNS_ACTIVE = 0


async def run_analysis(selected_date: str = None, force: bool = False) -> Dict:
    """Player-prop run. Counts active runs so the Game Predictor can stand aside."""
    global _NBA_RUNS_ACTIVE
    _NBA_RUNS_ACTIVE += 1
    try:
        return await _run_analysis_core(selected_date, force)
    finally:
        _NBA_RUNS_ACTIVE -= 1


async def _run_analysis_core(selected_date: str = None, force: bool = False) -> Dict:
    today_str = selected_date if selected_date else _nba_today().isoformat()
    historical_replay = today_str < _nba_today().isoformat()
    # File cache check first (skipped on force refresh — admin only)
    if not force:
        _fc = _cache_get('nba', today_str)
        # Do not serve the retired ESPN model-only replay as a sportsbook replay.
        if historical_replay and _fc and not _fc.get("historical_snapshot"):
            _fc = None
        if historical_replay and _fc and _fc.get("historical_replay_schema") != HISTORICAL_REPLAY_SCHEMA:
            _fc = None
        if _fc and not _nba_fg3m_logs_sane(_fc):
            _fc = None
        if _fc:
            _fc = await _nba_refresh_season_scope(today_str, _fc)
            if not historical_replay:
                _schedule_nba_alternates_warm(today_str)
            _cache.update(_fc)
            if not historical_replay:
                try: _nba_coach_capture_pregame(today_str, _fc)
                except Exception as _ce: print(f"[nba_coach_track] cached capture failed: {_ce}")
                # A saved run must also reach the Track Record (games not yet
                # tipped), even if its first save attempt failed.
                _nba_trk_bg_throttled(today_str, _fc)
            return _fc
        if (_cache.get('date') == today_str and _cache.get('picks') is not None
                and _cache.get("stat_parser_version") == 3
                and _cache.get('odds_loaded')
                and _nba_fg3m_logs_sane(_cache)
                and (not historical_replay or
                     _cache.get("historical_replay_schema") == HISTORICAL_REPLAY_SCHEMA)):
            _cache.update(await _nba_refresh_season_scope(today_str, _cache))
            if not historical_replay:
                _schedule_nba_alternates_warm(today_str)
                try: _nba_coach_capture_pregame(today_str, _cache)
                except Exception as _ce: print(f"[nba_coach_track] cached capture failed: {_ce}")
                _nba_trk_bg_throttled(today_str, dict(_cache))
            return _cache

    log = []
    log.append(f"Fetching schedule + {'archived sportsbook lines' if historical_replay else 'sportsbook lines'} for {today_str}...")

    # Games first — if there are none today (e.g. a playoff off-day), bail out
    # immediately and skip the odds fetch + entire pipeline. No slow run, no
    # failure-looking empty result.
    try:
        games = await get_today_games(today_str)
    except Exception as e:
        return {'date': today_str, 'picks': [], 'all_picks': [], 'games': [],
                'log': [f'Error: {e}'], 'total': 0}

    if not games:
        return {'date': today_str, 'picks': [], 'all_picks': [], 'games': [],
                'no_games': True,
                'log': [f'No NBA games scheduled for {today_str}.'], 'total': 0}

    if historical_replay:
        # Historical dates have a completely separate path.  It deliberately
        # does not load current rosters, Coach/Main records, bets, or live
        # odds caches: it uses ESPN schedule/final boxes, pre-date player logs,
        # and permanently cached archived Odds responses.
        try:
            saved = _nba_historical_saved_board(today_str, games, log)
        except Exception as exc:
            saved = None
            log.append(f"Saved historical replay unavailable: {exc}")
        if saved:
            saved = _nba_apply_season_scope({**saved, "games": games})
            _cache.update(saved)
            _cache_set("nba", today_str, saved)
            return saved
        result = await _nba_historical_board(today_str, games, log)
        result = _nba_apply_season_scope({**result, "games": games})
        _cache.update(result)
        if result.get("odds_loaded") and not result.get("historical_unavailable"):
            _cache_set("nba", today_str, result)
        if result.get("odds_loaded") and not result.get("historical_unavailable"):
            _nba_historical_persist_snapshot(today_str, result)
        return result
    else:
        try:
            _retry = _NBA_RETRY_ODDS.pop(today_str, None)
            if _retry and time.time() - _retry[0] < _NBA_RETRY_ODDS_TTL:
                odds_raw = _retry[1]
                log.append("OddsAPI: reusing lines from the run that stopped on ESPN")
            else:
                odds_raw = await get_odds_lines(today_str)
            odds_props = odds_raw
            log.append(f"OddsAPI: {len(odds_raw)} lines")
        except Exception as e:
            return {'date': today_str, 'picks': [], 'all_picks': [], 'games': [],
                    'log': [f'Error: {e}'], 'total': 0}

    log.append("Games: " + " | ".join(f"{g['away']} @ {g['home']}" for g in games))
    if not historical_replay:
        log.append(f"{len(odds_props)} sportsbook prop lines loaded")

    # Build lookups — odds_lookup is last-seen (compute uses bet365/us2 line
    # when available) which is the behavior the picks have been calibrated
    # against. dk_lookup below is first-seen for display.
    odds_lookup: Dict[tuple, Dict] = {}
    for prop in odds_props:
        key = (_nn(prop['player']), prop['stat'])
        odds_lookup[key] = {
            'line': prop['line'], 'odds': str(prop.get('odds', '')),
            'over_odds': str(prop.get('over_odds', '')),
            'under_odds': str(prop.get('under_odds', '')),
            'bookmaker': prop.get('bookmaker', ''),
            'bookmaker_label': prop.get('bookmaker_label', ''),
        }

    # dk_lookup uses Odds API lines as the sole sportsbook source
    dk_lookup: Dict[tuple, Dict] = {}
    for prop in odds_raw:
        key = (_nn(prop['player']), prop['stat'])
        if key not in dk_lookup:
            dk_lookup[key] = {
                'line': prop['line'],
                'over_odds': str(prop.get('over_odds', '')),
                'under_odds': str(prop.get('under_odds', '')),
                'bookmaker': prop.get('bookmaker', ''),
                'bookmaker_label': prop.get('bookmaker_label', ''),
            }

    # Map team_id -> abbreviation so we can match a player's per-game team
    # (ESPN exposes 'team.abbreviation' per gamelog event). Used to filter out
    # games the player played for a PREVIOUS team after a trade.
    tid_to_abbr: Dict[str, str] = {}
    for g in games:
        tid_to_abbr[g['home_id']] = g['home']
        tid_to_abbr[g['away_id']] = g['away']

    # Rosters
    team_ids = list({g['home_id'] for g in games} | {g['away_id'] for g in games})
    roster_results = await asyncio.gather(
        *[get_team_roster_espn(tid) for tid in team_ids], return_exceptions=True)
    rosters: Dict[str, List[Dict]] = {}
    for tid, res in zip(team_ids, roster_results):
        rosters[tid] = res if isinstance(res, list) else []
    _failed_rosters = [tid_to_abbr.get(tid, tid) for tid, res in zip(team_ids, roster_results)
                       if not isinstance(res, list)]
    if _failed_rosters:
        if not historical_replay:
            _NBA_RETRY_ODDS[today_str] = (time.time(), odds_raw)
        return {
            'date': today_str, 'picks': [], 'all_picks': [], 'games': games,
            'historical_replay': historical_replay, 'total': 0,
            'run_incomplete': True,
            'log': ['Error: ESPN did not return the roster for '
                    + ', '.join(_failed_rosters) + '. Retrying.'],
        }
    total_players = sum(len(v) for v in rosters.values())
    log.append(f"{total_players} players loaded")

    # Fetch game logs (all players, 3 seasons)
    all_player_ids = list({p['id'] for players in rosters.values() for p in players})
    if not historical_replay:
        # Live boards only analyse players with a sportsbook line posted today
        # (every live pick/prop loop skips unlined players), so fetching
        # history for the rest of the roster only slowed the run into its
        # deadline. Same picks, far fewer ESPN downloads.
        all_player_ids = list({
            p['id'] for players in rosters.values() for p in players
            if any((_nn(p['name']), s) in odds_lookup for s in STAT_CONFIG)})
    log.append(f"Fetching game logs for {len(all_player_ids)} players x {len(ESPN_SEASONS)} seasons...")
    # A full slate can exceed 200 player-season requests. Reusing one HTTP/2
    # connection pool avoids a fresh TLS handshake for every request, while the
    # bounded concurrency and hard deadline prevent an endless loading screen.
    sem = asyncio.Semaphore(36)

    async def fetch_player_logs(pid: str, client: httpx.AsyncClient):
        season_results = await asyncio.gather(
            *[get_player_gamelogs_espn(pid, s, sem, client) for s in ESPN_SEASONS],
            return_exceptions=True)
        all_logs = [g for res in season_results if isinstance(res, list) for g in res]
        return pid, all_logs

    async def fetch_all_logs():
        limits = httpx.Limits(max_connections=40, max_keepalive_connections=36)
        async with httpx.AsyncClient(timeout=8, limits=limits) as log_client:
            return await asyncio.gather(*[
                fetch_player_logs(pid, log_client) for pid in all_player_ids])

    # The download task is shielded: if the deadline passes it keeps running in
    # the background and fills the caches, so the next Get Picks completes.
    fetch_key = f"{today_str}|{historical_replay}|{len(all_player_ids)}"
    fetch_task = _NBA_LOG_FETCH_TASKS.get(fetch_key)
    if fetch_task is None or (fetch_task.done() and (fetch_task.cancelled() or fetch_task.exception())):
        fetch_task = asyncio.create_task(fetch_all_logs())
        _NBA_LOG_FETCH_TASKS[fetch_key] = fetch_task
        fetch_task.add_done_callback(
            lambda t, k=fetch_key: _NBA_LOG_FETCH_TASKS.pop(k, None)
            if _NBA_LOG_FETCH_TASKS.get(k) is t else None)
    try:
        log_results = await asyncio.wait_for(asyncio.shield(fetch_task), timeout=90)
    except asyncio.TimeoutError:
        if not historical_replay:
            _NBA_RETRY_ODDS[today_str] = (time.time(), odds_raw)
        return {
            'date': today_str, 'picks': [], 'all_picks': [], 'games': games,
            'historical_replay': historical_replay, 'total': 0,
            'run_incomplete': True,
            'log': [
                'Error: ESPN player history did not finish within 90 seconds; it keeps loading in the background. '
                'Please run this date again; completed player-season responses '
                'were cached for the retry.'
            ],
        }
    # Sort every player's games by date DESCENDING (most recent first).
    # The whole algorithm now uses "last N games in the moment" instead of
    # vs-specific-opponent history, so playoff + recent regular season games
    # flow naturally into picks.
    logs_by_player = {pid: sorted(logs, key=lambda l: l.get('date',''), reverse=True)
                      for pid, logs in log_results}
    if historical_replay:
        # A replay may only learn from ESPN games already played on or before
        # the selected date; the replayed game's own final stats must also stay
        # out of its pregame evidence.
        logs_by_player = {
            pid: [g for g in logs if str(g.get('date',''))[:10] < today_str]
            for pid, logs in logs_by_player.items()
        }
    total_entries = sum(len(v) for v in logs_by_player.values())
    log.append(f"{total_entries:,} historical game entries loaded")

    def _form_swing(logs, stat_key):
        """Display-only L5 form versus the player's own current-season baseline."""
        try:
            as_of = date.fromisoformat(today_str)
            season_start_year = as_of.year if as_of.month >= 10 else as_of.year - 1
            season_start = date(season_start_year, 10, 1).isoformat()
            season_logs = [
                row for row in logs
                if season_start <= str(row.get("date", ""))[:10] < today_str
                and row.get(stat_key) is not None
            ]
            recent = season_logs[:5]
            if len(recent) < 4 or len(season_logs) < 8:
                return {}
            recent_minutes = [float(row.get("MIN", 0) or 0) for row in recent]
            season_minutes = [float(row.get("MIN", 0) or 0) for row in season_logs]
            recent_mpg = sum(recent_minutes) / len(recent_minutes)
            season_mpg = sum(season_minutes) / len(season_minutes)
            # Avoid labeling low-minute or materially changed-role samples.
            if recent_mpg < 15 or season_mpg <= 0:
                return {}
            minute_change = (recent_mpg - season_mpg) / season_mpg
            if abs(minute_change) > 0.20:
                return {}
            recent_avg = sum(float(row[stat_key]) for row in recent) / len(recent)
            baseline_avg = sum(float(row[stat_key]) for row in season_logs) / len(season_logs)
            if baseline_avg <= 0:
                return {}
            change_pct = (recent_avg - baseline_avg) / baseline_avg * 100
            badge = "HOT" if change_pct >= 15 else "COLD" if change_pct <= -15 else None
            if not badge:
                return {}
            return {
                "form_badge": badge,
                "form_change_pct": round(change_pct, 1),
                "form_l5_avg": round(recent_avg, 1),
                "form_baseline_avg": round(baseline_avg, 1),
                "form_games": len(season_logs),
            }
        except Exception:
            return {}

    # Pattern analysis — original algorithm (find best threshold >=75%)
    log.append("Scanning matchup picks (70%+ threshold)...")
    picks = []

    for game in games:
        h, a = game['home'], game['away']
        h_name, a_name = game['home_name'], game['away_name']

        for player in rosters.get(game['home_id'], []):
            pid, pname = player['id'], player['name']
            # STARTER/ACTIVE FILTER: only live boards require a sportsbook
            # line posted today. Books drop lines for inactives and rarely post
            # lines for deep bench players. This solves "pick 1 isn't playing"
            # and the "more starters please" requests in one shot.
            has_any_line = any((_nn(pname), s) in odds_lookup for s in STAT_CONFIG)
            if not historical_replay and not has_any_line:
                continue
            # HISTORY: last 10 games vs THIS opponent at THIS location (H/A)
            # ONLY while playing for the current team (filters out games from
            # prior teams after trades — e.g. Fox's SAC home games vs OKC don't
            # count toward his SAS home record vs OKC).
            all_logs_player = logs_by_player.get(pid, [])
            # Last 10 games vs THIS opponent at THIS location (HOME). Player on
            # current team. H/A split because home vs away splits matter a lot.
            opp_logs_all = [l for l in all_logs_player
                            if l['opp'] == a and l.get('player_team') == h
                            and l.get('location') == 'Home']
            opp_logs = opp_logs_all[:10]
            for sk, sc in STAT_CONFIG.items():
                vals = [float(l[sk]) for l in opp_logs]
                last10 = opp_logs[:10]
                sb        = odds_lookup.get((_nn(pname), sk), {})
                fd_line   = None if historical_replay else sb.get('line')
                fd_odds   = None if historical_replay else sb.get('odds', '')
                l10_sb_hits = sum(1 for l in last10 if float(l[sk]) > fd_line) if fd_line and last10 else None
                dk_ob = dk_lookup.get((_nn(pname), sk), {})
                dk_line = None if historical_replay else dk_ob.get('line')
                dk_over_odds  = None if historical_replay else dk_ob.get('over_odds', '')
                dk_under_odds = None if historical_replay else dk_ob.get('under_odds', '')
                dk_hits = sum(1 for l in last10 if float(l[sk]) > dk_line) if dk_line and last10 else None
                # PATTERN: consistency measured at the ACTUAL betting line (over).
                result = (find_best_threshold(vals, sc['thresholds'])
                          if historical_replay else
                          pattern_at_line(vals, dk_line if dk_line is not None else fd_line))
                # PATTERN / LINE / STREAK: matchup + location specific.
                # MPA Special: player rhythm across all recent games (not opponent-filtered).
                recent10 = all_logs_player[:10]
                recent_vals = [float(l[sk]) for l in recent10]
                line_rec, line_rec_pct, line_rec_hits = _line_pick(dk_line, [float(l[sk]) for l in opp_logs_all[:10]], opp_logs_all[:10], sk)
                streak_rec, streak_n = _streak_pick(dk_line, opp_logs_all, sk)
                alt_rec, alt_evens, alt_odds = _alt_pick(dk_line, recent10, sk)
                # Conflict resolution: streak (matchup-specific) beats MPA Special (rhythm) when they disagree
                if streak_rec and alt_rec and streak_rec != alt_rec:
                    alt_rec = None
                # Include pick if consistency pattern OR streak OR MPA Special OR a 70%+ LINE
                # recommendation. LINE-only picks were previously dropped here; keeping them
                # opens the parlay pool up to all 70%+ starter plays (user request). They have
                # has_consistency False / hit_rate 0 so they sort below carded picks.
                if not result and not streak_rec and not alt_rec and not line_rec:
                    continue
                base = result or {'threshold': 0, 'hits': 0, 'games': len(last10),
                                  'hit_rate': 0.0, 'pct': 0.0}
                l10h = sum(1 for l in last10 if float(l[sk]) >= base['threshold']) if base['threshold'] else 0
                picks.append({**base, **_form_swing(all_logs_player, sk),
                              'player': pname, 'player_id': pid, 'team': h,
                              'team_id': game['home_id'],
                              'jersey': player.get('jersey',''), 'position': player.get('position',''),
                              'tipoff': game.get('tipoff',''),
                              'team_name': h_name, 'stat': sk,
                              'stat_label': sc['label'], 'emoji': sc['emoji'],
                              'location': 'Home', 'opp': a, 'opp_name': a_name,
                              'ladder': build_ladder(vals, dk_line if dk_line is not None else fd_line),
                              'best_bet': best_bet_at_line(dk_line if dk_line is not None else fd_line, vals),
                              'glog': [{'d': l['date'], 'v': l[sk]} for l in opp_logs],
                              'matchup': f"{a_name} @ {h_name}",
                              'l10_hits': l10h, 'l10_games': len(last10),
                              'fd_line': fd_line, 'fd_odds': fd_odds,
                              'fd_over_odds': sb.get('over_odds'), 'fd_under_odds': sb.get('under_odds'),
                              'fd_book': sb.get('bookmaker_label') or sb.get('bookmaker'),
                              'dk_book': dk_ob.get('bookmaker_label') or dk_ob.get('bookmaker'),
                              'l10_sb_hits': l10_sb_hits,
                              'dk_line': dk_line, 'dk_over_odds': dk_over_odds, 'dk_under_odds': dk_under_odds, 'dk_hits': dk_hits,
                              'line_rec': line_rec, 'line_rec_pct': line_rec_pct, 'line_rec_hits': line_rec_hits,
                              'streak_rec': streak_rec, 'streak_n': streak_n,
                              'alt_rec': alt_rec, 'alt_evens': alt_evens, 'alt_odds': alt_odds,
                              'has_consistency': result is not None,
                              'historical_replay': historical_replay,
                              'replay_threshold': result.get('threshold') if historical_replay and result else None,
                              'recent_avg': round(sum(recent_vals)/len(recent_vals), 1) if recent_vals else None,
                               'recent_glog': [{'d': l['date'], 'v': l[sk]} for l in recent10],
                              'gap': round((sum(recent_vals)/len(recent_vals)) - dk_line, 1) if recent_vals and dk_line else None,
                              'mpg': round(sum(float(l.get('MIN',0) or 0) for l in recent10)/len(recent10), 1) if recent10 else None})

        for player in rosters.get(game['away_id'], []):
            pid, pname = player['id'], player['name']
            has_any_line = any((_nn(pname), s) in odds_lookup for s in STAT_CONFIG)
            if not historical_replay and not has_any_line:
                continue
            all_logs_player = logs_by_player.get(pid, [])
            # Last 10 games vs THIS opponent at THIS location (AWAY).
            opp_logs_all = [l for l in all_logs_player
                            if l['opp'] == h and l.get('player_team') == a
                            and l.get('location') == 'Away']
            opp_logs = opp_logs_all[:10]
            for sk, sc in STAT_CONFIG.items():
                vals = [float(l[sk]) for l in opp_logs]
                last10 = opp_logs[:10]
                sb        = odds_lookup.get((_nn(pname), sk), {})
                fd_line   = sb.get('line')
                fd_odds   = sb.get('odds', '')
                l10_sb_hits = sum(1 for l in last10 if float(l[sk]) > fd_line) if fd_line and last10 else None
                dk_ob = dk_lookup.get((_nn(pname), sk), {})
                dk_line = dk_ob.get('line')
                dk_over_odds  = dk_ob.get('over_odds', '')
                dk_under_odds = dk_ob.get('under_odds', '')
                dk_hits = sum(1 for l in last10 if float(l[sk]) > dk_line) if dk_line and last10 else None
                # PATTERN: consistency measured at the ACTUAL betting line (over).
                result = pattern_at_line(vals, dk_line if dk_line is not None else fd_line)
                recent10 = all_logs_player[:10]
                recent_vals = [float(l[sk]) for l in recent10]
                line_rec, line_rec_pct, line_rec_hits = _line_pick(dk_line, [float(l[sk]) for l in opp_logs_all[:10]], opp_logs_all[:10], sk)
                streak_rec, streak_n = _streak_pick(dk_line, opp_logs_all, sk)
                alt_rec, alt_evens, alt_odds = _alt_pick(dk_line, recent10, sk)
                # Conflict resolution: streak (matchup-specific) beats MPA Special (rhythm) when they disagree
                if streak_rec and alt_rec and streak_rec != alt_rec:
                    alt_rec = None
                # LINE-only 70%+ picks kept too (see home-side note) — opens the parlay pool.
                if not result and not streak_rec and not alt_rec and not line_rec:
                    continue
                base = result or {'threshold': 0, 'hits': 0, 'games': len(last10),
                                  'hit_rate': 0.0, 'pct': 0.0}
                l10h = sum(1 for l in last10 if float(l[sk]) >= base['threshold']) if base['threshold'] else 0
                picks.append({**base, **_form_swing(all_logs_player, sk),
                              'player': pname, 'player_id': pid, 'team': a,
                              'team_id': game['away_id'],
                              'jersey': player.get('jersey',''), 'position': player.get('position',''),
                              'tipoff': game.get('tipoff',''),
                              'team_name': a_name, 'stat': sk,
                              'stat_label': sc['label'], 'emoji': sc['emoji'],
                              'location': 'Away', 'opp': h, 'opp_name': h_name,
                              'ladder': build_ladder(vals, dk_line if dk_line is not None else fd_line),
                              'best_bet': best_bet_at_line(dk_line if dk_line is not None else fd_line, vals),
                              'glog': [{'d': l['date'], 'v': l[sk]} for l in opp_logs],
                              'matchup': f"{a_name} @ {h_name}",
                              'l10_hits': l10h, 'l10_games': len(last10),
                              'fd_line': fd_line, 'fd_odds': fd_odds,
                              'fd_over_odds': sb.get('over_odds'), 'fd_under_odds': sb.get('under_odds'),
                              'fd_book': sb.get('bookmaker_label') or sb.get('bookmaker'),
                              'dk_book': dk_ob.get('bookmaker_label') or dk_ob.get('bookmaker'),
                              'l10_sb_hits': l10_sb_hits,
                              'dk_line': dk_line, 'dk_over_odds': dk_over_odds, 'dk_under_odds': dk_under_odds, 'dk_hits': dk_hits,
                              'line_rec': line_rec, 'line_rec_pct': line_rec_pct, 'line_rec_hits': line_rec_hits,
                              'streak_rec': streak_rec, 'streak_n': streak_n,
                              'alt_rec': alt_rec, 'alt_evens': alt_evens, 'alt_odds': alt_odds,
                              'has_consistency': result is not None,
                              'recent_avg': round(sum(recent_vals)/len(recent_vals), 1) if recent_vals else None,
                               'recent_glog': [{'d': l['date'], 'v': l[sk]} for l in recent10],
                              'gap': round((sum(recent_vals)/len(recent_vals)) - dk_line, 1) if recent_vals and dk_line else None,
                              'mpg': round(sum(float(l.get('MIN',0) or 0) for l in recent10)/len(recent10), 1) if recent10 else None})

    # Sort: consistency picks first (by hit rate), then non-consistency
    # streak/MPA picks. None-safe.
    picks.sort(key=lambda x: (x.get('has_consistency', False), x.get('hit_rate') or 0, x.get('threshold') or 0), reverse=True)
    # Take enough picks to surface TOP_N distinct players (cards group by player,
    # so 12 picks from 11 unique players = only 11 cards). Walk the sorted list
    # collecting picks until we hit TOP_N distinct names.
    # CARD MINUTES FLOOR: the top cards are reserved for starters + 6th-man rotation
    # players. Anyone averaging under CARD_MIN_MPG minutes over their recent games is a
    # low-minute backup and is skipped for the cards (they still appear in all_picks for
    # the parlay builder / all-by-game list). Unknown mpg (rare — no recent logs) is kept.
    # To revert / loosen, lower CARD_MIN_MPG (set to 0 to disable).
    CARD_MIN_MPG = 24
    top_picks = []
    _seen_players = set()
    _below = []  # picks from sub-floor (backup) players, held back for backfill
    for _pk in picks:
        _mpg = _pk.get('mpg')
        if _mpg is not None and _mpg < CARD_MIN_MPG:
            _below.append(_pk)
            continue
        top_picks.append(_pk)
        _seen_players.add(_pk['player'])
        if len(_seen_players) >= TOP_N:
            break
    # BACKFILL TO TOP_N: starters / 6th-men (>= CARD_MIN_MPG) always fill the cards first.
    # If too few clear the floor on a light slate, top the board off with the HIGHEST-minute
    # players among those under the floor (closest to a 6th man) so we still show a full
    # top TOP_N instead of only a handful. Deep-bench guys sort last and rarely make it.
    if len(_seen_players) < TOP_N:
        _below.sort(key=lambda x: (x.get('mpg') or 0), reverse=True)
        for _pk in _below:
            if _pk['player'] in _seen_players:
                top_picks.append(_pk)            # complete an already-carded player's picks
            elif len(_seen_players) < TOP_N:
                top_picks.append(_pk)
                _seen_players.add(_pk['player'])  # add a new backup only while under TOP_N
            # else: board already has TOP_N distinct players — skip further NEW players but
            # keep scanning so trailing picks of carded players aren't dropped (no early break).
    log.append(f"{len(picks)} qualifying picks -> {len(top_picks)} picks across top {len(_seen_players)} players shown")
    if odds_props:
        with_lines = sum(1 for p in picks if p.get('fd_line'))
        log.append(f"{with_lines} picks have sportsbook lines attached")

    odds_loaded = bool(odds_props)
    props_picks, props_nopick = [], []
    for game in games:
        h,a = game['home'],game['away']
        h_name,a_name = game['home_name'],game['away_name']
        matchup_str = f"{a_name} @ {h_name}"
        for loc,tid,opp_id,opp_name,side in [('Home',game['home_id'],a,a_name,'HOME'),('Away',game['away_id'],h,h_name,'AWAY')]:
            for player in rosters.get(tid,[]):
                pname,pid = player['name'],player['id']
                for sk,sc in STAT_CONFIG.items():
                    ob = odds_lookup.get((_nn(pname),sk),{})
                    if historical_replay:
                        line = None
                    else:
                        if not ob or ob.get('line') is None: continue
                        line = float(ob['line'])
                    dk_ob = dk_lookup.get((_nn(pname),sk),{})
                    dk_over = None if historical_replay else dk_ob.get('over_odds','')
                    dk_under = None if historical_replay else dk_ob.get('under_odds','')
                    # Same trade-aware filter: only games with current team vs today's opp at this location
                    cur_team = tid_to_abbr.get(tid, '')
                    opp_logs = [l for l in logs_by_player.get(pid, [])
                                if l['opp'] == opp_id and l['location'] == loc and l.get('player_team') == cur_team][:10]
                    if not opp_logs:
                        if historical_replay:
                            continue
                        props_nopick.append({**_form_swing(logs_by_player.get(pid, []), sk),'player':pname,'player_id':pid,'stat':sk,'stat_label':sc['label'],'emoji':sc['emoji'],'team':cur_team,'side':side,'opp_name':opp_name,'line':line,'avg':None,'games':0,'history':'—','gap':None,'pick':None,'fd_odds':ob.get('odds',''),'dk_over_odds':dk_over,'dk_under_odds':dk_under,'bookmaker':dk_ob.get('bookmaker') or ob.get('bookmaker',''),'bookmaker_label':dk_ob.get('bookmaker_label') or ob.get('bookmaker_label',''),'matchup':matchup_str})
                        continue
                    vals = [float(l[sk]) for l in opp_logs]
                    avg = round(sum(vals)/len(vals),1)
                    replay = find_best_threshold(vals, sc['thresholds']) if historical_replay else None
                    gap = round(avg-line,1) if line is not None else None
                    pick = ('OVER' if replay else
                            ('OVER' if avg>line else ('UNDER' if avg<line else None)))
                    entry = {**_form_swing(logs_by_player.get(pid, []), sk),'player':pname,'player_id':pid,'stat':sk,'stat_label':sc['label'],'emoji':sc['emoji'],'team':cur_team,'side':side,'opp_name':opp_name,'line':line,'replay_threshold':replay.get('threshold') if replay else None,'avg':avg,'games':len(vals),'history':','.join(str(int(v)) for v in vals[:8]),'gap':gap,'pick':pick,'fd_odds':None if historical_replay else ob.get('odds',''),'dk_over_odds':dk_over,'dk_under_odds':dk_under,'bookmaker':dk_ob.get('bookmaker') or ob.get('bookmaker',''),'bookmaker_label':dk_ob.get('bookmaker_label') or ob.get('bookmaker_label',''),'matchup':matchup_str,'historical_replay':historical_replay}
                    (props_picks if pick else props_nopick).append(entry)
    props_picks.sort(key=lambda x:abs(x.get('gap') or 0),reverse=True)
    log.append(f"Props: {len(props_picks)} picks")
    # Hot / Cold form section: separate spotlight boards; never alters the picks above.
    hot_cold = []
    if not historical_replay:
        try:
            hot_cold = _nba_hot_cold_rows(today_str, games, rosters, logs_by_player,
                                          dk_lookup, props_picks + props_nopick)
            log.append(f"Hot/Cold: {len(hot_cold)} form plays")
        except Exception as _hce:
            print(f"[nba_hc] build failed: {_hce}")
            log.append("Hot/Cold board unavailable for this run")
    result = {'date':today_str,'picks':top_picks,'all_picks':picks,'games':games,'log':log,'total':len(picks),'odds_loaded':odds_loaded,'historical_replay':historical_replay,'props_picks':props_picks,'props_nopick':props_nopick,'hot_cold':hot_cold}
    result = _nba_apply_season_scope(result)
    _cache.update(result)
    # Only cache if we actually got prop lines from the Odds API.
    # Otherwise the empty result gets pinned for 6h even after sportsbooks post lines.
    has_lines = historical_replay or bool(props_picks) or bool(props_nopick)
    if has_lines:
        _cache_set("nba", today_str, result)
        if not historical_replay:
            _nba_save_live_board_bg(today_str, result)
    else:
        print(f"[Cache] SKIP write — no prop lines yet for {today_str} (will retry on next request)")
    if not historical_replay:
        _nba_th.Thread(target=_nba_trk_bg, args=(today_str, result), daemon=True).start()
    # Coach snapshots are deliberately separate from the normal NBA ledger.
    # Only a complete, server-generated board may enter the Coach record.
    try:
        if not historical_replay:
            _nba_coach_capture_pregame(today_str, result)
    except Exception as _ce:
        print(f"[nba_coach_track] capture failed: {_ce}")
    try:
        from replit_push import push_picks_to_replit
        # Bake the picks into the page HTML so the Replit hub can serve an
        # instant, no-cold-start snapshot at moneypicksarena.com/dashboard/nba.
        import json as _json
        _inject = (
            '<script>window.__INITIAL_PICKS__ = '
            + _json.dumps(result).replace('</', '<\\/')
            + ';</script></head>'
        )
        from datetime import datetime as _dt, timedelta as _td
        _tomorrow_str = (_dt.fromisoformat(today_str) + _td(days=1)).date().isoformat()
        _snapshot_html = MAIN_HTML.replace("__TODAY__", today_str).replace("__TOMORROW__", _tomorrow_str).replace('</head>', _inject, 1)
        push_picks_to_replit("nba", result, html=_snapshot_html)
    except Exception as _e:
        print(f"[replit_push] nba push failed: {_e}")
    return result

# ─── HTML ─────────────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🏀 Money Buckets</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
/* responsive: phones & tablets (mobile fit) */
html,body{max-width:100%;overflow-x:hidden}
img{max-width:100%;height:auto}
@media (max-width:1200px){table{display:block;width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;white-space:nowrap}}
@media (max-width:560px){table{font-size:12px}table th,table td{padding:6px 8px}}
body{
  background:#0d0d0d;
  background-image:radial-gradient(ellipse at 50% 0%,rgba(253,184,39,.1) 0%,transparent 55%);
  color:#f0e6c8;font-family:'Segoe UI',system-ui,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:0;
}
/* ── Spinning basketball ── */
.spin-ball{
  width:80px;height:80px;border-radius:50%;
  background:radial-gradient(circle at 38% 35%,#FDB827 0%,#FDB827 55%,#7c2d12 100%);
  border:2px solid #7c2d12;
  position:relative;margin-bottom:24px;
  animation:spinBall 6s linear infinite;
  box-shadow:0 0 40px rgba(253,184,39,.5),0 0 80px rgba(253,184,39,.15);
}
.spin-ball::before{
  content:'';position:absolute;inset:-1px;border-radius:50%;
  border:2.5px solid rgba(124,45,18,.9);
  border-left-color:transparent;border-right-color:transparent;
  transform:rotate(30deg);
}
.spin-ball::after{
  content:'';position:absolute;inset:16px;border-radius:50%;
  border:2px solid rgba(124,45,18,.8);
  border-top-color:transparent;border-bottom-color:transparent;
}
@keyframes spinBall{from{transform:rotate(0)}to{transform:rotate(360deg)}}
/* ── Card ── */
.card{
  background:linear-gradient(145deg,rgba(15,23,42,.97),rgba(8,12,24,.99));
  border:1px solid rgba(42,42,42,.8);border-radius:24px;
  padding:40px 40px 36px;width:390px;text-align:center;
  box-shadow:0 30px 80px rgba(0,0,0,.7),0 0 0 1px rgba(253,184,39,.04),inset 0 1px 0 rgba(255,255,255,.03);
  position:relative;overflow:hidden;
}
.card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,transparent,#FDB827,#FDB827,#FDB827,transparent);
}
.logo-line{display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:4px}
.login-card h1{
  font-size:1.65rem;font-weight:900;letter-spacing:-.5px;
  background:linear-gradient(135deg,#FDB827 0%,#FDB827 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.sub{color:#374151;font-size:.75rem;margin-bottom:30px;letter-spacing:1.5px;text-transform:uppercase}
.field{position:relative;margin-bottom:13px}
.fi{position:absolute;left:14px;top:50%;transform:translateY(-50%);opacity:.35;font-size:.9rem;pointer-events:none}
input{
  width:100%;background:rgba(15,23,42,.8);
  border:1px solid rgba(42,42,42,.8);color:#d1d5db;
  padding:13px 16px 13px 42px;border-radius:12px;
  font-size:.95rem;outline:none;transition:border-color .2s,box-shadow .2s;
}
input:focus{border-color:#FDB827;box-shadow:0 0 0 3px rgba(253,184,39,.12)}
input::placeholder{color:#374151}
.btn-in{
  width:100%;margin-top:8px;
  background:linear-gradient(135deg,#FDB827,#FDB827);color:#0d0d0d;
  border:none;padding:14px;border-radius:12px;
  font-size:1rem;font-weight:900;letter-spacing:.5px;cursor:pointer;
  box-shadow:0 4px 20px rgba(253,184,39,.35);transition:transform .15s,box-shadow .15s;
}
.btn-in:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(253,184,39,.45)}
.btn-in:active{transform:translateY(0)}
.err{color:#f87171;font-size:.83rem;margin-top:14px;background:rgba(127,29,29,.3);padding:10px 14px;border-radius:10px;border:1px solid rgba(239,68,68,.2)}
.tagline{color:#0f1d2e;font-size:.68rem;margin-top:22px;letter-spacing:2px;text-transform:uppercase}
</style>
</head>
<body>
<script>
(function(){
  var HUB='https://moneypicksarena.com';
  var KEY='__mpa_token';
  var p=new URLSearchParams(window.location.search);
  var t=p.get('token');
  var a=p.get('admin'); if(a){try{localStorage.setItem('__mpa_admin',a);}catch(e){}}
  if(t){localStorage.setItem(KEY,t);}
  if(t||a){window.history.replaceState({},'',window.location.pathname);}
  // no redirect
})();
</script>

<div class="spin-ball"></div>
<div class="card">
  <div class="logo-line">
    <h1>Money Buckets</h1>
  </div>
  <p class="sub">NBA Pick Intelligence</p>
  <form method="post" action="/login">
    <div class="field"><span class="fi">👤</span><input name="username" type="text" placeholder="Username" required autocomplete="username"></div>
    <div class="field"><span class="fi">🔒</span><input name="password" type="password" placeholder="Password" required autocomplete="current-password"></div>
    <button class="btn-in" type="submit">Access Picks →</button>
    {error}
  </form>
  <p class="tagline">Sportsbook Lines · Ranked Picks · 70% Threshold</p>
</div>
</body>
</html>"""

MAIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NBA Money Buckets &mdash; Money Picks Arena</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900&family=Source+Sans+Pro:wght@300;400;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
/* responsive: phones & tablets (mobile fit) */
html,body{max-width:100%;overflow-x:hidden}
img{max-width:100%;height:auto}
@media (max-width:1200px){table{display:block;width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;white-space:nowrap}}
@media (max-width:560px){table{font-size:12px}table th,table td{padding:6px 8px}}
body{background:#0f0f0f;color:#fff;font-family:'Source Sans Pro',sans-serif;min-height:100vh}
.bg-glow{position:fixed;inset:0;background:radial-gradient(ellipse at 50% 20%,rgba(245,158,11,.05),transparent 65%);pointer-events:none;z-index:0}
nav{position:fixed;top:0;width:100%;background:rgba(10,10,10,.95);backdrop-filter:blur(12px);border-bottom:1px solid #1c1c1c;z-index:100;padding:0 32px;height:80px;display:flex;align-items:center}
.logo{font-family:'Playfair Display',serif;font-size:36px;font-weight:900;color:#f59e0b;letter-spacing:.02em;line-height:1}
.logo span{color:#fff}
.page{position:relative;z-index:1;max-width:1400px;margin:0 auto;padding:104px 24px 40px}
.app-hdr{text-align:center;margin-bottom:32px}
.app-hdr h1{font-family:'Playfair Display',serif;font-size:2.6rem;font-weight:900;color:#fff;margin-bottom:6px}
.app-hdr h1 span{color:#f59e0b}
.app-hdr p{font-size:.85rem;color:#6b7280;letter-spacing:.15em;text-transform:uppercase}
.card{background:#161616;border:1px solid #262626;border-radius:20px;padding:24px;margin-bottom:16px}
.date-row{display:flex;align-items:center;justify-content:center;gap:12px;margin-bottom:20px}
.date-row label{color:#9ca3af;font-weight:600;font-size:.85rem;letter-spacing:.08em;text-transform:uppercase}
.date-row input[type=date]{background:#0a0a0a;color:#fff;border:1px solid #2a2a2a;border-radius:10px;padding:10px 16px;font-size:.95rem;font-family:'Source Sans Pro',sans-serif;cursor:pointer;outline:none;transition:border .2s}
.date-row input[type=date]:focus{border-color:#f59e0b}
input[type=date]::-webkit-calendar-picker-indicator{filter:invert(1);opacity:.7;cursor:pointer}
.btn{padding:10px 24px;border-radius:8px;font-size:.88rem;font-weight:700;cursor:pointer;border:none;transition:all .2s;font-family:'Source Sans Pro',sans-serif}
.btn-run{background:#f59e0b;color:#000}
.btn-run:hover{background:#fbbf24;transform:translateY(-1px);box-shadow:0 4px 20px rgba(245,158,11,.35)}
.btn-run:disabled{background:#2a2a2a;color:#4b5563;cursor:not-allowed;transform:none;box-shadow:none}
.admin-only{display:none !important}
body.is-admin .admin-only{display:inline-block !important}
.nba-menu{position:relative}
.nba-nav-btn{background:#1d4ed8;color:#fff;border:1px solid #3b82f6;border-radius:10px;padding:9px 16px;font-weight:800;font-size:.82rem;cursor:pointer;white-space:nowrap}
.nba-nav-btn:hover{filter:brightness(1.12)}
.nba-menu-list{display:none;position:absolute;right:0;top:calc(100% + 8px);z-index:120;min-width:240px;max-height:75vh;overflow:auto;background:#0b1220;border:1px solid #334155;border-radius:12px;padding:6px;box-shadow:0 18px 40px rgba(0,0,0,.55)}
.nba-menu-list.open{display:block}
.nba-menu-item{display:flex;align-items:center;gap:10px;width:100%;background:transparent;border:0;border-left:4px solid var(--c);border-radius:6px;color:#f1f5f9;font-size:.86rem;font-weight:800;text-align:left;padding:10px 12px;margin:2px 0;cursor:pointer}
.nba-menu-item:hover,.nba-menu-item:focus{background:#1e293b;outline:none}
body.is-admin .nba-menu-item.admin-only{display:flex !important}
.nba-menu-head{color:#fbbf24;font-size:.66rem;font-weight:900;letter-spacing:.14em;text-transform:uppercase;padding:8px 12px 5px;margin-top:2px;border-bottom:1px solid #263244}
.nba-menu-head.tools{color:#7dd3fc;margin-top:8px;padding-top:10px;border-top:1px solid #263244}
#parlayCard{display:none}
body.is-admin #parlayCard{display:block}
#parlayLegs{background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:8px;padding:8px 12px;font-size:.9rem;font-weight:700}
.btn-force{background:#dc2626;color:#fff}
.btn-force:hover{background:#ef4444;transform:translateY(-1px);box-shadow:0 4px 20px rgba(220,38,38,.35)}
.ball-svg,.ball-shadow,.fd-indicator,.pick-emoji,.cr-emoji,.tb-ico,.msg-card .ico,.btn-out,.btn-refresh{display:none}
.games-bar{display:none;gap:8px;overflow-x:auto;padding-bottom:4px;margin-bottom:20px}
.game-chip{background:#161616;border:1px solid #262626;border-radius:10px;padding:9px 18px;white-space:nowrap;font-size:.82rem;flex-shrink:0;transition:border-color .2s}
.game-chip:hover{border-color:#f59e0b}
.game-chip b{color:#fff;font-weight:700}
.game-chip .sep{color:#374151;margin:0 5px}
.filter-bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.filter-btn{padding:7px 18px;border-radius:999px;border:1px solid #262626;background:#161616;color:#6b7280;font-size:.81rem;cursor:pointer;transition:all .2s;font-weight:600;font-family:'Source Sans Pro',sans-serif}
.filter-btn.active,.filter-btn:hover{background:rgba(245,158,11,.1);color:#f59e0b;border-color:rgba(245,158,11,.3)}
.section-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.section-title{font-size:1rem;font-weight:700;display:flex;align-items:center;gap:8px;color:#f59e0b;font-family:'Playfair Display',serif}
.count-pill{background:rgba(245,158,11,.1);color:#f59e0b;padding:4px 14px;border-radius:999px;font-size:.78rem;font-weight:700;border:1px solid rgba(245,158,11,.2)}
.picks-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-bottom:10px}
.pick-card{background:#161616;border:1px solid #262626;border-radius:20px;padding:22px;position:relative;overflow:hidden;transition:border-color .25s,transform .22s}
.pick-card:hover{border-color:rgba(245,158,11,.4);transform:translateY(-3px);box-shadow:0 14px 40px rgba(0,0,0,.5)}
.nbc-grid{grid-template-columns:repeat(auto-fill,minmax(min(290px,100%),1fr));gap:14px}
.nbc-card{padding:0;border-radius:18px;background:linear-gradient(160deg,#1a1a1a,#111);border:1px solid #2a2a2a;border-top:3px solid var(--acc);display:flex;flex-direction:column}
.nbc-card:hover{border-color:var(--acc);box-shadow:0 14px 40px rgba(0,0,0,.55)}
.nbc-body{padding:16px 15px 13px;flex:1}
.nbc-rank{position:absolute;top:8px;right:13px;font-family:'Playfair Display',serif;font-weight:900;font-size:1.7rem;color:rgba(245,158,11,.35);line-height:1}
.nbc-top{display:flex;align-items:center;gap:12px;margin-bottom:10px;padding-right:30px}
.nbc-hs{position:relative;width:62px;height:62px;flex:0 0 auto;border-radius:50%;background:radial-gradient(circle at 50% 30%,#2a3445,#141922);border:2px solid var(--acc);display:flex;align-items:center;justify-content:center;cursor:pointer}
.nbc-hs-img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;border-radius:50%;z-index:2}
.nbc-ini{font-family:'Playfair Display',serif;font-weight:800;font-size:1.2rem;color:#9ca3af;z-index:1}
.nbc-logo{position:absolute;right:-5px;bottom:-4px;width:25px;height:25px;z-index:3;background:#0f0f0f;border:1px solid #2a2a2a;border-radius:50%;padding:2px;object-fit:contain}
.nbc-id{flex:1;min-width:0}
.nbc-name{font-weight:900;color:#fff;font-size:1.05rem;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nbc-meta{font-size:.76rem;color:#9ca3af;margin-top:4px;display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.nbc-meta b{color:#e5e7eb}
.nbc-meta2{font-size:.68rem;color:#6b7280;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nbc-loc{font-size:.6rem;font-weight:900;letter-spacing:.06em;padding:2px 6px;border-radius:5px}
.nbc-loc.home{background:rgba(74,222,128,.12);color:#4ade80;border:1px solid rgba(74,222,128,.3)}
.nbc-loc.away{background:rgba(96,165,250,.12);color:#60a5fa;border:1px solid rgba(96,165,250,.3)}
.nbc-pos{font-size:.6rem;font-weight:900;padding:2px 6px;border-radius:5px;background:#1f2937;color:#e5e7eb;border:1px solid #374151}
.nbc-tagrow{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.nbc-mkt{font-size:.74rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--acc);padding:3px 9px;border-radius:6px;border:1px solid var(--acc);background:rgba(255,255,255,.03)}
.nbc-hero{display:flex;align-items:center;justify-content:space-between;gap:10px;border:1px solid;border-radius:12px;padding:10px 12px;margin-bottom:10px}
.nbc-hero-l{min-width:0}
.nbc-verdict{font-weight:900;font-size:1.25rem;letter-spacing:.01em;line-height:1.1}
.nbc-hero-sub{font-size:.7rem;color:#9ca3af;margin-top:3px}
.nbc-hero-r{display:flex;flex-direction:column;gap:3px;align-items:flex-end;flex:0 0 auto}
.nbc-px{display:flex;align-items:center;gap:6px;font-size:.78rem}
.nbc-px span{color:#6b7280;font-weight:800;font-size:.66rem}
.nbc-px b{font-family:monospace;color:#fbbf24;font-weight:900;font-size:.88rem}
.nbc-px-none{color:#6b7280;font-size:.7rem;font-weight:700}
.nbc-tiles{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:10px}
.nbc-tile{background:#141414;border:1px solid #232323;border-radius:9px;padding:7px 8px;text-align:center;min-width:0}
.nbc-tile .k{font-size:.56rem;color:#6b7280;text-transform:uppercase;letter-spacing:.04em;font-weight:800;line-height:1.25}
.nbc-tile .v{font-weight:900;font-size:.92rem;margin-top:3px;color:#f3f4f6}
.nbc-tile .v small{font-size:.66rem;color:#9ca3af;font-weight:700}
.nbc-dim{color:#4b5563}
.nbc-form{margin-bottom:10px}
.nbc-form-k{font-size:.62rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em;font-weight:800;margin-bottom:5px}
.nbc-form-k b{color:#e2e8f0}
.nbc-chips{display:flex;flex-wrap:wrap;gap:4px}
.nbc-chips span{min-width:26px;text-align:center;font-size:.74rem;font-weight:900;padding:3px 6px;border-radius:6px;border:1px solid}
.nbc-why{background:#15130b;border:1px solid rgba(253,184,39,.22);border-radius:8px;padding:7px 9px;margin-bottom:10px;font-size:.72rem;line-height:1.4;color:#cbd5e1}
.nbc-why b{color:#fff}
.nbc-why-k{color:#FDB827;font-weight:900;text-transform:uppercase;letter-spacing:.06em;margin-right:6px;font-size:.62rem}
.nbc-sigs{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px}
.nbc-sig{font-size:.66rem;font-weight:800;padding:3px 8px;border-radius:999px;border:1px solid;white-space:nowrap}
.nbc-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;border-top:1px solid #222;padding-top:10px}
.nbc-score{font-family:'Playfair Display',serif;font-weight:900;font-size:1.3rem;line-height:1;display:flex;align-items:baseline;gap:6px}
.nbc-score small{font-family:'Source Sans Pro',sans-serif;font-size:.6rem;color:#6b7280;font-weight:800;text-transform:uppercase;letter-spacing:.06em}
.nbc-tap{background:none;border:1px solid #333;color:#9ca3af;border-radius:8px;padding:6px 11px;font-size:.72rem;font-weight:800;cursor:pointer;transition:all .2s}
.nbc-tap:hover{border-color:var(--acc);color:var(--acc)}
.pick-rank{position:absolute;top:14px;right:15px;width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.82rem;font-weight:900}
.rank-1{background:linear-gradient(135deg,#C4901A,#f59e0b);color:#000;box-shadow:0 0 14px rgba(245,158,11,.5)}
.rank-2{background:linear-gradient(135deg,#374151,#9ca3af);color:#000}
.rank-3{background:linear-gradient(135deg,#7c2d12,#c2410c);color:#fff}
.rank-other{background:#1a1a1a;color:#4b5563;font-size:.75rem;border:1px solid #262626}
.pick-player{font-size:1.08rem;font-weight:800;color:#fff;margin-bottom:3px;letter-spacing:-.3px;padding-right:38px;font-family:'Playfair Display',serif}
.pick-team{font-size:.75rem;color:#6b7280;margin-bottom:12px;display:flex;align-items:center;gap:6px}
.loc-badge{background:#1a1a1a;padding:2px 9px;border-radius:10px;font-size:.7rem;color:#6b7280;border:1px solid #262626}
.stat-strip{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.stat-tag{padding:3px 10px;border-radius:10px;font-size:.7rem;font-weight:700;letter-spacing:.3px}
.tag-pts{background:rgba(109,40,217,.15);color:#a78bfa;border:1px solid rgba(109,40,217,.25)}
.tag-reb{background:rgba(37,99,235,.15);color:#60a5fa;border:1px solid rgba(37,99,235,.25)}
.tag-ast{background:rgba(5,150,105,.15);color:#34d399;border:1px solid rgba(5,150,105,.25)}
.tag-fg3m{background:rgba(220,38,38,.15);color:#f87171;border:1px solid rgba(220,38,38,.25)}
.tag-pra{background:rgba(168,85,247,.15);color:#c084fc;border:1px solid rgba(168,85,247,.25)}
.tag-combo{background:rgba(245,158,11,.15);color:#fbbf24;border:1px solid rgba(245,158,11,.25)}
.tag-blk{background:rgba(20,184,166,.15);color:#2dd4bf;border:1px solid rgba(20,184,166,.25)}
.tag-stl{background:rgba(236,72,153,.15);color:#f472b6;border:1px solid rgba(236,72,153,.25)}
.pick-pattern{font-size:.9rem;color:#7dd3fc;font-weight:700;margin-bottom:4px;line-height:1.4}
.l10vthr-desc{font-size:.88rem;color:#f59e0b;font-weight:700;margin-bottom:5px;line-height:1.4}
.fd-line-badge{display:inline-block;background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.2);color:#4ade80;border-radius:6px;padding:3px 10px;font-size:.78rem;font-weight:700;margin-bottom:6px}
.fd-inline{color:#4ade80;font-weight:700}
.l10vthr-inline{color:#f59e0b;font-weight:700}
.pick-matchup{font-size:.72rem;color:#374151;margin-bottom:16px}
.bar-wrap{background:#1a1a1a;border-radius:6px;height:8px;overflow:hidden;margin-bottom:10px;border:1px solid #262626}
.bar-fill{height:100%;border-radius:5px}
.bar-green{background:linear-gradient(90deg,#15803d,#4ade80)}
.bar-yellow{background:linear-gradient(90deg,#b45309,#f59e0b)}
.bar-orange{background:linear-gradient(90deg,#c2410c,#f97316)}
.stats-row{display:flex;justify-content:space-between;align-items:center}
.games-chip{background:#1a1a1a;padding:4px 12px;border-radius:20px;font-size:.75rem;color:#4b5563;border:1px solid #262626}
.pct{font-size:1.2rem;font-weight:900;letter-spacing:-.5px;font-family:'Playfair Display',serif}
.pct-green{color:#4ade80}
.pct-yellow{color:#f59e0b}
.pct-orange{color:#f97316}
.total-banner{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;background:#161616;border:1px solid rgba(74,222,128,.2);border-radius:18px;padding:18px 24px;margin:32px 0 20px}
.tb-left{display:flex;align-items:center;gap:12px}
.tb-title{font-size:.95rem;font-weight:700;color:#4ade80;font-family:'Playfair Display',serif}
.tb-sub{font-size:.72rem;color:#374151;margin-top:2px;letter-spacing:.8px;text-transform:uppercase}
.tb-count{font-size:2.2rem;font-weight:900;color:#4ade80;letter-spacing:-1.5px;font-family:'Playfair Display',serif}
.all-section-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:10px}
.all-section-title{font-size:.95rem;font-weight:700;color:#f59e0b;display:flex;align-items:center;gap:8px;font-family:'Playfair Display',serif}
.game-group{margin-bottom:14px}
.game-group-hdr{display:flex;align-items:center;justify-content:space-between;background:#161616;border:1px solid #262626;border-radius:13px;padding:12px 18px;margin-bottom:6px;cursor:pointer;user-select:none;transition:border-color .2s}
.game-group-hdr:hover{border-color:rgba(245,158,11,.3)}
.gg-label{font-size:.88rem;font-weight:700;color:#fff;display:flex;align-items:center;gap:8px}
.gg-meta{display:flex;align-items:center;gap:8px}
.gg-chevron{color:#4b5563;font-size:.85rem;transition:transform .2s}
.compact-picks{display:flex;flex-direction:column;gap:5px;margin-bottom:4px}
.compact-row{display:flex;align-items:center;gap:12px;background:#1a1a1a;border:1px solid #262626;border-radius:11px;padding:10px 15px;transition:border-color .2s}
.compact-row:hover{border-color:rgba(245,158,11,.25)}
.cr-info{flex:1;min-width:0}
.cr-player{font-size:.86rem;font-weight:700;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cr-pattern{font-size:.76rem;color:#60a5fa;font-weight:600;margin-top:2px}
.cr-right{display:flex;flex-direction:column;align-items:flex-end;gap:3px;flex-shrink:0}
.cr-bar-wrap{background:#1a1a1a;border-radius:4px;height:4px;width:68px;overflow:hidden;border:1px solid #262626}
.cr-bar-fill{height:100%;border-radius:4px}
.cr-pct{font-size:.9rem;font-weight:900;font-family:'Playfair Display',serif}
.cr-sample{font-size:.65rem;color:#374151}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(245,158,11,.3);border-top-color:#f59e0b;border-radius:50%;animation:spin .6s linear infinite;margin-right:6px;vertical-align:middle}
.loading-ball{width:48px;height:48px;border:3px solid rgba(245,158,11,.15);border-top:3px solid #f59e0b;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 18px}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes ballBounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-22px)}}
@keyframes shadowPulse{0%,100%{transform:scaleX(1)}50%{transform:scaleX(.55)}}
.msg-card{background:#161616;border:1px solid #262626;border-radius:20px;padding:60px 30px;text-align:center}
.msg-card h2{color:#fff;font-size:1.2rem;font-weight:800;margin-bottom:10px;font-family:'Playfair Display',serif}
.msg-card p{color:#6b7280;font-size:.88rem;line-height:1.75}
.log-box{background:#0a0a0a;border:1px solid #262626;border-radius:12px;padding:16px;font-size:.74rem;color:#374151;font-family:'Courier New',monospace;margin-top:20px;max-height:160px;overflow-y:auto;line-height:1.9}
footer{text-align:center;padding:32px 24px;color:#4b5563;font-size:.78rem;border-top:1px solid #1c1c1c;margin-top:24px;font-family:'Source Sans Pro',sans-serif}
.ft-logo{font-family:'Playfair Display',serif;color:#f59e0b;font-weight:700;font-size:.95rem;margin-bottom:6px}
.props-player-chip{background:#161616;border:1px solid #262626;border-radius:10px;padding:10px 14px;min-width:150px;cursor:pointer;transition:border-color .15s;user-select:none}
.props-player-chip:hover{border-color:#f59e0b}
.props-game-sel{background:#111;color:#fff;border:1px solid #2a2a2a;border-radius:8px;padding:8px 14px;font-size:.85rem;font-family:'Source Sans Pro',sans-serif;outline:none;min-width:220px;cursor:pointer}
/* NBA Track Record */
.nba-trk-tbl{width:100%;border-collapse:collapse;font-size:.8rem}
.nba-trk-tbl th,.nba-trk-tbl td{padding:8px 10px;text-align:left;border-bottom:1px solid #1e293b;color:#e2e8f0}
.nba-trk-tbl th{color:#818cf8;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;background:#0a0a14}
.nba-trk-tbl tr:hover td{background:#0f172a}
</style>
</head>
<body>
<div class="bg-glow"></div>
<nav style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px"><div class="logo">Money <span>Picks</span> Arena</div>
<div class="nba-menu">
  <button type="button" class="nba-nav-btn" id="nbaNavMenuBtn" aria-haspopup="true" aria-expanded="false" onclick="_nbaNavToggle(event)">&#9776; Records &amp; Tools &#9662;</button>
  <div class="nba-menu-list" id="nbaNavMenu" role="menu">
    <div class="nba-menu-head" role="presentation">&#128202; Records</div>
    <button type="button" role="menuitem" class="nba-menu-item" style="--c:#4338ca" onclick="_nbaNavGo(function(){document.getElementById('nba-track-section').scrollIntoView({behavior:'smooth',block:'start'});})">&#128202; Track Record</button>
    <button type="button" role="menuitem" class="nba-menu-item" style="--c:#0e7490" onclick="_nbaNavGo(openNbaOverflowRecord)">&#11088; Overflow Record</button>
    <button type="button" role="menuitem" class="nba-menu-item" style="--c:#ea580c" onclick="_nbaNavGo(openNbaHotColdRecord)">&#128293; Hot / Cold Record</button>
    <button type="button" role="menuitem" class="nba-menu-item" style="--c:#d97706" onclick="_nbaNavGo(openNbaDowReport)">&#128197; Day of Week Trends</button>
    <button type="button" role="menuitem" class="nba-menu-item" style="--c:#7c3aed" onclick="_nbaNavGo(function(){openNbaCoachTrack();})">&#129504; Coach Track Record</button>
    <button type="button" role="menuitem" class="nba-menu-item" style="--c:#8b5cf6" onclick="_nbaNavGo(function(){openNbaGpRecord();})">&#128302; Game Predictor Record</button>
    <div class="nba-menu-head tools" role="presentation">&#128736;&#65039; Tools</div>
    <button type="button" role="menuitem" class="nba-menu-item" style="--c:#ea580c" onclick="_nbaNavGo(openNbaHotColdBoard)">&#128293; Hot / Cold Board</button>
    <button type="button" role="menuitem" class="nba-menu-item" style="--c:#8b5cf6" onclick="_nbaNavGo(function(){openNbaGamePredictor();})">&#128302; Game Predictor</button>
    <button type="button" role="menuitem" class="nba-menu-item admin-only" style="--c:#d97706" onclick="_nbaNavGo(function(){var c=document.getElementById('parlayCard');if(c)c.scrollIntoView({behavior:'smooth',block:'center'});})">&#127920; Auto Parlay Builder</button>
    <button type="button" role="menuitem" class="nba-menu-item" style="--c:#a855f7" onclick="_nbaNavGo(function(){showNbaPerfectParlayBuilder();})">&#10024; Perfect Parlay</button>
    <button type="button" role="menuitem" class="nba-menu-item admin-only" style="--c:#0e7490" onclick="_nbaNavGo(function(){openNbaMyBets();})">&#128176; My Bets</button>
  </div>
</div>
<script>
function _nbaNavToggle(e){
  if(e)e.stopPropagation();
  var m=document.getElementById('nbaNavMenu'),b=document.getElementById('nbaNavMenuBtn');
  var open=!m.classList.contains('open');m.classList.toggle('open',open);if(b)b.setAttribute('aria-expanded',open?'true':'false');
}
function _nbaNavClose(){var m=document.getElementById('nbaNavMenu'),b=document.getElementById('nbaNavMenuBtn');if(m)m.classList.remove('open');if(b)b.setAttribute('aria-expanded','false');}
function _nbaNavGo(fn){_nbaNavClose();try{fn();}catch(err){console.error(err);}}
document.addEventListener('click',function(e){var w=e.target.closest?e.target.closest('.nba-menu'):null;if(!w)_nbaNavClose();});
document.addEventListener('keydown',function(e){if(e.key==='Escape')_nbaNavClose();});
</script></nav>
<div class="page">
<div class="app-hdr">
  <h1>NBA <span>Money Buckets</span></h1>
  <p>Pts &middot; Reb &middot; Ast &middot; 3PM &middot; Daily Picks</p>
</div>
<div class="card" style="text-align:center;max-width:600px;margin:0 auto 20px">
  <h2 style="font-family:'Playfair Display',serif;font-size:1.5rem;font-weight:700;color:#fff;margin-bottom:20px">Run Today's Picks</h2>
  <div class="date-row">
    <label>Date</label>
    <input type="date" id="datePicker" value="__TODAY__" min="__TODAY__" max="__TOMORROW__">
  </div>
  <div style="text-align:center"><button class="btn btn-run" id="getBtn" onclick="getPicks()">🎯 Get Picks</button><button class="btn btn-run admin-only" id="runBtn" onclick="runPicks()" style="margin-left:10px">Run Picks</button><button class="btn btn-force admin-only" id="forceBtn" onclick="runPicks(true)" style="margin-left:10px" title="Bypass cache and rebuild today's picks from scratch">Force Refresh</button></div>
</div>
<div class="card" id="parlayCard" style="text-align:center;max-width:600px;margin:0 auto 20px">
  <h2 style="font-family:'Playfair Display',serif;font-size:1.4rem;font-weight:700;color:#fff;margin-bottom:6px">🎰 Auto Parlay Builder <span style="font-size:.7rem;color:#777;font-family:sans-serif">admin only</span></h2>
  <div style="font-size:.74rem;color:#888;margin-bottom:16px">Pulls from any strong pick today — Pick, Line, Streak, MPA — best available odds priced in</div>
  <div style="display:flex;gap:10px;justify-content:center;align-items:center;flex-wrap:wrap">
    <label style="color:#999;font-weight:700">Legs</label>
    <select id="parlayLegs">
      <option>2</option><option>3</option><option>4</option><option>5</option><option>6</option><option>7</option><option>8</option><option>9</option><option>10</option>
    </select>
    <button class="btn" onclick="buildParlay()">Build Best Parlay</button>
    <button class="btn" onclick="generateParlay()" style="background:#1f2937;color:#fff">🎲 Generate New</button>
  </div>
  <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-top:10px;font-size:.74rem;color:#cbd5e1">
    <span style="color:#94a3b8;font-weight:700">Also include:</span>
    <label><input type="checkbox" name="parlayHcForm" value="hot"> 🔥 Hot</label>
    <label><input type="checkbox" name="parlayHcForm" value="cold"> 🧊 Cold</label>
    <label><input type="checkbox" name="parlayHcForm" value="double_cold"> ❄️ Double Cold</label>
  </div>
  <div id="parlayResult" style="margin-top:16px;text-align:left"></div>
</div>
<div id="nbaSeasonNotice" style="display:none;margin:0 0 12px;padding:10px 12px;border:1px solid #854d0e;border-radius:8px;color:#fde68a;font-size:.78rem"></div>
<div class="games-bar" id="gamesBar"></div>
<div id="filterBar" style="display:none" class="filter-bar">
  <button class="filter-btn active" data-stat="ALL" onclick="filterStat('ALL')">All Stats</button>
  <button class="filter-btn" data-stat="PTS" onclick="filterStat('PTS')">Points</button>
  <button class="filter-btn" data-stat="REB" onclick="filterStat('REB')">Rebounds</button>
  <button class="filter-btn" data-stat="AST" onclick="filterStat('AST')">Assists</button>
  <button class="filter-btn" data-stat="FG3M" onclick="filterStat('FG3M')">3-Pointers</button>
  <button class="filter-btn" data-stat="PRA" onclick="filterStat('PRA')">🃏 PRA</button>
  <button class="filter-btn" data-stat="PTS_REB" onclick="filterStat('PTS_REB')">💪 Pts+Reb</button>
  <button class="filter-btn" data-stat="PTS_AST" onclick="filterStat('PTS_AST')">⚡ Pts+Ast</button>
  <button class="filter-btn" data-stat="REB_AST" onclick="filterStat('REB_AST')">🔗 Reb+Ast</button>
  <button class="filter-btn" data-stat="BLK" onclick="filterStat('BLK')">🛡️ Blocks</button>
  <button class="filter-btn" data-stat="STL" onclick="filterStat('STL')">🧤 Steals</button>
  <span id="topSideChooser" style="display:none;flex-basis:100%;align-items:center;gap:7px;padding-top:3px">
    <button id="topBothBtn" class="filter-btn active" data-top-side="ALL" onclick="filterTopSide('ALL')" style="font-size:.72rem">Both</button>
    <button id="topOverBtn" class="filter-btn" data-top-side="OVER" onclick="filterTopSide('OVER')" style="font-size:.72rem;color:#4ade80">Over Top 10</button>
    <button id="topUnderBtn" class="filter-btn" data-top-side="UNDER" onclick="filterTopSide('UNDER')" style="font-size:.72rem;color:#f87171">Under Top 10</button>
  </span>
</div>
<div id="content"></div>
<div id="allPicksWrap" style="display:none">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px" id="signalDropdowns">
    <div style="background:#0e0e0e;border:1px solid #262626;border-radius:12px;overflow:hidden">
      <div onclick="toggleSig('streakList',this)" style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;cursor:pointer;background:linear-gradient(135deg,rgba(249,115,22,.12),rgba(249,115,22,.02));border-bottom:1px solid #262626">
        <div style="display:flex;align-items:center;gap:10px"><span style="font-size:1.1rem">🔥</span><span style="font-weight:900;color:#fb923c;letter-spacing:.05em">ALL STREAKS</span><span class="count-pill" id="streakCount">0</span></div>
        <span class="sig-chev" style="color:#666;transition:transform .2s">▼</span>
      </div>
      <div id="streakList" style="display:none;max-height:360px;overflow-y:auto"></div>
    </div>
    <div style="background:#0e0e0e;border:1px solid #262626;border-radius:12px;overflow:hidden">
      <div onclick="toggleSig('mpaList',this)" style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;cursor:pointer;background:linear-gradient(135deg,rgba(168,85,247,.12),rgba(168,85,247,.02));border-bottom:1px solid #262626">
        <div style="display:flex;align-items:center;gap:10px"><span style="font-size:1.1rem">⭐</span><span style="font-weight:900;color:#c084fc;letter-spacing:.05em">ALL MPA SPECIALS</span><span class="count-pill" id="mpaCount">0</span></div>
        <span class="sig-chev" style="color:#666;transition:transform .2s">▼</span>
      </div>
      <div id="mpaList" style="display:none;max-height:360px;overflow-y:auto"></div>
    </div>
  </div>
  <div class="total-banner">
    <div class="tb-left">
      <div class="tb-ico">📋</div>
      <div>
        <div class="tb-title">All Qualifying Picks</div>
        <div class="tb-sub">Every player hitting 70%+ · Grouped by game</div>
      </div>
    </div>
    <div class="tb-count" id="totalCount">0</div>
  </div>
  <div class="all-section-hdr">
    <div class="all-section-title">🎯 All Picks by Game</div>
    <input id="playerSearchInput" type="text" placeholder="Search player…" oninput="applyAllFilters()" style="background:#111;color:#fff;border:1px solid #2a2a2a;border-radius:8px;padding:7px 14px;font-size:.85rem;font-family:'Source Sans Pro',sans-serif;outline:none;width:180px;margin-bottom:6px" />
    <div style="display:flex;gap:8px;flex-wrap:wrap" id="allFilterBar">
      <button class="filter-btn active" data-stat="ALL" onclick="filterAll('ALL')">All</button>
      <button class="filter-btn" data-stat="PTS" onclick="filterAll('PTS')">🏀 Pts</button>
      <button class="filter-btn" data-stat="REB" onclick="filterAll('REB')">📊 Reb</button>
      <button class="filter-btn" data-stat="AST" onclick="filterAll('AST')">🎯 Ast</button>
      <button class="filter-btn" data-stat="FG3M" onclick="filterAll('FG3M')">🔥 3PM</button>
      <button class="filter-btn" data-stat="PRA" onclick="filterAll('PRA')">🃏 PRA</button>
      <button class="filter-btn" data-stat="PTS_REB" onclick="filterAll('PTS_REB')">💪 Pts+Reb</button>
      <button class="filter-btn" data-stat="PTS_AST" onclick="filterAll('PTS_AST')">⚡ Pts+Ast</button>
      <button class="filter-btn" data-stat="REB_AST" onclick="filterAll('REB_AST')">🔗 Reb+Ast</button>
      <button class="filter-btn" data-stat="BLK" onclick="filterAll('BLK')">🛡️ Blk</button>
      <button class="filter-btn" data-stat="STL" onclick="filterAll('STL')">🧤 Stl</button>
      <button class="filter-btn" id="oversBtn" onclick="toggleSide('OVER')" style="margin-left:8px">⬆ Overs only</button>
      <button class="filter-btn" id="undersBtn" onclick="toggleSide('UNDER')">⬇ Unders only</button>
    </div>
  </div>
  <div id="allPicksSection"></div>
</div>

<style>
 .nba-hc-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px}
 .nba-hc-lbl{color:#94a3b8;font-size:.62rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase;min-width:54px}
 .nba-hc-btn{background:#111827;color:#cbd5e1;border:1px solid #334155;border-radius:999px;padding:6px 12px;font-size:.72rem;font-weight:800;cursor:pointer}
 .nba-hc-btn.active{background:linear-gradient(135deg,#f97316,#7c3aed);color:#fff;border-color:#f97316}
 .nba-hc-n{opacity:.75;font-size:.62rem;margin-left:3px}
 .nba-hc-group{margin-top:16px}
 .nba-hc-gh{color:#fde68a;font-weight:900;font-size:.85rem;margin-bottom:8px}
 .nba-hc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:10px}
 .nba-hc-card{background:#111827;border:1px solid #1f2937;border-left:4px solid var(--fc);border-radius:12px;padding:10px 12px;font-size:.74rem;color:#e2e8f0}
 .nba-hc-top{display:flex;align-items:center;gap:9px;width:100%;background:none;border:0;color:inherit;text-align:left;cursor:pointer;padding:0;font:inherit}
 .nba-hc-rank{color:#64748b;font-weight:900;font-size:.72rem;min-width:24px}
 .nba-hc-head{width:42px;height:31px;object-fit:cover;border-radius:6px;background:#0b1121;flex:none}
 .nba-hc-who{display:flex;flex-direction:column;flex:1;min-width:0}
 .nba-hc-who b{font-size:.84rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .nba-hc-who small{color:#94a3b8;font-size:.64rem}
 .nba-hc-badge{color:var(--fc);font-size:.6rem;font-weight:900;letter-spacing:.06em;white-space:nowrap}
 .nba-hc-play{margin:8px 0 4px;font-size:.86rem;font-weight:800}
 .nba-hc-line{margin-top:4px;line-height:1.45}
 .nba-hc-muted{color:#64748b}
 .nba-hc-chips{display:inline-flex;gap:3px;margin-left:4px;vertical-align:middle}
 .nba-hc-chip{min-width:22px;text-align:center;border-radius:5px;padding:1px 4px;font-size:.64rem;font-weight:900}
 .nba-hc-chip.hit{background:rgba(74,222,128,.16);color:#4ade80}.nba-hc-chip.miss{background:rgba(248,113,113,.16);color:#f87171}.nba-hc-chip.push{background:rgba(250,204,21,.16);color:#facc15}
 .nba-hc-more{margin-top:7px;color:#a5b4fc;font-size:.66rem;font-weight:800;cursor:pointer}
 .nba-hc-drop{margin-top:8px;border-top:1px solid #1f2937;padding-top:6px}
 .nba-hc-dh{color:#94a3b8;font-size:.6rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;margin:6px 0 4px}
 .nba-hc-log{width:100%;border-collapse:collapse;font-size:.68rem}
 .nba-hc-log th{color:#818cf8;text-align:left;font-size:.58rem;padding:3px 4px}.nba-hc-log td{padding:3px 4px;border-top:1px solid #1f2937}
 .nba-hc-empty{border:1px dashed #334155;border-radius:10px;padding:14px;color:#94a3b8;font-size:.76rem;margin-top:10px}
</style>
<div id="nba-hotcold-section" style="display:none;max-width:1400px;margin:28px auto 0;padding:0 24px 10px">
  <div style="display:flex;justify-content:space-between;align-items:flex-end;gap:10px;flex-wrap:wrap;margin-bottom:10px">
    <div><div style="font-size:.78rem;font-weight:700;color:#f97316;text-transform:uppercase;letter-spacing:.15em">&#128293; Hot / &#129482; Cold Players</div>
    <div style="font-size:.7rem;color:#94a3b8;margin-top:4px;max-width:900px;line-height:1.5">Hot = OVER in 80%+ of the last 5 current-season games (2+ games in October) with the average above the genuine sportsbook line. Cold = the same for UNDER. Double Cold = Cold plus 70%+ Under in 3+ of the last 5 meetings vs today&#39;s opponent. Top 10 per form and market, ranks 11-20 as overflow; every play is saved pre-tip and tracked in the Hot / Cold Record. Edge appears only when the app has a same-line estimate and a genuine price.</div></div>
    <button type="button" class="nba-hc-btn" onclick="openNbaHotColdRecord()">&#128202; Hot / Cold Record</button>
  </div>
  <div id="nbaHcControls"></div>
  <div id="nbaHcBoard"></div>
</div>
<div id="props-section" style="display:none;max-width:1400px;margin:28px auto 0;padding:0 24px 40px">
  <div class="props-historical-label"></div>
  <div style="font-size:.78rem;font-weight:700;color:#f59e0b;text-transform:uppercase;letter-spacing:.15em;margin:0 0 14px;display:flex;align-items:center;gap:10px">&#9889; Player Props vs Opponent History</div>
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px">
    <select id="propsGameSel" class="props-game-sel" onchange="propsSelectGame(this.value)">
      <option value="">-- Select a game --</option>
    </select>
    <span id="propsGameHint" style="font-size:.75rem;color:#555">Select a game to browse players</span>
  </div>
  <div id="propsPlayerList" style="display:none"></div>
  <p style="font-size:.72rem;color:#555;margin-top:10px">
    <strong style="color:#f59e0b">Avg vr Opp</strong> = career avg at this location vs today&#39;s opponent (incl. playoffs) &nbsp;|&nbsp; Click any player to see all their prop lines
  </p>
</div>
</div><!-- /wrap -->

<style>
.nba-bets-tbl{width:100%;border-collapse:collapse;font-size:.82rem}
.nba-bets-tbl th{padding:7px 10px;text-align:left;font-size:.72rem;color:#94a3b8;font-weight:700;text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid #1e293b;white-space:nowrap}
.nba-bets-tbl td{padding:8px 10px;border-bottom:1px solid #0f172a;vertical-align:middle;color:#e2e8f0}
.nba-bets-tbl tr:last-child td{border-bottom:none}
.nba-bets-tbl tr:hover td{background:rgba(255,255,255,.02)}
</style>
<div id="nba-mybets-card" style="display:none;max-width:960px;margin:0 auto 24px;padding:0 16px">
  <div class="card" style="padding:20px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:10px">
      <div style="font-weight:800;color:#a5b4fc;font-size:1rem">&#128176; MY BETS &mdash; RECORD &amp; ROI</div>
      <div style="display:flex;gap:8px;align-items:center">
        <button onclick="getNbaBetsResults()" style="background:#1d4ed8;color:#fff;border:none;border-radius:8px;padding:8px 13px;font-size:.78rem;font-weight:700;cursor:pointer">&#128200; Settle Results</button>
        <button onclick="document.getElementById(&#39;nba-mybets-card&#39;).style.display=&#39;none&#39;" style="background:#1e293b;border:none;color:#94a3b8;border-radius:8px;padding:8px 11px;font-size:.9rem;cursor:pointer">&#215;</button>
      </div>
    </div>
    <div id="nba-mybets-body"><p style="color:#94a3b8;font-size:.85rem">Loading&#8230;</p></div>
  </div>
</div>
<footer>
  <div class="ft-logo">Money Picks Arena</div>
  <div>NBA Money Buckets &middot; Pts &middot; Reb &middot; Ast &middot; 3PM &middot; Blk &middot; Stl &middot; Combos</div>
  <div style="margin-top:8px;font-size:.7rem">For entertainment only. Not a betting service. Must be 18+.</div>
</footer>
<!-- NBA Track Record — always visible below picks -->
<style>
 .nba-trk-shell{border:1px solid #1e293b!important;background:#0b1121!important;border-radius:12px}
 .nba-acc-row { background: #111827; border: 1px solid #1e293b; border-radius: 8px; margin-bottom: 8px; overflow: hidden; border-left: 4px solid #3b82f6; display: flex; flex-direction: column; }
 .nba-acc-row.cat-Points { border-left-color: #3b82f6; }
 .nba-acc-row.cat-Rebounds { border-left-color: #f97316; }
 .nba-acc-row.cat-Assists { border-left-color: #fbbf24; }
 .nba-acc-row.cat-3Pointers { border-left-color: #ef4444; }
 .nba-acc-row.cat-PtsRebAst { border-left-color: #a855f7; }
 .nba-acc-row.cat-PtsReb { border-left-color: #8b5cf6; }
 .nba-acc-row.cat-PtsAst { border-left-color: #ec4899; }
 .nba-acc-row.cat-RebAst { border-left-color: #14b8a6; }
 .nba-acc-row.cat-Blocks { border-left-color: #22c55e; }
 .nba-acc-row.cat-Steals { border-left-color: #06b6d4; }
 .nba-acc-head { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; cursor: pointer; user-select: none; }
 .nba-acc-head:hover { background: #1e293b; }
 .nba-acc-title { display: flex; align-items: center; gap: 12px; font-size: .85rem; font-weight: 700; color:#f8fafc; }
 .nba-acc-side { font-size: .65rem; padding: 2px 8px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.2); font-weight: 700; }
 .nba-acc-side.over { color: #60a5fa; border-color: #1d4ed8; background: rgba(29,78,216,.1); }
 .nba-acc-side.under { color: #fcd34d; border-color: #b45309; background: rgba(180,83,9,.1); }
 .nba-acc-stats { display: flex; gap: 16px; font-size: .8rem; color: #cbd5e1; align-items: center; font-weight: 700; }
 .nba-acc-btn { background: #334155; color: #fff; border: none; border-radius: 4px; width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; font-weight: bold; cursor: pointer; }
 .nba-acc-body { display: none; border-top: 1px solid #1e293b; padding: 0; }
 .nba-acc-row.open .nba-acc-body { display: block; }
 .nba-acc-row.open .nba-acc-btn { background: #475569; transform: rotate(45deg); transition: transform 0.2s; }
 .nba-acc-body table { width: 100%; border-collapse: collapse; font-size: .75rem; }
 .nba-acc-body th { padding: 10px 16px; text-align: left; border-bottom: 1px solid #1e293b; color: #93c5fd; font-size: .65rem; text-transform: uppercase; }
 .nba-acc-body td { padding: 10px 16px; border-bottom: 1px solid #1e293b; color: #e2e8f0; }
 .nba-acc-body tr:last-child td { border-bottom: none; }
 .nba-record-hero{position:relative;overflow:hidden;border:1px solid #263553;border-radius:18px;padding:22px;background:radial-gradient(circle at 92% 10%,rgba(79,70,229,.2),transparent 38%),linear-gradient(145deg,#111827,#080d19)}
 .nba-record-kicker{color:#818cf8;font-size:.64rem;font-weight:900;letter-spacing:.16em;text-transform:uppercase}
 .nba-record-title{color:#fff;font-family:'Playfair Display',serif;font-size:1.45rem;margin:5px 0 4px}
 .nba-record-sub{color:#94a3b8;font-size:.75rem;line-height:1.5}
 .nba-record-badge{border:1px solid #166534;background:rgba(20,83,45,.2);color:#86efac;border-radius:999px;padding:6px 10px;font-size:.62rem;font-weight:900;letter-spacing:.05em}
 .nba-record-tools{display:flex;align-items:end;gap:10px;flex-wrap:wrap;margin-top:18px;padding-top:16px;border-top:1px solid rgba(148,163,184,.15)}
 .nba-record-label{color:#64748b;font-size:.6rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase}
 .nba-record-control{display:block;margin-top:5px;background:#070b14;color:#fff;border:1px solid #334155;border-radius:9px;padding:9px 11px;font-weight:800;outline:none}
 .nba-record-control:focus{border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.12)}
 .nba-view-tabs{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
 .nba-view-btn{background:#111827;color:#94a3b8;border:1px solid #334155;border-radius:8px;padding:9px 12px;font-size:.7rem;font-weight:900;cursor:pointer}
 .nba-view-btn.active{background:#4338ca;color:#fff;border-color:#6366f1}
 .nba-summary-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:16px 0}
 .nba-summary-card{background:linear-gradient(145deg,#111827,#0a0f1c);border:1px solid #1e293b;border-radius:13px;padding:14px}
 .nba-summary-label{color:#64748b;font-size:.58rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase}
 .nba-summary-value{color:#fff;font-size:1.25rem;font-weight:950;margin-top:5px}
 .nba-summary-note{color:#64748b;font-size:.62rem;margin-top:3px}
 .nba-record-empty{text-align:center;padding:42px 20px;border:1px dashed #334155;border-radius:14px;background:#0a0f1c}
 .nba-record-empty-icon{font-size:1.7rem;color:#475569;margin-bottom:10px}
 .nba-record-empty-title{color:#e2e8f0;font-weight:900;font-size:.95rem}
 .nba-record-empty-copy{color:#64748b;font-size:.73rem;line-height:1.55;max-width:460px;margin:7px auto 0}
 .nba-full-wrap{overflow-x:auto;border:1px solid #1e293b;border-radius:12px}
 .nba-full-table{width:100%;border-collapse:collapse;min-width:780px;font-size:.73rem}
 .nba-full-table th{background:#0a0f1c;color:#818cf8;text-align:left;padding:10px;font-size:.58rem;letter-spacing:.08em;text-transform:uppercase}
 .nba-full-table td{padding:10px;border-top:1px solid #1e293b;color:#e2e8f0}
 @media(max-width:700px){.nba-summary-grid{grid-template-columns:1fr 1fr}.nba-view-tabs{margin-left:0;width:100%}.nba-view-btn{flex:1}.nba-acc-head{align-items:flex-start;gap:10px}.nba-acc-stats{gap:7px;font-size:.68rem;flex-wrap:wrap;justify-content:flex-end}.nba-acc-body table{min-width:680px}.nba-record-hero{padding:17px}}
</style>
<div id="nba-track-section" style="max-width:1040px;margin:24px auto 0;padding:0 16px 40px">
  <div class="card nba-trk-shell" style="padding:14px">
    <div class="nba-record-hero">
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap">
        <div><div class="nba-record-kicker">NBA Performance Center · Main</div><h2 class="nba-record-title">&#127942; Official Track Record</h2><div class="nba-record-sub">Main selections only. Future captures: each market/side Top 10. Earlier dates retain their saved Main selections. Overflow and Coach are excluded; only genuine prices count toward profit and ROI.</div></div>
        <div class="nba-record-badge">&#9679; OFFICIAL PRE-GAME RECORD</div>
      </div>
      <div class="nba-record-tools">
        <label><span class="nba-record-label">Slate date</span><select id="nbaTrkDateSelect" class="nba-record-control" onchange="renderNbaTrackDay()"><option value="">No saved dates</option></select></label><label><span class="nba-record-label">Period</span><select id="nbaTrkPeriod" class="nba-record-control" onchange="renderNbaTrackDay()"><option value="day">Day</option><option value="week">Week · last 7 days</option><option value="month">Month</option><option value="all">All Time</option></select></label>
        <label><span class="nba-record-label">Flat bet amount</span><input type="number" id="nbaTrkBetSize" class="nba-record-control" value="100.00" min="1" step="5" style="width:110px" oninput="renderNbaTrackDay()"></label>
        <button onclick="loadNbaTrackRecord()" class="nba-view-btn">&#8635; Refresh results</button>
        <div class="nba-view-tabs"><button id="nbaTrkCatBtn" class="nba-view-btn active" onclick="setNbaTrkView('category')">By Category</button><button id="nbaTrkFullBtn" class="nba-view-btn" onclick="setNbaTrkView('full')">Full List</button></div>
      </div>
    </div>
    <div id="nbaTrkTopPct" style="display:none"></div><div id="nbaTrkTopStats" style="display:none"></div>
    <div id="nbaTrkBody"></div>
  </div>
</div>
<div id="nba-overflow-track-section" style="display:none;max-width:1040px;margin:24px auto 0;padding:0 16px 40px">
  <div class="card nba-trk-shell" style="padding:14px">
    <div class="nba-record-hero">
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap">
        <div><div class="nba-record-kicker" style="color:#22d3ee">NBA Performance Center · Overflow</div><h2 class="nba-record-title">Overflow Track Record</h2><div class="nba-record-sub">Future captures: every qualifying standard-line play ranked 11+ within its market and OVER/UNDER side. Earlier dates show only their originally saved overflow rows. Main, Coach and preseason results stay separate.</div></div>
        <button class="nba-view-btn" onclick="document.getElementById('nba-overflow-track-section').style.display='none'">Close</button>
      </div>
      <div class="nba-record-tools">
        <label><span class="nba-record-label">Slate date</span><select id="nbaOvfDateSelect" class="nba-record-control" onchange="renderNbaTrackDay('overflow')"><option value="">No saved dates</option></select></label><label><span class="nba-record-label">Period</span><select id="nbaOvfPeriod" class="nba-record-control" onchange="renderNbaTrackDay('overflow')"><option value="day">Day</option><option value="week">Week · last 7 days</option><option value="month">Month</option><option value="all">All Time</option></select></label>
        <label><span class="nba-record-label">Flat bet amount</span><input type="number" id="nbaOvfBetSize" class="nba-record-control" value="100.00" min="1" step="5" style="width:110px" oninput="renderNbaTrackDay('overflow')"></label>
        <button onclick="loadNbaTrackRecord('overflow')" class="nba-view-btn">&#8635; Refresh results</button>
        <div class="nba-view-tabs"><button id="nbaOvfCatBtn" class="nba-view-btn active" onclick="setNbaTrkView('category','overflow')">By Category</button><button id="nbaOvfFullBtn" class="nba-view-btn" onclick="setNbaTrkView('full','overflow')">Full List</button></div>
      </div>
    </div>
    <div id="nbaOvfTopPct" style="display:none"></div><div id="nbaOvfTopStats" style="display:none"></div>
    <div id="nbaOvfBody"></div>
  </div>
</div>
<div id="nba-hotcold-track-section" style="display:none;max-width:1040px;margin:24px auto 0;padding:0 16px 40px">
  <div class="card nba-trk-shell" style="padding:14px">
    <div class="nba-record-hero">
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap">
        <div><div class="nba-record-kicker" style="color:#f97316">NBA Performance Center · Hot / Cold</div><h2 class="nba-record-title">&#128293; Hot / Cold Track Record</h2><div class="nba-record-sub">Saved pre-tip Hot (OVER), Cold (UNDER) and Double Cold (UNDER) plays for every market: Top 10 per form and market, plus ranks 11-20 as Hot / Cold Overflow. Kept separate from the Main, Overflow and Coach totals. Hit rate counts every decided play; P/L counts only plays with a genuine saved price.</div></div>
        <button class="nba-view-btn" onclick="document.getElementById('nba-hotcold-track-section').style.display='none'">Close</button>
      </div>
      <div class="nba-record-tools">
        <label><span class="nba-record-label">Slate date</span><select id="nbaHcDateSelect" class="nba-record-control" onchange="renderNbaTrackDay('hotcold')"><option value="">No saved dates</option></select></label><label><span class="nba-record-label">Period</span><select id="nbaHcPeriod" class="nba-record-control" onchange="renderNbaTrackDay('hotcold')"><option value="day">Day</option><option value="week">Week · last 7 days</option><option value="month">Month</option><option value="all">All Time</option></select></label>
        <label><span class="nba-record-label">Board</span><select id="nbaHcBook" class="nba-record-control" onchange="renderNbaTrackDay('hotcold')"><option value="main">Top 10</option><option value="overflow">Overflow · ranks 11-20</option><option value="all">Top 10 + Overflow</option></select></label>
        <label><span class="nba-record-label">Flat bet amount</span><input type="number" id="nbaHcBetSize" class="nba-record-control" value="100.00" min="1" step="5" style="width:110px" oninput="renderNbaTrackDay('hotcold')"></label>
        <button onclick="loadNbaTrackRecord('hotcold')" class="nba-view-btn">&#8635; Refresh results</button>
        <div class="nba-view-tabs"><button id="nbaHcCatBtn" class="nba-view-btn active" onclick="setNbaTrkView('category','hotcold')">By Category</button><button id="nbaHcFullBtn" class="nba-view-btn" onclick="setNbaTrkView('full','hotcold')">Full List</button></div>
      </div>
    </div>
    <div id="nbaHcTopPct" style="display:none"></div><div id="nbaHcTopStats" style="display:none"></div>
    <div id="nbaHcBody"></div>
  </div>
</div>
<style>
 .nba-dow-wrap{overflow-x:auto;border:1px solid #1e293b;border-radius:12px;margin-top:10px}
 .nba-dow-table{width:100%;border-collapse:collapse;font-size:.72rem;min-width:860px}
 .nba-dow-table th{background:#0a0f1c;color:#818cf8;text-align:center;padding:9px 8px;font-size:.58rem;letter-spacing:.08em;text-transform:uppercase;white-space:nowrap}
 .nba-dow-table th:first-child,.nba-dow-table td:first-child{text-align:left;position:sticky;left:0;background:#0b1120;z-index:1}
 .nba-dow-table td{padding:8px;border-top:1px solid #1e293b;color:#e2e8f0;text-align:center;white-space:nowrap}
 .nba-dow-cell b{display:block;font-size:.8rem}
 .nba-dow-cell small{color:#94a3b8}
 .nba-dow-h3{color:#e2e8f0;font-size:.85rem;font-weight:900;margin:18px 0 4px}
 .nba-dow-agg{min-width:780px}
 .nba-dow-agg td{text-align:right}
 .nba-dow-agg td:first-child{text-align:left;white-space:normal}
 .nba-dow-pos{color:#4ade80!important;font-weight:900}.nba-dow-neg{color:#f87171!important;font-weight:900}.nba-dow-mut{color:#64748b!important}
 .nba-dow-note{border:1px solid #26334a;border-radius:10px;padding:10px 12px;background:#0f172a;margin:10px 0;color:#94a3b8;font-size:.72rem;line-height:1.45}
 .nba-dow-details>summary{cursor:pointer;color:#a5b4fc;font-weight:900;font-size:.8rem;margin:16px 0 4px}
</style>
<div id="nba-dow-section" style="display:none;max-width:1040px;margin:24px auto 0;padding:0 16px 40px">
  <div class="card nba-trk-shell" style="padding:14px">
    <div class="nba-record-hero">
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap">
        <div><div class="nba-record-kicker" style="color:#f59e0b">NBA Performance Center · Day of Week</div><h2 class="nba-record-title">&#128197; Day of Week Trends · By Day / Category</h2><div class="nba-record-sub">Win %, record, money (P/L) and ROI for every category, Overs and Unders, by day of the week — same layout as the NHL By Day / Category report. Built from saved Main, Overflow, Edge Coach and Hot / Cold Track Record picks only (ungraded picks show as pending) (Edge Coach is kept separate and shows each Coach category). It never changes picks or starts grading. Hit rate counts every decided pick; P/L counts only picks with genuine saved odds.</div></div>
        <button class="nba-view-btn" onclick="document.getElementById('nba-dow-section').style.display='none'">Close</button>
      </div>
      <div class="nba-record-tools">
        <div class="nba-view-tabs" style="margin-left:0"><button id="nbaDowLaneAll" class="nba-view-btn active" onclick="_nbaDowSetLane('all')">Main + Overflow</button><button id="nbaDowLaneMain" class="nba-view-btn" onclick="_nbaDowSetLane('main')">Main</button><button id="nbaDowLaneOvf" class="nba-view-btn" onclick="_nbaDowSetLane('overflow')">Overflow</button><button id="nbaDowLaneCoach" class="nba-view-btn" onclick="_nbaDowSetLane('coach')">Edge Coach</button><button id="nbaDowLaneHc" class="nba-view-btn" onclick="_nbaDowSetLane('hotcold')">Hot / Cold</button></div>
        <label><span class="nba-record-label">Weekday</span><select id="nbaDowDay" class="nba-record-control" onchange="_nbaDowRender()"><option value="">All days</option><option value="0">Monday</option><option value="1">Tuesday</option><option value="2">Wednesday</option><option value="3">Thursday</option><option value="4">Friday</option><option value="5">Saturday</option><option value="6">Sunday</option></select></label>
        <label><span class="nba-record-label">Category</span><select id="nbaDowCat" class="nba-record-control" onchange="_nbaDowRender()"><option value="">All categories</option></select></label>
        <label><span class="nba-record-label">Side</span><select id="nbaDowSide" class="nba-record-control" onchange="_nbaDowRender()"><option value="">Overs + Unders</option><option value="OVER">Overs</option><option value="UNDER">Unders</option></select></label>
        <label><span class="nba-record-label">Range</span><select id="nbaDowRange" class="nba-record-control" onchange="_nbaDowRangeChanged()"><option value="all">All Time</option><option value="week">Last 7 days</option><option value="month">Month</option><option value="custom">Custom</option></select></label><label id="nbaDowMonthWrap" style="display:none"><span class="nba-record-label">Month</span><input type="month" id="nbaDowMonth" class="nba-record-control" onchange="_nbaDowRender()"></label><label id="nbaDowFromWrap" style="display:none"><span class="nba-record-label">From</span><input type="date" id="nbaDowFrom" class="nba-record-control" onchange="_nbaDowRender()"></label><label id="nbaDowToWrap" style="display:none"><span class="nba-record-label">To</span><input type="date" id="nbaDowTo" class="nba-record-control" onchange="_nbaDowRender()"></label>
        <label><span class="nba-record-label">Flat bet amount</span><input type="number" id="nbaDowStake" class="nba-record-control" value="100.00" min="1" step="5" style="width:110px" oninput="_nbaDowRender()"></label>
        <button class="nba-view-btn" onclick="loadNbaDowReport(true)">&#8635; Refresh</button>
        <button class="nba-view-btn" onclick="_nbaDowCsv()">CSV</button>
      </div>
    </div>
    <div id="nbaDowStatus" style="color:#94a3b8;font-size:.72rem;margin:8px 2px"></div>
    <div id="nbaDowBody"></div>
  </div>
</div>
<script>
(function(){
var S={lane:'all',data:{},req:0};
var WD=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
var CATS=["Points","Rebounds","Assists","3-Pointers","Pts+Reb+Ast","Pts+Reb","Pts+Ast","Reb+Ast","Blocks","Steals"];
function $(id){return document.getElementById(id);}
function wdIdx(d){var t=new Date(String(d||'').slice(0,10)+'T12:00:00Z');if(isNaN(t))return -1;return (t.getUTCDay()+6)%7;}
function stake(){return Math.max(1,parseFloat(($('nbaDowStake')||{}).value)||100);}
function todayEt(){try{return new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York'}).format(new Date());}catch(e){return new Date().toISOString().slice(0,10);}}
function rows(){
  var d=S.data[S.lane];if(!d)return [];
  var out=[];(d.dates||[]).forEach(function(day){(day.detail||[]).forEach(function(x){out.push(Object.assign({},x,{_date:day.date}));});});
  return out;
}
function filtered(){
  var cat=$('nbaDowCat').value,side=$('nbaDowSide').value,range=$('nbaDowRange').value,day=$('nbaDowDay').value,t=todayEt(),start='0001-01-01';
  var end='9999-12-31',mon='';
  if(range==='week'){var d=new Date(t+'T12:00:00Z');d.setUTCDate(d.getUTCDate()-6);start=d.toISOString().slice(0,10);end=t;}
  else if(range==='month'){mon=$('nbaDowMonth').value||t.slice(0,7);}
  else if(range==='custom'){start=$('nbaDowFrom').value||start;end=$('nbaDowTo').value||end;}
  return rows().filter(function(r){
    if(mon){if(String(r._date).slice(0,7)!==mon)return false;}
    else if(r._date<start||r._date>end)return false;
    if(day!==''&&wdIdx(r._date)!==Number(day))return false;
    if(cat&&(r.category||'')!==cat)return false;
    if(side&&String(r.side||'OVER').toUpperCase()!==side)return false;
    return true;
  });
}
function resOf(r){var x=String(r.result||'PENDING').toUpperCase();if(x==='WON')x='WIN';if(x==='LOST')x='LOSS';return x;}
function agg(list){
  var a={n:list.length,w:0,l:0,push:0,v:0,pend:0,priced:0,net:0,staked:0,dates:{},tg:{},tgKnown:true},s=stake();
  list.forEach(function(r){
    var res=resOf(r);a.dates[r._date]=1;
    if(r.team)a.tg[r._date+'|'+r.team]=1;else a.tgKnown=false;
    if(res==='WIN')a.w++;else if(res==='LOSS')a.l++;else if(res==='PUSH')a.push++;else if(res==='VOID')a.v++;else a.pend++;
    if((res==='WIN'||res==='LOSS')&&_nbaTrkPriced(r.odds)){a.priced++;a.staked+=s;a.net+=nbaProfit(r.odds,s,res);}
  });
  a.nd=Object.keys(a.dates).length;a.ntg=a.tgKnown&&list.length?Object.keys(a.tg).length:null;
  a.rate=a.w+a.l?100*a.w/(a.w+a.l):null;a.roi=a.staked?100*a.net/a.staked:null;
  return a;
}
function pct(v){return v==null?'—':v.toFixed(1)+'%';}
function aggRow(label,a,attrs){
  var other=[];if(a.push)other.push(a.push+' push');if(a.v)other.push(a.v+' void');if(a.pend)other.push(a.pend+' pending');
  var cls=a.priced?(a.net>=0?'nba-dow-pos':'nba-dow-neg'):'nba-dow-mut';
  return '<tr'+(attrs||'')+'><td>'+label+'</td><td>'+a.n+'</td><td>'+a.nd+'</td><td>'+(a.ntg==null?'n/a':a.ntg)+'</td><td>'+a.w+'-'+a.l+'</td>'+
    '<td style="color:'+(a.rate==null?'#64748b':rateColor(a.rate))+';font-weight:900">'+pct(a.rate)+'</td><td class="nba-dow-mut">'+(other.join(', ')||'—')+'</td>'+
    '<td>'+a.priced+'</td><td class="'+cls+'">'+(a.priced?money(a.net):'—')+'</td><td class="'+cls+'">'+pct(a.roi)+'</td></tr>';
}
function aggTable(first,rowsHtml){
  return '<div class="nba-dow-wrap"><table class="nba-dow-table nba-dow-agg"><thead><tr><th>'+nbaTrkEsc(first)+'</th><th>Picks</th><th>Dates</th><th>Team-games</th><th>W-L</th><th>Win %</th><th>Not decided</th><th>Priced</th><th>Money (P/L)</th><th>ROI</th></tr></thead><tbody>'+rowsHtml+'</tbody></table></div>';
}
function hcCats(){var o=[];['Hot','Cold','Double Cold'].forEach(function(f){CATS.forEach(function(c){o.push(f+' '+c);});});return o;}
function catOrder(list){
  var coach=S.lane==='coach',cats=coach?[]:(S.lane==='hotcold'?hcCats():CATS.slice());
  list.forEach(function(r){var c=r.category||'';if(c&&cats.indexOf(c)<0)cats.push(c);});
  if(coach)cats.sort();
  if(S.lane==='hotcold'){var present={};list.forEach(function(r){present[r.category||'']=1;});cats=cats.filter(function(c){return present[c];});}
  return cats;
}
function rateColor(p){return p>=60?'#4ade80':p>=50?'#facc15':'#f87171';}
function cell(a){
  var n=a.w+a.l;if(!n)return '<td class="nba-dow-cell"><small>—</small></td>';
  var p=a.w/n*100;
  return '<td class="nba-dow-cell"><b style="color:'+rateColor(p)+'">'+p.toFixed(0)+'%</b><small>'+a.w+'-'+a.l+'</small></td>';
}
function money(v){return (v>=0?'+':'−')+'$'+Math.abs(v).toFixed(2);}
function sideCell(list){var a=agg(list),n=a.w+a.l;return n?'<span style="color:'+rateColor(a.w/n*100)+'">'+a.w+'-'+a.l+' · '+(a.w/n*100).toFixed(1)+'%</span>':'—';}
function fillCats(){
  var sel=$('nbaDowCat'),cur=sel.value,seen={};
  var list=S.lane==='coach'?[]:(S.lane==='hotcold'?hcCats():CATS.slice());rows().forEach(function(r){var c=r.category||'';if(c&&list.indexOf(c)<0)list.push(c);});
  if(S.lane==='coach')list.sort();
  sel.innerHTML='<option value="">All categories</option>'+list.map(function(c){return '<option value="'+nbaTrkEsc(c)+'">'+nbaTrkEsc(c)+'</option>';}).join('');
  if(list.indexOf(cur)>=0)sel.value=cur;
}
window._nbaDowRender=function(){
  var body=$('nbaDowBody');if(!body)return;
  if(!S.data[S.lane]){body.innerHTML='';return;}
  if($('nbaDowRange').value==='custom'&&$('nbaDowFrom').value&&$('nbaDowTo').value&&$('nbaDowFrom').value>$('nbaDowTo').value){body.innerHTML=_nbaRecordEmpty('Check the dates','From must be on or before To.');return;}
  var list=filtered(),s=stake();
  if(!list.length){body.innerHTML=_nbaRecordEmpty('No saved picks for these filters','Pick another weekday, range, lane, category or side. Nothing is graded from this page.');return;}
  var selDay=$('nbaDowDay').value,days=selDay===''?[0,1,2,3,4,5,6]:[Number(selDay)];
  var tot=agg(list),h='';
  if(selDay!==''){
    var sd={};list.forEach(function(r){sd[r._date]=1;});
    h+='<div style="color:#94a3b8;font-size:.74rem;margin:0 0 10px">'+WD[Number(selDay)]+' only · slates: '+nbaTrkEsc(Object.keys(sd).sort().reverse().join(', '))+' · <a href="#" onclick="document.getElementById(&quot;nbaDowDay&quot;).value=&quot;&quot;;_nbaDowRender();return false" style="color:#818cf8">show all days</a></div>';
  }
  h+=_nbaRecordSummary(tot.w,tot.l,tot.pend,tot.net,tot.staked,s);
  h+='<div class="nba-dow-note">Win % counts decided picks only; pending, push and void are listed separately. Money (P/L) and ROI use the flat bet amount above and only genuine saved odds (-1000 or better); unpriced picks count toward W-L but have no payout. Same-day and same-team picks are related, so read Dates and Team-games, not just Picks. This is a results report only and never changes picks.</div>';
  h+='<div class="nba-dow-h3">Overall</div>'+aggTable('Scope',aggRow('<b>Filtered picks</b>',tot));
  var byDay=WD.map(function(){return [];});list.forEach(function(r){var i=wdIdx(r._date);if(i>=0)byDay[i].push(r);});
  var dh='';
  byDay.forEach(function(L,i){
    if(days.indexOf(i)<0)return;
    dh+=aggRow('<b style="color:#a5b4fc;text-decoration:underline">'+WD[i]+'</b>',agg(L),' style="cursor:pointer" title="Show '+WD[i]+' only" onclick="document.getElementById(&quot;nbaDowDay&quot;).value=&quot;'+i+'&quot;;_nbaDowRender()"');
  });
  h+='<div class="nba-dow-h3">By weekday'+(selDay===''?' <span style="color:#64748b;font-weight:600;font-size:.7rem">· tap a day to see only that day</span>':'')+'</div>'+aggTable('Weekday',dh);
  var cat=$('nbaDowCat').value,side=$('nbaDowSide').value,coach=S.lane==='coach';
  var cats=cat?[cat]:catOrder(list),sides=side?[side]:['OVER','UNDER'];
  var sideOf=function(r){return String(r.side||'OVER').toUpperCase();};
  var ch='',sh='',csh='';
  cats.forEach(function(c){
    var L=list.filter(function(r){return (r.category||'')===c;});
    if(coach&&!L.length)return;
    ch+=aggRow('<b>'+nbaTrkEsc(c)+'</b>',agg(L));
  });
  sides.forEach(function(sd){sh+=aggRow('<span class="nba-acc-side '+(sd==='OVER'?'over':'under')+'">'+sd+'</span>',agg(list.filter(function(r){return sideOf(r)===sd;})));});
  cats.forEach(function(c){sides.forEach(function(sd){
    var L=list.filter(function(r){return (r.category||'')===c&&sideOf(r)===sd;});
    if(coach&&!L.length)return;
    csh+=aggRow('<b>'+nbaTrkEsc(c)+'</b> <span class="nba-acc-side '+(sd==='OVER'?'over':'under')+'">'+sd+'</span>',agg(L));
  });});
  h+='<details class="nba-dow-details" open><summary>Category and side breakdowns</summary>';
  h+='<div class="nba-dow-h3">By category</div>'+aggTable('Category',ch);
  h+='<div class="nba-dow-h3">By side</div>'+aggTable('Side',sh);
  h+='<div class="nba-dow-h3">Category and side</div>'+aggTable('Category / side',csh)+'</details>';
  var mh='';
  cats.forEach(function(c){sides.forEach(function(sd){
    var L=list.filter(function(r){return (r.category||'')===c&&sideOf(r)===sd;});
    if(coach&&!L.length)return;
    var row='<td><b>'+nbaTrkEsc(c)+'</b> <span class="nba-acc-side '+(sd==='OVER'?'over':'under')+'">'+sd+'</span></td>';
    days.forEach(function(i){row+=cell(agg(L.filter(function(r){return wdIdx(r._date)===i;})));});
    var a=agg(L);if(days.length>1)row+=cell(a);
    var cls=a.staked?(a.net>=0?'nba-dow-pos':'nba-dow-neg'):'nba-dow-mut';
    row+='<td class="'+cls+'">'+(a.staked?money(a.net):'—')+'</td><td class="'+cls+'">'+pct(a.roi)+'</td>';
    mh+='<tr>'+row+'</tr>';
  });});
  h+='<div class="nba-dow-h3">'+(days.length>1?'Every category by day':'Every category on '+WD[days[0]])+' (win % · W-L)</div><div class="nba-dow-wrap"><table class="nba-dow-table"'+(days.length>1?'':' style="min-width:0"')+'><thead><tr><th>Category / side</th>'+days.map(function(i){return '<th>'+(days.length>1?WD[i].slice(0,3):WD[i])+'</th>';}).join('')+(days.length>1?'<th>Total</th>':'')+'<th>Money (P/L)</th><th>ROI</th></tr></thead><tbody>'+mh+'</tbody></table></div>';
  body.innerHTML=h;
};
window._nbaDowRangeChanged=function(){
  var m=$('nbaDowRange').value;
  $('nbaDowMonthWrap').style.display=m==='month'?'':'none';
  $('nbaDowFromWrap').style.display=m==='custom'?'':'none';
  $('nbaDowToWrap').style.display=m==='custom'?'':'none';
  if(m==='month'&&!$('nbaDowMonth').value)$('nbaDowMonth').value=todayEt().slice(0,7);
  window._nbaDowRender();
};
window._nbaDowSetLane=function(l){
  S.lane=(l==='overflow'||l==='main'||l==='coach'||l==='hotcold')?l:'all';
  $('nbaDowCat').value='';
  $('nbaDowLaneAll').classList.toggle('active',S.lane==='all');
  $('nbaDowLaneCoach').classList.toggle('active',S.lane==='coach');
  $('nbaDowLaneHc').classList.toggle('active',S.lane==='hotcold');
  $('nbaDowLaneMain').classList.toggle('active',S.lane==='main');
  $('nbaDowLaneOvf').classList.toggle('active',S.lane==='overflow');
  if(S.data[S.lane]){fillCats();window._nbaDowRender();}else window.loadNbaDowReport(true);
};
window.loadNbaDowReport=function(force){
  var lane=S.lane,id=++S.req,body=$('nbaDowBody'),st=$('nbaDowStatus');
  if(!force&&S.data[lane]){fillCats();window._nbaDowRender();return;}
  body.innerHTML=_nbaRecordEmpty('Loading saved results','Reading graded official picks.');st.textContent='';
  var tq=lane==='coach'?'&_tok='+encodeURIComponent(localStorage.getItem('__mpa_token')||''):'';
  fetch('/api/nba/day-of-week-record?lane='+lane+tq).then(function(r){
    return r.json().catch(function(){return {};}).then(function(d){if(!r.ok)throw new Error(d.detail||('HTTP '+r.status));return d;});
  }).then(function(d){
    if(id!==S.req)return;
    if(!d||!Array.isArray(d.dates))throw new Error('Unexpected record response');
    S.data[lane]=d;st.textContent=(lane==='hotcold'?'Hot / Cold':lane==='coach'?'Edge Coach':lane==='overflow'?'Overflow':lane==='main'?'Main':'Main + Overflow')+' records loaded: '+d.dates.length+' graded slates.';
    fillCats();window._nbaDowRender();
  }).catch(function(e){
    if(id!==S.req)return;
    body.innerHTML=_nbaRecordEmpty('Day of Week report unavailable',(e&&e.message)||'Saved results could not be loaded. No grading was started.');
  });
};
window.openNbaDowReport=function(){
  var s=$('nba-dow-section');if(!s)return;s.style.display='block';
  s.scrollIntoView({behavior:'smooth',block:'start'});
  window.loadNbaDowReport(true);
};
function csvCell(v){var t=String(v==null?'':v);if(/^[=+@-]/.test(t)&&isNaN(Number(t)))t="'"+t;return '"'+t.split('"').join('""')+'"';}
window._nbaDowCsv=function(){
  var list=filtered(),s=stake();if(!list.length)return;
  var lines=[['record','date','weekday','name','team','category','market','side','line','odds','actual','result','priced_profit'].map(csvCell).join(',')];
  list.forEach(function(r){
    var res=resOf(r),p='';
    if((res==='WIN'||res==='LOSS')&&_nbaTrkPriced(r.odds))p=nbaProfit(r.odds,s,res).toFixed(2);
    var i=wdIdx(r._date);
    lines.push([S.lane==='coach'?'coach':(S.lane==='hotcold'?(r.is_overflow?'hotcold_overflow':'hotcold'):(r.is_overflow?'overflow':'main')),r._date,i<0?'':WD[i],r.name||r.player,r.team,r.category,r.market||r.stat_label||r.stat||'',r.side,r.line,r.odds,r.actual,res,p].map(csvCell).join(','));
  });
  var blob=new Blob([lines.join(String.fromCharCode(13,10))],{type:'text/csv;charset=utf-8'});
  var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='nba_day_of_week_'+S.lane+'.csv';
  document.body.appendChild(a);a.click();setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},500);
};
})();
</script>
<script>
// Hub Access Gate - client side only, no server round-trip
(function(){
  var HUB='https://moneypicksarena.com';
  var KEY='__mpa_token';
  var p=new URLSearchParams(window.location.search);
  var t=p.get('token');
  var a=p.get('admin'); if(a){try{localStorage.setItem('__mpa_admin',a);}catch(e){}}
  if(t){localStorage.setItem(KEY,t);}
  if(t||a){window.history.replaceState({},'',window.location.pathname);}
  var tok=localStorage.getItem(KEY);
  if(!tok){window.location.href='https://moneypicksarena.com'; return;}
})();
// Auto-enable admin view if this logged-in user is the admin (cosmetic — the
// server independently re-verifies before honoring any force refresh).
function _nbaUnlockDates(){try{var dp=document.getElementById('datePicker');if(dp){dp.removeAttribute('min');dp.removeAttribute('max');}}catch(e){}}
if(window.IS_ADMIN){document.body.classList.add('is-admin');_nbaUnlockDates();}else{var _at=localStorage.getItem('__mpa_token')||'';var _ak=localStorage.getItem('__mpa_admin')||'';if(_at||_ak){fetch('/api/whoami?_tok='+encodeURIComponent(_at)+'&admin='+encodeURIComponent(_ak)).then(r=>r.json()).then(d=>{if(d&&d.is_admin){window.IS_ADMIN=true;document.body.classList.add('is-admin');_nbaUnlockDates();}}).catch(function(){});}}
let nbaHotColdData=null;
let top10=[], allPicksData=[], nbaPropsData=[], nbaApprovedPicks=[], activeTopStat='ALL', activeTopSide='ALL', activeAllStat='ALL', sideFilter=null;

function pctClass(p){return p>=90?['pct-green','bar-green']:p>=80?['pct-yellow','bar-yellow']:['pct-orange','bar-orange']}
function _nbaHistImplied(odds){
  const n=parseFloat(String(odds==null?'':odds).replace('+',''));
  if(!n||isNaN(n))return 0;
  return n>0?100/(n+100):Math.abs(n)/(Math.abs(n)+100);
}
function _nbaNormalizeHistoricalData(data){
  if(!data||!data.historical_replay)return data;
  const best=new Map();
  (data.all_picks||data.picks||[]).forEach(p=>{
    // Available quotes without a qualifying matchup signal are not picks.
    if(!['OVER','UNDER'].includes(p.pick)||!p.games||p.pct==null)return;
    const key=`${String(p.player||'').toLowerCase()}|${p.stat||''}|${p.event_id||p.matchup||''}`;
    // The server owns standard-line selection; never replace it using juice.
    if(!best.has(key))best.set(key,p);
  });
  const clean=Array.from(best.values()).sort((a,b)=>
    Number(b.mpg!=null&&b.mpg>=24)-Number(a.mpg!=null&&a.mpg>=24) ||
    (b.pct||0)-(a.pct||0) || (b.games||0)-(a.games||0) ||
    String(a.player||'').localeCompare(String(b.player||'')));
  const statOrder=['PTS','REB','AST','FG3M','PRA','PTS_REB','PTS_AST','REB_AST','BLK','STL'];
  const pools={};statOrder.forEach(s=>pools[s]=clean.filter(p=>p.stat===s));
  const balanced=[],seenPlayers=new Set();let depth=0,added=true;
  while(balanced.length<12&&added){
    added=false;
    for(const s of statOrder){
      if(pools[s]&&pools[s][depth]){
        added=true;
        const candidate=pools[s][depth];
        const playerKey=String(candidate.player||'').trim().toLowerCase();
        if(seenPlayers.has(playerKey))continue;
        seenPlayers.add(playerKey);
        balanced.push(candidate);
        if(balanced.length===12)break;
      }
    }
    depth++;
  }
  data.all_picks=clean;
  data.picks=balanced;
  data.total=clean.length;
  return data;
}
function statTag(s){
  const m={PTS:['tag-pts','Points'],REB:['tag-reb','Rebounds'],AST:['tag-ast','Assists'],FG3M:['tag-fg3m','3-Pointers'],PRA:['tag-pra','Pts+Reb+Ast'],PTS_REB:['tag-combo','Pts+Reb'],PTS_AST:['tag-combo','Pts+Ast'],REB_AST:['tag-combo','Reb+Ast'],BLK:['tag-blk','Blocks'],STL:['tag-stl','Steals']};
  const [c,l]=m[s]||['',''];
  return `<span class="stat-tag ${c}">${l}</span>`;
}
function rankClass(i){return i===0?'rank-1':i===1?'rank-2':i===2?'rank-3':'rank-other'}

function filterStat(stat){
  activeTopStat=stat;
  activeTopSide='ALL';
  document.querySelectorAll('#filterBar .filter-btn[data-stat]').forEach(b=>b.classList.toggle('active',b.dataset.stat===stat));
  document.querySelectorAll('#filterBar .filter-btn[data-top-side]').forEach(b=>b.classList.toggle('active',b.dataset.topSide==='ALL'));
  const chooser=document.getElementById('topSideChooser');
  if(chooser) chooser.style.display=stat==='ALL'?'none':'flex';
  if(stat!=='ALL'){
    const names={PTS:'Points',REB:'Rebounds',AST:'Assists',FG3M:'3-Pointers',PRA:'PRA',PTS_REB:'Pts+Reb',PTS_AST:'Pts+Ast',REB_AST:'Reb+Ast',BLK:'Blocks',STL:'Steals'};
    const name=names[stat]||stat;
    const both=document.getElementById('topBothBtn'),over=document.getElementById('topOverBtn'),under=document.getElementById('topUnderBtn');
    if(both) both.textContent=name+' — Both';
    if(over) over.textContent=name+' Over Top 10';
    if(under) under.textContent=name+' Under Top 10';
  }
  renderTopCategory();
}

function filterTopSide(side){
  activeTopSide=side;
  document.querySelectorAll('#filterBar .filter-btn[data-top-side]').forEach(b=>b.classList.toggle('active',b.dataset.topSide===side));
  renderTopCategory();
}

function _nbaTopSideOf(p){
  return p&&p.historical_replay&&p.side?p.side:_nbaPickVerdict(p);
}

function renderTopCategory(){
  let source;
  if(activeTopStat==='ALL'&&activeTopSide==='ALL'){
    source=top10;
  }else{
    source=allPicksData.filter(p=>
      (activeTopStat==='ALL'||p.stat===activeTopStat)&&
      (activeTopSide==='ALL'||_nbaTopSideOf(p)===activeTopSide)
    ).slice(0,activeTopSide==='ALL'?12:10);
  }
  renderTop10Cards(source);
}

function filterAll(stat){
  activeAllStat=stat;
  document.querySelectorAll('#allFilterBar .filter-btn[data-stat]').forEach(b=>b.classList.toggle('active',b.dataset.stat===stat));
  applyAllFilters();
}

function toggleSide(side){
  sideFilter = (sideFilter===side) ? null : side;
  document.getElementById('oversBtn').classList.toggle('active',sideFilter==='OVER');
  document.getElementById('undersBtn').classList.toggle('active',sideFilter==='UNDER');
  applyAllFilters();
}

function applyAllFilters(){
  const searchQ=(document.getElementById('playerSearchInput')||{}).value||'';
  const sq=searchQ.toLowerCase().trim();
  let filtered = activeAllStat==='ALL' ? allPicksData : allPicksData.filter(p=>p.stat===activeAllStat);
  if(sq) filtered=filtered.filter(p=>(p.player||'').toLowerCase().includes(sq));
  if(sideFilter){
    filtered = filtered.filter(p => p.side===sideFilter || p.line_rec===sideFilter || p.streak_rec===sideFilter || p.alt_rec===sideFilter);
    // Rank by gap: biggest +gap first for OVERs, biggest -gap first for UNDERs
    filtered = filtered.slice().sort((a,b)=>{
      const ga=a.gap==null?0:a.gap, gb=b.gap==null?0:b.gap;
      return sideFilter==='OVER' ? (gb-ga) : (ga-gb);
    });
  }
  document.getElementById('totalCount').textContent=filtered.length;
  renderAllByGame(filtered, sideFilter);
}

function renderTop10Cards(picks){
  if(!picks.length){
    document.getElementById('content').innerHTML='<div class="msg-card"><span class="ico"></span><h2>No picks</h2><p>Try "All Stats".</p></div>';
    return;
  }
  // Group picks by player so each player gets ONE trading-card; first occurrence wins rank order.
  const byPlayer={},order=[];
  picks.forEach(p=>{ if(!byPlayer[p.player]){byPlayer[p.player]=[];order.push(p.player);} byPlayer[p.player].push(p); });
  const dirColor=d=>d==='OVER'?'#4ade80':d==='UNDER'?'#f87171':'#9ca3af';
  const dirBg=d=>d==='OVER'?'rgba(74,222,128,.14)':d==='UNDER'?'rgba(239,68,68,.14)':'rgba(156,163,175,.1)';
  const _statTitle={PTS:'Points',REB:'Rebounds',AST:'Assists',FG3M:'3-Pointers',PRA:'Pts+Reb+Ast',PTS_REB:'Pts+Reb',PTS_AST:'Pts+Ast',REB_AST:'Reb+Ast',BLK:'Blocks',STL:'Steals'};
  const _catName=activeTopStat==='ALL'?'All Stats':(_statTitle[activeTopStat]||activeTopStat);
  const _boardTitle=activeTopSide==='ALL'
    ? (activeTopStat==='ALL'?'Top Picks Today':`${_catName} Picks`)
    : `${_catName} · Top 10 ${activeTopSide==='OVER'?'Over':'Under'}`;
  let html=`<div class="section-hdr"><div class="section-title">${_boardTitle}</div><span class="count-pill">${order.length} player${order.length!==1?'s':''}</span></div><div class="picks-grid nbc-grid">`;
  order.forEach((pname,i)=>{
    // Only show the single best pick per player card (highest-ranked, since
    // picks are pre-sorted by has_consistency desc, hit_rate desc, threshold desc).
    const stats=[byPlayer[pname][0]];
    const p=stats[0];
    const formBadge=p.form_badge==='HOT'
      ? '<span title="Last-5 production is at least 15% above this player’s season average, with stable minutes." style="background:rgba(249,115,22,.2);color:#fb923c;border:1px solid #fb923c88;padding:4px 9px;border-radius:999px;font-size:.68rem;font-weight:900">HOT</span>'
      : p.form_badge==='COLD'
      ? '<span title="Last-5 production is at least 15% below this player’s season average, with stable minutes." style="background:rgba(96,165,250,.18);color:#60a5fa;border:1px solid #60a5fa88;padding:4px 9px;border-radius:999px;font-size:.68rem;font-weight:900">COLD</span>'
      : '';
    const cardKey=ladReg(p);
    const teamLogo=`https://a.espncdn.com/i/teamlogos/nba/500/${(p.team||'').toLowerCase()}.png`;
    const headshot=p.headshot||(
      p.player_id?`https://a.espncdn.com/i/headshots/nba/players/full/${p.player_id}.png`:''
    );
    const tip=p.tipoff?new Date(p.tipoff).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',timeZoneName:'short'}):'';
    const s=p;
    const loc=(p.location||'').toLowerCase();
    // VERDICT RULES (unchanged):
    // 1) PATTERN is the strongest signal. If it qualifies, the pick IS the pattern.
    // 2) If no pattern, fall back to vote-based verdict from LINE/STREAK/MPA.
    let verdict=null, verdictText='', verdictColor='#9ca3af';
    if(s.historical_replay && s.side){
      verdict=s.side; verdictText=`${s.side} ${s.line}`; verdictColor=dirColor(s.side);
    } else if(s.has_consistency){
      verdict='PATTERN'; verdictText=`PICK ${s.threshold}+`; verdictColor='#FDB827';
    } else {
      const votes={OVER:0,UNDER:0};
      if(s.line_rec) votes[s.line_rec]++;
      if(s.streak_rec) votes[s.streak_rec]++;
      if(s.alt_rec) votes[s.alt_rec]++;
      const tot=votes.OVER+votes.UNDER;
      if(tot && votes.OVER!==votes.UNDER){
        verdict=votes.OVER>votes.UNDER?'OVER':'UNDER';
        verdictText=`${verdict}${s.dk_line!=null?' '+s.dk_line:''}`;
        verdictColor=dirColor(verdict);
      }
    }
    // When a pattern qualifies, only show signals that agree (OVER) as reinforcement.
    const patternOverride=s.has_consistency;
    const sig=(c,t)=>`<span class="nbc-sig" style="color:${c};background:${c}1f;border-color:${c}55">${t}</span>`;
    const badges=[];
    if(s.historical_replay && s.side) badges.push(sig(dirColor(s.side),`${s.side} ${s.line} · ${s.book||'Archived sportsbook'}`));
    if(s.line_rec && (!patternOverride || s.line_rec==='OVER')) badges.push(sig(dirColor(s.line_rec),`LINE ${s.line_rec} ${s.line_rec_hits} (${s.line_rec_pct}%)`));
    if(s.streak_rec && (!patternOverride || s.streak_rec==='OVER')) badges.push(sig(dirColor(s.streak_rec),`🔥 ${s.streak_n} STRAIGHT ${s.streak_rec}`));
    if(s.alt_rec && (!patternOverride || s.alt_rec==='OVER')) badges.push(sig(dirColor(s.alt_rec),`⭐ MPA ${s.alt_rec}${s.alt_evens&&s.alt_odds?` · even ${s.alt_evens} · odd ${s.alt_odds}`:''}`));
    // Hero line: the pick plus both sportsbook prices.
    const _ov=s.dk_over_odds||s.fd_odds||'';
    const _un=s.dk_under_odds||'';
    const priceHtml=(_ov||_un)
      ? `${_ov?`<div class="nbc-px"><span>O</span><b>${_ov}</b></div>`:''}${_un?`<div class="nbc-px"><span>U</span><b>${_un}</b></div>`:''}`
      : `<div class="nbc-px nbc-px-none">${s.dk_line!=null?'Odds n/a':'No book line'}</div>`;
    const hero=`<div class="nbc-hero" style="border-color:${verdictColor}55;background:linear-gradient(135deg,${verdictColor}1a,rgba(255,255,255,.015))">
        <div class="nbc-hero-l">
          <div class="nbc-verdict" style="color:${verdictColor}">${verdictText||'NO CONSENSUS'}</div>
          <div class="nbc-hero-sub">${s.dk_line!=null?`Book line ${s.dk_line} · `:''}${s.stat_label}</div>
        </div>
        <div class="nbc-hero-r">${priceHtml}</div>
      </div>`;
    // Stat tiles
    const tiles=[];
    const tile=(k,v)=>tiles.push(`<div class="nbc-tile"><div class="k">${k}</div><div class="v">${v}</div></div>`);
    const pctClr=v=>v>=80?'#4ade80':v>=65?'#fbbf24':'#f87171';
    if(s.games && (s.threshold || (s.historical_replay && s.pick))){
      tile(`${s.threshold?s.threshold+'+':s.pick+' '+s.line} vs ${p.opp} ${loc}`,`<span style="color:${pctClr(s.pct)}">${s.hits}/${s.games}</span> <small>${s.pct}%</small>`);
    }
    if(s.dk_line!=null && s.dk_hits!=null){
      const over=s.dk_hits, under=(s.l10_games||10)-over;
      tile(`vs line L${s.l10_games||10} · ${p.opp} ${loc}`,`<span style="color:#4ade80">${over}O</span> <span class="nbc-dim">·</span> <span style="color:#f87171">${under}U</span>`);
    }
    if(s.recent_avg!=null){
      const g=s.gap!=null?s.gap:null;
      const gc=g==null?'#94a3b8':g>0?'#4ade80':g<0?'#f87171':'#94a3b8';
      tile('L10 all-opp avg',`${s.recent_avg}${g==null?'':` <small style="color:${gc}">${g>0?'+':''}${g}</small>`}`);
    }
    if(s.best_bet){
      const bb=s.best_bet, isPass=bb.side==='PASS';
      const bc=isPass?'#9ca3af':dirColor(bb.side);
      const cc=bb.conf==='STRONG'?'#4ade80':bb.conf==='LEAN'?'#fbbf24':'#9ca3af';
      tile('Best bet',`<span style="color:${bc}">${isPass?'NO EDGE '+bb.line:bb.side+' '+bb.line}</span> <small>${bb.hits}/${bb.games} (${bb.pct}%)</small> <small style="color:${cc};font-weight:900">${bb.conf}</small>`);
    }
    // Pre-game form strip (historical replay)
    let recentHtml='';
    if(s.historical_replay && s.pick && s.games){
      const _recent=(s.recent_glog||[]).slice(0,10);
      if(_recent.length){
        const ln=Number(s.line);
        recentHtml=`<div class="nbc-form"><div class="nbc-form-k">Last ${_recent.length} ${s.stat_label} before game</div><div class="nbc-chips">${_recent.map(x=>{
          const v=Number(x.v), hit=isFinite(ln)&&isFinite(v)?(s.pick==='UNDER'?v<ln:v>ln):null;
          const c=hit==null?'#94a3b8':hit?'#4ade80':'#f87171';
          return `<span style="color:${c};border-color:${c}55;background:${c}14">${x.v}</span>`;}).join('')}</div></div>`;
      }else if(s.recent_avg!=null){
        recentHtml=`<div class="nbc-form"><div class="nbc-form-k">Pre-game L10 ${s.stat_label} average: <b>${s.recent_avg}</b></div></div>`;
      }
    }
    const why=s.threshold
      ? `<div class="nbc-why"><span class="nbc-why-k">Pick history</span>Hit <b style="color:#FDB827">${s.threshold}+</b> ${s.stat_label} in <b>${s.hits}/${s.games}</b> vs ${p.opp} ${loc}</div>`
      : '';
    // Footer score = strongest verified hit rate on the card.
    const scorePct=s.games&&s.pct!=null?s.pct:(s.best_bet&&s.best_bet.side!=='PASS'?s.best_bet.pct:(s.line_rec_pct!=null?s.line_rec_pct:null));
    const ini=String(pname||'').split(/ +/).map(w=>w.charAt(0)).join('').slice(0,2).toUpperCase();
    const mkt=String(p.stat||'').toUpperCase();
    const acc={PTS:'#a78bfa',REB:'#60a5fa',AST:'#34d399',FG3M:'#f87171',PRA:'#c084fc',BLK:'#2dd4bf',STL:'#f472b6'}[mkt]||'#fbbf24';
    const _bverd=_nbaPickVerdict(p);
    const _betHtml=(window.IS_ADMIN&&!p.historical_replay)?_nbaBetBtn(p,_bverd):'';
    html+=`
    <div class="pick-card nbc-card" style="--acc:${acc}">
      <div class="nbc-rank">${i+1}</div>
      <div class="nbc-body">
        <div class="nbc-top">
          <div class="nbc-hs" onclick="openLadder('${cardKey}')" title="Open game log">
            <span class="nbc-ini">${ini}</span>
            ${headshot?`<img class="nbc-hs-img" src="${headshot}" alt="${pname}" onerror="if(this.dataset.fallback){this.style.display='none';return;}this.dataset.fallback='1';this.src='https://a.espncdn.com/i/headshots/nba/players/full/${p.player_id||''}.png'"/>`:''}
            <img class="nbc-logo" src="${teamLogo}" alt="${p.team}" onerror="this.style.display='none'"/>
          </div>
          <div class="nbc-id">
            <div class="nbc-name">${pname}</div>
            <div class="nbc-meta"><b>${p.team}</b> vs <b>${p.opp}</b>${loc?`<span class="nbc-loc ${loc==='home'?'home':'away'}">${loc.toUpperCase()}</span>`:''}${p.position?`<span class="nbc-pos">${p.position}</span>`:''}</div>
            <div class="nbc-meta2">${tip?`⏱ ${tip}`:''}${tip&&p.matchup?' · ':''}${p.matchup||''}</div>
          </div>
        </div>
        <div class="nbc-tagrow"><span class="nbc-mkt">${s.emoji||''} ${s.stat_label}</span>${formBadge}</div>
        ${hero}
        ${tiles.length?`<div class="nbc-tiles">${tiles.join('')}</div>`:''}
        ${recentHtml}
        ${why}
        ${badges.length?`<div class="nbc-sigs">${badges.join('')}</div>`:''}
        <div class="nbc-foot">
          <div class="nbc-score" style="color:${scorePct==null?'#64748b':pctClr(scorePct)}">${scorePct==null?'—':scorePct+'%'}<small>hit rate</small></div>
          <button class="nbc-tap" onclick="openLadder('${cardKey}')">📊 Game Log</button>
        </div>
      </div>
      ${_betHtml}
    </div>`;
  });
  html+='</div>';
  document.getElementById('content').innerHTML=html;
}

function renderAllByGame(picks){
  const el=document.getElementById('allPicksSection');
  if(!picks.length){el.innerHTML='<div class="msg-card" style="padding:30px"><span class="ico"></span><p>No picks for this filter.</p></div>';return;}
  const groups={},order=[];
  for(const p of picks){if(!groups[p.matchup]){groups[p.matchup]=[];order.push(p.matchup);}groups[p.matchup].push(p);}
  let html='';
  for(const matchup of order){
    const gp=groups[matchup];
    const gameId='g_'+matchup.replace(/[^a-z0-9]/gi,'_');
    html+=`<div class="game-group">
      <div class="game-group-hdr" onclick="toggleGroup('${gameId}',this)">
        <span class="gg-label"> ${matchup}</span>
        <div class="gg-meta"><span class="count-pill">${gp.length} pick${gp.length!==1?'s':''}</span><span class="gg-chevron"></span></div>
      </div>
      <div class="compact-picks" id="${gameId}" style="display:none">`;
    // Sub-group by player so each player has one expandable row
    const byPlayer = {}; const playerOrder = [];
    for(const p of gp){
      if(!byPlayer[p.player]){byPlayer[p.player]=[];playerOrder.push(p.player);}
      byPlayer[p.player].push(p);
    }
    for(const pname of playerOrder){
      const rows = byPlayer[pname];
      const first = rows[0];
      const pid = gameId+'_'+pname.replace(/[^a-z0-9]/gi,'_');
      // Best % across this player's picks for summary chip
      const bestPct = Math.max(...rows.map(r=>r.pct||0));
      const stats = rows.map(r=>r.stat_label).join(' · ');
      html+=`<div class="player-group" style="border-bottom:1px solid #1f1f1f">
        <div onclick="togglePlayer('${pid}',this)" style="display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;background:#141414">
          <span style="font-size:1.1rem">${first.emoji}</span>
          <div style="flex:1;min-width:0">
            <div style="font-weight:700;color:#fff;font-size:.88rem">${pname} <span style="color:#1e3a5f;font-size:.65rem">${first.team}${first.location==='Home'?' HOME':' AWAY'}</span> ${first.form_badge?`<span style="color:${first.form_badge==='HOT'?'#fb923c':'#60a5fa'};font-size:.62rem;font-weight:900">${first.form_badge}</span>`:''}</div>
            <div style="color:#777;font-size:.7rem;margin-top:2px">${rows.length} pick${rows.length!==1?'s':''} · ${stats}</div>
          </div>
          ${bestPct>0?`<span style="color:#fbbf24;font-weight:700;font-size:.78rem">${bestPct}%</span>`:''}
          <span class="pg-chevron" style="color:#666;font-size:.8rem;transition:transform .2s">▼</span>
        </div>
        <div id="${pid}" style="display:none;flex-direction:column;background:#0e0e0e">`;
      for(const p of rows){
        const [pc,bc]=pctClass(p.pct);
        const ladKey=ladReg(p);
        const badges = [];
        if(p.form_badge) badges.push(`<span title="Display-only last-5 form versus season average" style="background:${p.form_badge==='HOT'?'rgba(249,115,22,.15)':'rgba(96,165,250,.15)'};color:${p.form_badge==='HOT'?'#fb923c':'#60a5fa'};padding:2px 7px;border-radius:6px;font-size:.65rem;font-weight:800;margin-right:4px">${p.form_badge}</span>`);
        if(p.has_consistency) badges.push(`<span style="background:rgba(245,158,11,.15);color:#fbbf24;padding:2px 7px;border-radius:6px;font-size:.65rem;font-weight:700;margin-right:4px">PICK ${p.pct}%</span>`);
        if(p.historical_replay && p.side) badges.push(`<span style="background:${p.side==='UNDER'?'rgba(239,68,68,.15)':'rgba(74,222,128,.15)'};color:${p.side==='UNDER'?'#f87171':'#4ade80'};padding:2px 7px;border-radius:6px;font-size:.65rem;font-weight:700;margin-right:4px">${p.side} ${p.line} · ${p.book||'Archived sportsbook'}</span>`);
        const loc=(p.location||'').toLowerCase();
        if(p.line_rec) badges.push(`<span style="background:${p.line_rec==='UNDER'?'rgba(239,68,68,.15)':'rgba(74,222,128,.15)'};color:${p.line_rec==='UNDER'?'#f87171':'#4ade80'};padding:2px 7px;border-radius:6px;font-size:.65rem;font-weight:700;margin-right:4px">LINE ${p.line_rec} ${p.dk_line} ${p.line_rec_pct}% vs ${p.opp} ${loc}</span>`);
        if(p.streak_rec) badges.push(`<span style="background:rgba(249,115,22,.15);color:#fb923c;padding:2px 7px;border-radius:6px;font-size:.65rem;font-weight:700;margin-right:4px">🔥 ${p.streak_n} in a row ${p.streak_rec} ${p.dk_line} vs ${p.opp} ${loc}</span>`);
        if(p.alt_rec) badges.push(`<span style="background:rgba(168,85,247,.15);color:#c084fc;padding:2px 7px;border-radius:6px;font-size:.65rem;font-weight:700;margin-right:4px">⭐ MPA SPECIAL ${p.alt_rec} ${p.dk_line} vs ${p.opp} ${loc}${p.alt_evens&&p.alt_odds?` (even: ${p.alt_evens} · odd: ${p.alt_odds})`:''}</span>`);
        const patternLine = p.has_consistency
          ? (() => { const pl = (p.dk_line!=null?p.dk_line:p.fd_line); return `${p.threshold}+ ${p.stat_label}  ${p.hits}/${p.games} vs ${p.opp}${pl!=null ? `  <span class="fd-inline"> ${pl}</span>` : ''}`; })()
          : `${p.stat_label} vs ${p.opp}${p.dk_line ? `  line ${p.dk_line}` : ''}`;
        html+=`<div onclick="openLadder('${ladKey}')" style="display:flex;align-items:center;gap:10px;padding:8px 14px 8px 38px;border-top:1px solid #1a1a1a;cursor:pointer">
          <div style="flex:1;min-width:0">
            <div style="color:#bbb;font-size:.78rem">${patternLine} <span style="color:#FDB827;font-size:.62rem;font-weight:800">📊 TAP</span></div>
            <div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:3px">${badges.join('')}</div>
          </div>
          
          ${p.has_consistency ? `<div style="text-align:right"><div class="cr-pct ${pc}" style="font-size:.85rem;font-weight:800">${p.pct}%</div><div style="color:#666;font-size:.65rem">${p.hits}/${p.games}</div></div>` : ''}
        </div>`;
      }
      html+='</div></div>';
    }
    html+='</div></div>';
  }
  el.innerHTML=html;
}

function toggleSig(id,hdr){
  const el=document.getElementById(id);
  if(!el)return;
  const ch=hdr.querySelector('.sig-chev');
  const hidden=el.style.display==='none';
  el.style.display=hidden?'block':'none';
  if(ch)ch.style.transform=hidden?'rotate(180deg)':'';
}
function renderSignalLists(picks){
  // Build set of player|stat combos that have a strong PATTERN (always OVER-direction).
  // Any STREAK/MPA UNDER on the same combo contradicts the pattern, so suppress it.
  const patternKeys=new Set((picks||[]).filter(p=>p.has_consistency).map(p=>`${p.player}|${p.stat}`));
  const contradicts=p=>{const k=`${p.player}|${p.stat}`;return patternKeys.has(k);};
  const streaks=(picks||[]).filter(p=>p.streak_rec && !(p.streak_rec==='UNDER' && contradicts(p))).slice().sort((a,b)=>(b.streak_n||0)-(a.streak_n||0));
  const mpas=(picks||[]).filter(p=>p.alt_rec);
  const sc=document.getElementById('streakCount'); if(sc) sc.textContent=streaks.length;
  const mc=document.getElementById('mpaCount'); if(mc) mc.textContent=mpas.length;
  const dirColor=d=>d==='OVER'?'#4ade80':d==='UNDER'?'#f87171':'#9ca3af';
  const dirBg=d=>d==='OVER'?'rgba(74,222,128,.14)':d==='UNDER'?'rgba(239,68,68,.14)':'rgba(156,163,175,.1)';
  const row=(p,sigHTML)=>{const k=ladReg(p);return `<div onclick="openLadder('${k}')" style="display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;padding:9px 14px;border-bottom:1px solid #1a1a1a;cursor:pointer">
    <div style="min-width:0">
      <div style="font-weight:700;color:#fff;font-size:.82rem">${p.emoji} ${p.player} <span style="color:#777;font-size:.7rem">${p.team} vs ${p.opp}</span></div>
      <div style="color:#999;font-size:.7rem;margin-top:2px">${p.stat_label}${p.dk_line?` · line ${p.dk_line}`:''} <span style="color:#FDB827;font-size:.6rem;font-weight:800">📊 TAP</span></div>
    </div>
    ${sigHTML}
  </div>`;};
  const sl=document.getElementById('streakList');
  if(sl) sl.innerHTML = streaks.length ? streaks.map(p=>row(p,`<span style="background:${dirBg(p.streak_rec)};color:${dirColor(p.streak_rec)};border:1px solid ${dirColor(p.streak_rec)}55;padding:4px 9px;border-radius:6px;font-weight:900;font-size:.72rem;white-space:nowrap">🔥 ${p.streak_n} ${p.streak_rec}</span>`)).join('') : '<div style="padding:18px;text-align:center;color:#555;font-size:.78rem">No streaks today</div>';
  const ml=document.getElementById('mpaList');
  if(ml) ml.innerHTML = mpas.length ? mpas.map(p=>row(p,`<span style="background:${dirBg(p.alt_rec)};color:${dirColor(p.alt_rec)};border:1px solid ${dirColor(p.alt_rec)}55;padding:4px 9px;border-radius:6px;font-weight:900;font-size:.72rem;white-space:nowrap">⭐ ${p.alt_rec}${p.alt_evens&&p.alt_odds?` (even: ${p.alt_evens} · odd: ${p.alt_odds})`:''}</span>`)).join('') : '<div style="padding:18px;text-align:center;color:#555;font-size:.78rem">No MPA specials today</div>';
}
// ===== Admin Auto Parlay Builder =====
function _amToDec(a){var s=String(a==null?'':a).replace('+','').trim();var n=parseFloat(s);if(!n||isNaN(n))return null;return n>0?1+n/100:1+100/Math.abs(n);}
function _decToAm(d){if(!d||d<=1)return null;return d>=2?'+'+Math.round((d-1)*100):'-'+Math.round(100/(d-1));}
function _mpaRate(p){function parse(s){if(!s)return[0,0];var m=String(s).split('/');return[parseFloat(m[0])||0,parseFloat(m[1])||0];}var e=parse(p.alt_evens),o=parse(p.alt_odds);var h=e[0]+o[0],t=e[1]+o[1];return t?h/t*100:0;}
function _fmtOdds(o){if(o==null||o==='')return null;var s=String(o).trim();return (s.charAt(0)==='-'||s.charAt(0)==='+')?s:'+'+s;}
function _legCandidates(p){
  var pat = !!p.has_consistency; // PATTERN is always OVER-direction
  var line = (p.dk_line!=null?p.dk_line:p.fd_line);
  function oddsFor(dir){ return dir==='OVER' ? (p.dk_over_odds||p.fd_odds||'') : (p.dk_under_odds||''); }
  var c=[];
  if(pat){ c.push({type:'PICK',dir:'OVER',conf:(p.pct||0),reason:'📊 PICK '+(p.hits||0)+'/'+(p.games||0)+' ('+(p.pct||0)+'%) vs '+p.opp}); }
  if(p.line_rec && !(pat && p.line_rec!=='OVER')){ c.push({type:'LINE',dir:p.line_rec,conf:(p.line_rec_pct||0),reason:'📈 LINE '+p.line_rec+' '+(p.line_rec_hits||'')+' ('+(p.line_rec_pct||0)+'%) vs '+p.opp}); }
  if(p.streak_rec && !(pat && p.streak_rec!=='OVER')){ var n=p.streak_n||0; c.push({type:'STREAK',dir:p.streak_rec,conf:Math.min(99,85+n),reason:'🔥 '+n+'-game '+p.streak_rec+' streak vs '+p.opp}); }
  if(p.alt_rec && !(pat && p.alt_rec!=='OVER')){ c.push({type:'MPA',dir:p.alt_rec,conf:Math.round(_mpaRate(p)),reason:'⭐ MPA '+p.alt_rec+((p.alt_evens&&p.alt_odds)?(' (even '+p.alt_evens+' · odd '+p.alt_odds+')'):'')}); }
  c.forEach(function(x){ x.p=p; x.player=p.player; x.team=p.team; x.opp=p.opp; x.stat=p.stat_label||p.stat; x.emoji=p.emoji||''; x.line=line; x.odds=oddsFor(x.dir); x.dec=_amToDec(x.odds); x.hasOdds=!!x.dec; x.mpg=(p.mpg!=null?p.mpg:null); });
  return c;
}
function _legScore(c){
  // Priced legs first, then signal strength, then bigger payout odds as a tiebreaker.
  // SOFT STARTER LEAN: players at 20+ mpg get an ~11.5 confidence-point bonus so
  // starters float to the top, but a role-player whose signal is 12+ points higher
  // still wins (conf values are integers, so 11.5 makes the boundary deterministic).
  // To convert back, delete the starterBonus term below.
  var starterBonus = (c.mpg!=null && c.mpg>=20) ? 11.5*1e4 : 0;
  return (c.hasOdds?1:0)*1e9 + (c.conf||0)*1e4 + starterBonus + (c.dec?Math.min(c.dec,11)*100:0);
}
// ODDS FLOOR: a priced leg only qualifies at -500 or better (drops -920-type juice),
// OVER or UNDER. Unpriced legs (no odds) are left alone as last-resort filler.
// To convert back, remove the _floorOk gate in _parlayPool below.
function _floorOk(odds){ if(odds==null||odds==='') return true; var a=parseFloat(odds); if(isNaN(a)||a===0) return true; return a>=-500; }
function _parlayPool(){
  // HARD MINUTES FLOOR: players under this mpg never enter the parlay builder — low-minute
  // guys are unreliable, especially in the playoffs when rotations shrink. Unknown mpg is
  // kept (rare). This is a hard cut, separate from the soft 20-mpg starter lean in
  // _legScore. To revert, set _MIN_MPG to 0.
  var _MIN_MPG=18;
  // ONE PLAY PER PLAYER+STAT: a player can contribute several plays (Points, Rebounds,
  // Assists ...), keeping the best signal for each stat — this widens the pool toward the
  // full count of strong signals. The parlay builder still never uses the same player twice
  // in one ticket (enforced in _renderParlay). To collapse back to one play per player,
  // key on c.player alone.
  var byKey={};
  (allPicksData||[]).forEach(function(p){
    if(p.historical_replay) return; // replay rows are deliberately unpriced
    _legCandidates(p).forEach(function(c){
      if(c.mpg!=null && c.mpg<_MIN_MPG) return;
      if(!_floorOk(c.odds)) return;
      var key=c.player+'|'+c.stat;
      var cur=byKey[key];
      if(!cur || _legScore(c)>_legScore(cur)) byKey[key]=c;
    });
  });
  // Optional Hot / Cold legs: read the live checkboxes at build time.
  var hcForms={};Array.prototype.slice.call(document.querySelectorAll('input[name="parlayHcForm"]:checked')).forEach(function(el){hcForms[el.value]=1;});
  var mpgOf={};(allPicksData||[]).forEach(function(p){if(p.mpg!=null)mpgOf[p.player]=p.mpg;});
  (Array.isArray(nbaHotColdData)?nbaHotColdData:[]).forEach(function(r){
    if(!hcForms[r.form_key]) return;
    var t=r.form_key==='double_cold'?'DCOLD':(r.form==='HOT'?'HOT':'COLD');
    var c={type:t,dir:r.pick,conf:Math.round((r.recent_hits||0)/Math.max(1,r.recent_total||1)*100),
      reason:(t==='HOT'?'🔥 HOT ':(t==='DCOLD'?'❄️ DOUBLE COLD ':'🧊 COLD '))+r.recent_hits+'/'+r.recent_total+' last '+r.recent_total+' (avg '+r.recent_avg+')'+(r.opp_total?' · '+r.opp_hits+'/'+r.opp_total+' vs '+r.opp:''),
      p:r,player:r.player,team:r.team,opp:r.opp,stat:r.stat_label,emoji:r.emoji||'',line:r.line,
      odds:r.odds==null?'':((r.odds>0?'+':'')+r.odds),mpg:(mpgOf[r.player]!=null?mpgOf[r.player]:null)};
    c.dec=_amToDec(c.odds);c.hasOdds=!!c.dec;
    if(c.mpg!=null && c.mpg<_MIN_MPG) return;
    if(!_floorOk(c.odds)) return;
    var key=c.player+'|'+c.stat,cur=byKey[key];
    if(!cur || _legScore(c)>_legScore(cur)) byKey[key]=c;
  });
  return Object.keys(byKey).map(function(k){return byKey[k];}).sort(function(a,b){return _legScore(b)-_legScore(a);});
}
function _shuffle(a){ for(var i=a.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1));var t=a[i];a[i]=a[j];a[j]=t;} return a; }
function closeParlay(){ var o=document.getElementById('parlayResult'); if(o) o.innerHTML=''; }
function buildParlay(){ _renderParlay(false); }
function generateParlay(){ _renderParlay(true); }
function _renderParlay(randomize){
  var sel=document.getElementById('parlayLegs');
  var n=parseInt(sel?sel.value:'3',10)||3;
  var out=document.getElementById('parlayResult');
  if(!out) return;
  if(!allPicksData||!allPicksData.length){ out.innerHTML='<div style="color:#888;padding:10px">Run today&#39;s picks first, then build a parlay.</div>'; return; }
  var cands=_parlayPool();
  // Capacity is DISTINCT PLAYERS — a parlay never uses the same player twice, even though the
  // pool now holds multiple stat-plays per player.
  var seenP={}; cands.forEach(function(c){ seenP[c.player]=1; });
  var availP=Object.keys(seenP).length;
  if(availP<n){ out.innerHTML='<div style="color:#f87171;padding:10px">Only '+availP+' qualifying player'+(availP!==1?'s':'')+' on the board. Pick a smaller parlay.</div>'; return; }
  // Greedy pick of n legs with DISTINCT players. Pass 1 honors the avoid set (the players in
  // the parlay currently shown, so "Generate New" gives a fresh list for ANY size now, 6-leg
  // included); pass 2 fills any shortfall ignoring the avoid set but still keeping players
  // unique. To drop the fresh-list behavior, pass null as avoidSet below.
  function _pickLegs(ordered, avoidSet){
    var used={}, picked=[], i, c;
    for(i=0;i<ordered.length && picked.length<n;i++){
      c=ordered[i];
      if(used[c.player]) continue;
      if(avoidSet && avoidSet[c.player]) continue;
      used[c.player]=1; picked.push(c);
    }
    for(i=0;i<ordered.length && picked.length<n;i++){
      c=ordered[i];
      if(used[c.player]) continue;
      used[c.player]=1; picked.push(c);
    }
    return picked;
  }
  var legs;
  if(randomize){
    var avoidSet=null;
    if(window._lastParlayPlayers && window._lastParlayPlayers.length){
      avoidSet={}; window._lastParlayPlayers.forEach(function(pl){ avoidSet[pl]=1; });
    }
    legs=_pickLegs(_shuffle(cands.slice()), avoidSet).sort(function(a,b){return _legScore(b)-_legScore(a);});
  } else {
    legs=_pickLegs(cands.slice(), null);
  }
  window._lastParlayPlayers=legs.map(function(l){return l.player;});
  var dec=1, priced=0, missing=0;
  legs.forEach(function(l){ if(l.dec){dec*=l.dec;priced++;}else{missing++;} });
  var am = priced? _decToAm(dec) : null;
  var payout = priced? (100*dec) : null;
  var dirColor=function(d){return d==='OVER'?'#4ade80':d==='UNDER'?'#f87171':'#9ca3af';};
  var tagBg={PICK:'rgba(253,184,39,.16)',LINE:'rgba(74,222,128,.14)',STREAK:'rgba(249,115,22,.16)',MPA:'rgba(168,85,247,.16)',HOT:'rgba(249,115,22,.16)',COLD:'rgba(56,189,248,.16)',DCOLD:'rgba(129,140,248,.18)'};
  var tagFg={PICK:'#FDB827',LINE:'#4ade80',STREAK:'#fb923c',MPA:'#c084fc',HOT:'#fb923c',COLD:'#7dd3fc',DCOLD:'#a5b4fc'};
  var rows=legs.map(function(l,i){var fo=_fmtOdds(l.odds);return '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid #1a1a1a">'
    +'<div style="min-width:0">'
    +'<div style="font-weight:800;color:#fff;font-size:.85rem">'+(i+1)+'. '+(l.emoji||'')+' '+l.player+' <span style="color:#777;font-size:.7rem">'+l.team+' vs '+l.opp+'</span> <span style="background:'+(tagBg[l.type]||'#222')+';color:'+(tagFg[l.type]||'#aaa')+';padding:1px 6px;border-radius:4px;font-size:.6rem;font-weight:800">'+l.type+'</span></div>'
    +'<div style="color:#999;font-size:.72rem;margin-top:2px">'+l.stat+(l.line!=null?(' · line '+l.line):'')+(l.mpg!=null?(' · '+l.mpg+' mpg'):'')+' · '+l.reason+'</div>'
    +'</div>'
    +'<div style="text-align:right;white-space:nowrap">'
    +'<div style="color:'+dirColor(l.dir)+';font-weight:900;font-size:.8rem">'+l.dir+'</div>'
    +'<div style="color:#FDB827;font-size:.72rem;font-weight:800">'+(fo||'odds N/A')+'</div>'
    +'</div></div>';}).join('');
  var header='<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border-bottom:1px solid #262626;background:#121212">'
    +'<span style="font-weight:800;color:#ccc;font-size:.74rem">'+(randomize?'RANDOM MIX':'TOP PLAYS')+'</span>'
    +'<span onclick="closeParlay()" title="Close" style="cursor:pointer;color:#888;font-weight:900;font-size:1.15rem;line-height:1;padding:0 6px">×</span></div>';
  var summary='<div style="display:flex;justify-content:space-between;align-items:center;padding:12px;background:linear-gradient(135deg,rgba(253,184,39,.12),rgba(253,184,39,.02));border-top:1px solid #262626">'
    +'<div style="font-weight:900;color:#FDB827">'+n+'-LEG PARLAY</div>'
    +'<div style="text-align:right">'+(am?('<div style="font-weight:900;color:#4ade80;font-size:1.05rem">'+am+'</div><div style="color:#999;font-size:.7rem">$100 → $'+payout.toFixed(2)+(missing?(' · '+priced+'/'+n+' legs priced'):'')+'</div>'):('<div style="color:#888;font-size:.78rem">No book odds available for these legs</div>'))+'</div>'
    +'</div>';
  out.innerHTML='<div style="background:#0e0e0e;border:1px solid #262626;border-radius:12px;overflow:hidden">'+header+rows+summary+'</div>';
}
function togglePlayer(id,hdr){
  const el=document.getElementById(id);
  if(!el)return;
  const ch=hdr.querySelector('.pg-chevron');
  const hidden=el.style.display==='none';
  el.style.display=hidden?'flex':'none';
  if(ch)ch.style.transform=hidden?'rotate(180deg)':'';
}
function closeLadder(ev){
  if(ev && ev.target && ev.target.id!=='ladderOverlay' && ev.type==='click') return;
  const o=document.getElementById('ladderOverlay'); if(o) o.remove();
}
function ladKeyOf(p){return 'lad_'+((p.player_id||'')+'_'+p.stat+'_'+p.location+'_'+p.opp).replace(/[^a-z0-9]/gi,'_');}
function ladReg(p){const k=ladKeyOf(p);window.__LAD__=window.__LAD__||{};window.__LAD__[k]=p;return k;}
async function openLadder(key){
  const p=(window.__LAD__||{})[key]; if(!p) return;
  const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const loc=(p.location||'').toLowerCase();
  const fmtD=s=>{try{const d=new Date(s);return (d.getMonth()+1)+'/'+d.getDate()+'/'+String(d.getFullYear()).slice(2);}catch(e){return s||'';}};
  const anchor=(p.dk_line!=null?p.dk_line:p.fd_line);
  const invalidThreeLog=String(p.stat||'').toUpperCase()==='FG3M'&&
    (p.glog||[]).concat(p.recent_glog||[]).some(g=>{
      const v=Number(g&&g.v);
      return !Number.isInteger(v)||v<0||v>20;
    });
  if(p.player_id&&(!(p.glog||[]).length||!(p.recent_glog||[]).length||invalidThreeLog)){
    try{
      const dp=document.getElementById('datePicker');
      const tok=localStorage.getItem('__mpa_token')||'';
      const q=new URLSearchParams({date:(dp&&dp.value)||'',player_id:String(p.player_id),
        stat:String(p.stat||''),team:String(p.team||''),opp:String(p.opp||''),
        location:String(p.location||''),_tok:tok});
      const response=await fetch('/api/nba/player-pre-game-log?'+q.toString(),{cache:'no-store'});
      if(response.ok){
        const data=await response.json();
        if(Array.isArray(data.matchup))p.glog=data.matchup;
        if(Array.isArray(data.recent))p.recent_glog=data.recent;
      }
    }catch(e){}
  }
  // Game log vs this opponent at this location
  const glog=p.glog||[];
  const recent=p.recent_glog||[];
  let logHTML;
  if(glog.length){
    logHTML=glog.map(g=>`<div style="display:flex;justify-content:space-between;padding:6px 10px;border-bottom:1px solid #1a1a1a;font-size:.82rem">
      <span style="color:#999">${fmtD(g.d)}</span>
      <span style="color:#fff;font-weight:800">${esc(g.v)}</span></div>`).join('');
  } else {
    logHTML='<div style="padding:14px;color:#666;text-align:center;font-size:.8rem">No prior '+esc(p.stat_label)+' results found vs '+esc(p.opp)+' at this location</div>';
  }
  const recentHTML=recent.length
    ? recent.map(g=>`<div style="display:flex;justify-content:space-between;padding:6px 10px;border-bottom:1px solid #1a1a1a;font-size:.82rem">
        <span style="color:#999">${fmtD(g.d)}</span>
        <span style="color:#fff;font-weight:800">${esc(g.v)}</span></div>`).join('')
    : '<div style="padding:14px;color:#666;text-align:center;font-size:.8rem">No pre-game last-10 results available</div>';
  // Ladder: book line ±3, over/under counts at each
  const lad=p.ladder||[];
  let ladHTML;
  if(lad.length){
    ladHTML=`<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;font-size:.7rem;color:#777;font-weight:800;padding:6px 10px;border-bottom:1px solid #2a2a2a">
        <span>LINE</span><span style="text-align:center;color:#4ade80">OVER</span><span style="text-align:right;color:#f87171">UNDER</span></div>`
      + lad.map(r=>{
        const under=r.games-r.hits;
        const isAnchor=(anchor!=null&&r.t===anchor);
        const opct=r.pct, oc=opct>=70?'#4ade80':opct>=50?'#fbbf24':'#f87171';
        return `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;padding:7px 10px;border-bottom:1px solid #161616;font-size:.84rem;${isAnchor?'background:rgba(253,184,39,.10)':''}">
          <span style="color:#fff;font-weight:${isAnchor?'900':'700'}">${r.t}${isAnchor?' ◄ BOOK':''}</span>
          <span style="text-align:center;color:${oc};font-weight:800">${r.hits}/${r.games} <span style="color:#666;font-size:.7rem">(${opct}%)</span></span>
          <span style="text-align:right;color:#bbb;font-weight:700">${under}/${r.games}</span></div>`;
      }).join('');
  } else {
    ladHTML='<div style="padding:14px;color:#666;text-align:center;font-size:.8rem">No book line posted for this stat</div>';
  }
  closeLadder();
  const ov=document.createElement('div');
  ov.id='ladderOverlay';
  ov.setAttribute('onclick','closeLadder(event)');
  ov.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.78);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px';
  ov.innerHTML=`<div style="background:#101010;border:1px solid #2a2a2a;border-radius:14px;max-width:420px;width:100%;max-height:86vh;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,.6)">
    <div style="background:linear-gradient(135deg,#1e3a5f,#0a1a2e);padding:14px 16px;border-bottom:2px solid #FDB827;display:flex;justify-content:space-between;align-items:flex-start">
      <div>
        <div style="color:#fff;font-weight:900;font-size:1.02rem">${esc(p.emoji)} ${esc(p.player)}</div>
        <div style="color:#FDB827;font-size:.76rem;font-weight:700;margin-top:2px">${esc(p.stat_label)} · ${recent.length} most recent before game${anchor!=null?' · line '+anchor:''}</div>
      </div>
      <span onclick="closeLadder()" style="color:#888;font-size:1.4rem;line-height:1;cursor:pointer;padding:0 4px">×</span>
    </div>
    <div style="padding:12px 14px">
      <div style="color:#888;font-size:.7rem;font-weight:800;letter-spacing:.08em;margin-bottom:5px">ALT LINE HIT RATES (book ±3)</div>
      <div style="background:#0a0a0a;border:1px solid #1f1f1f;border-radius:9px;overflow:hidden;margin-bottom:14px">${ladHTML}</div>
      <div style="color:#888;font-size:.7rem;font-weight:800;letter-spacing:.08em;margin-bottom:5px">LAST ${recent.length||10} ${esc(p.stat_label)} RESULTS BEFORE GAME</div>
      <div style="background:#0a0a0a;border:1px solid #1f1f1f;border-radius:9px;overflow:hidden;margin-bottom:14px">${recentHTML}</div>
      <div style="color:#888;font-size:.7rem;font-weight:800;letter-spacing:.08em;margin-bottom:5px">GAME LOG vs ${esc(p.opp)} (${loc})</div>
      <div style="background:#0a0a0a;border:1px solid #1f1f1f;border-radius:9px;overflow:hidden">${logHTML}</div>
    </div>
  </div>`;
  document.body.appendChild(ov);
}
function toggleGroup(id,hdr){
  const el=document.getElementById(id);
  const ch=hdr.querySelector('.gg-chevron');
  if(!el)return;
  const hidden=el.style.display==='none';
  el.style.display=hidden?'flex':'none';
  if(hidden)el.style.flexDirection='column';
  if(ch)ch.style.transform=hidden?'':'rotate(-90deg)';
}
function renderGames(games){
  var gb=document.getElementById('gamesBar'),notice=document.getElementById('nbaSeasonNotice');
  if(!games||!games.length){
    if(gb){gb.innerHTML='';gb.style.display='none';}
    if(notice){notice.textContent='';notice.style.display='none';}
    return;
  }
  var preseason=games.some(g=>Number(g.season_type)===1);
  var verified=games.every(g=>[1,2,3].includes(Number(g.season_type)));
  if(notice){
    notice.style.display=(preseason||!verified)?'block':'none';
    notice.textContent=preseason
      ?'PRESEASON — Experimental picks. Not included in Main, Overflow or Coach records. Minutes and rotations may change.'
      :!verified?'SEASON TYPE UNVERIFIED — View-only picks; not included in Main, Overflow or Coach records.':'';
  }
  gb.style.display='flex';
  gb.innerHTML=games.map(g=>
    `<div class="game-chip"><b>${g.away}</b><span class="sep">@</span><b>${g.home}</b>${Number(g.season_type)===1?'<small style="color:#fbbf24;font-weight:800;margin-left:6px">PRESEASON</small>':![2,3].includes(Number(g.season_type))?'<small style="color:#fbbf24;margin-left:6px">UNVERIFIED</small>':''}</div>`
  ).join('');
}


async function runPicks(force=false){
  if(force && !window.IS_ADMIN) return;
  const selectedDate=document.getElementById('datePicker').value;
  const isHistorical=selectedDate<'__TODAY__';
  document.getElementById('content').innerHTML=`
    <div class="msg-card">
      <div class="loading-ball"></div>
      <div class="ball-shadow"></div>
      <h2 style="color:#FDB827">${isHistorical?'Loading Historical Sportsbook Replay':'Analyzing NBA Picks'}</h2>
      <p>${isHistorical?'Loading the final pregame Odds API snapshot and final box scores':'Pulling current matchup data'} for <strong style="color:#FDB827">${selectedDate}</strong>.<br>
      <span style="color:#1e3a5f">${isHistorical?'Purchased historical responses are permanently cached.':'This can take up to 45 seconds.'}</span></p>
    </div>`;
  document.getElementById('allPicksWrap').style.display='none';
  try{
    const _nbaTok=localStorage.getItem('__mpa_token')||'';
    const _adm=localStorage.getItem('__mpa_admin')||'';
    // ESPN can be slow; its downloads keep going on the server, so a stopped
    // run is retried automatically (saved sportsbook lines are reused).
    let data=null;
    const _maxTries=6;
    for(let _try=1;_try<=_maxTries;_try++){
      const r=await fetch('/run?_tok='+encodeURIComponent(_nbaTok)+'&admin='+encodeURIComponent(_adm),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:selectedDate,force:!!force})});
      if(!r.ok)throw new Error('Server error '+r.status);
      data=_nbaNormalizeHistoricalData(await r.json());
      if(!data.run_incomplete||_try===_maxTries)break;
      document.getElementById('content').innerHTML=`
        <div class="msg-card">
          <div class="loading-ball"></div>
          <div class="ball-shadow"></div>
          <h2 style="color:#FDB827">Still Loading ESPN Data</h2>
          <p>ESPN is responding slowly. Continuing automatically (attempt ${_try+1} of ${_maxTries}) for <strong style="color:#FDB827">${selectedDate}</strong>.</p>
        </div>`;
      await new Promise(function(res){setTimeout(res,3000);});
    }
    window.__NBA_BOARD_DATE__=data.date||selectedDate;
    window.__NBA_HISTORICAL_REPLAY__=!!data.historical_replay;
    window.__NBA_HISTORICAL_SNAPSHOT__=data.historical_snapshot||'';
    renderGames(data.games);
    if(data.no_games){
      document.getElementById('filterBar').style.display='none';
      document.getElementById('allPicksWrap').style.display='none';
      document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2>No Games Today</h2><p>No NBA games scheduled for ${data.date||selectedDate}. Check back on game day.</p></div>`;
      return;
    }
    top10=data.picks||[];
    allPicksData=data.all_picks||[];
    nbaPropsData=(data.props_picks||[]).concat(data.props_nopick||[]);
    nbaHotColdData=Array.isArray(data.hot_cold)?data.hot_cold:null;
    nbaApprovedPicks=(data.historical_replay?data.props_picks:data.picks)||[];
    activeTopStat='ALL';activeTopSide='ALL';activeAllStat='ALL';
    var _topSideChooser=document.getElementById('topSideChooser');if(_topSideChooser)_topSideChooser.style.display='none';
    document.querySelectorAll('#filterBar .filter-btn[data-top-side]').forEach(b=>b.classList.toggle('active',b.dataset.topSide==='ALL'));
    const log=data.log||[];
    if(data.run_incomplete){
      document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2>Run Didn&#39;t Finish</h2><p>${log[log.length-1]||'Player history did not finish loading.'} ESPN did not finish after several automatic retries. Press Run again in a minute.</p></div>`;
      return;
    }
    if(!top10.length && !allPicksData.length){
      document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2>${data.historical_replay?'Historical Replay Unavailable':'No Qualifying Picks'}</h2><p>${data.historical_replay?(log[log.length-1]||'No complete archived player-prop slate was available.'):'No 70%+ picks for today matchups; sportsbook lines may not be posted yet.'}</p></div><div class="log-box">${log.join('<br>')}</div>`;
      renderPropsSection(data.props_picks, data.props_nopick);
      return;
    }
    if(top10.length){
      document.getElementById('filterBar').style.display='flex';
      renderTop10Cards(top10);
    } else {
      // Card minutes floor filtered everyone off the cards, but there are still 70%+ plays
      // for the all-by-game list / parlay builder below — surface those, not a dead page.
      document.getElementById('filterBar').style.display='none';
      document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2>No Cards Today</h2><p>No starters or 6th-man players cleared the minutes floor for cards. All 70%+ plays and the parlay builder are below.</p></div>`;
    }
    const lb=document.createElement('div');
    lb.className='log-box';
    lb.innerHTML=log.join('<br>')+`<br> ${data.total} total picks found`;
    // Log box hidden from end users — internal diagnostics only.
    // document.getElementById('content').appendChild(lb);
    document.getElementById('totalCount').textContent=allPicksData.length;
    document.getElementById('allPicksWrap').style.display='block';
    renderSignalLists(allPicksData);
    renderAllByGame(allPicksData);
    renderPropsSection(data.props_picks, data.props_nopick);
  }catch(e){
    document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2 style="color:#ef4444">Something went wrong</h2><p>${e.message}</p></div>`;
  }
}

// Get Picks: load saved picks for the chosen date (read-only, never runs the pipeline).
async function getPicks(){
  const selectedDate=document.getElementById('datePicker').value;
  const btn=document.getElementById('getBtn');
  const orig=btn.textContent;
  btn.disabled=true; btn.textContent='Loading...';
  document.getElementById('content').innerHTML=`<div class="msg-card"><h2 style="color:#FDB827">Loading saved picks...</h2></div>`;
  document.getElementById('allPicksWrap').style.display='none';
  try{
    const _nbaTok=localStorage.getItem('__mpa_token')||'';
    const r=await fetch('/api/cached?target_date='+encodeURIComponent(selectedDate)+'&_tok='+encodeURIComponent(_nbaTok));
    if(r.status===404){ document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2>Picks Not Ready</h2><p>Today's picks aren't ready yet - check back a little later.</p></div>`; return; }
    if(!r.ok)throw new Error('Server error '+r.status);
    const data=_nbaNormalizeHistoricalData(await r.json());
    window.__NBA_BOARD_DATE__=data.date||selectedDate;
    window.__NBA_HISTORICAL_REPLAY__=!!data.historical_replay;
    window.__NBA_HISTORICAL_SNAPSHOT__=data.historical_snapshot||'';
    renderGames(data.games);
    if(data.no_games){
      document.getElementById('filterBar').style.display='none';
      document.getElementById('allPicksWrap').style.display='none';
      document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2>No Games Today</h2><p>No NBA games scheduled for ${data.date||selectedDate}. Check back on game day.</p></div>`;
      return;
    }
    top10=data.picks||[];
    allPicksData=data.all_picks||[];
    nbaPropsData=(data.props_picks||[]).concat(data.props_nopick||[]);
    nbaHotColdData=Array.isArray(data.hot_cold)?data.hot_cold:null;
    nbaApprovedPicks=(data.historical_replay?data.props_picks:data.picks)||[];
    activeTopStat='ALL';activeTopSide='ALL';activeAllStat='ALL';
    var _topSideChooser=document.getElementById('topSideChooser');if(_topSideChooser)_topSideChooser.style.display='none';
    document.querySelectorAll('#filterBar .filter-btn[data-top-side]').forEach(b=>b.classList.toggle('active',b.dataset.topSide==='ALL'));
    if(top10.length){
      document.getElementById('filterBar').style.display='flex';
      renderTop10Cards(top10);
    } else {
      document.getElementById('filterBar').style.display='none';
      document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2>No Cards Today</h2><p>All 70%+ plays and the parlay builder are below.</p></div>`;
    }
    if(top10.length||allPicksData.length){
      document.getElementById('totalCount').textContent=allPicksData.length;
      document.getElementById('allPicksWrap').style.display='block';
      renderSignalLists(allPicksData);
      renderAllByGame(allPicksData);
    }
    renderPropsSection(data.props_picks, data.props_nopick);
  }catch(e){
    document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2 style="color:#ef4444">Something went wrong</h2><p>${e.message}</p></div>`;
  }finally{
    btn.disabled=false; btn.textContent=orig;
  }
}

// ─── Hot / Cold board (NHL layout): separate spotlight boards + record ───
var _nbaHc={form:'hot',stat:'ALL',edge:'ALL',book:'main'};
var _NBA_HC_STATS=[['PTS','Points'],['REB','Rebounds'],['AST','Assists'],['FG3M','3-Pointers'],['PRA','Pts+Reb+Ast'],['PTS_REB','Pts+Reb'],['PTS_AST','Pts+Ast'],['REB_AST','Reb+Ast'],['BLK','Blocks'],['STL','Steals']];
var _NBA_HC_FORMS=[['hot','&#128293; Hot'],['cold','&#129482; Cold'],['double_cold','&#10052;&#65039; Double Cold']];
function _nbaHcEsc(v){return String(v==null?'':v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function _nbaHcSet(key,val){_nbaHc[key]=val;renderNbaHotColdSection();}
function _nbaHcToggle(el){var d=el&&el.parentElement&&el.parentElement.querySelector('.nba-hc-drop');if(!d)return;var open=d.style.display!=='none';d.style.display=open?'none':'block';el.setAttribute('aria-expanded',open?'false':'true');}
function _nbaHcBtns(key,opts,counts){
  return opts.map(function(o){
    var n=counts&&counts[o[0]]!=null?' <span class="nba-hc-n">'+counts[o[0]]+'</span>':'';
    return '<button type="button" class="nba-hc-btn'+(_nbaHc[key]===o[0]?' active':'')+'" onclick="_nbaHcSet(&#39;'+key+'&#39;,&#39;'+o[0]+'&#39;)">'+o[1]+n+'</button>';
  }).join('');
}
function _nbaHcCard(r){
  var over=r.pick==='OVER',sideCol=over?'#4ade80':'#f87171',formCol=r.form==='HOT'?'#f97316':'#38bdf8';
  var tip='';try{if(r.tipoff)tip=new Date(r.tipoff).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'});}catch(e){}
  var head=r.player_id?'<img class="nba-hc-head" loading="lazy" alt="" src="https://a.espncdn.com/i/headshots/nba/players/full/'+encodeURIComponent(r.player_id)+'.png" onerror="this.style.visibility=&#39;hidden&#39;">':'<div class="nba-hc-head"></div>';
  var chips=(r.recent_games||[]).map(function(g){var v=Number(g.v),hit=over?v>r.line:v<r.line,push=v===Number(r.line);return '<span class="nba-hc-chip '+(push?'push':(hit?'hit':'miss'))+'" title="'+_nbaHcEsc(g.d+' '+(g.loc==='away'?'@ ':'vs ')+g.opp)+'">'+_nbaHcEsc(g.v)+'</span>';}).join('');
  var odds=r.odds!=null?'<b>'+(r.odds>0?'+':'')+_nbaHcEsc(r.odds)+'</b> <span class="nba-hc-muted">'+_nbaHcEsc(r.book||'Book unavailable')+'</span>':'<span class="nba-hc-muted">No genuine price · tracked W/L only</span>';
  var edge=r.edge==null?'<span class="nba-hc-muted">Edge —</span>':'<span style="color:'+(r.edge>0?'#4ade80':'#f87171')+';font-weight:800">Edge '+(r.edge>0?'+':'')+Number(r.edge).toFixed(1)+' pts</span>';
  var prob=r.app_probability==null?'':' <span class="nba-hc-muted">· App '+Number(r.app_probability).toFixed(1)+'% vs Book '+(r.implied_probability==null?'—':Number(r.implied_probability).toFixed(1)+'%')+'</span>';
  var vs=r.opp_total?('<b>'+r.opp_hits+'/'+r.opp_total+'</b> '+(over?'Over':'Under')+' vs '+_nbaHcEsc(r.opp)+' ('+r.opp_rate+'%)'+(r.matchup_supports?' <span style="color:#a5b4fc;font-weight:800">· matchup supports</span>':'')):'<span class="nba-hc-muted">No recent meetings vs '+_nbaHcEsc(r.opp)+'</span>';
  var logRows=(r.recent_games||[]).map(function(g){var v=Number(g.v),push=v===Number(r.line),hit=over?v>r.line:v<r.line;return '<tr><td>'+_nbaHcEsc(g.d)+'</td><td>'+(g.loc==='away'?'@ ':'vs ')+_nbaHcEsc(g.opp)+'</td><td><b>'+_nbaHcEsc(g.v)+'</b></td><td style="color:'+(push?'#facc15':(hit?'#4ade80':'#f87171'))+';font-weight:800">'+(push?'PUSH':(hit?'HIT':'MISS'))+'</td></tr>';}).join('');
  var vsRows=(r.opp_games||[]).map(function(g){var v=Number(g.v),push=v===Number(r.line),hit=over?v>r.line:v<r.line;return '<tr><td>'+_nbaHcEsc(g.d)+'</td><td>'+(g.loc==='away'?'Away':'Home')+'</td><td><b>'+_nbaHcEsc(g.v)+'</b></td><td style="color:'+(push?'#facc15':(hit?'#4ade80':'#f87171'))+';font-weight:800">'+(push?'PUSH':(hit?'HIT':'MISS'))+'</td></tr>';}).join('');
  var drop='<div class="nba-hc-drop" style="display:none">'+
    '<div class="nba-hc-dh">Last '+r.recent_total+' games · current season · all venues</div>'+
    '<table class="nba-hc-log"><thead><tr><th>Date</th><th>Opp</th><th>'+_nbaHcEsc(r.stat_label)+'</th><th>vs '+_nbaHcEsc(r.line)+'</th></tr></thead><tbody>'+logRows+'</tbody></table>'+
    '<div class="nba-hc-dh">Last '+(r.opp_total||0)+' meetings vs '+_nbaHcEsc(r.opp)+' · any venue / season</div>'+
    (vsRows?'<table class="nba-hc-log"><thead><tr><th>Date</th><th>Venue</th><th>'+_nbaHcEsc(r.stat_label)+'</th><th>vs '+_nbaHcEsc(r.line)+'</th></tr></thead><tbody>'+vsRows+'</tbody></table>':'<div class="nba-hc-muted" style="font-size:.7rem">No meetings on file.</div>')+
  '</div>';
  return '<div class="nba-hc-card" style="--fc:'+formCol+'">'+
    '<button type="button" class="nba-hc-top" aria-expanded="false" onclick="_nbaHcToggle(this)">'+
      '<span class="nba-hc-rank">#'+r.rank+'</span>'+head+
      '<span class="nba-hc-who"><b>'+_nbaHcEsc(r.player)+'</b><small>'+_nbaHcEsc(r.team)+' '+(r.venue==='AWAY'?'@':'vs')+' '+_nbaHcEsc(r.opp)+(tip?' · '+_nbaHcEsc(tip):'')+'</small></span>'+
      '<span class="nba-hc-badge">'+(r.form_key==='double_cold'?'&#10052;&#65039; DOUBLE COLD':(r.form==='HOT'?'&#128293; HOT':'&#129482; COLD'))+'</span>'+
    '</button>'+
    '<div class="nba-hc-play"><span style="color:'+sideCol+';font-weight:900">'+r.pick+' '+_nbaHcEsc(r.line)+'</span> '+_nbaHcEsc(r.emoji||'')+' '+_nbaHcEsc(r.stat_label)+'</div>'+
    '<div class="nba-hc-line">'+odds+'</div>'+
    '<div class="nba-hc-line"><b>'+r.recent_hits+'/'+r.recent_total+'</b> last '+r.recent_total+' · avg <b>'+_nbaHcEsc(r.recent_avg)+'</b> <span class="nba-hc-chips">'+chips+'</span></div>'+
    '<div class="nba-hc-line">'+vs+'</div>'+
    '<div class="nba-hc-line">'+edge+prob+'</div>'+
    '<div class="nba-hc-more" onclick="_nbaHcToggle(this.parentElement.querySelector(&#39;.nba-hc-top&#39;))">&#9662; Game log &amp; matchup history</div>'+
    drop+
  '</div>';
}
function renderNbaHotColdSection(){
  var sec=document.getElementById('nba-hotcold-section');if(!sec)return;
  if(window.__NBA_HISTORICAL_REPLAY__){sec.style.display='none';return;}
  var data=nbaHotColdData,body=document.getElementById('nbaHcBoard'),ctl=document.getElementById('nbaHcControls');
  var loaded=(allPicksData&&allPicksData.length)||(nbaPropsData&&nbaPropsData.length);
  if(!Array.isArray(data)&&!loaded){sec.style.display='none';return;}
  sec.style.display='block';
  if(!Array.isArray(data)){ctl.innerHTML='';body.innerHTML='<div class="nba-hc-empty">This saved run was built before the Hot / Cold section existed. The next live run builds it automatically.</div>';return;}
  var formCounts={};_NBA_HC_FORMS.forEach(function(f){formCounts[f[0]]=data.filter(function(r){return r.form_key===f[0]&&!r.is_overflow;}).length;});
  var statCounts={ALL:data.filter(function(r){return r.form_key===_nbaHc.form&&!r.is_overflow;}).length};
  _NBA_HC_STATS.forEach(function(s){statCounts[s[0]]=data.filter(function(r){return r.form_key===_nbaHc.form&&r.stat===s[0]&&!r.is_overflow;}).length;});
  ctl.innerHTML='<div class="nba-hc-row"><span class="nba-hc-lbl">Form</span>'+_nbaHcBtns('form',_NBA_HC_FORMS,formCounts)+'</div>'+
    '<div class="nba-hc-row"><span class="nba-hc-lbl">Market</span>'+_nbaHcBtns('stat',[['ALL','All markets']].concat(_NBA_HC_STATS),statCounts)+'</div>'+
    '<div class="nba-hc-row"><span class="nba-hc-lbl">Edge</span>'+_nbaHcBtns('edge',[['ALL','All plays'],['POS','Positive edge only']])+
    '<span class="nba-hc-lbl" style="margin-left:12px">Board</span>'+_nbaHcBtns('book',[['main','Top 10'],['overflow','Overflow 11-20']])+'</div>';
  var rows=data.filter(function(r){
    if(r.form_key!==_nbaHc.form)return false;
    if(_nbaHc.stat!=='ALL'&&r.stat!==_nbaHc.stat)return false;
    if(_nbaHc.edge==='POS'&&!(r.edge!=null&&r.edge>0))return false;
    return _nbaHc.book==='overflow'?!!r.is_overflow:!r.is_overflow;
  });
  if(!rows.length){body.innerHTML='<div class="nba-hc-empty">No player qualifies for this form / market / filter on the loaded slate.</div>';return;}
  var html='';
  _NBA_HC_STATS.forEach(function(s){
    var list=rows.filter(function(r){return r.stat===s[0];}).sort(function(a,b){return a.rank-b.rank;});
    if(!list.length)return;
    html+='<div class="nba-hc-group"><div class="nba-hc-gh">'+_nbaHcEsc(list[0].category)+' <span class="nba-hc-muted">· '+(_nbaHc.book==='overflow'?'ranks 11-20':'Top 10')+' · '+list.length+' play'+(list.length===1?'':'s')+'</span></div><div class="nba-hc-grid">'+list.map(_nbaHcCard).join('')+'</div></div>';
  });
  body.innerHTML=html;
}
function renderPropsSection(picks, nopick) {
  try{renderNbaHotColdSection();}catch(e){console.error('Hot/Cold render failed',e);}
  var sec = document.getElementById('props-section');
  if (!sec) return;
  var all = (picks||[]).concat(nopick||[]);
  sec.style.display = all.length ? 'block' : 'none';
  if (!all.length) return;
   var histHead=window.__NBA_HISTORICAL_REPLAY__
     ? '<div style="color:#a5b4fc;font-weight:900;font-size:.8rem;margin-bottom:8px">HISTORICAL SPORTSBOOK REPLAY · archived snapshot '+(window.__NBA_HISTORICAL_SNAPSHOT__||'timestamp unavailable')+' · exact archived lines/prices; actual/result shown when final</div>'
     : '';
   sec.querySelector('.props-historical-label') && (sec.querySelector('.props-historical-label').innerHTML=histHead);
  var sigMap = {};
  (allPicksData||[]).forEach(function(s){ sigMap[s.player+'|'+s.stat]=s; });
  var byGame = {};
  all.forEach(function(p){
    var m=p.matchup||''; if(!m) return;
    if(!byGame[m]) byGame[m]={};
    if(!byGame[m][p.player]) byGame[m][p.player]=[];
    byGame[m][p.player].push(p);
  });
  window.__PROPS_BY_GAME__=byGame; window.__PROPS_SIG__=sigMap;
  var sel=document.getElementById('propsGameSel'); if(!sel) return;
  var matchups=Object.keys(byGame);
  sel.innerHTML='<option value="">-- Select a game --</option>'+
    matchups.map(function(m){ return '<option value="'+m+'">'+m+'</option>'; }).join('');
  document.getElementById('propsPlayerList').style.display='none';
  if(matchups.length===1){ sel.value=matchups[0]; propsSelectGame(matchups[0]); }
}
function propsSelectGame(matchup) {
  var plist=document.getElementById('propsPlayerList'); if(!plist) return;
  var hint=document.getElementById('propsGameHint');
  if(!matchup){ plist.style.display='none'; if(hint) hint.textContent='Select a game to browse players'; return; }
  var byGame=window.__PROPS_BY_GAME__||{}; if(!byGame[matchup]){ plist.style.display='none'; return; }
  window.__PROPS_PLAYERS__={};
  var players=Object.keys(byGame[matchup]);
  players.sort(function(a,b){ return a.split(' ').pop().localeCompare(b.split(' ').pop()); });
   if(hint) hint.textContent=window.__NBA_HISTORICAL_REPLAY__
     ? players.length+' players · archived sportsbook lines/prices (no pregame player-stat model edge)'
    : players.length+' players with prop lines';
  var chips=players.map(function(name,idx){
    window.__PROPS_PLAYERS__[idx]=name;
    var ent=byGame[matchup][name]; var first=ent[0]||{};
    var side=first.side||''; var opp=first.opp_name||''; var cnt=ent.length;
    var photo=first.headshot||(first.player_id?'https://a.espncdn.com/i/headshots/nba/players/full/'+first.player_id+'.png':'');
    var sb=side==='HOME'?'rgba(253,184,39,.15)':'rgba(99,102,241,.15)';
    var sc=side==='HOME'?'#fbbf24':'#818cf8';
    return '<div class="props-player-chip" onclick="openPropsPlayer('+idx+')" style="display:flex;align-items:center;gap:8px">' +
      (photo?'<img src="'+photo+'" alt="'+name+'" style="width:36px;height:36px;border-radius:50%;object-fit:cover;object-position:top;background:#172554" onerror="this.style.display=\\'none\\'"/>':'') +
      '<div><div style="font-weight:800;color:#fff;font-size:.88rem;font-family:Playfair Display,serif">'+name+
      (first.form_badge?'<span title="Display-only last-5 form versus season average" style="margin-left:6px;color:'+(first.form_badge==='HOT'?'#fb923c':'#60a5fa')+';font-size:.62rem;font-weight:900">'+first.form_badge+'</span>':'')+'</div>' +
      '<div style="font-size:.7rem;color:#888;margin-top:3px">' +
        '<span style="background:'+sb+';color:'+sc+';padding:1px 5px;border-radius:3px;font-weight:700;margin-right:4px">'+side+'</span>' +
        'vs '+opp+' &middot; '+cnt+' props</div></div>' +
    '</div>';
  }).join('');
  plist.innerHTML='<div style="display:flex;flex-wrap:wrap;gap:8px;padding:4px 0 12px">'+chips+'</div>';
  plist.style.display='block';
}
function closePropsModal(ev){
  if(ev && ev.target && ev.target.id!=='propsPlayerModal') return;
  var o=document.getElementById('propsPlayerModal'); if(o) o.remove();
}
function openPropsPlayer(idx){
  var name=(window.__PROPS_PLAYERS__||{})[idx]; if(name==null) return;
  var matchup=(document.getElementById('propsGameSel')||{}).value||'';
  var byGame=window.__PROPS_BY_GAME__||{};
  var entries=(byGame[matchup]||{})[name]; if(!entries||!entries.length) return;
  var sigMap=window.__PROPS_SIG__||{};
  var esc=function(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); };
  var first=entries[0]||{}; var side=first.side||''; var opp=first.opp_name||first.opp||'';
  var playerPhoto=first.headshot||(first.player_id?'https://a.espncdn.com/i/headshots/nba/players/full/'+first.player_id+'.png':'');
  var rows=entries.map(function(p){
    var sig=sigMap[name+'|'+p.stat]||{};
    var isO=p.pick==='OVER', isU=p.pick==='UNDER';
    var clr=isO?'#4ade80':isU?'#f87171':'#888';
    var dkO=p.dk_over_odds||(sig.dk_over_odds||'');
    var dkU=p.dk_under_odds||(sig.dk_under_odds||'');
    var ba=[];
    var form=p.form_badge||sig.form_badge||'';
    if(form) ba.push('<span title="Display-only last-5 form versus season average" style="background:'+(form==='HOT'?'rgba(249,115,22,.15)':'rgba(96,165,250,.15)')+';color:'+(form==='HOT'?'#fb923c':'#60a5fa')+';padding:1px 5px;border-radius:3px;font-size:.6rem;font-weight:800">'+form+'</span>');
    if(sig.has_consistency) ba.push('<span style="background:rgba(245,158,11,.15);color:#fbbf24;padding:1px 5px;border-radius:3px;font-size:.6rem;font-weight:700">PAT '+sig.pct+'%</span>');
    if(sig.line_rec) ba.push('<span style="background:rgba(74,222,128,.12);color:#4ade80;padding:1px 5px;border-radius:3px;font-size:.6rem;font-weight:700">LINE '+sig.line_rec+'</span>');
    if(sig.streak_rec) ba.push('<span style="background:rgba(249,115,22,.12);color:#fb923c;padding:1px 5px;border-radius:3px;font-size:.6rem;font-weight:700">STREAK '+sig.streak_n+'</span>');
    if(sig.alt_rec) ba.push('<span style="background:rgba(168,85,247,.12);color:#c084fc;padding:1px 5px;border-radius:3px;font-size:.6rem;font-weight:700">MPA '+sig.alt_rec+'</span>');
    var badges=ba.length?'<div style="margin-top:3px;display:flex;flex-wrap:wrap;gap:3px">'+ba.join('')+'</div>':'';
    var hist=(p.history||'').split(',').filter(Boolean).slice(0,8);
    var histHtml=hist.length?hist.map(function(v){
      var n=parseFloat(v); var ln=p.line;
      var hc=(!isNaN(n)&&ln!=null)?(n>ln?'#4ade80':n<ln?'#f87171':'#888'):'#666';
      return '<span style="color:'+hc+';font-weight:700">'+v+'</span>';
    }).join('<span style="color:#333;margin:0 1px">,</span>'):'<span style="color:#555">—</span>';
    var avgDisp=p.avg!=null?esc(String(p.avg))+'<span style="color:#777;font-size:.7rem"> ('+esc(String(p.games))+'g)</span>':'<span style="color:#555">no history</span>';
    var pickCell=(isO?'<span style="color:#4ade80;font-weight:900;font-size:.95rem">O</span>':isU?'<span style="color:#f87171;font-weight:900;font-size:.95rem">U</span>':'<span style="color:#555">—</span>')+badges;
    var _trackCell='';
    if(window.IS_ADMIN && !window.__NBA_HISTORICAL_REPLAY__){
      if(p.line!=null){
        var _bside=isU?'UNDER':'OVER';
        var _bo=(p.dk_over_odds!=null?p.dk_over_odds:(sig.dk_over_odds!=null?sig.dk_over_odds:null));
        var _bu=(p.dk_under_odds!=null?p.dk_under_odds:(sig.dk_under_odds!=null?sig.dk_under_odds:null));
        var _bodds=_bside==='OVER'?(_bo!=null?_bo:_bu):(_bu!=null?_bu:_bo);
        var _bk='np'+(++_nbaBetN);
        (window.__NBA_BET_SRC__=window.__NBA_BET_SRC__||{})[_bk]={name:name,team:(p.team||''),opp:opp,category:(p.stat_label||''),side:_bside,stat_key:(p.stat||''),stat_label:(p.stat_label||''),line:p.line,odds:(_bodds!=null?_bodds:null),date:(p.tipoff?String(p.tipoff).slice(0,10):'')};
        _trackCell='<td style="padding:10px 12px"><button data-betkey="'+_bk+'" class="admin-only" onclick="event.stopPropagation();_nbaBetForm(this.dataset.betkey)" style="background:#1a1740;color:#a5b4fc;border:1px solid #312e81;border-radius:7px;padding:5px 11px;font-size:.72rem;font-weight:800;cursor:pointer;white-space:nowrap">Track</button></td>';
      } else {
        _trackCell='<td style="padding:10px 12px;color:#555">—</td>';
      }
    }
    return '<tr style="border-bottom:1px solid #1a1a1a">' +
      '<td style="padding:10px 12px;white-space:nowrap">'+esc(p.emoji)+' <span style="color:#FDB827;font-weight:700">'+esc(p.stat_label)+'</span></td>' +
      '<td style="padding:10px 12px;font-family:monospace;font-weight:800;color:#fff">'+esc(String(p.line||'—'))+'</td>' +
      '<td style="padding:10px 12px;font-family:monospace;font-size:.82rem;color:#4ade80">'+(dkO||'—')+'</td>' +
      '<td style="padding:10px 12px;font-family:monospace;font-size:.82rem;color:#f87171">'+(dkU||'—')+'</td>' +
      '<td style="padding:10px 12px;font-family:monospace;font-size:.9rem;color:'+clr+';font-weight:700">'+avgDisp+'</td>' +
      '<td style="padding:10px 12px;font-size:.75rem;font-family:monospace;max-width:160px">'+histHtml+'</td>' +
      '<td style="padding:10px 12px">'+pickCell+'</td>' +
       (window.__NBA_HISTORICAL_REPLAY__?'<td style="padding:10px 12px;font-size:.75rem;color:#a5b4fc;font-weight:800">'+(p.actual!=null?('ACTUAL '+esc(p.actual)+' · '+esc(p.result||'PENDING')):'actual pending')+'</td>':'') +
      _trackCell +
    '</tr>';
  });
  closePropsModal();
  var ov=document.createElement('div');
  ov.id='propsPlayerModal';
  ov.setAttribute('onclick','closePropsModal(event)');
  ov.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.78);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px';
  ov.innerHTML=
    '<div style="background:#101010;border:1px solid #2a2a2a;border-radius:14px;max-width:820px;width:100%;max-height:86vh;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,.6)">' +
      '<div style="background:linear-gradient(135deg,#1e3a5f,#0a1a2e);padding:14px 18px;border-bottom:2px solid #FDB827;display:flex;justify-content:space-between;align-items:flex-start;position:sticky;top:0;z-index:1">' +
        '<div style="display:flex;align-items:center;gap:12px">' +
          (playerPhoto?'<img src="'+playerPhoto+'" alt="'+esc(name)+'" style="width:54px;height:54px;border-radius:50%;object-fit:cover;object-position:top;background:#172554;border:1px solid #FDB827" onerror="this.style.display=\\'none\\'"/>':'') +
          '<div>' +
          '<div style="color:#fff;font-weight:900;font-size:1.05rem;font-family:Playfair Display,serif">'+esc(name)+'</div>' +
          '<div style="color:#FDB827;font-size:.76rem;font-weight:700;margin-top:3px">'+esc(side)+' &middot; vs '+esc(opp)+' &middot; '+esc(matchup)+'</div>' +
          '</div>' +
        '</div>' +
        '<span onclick="closePropsModal()" style="color:#888;font-size:1.5rem;line-height:1;cursor:pointer;padding:0 4px">&times;</span>' +
      '</div>' +
      '<div style="overflow-x:auto">' +
        '<table style="width:100%;border-collapse:collapse;font-size:.82rem;background:#101010">' +
          '<thead><tr style="background:#1a1a1a;border-bottom:1px solid rgba(245,158,11,.2)">' +
            '<th style="padding:9px 12px;text-align:left;color:#f59e0b;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;white-space:nowrap">Stat</th>' +
            '<th style="padding:9px 12px;text-align:left;color:#f59e0b;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em">Line</th>' +
            '<th style="padding:9px 12px;text-align:left;color:#4ade80;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em">Over</th>' +
            '<th style="padding:9px 12px;text-align:left;color:#f87171;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em">Under</th>' +
            '<th style="padding:9px 12px;text-align:left;color:#f59e0b;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em">Avg vr Opp</th>' +
            '<th style="padding:9px 12px;text-align:left;color:#f59e0b;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;white-space:nowrap">Last 8 vr Opp</th>' +
            '<th style="padding:9px 12px;text-align:left;color:#f59e0b;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em">Pick &amp; Signal</th>' +
            (window.__NBA_HISTORICAL_REPLAY__?'<th style="padding:9px 12px;text-align:left;color:#a5b4fc;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em">Actual / Result</th>':'') +
            (window.IS_ADMIN?'<th style="padding:9px 12px;text-align:left;color:#a5b4fc;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em">Track</th>':'') +
          '</tr></thead>' +
          '<tbody>'+rows.join('')+'</tbody>' +
        '</table>' +
      '</div>' +
      '<p style="font-size:.68rem;color:#444;padding:10px 14px;margin:0">Last 8 = head-to-head games at this location, newest-first &middot; Green = over the line, Red = under</p>' +
    '</div>';
  document.body.appendChild(ov);
}

// Snapshot mode: hub serves this page with picks baked in as
// window.__INITIAL_PICKS__ — skip the /run fetch and render directly.
document.addEventListener('DOMContentLoaded', function(){
  if (!window.__INITIAL_PICKS__) return;
  try {
    var data = _nbaNormalizeHistoricalData(window.__INITIAL_PICKS__);
    window.__NBA_BOARD_DATE__=data.date||'';
    var dp = document.getElementById('datePicker');
    if (dp && data.date) dp.value = data.date;
    if (data.games) renderGames(data.games);
    top10        = data.picks || [];
    allPicksData = data.all_picks || [];
    nbaPropsData = (data.props_picks || []).concat(data.props_nopick || []);
    nbaHotColdData = Array.isArray(data.hot_cold) ? data.hot_cold : null;
    nbaApprovedPicks = (data.historical_replay ? data.props_picks : data.picks) || [];
    activeTopStat = 'ALL'; activeTopSide = 'ALL'; activeAllStat = 'ALL';
    var _topSideChooser=document.getElementById('topSideChooser');if(_topSideChooser)_topSideChooser.style.display='none';
    document.querySelectorAll('#filterBar .filter-btn[data-top-side]').forEach(b=>b.classList.toggle('active',b.dataset.topSide==='ALL'));
    if (top10.length) {
      var fb = document.getElementById('filterBar');
      if (fb) fb.style.display = 'flex';
      renderTop10Cards(top10);
    }
    if (allPicksData.length) {
      // Render the all-picks sections whenever there are plays, even if the card minutes
      // floor left zero cards.
      var tc = document.getElementById('totalCount');
      if (tc) tc.textContent = allPicksData.length;
      var ap = document.getElementById('allPicksWrap');
      if (ap) ap.style.display = 'block';
      renderSignalLists(allPicksData);
      renderAllByGame(allPicksData);
    }
    renderPropsSection(data.props_picks, data.props_nopick);
  } catch (e) { console.error('snapshot render failed', e); }
});
// ── My Bets ──────────────────────────────────────────────────────────────────
function _nbaEsc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function _nbaMoney(v){var n=Number(v)||0;return(n>=0?'$':'\u2212$')+Math.abs(n).toFixed(2);}
function _nbaBetAuthQS(){
  var tok=localStorage.getItem('__mpa_token')||'';
  var adm=localStorage.getItem('__mpa_admin')||new URLSearchParams(location.search).get('admin')||'';
  return '?token='+encodeURIComponent(tok)+(adm?('&admin='+encodeURIComponent(adm)):'');
}
function _nbaBetToast(msg){
  var t=document.createElement('div');t.textContent=msg;
  t.style.cssText='position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:#312e81;color:#fff;padding:10px 20px;border-radius:10px;font-weight:700;font-size:.85rem;z-index:99999;white-space:nowrap;pointer-events:none;box-shadow:0 4px 20px rgba(0,0,0,.5)';
  document.body.appendChild(t);
  setTimeout(function(){t.style.opacity='0';t.style.transition='opacity .4s';setTimeout(function(){t.remove();},400);},2200);
}
var _nbaBetN=0;
window.__NBA_BET_SRC__=window.__NBA_BET_SRC__||{};
function _nbaPickVerdict(s){
  if(!s) return null;
  if(s.has_consistency) return 'OVER';
  var votes={OVER:0,UNDER:0};
  if(s.line_rec) votes[s.line_rec]++;
  if(s.streak_rec) votes[s.streak_rec]++;
  if(s.alt_rec) votes[s.alt_rec]++;
  var tot=votes.OVER+votes.UNDER;
  if(tot&&votes.OVER!==votes.UNDER) return votes.OVER>votes.UNDER?'OVER':'UNDER';
  return null;
}
function _nbaBetBtn(p,verdict){
  if(p.dk_line==null) return '';
  var side=verdict==='OVER'||verdict==='UNDER'?verdict:'OVER';
  var odds=side==='OVER'?(p.dk_over_odds||p.dk_under_odds||null):(p.dk_under_odds||p.dk_over_odds||null);
  var tipDate=p.tipoff?p.tipoff.slice(0,10):'';
  var k='nb'+(++_nbaBetN);
  window.__NBA_BET_SRC__[k]={
    name:p.player,team:(p.team||''),opp:(p.opp||''),
    category:(p.stat_label||''),side:side,
    stat_key:(p.stat||''),stat_label:(p.stat_label||''),
    line:p.dk_line,odds:(odds!=null?odds:null),date:tipDate
  };
  return '<button data-betkey="'+k+'" class="admin-only" onclick="event.stopPropagation();_nbaBetForm(this.dataset.betkey)" style="width:100%;background:#1a1740;color:#a5b4fc;border:none;border-top:1px solid #26263a;padding:8px;font-size:.76rem;font-weight:800;cursor:pointer;border-radius:0 0 14px 14px;letter-spacing:.04em">Track Bet</button>';
}
function _nbaBetForm(key){
  var src=(window.__NBA_BET_SRC__||{})[key]; if(!src) return;
  window.__NBA_BET_CUR__=src;
  var ov=document.getElementById('nba-bet-modal');
  if(!ov){
    ov=document.createElement('div'); ov.id='nba-bet-modal';
    ov.style.cssText='position:fixed;inset:0;background:rgba(2,6,23,.82);z-index:10000;display:flex;align-items:center;justify-content:center;padding:16px';
    ov.onclick=function(e){if(e.target===ov)ov.style.display='none';};
    document.body.appendChild(ov);
  }
  var pickTxt=src.side+' '+src.line+' '+(src.stat_label||'');
  ov.innerHTML=`<div style="background:#0f172a;border:1px solid #312e81;border-radius:16px;max-width:360px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.6)">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;padding:16px 18px;border-bottom:1px solid #1e293b">
      <div>
        <div style="font-weight:800;color:#fff;font-size:1.02rem">${_nbaEsc(src.name)}</div>
        <div style="color:#a5b4fc;font-size:.82rem;font-weight:800;margin-top:2px">${_nbaEsc(pickTxt)}</div>
        <div style="color:#94a3b8;font-size:.72rem;margin-top:2px">${_nbaEsc(src.category||'')}${src.opp?' &middot; vs '+_nbaEsc(src.opp):''}${src.date?' &middot; '+src.date:''}</div>
      </div>
      <button onclick="document.getElementById('nba-bet-modal').style.display='none'" style="background:#1e293b;border:none;color:#cbd5e1;width:30px;height:30px;border-radius:8px;cursor:pointer;font-size:1rem">&#215;</button>
    </div>
    <div style="padding:16px 18px;display:grid;gap:12px">
      <label style="font-size:.72rem;color:#94a3b8;font-weight:600">Odds (American)<input id="nba-bet-odds" type="number" value="${src.odds!=null?src.odds:''}" style="display:block;width:100%;margin-top:5px;background:#0b1120;border:1px solid #334155;border-radius:8px;padding:9px 11px;color:#fbbf24;font-family:monospace;font-weight:700;font-size:.95rem"></label>
      <label style="font-size:.72rem;color:#94a3b8;font-weight:600">Bet size ($)<input id="nba-bet-stake" type="number" min="0" step="0.01" placeholder="e.g. 50" style="display:block;width:100%;margin-top:5px;background:#0b1120;border:1px solid #334155;border-radius:8px;padding:9px 11px;color:#fff;font-weight:700;font-size:.95rem"></label>
      <div id="nba-bet-payout" style="font-size:.78rem;color:#64748b;min-height:1em"></div>
      <div id="nba-bet-msg" style="font-size:.76rem;color:#f87171;min-height:1em"></div>
      <button id="nba-bet-save" onclick="_nbaSaveBet()" style="background:#4338ca;color:#fff;border:none;border-radius:9px;padding:11px;font-weight:800;cursor:pointer;font-size:.92rem">Log Bet</button>
    </div>
  </div>`;
  ov.style.display='flex';
  var so=document.getElementById('nba-bet-odds'),ss=document.getElementById('nba-bet-stake');
  function _calc(){
    var o=parseFloat(so.value),s=parseFloat(ss.value);
    var pay=document.getElementById('nba-bet-payout');
    if(!isFinite(o)||!isFinite(s)||s<=0){pay.textContent='';return;}
    var win=o>0?s*(o/100):s*(100/Math.abs(o));
    pay.innerHTML='To win <strong style="color:#4ade80">$'+win.toFixed(2)+'</strong> &middot; total payout <strong style="color:#cbd5e1">$'+(s+win).toFixed(2)+'</strong>';
  }
  so.oninput=_calc;ss.oninput=_calc;_calc();
  setTimeout(function(){ss.focus();},50);
}
async function _nbaSaveBet(){
  var src=window.__NBA_BET_CUR__;if(!src) return;
  var o=parseFloat(document.getElementById('nba-bet-odds').value);
  var s=parseFloat(document.getElementById('nba-bet-stake').value);
  var msg=document.getElementById('nba-bet-msg');
  if(!isFinite(o)){msg.textContent='Enter the odds.';return;}
  if(!isFinite(s)||s<=0){msg.textContent='Enter a bet size greater than 0.';return;}
  var btn=document.getElementById('nba-bet-save');btn.disabled=true;btn.textContent='Saving\u2026';
  try{
    var body=Object.assign({},src,{odds:Math.round(o),stake:s,placed_at:new Date().toISOString()});
    var res=await fetch('/api/bets'+_nbaBetAuthQS(),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!res.ok){throw new Error(await res.text());}
    document.getElementById('nba-bet-modal').style.display='none';
    _nbaBetToast('\u2705 Bet logged');
    var mb=document.getElementById('nba-mybets-card');
    if(mb&&mb.style.display!=='none') openNbaMyBets(false);
  }catch(e){msg.textContent=(e.message||'Save failed');btn.disabled=false;btn.textContent='Log Bet';}
}
async function openNbaMyBets(scroll){
  var card=document.getElementById('nba-mybets-card');if(!card) return;
  card.style.display='block';
  if(scroll!==false) card.scrollIntoView({behavior:'smooth',block:'start'});
  document.getElementById('nba-mybets-body').innerHTML='<p style="color:#94a3b8;font-size:.85rem">Loading\u2026</p>';
  try{
    var res=await fetch('/api/bets'+_nbaBetAuthQS());
    if(!res.ok){
      var t=await res.text();
      if(res.status===403) t='Session expired \u2014 reopen from hub';
      throw new Error(t);
    }
    window.__NBA_MYBETS__=await res.json();
    renderNbaMyBets(window.__NBA_MYBETS__);
  }catch(e){
    document.getElementById('nba-mybets-body').innerHTML='<p style="color:#f87171;padding:16px">'+(e.message||'Error loading bets')+'</p>';
  }
}
async function getNbaBetsResults(){await openNbaMyBets();}
function _nbaBetOddsDisp(o){return o!=null?((o>0?'+':'')+o):'\u2014';}
function _nbaResColor(r){return r==='WIN'?'#4ade80':(r==='LOSS'?'#f87171':(r==='PUSH'?'#facc15':'#94a3b8'));}
function _nbaStatBox(lbl,val,clr){
  return '<div style="background:#111;border-radius:10px;padding:10px 14px;min-width:92px">'
    +'<div style="font-size:.64rem;color:#64748b;text-transform:uppercase;letter-spacing:.08em">'+lbl+'</div>'
    +'<div style="font-size:1.12rem;font-weight:800;color:'+(clr||'#e2e8f0')+'">'+val+'</div></div>';
}
function renderNbaMyBets(d){
  var s=d.summary||{};var bets=d.bets||[];
  var roiTxt=s.roi!=null?((s.roi>0?'+':'')+s.roi+'%'):'\u2014';
  var roiClr=s.roi==null?'#94a3b8':(s.roi>0?'#4ade80':(s.roi<0?'#f87171':'#facc15'));
  var netClr=(s.profit||0)>0?'#4ade80':((s.profit||0)<0?'#f87171':'#cbd5e1');
  var recTxt=(s.wins||0)+'-'+(s.losses||0)+(s.push?('-'+s.push+'P'):'');
  var head='<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:18px">'
    +_nbaStatBox('Record',recTxt,'#e2e8f0')
    +_nbaStatBox('Pending',(s.pending||0),'#94a3b8')
    +_nbaStatBox('Staked',_nbaMoney(s.staked||0),'#cbd5e1')
    +_nbaStatBox('Net',_nbaMoney(s.profit||0),netClr)
    +_nbaStatBox('Returned',_nbaMoney(s.returned||0),'#cbd5e1')
    +_nbaStatBox('ROI',roiTxt,roiClr)
    +'<div style="margin-left:auto"><button onclick="downloadNbaMyBetsCSV()" style="background:#4338ca;color:#fff;border:none;border-radius:8px;padding:8px 12px;font-size:.78rem;font-weight:700;cursor:pointer">&#11015; CSV</button></div>'
    +'</div>';
  var bc=(s.by_category||[]).map(function(c){
    var croi=c.roi!=null?((c.roi>0?'+':'')+c.roi+'%'):'\u2014';
    var cclr=c.roi==null?'#94a3b8':(c.roi>0?'#4ade80':(c.roi<0?'#f87171':'#facc15'));
    return '<tr><td style="font-weight:600">'+_nbaEsc(c.category)+'</td>'
      +'<td style="font-family:monospace">'+c.wins+'-'+c.losses+(c.push?('-'+c.push+'P'):'')+'</td>'
      +'<td style="font-family:monospace;color:#94a3b8">'+(c.pending||0)+'</td>'
      +'<td style="font-family:monospace">'+_nbaMoney(c.staked)+'</td>'
      +'<td style="font-family:monospace;color:'+((c.profit||0)>=0?'#4ade80':'#f87171')+'">'+_nbaMoney(c.profit)+'</td>'
      +'<td style="font-family:monospace;font-weight:700;color:'+cclr+'">'+croi+'</td></tr>';
  }).join('');
  var bcHtml=bc?'<div style="overflow-x:auto;margin-bottom:18px"><table class="nba-bets-tbl"><thead><tr><th>Category</th><th>W-L</th><th>Pend</th><th>Staked</th><th>Net</th><th>ROI</th></tr></thead><tbody>'+bc+'</tbody></table></div>':'';
  var rows=bets.map(function(b){
    var res=b.result||'pending';
    var delBtn='<button data-delid="'+b.id+'" onclick="_nbaDeleteBet(this.dataset.delid)" title="Remove" style="background:none;border:none;color:#64748b;cursor:pointer;font-size:1rem">&#10006;</button>';
    var pk=b.side+' '+b.line+' '+(b.stat_label||'');
    var actTxt=b.actual!=null?(' <span style="color:#64748b;font-weight:400;font-size:.72rem">('+b.actual+')</span>'):'';
    return '<tr>'
      +'<td style="white-space:nowrap;color:#94a3b8;font-family:monospace;font-size:.76rem">'+(b.date||'')+'</td>'
      +'<td style="font-weight:600">'+_nbaEsc(b.name||'')+'<div style="font-size:.68rem;color:#64748b">'+_nbaEsc(b.category||'')+'</div></td>'
      +'<td style="font-size:.82rem">'+_nbaEsc(pk)+'</td>'
      +'<td style="font-family:monospace">'+_nbaBetOddsDisp(b.odds)+'</td>'
      +'<td style="font-family:monospace">'+_nbaMoney(b.stake)+'</td>'
      +'<td style="font-weight:800;color:'+_nbaResColor(res)+'">'+(res==='pending'?'pending':res)+actTxt+'</td>'
      +'<td style="font-family:monospace;font-weight:700;color:'+((b.profit||0)>=0?'#4ade80':'#f87171')+'">'+(b.profit!=null?_nbaMoney(b.profit):'\u2014')+'</td>'
      +'<td>'+delBtn+'</td></tr>';
  }).join('');
  var rowsHtml=bets.length
    ?'<div style="overflow-x:auto"><table class="nba-bets-tbl"><thead><tr><th>Date</th><th>Player</th><th>Pick</th><th>Odds</th><th>Stake</th><th>Result</th><th>Profit</th><th></th></tr></thead><tbody>'+rows+'</tbody></table></div>'
    :'<p style="color:#94a3b8;padding:16px">No bets logged yet. Click <strong style="color:#c7d2fe">Track Bet</strong> on any pick card to start.</p>';
  document.getElementById('nba-mybets-body').innerHTML=head+bcHtml+rowsHtml;
}
async function _nbaDeleteBet(id){
  if(!confirm('Remove this bet from your log?')) return;
  try{
    var res=await fetch('/api/bets/'+encodeURIComponent(id)+_nbaBetAuthQS(),{method:'DELETE'});
    if(!res.ok) throw new Error(await res.text());
    openNbaMyBets(false);
  }catch(e){alert(e.message||'Delete failed');}
}
function downloadNbaMyBetsCSV(){
  var d=window.__NBA_MYBETS__;if(!d){alert('Open My Bets first.');return;}
  var rows=[['Date','Player','Team','Category','Side','Pick','Odds','Stake','Result','Actual','Profit']];
  (d.bets||[]).forEach(function(b){
    rows.push([b.date||'',b.name||'',b.team||'',b.category||'',b.side||'',
      b.side+' '+b.line+' '+(b.stat_label||''),
      b.odds!=null?b.odds:'',b.stake!=null?b.stake:'',
      b.result||'',b.actual!=null?b.actual:'',b.profit!=null?b.profit:'']);
  });
  function _c(v){var sv=String(v==null?'':v);if(/[,"\\n]/.test(sv))sv='"'+sv.replace(/"/g,'""')+'"';return sv;}
  var csv=rows.map(function(r){return r.map(_c).join(',');}).join('\\r\\n');
  var blob=new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8;'});
  var url=URL.createObjectURL(blob);
  var a=document.createElement('a');a.href=url;a.download='nba-my-bets.csv';
  document.body.appendChild(a);a.click();document.body.removeChild(a);URL.revokeObjectURL(url);
}
// ── NBA Track Record ──────────────────────────────────────────────────────────
var _nbaTrkData=null;
var _nbaTrkView='category';
var _nbaOvfData=null;
var _nbaOvfView='category';
var _nbaHcData=null;
var _nbaHcView='category';
function _nbaRecPrefix(record){return record==='overflow'?'nbaOvf':(record==='hotcold'?'nbaHc':'nbaTrk');}
function openNbaHotColdRecord(){
  var section=document.getElementById('nba-hotcold-track-section');
  section.style.display='block';
  section.scrollIntoView({behavior:'smooth',block:'start'});
  if(_nbaHcData===null)loadNbaTrackRecord('hotcold');
}
function openNbaHotColdBoard(){
  var section=document.getElementById('nba-hotcold-section');
  if(section&&section.style.display!=='none'){section.scrollIntoView({behavior:'smooth',block:'start'});return;}
  alert('Load or run picks first - the Hot / Cold board is built with each live run.');
}
function openNbaOverflowRecord(){
  var section=document.getElementById('nba-overflow-track-section');
  section.style.display='block';
  section.scrollIntoView({behavior:'smooth',block:'start'});
  if(_nbaOvfData===null)loadNbaTrackRecord('overflow');
}
function setNbaTrkView(view,record){
  var prefix=_nbaRecPrefix(record);
  view=view==='full'?'full':'category';
  if(record==='overflow')_nbaOvfView=view;else if(record==='hotcold')_nbaHcView=view;else _nbaTrkView=view;
  var c=document.getElementById(prefix+'CatBtn'),f=document.getElementById(prefix+'FullBtn');
  if(c)c.classList.toggle('active',view==='category');
  if(f)f.classList.toggle('active',view==='full');
  renderNbaTrackDay(record);
}
function _nbaRecordEmpty(title,copy){
  return '<div class="nba-record-empty"><div class="nba-record-empty-icon">&#9635;</div><div class="nba-record-empty-title">'+nbaTrkEsc(title)+'</div><div class="nba-record-empty-copy">'+nbaTrkEsc(copy)+'</div></div>';
}
function _nbaRecordSummary(w,l,pending,net,staked,stake){
  var n=w+l,rate=n?w/n*100:0,roi=staked?net/staked*100:0;
  var netColor=net>0?'#4ade80':net<0?'#f87171':'#e2e8f0';
  var rateColor=rate>=60?'#4ade80':rate>=50?'#facc15':'#f87171';
  return '<div class="nba-summary-grid">'+
    '<div class="nba-summary-card"><div class="nba-summary-label">Record</div><div class="nba-summary-value">'+w+'-'+l+'</div><div class="nba-summary-note">'+(pending?pending+' pending':'All graded')+'</div></div>'+
    '<div class="nba-summary-card"><div class="nba-summary-label">Win rate</div><div class="nba-summary-value" style="color:'+rateColor+'">'+(n?rate.toFixed(1)+'%':'—')+'</div><div class="nba-summary-note">'+n+' decided plays</div></div>'+
    '<div class="nba-summary-card"><div class="nba-summary-label">Net profit</div><div class="nba-summary-value" style="color:'+netColor+'">'+(net>=0?'+':'−')+'$'+Math.abs(net).toFixed(2)+'</div><div class="nba-summary-note">$'+stake.toFixed(2)+' flat per priced play</div></div>'+
    '<div class="nba-summary-card"><div class="nba-summary-label">ROI</div><div class="nba-summary-value" style="color:'+netColor+'">'+(staked?(roi>=0?'+':'−')+Math.abs(roi).toFixed(1)+'%':'—')+'</div><div class="nba-summary-note">$'+staked.toFixed(2)+' total risked</div></div>'+
  '</div>';
}
function _nbaRecordFullList(rows,stake,historical){
  var body=(rows||[]).map(function(b,i){
    var res=String(b.result||'PENDING').toUpperCase(),rc=res==='WIN'?'#4ade80':res==='LOSS'?'#f87171':'#fbbf24';
    var priced=_nbaTrkPriced(b.odds);
    var pl=priced&&(res==='WIN'||res==='LOSS'||res==='PUSH')?nbaProfit(b.odds,stake,res):null,pc=(pl||0)>=0?'#4ade80':'#f87171';
    var name=b.name||b.player,cat=b.category||b.stat_label||b.stat||b.stat_key;
    return '<tr><td>'+(b.rank!=null?'#'+nbaTrkEsc(b.rank):(i+1))+(b._pdate?'<br><small style="color:#64748b">'+nbaTrkEsc(b._pdate)+'</small>':'')+'</td><td><b>'+nbaTrkEsc(name)+'</b><br><small style="color:#64748b">'+nbaTrkEsc(b.team||'')+(b.opp?' vs '+nbaTrkEsc(b.opp):'')+'</small></td>'+
      '<td>'+nbaTrkEsc(cat)+'</td><td><b style="color:'+(String(b.side).toUpperCase()==='OVER'?'#4ade80':'#fbbf24')+'">'+nbaTrkEsc(b.side)+' '+nbaTrkEsc(b.line)+'</b></td>'+
      '<td>'+nbaTrkEsc(b.odds)+'<br><small style="color:#64748b">'+nbaTrkEsc(b.book||'')+'</small></td><td>'+(priced?'$'+stake.toFixed(2):'—')+'</td><td>'+nbaTrkEsc(b.actual)+'</td>'+
      '<td style="color:'+rc+';font-weight:900">'+res+'</td><td style="color:'+pc+';font-weight:900">'+(pl==null?'—':(pl>=0?'+':'−')+'$'+Math.abs(pl).toFixed(2))+'</td>'+
      (historical?'<td>'+nbaTrkEsc(b.hits)+'/'+nbaTrkEsc(b.games)+'<br><small style="color:#64748b">'+nbaTrkEsc(b.pct)+'% · '+nbaTrkEsc(b.mpg)+' MPG</small></td>':'')+'</tr>';
  }).join('');
  return '<div class="nba-full-wrap"><table class="nba-full-table"><thead><tr><th>#</th><th>Player</th><th>Market</th><th>Pick</th><th>Odds / Book</th><th>Stake</th><th>Actual</th><th>Result</th><th>P/L</th>'+(historical?'<th>Evidence</th>':'')+'</tr></thead><tbody>'+body+'</tbody></table></div>';
}
function _nbaTrkDayName(record){
  var prefix=_nbaRecPrefix(record);
  var dp=document.getElementById(prefix+'DateSelect'),dn=document.getElementById(prefix+'DayName');
  if(!dp||!dn) return;
  try{var days=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
    if(dp.value) dn.textContent=days[new Date(dp.value+'T12:00:00').getDay()];}catch(e){dn.textContent='';}
}
function nbaProfit(odds, stake, res) {
  if (res === 'PUSH') return 0;
  if (res === 'LOSS') return -stake;
  if (res !== 'WIN') return 0;
  var o = parseFloat(String(odds).replace('+',''));
  if (isNaN(o)) return stake * 0.91; // fallback
  if (o > 0) return stake * (o / 100);
  if (o < 0) return stake * (100 / Math.abs(o));
  return 0;
}
function nbaTrkEsc(x){return String(x==null?'—':x).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function _nbaTrkPriced(odds){
  var o=Number(String(odds==null?'':odds).replace('+',''));
  return Number.isFinite(o)&&(o>0||(o>=-1000&&o<=-100));
}
async function loadNbaTrackRecord(record){
  var ovf=record==='overflow',hc=record==='hotcold',prefix=_nbaRecPrefix(record);
  var body=document.getElementById(prefix+'Body');
    if(body) body.innerHTML=_nbaRecordEmpty('Loading official results','Fetching the latest graded pre-game snapshots.');
  try{
    var r=await fetch(hc?'/api/hot-cold-track-record':(ovf?'/api/overflow-track-record':'/api/track-record'));
    if(!r.ok) throw new Error(await r.text());
    var d=await r.json();
    var data=d.dates||[];
    if(ovf)_nbaOvfData=data;else if(hc)_nbaHcData=data;else _nbaTrkData=data;
    
    var ds = document.getElementById(prefix+'DateSelect');
    if(ds){
      var previousDate=ds.value;
      ds.innerHTML = '';
      if(data.length === 0) {
        var o = document.createElement('option'); o.value = ''; o.textContent = 'No saved dates'; ds.appendChild(o);
      } else {
        data.forEach(function(x){
          var o = document.createElement('option'); o.value = x.date; o.textContent = x.date+(x.pending?' (pending)':''); ds.appendChild(o);
        });
        ds.value = data.some(function(x){return x.date===previousDate;})?previousDate:data[0].date;
      }
      _nbaTrkDayName(record);
    }
    
    renderNbaTrackDay(record);
  }catch(e){
    if(body) body.innerHTML=_nbaRecordEmpty('Track Record unavailable',e.message||'The official record could not be loaded.');
  }
}
function _nbaPeriodBounds(period,anchor){
  var start=anchor,end=anchor,d=new Date(anchor+'T12:00:00Z');
  if(period==='week'){d.setUTCDate(d.getUTCDate()-6);start=d.toISOString().slice(0,10);}
  else if(period==='month'){start=anchor.slice(0,7)+'-01';d=new Date(start+'T12:00:00Z');d.setUTCMonth(d.getUTCMonth()+1);d.setUTCDate(0);end=d.toISOString().slice(0,10);}
  else if(period==='all'){start='0001-01-01';end='9999-12-31';}
  return {start:start,end:end};
}
function _nbaPeriodLabel(period,bounds,picked){
  var n=picked.length,first=picked[picked.length-1].date,last=picked[0].date;
  var name=period==='week'?'Week (last 7 days)':period==='month'?'Month':'All Time';
  return name+' · '+(first===last?first:first+' to '+last)+' · '+n+' graded slate'+(n===1?'':'s');
}
function toggleTrkAcc(el) { el.parentElement.classList.toggle('open'); }
function nbaTrkCatClass(cat) { return 'cat-' + String(cat||'').replace(/[^a-zA-Z0-9]/g, ''); }
function renderNbaTrackDay(record){
  var ovf=record==='overflow',hc=record==='hotcold',prefix=_nbaRecPrefix(record);
  var data=ovf?_nbaOvfData:(hc?_nbaHcData:_nbaTrkData),view=ovf?_nbaOvfView:(hc?_nbaHcView:_nbaTrkView);
  var sel = document.getElementById(prefix+'DateSelect').value;
  var out = document.getElementById(prefix+'Body');
  var topPct = document.getElementById(prefix+'TopPct');
  var topStats = document.getElementById(prefix+'TopStats');
  var stake = Math.max(1,parseFloat(document.getElementById(prefix+'BetSize').value) || 100);
  
  if(hc&&data&&data.length){
    var hcBookEl=document.getElementById('nbaHcBook'),hcBook=hcBookEl?hcBookEl.value:'main';
    data=data.map(function(d){return Object.assign({},d,{detail:(d.detail||[]).filter(function(x){return hcBook==='all'||(hcBook==='overflow'?!!x.is_overflow:!x.is_overflow);})});});
  }
  if(hc&&(!data||!data.length)){
    if(out) out.innerHTML=_nbaRecordEmpty('No saved Hot / Cold results yet','Each live pre-tip run saves the Hot, Cold and Double Cold Top 10 (and ranks 11-20) per market. Results appear here once games finish.');
    if(topPct) topPct.innerHTML=''; if(topStats) topStats.innerHTML='';
    return;
  }
  if(!data||!data.length){
    if(out) out.innerHTML=_nbaRecordEmpty(ovf?'No saved overflow results yet':'No official results yet',ovf?'This record only contains saved overflow selections. New eligible pre-tip runs capture every qualifying play after each market/side Top 10; older dates are not backfilled.':'Pre-game snapshots will appear here after their games finish and the picks are graded.');
    if(topPct) topPct.innerHTML=''; if(topStats) topStats.innerHTML='';
    return;
  }
  
  var periodEl=document.getElementById(prefix+'Period'),period=periodEl?periodEl.value:'day';
  var bounds=_nbaPeriodBounds(period,sel||data[0].date);
  var picked=data.filter(function(d){return d.date>=bounds.start&&d.date<=bounds.end;});
  if(!picked.length) { if(out) out.innerHTML=_nbaRecordEmpty('No record for this '+(period==='day'?'date':'period'),'Choose another saved slate or period.'); if(topPct) topPct.innerHTML=''; if(topStats) topStats.innerHTML=''; return; }
  var day={date:sel,detail:[]};
  picked.forEach(function(d){(d.detail||[]).forEach(function(x){day.detail.push(Object.assign({},x,{_pdate:period==='day'?'':d.date}));});});
  
  var cats = ["Points","Rebounds","Assists","3-Pointers","Pts+Reb+Ast","Pts+Reb","Pts+Ast","Reb+Ast","Blocks","Steals"];
  if(hc){var hcCats=[];['Hot','Cold','Double Cold'].forEach(function(f){cats.forEach(function(c){hcCats.push(f+' '+c);});});cats=hcCats;}
  var groups = {};
  var dw=0, dl=0, dp=0, dpend=0, dnet=0, dstaked=0;
  
  (day.detail||[]).forEach(function(x){
    var cat = x.category||x.stat_key||"?";
    var s = (x.side||'OVER').toUpperCase();
    var k = cat+'|'+s;
    if(!groups[k]) groups[k] = { cat:cat, side:s, w:0, l:0, p:0, pend:0, net:0, staked:0, rows:[] };
    var g = groups[k];
    g.rows.push(x);
    
    if (x.result==='WIN'||x.result==='LOSS'||x.result==='PUSH') {
      var priced=_nbaTrkPriced(x.odds);
      x.clientProfit = priced?nbaProfit(x.odds, stake, x.result):null;
      if (priced && x.result !== 'PUSH') {
        g.staked += stake;
        dstaked += stake;
      }
      g.net += x.clientProfit;
      dnet += x.clientProfit;
      if(x.result==='WIN') { g.w++; dw++; }
      else if(x.result==='LOSS') { g.l++; dl++; }
      else { g.p++; dp++; }
    } else {
      x.clientProfit = null;
      g.pend++;
      dpend++;
    }
  });
  
  var droi = dstaked>0 ? (dnet/dstaked*100).toFixed(1) : '0.0';
  var dpctStr = (dw+dl)>0 ? (dw/(dw+dl)*100).toFixed(1)+'%' : '0%';
  if (topPct) topPct.innerHTML = dw+'W/'+dl+'L ('+dpctStr+')';
  if (topStats) {
    var netColor = dnet > 0 ? '#4ade80' : dnet < 0 ? '#f87171' : '#cbd5e1';
    var roiColor = (dstaked > 0 && dnet > 0) ? '#4ade80' : (dstaked > 0 && dnet < 0) ? '#f87171' : '#cbd5e1';
    topStats.innerHTML = 
      '<span style="color:'+netColor+'">Net '+(dnet>=0?'+':'')+'$'+dnet.toFixed(2)+'</span>'+
      '<span style="color:'+roiColor+'">ROI '+(dnet>=0?'+':'')+droi+'%</span>'+
      (dpend > 0 ? '<span style="color:#fbbf24">'+dpend+' pending</span>' : '')+
      '<span style="color:#64748b;font-weight:400;font-size:.75rem;margin-left:8px"> $'+stake.toFixed(2)+'/play · '+(dw+dl)+' priced plays</span>';
  }
  
  var html = (period==='day'?'':'<div style="color:#94a3b8;font-size:.74rem;margin:0 0 10px">'+nbaTrkEsc(_nbaPeriodLabel(period,bounds,picked))+'</div>')+_nbaRecordSummary(dw,dl,dpend,dnet,dstaked,stake);
  if(view==='full'){
    html+=_nbaRecordFullList(day.detail||[],stake,false);
    if(out)out.innerHTML=html;
    return;
  }
  function renderGroup(g) {
    var total = g.w+g.l;
    var pct = total>0 ? (g.w/total*100).toFixed(1)+'%' : '0%';
    var color = g.net>0 ? '#4ade80' : g.net<0 ? '#f87171' : '#cbd5e1';
    var pendStr = g.pend ? ' · '+g.pend+' pending' : '';
    html += '<div class="nba-acc-row '+nbaTrkCatClass(g.cat)+'">'+
      '<div class="nba-acc-head" onclick="toggleTrkAcc(this)">'+
        '<div class="nba-acc-title"><span style="color:#64748b;font-weight:800;font-size:.7rem;text-transform:uppercase;letter-spacing:.1em">Category</span> '+nbaTrkEsc(g.cat)+' <span class="nba-acc-side '+(g.side==='OVER'?'over':'under')+'">'+g.side+'</span></div>'+
        '<div class="nba-acc-stats">'+
          '<span>'+g.w+'W · '+g.l+'L'+pendStr+'</span>'+
          '<span style="color:#fcd34d">'+pct+'</span>'+
          '<span style="color:'+color+';width:80px;text-align:right">'+(g.net>=0?'+':'')+'$'+g.net.toFixed(2)+'</span>'+
          '<button class="nba-acc-btn">+</button>'+
        '</div>'+
      '</div>'+
      '<div class="nba-acc-body"><div style="overflow-x:auto"><table><thead><tr>'+
      '<th>#</th><th>Player</th><th>Market</th><th>Pick</th><th>Odds / Book</th><th>Stake</th><th>Actual</th><th>Result</th><th>P/L</th>'+
      '</tr></thead><tbody>'+
      g.rows.map(function(b){
        var rc=b.result==='WIN'?'#4ade80':b.result==='LOSS'?'#f87171':'#fbbf24';
        var pc=(b.clientProfit||0)>=0?'#4ade80':'#f87171';
        return '<tr>'+
          '<td>#'+b.rank+(b._pdate?'<br><small style="color:#64748b">'+nbaTrkEsc(b._pdate)+'</small>':'')+'</td><td>'+nbaTrkEsc(b.name)+'<br><small style="color:#94a3b8">'+nbaTrkEsc(b.team)+'</small></td>'+
          '<td>'+nbaTrkEsc(b.category)+'</td>'+
          '<td><span style="font-weight:700">'+nbaTrkEsc(b.side)+'</span> '+nbaTrkEsc(b.line)+'</td>'+
          '<td style="color:#94a3b8">'+nbaTrkEsc(b.odds)+'<br><small>'+nbaTrkEsc(b.book||'')+'</small></td><td>'+(_nbaTrkPriced(b.odds)?'$'+stake.toFixed(2):'—')+'</td><td>'+nbaTrkEsc(b.actual)+'</td>'+
          '<td style="color:'+rc+';font-weight:700">'+nbaTrkEsc(b.result||'PENDING')+'</td>'+
          '<td style="color:'+pc+';font-weight:700">'+(b.clientProfit!=null?(b.clientProfit>=0?'+':'')+'$'+Number(b.clientProfit).toFixed(2):'—')+'</td>'+
          '</tr>';
      }).join('')+
      '</tbody></table></div></div>'+
    '</div>';
  }
  
  cats.forEach(function(c){
    ['OVER','UNDER'].forEach(function(s){
      if(groups[c+'|'+s]) renderGroup(groups[c+'|'+s]);
    });
  });
  
  var handled = {}; cats.forEach(function(c){ handled[c]=true; });
  Object.keys(groups).forEach(function(k){
    var g = groups[k];
    if(!handled[g.cat]) renderGroup(g);
  });
  
  if(out) out.innerHTML = html || _nbaRecordEmpty('No records for this date','Choose another saved slate from the date menu.');
}
document.addEventListener('DOMContentLoaded',function(){
  var dp=document.getElementById('nbaTrkDate');
  if(dp){dp.value=new Date().toISOString().slice(0,10);
    dp.addEventListener('change',function(){_nbaTrkDayName();renderNbaTrackDay();});}
  _nbaTrkDayName();
  loadNbaTrackRecord();
  var top=document.getElementById('nba-btn-top'),bot=document.getElementById('nba-btn-bot');
  function _sc(){var y=window.pageYOffset||document.documentElement.scrollTop;
    var atBot=(y+window.innerHeight)>=document.body.scrollHeight-50;
    if(top) top.style.display=y>400?'block':'none';
    if(bot) bot.style.display=!atBot?'block':'none';}
  window.addEventListener('scroll',_sc,{passive:true});_sc();
});
</script>
<button id="nba-btn-top" onclick="window.scrollTo({top:0,behavior:'smooth'})" title="Back to top" style="position:fixed;bottom:76px;right:22px;z-index:9999;display:none;width:48px;height:48px;border-radius:50%;border:none;cursor:pointer;background:#f59e0b;color:#0a0a0a;font-size:1.4rem;font-weight:900;box-shadow:0 4px 14px rgba(0,0,0,.45);line-height:1">&#8593;</button>
<button id="nba-btn-bot" onclick="window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'})" title="Scroll to bottom" style="position:fixed;bottom:22px;right:22px;z-index:9999;display:none;width:48px;height:48px;border-radius:50%;border:none;cursor:pointer;background:#0ea5e9;color:#0a0a0a;font-size:1.4rem;font-weight:900;box-shadow:0 4px 14px rgba(0,0,0,.45);line-height:1">&#8595;</button>
</body>

</body>
</html>"""

# Kept outside MAIN_HTML so this large non-raw Python HTML string cannot be
# accidentally damaged by Coach JavaScript template braces or backslashes.
NBA_COACH_HTML = r"""
<style>
.nba-coach-shell{border:1px solid rgba(245,158,11,.45)!important;background:linear-gradient(145deg,#1f1406,#100902)!important}
.nba-coach-presets{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0 9px}
.nba-coach-preset{background:#111827;color:#cbd5e1;border:1px solid #334155;border-radius:999px;padding:7px 11px;font-size:.69rem;font-weight:900;cursor:pointer}
.nba-coach-preset:hover{border-color:#f59e0b;color:#fde68a}
.nba-coach-preset.over-preset{border-color:#1d4ed8;color:#93c5fd}
.nba-coach-preset.under-preset{border-color:#b45309;color:#fcd34d}
.nba-coach-row{display:flex;gap:8px;margin-top:16px}
.nba-coach-input{flex:1;min-width:0;background:#070d18;color:#fff;border:1px solid #334155;border-radius:11px;padding:12px 14px;font:inherit;font-size:.84rem;outline:none}
.nba-coach-input:focus{border-color:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.1)}
.nba-coach-send{background:linear-gradient(135deg,#d97706,#f59e0b);color:#fff;border:0;border-radius:11px;padding:0 18px;font-weight:900;cursor:pointer}
.nba-coach-answer{display:none;margin-top:14px;border-top:1px solid rgba(245,158,11,.25);padding-top:14px}
.nba-coach-question{margin-left:auto;max-width:82%;background:#271604;border:1px solid rgba(245,158,11,.3);border-radius:12px 12px 3px 12px;padding:9px 12px;color:#fde68a;font-size:.75rem}
.nba-coach-table-wrap{overflow-x:auto;margin-top:10px;border:1px solid #292929;border-radius:11px}
.nba-coach-table{width:100%;border-collapse:collapse;font-size:.7rem;min-width:720px}
.nba-coach-table th{background:#111;color:#9ca3af;text-align:left;padding:8px 9px;font-size:.59rem;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap}
.nba-coach-table td{padding:9px;border-top:1px solid #222;color:#e5e7eb;vertical-align:top}
.nba-coach-table tbody tr{cursor:pointer}
.nba-coach-table tbody tr:hover{background:rgba(245,158,11,.06)}
@media(max-width:600px){.nba-coach-row{flex-direction:column}.nba-coach-send{padding:12px}.nba-coach-question{max-width:94%}}
.nba-coach-shell.ncx{border:1px solid rgba(56,189,248,.45)!important;background:linear-gradient(145deg,#101827,#0b1220)!important}
.ncx-head{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:14px;align-items:center}
.ncx-badge{width:48px;height:48px;border-radius:14px;display:grid;place-items:center;font-size:1.5rem;background:radial-gradient(circle at 30% 25%,rgba(56,189,248,.35),rgba(124,58,237,.25));border:1px solid rgba(125,211,252,.4);box-shadow:0 0 22px rgba(56,189,248,.18)}
.ncx-kicker{color:#38bdf8;font-size:.62rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase}
.ncx-title{font-family:'Playfair Display',serif;color:#fff;font-size:1.5rem;margin:2px 0 0;line-height:1.15}
.ncx-sub{color:#94a3b8;font-size:.74rem;margin-top:3px}
.ncx-side{display:flex;flex-direction:column;gap:7px;align-items:stretch;min-width:190px}
.ncx-noinv{align-self:flex-end;border:1px solid rgba(74,222,128,.38);background:rgba(74,222,128,.09);color:#86efac;border-radius:999px;padding:4px 9px;font-size:.6rem;font-weight:900;letter-spacing:.06em}
.ncx-trk{background:#0f1726;color:#7dd3fc;border:1px solid rgba(56,189,248,.45);border-radius:10px;padding:8px 12px;font-size:.72rem;font-weight:900;cursor:pointer;white-space:nowrap}
.ncx-trk:hover{background:rgba(56,189,248,.12)}
.ncx-parlay{background:linear-gradient(135deg,#0369a1,#7c3aed);color:#fff;border:1px solid rgba(125,211,252,.5);border-radius:10px;padding:9px 14px;font-size:.74rem;font-weight:900;cursor:pointer;white-space:nowrap;box-shadow:0 6px 18px rgba(124,58,237,.25)}
.ncx-parlay:hover{filter:brightness(1.12)}
.ncx-tabs{display:flex;gap:4px;margin:18px 0 0;padding:4px;background:#0a101c;border:1px solid #1e293b;border-radius:12px}
.ncx-tab{flex:1;background:transparent;border:0;border-radius:9px;color:#94a3b8;font-size:.74rem;font-weight:900;padding:9px 8px;cursor:pointer;white-space:nowrap}
.ncx-tab span{display:inline-block;margin-left:4px;background:#1e293b;color:#cbd5e1;border-radius:999px;padding:1px 7px;font-size:.6rem}
.ncx-tab:hover{color:#e2e8f0}
.ncx-tab.active{background:linear-gradient(135deg,rgba(56,189,248,.2),rgba(124,58,237,.18));color:#fff;box-shadow:inset 0 0 0 1px rgba(125,211,252,.35)}
.ncx-tab.active span{background:#38bdf8;color:#0b1220}
.ncx-panels{margin:10px 0 12px}
.ncx-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px}
.ncx-grid[hidden]{display:none}
.nba-coach-preset.ncx-preset{position:relative;text-align:left;border-radius:12px;padding:11px 12px 11px 15px;background:linear-gradient(135deg,#0f1726,#0b111d);border:1px solid #1f2b3d;color:#e2e8f0;overflow:hidden;transition:transform .12s ease,border-color .12s ease,box-shadow .12s ease}
.ncx-preset:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--c)}
.ncx-preset b{display:block;font-size:.76rem;font-weight:900;color:#f8fafc}
.ncx-preset small{display:block;font-size:.64rem;font-weight:600;color:#8b9ab0;margin-top:3px}
.nba-coach-preset.ncx-preset:hover{transform:translateY(-1px);border-color:var(--c);box-shadow:0 6px 16px rgba(0,0,0,.35);color:#e2e8f0}
.ncx .nba-coach-preset.is-on{border-color:var(--c,#38bdf8);box-shadow:0 0 0 1px var(--c,#38bdf8) inset}
.ncx-mk{position:relative;background:linear-gradient(135deg,#0f1726,#0b111d);border:1px solid #1f2b3d;border-radius:12px;padding:10px 12px 10px 15px;overflow:hidden}
.ncx-mk:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:linear-gradient(#22c55e,#f87171)}
.ncx-mk-t{color:#f8fafc;font-size:.76rem;font-weight:900;margin-bottom:7px}
.ncx-mk-b{display:flex;gap:6px}
.ncx-mk-b .nba-coach-preset{flex:1;text-align:center;border-radius:8px;background:#0a101c;padding:6px 4px;font-size:.64rem;white-space:nowrap}
.nba-coach-preset.ncx-ov{color:#86efac;border-color:rgba(74,222,128,.35)}
.nba-coach-preset.ncx-un{color:#fca5a5;border-color:rgba(248,113,113,.35)}
.ncx .nba-coach-preset.ncx-ov.is-on,.ncx .nba-coach-preset.ncx-un.is-on{background:rgba(56,189,248,.16);border-color:#38bdf8;color:#fff;box-shadow:none}
.nba-coach-row.ncx-ask{align-items:center;gap:8px;margin-top:0;background:#0a101c;border:1px solid #1e293b;border-radius:12px;padding:6px 6px 6px 12px}
.ncx-ask:focus-within{border-color:#38bdf8;box-shadow:0 0 0 3px rgba(56,189,248,.12)}
.ncx-ask-ico{font-size:1rem;opacity:.8}
.ncx-ask .nba-coach-input{background:transparent;border:0;box-shadow:none!important}
.ncx-ask .nba-coach-send{align-self:stretch;padding:10px 20px;font-size:.8rem;background:linear-gradient(135deg,#0284c7,#7c3aed)}
.ncx-how{margin-top:8px;color:#64748b;font-size:.65rem;line-height:1.45}
.ncx-how>summary{cursor:pointer;color:#7c8aa0;font-weight:800;list-style:none;display:inline-block}
.ncx-how>summary::-webkit-details-marker{display:none}
.ncx-how>div{margin-top:6px}
@media(max-width:620px){.ncx-head{grid-template-columns:auto minmax(0,1fr)}.ncx-side{grid-column:1/-1;min-width:0}.ncx-noinv{align-self:flex-start}.nba-coach-row.ncx-ask{flex-direction:row;flex-wrap:wrap}.ncx-ask .nba-coach-send{width:100%}.ncx-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.ncx-mk-grid{grid-template-columns:1fr}}
@media(max-width:420px){.ncx-grid{grid-template-columns:1fr}}
</style>
<section id="nba-coach" class="card nba-coach-shell ncx" style="max-width:960px;margin:28px auto 20px;padding:20px 22px">
  <div class="ncx-head">
    <div class="ncx-badge">&#127936;</div>
    <div class="ncx-titles">
      <div class="ncx-kicker">Grounded NBA analysis · genuine sportsbook prices</div>
      <h2 class="ncx-title">The Edge Coach</h2>
      <div class="ncx-sub">Pick a play type below, or ask the Coach a question.</div>
    </div>
    <div class="ncx-side">
      <div class="ncx-noinv">NO INVENTED PLAYS</div>
      <button type="button" class="ncx-trk" onclick="openNbaCoachTrack()">&#129504; Coach Track Record</button>
      <button type="button" class="ncx-parlay" onclick="showNbaPerfectParlayBuilder()">&#10024; Perfect Parlay</button>
    </div>
  </div>
  <div class="ncx-tabs" role="tablist">
    <button type="button" class="ncx-tab active" data-ncx-tab="top" onclick="_ncxTab('top')">&#11088; Top Picks <span>4</span></button>
    <button type="button" class="ncx-tab" data-ncx-tab="props" onclick="_ncxTab('props')">&#127936; Player Props <span>10</span></button>
    <button type="button" class="ncx-tab" data-ncx-tab="form" onclick="_ncxTab('form')">&#128293; Hot / Cold <span>3</span></button>
  </div>
  <div class="ncx-panels">
    <div class="ncx-grid" data-ncx-panel="top">
      <button type="button" class="nba-coach-preset ncx-preset" style="--c:#38bdf8" onclick="nbaCoachPreset('safest')"><b class="ncx-t">Safest Bets</b><small>80%+ vs opponent H/A, moved to a safer line</small></button>
      <button type="button" class="nba-coach-preset ncx-preset" style="--c:#a78bfa" onclick="nbaCoachPreset('positive edge')"><b class="ncx-t">Coach Edge</b><small>Biggest app-vs-book probability gap</small></button>
      <button type="button" class="nba-coach-preset ncx-preset" style="--c:#f59e0b" onclick="nbaCoachPreset('alternate_minus')"><b class="ncx-t">Best &#8722; Alternate Plays · Top 10</b><small>Safer alternate lines, minus odds</small></button>
      <button type="button" class="nba-coach-preset ncx-preset" style="--c:#4ade80" onclick="nbaCoachPreset('alternate_plus')"><b class="ncx-t">Best + Alternate Plays · Top 10</b><small>Plus-money alternate lines with edge</small></button>
    </div>
    <div class="ncx-grid" data-ncx-panel="form" hidden>
      <button type="button" class="nba-coach-preset ncx-preset" style="--c:#f97316" onclick="nbaCoachPreset('hot')"><b class="ncx-t">&#128293; Hot · Top 10</b><small>OVER in 80%+ of last 5 · avg over the line</small></button>
      <button type="button" class="nba-coach-preset ncx-preset" style="--c:#38bdf8" onclick="nbaCoachPreset('cold')"><b class="ncx-t">&#129482; Cold · Top 10</b><small>UNDER in 80%+ of last 5 · avg under the line</small></button>
      <button type="button" class="nba-coach-preset ncx-preset" style="--c:#818cf8" onclick="nbaCoachPreset('double_cold')"><b class="ncx-t">&#10052;&#65039; Double Cold · Top 10</b><small>Cold + 70%+ Under in 3+ meetings vs opponent</small></button>
    </div>
    <div class="ncx-grid ncx-mk-grid" data-ncx-panel="props" hidden>
        <div class="ncx-mk"><div class="ncx-mk-t">Points</div><div class="ncx-mk-b"><button type="button" class="nba-coach-preset ncx-ov" onclick="nbaCoachPreset('points over')">Over · Top 10</button><button type="button" class="nba-coach-preset ncx-un" onclick="nbaCoachPreset('points under')">Under · Top 10</button></div></div>
        <div class="ncx-mk"><div class="ncx-mk-t">Rebounds</div><div class="ncx-mk-b"><button type="button" class="nba-coach-preset ncx-ov" onclick="nbaCoachPreset('rebounds over')">Over · Top 10</button><button type="button" class="nba-coach-preset ncx-un" onclick="nbaCoachPreset('rebounds under')">Under · Top 10</button></div></div>
        <div class="ncx-mk"><div class="ncx-mk-t">Assists</div><div class="ncx-mk-b"><button type="button" class="nba-coach-preset ncx-ov" onclick="nbaCoachPreset('assists over')">Over · Top 10</button><button type="button" class="nba-coach-preset ncx-un" onclick="nbaCoachPreset('assists under')">Under · Top 10</button></div></div>
        <div class="ncx-mk"><div class="ncx-mk-t">3-Pointers</div><div class="ncx-mk-b"><button type="button" class="nba-coach-preset ncx-ov" onclick="nbaCoachPreset('3-pointers over')">Over · Top 10</button><button type="button" class="nba-coach-preset ncx-un" onclick="nbaCoachPreset('3-pointers under')">Under · Top 10</button></div></div>
        <div class="ncx-mk"><div class="ncx-mk-t">PRA</div><div class="ncx-mk-b"><button type="button" class="nba-coach-preset ncx-ov" onclick="nbaCoachPreset('pra over')">Over · Top 10</button><button type="button" class="nba-coach-preset ncx-un" onclick="nbaCoachPreset('pra under')">Under · Top 10</button></div></div>
        <div class="ncx-mk"><div class="ncx-mk-t">Pts+Reb</div><div class="ncx-mk-b"><button type="button" class="nba-coach-preset ncx-ov" onclick="nbaCoachPreset('pts+reb over')">Over · Top 10</button><button type="button" class="nba-coach-preset ncx-un" onclick="nbaCoachPreset('pts+reb under')">Under · Top 10</button></div></div>
        <div class="ncx-mk"><div class="ncx-mk-t">Pts+Ast</div><div class="ncx-mk-b"><button type="button" class="nba-coach-preset ncx-ov" onclick="nbaCoachPreset('pts+ast over')">Over · Top 10</button><button type="button" class="nba-coach-preset ncx-un" onclick="nbaCoachPreset('pts+ast under')">Under · Top 10</button></div></div>
        <div class="ncx-mk"><div class="ncx-mk-t">Reb+Ast</div><div class="ncx-mk-b"><button type="button" class="nba-coach-preset ncx-ov" onclick="nbaCoachPreset('reb+ast over')">Over · Top 10</button><button type="button" class="nba-coach-preset ncx-un" onclick="nbaCoachPreset('reb+ast under')">Under · Top 10</button></div></div>
        <div class="ncx-mk"><div class="ncx-mk-t">Blocks</div><div class="ncx-mk-b"><button type="button" class="nba-coach-preset ncx-ov" onclick="nbaCoachPreset('blocks over')">Over · Top 10</button><button type="button" class="nba-coach-preset ncx-un" onclick="nbaCoachPreset('blocks under')">Under · Top 10</button></div></div>
        <div class="ncx-mk"><div class="ncx-mk-t">Steals</div><div class="ncx-mk-b"><button type="button" class="nba-coach-preset ncx-ov" onclick="nbaCoachPreset('steals over')">Over · Top 10</button><button type="button" class="nba-coach-preset ncx-un" onclick="nbaCoachPreset('steals under')">Under · Top 10</button></div></div>
    </div>
  </div>
  <div class="nba-coach-row ncx-ask">
    <span class="ncx-ask-ico">&#128172;</span>
    <input id="nbaCoachQuery" class="nba-coach-input" aria-label="Coach Edge search" placeholder="Ask the Coach, e.g. best rebounds overs tonight" onkeydown="if(event.key==='Enter')nbaCoachSearch()">
    <button class="nba-coach-send" onclick="nbaCoachSearch()">Analyze</button>
  </div>
  <details class="ncx-how"><summary>&#9432; How it works</summary><div>Requires a loaded NBA board and genuine sportsbook prices. Safest Bets takes sides that hit the book line 80%+ against today’s opponent at this venue, then moves to the genuine line on the safer side (lower Over, higher Under) that hit 100% of those games, with odds no shorter than -1000. Best - Alternate Plays ranks qualifying sides by highest app probability; Coach Edge still requires positive probability edge. Best + Alternate Plays ranks positive-odds alternates by Coach Edge.</div></details>
  <div id="nbaCoachMsg" role="status" style="color:#fbbf24;font-size:.72rem;margin-top:10px"></div>
  <div id="nbaCoachResults" class="nba-coach-answer"></div>
  <div id="nbaCoachTrackPanel" style="display:none;margin-top:22px;padding-top:16px;border-top:1px solid #1e293b">
    <div class="nba-record-hero" style="background:radial-gradient(circle at 92% 10%,rgba(124,58,237,.2),transparent 38%),linear-gradient(145deg,#111827,#080d19)">
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap">
        <div><div class="nba-record-kicker" style="color:#c4b5fd">Edge Coach Analytics</div><h3 class="nba-record-title">&#129504; Coach Track Record</h3><div class="nba-record-sub">Official pre-tip Coach snapshots and isolated historical Coach replay results.</div></div>
        <button class="nba-view-btn" onclick="document.getElementById('nbaCoachTrackPanel').style.display='none'">Close</button>
      </div>
      <div class="nba-record-tools">
        <label><span class="nba-record-label">Record source</span><select id="nbaCoachTrkSource" class="nba-record-control"><option value="official">Official Coach</option><option value="historical">Historical Edge Coach</option></select></label>
        <label><span class="nba-record-label">Record date</span><input type="date" id="nbaCoachTrkDate" class="nba-record-control" value="__TODAY__"></label>
        <label><span class="nba-record-label">Flat bet amount</span><input type="number" id="nbaCoachTrkStake" class="nba-record-control" value="100.00" min="1" step="5" style="width:110px" oninput="renderNbaCoachTrackRecord()"></label>
        <button class="nba-view-btn active" onclick="loadNbaCoachTrackRecord()">Get Results</button>
        <div class="nba-view-tabs"><button id="nbaCoachCatBtn" class="nba-view-btn active" onclick="setNbaCoachTrackView('category')">By Category</button><button id="nbaCoachFullBtn" class="nba-view-btn" onclick="setNbaCoachTrackView('full')">Full List</button></div>
      </div>
    </div>
    <div id="nbaCoachTrackMsg" role="status" style="color:#c4b5fd;font-size:.68rem;margin:10px 4px 0"></div>
    <div id="nbaCoachTrackResults"></div>
  </div>
</section>
<script>
 var __nbaCoachRows=[],__nbaPerfectParlayPool=[],__nbaPerfectParlayLegs=[];
 var __nbaPerfectParlaySettings={legs:3,categories:[],side:'ALL',rotation:'PREFER'};
 var __nbaPerfectParlayStake=100,__nbaPerfectParlayDate='',__nbaPerfectParlayNotice='',__nbaPerfectParlayBoardSource=null;
 var __nbaPerfectParlayCategories=[
  {key:'PTS',label:'Points'},{key:'REB',label:'Rebounds'},{key:'AST',label:'Assists'},
  {key:'FG3M',label:'3-Pointers'},{key:'PRA',label:'Pts + Reb + Ast'},
  {key:'PTS_REB',label:'Pts + Reb'},{key:'PTS_AST',label:'Pts + Ast'},
  {key:'REB_AST',label:'Reb + Ast'},{key:'BLK',label:'Blocks'},{key:'STL',label:'Steals'}
 ];
 var __nbaPerfectParlayFormCats=[
  {key:'HC_HOT',form:'hot',label:'&#128293; Hot'},{key:'HC_COLD',form:'cold',label:'&#129482; Cold'},
  {key:'HC_DOUBLE_COLD',form:'double_cold',label:'&#10052;&#65039; Double Cold'}
 ];
function _nbaHcParlayRows(selected,side){
 var forms={};(__nbaPerfectParlayFormCats||[]).forEach(function(c){if((selected||[]).indexOf(c.key)>=0)forms[c.form]=c;});
 var src=(typeof nbaHotColdData!=='undefined'&&nbaHotColdData)||[],out=[];
 src.forEach(function(r){
  if(!r||!forms[r.form_key])return;
  if(side&&side!=='ALL'&&r.pick!==side)return;
  if(r.odds==null||r.app_probability==null)return;
  var american=Number(r.odds),implied=_nbaPerfectParlayImplied(american),model=Number(r.app_probability)/100;
  if(!isFinite(american)||implied==null||!isFinite(model)||model<=0||model-implied<=0)return;
  var mpg=null;(allPicksData||[]).some(function(p){if(String(p.player)===String(r.player)&&p.mpg!=null){mpg=p.mpg;return true;}return false;});
  out.push({player:r.player,team:r.team,stat:r.stat,category:r.category,side:r.pick,line:Number(r.line),odds:american,
   model_probability:model,implied_probability:implied,edge:model-implied,coach_edge:(model-implied)*100,
   source:r.book,book:r.book,_board_mpg:mpg,_board_pct:Number(r.app_probability),recent_average:r.recent_avg,
   game_log:(r.recent_games||[]).map(function(g){return g.v;}),
   opponent_history:(r.opp_games||[]).map(function(g){return {d:g.d,v:g.v};}),
   selection_reason:String(r.form||'')+' form ('+r.recent_hits+'/'+r.recent_total+' L5) plus positive Coach Edge at the exact standard line.'});
 });
 return out;
}
function _ncxTab(key){
  document.querySelectorAll('#nba-coach .ncx-tab').forEach(function(t){t.classList.toggle('active',t.getAttribute('data-ncx-tab')===key);});
  document.querySelectorAll('#nba-coach .ncx-grid').forEach(function(g){g.hidden=g.getAttribute('data-ncx-panel')!==key;});
}
document.addEventListener('click',function(e){
  var b=e.target&&e.target.closest?e.target.closest('#nba-coach .nba-coach-preset'):null;
  if(!b)return;
  document.querySelectorAll('#nba-coach .nba-coach-preset.is-on').forEach(function(x){if(x!==b)x.classList.remove('is-on');});
  b.classList.add('is-on');
});
function nbaCoachPreset(q){
 var labels={
  safest:'What are the safest NBA bets?',
  'positive edge':'What are the best NBA Coach Edge plays?',
  alternate_minus:'What are the Best - Alternate NBA Plays? — Top 10',
  alternate_plus:'What are the Best + Alternate NBA Plays? — Top 10',
  hot:'What are the Hot NBA plays? — Top 10',
  cold:'What are the Cold NBA plays? — Top 10',
  double_cold:'What are the Double Cold NBA plays? — Top 10',
   'points over':'What are the Top 10 Points OVER plays?',
   'points under':'What are the Top 10 Points UNDER plays?',
   'rebounds over':'What are the Top 10 Rebounds OVER plays?',
   'rebounds under':'What are the Top 10 Rebounds UNDER plays?',
   'assists over':'What are the Top 10 Assists OVER plays?',
   'assists under':'What are the Top 10 Assists UNDER plays?',
   '3-pointers over':'What are the Top 10 3-Pointers OVER plays?',
   '3-pointers under':'What are the Top 10 3-Pointers UNDER plays?',
   'pra over':'What are the Top 10 PRA OVER plays?',
   'pra under':'What are the Top 10 PRA UNDER plays?',
   'pts+reb over':'What are the Top 10 Pts+Reb OVER plays?',
   'pts+reb under':'What are the Top 10 Pts+Reb UNDER plays?',
   'pts+ast over':'What are the Top 10 Pts+Ast OVER plays?',
   'pts+ast under':'What are the Top 10 Pts+Ast UNDER plays?',
   'reb+ast over':'What are the Top 10 Reb+Ast OVER plays?',
   'reb+ast under':'What are the Top 10 Reb+Ast UNDER plays?',
   'blocks over':'What are the Top 10 Blocks OVER plays?',
   'blocks under':'What are the Top 10 Blocks UNDER plays?',
   'steals over':'What are the Top 10 Steals OVER plays?',
   'steals under':'What are the Top 10 Steals UNDER plays?'
 };
 var input=document.getElementById('nbaCoachQuery');
 if(input)input.value=labels[q]||q;
 var formModes={hot:1,cold:1,double_cold:1};
 nbaCoachSearch(formModes[q]?q:(q==='alternate_minus'?'alternate_minus':(q==='alternate_plus'?'alternate_plus':(q==='safest'?'safest':'all'))),labels[q]||q);
}
function _nbaPerfectParlayCommit(html){
 var msg=document.getElementById('nbaCoachMsg'),box=document.getElementById('nbaCoachResults');
 if(msg)msg.textContent='';
 if(box){box.innerHTML=html;box.style.display='block';}
}
function setNbaPerfectParlayCategories(checked){
 document.querySelectorAll('input[name="nbaPerfectParlayCat"]:not(:disabled)').forEach(function(el){el.checked=!!checked;});
}
function _nbaPerfectParlayBoardRows(side){
 var rows=[],wantedSide=side||'ALL';
 var approved={};
 (nbaApprovedPicks||[]).forEach(function(p){
  if(!p.historical_replay&&String(p.line_rec||p.streak_rec||'OVER').toUpperCase()!==_nbaTopSideOf(p))return;
  var key=_nbaPerfectParlayExactKey(p);if(key)approved[key]=true;
 });
 (__nbaPerfectParlayCategories||[]).forEach(function(cat){
  var categoryRows=(allPicksData||[]).filter(function(p){
   return approved[_nbaPerfectParlayExactKey(p)]&&String((p&&p.stat)||'').toUpperCase()===cat.key&&
    (wantedSide==='ALL'||_nbaTopSideOf(p)===wantedSide);
  }).slice(0,10);
  rows=rows.concat(categoryRows);
 });
 return rows;
}
function _nbaPerfectParlayImplied(odds){
 var o=Number(String(odds==null?'':odds).replace('+',''));
 if(!isFinite(o)||!o||o<-1000)return null;
 return o>0?100/(o+100):Math.abs(o)/(Math.abs(o)+100);
}
function _nbaPerfectParlayLocalRows(boardByKey){
 var propsByKey={};
 (nbaPropsData||[]).forEach(function(p){
  var key=_nbaPerfectParlayExactKey(p);
  if(!key||key==='|')return;
  if(!propsByKey[key])propsByKey[key]=[];
  propsByKey[key].push(p);
 });
 var labels={};(__nbaPerfectParlayCategories||[]).forEach(function(cat){labels[cat.key]=cat.label;});
 return Object.keys(boardByKey).map(function(key){
  var board=boardByKey[key],side=_nbaTopSideOf(board);
  if(side!=='OVER'&&side!=='UNDER')return null;
  var exactKey=_nbaPerfectParlayExactKey(board);
  var candidates=[board].concat(propsByKey[exactKey]||[]),chosen=null,odds=null,line=null;
  for(var i=0;i<candidates.length;i++){
   var p=candidates[i]||{};
   if(_nbaPerfectParlayExactKey(p)!==exactKey)continue;
   line=_nbaPerfectParlayLine(p);
   var quoteLine=p.dk_line!=null?p.dk_line:p.line;
   odds=Number(quoteLine)===Number(line)?(side==='OVER'?p.dk_over_odds:p.dk_under_odds):null;
   if(odds==null&&p.line!=null&&Number(p.line)===Number(line))odds=side==='OVER'?p.over_odds:p.under_odds;
   if(odds==null&&p.historical_replay&&Number(p.line)===Number(line))odds=p.fd_odds;
   if(odds==null&&side==='OVER'&&p.fd_line!=null&&Number(p.fd_line)===Number(line))odds=p.fd_odds;
   var book=p.bookmaker_label||p.book||p.bookmaker;
   if(line!=null&&odds!=null&&String(odds)!==''&&book&&_nbaPerfectParlayImplied(odds)!=null){chosen=p;break;}
  }
  if(!chosen)return null;
  var american=Number(String(odds).replace('+','')),implied=_nbaPerfectParlayImplied(american);
  var model=Number(board.pct)/100;
  var edge=model-(implied==null?model:implied);
  var source=chosen.bookmaker_label||chosen.book||chosen.bookmaker;
  if(!isFinite(american)||american<-1000||implied==null||!isFinite(model)||model<=0||edge<=0)return null;
  return {
   player:board.player,team:board.team,stat:board.stat,
   category:labels[String(board.stat||'').toUpperCase()]||board.stat_label||board.stat,
   side:side,line:Number(line),odds:american,model_probability:model,
   implied_probability:implied,edge:edge,coach_edge:edge*100,
   source:source,book:source,_board_mpg:board.mpg,_board_pct:Number(board.pct||0),
   recent_average:board.recent_avg,game_log:(board.recent_glog||[]).map(function(g){return g&&g.v;}),
   opponent_history:board.glog||[],selection_reason:'Displayed app-board probability exceeds sportsbook implied probability.'
  };
 }).filter(Boolean);
}
function showNbaPerfectParlayBuilder(){
  var saved=__nbaPerfectParlaySettings||{},savedLegs=Number(saved.legs)||3;
 var options='';
  for(var i=2;i<=10;i++)options+='<option value="'+i+'"'+(i===savedLegs?' selected':'')+'>'+i+' legs</option>';
  var available={};
  _nbaPerfectParlayBoardRows(saved.side||'ALL').forEach(function(p){var stat=String((p&&p.stat)||'').toUpperCase();if(stat)available[stat]=(available[stat]||0)+1;});
  var selected=(saved.categories&&saved.categories.length)?saved.categories.slice():__nbaPerfectParlayCategories.map(function(x){return x.key;});
  __nbaPerfectParlayFormCats.forEach(function(cat){available[cat.key]=_nbaHcParlayRows([cat.key],saved.side||'ALL').length;});
  var categoryOptions=__nbaPerfectParlayCategories.concat(__nbaPerfectParlayFormCats).map(function(cat){
   var count=available[cat.key]||0,checked=selected.indexOf(cat.key)>=0,disabled=!count;
   return '<label style="display:flex;align-items:center;gap:6px;padding:7px 9px;background:'+(checked&&!disabled?'rgba(59,130,246,.14)':'#020617')+';border:1px solid '+(checked&&!disabled?'#3b82f6':'#334155')+';border-radius:7px;color:'+(disabled?'#475569':'#e2e8f0')+';font-size:.68rem;font-weight:800;cursor:'+(disabled?'not-allowed':'pointer')+'">'+
    '<input type="checkbox" name="nbaPerfectParlayCat" value="'+cat.key+'"'+(checked&&!disabled?' checked':'')+(disabled?' disabled':'')+' style="accent-color:#3b82f6">'+
    cat.label+' <span style="color:#64748b">('+count+')</span></label>';
  }).join('');
  var fieldStyle='margin-top:5px;min-width:145px;background:#020617;color:#fff;border:1px solid #475569;border-radius:7px;padding:9px 10px;font-weight:800';
 _nbaPerfectParlayCommit(
  '<div><div class="nba-coach-question">&#10024; Perfect Parlay</div>'+
   '<div style="margin-top:11px;color:#cbd5e1;font-size:.78rem;line-height:1.55">Choose your markets, direction, rotation preference, and number of legs. Every result still comes only from this date’s displayed app boards with a genuine standard line and positive Coach Edge.</div>'+
   '<div style="margin-top:14px;padding:12px;background:#0f172a;border:1px solid #334155;border-radius:10px">'+
    '<div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:7px"><div style="color:#94a3b8;font-size:.68rem;font-weight:900">CATEGORIES — SELECT ONE OR MORE</div><div style="display:flex;gap:5px"><button onclick="setNbaPerfectParlayCategories(true)" style="background:#1e293b;color:#bfdbfe;border:1px solid #334155;border-radius:6px;padding:5px 8px;font-size:.62rem;font-weight:800;cursor:pointer">Select all</button><button onclick="setNbaPerfectParlayCategories(false)" style="background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:6px;padding:5px 8px;font-size:.62rem;font-weight:800;cursor:pointer">Clear</button></div></div>'+
    '<div style="display:flex;flex-wrap:wrap;gap:7px">'+categoryOptions+'</div>'+
    '<div style="display:flex;align-items:end;gap:9px;flex-wrap:wrap;margin-top:13px">'+
     '<label style="color:#94a3b8;font-size:.68rem;font-weight:800">PARLAY SIZE<br><select id="nbaPerfectParlayLegs" style="'+fieldStyle+'">'+options+'</select></label>'+
     '<label style="color:#94a3b8;font-size:.68rem;font-weight:800">SIDE<br><select id="nbaPerfectParlaySide" style="'+fieldStyle+'"><option value="ALL"'+(saved.side==='ALL'?' selected':'')+'>Best available</option><option value="OVER"'+(saved.side==='OVER'?' selected':'')+'>Overs only</option><option value="UNDER"'+(saved.side==='UNDER'?' selected':'')+'>Unders only</option></select></label>'+
     '<label style="color:#94a3b8;font-size:.68rem;font-weight:800">PLAYER POOL<br><select id="nbaPerfectParlayRotation" style="'+fieldStyle+'"><option value="PREFER"'+(saved.rotation==='PREFER'?' selected':'')+'>Prefer 24+ MPG</option><option value="ONLY"'+(saved.rotation==='ONLY'?' selected':'')+'>24+ MPG only</option><option value="ANY"'+(saved.rotation==='ANY'?' selected':'')+'>Any qualifying player</option></select></label>'+
    '<button id="nbaPerfectParlayBuildBtn" onclick="buildNbaPerfectParlay()" style="background:linear-gradient(135deg,#d97706,#7c3aed);color:#fff;border:0;border-radius:8px;padding:10px 15px;font-weight:950;cursor:pointer">BUILD PERFECT PARLAY</button>'+
     '</div><div id="nbaPerfectParlayBuildStatus" role="status" style="display:none;margin-top:10px;padding:9px 11px;border-radius:8px;background:rgba(59,130,246,.1);border:1px solid rgba(96,165,250,.28);color:#bfdbfe;font-size:.7rem;font-weight:800">Building from the selected category boards…</div></div></div>');
}
function _nbaPerfectParlayAmerican(decimalOdds){
 var d=Number(decimalOdds);if(!isFinite(d)||d<=1)return 'N/A';
 var american=d>=2?(d-1)*100:-100/(d-1),rounded=Math.round(american);
 return (rounded>0?'+':'')+rounded;
}
function _nbaPerfectParlayPlayerKey(x){return String((x&&x.player)||'').toLowerCase().replace(/[^a-z0-9]/g,'')}
function _nbaPerfectParlayBoardKey(x){
 return _nbaPerfectParlayPlayerKey(x)+'|'+String((x&&x.stat)||'').toUpperCase();
}
function _nbaPerfectParlayLine(p){
 return p.historical_replay?p.line:(p.dk_line!=null?p.dk_line:p.fd_line);
}
function _nbaPerfectParlayExactKey(p){
 var line=_nbaPerfectParlayLine(p),side=_nbaTopSideOf(p);
 if(line==null||!isFinite(Number(line))||!['OVER','UNDER'].includes(side))return '';
 return String(window.__NBA_BOARD_DATE__||'')+'|'+_nbaPerfectParlayBoardKey(p)+'|'+side+'|'+Number(line);
}
function _nbaPerfectParlayStarter(x){
 var mpg=Number(x&&x._board_mpg);
 return isFinite(mpg)&&mpg>=24;
}
function changeNbaPerfectParlayLeg(legIndex){
 if(!_nbaPerfectParlayDateCurrent())return;
 var legs=__nbaPerfectParlayLegs||[],pool=__nbaPerfectParlayPool||[],current=legs[legIndex];
 if(!current||!pool.length)return;
 var used={};
 legs.forEach(function(x,i){if(i!==legIndex)used[_nbaPerfectParlayPlayerKey(x)]=1;});
 var currentKey=_nbaPerfectParlayPlayerKey(current),start=pool.findIndex(function(x){return _nbaPerfectParlayPlayerKey(x)===currentKey});
 for(var step=1;step<=pool.length;step++){
  var next=pool[(Math.max(start,0)+step)%pool.length],nextKey=_nbaPerfectParlayPlayerKey(next);
  if(nextKey!==currentKey&&!used[nextKey]){
   legs[legIndex]=next;
   __nbaPerfectParlayNotice='Leg replaced. Odds and estimated payout updated.';
   renderNbaPerfectParlay();
   return;
  }
 }
}
function _nbaPerfectParlayDateCurrent(){
 var dp=document.getElementById('datePicker');
 if(__nbaPerfectParlayBoardSource===allPicksData&&__nbaPerfectParlayDate&&__nbaPerfectParlayDate===String(window.__NBA_BOARD_DATE__||'')&&(!dp||dp.value===__nbaPerfectParlayDate))return true;
 var note=document.getElementById('nbaParlayNotice');
 if(note)note.textContent='The date or loaded board changed. Build a new parlay from the current app picks first.';
 return false;
}
function generateNewNbaPerfectParlay(){
 if(!_nbaPerfectParlayDateCurrent())return;
 var legs=__nbaPerfectParlayLegs||[],pool=__nbaPerfectParlayPool||[],used={};
 legs.forEach(function(x){used[_nbaPerfectParlayPlayerKey(x)]=true;});
 var fresh=pool.filter(function(x){return !used[_nbaPerfectParlayPlayerKey(x)];});
 if(!fresh.length){
  __nbaPerfectParlayNotice='No other qualifying players in this pool. Edit your categories, side, or leg count for more options.';
  renderNbaPerfectParlay();return;
 }
 var next=fresh.slice(0,legs.length);
 // Keep only the minimum necessary overlap if the approved pool is small.
 var kept=legs.slice(1).concat(legs.slice(0,1));
 next=next.concat(kept.slice(0,legs.length-next.length));
 var replacements=Math.min(fresh.length,legs.length);
 __nbaPerfectParlayLegs=next;
 __nbaPerfectParlayNotice=replacements===legs.length?'New parlay: every player replaced. Same filters, same approved pool.':
  replacements+' new player'+(replacements===1?'':'s')+' included; '+(legs.length-replacements)+' retained because this pool has only '+pool.length+' qualifying players.';
 renderNbaPerfectParlay();
}
function updateNbaPerfectParlayStake(input){
 var stake=Number(input.value),valid=input.value.trim()!==''&&isFinite(stake)&&stake>0;
 input.setCustomValidity(valid?'':'Enter a stake greater than zero.');
 var combined=1;
 (__nbaPerfectParlayLegs||[]).forEach(function(x){combined*=Number(_amToDec(x.odds)||1);});
 if(valid)__nbaPerfectParlayStake=stake;
 var profit=document.getElementById('nbaParlayProfit'),total=document.getElementById('nbaParlayReturn');
 if(profit)profit.textContent=valid?'$'+(stake*(combined-1)).toFixed(2):'—';
 if(total)total.textContent=valid?'$'+(stake*combined).toFixed(2):'—';
}
function renderNbaPerfectParlay(){
 var legs=__nbaPerfectParlayLegs||[],requested=legs.length;
 if(!requested)return;
 var combined=1,hundredCount=0,used={};
 legs.forEach(function(x){combined*=Number(_amToDec(x.odds)||1);if(Number(x.model_probability)>=.9995)hundredCount++;used[_nbaPerfectParlayPlayerKey(x)]=1;});
 var exampleStake=__nbaPerfectParlayStake,exampleProfit=exampleStake*(combined-1),exampleReturn=exampleStake*combined;
 var freshCount=__nbaPerfectParlayPool.filter(function(x){return !used[_nbaPerfectParlayPlayerKey(x)];}).length;
 var actionStyle='border-radius:8px;padding:11px 15px;font-weight:900;cursor:pointer;';
 var newButton='<button onclick="generateNewNbaPerfectParlay()" '+(!freshCount?'disabled ':'')+'style="'+actionStyle+'background:linear-gradient(135deg,#d97706,#7c3aed);color:#fff;border:1px solid #a78bfa;'+(!freshCount?'opacity:.5;cursor:not-allowed;':'')+'">&#8635; Generate New</button>';
 __nbaCoachRows=legs.slice();
 var rows=legs.map(function(x,i){
  var exact=Number(x.model_probability)>=.9995,currentKey=_nbaPerfectParlayPlayerKey(x);
  var canSwap=__nbaPerfectParlayPool.some(function(alt){var key=_nbaPerfectParlayPlayerKey(alt);return key!==currentKey&&!used[key]});
  return '<tr>'+
   '<td>'+(i+1)+'</td>'+
   '<td onclick="nbaCoachDetail('+i+')" style="cursor:pointer"><b style="color:#fff;text-decoration:underline;text-decoration-style:dotted;text-underline-offset:2px">'+_nbaEsc(x.player)+'</b><br><span style="color:#64748b">'+_nbaEsc(x.team||'NBA')+(exact?' · 100% APP PLAY':'')+'</span></td>'+
   '<td>'+_nbaEsc(x.category)+'<br><b style="color:'+(x.side==='OVER'?'#4ade80':'#f87171')+'">'+_nbaEsc(x.side)+' '+_nbaEsc(String(x.line))+'</b></td>'+
   '<td>'+_nbaEsc((Number(x.odds)>0?'+':'')+String(x.odds))+'<br><span style="color:#64748b;font-size:.6rem">'+_nbaEsc(x.source||x.book||'Sportsbook')+'</span></td>'+
   '<td style="color:'+(exact?'#fbbf24':'#e5e7eb')+';font-weight:'+(exact?'900':'700')+'">'+Number(x.model_probability*100).toFixed(1)+'%</td>'+
   '<td style="color:#4ade80!important;font-weight:900">+'+Number(x.edge*100).toFixed(2)+' pts</td>'+
   '<td onclick="event.stopPropagation()" style="text-align:center">'+(canSwap?'<button aria-label="Change '+_nbaEsc(x.player)+' leg" title="Change this leg" onclick="event.stopPropagation();changeNbaPerfectParlayLeg('+i+')" style="width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;background:#1e3a8a;color:#bfdbfe;border:1px solid #3b82f6;border-radius:50%;padding:0;font-size:1rem;font-weight:900;line-height:1;cursor:pointer;box-shadow:0 2px 8px rgba(37,99,235,.25)">&#8635;</button>':'<span title="No unused qualifying alternatives" style="display:inline-flex;width:32px;height:32px;align-items:center;justify-content:center;color:#475569;border:1px solid #1e293b;border-radius:50%;font-size:.75rem">&#8212;</span>')+'</td></tr>';
 }).join('');
 var hundredNote=hundredCount
  ?hundredCount+' exact 100% app-probability play'+(hundredCount===1?' is':'s are')+' included.'
  :'No exact 100% Coach play is included; these legs use positive Coach Edge.';
  var settings=__nbaPerfectParlaySettings||{},catLabels=__nbaPerfectParlayCategories.filter(function(cat){return (settings.categories||[]).indexOf(cat.key)>=0;}).map(function(cat){return cat.label;});
  var filterNote=(catLabels.length===__nbaPerfectParlayCategories.length?'All categories':catLabels.join(', '))+' · '+(settings.side==='ALL'?'Best available side':settings.side+' only')+' · '+(settings.rotation==='ONLY'?'24+ MPG only':(settings.rotation==='ANY'?'Any qualifying player':'Prefer 24+ MPG'));
 _nbaPerfectParlayCommit(
  '<div><div style="display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap"><div class="nba-coach-question">&#10024; Perfect Parlay · '+requested+' Legs</div>'+newButton+'</div>'+
  '<div style="display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap;margin-top:11px;padding:10px 12px;background:linear-gradient(135deg,rgba(180,83,9,.18),rgba(124,58,237,.18));border:1px solid rgba(251,191,36,.35);border-radius:10px">'+
    '<div style="color:#e5e7eb;font-size:.74rem;line-height:1.5">'+hundredNote+' Every leg comes from this date’s displayed app boards. Use Change Leg to cycle through the remaining approved plays.<br><span style="color:#a5b4fc;font-weight:800">'+_nbaEsc(filterNote)+'</span></div>'+
    '<div style="display:flex;flex-wrap:wrap;gap:18px;align-items:center;width:100%;padding-top:12px;border-top:1px solid #475569">'+
    '<div><div style="color:#94a3b8;font-size:.65rem">COMBINED ODDS</div><b style="color:#fbbf24;font-size:1.3rem">'+_nbaPerfectParlayAmerican(combined)+'</b><div style="color:#94a3b8;font-size:.65rem">'+combined.toFixed(2)+' decimal</div></div>'+
    '<label style="color:#cbd5e1;font-size:.7rem">BET AMOUNT ($)<br><input aria-label="Parlay stake in dollars" type="number" min="0.01" step="0.01" value="'+exampleStake+'" oninput="updateNbaPerfectParlayStake(this)" style="margin-top:5px;width:105px;padding:9px;background:#020617;border:1px solid #64748b;border-radius:7px;color:white;font-size:1rem"></label>'+
    '<div><div style="color:#94a3b8;font-size:.65rem">POTENTIAL PROFIT</div><b id="nbaParlayProfit" style="color:#4ade80;font-size:1.3rem">$'+exampleProfit.toFixed(2)+'</b></div>'+
    '<div><div style="color:#94a3b8;font-size:.65rem">TOTAL RETURN · INCLUDES STAKE</div><b id="nbaParlayReturn" style="color:#fff;font-size:1.3rem">$'+exampleReturn.toFixed(2)+'</b></div></div>'+
  '</div>'+
  '<div id="nbaParlayNotice" role="status" aria-live="polite" style="margin:10px 0;color:#cbd5e1;font-size:.72rem">'+_nbaEsc(__nbaPerfectParlayNotice||(__nbaPerfectParlayPool.length+' qualifying players · '+freshCount+' unused alternatives. Generate New keeps your current filters.'))+'</div>'+
  '<div class="nba-coach-table-wrap"><table class="nba-coach-table" style="min-width:930px"><thead><tr><th>#</th><th>Player</th><th>Play</th><th>Odds</th><th>App Prob</th><th>Coach Edge</th><th>Change Leg</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+
   '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px">'+newButton+'<button onclick="showNbaPerfectParlayBuilder()" style="background:#1e293b;color:#fff;border:1px solid #475569;'+actionStyle+'">Edit parlay options</button></div><div style="margin-top:10px;color:#94a3b8;font-size:.65rem;line-height:1.5">Estimated payout if every leg wins. Prices may come from different books; your sportsbook may offer a different combined price. App percentages are not guarantees.</div></div>');
}
function buildNbaPerfectParlay(){
 var select=document.getElementById('nbaPerfectParlayLegs');
 var requested=Math.max(2,Math.min(10,parseInt((select&&select.value)||'3',10)||3));
 var selectedCategories=Array.prototype.slice.call(document.querySelectorAll('input[name="nbaPerfectParlayCat"]:checked')).map(function(el){return el.value;});
 var sideEl=document.getElementById('nbaPerfectParlaySide'),rotationEl=document.getElementById('nbaPerfectParlayRotation');
 var selectedSide=(sideEl&&sideEl.value)||'ALL',rotationMode=(rotationEl&&rotationEl.value)||'PREFER';
 __nbaPerfectParlaySettings={legs:requested,categories:selectedCategories.slice(),side:selectedSide,rotation:rotationMode};
 var msg=document.getElementById('nbaCoachMsg'),box=document.getElementById('nbaCoachResults');
 var tok=localStorage.getItem('__mpa_token')||'',dp=document.getElementById('datePicker'),ds=(dp&&dp.value)||'__TODAY__';
 if(!selectedCategories.length){
  _nbaPerfectParlayCommit('<div><div class="nba-coach-question">&#10024; Perfect Parlay</div><div style="margin-top:11px;color:#fbbf24;font-size:.78rem;line-height:1.5">Select at least one category before building the parlay.</div><button onclick="showNbaPerfectParlayBuilder()" style="margin-top:12px;background:#1e293b;color:#fff;border:1px solid #475569;border-radius:7px;padding:8px 11px;font-weight:800;cursor:pointer">Edit parlay options</button></div>');
  return;
 }
 if(!window.__NBA_BOARD_DATE__||String(window.__NBA_BOARD_DATE__)!==String(ds)){
  _nbaPerfectParlayCommit('<div><div class="nba-coach-question">&#10024; Perfect Parlay · '+requested+' Legs</div><div style="margin-top:11px;color:#fbbf24;font-size:.78rem;line-height:1.5">Load the app picks for '+_nbaEsc(ds)+' first. Perfect Parlay only uses the displayed boards from the same selected date.</div></div>');
  return;
 }
 var boardByKey={};
  _nbaPerfectParlayBoardRows(selectedSide).forEach(function(p){
  if(selectedCategories.indexOf(String((p&&p.stat)||'').toUpperCase())<0)return;
  var key=_nbaPerfectParlayBoardKey(p);
  if(!key||key==='|')return;
  var old=boardByKey[key];
  if(!old||Number(p.pct||0)>Number(old.pct||0))boardByKey[key]=p;
 });
 var boardKeys=Object.keys(boardByKey);
 var hcRows=_nbaHcParlayRows(selectedCategories,selectedSide);
 if(!boardKeys.length&&!hcRows.length){
  _nbaPerfectParlayCommit('<div><div class="nba-coach-question">&#10024; Perfect Parlay · '+requested+' Legs</div><div style="margin-top:11px;color:#fbbf24;font-size:.78rem;line-height:1.5">This date has no displayed app-board picks available for Perfect Parlay.</div></div>');
  return;
 }
 try{
  var raw=_nbaPerfectParlayLocalRows(boardByKey).concat(hcRows);
  var byPlayer={};
  raw.forEach(function(x){
   var key=String(x.player||'').toLowerCase().replace(/[^a-z0-9]/g,'');
   if(!key)return;
   var old=byPlayer[key];
   if(!old
      ||Number(x.model_probability)>Number(old.model_probability)
      ||(Number(x.model_probability)===Number(old.model_probability)&&_nbaPerfectParlayStarter(x)&&!_nbaPerfectParlayStarter(old))
      ||(Number(x.model_probability)===Number(old.model_probability)&&_nbaPerfectParlayStarter(x)===_nbaPerfectParlayStarter(old)&&Number(x.edge)>Number(old.edge)))
    byPlayer[key]=x;
  });
  var pool=Object.keys(byPlayer).map(function(k){return byPlayer[k];});
  pool.sort(function(a,b){
   return Number(b.model_probability)-Number(a.model_probability)
     ||(rotationMode==='ANY'?0:(Number(_nbaPerfectParlayStarter(b))-Number(_nbaPerfectParlayStarter(a))))
     ||(rotationMode==='ANY'?0:(Number(b._board_mpg||0)-Number(a._board_mpg||0)))
    ||Number(b.edge)-Number(a.edge)
    ||String(a.player).localeCompare(String(b.player));
  });
  var starters=pool.filter(_nbaPerfectParlayStarter);
  var depth=pool.filter(function(x){return !_nbaPerfectParlayStarter(x);});
  pool=rotationMode==='ONLY'?starters:(rotationMode==='PREFER'?(starters.length>=requested?starters:starters.concat(depth)):pool);
  if(pool.length<requested){
   _nbaPerfectParlayCommit('<div><div class="nba-coach-question">&#10024; Perfect Parlay · '+requested+' Legs</div><div style="margin-top:11px;color:#cbd5e1;font-size:.78rem;line-height:1.5">Only '+pool.length+' unique player'+(pool.length===1?'':'s')+' from this date’s displayed app boards currently qualify with a genuine standard-line sportsbook price and positive Coach Edge. Choose fewer legs.</div><div style="margin-top:12px"><button onclick="showNbaPerfectParlayBuilder()" style="background:#1e293b;color:#fff;border:1px solid #475569;border-radius:7px;padding:8px 11px;font-weight:800;cursor:pointer">Choose another size</button></div></div>');
   return;
  }
   __nbaPerfectParlayPool=pool.slice();
   __nbaPerfectParlayLegs=pool.slice(0,requested);
   __nbaPerfectParlayDate=String(ds);
   __nbaPerfectParlayBoardSource=allPicksData;
   __nbaPerfectParlayNotice='';
   renderNbaPerfectParlay();
 }catch(e){
  _nbaPerfectParlayCommit('<div><div class="nba-coach-question">&#10024; Perfect Parlay</div><div style="margin-top:11px;color:#f87171;font-size:.78rem">'+_nbaEsc(e.message||'Perfect Parlay unavailable.')+'</div><button onclick="showNbaPerfectParlayBuilder()" style="margin-top:12px;background:#1e293b;color:#fff;border:1px solid #475569;border-radius:7px;padding:8px 11px;font-weight:800;cursor:pointer">Edit parlay options</button></div>');
 }
}
async function nbaCoachSearch(forceMode,presetQuery){
  var question=document.getElementById('nbaCoachQuery').value||'';
   var q=presetQuery||question, mode=forceMode||(
     /best\s*[-–]\s*alternate|minus|favorite|negative\s+odds/i.test(q)?'alternate_minus':
     (/best\s*\+\s*alternate|plus|underdog|positive\s+odds/i.test(q)?'alternate_plus':
     (/\balternate\b|\balt\b/i.test(q)?'alternate':'all')));
  var msg=document.getElementById('nbaCoachMsg'), box=document.getElementById('nbaCoachResults');
  if(!String(question).trim()){document.getElementById('nbaCoachQuery').focus();return;}
  msg.textContent='Analyzing loaded NBA props…'; box.style.display='none';box.innerHTML='';
  var tok=localStorage.getItem('__mpa_token')||'', dp=document.getElementById('datePicker'), ds=(dp&&dp.value)||'__TODAY__';
   var controller=window.AbortController?new AbortController():null;
   var timer=controller?setTimeout(function(){controller.abort();},40000):null;
   try{
     var r=await fetch('/api/nba/coach-edge?_tok='+encodeURIComponent(tok),{method:'POST',headers:{'Content-Type':'application/json'},signal:controller?controller.signal:undefined,body:JSON.stringify({date:ds,query:q,mode:mode,count:10})});
    var d=await r.json(); if(!r.ok) throw new Error(d.detail||'Coach Edge unavailable');
    __nbaCoachRows=(d.results||[]).slice(0,10); msg.textContent='';
    var questionHtml='<div class="nba-coach-question">'+_nbaEsc(question)+'</div>';
    if(!__nbaCoachRows.length){
      box.innerHTML=questionHtml+'<div style="margin-top:11px;color:#cbd5e1;font-size:.78rem;line-height:1.5">'+_nbaEsc(d.message||'No loaded NBA prop matched that request with a genuine sportsbook price and positive Coach Edge.')+'</div>';
      box.style.display='block';return;
    }
    var isSafe=mode==='safest';
    var rows=__nbaCoachRows.map(function(x,i){
      var noEdge=x.edge==null,e=Number(x.edge*100),moved=x.base_line!=null&&Number(x.base_line)!==Number(x.line);
      var formTxt=x.form_category?'<br><span style="color:'+(x.form==='HOT'?'#fb923c':'#7dd3fc')+';font-size:.6rem;font-weight:900">'+_nbaEsc(x.form_category)+(x.double_cold?' · DOUBLE COLD':'')+' · '+_nbaEsc(String(x.recent_hits))+'/'+_nbaEsc(String(x.recent_total))+' L5</span>':'';
      var fromTxt=x.base_line!=null?'<br><span style="color:#64748b;font-size:.62rem">'+(moved?'from book '+_nbaEsc(String(x.base_line))+' · ':'book line · ')+_nbaEsc(String(x.base_hits))+'/'+_nbaEsc(String(x.games))+'</span>':'';
      return '<tr tabindex="0" onclick="nbaCoachDetail('+i+')" onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();nbaCoachDetail('+i+')}">'+
       '<td>'+(i+1)+'</td><td><b style="color:#fff;text-decoration:underline;text-decoration-style:dotted;text-underline-offset:2px">'+_nbaEsc(x.player)+'</b><br><span style="color:#64748b">'+_nbaEsc(x.team||'NBA')+(x.alternate?' · ALTERNATE':'')+'</span></td>'+
       '<td>'+_nbaEsc(x.category)+'<br><b style="color:'+(x.side==='OVER'?'#4ade80':'#f87171')+'">'+_nbaEsc(x.side)+' '+_nbaEsc(String(x.line))+'</b>'+fromTxt+formTxt+'</td>'+
       '<td>'+_nbaEsc((Number(x.odds)>0?'+':'')+String(x.odds))+'<br><span style="color:#64748b;font-size:.6rem">'+_nbaEsc(x.source||'Sportsbook')+'</span></td>'+
       '<td'+(isSafe&&Number(x.model_probability)>=.9995?' style="color:#fbbf24!important;font-weight:900"':'')+'>'+(x.model_probability==null?'—':Number(x.model_probability*100).toFixed(1)+'%')+(isSafe&&x.games?'<br><span style="color:#64748b;font-size:.6rem">'+_nbaEsc(String(x.hits))+'/'+_nbaEsc(String(x.games))+'</span>':'')+'</td><td>'+Number(x.implied_probability*100).toFixed(1)+'%</td>'+
       '<td style="color:'+(noEdge?'#64748b':(e>=0?'#4ade80':'#f87171'))+'!important;font-weight:700">'+(noEdge?'—':(e>=0?'+':'')+e.toFixed(2)+' pts')+'</td></tr>';
    }).join('');
    var summary='<div style="margin-top:11px;color:#e5e7eb;font-size:.76rem;line-height:1.5">'+_nbaEsc(d.message||(isSafe
      ?('I found '+__nbaCoachRows.length+' players who hit the book line 80%+ against today’s opponent at this venue, then moved each to the safer line with the best hit rate. Ranked by app hit rate.')
      :('I checked the loaded NBA board and ranked '+__nbaCoachRows.length+' matching positive-edge plays.')))+(isSafe?' App % is the raw hit rate in those games, not a guarantee.':' Probability edge is shown in percentage points, not traditional expected ROI.')+'</div>';
    box.innerHTML=questionHtml+summary+'<div class="nba-coach-table-wrap"><table class="nba-coach-table"><thead><tr><th>#</th><th>Player</th><th>Play</th><th>Odds</th><th>'+(isSafe?'App Hit Rate':'App Prob')+'</th><th>Implied</th><th>'+(isSafe?'Edge':'Coach Edge')+'</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
    box.style.display='block';
   }catch(e){
     var text=e&&e.name==='AbortError'?'Coach Edge timed out while warming archived data.':(e.message||'Coach Edge unavailable.');
     msg.textContent=text;
     box.innerHTML='<button id="nbaCoachRetry" class="btn" style="margin-top:10px;background:#92400e;color:#fff">Retry</button>';
     var retry=document.getElementById('nbaCoachRetry');
     if(retry) retry.addEventListener('click',function(){nbaCoachSearch(mode,q);});
     box.style.display='block';
   }finally{if(timer)clearTimeout(timer);}
}
function openNbaCoachTrack(){
 var panel=document.getElementById('nbaCoachTrackPanel');if(!panel)return;
 panel.style.display='block';
 panel.scrollIntoView({behavior:'smooth',block:'start'});
 loadNbaCoachTrackRecord();
}
function nbaCoachDetail(i){
 var x=__nbaCoachRows[i], k='coach'+i, dp=document.getElementById('datePicker');
 if(!x)return;
 window.__NBA_BET_SRC__=window.__NBA_BET_SRC__||{};
 window.__NBA_BET_SRC__[k]={name:x.player,team:x.team||'',opp:'',category:x.category,side:x.side,stat_key:x.stat,stat_label:x.category,line:x.line,odds:x.odds,date:(dp&&dp.value)||'__TODAY__'};
 window.__NBA_COACH_BET_KEY__=k;
 _nbaCloseCoachDetail();
 var ov=document.createElement('div');
 ov.id='nba-coach-detail-modal';
 ov.setAttribute('role','dialog');
 ov.setAttribute('aria-modal','true');
 ov.setAttribute('aria-label','Coach Edge details for '+x.player);
 ov.style.cssText='position:fixed;inset:0;background:rgba(2,6,23,.84);z-index:10020;display:flex;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(4px);animation:nbaCoachFadeIn .16s ease-out';
 ov.onclick=function(e){if(e.target===ov)_nbaCloseCoachDetail();};
 var sideColor=x.side==='OVER'?'#4ade80':'#f87171';
 var lineNum=Number(x.line);
 var _isHit=function(v){var n=Number(v);if(!isFinite(n)||!isFinite(lineNum))return null;return x.side==='UNDER'?n<lineNum:n>lineNum;};
 var _chips=function(list){
  if(!list.length)return '<span style="color:#64748b;font-size:.78rem">No qualifying games</span>';
  return list.map(function(v){var h=_isHit(v),c=h==null?'#94a3b8':(h?'#4ade80':'#f87171');
   return '<span style="min-width:30px;text-align:center;font-size:.8rem;font-weight:900;padding:4px 7px;border-radius:7px;color:'+c+';border:1px solid '+c+'55;background:'+c+'14">'+_nbaEsc(String(v))+'</span>';}).join('');
 };
 var games=(x.game_log||[]).filter(function(v){return v!=null&&v!=='';});
 var oppList=Array.isArray(x.opponent_history)?x.opponent_history:String(x.opponent_history||'').split(',').map(function(v){return v.trim();}).filter(function(v){return v&&v!=='—';});
 var gHits=games.filter(function(v){return _isHit(v)===true;}).length;
 var oHits=oppList.filter(function(v){return _isHit(v)===true;}).length;
 var board=null;
 try{if(typeof allPicksData!=='undefined'&&allPicksData){var nm=String(x.player||'').toLowerCase();board=allPicksData.find(function(p){return String(p.player||'').toLowerCase()===nm;})||null;}}catch(e){}
 var pid=board&&board.player_id;
 var head=(board&&board.headshot)||(pid?'https://a.espncdn.com/i/headshots/nba/players/full/'+pid+'.png':'');
 var team=String(x.team||(board&&board.team)||'');
 var logo=team?'https://a.espncdn.com/i/teamlogos/nba/500/'+team.toLowerCase()+'.png':'';
 var ini=String(x.player||'').split(/ +/).map(function(w){return w.charAt(0);}).join('').slice(0,2).toUpperCase();
 var tip=board&&board.tipoff?new Date(board.tipoff).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',timeZoneName:'short'}):'';
 var meta=_nbaEsc(team||'NBA')+(board&&board.opp?' vs '+_nbaEsc(board.opp):'')+(board&&board.location?' · '+_nbaEsc(String(board.location).toUpperCase()):'')+(tip?' · '+_nbaEsc(tip):'');
 var model=Math.max(0,Math.min(1,Number(x.model_probability)||0)),implied=Math.max(0,Math.min(1,Number(x.implied_probability)||0));
 var oddsTxt=(Number(x.odds)>0?'+':'')+String(x.odds);
 var tileCss='background:#0f172a;border:1px solid #1e293b;border-radius:11px;padding:10px;text-align:center';
 var kCss='color:#64748b;font-size:.6rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase';
 var secCss='margin-top:12px;background:#0b1220;border:1px solid #1e293b;border-radius:12px;padding:12px 14px';
 ov.innerHTML='<style>@keyframes nbaCoachFadeIn{from{opacity:0}to{opacity:1}}@keyframes nbaCoachSlideIn{from{opacity:0;transform:translateY(12px) scale(.985)}to{opacity:1;transform:translateY(0) scale(1)}}</style>'+
 '<div style="background:linear-gradient(170deg,#111827,#0a0f1c);border:1px solid #334155;border-top:3px solid '+sideColor+';border-radius:18px;max-width:600px;width:100%;max-height:90vh;overflow:auto;box-shadow:0 24px 80px rgba(0,0,0,.7);animation:nbaCoachSlideIn .18s ease-out">'+
 '<div style="display:flex;align-items:center;gap:14px;padding:18px 18px 14px;background:radial-gradient(ellipse at top left,rgba(99,102,241,.18),transparent 65%)">'+
  '<div style="position:relative;width:68px;height:68px;flex:0 0 auto;border-radius:50%;background:radial-gradient(circle at 50% 30%,#2a3445,#141922);border:2px solid '+sideColor+';display:flex;align-items:center;justify-content:center">'+
   '<span style="font-weight:900;font-size:1.25rem;color:#94a3b8">'+_nbaEsc(ini)+'</span>'+
   (head?'<img src="'+head+'" alt="" onerror="this.style.display=&#39;none&#39;" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;border-radius:50%">':'')+
   (logo?'<img src="'+logo+'" alt="" onerror="this.style.display=&#39;none&#39;" style="position:absolute;right:-5px;bottom:-4px;width:27px;height:27px;background:#0b1220;border:1px solid #334155;border-radius:50%;padding:2px;object-fit:contain">':'')+
  '</div>'+
  '<div style="flex:1;min-width:0"><div style="color:#fff;font-size:1.22rem;font-weight:900;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+_nbaEsc(x.player)+'</div>'+
   '<div style="color:#94a3b8;font-size:.78rem;font-weight:700;margin-top:4px">'+meta+'</div>'+
   '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:7px"><span style="font-size:.66rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:#a5b4fc;border:1px solid rgba(165,180,252,.45);background:rgba(99,102,241,.12);padding:3px 8px;border-radius:6px">'+_nbaEsc(x.category)+'</span>'+
   (x.alternate?'<span style="font-size:.66rem;font-weight:900;letter-spacing:.08em;color:#fbbf24;border:1px solid rgba(251,191,36,.45);background:rgba(251,191,36,.1);padding:3px 8px;border-radius:6px">GENUINE ALTERNATE</span>':'')+'</div></div>'+
  '<button onclick="_nbaCloseCoachDetail()" aria-label="Close Coach details" style="align-self:flex-start;background:#1e293b;border:0;color:#cbd5e1;width:34px;height:34px;border-radius:9px;cursor:pointer;font-size:1.1rem;flex-shrink:0">&#215;</button></div>'+
 '<div style="padding:4px 18px 18px">'+
 '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;border:1px solid '+sideColor+'55;background:linear-gradient(135deg,'+sideColor+'1c,rgba(255,255,255,.02));border-radius:14px;padding:13px 15px">'+
  '<div><div style="color:#64748b;font-size:.6rem;font-weight:900;letter-spacing:.1em">THE PLAY</div>'+
  '<div style="color:'+sideColor+';font-size:1.45rem;font-weight:900;line-height:1.15">'+_nbaEsc(x.side)+' '+_nbaEsc(String(x.line))+'</div>'+
  '<div style="color:#94a3b8;font-size:.74rem;margin-top:2px"><b style="color:#fbbf24;font-family:monospace;font-size:.92rem">'+_nbaEsc(oddsTxt)+'</b> · '+_nbaEsc(x.source||'Sportsbook line')+'</div></div>'+
  '<div style="text-align:center;background:'+(x.edge>=0?'rgba(74,222,128,.1)':'rgba(248,113,113,.1)')+';border:1px solid '+(x.edge>=0?'rgba(74,222,128,.35)':'rgba(248,113,113,.35)')+';border-radius:12px;padding:8px 12px"><div style="color:'+(x.edge>=0?'#4ade80':'#f87171')+';font-size:1.35rem;font-weight:900;line-height:1">'+(x.edge==null?'—':(x.edge>=0?'+':'')+(x.edge*100).toFixed(1)+'%')+'</div><div style="color:#94a3b8;font-size:.58rem;font-weight:900;letter-spacing:.1em;margin-top:3px">'+(x.safest?'EDGE':'COACH EDGE')+'</div></div></div>'+
 (x.base_line!=null?'<div style="display:flex;align-items:center;gap:10px;margin-top:12px;background:#0b1220;border:1px solid #1e293b;border-radius:12px;padding:11px 14px">'+
  '<div style="flex:1;text-align:center"><div style="color:#64748b;font-size:.6rem;font-weight:900;letter-spacing:.08em">BOOK LINE</div><div style="color:#cbd5e1;font-weight:900;font-size:1.05rem;margin-top:2px">'+_nbaEsc(x.side)+' '+_nbaEsc(String(x.base_line))+'</div><div style="color:#94a3b8;font-size:.72rem">'+_nbaEsc(String(x.base_hits))+'/'+_nbaEsc(String(x.games))+' · '+Math.round(Number(x.base_hit_rate)*100)+'%</div></div>'+
  '<div style="color:'+sideColor+';font-size:1.3rem;font-weight:900">&#8594;</div>'+
  '<div style="flex:1;text-align:center"><div style="color:#64748b;font-size:.6rem;font-weight:900;letter-spacing:.08em">SAFER LINE</div><div style="color:'+sideColor+';font-weight:900;font-size:1.05rem;margin-top:2px">'+_nbaEsc(x.side)+' '+_nbaEsc(String(x.line))+'</div><div style="color:'+(Number(x.model_probability)>=.9995?'#fbbf24':'#94a3b8')+';font-size:.72rem;font-weight:800">'+_nbaEsc(String(x.hits))+'/'+_nbaEsc(String(x.games))+' · '+Math.round(Number(x.model_probability)*100)+'%</div></div></div>':'')+
 '<div style="'+secCss+'"><div style="display:flex;justify-content:space-between;font-size:.7rem;font-weight:800;margin-bottom:7px"><span style="color:#a5b4fc">'+(x.safest?'APP HIT RATE ':'MODEL ')+(x.model_probability==null?'— (no same-line app estimate)':(model*100).toFixed(1)+'%')+'</span><span style="color:#94a3b8">BOOK IMPLIED '+(implied*100).toFixed(1)+'%</span></div>'+
  '<div style="position:relative;height:12px;background:#1e293b;border-radius:999px;overflow:visible">'+
   '<div style="position:absolute;left:0;top:0;bottom:0;width:'+(model*100).toFixed(1)+'%;background:linear-gradient(90deg,#4338ca,#818cf8);border-radius:999px"></div>'+
   '<div title="Book implied" style="position:absolute;top:-4px;bottom:-4px;left:calc('+(implied*100).toFixed(1)+'% - 1px);width:3px;background:#f8fafc;border-radius:2px;box-shadow:0 0 6px rgba(255,255,255,.6)"></div>'+
  '</div><div style="color:#64748b;font-size:.66rem;margin-top:7px">White marker = sportsbook break-even. Bar past the marker = edge.</div></div>'+
 '<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:12px">'+
  '<div style="'+tileCss+'"><div style="'+kCss+'">Recent avg</div><div style="color:#e2e8f0;font-weight:900;font-size:1.05rem;margin-top:3px">'+_nbaEsc(String(x.recent_average==null?'—':x.recent_average))+'</div></div>'+
  '<div style="'+tileCss+'"><div style="'+kCss+'">Log vs line</div><div style="color:#e2e8f0;font-weight:900;font-size:1.05rem;margin-top:3px">'+(games.length?gHits+'/'+games.length:'—')+'</div></div>'+
  '<div style="'+tileCss+'"><div style="'+kCss+'">Vs opponent</div><div style="color:#e2e8f0;font-weight:900;font-size:1.05rem;margin-top:3px">'+(oppList.length?oHits+'/'+oppList.length:'—')+'</div></div></div>'+
 '<div style="'+secCss+'"><div style="color:#c4b5fd;font-size:.66rem;font-weight:900;letter-spacing:.08em;margin-bottom:8px">GAME LOG · '+_nbaEsc(x.side)+' '+_nbaEsc(String(x.line))+'</div><div style="display:flex;flex-wrap:wrap;gap:5px">'+_chips(games)+'</div>'+
  '<div style="color:#c4b5fd;font-size:.66rem;font-weight:900;letter-spacing:.08em;margin:12px 0 8px">OPPONENT HISTORY</div><div style="display:flex;flex-wrap:wrap;gap:5px">'+(oppList.length?_chips(oppList):'<span style="color:#64748b;font-size:.78rem">'+_nbaEsc(String(x.opponent_history||'No qualifying opponent history'))+'</span>')+'</div>'+
  '<div style="color:#64748b;font-size:.64rem;margin-top:9px"><span style="color:#4ade80">Green</span> = would have cashed · <span style="color:#f87171">Red</span> = missed</div></div>'+
 '<div style="margin-top:10px;color:#94a3b8;font-size:.76rem;line-height:1.5">'+_nbaEsc(x.selection_reason||'Positive Coach Edge based on opponent history and the exact sportsbook price.')+'</div>'+
 '<button onclick="_nbaCoachOpenBet()" style="width:100%;margin-top:14px;padding:12px 14px;background:linear-gradient(90deg,#4338ca,#6366f1);color:#fff;border:0;border-radius:11px;font-weight:900;cursor:pointer;letter-spacing:.02em">Open exact bet form</button>'+
 '</div></div>';
 document.body.appendChild(ov);
 document.body.style.overflow='hidden';
}
function _nbaCloseCoachDetail(){
 var ov=document.getElementById('nba-coach-detail-modal');
 if(ov)ov.remove();
 document.body.style.overflow='';
}
function _nbaCoachOpenBet(){
 var key=window.__NBA_COACH_BET_KEY__;
 _nbaCloseCoachDetail();
 if(key)_nbaBetForm(key);
}
document.addEventListener('keydown',function(e){
 if(e.key==='Escape'&&document.getElementById('nba-coach-detail-modal'))_nbaCloseCoachDetail();
});
var _nbaCoachTrackData=null;
var _nbaCoachTrackView='category';
function setNbaCoachTrackView(view){
 _nbaCoachTrackView=view==='full'?'full':'category';
 var c=document.getElementById('nbaCoachCatBtn'),f=document.getElementById('nbaCoachFullBtn');
 if(c)c.classList.toggle('active',_nbaCoachTrackView==='category');
 if(f)f.classList.toggle('active',_nbaCoachTrackView==='full');
 renderNbaCoachTrackRecord();
}
async function loadNbaCoachTrackRecord(){
  var msg=document.getElementById('nbaCoachTrackMsg'),box=document.getElementById('nbaCoachTrackResults');
  if(msg) msg.textContent='Loading Coach Track Record…';
  try{
    var tok=localStorage.getItem('__mpa_token')||'';
    var source=(document.getElementById('nbaCoachTrkSource')||{}).value||'official';
    var ds=(document.getElementById('nbaCoachTrkDate')||{}).value||'';
    var r=await fetch('/api/nba/coach-track-record?_tok='+encodeURIComponent(tok)+'&source='+encodeURIComponent(source)+'&date_str='+encodeURIComponent(ds));
    var d=await r.json(); if(!r.ok) throw new Error(d.detail||'Track Record unavailable');
    _nbaCoachTrackData=d;
    if(msg) msg.textContent=((d.dates||[]).length?'Loaded Coach results.':'No Coach results saved for this selection.')
      +(d.alternate_error?' Alternate record: '+d.alternate_error:'');
    renderNbaCoachTrackRecord();
  }catch(e){if(msg)msg.textContent=e.message||'Error loading Coach Track Record';if(box)box.innerHTML='';}
}
function renderNbaCoachTrackRecord(){
 var box=document.getElementById('nbaCoachTrackResults');if(!box||!_nbaCoachTrackData)return;
 var ds=(document.getElementById('nbaCoachTrkDate')||{}).value||'',stake=Number((document.getElementById('nbaCoachTrkStake')||{}).value)||100;
 var rows=[];(_nbaCoachTrackData.dates||[]).forEach(function(day){if(!ds||day.date===ds)(day.detail||[]).forEach(function(x){rows.push(Object.assign({date:day.date},x));});});
 var tw=0,tl=0,tpend=0,tnet=0,tstaked=0;
 rows.forEach(function(x){var r=String(x.result||'PENDING').toUpperCase();if(r==='WIN'){tw++;tnet+=nbaProfit(x.odds,stake,r);tstaked+=stake}else if(r==='LOSS'){tl++;tnet-=stake;tstaked+=stake}else if(r!=='PUSH')tpend++;});
 var summary=_nbaRecordSummary(tw,tl,tpend,tnet,tstaked,stake);
 if(!rows.length){box.innerHTML=_nbaRecordEmpty('No Coach results for this selection','For historical dates, run the NBA historical replay first.');return;}
 if(_nbaCoachTrackView==='full'){box.innerHTML=summary+_nbaRecordFullList(rows,stake,false);return;}
 var groups={};rows.forEach(function(x){var k=x.preset||x.category||'Coach Edge';(groups[k]||(groups[k]=[])).push(x);});
 var html=summary;Object.keys(groups).forEach(function(k){var list=groups[k],w=0,l=0,p=0,pend=0,net=0,priced=0;
   list.forEach(function(x){var r=String(x.result||'PENDING').toUpperCase();if(r==='WIN'){w++;priced++;net+=nbaProfit(x.odds,stake,r)}else if(r==='LOSS'){l++;priced++;net-=stake}else if(r==='PUSH')p++;else pend++;});
   var rate=w+l?w/(w+l)*100:null,roi=priced?net/(priced*stake)*100:null,color=net>=0?'#4ade80':'#f87171';
   var detail=list.map(function(x){var r=String(x.result||'PENDING').toUpperCase(),pl=(r==='WIN'||r==='LOSS')?nbaProfit(x.odds,stake,r):null;return '<tr><td>'+_nbaEsc(x.date)+'</td><td><b>'+_nbaEsc(x.name||x.player)+'</b><br><small>'+_nbaEsc(x.team||'')+'</small></td><td>'+_nbaEsc(x.category||x.stat||'')+'<br><b>'+_nbaEsc(x.side)+' '+_nbaEsc(x.line)+'</b></td><td>'+_nbaEsc(x.odds)+'<br><small>'+_nbaEsc(x.book||'')+'</small></td><td>'+_nbaEsc(x.actual)+'</td><td style=\"color:'+(r==='WIN'?'#4ade80':r==='LOSS'?'#f87171':'#fbbf24')+'\">'+r+'<br><small>'+(pl==null?'—':(pl>=0?'+$':'-$')+Math.abs(pl).toFixed(2))+'</small></td><td><small>App '+Number((x.model_probability||0)*((x.model_probability||0)<=1?100:1)).toFixed(1)+'%<br>Implied '+Number((x.implied_probability||0)*((x.implied_probability||0)<=1?100:1)).toFixed(1)+'%<br>Edge '+Number(x.coach_edge!=null?x.coach_edge:(x.edge||0)*100).toFixed(1)+' pts</small></td></tr>';}).join('');
   html+='<details class=\"nba-acc-row\"><summary class=\"nba-acc-head\"><b>'+_nbaEsc(k)+'</b><span class=\"nba-acc-stats\">'+w+'W · '+l+'L'+(p?' · '+p+'P':'')+(pend?' · '+pend+' pending':'')+' · '+(rate==null?'—':rate.toFixed(1)+'%')+' · <b style=\"color:'+color+'\">'+(net>=0?'+$':'-$')+Math.abs(net).toFixed(2)+'</b> · '+(roi==null?'—':roi.toFixed(1)+'% ROI')+'</span></summary><div class=\"nba-acc-body\" style=\"display:block\"><div style=\"overflow-x:auto\"><table class=\"nba-trk-tbl\"><thead><tr><th>Date</th><th>Player</th><th>Play</th><th>Odds/Book</th><th>Actual</th><th>Result/P&L</th><th>Probabilities</th></tr></thead><tbody>'+detail+'</tbody></table></div></div></details>';
 });
 box.innerHTML=html||_nbaRecordEmpty('No Coach results for this selection','For historical dates, run the NBA historical replay first.');
}
</script>
"""

NBA_HISTORICAL_HTML = r"""
<section id="nba-historical-track" class="card nba-hist-shell" style="max-width:1040px;margin:22px auto;padding:14px">
 <style>
 .nba-hist-shell{border:1px solid #1e293b!important;background:#0b1121!important;border-radius:12px}
 .nba-hist-admin{display:none;margin-top:12px}
 .nba-acc-row { background: #111827; border: 1px solid #1e293b; border-radius: 8px; margin-bottom: 8px; overflow: hidden; border-left: 4px solid #3b82f6; display: flex; flex-direction: column; }
 .nba-acc-row.cat-Points { border-left-color: #3b82f6; }
 .nba-acc-row.cat-Rebounds { border-left-color: #f97316; }
 .nba-acc-row.cat-Assists { border-left-color: #fbbf24; }
 .nba-acc-row.cat-3Pointers { border-left-color: #ef4444; }
 .nba-acc-row.cat-PtsRebAst { border-left-color: #a855f7; }
 .nba-acc-row.cat-PtsReb { border-left-color: #8b5cf6; }
 .nba-acc-row.cat-PtsAst { border-left-color: #ec4899; }
 .nba-acc-row.cat-RebAst { border-left-color: #14b8a6; }
 .nba-acc-row.cat-Blocks { border-left-color: #22c55e; }
 .nba-acc-row.cat-Steals { border-left-color: #06b6d4; }
 .nba-acc-head { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; cursor: pointer; user-select: none; }
 .nba-acc-head:hover { background: #1e293b; }
 .nba-acc-title { display: flex; align-items: center; gap: 12px; font-size: .85rem; font-weight: 700; color:#f8fafc; }
 .nba-acc-side { font-size: .65rem; padding: 2px 8px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.2); font-weight: 700; }
 .nba-acc-side.over { color: #60a5fa; border-color: #1d4ed8; background: rgba(29,78,216,.1); }
 .nba-acc-side.under { color: #fcd34d; border-color: #b45309; background: rgba(180,83,9,.1); }
 .nba-acc-stats { display: flex; gap: 16px; font-size: .8rem; color: #cbd5e1; align-items: center; font-weight: 700; }
 .nba-acc-btn { background: #334155; color: #fff; border: none; border-radius: 4px; width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; font-weight: bold; cursor: pointer; }
 .nba-acc-body { display: none; border-top: 1px solid #1e293b; padding: 0; }
 .nba-acc-row.open .nba-acc-body { display: block; }
 .nba-acc-row.open .nba-acc-btn { background: #475569; transform: rotate(45deg); transition: transform 0.2s; }
 .nba-acc-body table { width: 100%; border-collapse: collapse; font-size: .75rem; }
 .nba-acc-body th { padding: 10px 16px; text-align: left; border-bottom: 1px solid #1e293b; color: #93c5fd; font-size: .65rem; text-transform: uppercase; }
 .nba-acc-body td { padding: 10px 16px; border-bottom: 1px solid #1e293b; color: #e2e8f0; }
 .nba-acc-body tr:last-child td { border-bottom: none; }
 @media(max-width:600px){.nba-acc-body table{min-width:680px}.nba-acc-stats{gap:8px;font-size:.7rem}}
 </style>
 <div class="nba-record-hero" style="background:radial-gradient(circle at 92% 10%,rgba(14,116,144,.2),transparent 38%),linear-gradient(145deg,#111827,#080d19)">
  <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap">
   <div><div class="nba-record-kicker" style="color:#22d3ee">NBA Replay Lab</div><h2 class="nba-record-title">&#128338; Historical Track Record</h2><div class="nba-record-sub">Point-in-time historical replays for research and model comparison. These results never enter the official pre-game record.</div></div>
   <div class="nba-record-badge" style="border-color:#155e75;background:rgba(8,47,73,.35);color:#67e8f9">&#9679; REPLAY · RESEARCH ONLY</div>
  </div>
  <div class="nba-record-tools">
   <label><span class="nba-record-label">Replay month</span><select id="nbaHistMonth" class="nba-record-control" onchange="nbaHistLoad()"></select></label>
   <button class="nba-view-btn active" onclick="nbaHistLoad()">Load Month</button>
   <label><span class="nba-record-label">Replay date</span><select id="nbaHistDateSelect" class="nba-record-control" onchange="nbaHistRenderAll()"><option value="">No saved dates</option></select></label>
   <label><span class="nba-record-label">Flat bet amount</span><input type="number" id="nbaHistBetSize" class="nba-record-control" value="100.00" min="1" step="5" style="width:110px" oninput="nbaHistRenderAll()"></label>
   <div class="nba-view-tabs"><button id="nbaHistCatBtn" class="nba-view-btn active" onclick="setNbaHistView('category')">By Category</button><button id="nbaHistFullBtn" class="nba-view-btn" onclick="setNbaHistView('full')">Full List</button></div>
  </div>
 </div>
 <div id="nbaHistMsg" role="status" style="color:#67e8f9;font-size:.68rem;margin:10px 4px 0"></div>
 <div id="nbaHistAdmin" class="nba-hist-admin" style="padding:12px 4px 0"><button class="nba-view-btn" onclick="nbaHistRun()" style="background:#92400e;color:#fff;border-color:#b45309">Run Selected Month</button> <span id="nbaHistProgress" style="color:#fbbf24;font-size:.7rem;margin-left:8px"></span></div>
 <div id="nbaHistTopPct" style="display:none"></div><div id="nbaHistTopStats" style="display:none"></div>
 <div id="nbaHistDates"></div>
</section>
<script>
window._nbaHistData = [];
window._nbaHistView = 'category';
function setNbaHistView(view){
  window._nbaHistView=view==='full'?'full':'category';
  var c=document.getElementById('nbaHistCatBtn'),f=document.getElementById('nbaHistFullBtn');
  if(c)c.classList.toggle('active',window._nbaHistView==='category');
  if(f)f.classList.toggle('active',window._nbaHistView==='full');
  nbaHistRenderAll();
}
function nbaHistToken(){return localStorage.getItem('__mpa_token')||''}
function nbaHistEsc(x){return String(x==null?'—':x).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function nbaProfit(odds, stake, res) {
  if (res === 'PUSH') return 0;
  if (res === 'LOSS') return -stake;
  if (res !== 'WIN') return 0;
  var o = parseFloat(String(odds).replace('+',''));
  if (isNaN(o)) return stake * 0.91; // fallback
  if (o > 0) return stake * (o / 100);
  if (o < 0) return stake * (100 / Math.abs(o));
  return 0;
}
function nbaHistDetail(rows){
  return '<div style="overflow-x:auto"><table class="nba-hist-table"><thead><tr><th>Player</th><th>Market</th><th>Pick</th><th>Odds / Book</th><th>Actual</th><th>Result</th><th>P/L</th><th>Evidence</th></tr></thead><tbody>'+
  ((rows||[]).map(function(x){
    var rc=x.result==='WIN'?'#4ade80':x.result==='LOSS'?'#f87171':'#fbbf24';
    return '<tr><td>'+nbaHistEsc(x.player)+'<br><small>'+nbaHistEsc(x.team)+' vs '+nbaHistEsc(x.opp)+'</small></td>'+
    '<td>'+nbaHistEsc(x.stat_label||x.stat)+'</td><td>'+nbaHistEsc(x.side)+' '+nbaHistEsc(x.line)+'</td>'+
    '<td>'+nbaHistEsc(x.odds)+'<br><small>'+nbaHistEsc(x.book)+'</small></td><td>'+nbaHistEsc(x.actual)+'</td>'+
    '<td style="color:'+rc+'">'+nbaHistEsc(x.result||'PENDING')+'</td><td>'+((x.clientProfit==null)?'—':(Number(x.clientProfit)>=0?'+':'')+'$'+Number(x.clientProfit).toFixed(2))+'</td>'+
    '<td>'+nbaHistEsc(x.hits)+'/'+nbaHistEsc(x.games)+' ('+nbaHistEsc(x.pct)+'%)<br><small>'+nbaHistEsc(x.mpg)+' MPG</small></td></tr>'
  }).join(''))+'</tbody></table></div>'
}
function nbaHistMonth(){return document.getElementById('nbaHistMonth').value}
function toggleHistAcc(el) { el.parentElement.classList.toggle('open'); }
function nbaHistCatClass(cat) { return 'cat-' + String(cat||'').replace(/[^a-zA-Z0-9]/g, ''); }
function nbaHistRenderAll() {
  var stake = parseFloat(document.getElementById('nbaHistBetSize').value) || 100;
  var sel = document.getElementById('nbaHistDateSelect').value;
  var out = document.getElementById('nbaHistDates');
  var topPct = document.getElementById('nbaHistTopPct');
  var topStats = document.getElementById('nbaHistTopStats');
  
  if(!window._nbaHistData || !window._nbaHistData.length) {
    if(out) out.innerHTML=_nbaRecordEmpty('No historical results loaded','Choose a replay month. Saved replay dates and graded results will appear here.');
    if(topPct) topPct.innerHTML=''; if(topStats) topStats.innerHTML='';
    return;
  }
  
  var day = window._nbaHistData.find(function(d){return d.date===sel;});
  if(!day) { if(out) out.innerHTML=_nbaRecordEmpty('No replay for this date','Choose another saved replay date from the menu.'); if(topPct) topPct.innerHTML=''; if(topStats) topStats.innerHTML=''; return; }
  
  var cats = ["Points","Rebounds","Assists","3-Pointers","Pts+Reb+Ast","Pts+Reb","Pts+Ast","Reb+Ast","Blocks","Steals"];
  var groups = {};
  var dw=0, dl=0, dp=0, dpend=0, dnet=0, dstaked=0;
  
  day.detail.forEach(function(x){
    var cat = x.stat_label||x.category||x.stat;
    var s = (x.side||'OVER').toUpperCase();
    var k = cat+'|'+s;
    if(!groups[k]) groups[k] = { cat:cat, side:s, w:0, l:0, p:0, pend:0, net:0, staked:0, rows:[] };
    var g = groups[k];
    g.rows.push(x);
    
    if (x.result==='WIN'||x.result==='LOSS'||x.result==='PUSH') {
      x.clientProfit = nbaProfit(x.odds, stake, x.result);
      if (x.result !== 'PUSH') {
        g.staked += stake;
        dstaked += stake;
      }
      g.net += x.clientProfit;
      dnet += x.clientProfit;
      if(x.result==='WIN') { g.w++; dw++; }
      else if(x.result==='LOSS') { g.l++; dl++; }
      else { g.p++; dp++; }
    } else {
      x.clientProfit = null;
      g.pend++;
      dpend++;
    }
  });
  
  var droi = dstaked>0 ? (dnet/dstaked*100).toFixed(1) : '0.0';
  var dpctStr = (dw+dl)>0 ? (dw/(dw+dl)*100).toFixed(1)+'%' : '0%';
  if (topPct) topPct.innerHTML = dw+'W/'+(dw+dl)+'L ('+dpctStr+')';
  if (topStats) {
    var netColor = dnet > 0 ? '#4ade80' : dnet < 0 ? '#f87171' : '#cbd5e1';
    var roiColor = (dstaked > 0 && dnet > 0) ? '#4ade80' : (dstaked > 0 && dnet < 0) ? '#f87171' : '#cbd5e1';
    topStats.innerHTML = 
      '<span style="color:'+netColor+'">Net '+(dnet>=0?'+':'')+'$'+dnet.toFixed(2)+'</span>'+
      '<span style="color:'+roiColor+'">ROI '+(dnet>=0?'+':'')+droi+'%</span>'+
      (dpend > 0 ? '<span style="color:#fbbf24">'+dpend+' pending</span>' : '')+
      '<span style="color:#64748b;font-weight:400;font-size:.75rem;margin-left:8px"> $'+stake.toFixed(2)+'/play · '+(dw+dl)+' priced plays</span>';
  }
  
  var html = _nbaRecordSummary(dw,dl,dpend,dnet,dstaked,stake);
  if(window._nbaHistView==='full'){
    html+=_nbaRecordFullList(day.detail||[],stake,true);
    if(out)out.innerHTML=html;
    return;
  }
  function renderGroup(g) {
    var total = g.w+g.l;
    var pct = total>0 ? (g.w/total*100).toFixed(1)+'%' : '0%';
    var color = g.net>0 ? '#4ade80' : g.net<0 ? '#f87171' : '#cbd5e1';
    var pendStr = g.pend ? ' · '+g.pend+' pending' : '';
    html += '<div class="nba-acc-row '+nbaHistCatClass(g.cat)+'">'+
      '<div class="nba-acc-head" onclick="toggleHistAcc(this)">'+
        '<div class="nba-acc-title"><span style="color:#64748b;font-weight:800;font-size:.7rem;text-transform:uppercase;letter-spacing:.1em">Category</span> '+nbaHistEsc(g.cat)+' <span class="nba-acc-side '+(g.side==='OVER'?'over':'under')+'">'+g.side+'</span></div>'+
        '<div class="nba-acc-stats">'+
          '<span>'+g.w+'W · '+g.l+'L'+pendStr+'</span>'+
          '<span style="color:#fcd34d">'+pct+'</span>'+
          '<span style="color:'+color+';width:80px;text-align:right">'+(g.net>=0?'+':'')+'$'+g.net.toFixed(2)+'</span>'+
          '<button class="nba-acc-btn">+</button>'+
        '</div>'+
      '</div>'+
      '<div class="nba-acc-body"><div style="overflow-x:auto"><table><thead><tr>'+
      '<th>Player</th><th>Market</th><th>Pick</th><th>Odds / Book</th><th>Actual</th><th>Result</th><th>P/L</th><th>Evidence</th>'+
      '</tr></thead><tbody>'+
      g.rows.map(function(b){
        var rc=b.result==='WIN'?'#4ade80':b.result==='LOSS'?'#f87171':'#fbbf24';
        var pc=(b.clientProfit||0)>=0?'#4ade80':'#f87171';
        return '<tr>'+
          '<td>'+nbaHistEsc(b.player||b.name)+'<br><small style="color:#94a3b8">'+nbaHistEsc(b.team)+' vs '+nbaHistEsc(b.opp)+'</small></td>'+
          '<td>'+nbaHistEsc(b.stat_label||b.category||b.stat)+'</td>'+
          '<td><span style="font-weight:700">'+nbaHistEsc(b.side)+'</span> '+nbaHistEsc(b.line)+'</td>'+
          '<td style="color:#94a3b8">'+nbaHistEsc(b.odds)+'<br><small>'+nbaHistEsc(b.book)+'</small></td><td>'+nbaHistEsc(b.actual)+'</td>'+
          '<td style="color:'+rc+';font-weight:700">'+nbaHistEsc(b.result||'PENDING')+'</td>'+
          '<td style="color:'+pc+';font-weight:700">'+(b.clientProfit!=null?(b.clientProfit>=0?'+':'')+'$'+Number(b.clientProfit).toFixed(2):'—')+'</td>'+
          '<td>'+(b.hits!=null?nbaHistEsc(b.hits)+'/'+nbaHistEsc(b.games)+' ('+nbaHistEsc(b.pct)+'%)<br><small>'+nbaHistEsc(b.mpg)+' MPG</small>':'')+'</td>'+
          '</tr>';
      }).join('')+
      '</tbody></table></div></div>'+
    '</div>';
  }
  
  cats.forEach(function(c){
    ['OVER','UNDER'].forEach(function(s){
      if(groups[c+'|'+s]) renderGroup(groups[c+'|'+s]);
    });
  });
  
  var handled = {}; cats.forEach(function(c){ handled[c]=true; });
  Object.keys(groups).forEach(function(k){
    var g = groups[k];
    if(!handled[g.cat]) renderGroup(g);
  });
  
  if(out) out.innerHTML = html || _nbaRecordEmpty('No replay results for this date','Choose another saved replay date from the menu.');
}
async function nbaHistLoad(){
 var m=nbaHistMonth(),msg=document.getElementById('nbaHistMsg');msg.textContent='Loading…';
 try{var r=await fetch('/api/nba/historical-track-record?month='+encodeURIComponent(m),{headers:{Authorization:'Bearer '+nbaHistToken()}}),d=await r.json();if(!r.ok)throw Error(d.detail||'Unavailable');
  window._nbaHistData = (d.dates||[]).slice().sort(function(a,b){return String(b.date).localeCompare(String(a.date));});
  var ds = document.getElementById('nbaHistDateSelect');
  ds.innerHTML = '';
  if(window._nbaHistData.length === 0) {
    var o = document.createElement('option'); o.value = ''; o.textContent = 'No saved dates'; ds.appendChild(o);
  } else {
    window._nbaHistData.forEach(function(x){
      var o = document.createElement('option'); o.value = x.date; o.textContent = x.date; ds.appendChild(o);
    });
    ds.value = window._nbaHistData[0].date;
  }
  msg.textContent='Loaded '+(d.dates||[]).length+' replay dates.'; if(window.IS_ADMIN)document.getElementById('nbaHistAdmin').style.display='block';
  nbaHistRenderAll();
 }catch(e){msg.textContent=e.message}
}
async function nbaHistRun(){var m=nbaHistMonth(),r=await fetch('/api/nba/historical-track-record/month',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+nbaHistToken()},body:JSON.stringify({month:m})});var d=await r.json();document.getElementById('nbaHistProgress').textContent=d.detail||'Started';if(d.started)nbaHistPoll()}
async function nbaHistPoll(){if(!window.IS_ADMIN)return;var m=nbaHistMonth(),r=await fetch('/api/nba/historical-track-record/month?month='+encodeURIComponent(m),{headers:{Authorization:'Bearer '+nbaHistToken()}}),d=await r.json(),s=d.status||{};document.getElementById('nbaHistProgress').textContent=(s.current_date||'')+' · '+(s.completed_dates||[]).length+' complete';if(s.running)setTimeout(nbaHistPoll,2500);else nbaHistLoad()}
document.addEventListener('DOMContentLoaded',function(){var s=document.getElementById('nbaHistMonth'),n=new Date();for(var i=0;i<36;i++){var d=new Date(n.getFullYear(),n.getMonth()-i,1),v=d.toISOString().slice(0,7),o=document.createElement('option');o.value=v;o.textContent=v;s.appendChild(o)}nbaHistLoad()})
</script>
"""

# ─── Bet Log Routes ───────────────────────────────────────────────────────────
NBA_GP_HTML = r"""
<style>
.ngp-shell{max-width:1240px;margin:0 auto 22px;border:1px solid rgba(167,139,250,.45)!important;background:radial-gradient(circle at 95% 0%,rgba(124,58,237,.18),transparent 40%),linear-gradient(145deg,#120f22,#0a0a14)!important;border-radius:18px;padding:18px}
.ngp-head{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:14px;align-items:center}
.ngp-badge{width:50px;height:50px;border-radius:14px;display:grid;place-items:center;font-size:1.55rem;background:radial-gradient(circle at 30% 25%,rgba(167,139,250,.4),rgba(56,189,248,.2));border:1px solid rgba(196,181,253,.45)}
.ngp-kicker{color:#a78bfa;font-size:.62rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase}
.ngp-title{font-family:'Playfair Display',serif;color:#fff;font-size:1.55rem;margin:2px 0 0;line-height:1.15}
.ngp-sub{color:#94a3b8;font-size:.74rem;margin-top:3px}
.ngp-side{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}
.ngp-btn{background:#15122a;color:#c4b5fd;border:1px solid rgba(167,139,250,.45);border-radius:10px;padding:8px 12px;font-size:.72rem;font-weight:900;cursor:pointer;white-space:nowrap}
.ngp-btn:hover{background:rgba(167,139,250,.14)}
.ngp-msg{color:#fbbf24;font-size:.74rem;margin:10px 0 0}
.ngp-rec-strip{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.ngp-rec-box{flex:1;min-width:150px;background:#0d0b1a;border:1px solid #262046;border-radius:12px;padding:9px 12px;cursor:pointer}
.ngp-rec-box .k{color:#8b86b5;font-size:.58rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase}
.ngp-rec-box .v{color:#fff;font-size:1.05rem;font-weight:900;margin-top:2px}
.ngp-rec-box .s{color:#94a3b8;font-size:.66rem;margin-top:1px}
.ngp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px;margin-top:16px}
@media(max-width:760px){.ngp-grid{grid-template-columns:1fr}.ngp-head{grid-template-columns:auto 1fr}.ngp-side{grid-column:1/-1;justify-content:flex-start}}
.ngp-card{background:linear-gradient(180deg,#14121f,#0e0d16);border:1px solid #29243f;border-radius:18px;padding:16px;overflow:hidden;position:relative}
.ngp-card.final{border-color:#334155}
.ngp-top{display:flex;align-items:center;justify-content:space-between;gap:8px}
.ngp-mu{display:flex;align-items:center;gap:8px;font-weight:900;color:#fff;font-size:1rem}
.ngp-mu img{width:30px;height:30px;object-fit:contain}
.ngp-at{color:#4b5563;font-weight:500}
.ngp-time{color:#94a3b8;font-size:.7rem;text-align:right}
.ngp-tags{display:flex;gap:5px;flex-wrap:wrap;margin-top:8px}
.ngp-tag{border-radius:999px;padding:2px 8px;font-size:.58rem;font-weight:900;letter-spacing:.06em;border:1px solid #334155;color:#cbd5e1}
.ngp-tag.saved{border-color:rgba(74,222,128,.45);color:#86efac;background:rgba(74,222,128,.08)}
.ngp-tag.retro{border-color:rgba(251,191,36,.45);color:#fcd34d;background:rgba(251,191,36,.08)}
.ngp-tag.live{border-color:rgba(248,113,113,.5);color:#fca5a5;background:rgba(248,113,113,.1)}
.ngp-hero{display:flex;align-items:center;gap:12px;margin-top:12px;padding:12px 14px;border-radius:14px;background:linear-gradient(135deg,rgba(167,139,250,.16),rgba(167,139,250,.04));border:1px solid rgba(167,139,250,.35)}
.ngp-hero img{width:44px;height:44px;object-fit:contain}
.ngp-hero .lbl{color:#a5a0d0;font-size:.6rem;font-weight:900;letter-spacing:.1em}
.ngp-hero .team{color:#fff;font-size:1.08rem;font-weight:900;line-height:1.15}
.ngp-hero .score{color:#c4b5fd;font-size:.76rem;font-weight:800;margin-top:2px}
.ngp-hero .prob{margin-left:auto;text-align:center}
.ngp-hero .prob b{display:block;color:#fff;font-size:1.45rem;font-weight:900;line-height:1}
.ngp-hero .prob small{color:#a5a0d0;font-size:.56rem;font-weight:900;letter-spacing:.1em}
.ngp-bar-l{display:flex;justify-content:space-between;font-size:.68rem;font-weight:900;margin:12px 0 4px}
.ngp-bar{height:9px;border-radius:999px;background:#1e1b31;overflow:hidden;display:flex}
.ngp-bar .a{background:linear-gradient(90deg,#3b82f6,#60a5fa)}
.ngp-bar .h{background:linear-gradient(90deg,#f59e0b,#fbbf24)}
.ngp-boxes{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-top:10px}
.ngp-box{background:#0b0a13;border:1px solid #221e36;border-radius:11px;padding:8px 6px;text-align:center}
.ngp-box .k{color:#7c7799;font-size:.55rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase}
.ngp-box .v{color:#fff;font-size:.95rem;font-weight:900;margin-top:2px}
.ngp-box .s{color:#8b93a7;font-size:.6rem;margin-top:1px}
.ngp-box.over{border-color:rgba(74,222,128,.45);background:rgba(74,222,128,.07)}.ngp-box.over .v{color:#4ade80}
.ngp-box.under{border-color:rgba(248,113,113,.45);background:rgba(248,113,113,.07)}.ngp-box.under .v{color:#f87171}
.ngp-box.pick{border-color:rgba(167,139,250,.5);background:rgba(167,139,250,.08)}.ngp-box.pick .v{color:#c4b5fd}
.ngp-box.lean .v{color:#94a3b8}
.ngp-ml{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;color:#a1a1aa;font-size:.68rem;margin-top:9px}
.ngp-ml b{color:#e5e7eb}
.ngp-res{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.ngp-chip{border-radius:8px;padding:4px 8px;font-size:.64rem;font-weight:900}
.ngp-chip.WIN{background:rgba(74,222,128,.14);color:#4ade80;border:1px solid rgba(74,222,128,.4)}
.ngp-chip.LOSS{background:rgba(248,113,113,.12);color:#f87171;border:1px solid rgba(248,113,113,.4)}
.ngp-chip.PUSH{background:rgba(250,204,21,.1);color:#facc15;border:1px solid rgba(250,204,21,.4)}
.ngp-sec{border-top:1px solid #221e36;margin-top:12px;padding-top:10px}
.ngp-sec>summary{cursor:pointer;color:#c4b5fd;font-size:.74rem;font-weight:900;list-style:none}
.ngp-sec>summary::-webkit-details-marker{display:none}
.ngp-sec>summary:before{content:"\25B8  ";color:#7c7799}
.ngp-sec[open]>summary:before{content:"\25BE  "}
.ngp-tbl{width:100%;border-collapse:collapse;font-size:.66rem;margin-top:8px}
.ngp-tbl th{color:#7c7799;font-weight:900;text-align:left;padding:5px 4px;font-size:.56rem;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #221e36}
.ngp-tbl td{padding:6px 4px;border-bottom:1px solid #1a1729;color:#e2e8f0;vertical-align:top}
.ngp-tbl td.n{color:#8b93a7;font-size:.6rem}
.ngp-tbl tr.same td{background:rgba(245,158,11,.1);color:#fde68a}
.ngp-tbl tr.today td{background:linear-gradient(90deg,rgba(167,139,250,.28),rgba(167,139,250,.1));color:#fff;font-weight:900;border-top:1px solid rgba(167,139,250,.6);border-bottom:1px solid rgba(167,139,250,.6)}
.ngp-pos{color:#4ade80!important;font-weight:800}.ngp-neg{color:#f87171!important;font-weight:800}.ngp-zero{color:#64748b!important}
.ngp-teams{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.ngp-team{background:#0b0a13;border:1px solid #221e36;border-radius:12px;padding:10px}
.ngp-thdr{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.ngp-thdr img{width:22px;height:22px;object-fit:contain}
.ngp-thdr b{color:#fff;font-size:.8rem}
.ngp-ha{margin-left:auto;font-size:.54rem;font-weight:900;border-radius:999px;padding:1px 7px}
.ngp-ha.h{background:rgba(245,158,11,.15);color:#fbbf24}.ngp-ha.a{background:rgba(96,165,250,.15);color:#93c5fd}
.ngp-sr{display:flex;justify-content:space-between;gap:6px;font-size:.64rem;padding:3px 0;border-bottom:1px dashed #1c1930}
.ngp-sr span:first-child{color:#8b93a7}.ngp-sr span:last-child{color:#e5e7eb;font-weight:800;text-align:right}
.ngp-l10{display:flex;gap:3px;flex-wrap:wrap;margin-top:7px}
.ngp-l10 span{width:20px;height:20px;border-radius:6px;display:grid;place-items:center;font-size:.58rem;font-weight:900;cursor:default}
.ngp-l10 .W{background:rgba(74,222,128,.16);color:#4ade80;border:1px solid rgba(74,222,128,.35)}
.ngp-l10 .L{background:rgba(248,113,113,.13);color:#f87171;border:1px solid rgba(248,113,113,.35)}
.ngp-badge2{display:inline-block;font-size:.55rem;font-weight:900;border-radius:6px;padding:1px 6px;margin:5px 4px 0 0}
.ngp-badge2.b2b{background:rgba(248,113,113,.15);color:#fca5a5}.ngp-badge2.stk{background:#1e1b31;color:#c4b5fd}.ngp-badge2.rest{background:rgba(56,189,248,.12);color:#7dd3fc}
.ngp-inj{font-size:.64rem;padding:4px 0;border-bottom:1px dashed #1c1930;display:flex;justify-content:space-between;gap:6px}
.ngp-inj .st{font-weight:900}.ngp-inj .st.out{color:#f87171}.ngp-inj .st.q{color:#fbbf24}.ngp-inj .st.p{color:#94a3b8}
.ngp-inj .imp{color:#f87171;font-weight:900;white-space:nowrap}
.ngp-crewtag{display:inline-block;border-radius:999px;padding:2px 9px;font-size:.6rem;font-weight:900;margin-left:6px}
.ngp-crewtag.hi{background:rgba(248,113,113,.14);color:#fca5a5}.ngp-crewtag.lo{background:rgba(74,222,128,.12);color:#86efac}.ngp-crewtag.nt{background:#1e1b31;color:#c4b5fd}
.ngp-note{color:#8b93a7;font-size:.62rem;margin-top:7px;line-height:1.45}
.ngp-how{margin-top:14px;color:#94a3b8;font-size:.72rem}
.ngp-how summary{cursor:pointer;color:#a78bfa;font-weight:900}
.ngp-how div{margin-top:8px;line-height:1.55}
.ngp-recpanel{margin-top:16px;border-top:1px solid #262046;padding-top:14px}
.ngp-day{background:#0d0b1a;border:1px solid #221e36;border-radius:12px;margin-top:8px}
.ngp-day>summary{cursor:pointer;padding:10px 12px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:.72rem;color:#e5e7eb;font-weight:800}
.ngp-day .body{padding:0 12px 10px;overflow-x:auto}
</style>
<div class="card ngp-shell" id="nbaGpSection">
  <div class="ngp-head">
    <div class="ngp-badge">&#128302;</div>
    <div><div class="ngp-kicker">Team model &middot; every game</div><h2 class="ngp-title">Game Predictor</h2>
      <div class="ngp-sub">Winner, spread and total: home/road points for &amp; against, last 10, rest, head-to-head, injuries and referee crews.</div></div>
    <div class="ngp-side"><button type="button" class="ngp-btn" id="nbaGpLoadBtn" onclick="nbaGpLoad(true)">&#128302; Load predictions</button><button type="button" class="ngp-btn" onclick="openNbaGpRecord()">&#128200; Predictor Record</button></div>
  </div>
  <div id="nbaGpRecordStrip" class="ngp-rec-strip"></div>
  <div id="nbaGpMsg" class="ngp-msg" role="status"></div>
  <div id="nbaGpGrid" class="ngp-grid"></div>
  <div id="nbaGpRecordPanel" class="ngp-recpanel" style="display:none"></div>
  <details class="ngp-how"><summary>&#9432; How the model works</summary><div>
    <b>Baseline:</b> the home team's home points for/against and the away team's road points for/against. Last season anchors the numbers early on; this season takes over as games are played.<br>
    <b>Last 10:</b> each team's last 10 scoring and defense versus its season level (capped at &plusmn;3.5 pts).<br>
    <b>Rest:</b> back-to-backs and 3-games-in-4-nights cost points; the rested opponent gains a little.<br>
    <b>Head-to-head:</b> the last 10 meetings, with games in today's building weighted double (capped at &plusmn;4 on the total, &plusmn;3 on the margin).<br>
    <b>Injuries:</b> Out/Doubtful/Questionable rotation players remove part of their scoring, weighted by how likely they are to sit (max 9 pts per team).<br>
    <b>Referees:</b> the posted crew's points, fouls and free throws per game versus league average (two seasons, shrunk for small samples) adjust the total by up to &plusmn;3.5 and the home margin by up to &plusmn;1.<br>
    <b>Picks:</b> win % comes from the projected margin. ATS picks need a 1.5-point edge over the spread; O/U picks need a 2-point edge over the total. Book lines are ESPN's posted odds; prices shorter than -1000 are never shown.<br>
    <b>Record:</b> the last forecast before each tip-off is saved and graded after the final. Started games always show the saved pregame forecast. Kept separate from every player-prop record.
  </div></details>
</div>
<script>
var __nbaGp={data:null,loading:false,date:''};
function _ngpEsc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function _ngpQS(){var t=localStorage.getItem('__mpa_token')||'',a=localStorage.getItem('__mpa_admin')||'';return '_tok='+encodeURIComponent(t)+(a?'&admin='+encodeURIComponent(a):'');}
function _ngpDate(){var dp=document.getElementById('datePicker');return (dp&&dp.value)||'__TODAY__';}
function _ngpOdds(o){if(o==null||o==='')return '';var n=Number(o);if(!isFinite(n))return '';return (n>0?'+':'')+Math.round(n);}
function _ngpSigned(v,dec){if(v==null||!isFinite(Number(v)))return '—';var n=Number(v);var t=n.toFixed(dec==null?1:dec);return (n>0?'+':'')+t;}
function _ngpCls(v){var n=Number(v);return !isFinite(n)||Math.abs(n)<0.05?'ngp-zero':(n>0?'ngp-pos':'ngp-neg');}
function _ngpTime(g){try{return new Date(g.tip).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',timeZoneName:'short'});}catch(e){return '';}}
function _ngpSpread(v){if(v==null)return '—';var n=Number(v);return n===0?'PK':(n>0?'+':'')+n;}
function _ngpRec(b){if(!b)return '—';return b.wins+'-'+b.losses+(b.pushes?'-'+b.pushes:'');}
function _ngpMoney(v){if(v==null)return '$0';var n=Number(v);return (n<0?'-$':'+$')+Math.abs(n).toFixed(0);}
function nbaGpLoad(force){
  var ds=_ngpDate(),grid=document.getElementById('nbaGpGrid'),msg=document.getElementById('nbaGpMsg');
  if(!grid)return;
  if(__nbaGp.loading)return;
  if(!force&&__nbaGp.data&&__nbaGp.date===ds)return;
  __nbaGp.loading=true;__nbaGp.date=ds;
  var lb=document.getElementById('nbaGpLoadBtn');if(lb)lb.innerHTML='&#8635; Refresh';
  msg.textContent='Loading Game Predictor for '+ds+'… first load of the day can take up to a minute.';
  if(!__nbaGp.data)grid.innerHTML='';
  fetch('/api/nba/game-predictor?date_str='+encodeURIComponent(ds)+'&'+_ngpQS()+(force?'&_r='+Date.now():''))
    .then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.detail||('HTTP '+r.status));return j;});})
    .then(function(d){
      __nbaGp.data=d;
      var gs=d.games||[];
      if(!gs.length){msg.textContent=d.message||'No NBA games on this date.';grid.innerHTML='';return;}
      var notes=[];
      if(!d.refsAvailable)notes.push('Referee history source unavailable right now; crews are shown without an adjustment.');
      if(d.partialHistory)notes.push('Some team history could not be loaded; affected numbers use what was available.');
      msg.textContent=notes.join(' ');
      grid.innerHTML=gs.map(_ngpCard).join('');
    })
    .catch(function(e){msg.textContent='Game Predictor unavailable: '+e.message;})
    .then(function(){__nbaGp.loading=false;});
  nbaGpLoadRecordStrip();
}
function _ngpTeamPanel(g,side){
  var p=side==='h'?g.homeProfile:g.awayProfile;if(!p)return '';
  var ab=side==='h'?g.home:g.away,logo=side==='h'?g.home_logo:g.away_logo,isH=side==='h';
  var pv=p.prev_venue||{},cv=p.cur_venue||{},cur=p.cur||{},l10=p.l10||{};
  var ss=Number(g.season)||0,prevLbl=(ss-2)+'–'+String(ss-1).slice(-2),curLbl=(ss-1)+'–'+String(ss).slice(-2);
  function pf(x){return x&&x.games?x.pf+' / '+x.pa+' <span style="color:#64748b">('+x.games+')</span>':'—';}
  var chips=(p.l10_games||[]).map(function(x){var w=x.pf>x.pa;return '<span class="'+(w?'W':'L')+'" title="'+_ngpEsc(x.date+' '+(x.home?'vs ':'@ ')+x.opp+' '+x.pf+'-'+x.pa)+'">'+(w?'W':'L')+'</span>';}).join('');
  var badges='';
  if(p.b2b)badges+='<span class="ngp-badge2 b2b">BACK-TO-BACK</span>';
  if(p.three_in_four)badges+='<span class="ngp-badge2 b2b">3 IN 4</span>';
  if(p.streak)badges+='<span class="ngp-badge2 stk">'+_ngpEsc(p.streak)+'</span>';
  if(p.rest_days!=null&&!p.b2b&&p.rest_days<=7)badges+='<span class="ngp-badge2 rest">'+(p.rest_days-1)+' DAY'+(p.rest_days-1===1?'':'S')+' REST</span>';
  return '<div class="ngp-team"><div class="ngp-thdr"><img src="'+_ngpEsc(logo)+'" alt="" onerror="this.style.display=&#39;none&#39;"><b>'+_ngpEsc(ab)+'</b><span class="ngp-ha '+(isH?'h':'a')+'">'+(isH?'HOME':'AWAY')+'</span></div>'
    +'<div class="ngp-sr"><span>'+prevLbl+' '+(isH?'home':'road')+' PF / PA</span><span>'+pf(pv)+'</span></div>'
    +'<div class="ngp-sr"><span>'+curLbl+' '+(isH?'home':'road')+' PF / PA</span><span>'+pf(cv)+'</span></div>'
    +'<div class="ngp-sr"><span>'+curLbl+' all games</span><span>'+pf(cur)+'</span></div>'
    +'<div class="ngp-sr"><span>'+(isH?'Home':'Road')+' record</span><span>'+(cv.games?cv.w+'-'+cv.l:'—')+'</span></div>'
    +'<div class="ngp-sr"><span>Last 10'+(p.l10_spans_prev?' (incl. last season)':'')+'</span><span>'+(l10.games?l10.w+'-'+l10.l+' · '+l10.pf+' / '+l10.pa:'—')+'</span></div>'
    +'<div class="ngp-l10">'+chips+'</div><div>'+badges+'</div></div>';
}
function _ngpInj(list,ab){
  if(!list||!list.length)return '<div class="ngp-note">'+_ngpEsc(ab)+': no injuries listed.</div>';
  return '<div style="margin-top:6px"><div style="color:#e5e7eb;font-size:.66rem;font-weight:900">'+_ngpEsc(ab)+'</div>'+list.map(function(x){
    var s=String(x.status||'').toLowerCase(),c=s.indexOf('out')>=0||s.indexOf('doubt')>=0?'out':(s.indexOf('quest')>=0||s.indexOf('day')>=0?'q':'p');
    return '<div class="ngp-inj"><span><b style="color:#fff">'+_ngpEsc(x.name)+'</b> <span style="color:#64748b">'+_ngpEsc(x.pos||'')+'</span> · <span class="st '+c+'">'+_ngpEsc(x.status)+'</span>'
      +(x.detail?' <span style="color:#64748b">'+_ngpEsc(x.detail)+'</span>':'')
      +'<br><span style="color:#8b93a7">'+(x.ppg!=null?x.ppg+' ppg · '+x.mpg+' mpg · ':'')+_ngpEsc(x.reason||'')+'</span></span>'
      +'<span class="imp">'+(x.counted?'-'+x.lost+' pts':'')+'</span></div>';
  }).join('')+'</div>';
}
function _ngpCrew(c){
  if(!c||!c.refs||!c.refs.length)return '<div class="ngp-note">Referee crew not posted yet. The NBA posts crews on game day (around 9 AM ET); refresh then.</div>';
  var tag=c.tag?'<span class="ngp-crewtag '+(c.tag.indexOf('High')===0?'hi':c.tag.indexOf('Lets')===0?'lo':'nt')+'">'+_ngpEsc(c.tag)+'</span>':'';
  var rows=c.refs.map(function(r){
    if(!r.games)return '<tr><td>'+_ngpEsc(r.name)+'</td><td colspan="5" class="n">No referee history found</td></tr>';
    return '<tr><td><b>'+_ngpEsc(r.name)+'</b></td><td>'+r.games+'</td>'
      +'<td>'+(r.pf!=null?r.pf:'—')+' <span class="'+_ngpCls(r.pf_rel)+'">('+_ngpSigned(r.pf_rel)+')</span></td>'
      +'<td>'+(r.fta!=null?r.fta:'—')+' <span class="'+_ngpCls(r.fta_rel)+'">('+_ngpSigned(r.fta_rel)+')</span></td>'
      +'<td>'+(r.pts!=null?r.pts:'—')+' <span class="'+_ngpCls(r.pts_rel)+'">('+_ngpSigned(r.pts_rel)+')</span></td>'
      +'<td>'+(r.home_win!=null?Math.round(r.home_win*1000)/10+'%':'—')+'</td></tr>';
  }).join('');
  return '<div style="margin-top:6px;font-size:.68rem;color:#e5e7eb;font-weight:900">Crew'+tag+'</div>'
    +'<div style="overflow-x:auto"><table class="ngp-tbl"><thead><tr><th>Referee</th><th>G</th><th>Fouls/G</th><th>FTA/G</th><th>Pts/G</th><th>Home W%</th></tr></thead><tbody>'+rows+'</tbody></table></div>'
    +(c.known?'<div class="ngp-note">Crew vs league average: fouls '+_ngpSigned(c.pf_rel)+', free-throw attempts '+_ngpSigned(c.fta_rel)+', points '+_ngpSigned(c.pts_rel)+' per game (sample-adjusted)'
      +(c.home_win!=null&&c.league_home_win!=null?'; home teams win '+Math.round(c.home_win*1000)/10+'% with this crew vs '+Math.round(c.league_home_win*1000)/10+'% league-wide':'')
      +'. Model effect: total '+_ngpSigned(c.total_adj)+', home margin '+_ngpSigned(c.margin_adj)+'. Numbers in brackets are vs league average.</div>':'');
}
function _ngpMeetings(g){
  var line=g.book&&g.book.total!=null?Number(g.book.total):null;
  var today='<tr class="today"><td>TODAY</td><td>'+_ngpEsc(g.away)+' @ '+_ngpEsc(g.home)+'</td>';
  if(g.completed&&g.home_score!=null){
    var tot=g.home_score+g.away_score,hw=g.home_score>g.away_score;
    today+='<td>'+g.away_score+'–'+g.home_score+'</td><td>'+_ngpEsc(hw?g.home:g.away)+'</td><td>'+(hw?'W':'L')+'</td><td>'+tot+'</td><td>'+(line==null?'—':tot>line?'OVER':tot<line?'UNDER':'PUSH')+'</td></tr>';
  }else{
    today+='<td>Proj '+Math.round(g.projAway)+'–'+Math.round(g.projHome)+'</td><td>'+_ngpEsc(g.pickTeam)+' '+g.pickProb+'%</td><td>—</td><td>'+g.projTotal+'</td><td>'+(line==null?'—':'Book '+line)+'</td></tr>';
  }
  var rows=(g.meetings||[]).map(function(m){
    var hw=m.winner===g.home,ou=line==null?'—':m.total>line?'OVER':m.total<line?'UNDER':'PUSH';
    return '<tr'+(m.sameVenue?' class="same"':'')+'><td>'+_ngpEsc(m.date)+(m.playoffs?' <span style="color:#a78bfa">PO</span>':'')+'</td>'
      +'<td>'+_ngpEsc(m.away)+' @ '+_ngpEsc(m.home)+(m.sameVenue?' · SAME':'')+'</td>'
      +'<td>'+m.awayPts+'–'+m.homePts+'</td><td>'+_ngpEsc(m.winner)+'</td><td>'+(hw?'W':'L')+'</td><td>'+m.total+'</td><td>'+ou+'</td></tr>';
  }).join('');
  var same=(g.meetings||[]).filter(function(m){return m.sameVenue;}),hW=(g.meetings||[]).filter(function(m){return m.winner===g.home;}).length;
  var avg=(g.meetings||[]).length?Math.round((g.meetings||[]).reduce(function(s,m){return s+m.total;},0)/g.meetings.length*10)/10:null;
  return '<div class="ngp-note">'+_ngpEsc(g.home)+' is '+hW+'-'+((g.meetings||[]).length-hW)+' in the last '+(g.meetings||[]).length+' meetings'+(avg!=null?' · avg total '+avg:'')+'. Gold rows = games in '+_ngpEsc(g.home)+'\'s building (today\'s venue). W/L is for '+_ngpEsc(g.home)+'. O/U compares with today\'s book total.</div>'
    +'<div style="overflow-x:auto"><table class="ngp-tbl"><thead><tr><th>Date</th><th>Game</th><th>Score</th><th>Winner</th><th>W/L</th><th>Total</th><th>vs O/U</th></tr></thead><tbody>'+today+(rows||'<tr><td colspan="7" class="n">No meetings in the last five seasons.</td></tr>')+'</tbody></table></div>';
}
function _ngpCard(g){
  var b=g.book||{},hp=Math.round((g.winProbHome||0.5)*100),ap=100-hp;
  var pickHome=g.pickTeam===g.home,pickLogo=pickHome?g.home_logo:g.away_logo,pickName=pickHome?g.home_name:g.away_name;
  var tags='';
  if(g.stype===1)tags+='<span class="ngp-tag">PRESEASON · NOT TRACKED</span>';
  if(g.stype===3)tags+='<span class="ngp-tag">PLAYOFFS</span>';
  if(g.savedPrediction)tags+='<span class="ngp-tag saved">SAVED PREGAME FORECAST</span>';
  else if(g.retro)tags+='<span class="ngp-tag retro">RETRO VIEW · NOT A SAVED FORECAST</span>';
  if(g.state==='in')tags+='<span class="ngp-tag live">LIVE · '+_ngpEsc(g.status)+'</span>';
  var right=g.completed&&g.home_score!=null?'<b style="color:#fff">FINAL '+g.away_score+'–'+g.home_score+'</b>':_ngpEsc(_ngpTime(g));
  var homeSpr=g.projMargin!=null?-g.projMargin:null;
  var atsCls=g.atsPick?'pick':'lean',atsV=g.atsPick?_ngpEsc(g.atsPick)+' '+_ngpSpread(g.atsPick===g.home?b.home_spread:(b.home_spread!=null?-b.home_spread:null)):(g.atsLean?'Lean '+_ngpEsc(g.atsLean):'—');
  var atsS=g.atsEdge!=null?'edge '+Math.abs(g.atsEdge).toFixed(1)+' pts'+(g.atsPick?'':' · no play'):'no spread posted';
  var ouCls=g.ouPick==='OVER'?'over':g.ouPick==='UNDER'?'under':'lean',ouV=g.ouPick?g.ouPick+' '+b.total:(g.ouLean?'Lean '+g.ouLean:'—');
  var ouOdds=g.ouPick==='OVER'?b.over_odds:g.ouPick==='UNDER'?b.under_odds:null;
  var ouS=g.ouEdge!=null?'edge '+Math.abs(g.ouEdge).toFixed(1)+' pts'+(g.ouPick?(ouOdds!=null?' · '+_ngpOdds(ouOdds):''):' · no play'):'no total posted';
  var ml='';
  if(g.pickMl!=null){var imp=Number(g.pickMl)>0?100/(Number(g.pickMl)+100):-Number(g.pickMl)/(-Number(g.pickMl)+100);
    ml='<span>'+_ngpEsc(g.pickTeam)+' ML <b>'+_ngpOdds(g.pickMl)+'</b> · model '+g.pickProb+'% vs implied '+(imp*100).toFixed(1)+'% → <b class="'+_ngpCls(g.mlEdge)+'">'+_ngpSigned(g.mlEdge)+' pts</b></span>';}
  else ml='<span>Moneyline unavailable'+(b.hidden_short?' (shorter than -1000)':'')+'</span>';
  var bpi=g.bpiHome!=null?'<span>ESPN BPI: '+_ngpEsc(g.home)+' '+g.bpiHome+'%</span>':'';
  var res='';
  if(g.result){var r=g.result;
    res='<div class="ngp-res"><span class="ngp-chip '+r.winner+'">Winner '+(r.winner==='WIN'?'✓':'✗')+'</span>'
      +(r.ats?'<span class="ngp-chip '+r.ats+'">ATS '+_ngpEsc(r.ats_pick)+' '+_ngpSpread(r.ats_line)+' · '+r.ats+'</span>':'')
      +(r.ou?'<span class="ngp-chip '+r.ou+'">'+r.ou_pick+' '+r.ou_line+' ('+r.ou_total+') · '+r.ou+'</span>':'')+'</div>';}
  var fx=(g.factors||[]).map(function(f){
    var isBase=f.name==='Home/road scoring baseline';
    return '<tr><td><b>'+_ngpEsc(f.name)+'</b><div class="n" style="color:#8b93a7;font-weight:500">'+_ngpEsc(f.note||'')+'</div></td>'
      +'<td class="'+(isBase?'':_ngpCls(f.away))+'">'+(isBase?f.away:_ngpSigned(f.away))+'</td><td class="'+(isBase?'':_ngpCls(f.home))+'">'+(isBase?f.home:_ngpSigned(f.home))+'</td></tr>';
  }).join('')+'<tr><td><b>Projected score</b></td><td><b>'+g.projAway+'</b></td><td><b>'+g.projHome+'</b></td></tr>';
  return '<div class="ngp-card'+(g.completed?' final':'')+'">'
    +'<div class="ngp-top"><div class="ngp-mu"><img src="'+_ngpEsc(g.away_logo)+'" alt="" onerror="this.style.display=&#39;none&#39;">'+_ngpEsc(g.away)+' <span class="ngp-at">@</span> '+_ngpEsc(g.home)+'<img src="'+_ngpEsc(g.home_logo)+'" alt="" onerror="this.style.display=&#39;none&#39;"></div>'
    +'<div class="ngp-time">'+right+'<br>'+_ngpEsc(g.venue||'')+'</div></div>'
    +(tags?'<div class="ngp-tags">'+tags+'</div>':'')
    +'<div class="ngp-hero"><img src="'+_ngpEsc(pickLogo)+'" alt="" onerror="this.style.display=&#39;none&#39;"><div><div class="lbl">PREDICTED WINNER</div><div class="team">'+_ngpEsc(pickName||g.pickTeam)+'</div>'
    +'<div class="score">Projected '+_ngpEsc(g.away)+' '+g.projAway+' – '+g.projHome+' '+_ngpEsc(g.home)+'</div></div>'
    +'<div class="prob"><b>'+g.pickProb+'%</b><small>WIN PROB</small></div></div>'
    +'<div class="ngp-bar-l"><span style="color:#60a5fa">'+_ngpEsc(g.away)+' '+ap+'%</span><span style="color:#fbbf24">'+hp+'% '+_ngpEsc(g.home)+'</span></div>'
    +'<div class="ngp-bar"><div class="a" style="width:'+ap+'%"></div><div class="h" style="width:'+hp+'%"></div></div>'
    +'<div class="ngp-boxes">'
      +'<div class="ngp-box"><div class="k">Model spread</div><div class="v">'+_ngpEsc(g.home)+' '+_ngpSpread(homeSpr==null?null:Math.round(homeSpr*10)/10)+'</div><div class="s">margin '+_ngpSigned(g.projMargin)+'</div></div>'
      +'<div class="ngp-box"><div class="k">Book spread</div><div class="v">'+(b.home_spread!=null?_ngpEsc(g.home)+' '+_ngpSpread(b.home_spread):'—')+'</div><div class="s">'+_ngpEsc(b.book||'')+(b.home_spread_odds!=null?' · '+_ngpOdds(b.home_spread_odds):'')+'</div></div>'
      +'<div class="ngp-box '+atsCls+'"><div class="k">ATS pick</div><div class="v">'+atsV+'</div><div class="s">'+atsS+'</div></div>'
      +'<div class="ngp-box"><div class="k">Proj total</div><div class="v">'+g.projTotal+'</div><div class="s">'+_ngpEsc(g.away)+' '+g.projAway+' + '+_ngpEsc(g.home)+' '+g.projHome+'</div></div>'
      +'<div class="ngp-box"><div class="k">Book O/U</div><div class="v">'+(b.total!=null?b.total:'—')+'</div><div class="s">'+(b.over_odds!=null?'o '+_ngpOdds(b.over_odds):'')+(b.under_odds!=null?' · u '+_ngpOdds(b.under_odds):'')+'</div></div>'
      +'<div class="ngp-box '+ouCls+'"><div class="k">O/U pick</div><div class="v">'+ouV+'</div><div class="s">'+ouS+'</div></div>'
    +'</div>'
    +'<div class="ngp-ml">'+ml+bpi+'</div>'
    +res
    +'<details class="ngp-sec"><summary>Why this pick · factor breakdown</summary><div style="overflow-x:auto"><table class="ngp-tbl"><thead><tr><th>Factor</th><th>'+_ngpEsc(g.away)+'</th><th>'+_ngpEsc(g.home)+'</th></tr></thead><tbody>'+fx+'</tbody></table></div></details>'
    +'<details class="ngp-sec" open><summary>Teams · points for/against, last 10 &amp; rest</summary><div class="ngp-teams">'+_ngpTeamPanel(g,'a')+_ngpTeamPanel(g,'h')+'</div></details>'
    +'<details class="ngp-sec"><summary>Last 10 meetings · today highlighted</summary>'+_ngpMeetings(g)+'</details>'
    +'<details class="ngp-sec"><summary>Injuries'+((g.lostHome||g.lostAway)?' · '+_ngpEsc(g.away)+' -'+(g.lostAway||0)+' / '+_ngpEsc(g.home)+' -'+(g.lostHome||0)+' pts':'')+'</summary>'+_ngpInj(g.injAway,g.away)+_ngpInj(g.injHome,g.home)+'</details>'
    +'<details class="ngp-sec"><summary>Referees'+(g.crew&&g.crew.tag?' · '+_ngpEsc(g.crew.tag):'')+'</summary>'+_ngpCrew(g.crew)+'</details>'
    +'</div>';
}
function nbaGpLoadRecordStrip(){
  var el=document.getElementById('nbaGpRecordStrip');if(!el)return;
  fetch('/api/nba/game-predictor/record?'+_ngpQS()).then(function(r){return r.ok?r.json():null;}).then(function(d){
    if(!d){el.innerHTML='';return;}
    __nbaGp.record=d;var t=d.totals||{};
    function box(k,label){var b=t[k]||{};return '<div class="ngp-rec-box" onclick="openNbaGpRecord()"><div class="k">'+label+'</div><div class="v">'+_ngpRec(b)+(b.pct!=null?' <span style="color:#a78bfa;font-size:.8rem">'+b.pct+'%</span>':'')+'</div><div class="s">'+(b.priced?_ngpMoney(b.profit)+' · ROI '+(b.roi!=null?b.roi+'%':'—')+' at $'+d.stake:'No graded games yet')+'</div></div>';}
    el.innerHTML=box('winner','Winner record')+box('ats','ATS record')+box('ou','O/U record');
    var p=document.getElementById('nbaGpRecordPanel');if(p&&p.style.display!=='none')_ngpRenderRecord();
  }).catch(function(){el.innerHTML='';});
}
function _ngpRenderRecord(){
  var p=document.getElementById('nbaGpRecordPanel'),d=__nbaGp.record;if(!p)return;
  if(!d){p.innerHTML='<div class="ngp-note">Loading record…</div>';return;}
  var days=(d.days||[]);
  var html='<div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><div style="color:#fff;font-weight:900;font-size:1rem">&#128200; Game Predictor Record</div><button type="button" class="ngp-btn" onclick="document.getElementById(&#39;nbaGpRecordPanel&#39;).style.display=&#39;none&#39;">Close</button></div>'
    +'<div class="ngp-note">Saved pregame forecasts only, graded after the final. Winner P/L uses the saved moneyline; ATS and O/U use the saved price (or -110 when no price was posted). $'+d.stake+' per pick.</div>';
  if(!days.length)html+='<div class="ngp-note" style="font-size:.74rem">No saved forecasts yet. The first forecasts are saved the first time the predictor loads before tip-off.</div>';
  html+=days.map(function(day){
    var rows=(day.games||[]).map(function(r){
      return '<tr><td>'+_ngpEsc(r.away)+' @ '+_ngpEsc(r.home)+'</td><td>'+r.away_score+'–'+r.home_score+'</td>'
        +'<td>'+_ngpEsc(r.pickTeam)+' '+(r.pickProb||'')+'% <span class="ngp-chip '+r.winner+'">'+r.winner+'</span></td>'
        +'<td>'+(r.ats?_ngpEsc(r.ats_pick)+' '+_ngpSpread(r.ats_line)+' <span class="ngp-chip '+r.ats+'">'+r.ats+'</span>':'<span class="n">no play</span>')+'</td>'
        +'<td>'+(r.ou?r.ou_pick+' '+r.ou_line+' <span class="ngp-chip '+r.ou+'">'+r.ou+'</span>':'<span class="n">no play</span>')+'</td></tr>';
    }).join('');
    var pend=day.saved-(day.games||[]).length;
    return '<details class="ngp-day"><summary><span>'+_ngpEsc(day.date)+'</span><span>Winner '+_ngpRec(day.winner)+' · ATS '+_ngpRec(day.ats)+' · O/U '+_ngpRec(day.ou)+(pend>0?' · <span style="color:#94a3b8">'+pend+' pending</span>':'')+'</span></summary>'
      +'<div class="body">'+(rows?'<table class="ngp-tbl"><thead><tr><th>Game</th><th>Final</th><th>Winner pick</th><th>ATS</th><th>O/U</th></tr></thead><tbody>'+rows+'</tbody></table>':'<div class="ngp-note">Games not final yet.</div>')+'</div></details>';
  }).join('');
  p.innerHTML=html;
}
function openNbaGpRecord(){
  var s=document.getElementById('nbaGpSection'),p=document.getElementById('nbaGpRecordPanel');
  if(!p)return;p.style.display='block';_ngpRenderRecord();if(!__nbaGp.record)nbaGpLoadRecordStrip();
  p.scrollIntoView({behavior:'smooth',block:'start'});
}
function openNbaGamePredictor(){var s=document.getElementById('nbaGpSection');if(s)s.scrollIntoView({behavior:'smooth',block:'start'});nbaGpLoad(false);}
function _ngpIdle(){
  var msg=document.getElementById('nbaGpMsg'),grid=document.getElementById('nbaGpGrid'),lb=document.getElementById('nbaGpLoadBtn');
  __nbaGp.data=null;__nbaGp.date='';
  if(grid)grid.innerHTML='';
  if(lb)lb.innerHTML='&#128302; Load predictions';
  if(msg)msg.innerHTML='<span style="color:#94a3b8">Predictions load only when you ask, so they never slow down a player-prop run. Click <b style="color:#c4b5fd">Load predictions</b> for '+_ngpEsc(_ngpDate())+'.</span>';
}
(function(){
  function init(){var dp=document.getElementById('datePicker');if(dp)dp.addEventListener('change',function(){if(!__nbaGp.loading)_ngpIdle();});_ngpIdle();}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
</script>
"""


@app.get("/api/bets")
async def nba_get_bets(request: Request, token: str = "", admin: str = "", settle: bool = True):
    from fastapi import HTTPException
    tok = token or request.headers.get("Authorization","").replace("Bearer ","").strip()
    if not _nba_bet_admin_ok(tok, admin):
        raise HTTPException(status_code=403, detail="Admin only")
    with _NBA_BET_LOCK:
        data = _nba_load_bets()
        key = _nba_bet_user_key(tok, admin)
        snapshot = list(data.get(key, []))
    # Settle OFF-lock: ESPN network calls (now cached) must not block POST/DELETE
    # behind _NBA_BET_LOCK. Re-acquire only to persist, MERGING settled fields by
    # id so a bet added concurrently (during settle) is never clobbered.
    if settle:
        loop = asyncio.get_running_loop()
        changed = await loop.run_in_executor(None, _nba_settle_batch, snapshot)
        # Apply ONLY bets settled to a terminal result this pass, and only onto a
        # still-pending on-disk bet — never write pending/None back and never flip an
        # already-terminal value (so a concurrent settle pass can't be clobbered).
        settled = ({b.get("id"): b for b in snapshot
                    if b.get("id") and b.get("result") in ("WIN", "LOSS", "PUSH")}
                   if changed else {})
        if settled:
            with _NBA_BET_LOCK:
                data = _nba_load_bets()
                for b in data.get(key, []):
                    s = settled.get(b.get("id"))
                    if s and b.get("result") not in ("WIN", "LOSS", "PUSH"):
                        for f in ("result", "actual", "profit", "settled_at"):
                            b[f] = s.get(f)
                _nba_save_bets(data)
    snapshot.sort(key=lambda b: (b.get("date",""), b.get("placed_at","")), reverse=True)
    return {"bets": snapshot, "summary": _nba_summarize_bets(snapshot)}

@app.post("/api/bets")
async def nba_add_bet(request: Request, token: str = "", admin: str = ""):
    from fastapi import HTTPException
    tok = token or request.headers.get("Authorization","").replace("Bearer ","").strip()
    if not _nba_bet_admin_ok(tok, admin):
        raise HTTPException(status_code=403, detail="Admin only")
    body = await request.json()
    try:
        stake = round(float(body.get("stake")), 2)
        odds = int(round(float(body.get("odds"))))
        line = float(body.get("line"))
    except Exception:
        raise HTTPException(status_code=400, detail="stake, odds and line must be numbers")
    if stake <= 0:
        raise HTTPException(status_code=400, detail="Bet size must be greater than 0")
    name = (body.get("name") or "").strip()
    stat_key = (body.get("stat_key") or "").strip()
    side = (body.get("side") or "OVER").strip().upper()
    if not name or stat_key not in _NBA_BET_STAT_KEYS or side not in ("OVER","UNDER"):
        raise HTTPException(status_code=400, detail="Invalid bet")
    bdate = (body.get("date") or _nba_today().isoformat()).strip()
    bet = {"id": _nba_uuid.uuid4().hex[:12], "date": bdate,
        "name": name, "team": (body.get("team") or "").strip(),
        "opp": (body.get("opp") or "").strip(),
        "category": (body.get("category") or "?").strip(),
        "side": side, "stat_key": stat_key,
        "stat_label": (body.get("stat_label") or "").strip(),
        "line": line, "odds": odds, "stake": stake,
        "placed_at": (body.get("placed_at") or _nba_today().isoformat()),
        "result": "pending", "actual": None, "profit": None, "settled_at": None}
    _nba_settle_bet(bet)
    with _NBA_BET_LOCK:
        data = _nba_load_bets()
        key = _nba_bet_user_key(tok, admin)
        data.setdefault(key, []).append(bet)
        _nba_save_bets(data)
    return {"ok": True, "bet": bet}

@app.delete("/api/bets/{bet_id}")
async def nba_delete_bet(bet_id: str, request: Request, token: str = "", admin: str = ""):
    from fastapi import HTTPException
    tok = token or request.headers.get("Authorization","").replace("Bearer ","").strip()
    if not _nba_bet_admin_ok(tok, admin):
        raise HTTPException(status_code=403, detail="Admin only")
    with _NBA_BET_LOCK:
        data = _nba_load_bets()
        key = _nba_bet_user_key(tok, admin)
        bets = data.get(key, [])
        new_bets = [b for b in bets if b.get("id") != bet_id]
        if len(new_bets) != len(bets):
            data[key] = new_bets
            _nba_save_bets(data)
    return {"ok": True}

@app.get("/api/bets/summary")
async def nba_bets_summary(request: Request, token: str = "", admin: str = ""):
    """Hub-callable summary — aggregated record and ROI only."""
    from fastapi import HTTPException
    tok = token or request.headers.get("Authorization","").replace("Bearer ","").strip()
    if not _nba_bet_admin_ok(tok, admin):
        raise HTTPException(status_code=403, detail="Admin only")
    with _NBA_BET_LOCK:
        data = _nba_load_bets()
        key = _nba_bet_user_key(tok, admin)
        bets = list(data.get(key, []))
    return {"sport": "NBA", "summary": _nba_summarize_bets(bets)}

# ─── NBA Coach Edge (isolated research surface) ───────────────────────────────
# This deliberately consumes the already loaded standard board and the dedicated
# alternate cache only.  It never writes to picks, bets, parlays, locks, or the
# historical NBA ledger.
def _nba_coach_implied(odds):
    try:
        o = float(str(odds).replace("+", ""))
        if o < -1000:
            return None
        return round((100.0 / (o + 100.0) if o >= 0 else abs(o) / (abs(o) + 100.0)), 5)
    except Exception:
        return None

def _nba_coach_prob(row, side, line):
    """Bounded empirical probability from the available H/A opponent log."""
    raw = row.get("history") or ""
    vals = []
    for x in str(raw).split(","):
        try: vals.append(float(x))
        except Exception: pass
    if not vals:
        # Durable historical replay rows retain the exact qualifying side,
        # line, hits, and games even when the raw comma-separated game log is
        # unavailable after a redeploy. Reuse only that exact saved signal;
        # never project it onto the opposite side or an alternate line.
        try:
            saved_side = str(row.get("side") or row.get("pick") or "").upper()
            saved_line = float(row.get("line"))
            games = int(row.get("games") or 0)
            hits = int(row.get("hits") or 0)
        except Exception:
            saved_side, saved_line, games, hits = "", None, 0, 0
        if (games > 0 and 0 <= hits <= games and saved_side == side
                and saved_line is not None and abs(saved_line - float(line)) < 1e-9):
            prob = (hits + 1.0) / (games + 2.0)
            return max(0.01, min(0.99, prob)), []
        # Props with no exact saved signal are not eligible; do not manufacture a prior.
        return None, []
    hits = sum(v > line if side == "OVER" else v < line for v in vals)
    # Laplace smoothing prevents a tiny 1/1 sample displaying 100%, while retaining
    # the app's empirical signal.  The bound is explicit and deterministic.
    prob = (hits + 1.0) / (len(vals) + 2.0)
    return max(0.01, min(0.99, prob)), vals

def _nba_coach_source(row):
    # Live rows use bookmaker_label/bookmaker; archived historical rows retain
    # the exact sportsbook title under book. Never infer a sportsbook, but do
    # not discard the archived identity when it is present.
    return (row.get("bookmaker_label") or row.get("book")
            or row.get("bookmaker") or "Odds API (bookmaker not retained)")

_NBA_SAFEST_MIN_RATE = 0.80

def _nba_coach_safest_rows(standard, alternate, count=10):
    """Safest Bets: 80%+ vs today's opponent at this venue, then the safer line.

    A side qualifies when the player's H/A history against today's opponent
    clears the posted book line at least 80% of the time. The play then moves
    to the genuine sportsbook line on the safer side of that book line (lower
    for OVER, higher for UNDER) that hits 100% of that same history; when no
    priced line reaches 100%, the highest-hit-rate priced line is used. Only
    real quoted prices no shorter than -1000 are eligible; no line or price is
    ever synthesized.
    """
    def _american(o):
        try:
            v = float(str(o).replace("+", ""))
        except Exception:
            return None
        return v if v >= -1000 and _nba_coach_implied(o) is not None else None
    ladders = {}
    for a in alternate or []:
        stat = a.get("stat") or a.get("stat_key")
        name = a.get("player") or a.get("name") or ""
        if stat in STAT_CONFIG and name:
            ladders.setdefault((_nn(name), stat), []).append(a)
    out = []
    for r in standard or []:
        stat = r.get("stat") or r.get("stat_key")
        if stat not in STAT_CONFIG:
            continue
        name = r.get("player") or r.get("name") or ""
        try:
            base_line = float(r.get("dk_line") if r.get("dk_line") is not None else r.get("line"))
        except Exception:
            continue
        vals = []
        for x in str(r.get("history") or "").split(","):
            try: vals.append(float(x))
            except Exception: pass
        if not name or not vals:
            continue
        n = len(vals)
        for side in ("OVER", "UNDER"):
            hit = (lambda v, ln: v > ln) if side == "OVER" else (lambda v, ln: v < ln)
            base_hits = sum(hit(v, base_line) for v in vals)
            if base_hits / n < _NBA_SAFEST_MIN_RATE:
                continue
            cands = []
            std_odds = r.get("dk_over_odds") if side == "OVER" else r.get("dk_under_odds")
            if std_odds in (None, ""):
                std_odds = r.get("over_odds") if side == "OVER" else r.get("under_odds")
            if std_odds in (None, "") and side == "OVER":
                std_odds = r.get("fd_odds") or r.get("odds")
            if _american(std_odds) is not None:
                cands.append((base_line, std_odds, _nba_coach_source(r), False))
            for a in ladders.get((_nn(name), stat), []):
                try:
                    ln = float(a.get("line"))
                except Exception:
                    continue
                if (side == "OVER" and not ln < base_line) or (side == "UNDER" and not ln > base_line):
                    continue
                o = a.get("over_odds") if side == "OVER" else a.get("under_odds")
                if _american(o) is not None:
                    cands.append((ln, o, _nba_coach_source(a), True))
            if not cands:
                continue
            def _rank(c, hit=hit, side=side):
                hits = sum(hit(v, c[0]) for v in vals)
                closeness = c[0] if side == "OVER" else -c[0]
                return (hits, closeness, _american(c[1]))
            line, odds, source, is_alt = max(cands, key=_rank)
            hits = sum(hit(v, line) for v in vals)
            model = hits / n
            implied = _nba_coach_implied(odds)
            edge = model - implied
            venue = str(r.get("side") or r.get("location") or "").lower()
            opp = r.get("opp_name") or r.get("opp") or "opponent"
            moved = (line != base_line)
            out.append({"player": name, "team": r.get("team", ""), "stat": stat,
                "category": STAT_CONFIG[stat]["label"], "side": side,
                "line": line, "odds": odds,
                "model_probability": round(model, 5), "implied_probability": implied,
                "edge": round(edge, 5), "coach_edge": round(edge * 100, 2),
                "book": source, "source": source, "alternate": bool(is_alt),
                "base_line": base_line, "base_hits": base_hits, "hits": hits, "games": n,
                "base_hit_rate": round(base_hits / n, 5), "safest": True,
                "recent_average": round(sum(vals[-10:]) / len(vals[-10:]), 1),
                "game_log": vals[-10:], "opponent_history": r.get("history") or "—",
                "selection_reason": (
                    f"{side} {base_line:g} hit {base_hits}/{n} vs {opp}"
                    f"{(' ' + venue) if venue in ('home', 'away') else ''}; "
                    + (f"safer line {side} {line:g} hits {hits}/{n} ({model*100:.0f}%) at {odds}."
                       if moved else
                       f"no safer priced line improved on it, so the book line is kept at {odds}."))})
    out.sort(key=lambda x: (x["model_probability"], x["games"],
                            _american(x["odds"]) or -1000), reverse=True)
    unique_rows, seen_players = [], set()
    for row in out:
        key = re.sub(r"[^a-z0-9]+", "", str(row.get("player") or "").lower())
        if not key or key in seen_players:
            continue
        seen_players.add(key)
        unique_rows.append(row)
    return unique_rows[:max(1, min(int(count or 10), 10))]

def _nba_coach_rows(standard, alternate, query="", mode="all", count=100):
    if mode == "safest":
        return _nba_coach_safest_rows(standard, alternate, count)
    q = (query or "").lower().strip()
    cat_terms = {k.lower(): k for k in STAT_CONFIG}
    cat_terms.update({v["label"].lower(): k for k, v in STAT_CONFIG.items()})
    wanted_cat = next((v for k, v in cat_terms.items() if k in q), None)
    wanted_side = "UNDER" if re.search(r"\bunder\b|\bless\b", q) else ("OVER" if re.search(r"\bover\b|\bmore\b", q) else None)
    intent_only = bool(q) and not wanted_cat and not wanted_side and all(
        t in {"safest","bets","positive","edge","top","pick","picks","coach","alternate","alt","genuine","lines",
              "best","minus","plus","favorite","underdog","american","odds","plays"}
        for t in q.split())
    if mode in ("alternate", "alternate_minus", "alternate_plus"):
        intent_only = True
    wanted_name = re.sub(r"\b(over|under|more|less|safest|edge|alternate|alt|coach|pick|top|best|minus|plus|favorite|underdog|american|odds|plays)\b", " ", q)
    def make(src, is_alt):
        out = []
        for r in src or []:
            stat = r.get("stat") or r.get("stat_key")
            if stat not in STAT_CONFIG: continue
            name = r.get("player") or r.get("name") or ""
            if wanted_cat and stat != wanted_cat: continue
            searchable = (name + " " + str(r.get("team","")) + " " + str(r.get("matchup","")) +
                           " " + str(r.get("opp_name","")) + " " + STAT_CONFIG[stat]["label"]).lower()
            if q and not any(tok in searchable for tok in q.split() if len(tok)>2 and tok not in
                             {"safest","bets","positive","edge","top","pick","picks","coach","alternate","alt","genuine","lines",
                              "best","minus","plus","favorite","underdog","american","odds","plays"}):
                # A category/side-only query is allowed; otherwise require a token.
                if not wanted_cat and not wanted_side and not intent_only: continue
            line = r.get("line") if is_alt else (r.get("dk_line") if r.get("dk_line") is not None else r.get("line"))
            if line is None: continue
            sides = [wanted_side] if wanted_side else ["OVER", "UNDER"]
            for side in sides:
                if is_alt:
                    odds = r.get("over_odds") if side == "OVER" else r.get("under_odds")
                else:
                    odds = (
                        r.get("dk_over_odds") if side == "OVER"
                        else r.get("dk_under_odds"))
                    if odds in (None, ""):
                        odds = r.get("over_odds") if side == "OVER" else r.get("under_odds")
                    if odds in (None, "") and side == "OVER":
                        odds = r.get("fd_odds") or r.get("odds")
                try:
                    american = float(str(odds).replace("+", ""))
                except Exception:
                    american = None
                if is_alt and mode == "alternate_minus" and (
                        american is None or american > -100 or american < -1000):
                    continue
                if is_alt and mode == "alternate_plus" and (
                        american is None or american <= 0):
                    continue
                implied = _nba_coach_implied(odds)
                if implied is None: continue
                signal = r
                if is_alt:
                    signal = next((x for x in standard if (x.get("player") or "").lower() == name.lower() and (x.get("stat") or "") == stat), r)
                model, vals = _nba_coach_prob(signal, side, float(line))
                if model is None: continue
                edge = model - implied
                if edge <= 0: continue
                out.append({"player":name,"team":r.get("team") or signal.get("team",""),"stat":stat,
                    "category":STAT_CONFIG[stat]["label"],"side":side,"line":line,"odds":odds,
                    "model_probability":round(model,5),"implied_probability":implied,
                    "edge":round(edge,5),"coach_edge":round(edge*100,2),
                    "book":_nba_coach_source(r),
                    "recent_average":(
                        round(sum(vals[-10:])/len(vals[-10:]),1)
                        if vals else None),
                    "game_log":vals[-10:],"opponent_history":signal.get("history") or "—",
                    "source":_nba_coach_source(r),"alternate":bool(is_alt),
                    "selection_reason":"Positive Coach Edge: empirical H/A opponent log exceeds American-odds implied probability."})
        return out
    is_alt_mode = mode in ("alternate", "alternate_minus", "alternate_plus")
    rows = make(alternate if is_alt_mode else standard, is_alt_mode)
    if mode == "alternate_minus":
        rows.sort(key=lambda x:(x["model_probability"], x["edge"]), reverse=True)
    else:
        rows.sort(key=lambda x:x["edge"], reverse=True)
    # Every Coach answer is a true Top 10 and may show a player only once.
    # Because rows are already sorted by Coach Edge, the first row retained for
    # a player is that player's strongest qualifying market/side.
    unique_rows = []
    seen_players = set()
    for row in rows:
        player_key = re.sub(r"[^a-z0-9]+", "", str(row.get("player") or "").lower())
        if not player_key or player_key in seen_players:
            continue
        seen_players.add(player_key)
        unique_rows.append(row)
    return unique_rows[:max(1, min(int(count or 10), 10))]

_NBA_COACH_FORM_PRESETS = (
    ("Hot Top 10", "hot"),
    ("Cold Top 10", "cold"),
    ("Double Cold Top 10", "double_cold"),
)

def _nba_coach_form_rows(hot_cold, form_key, query="", count=10):
    """Hot / Cold / Double Cold Coach presets: the section's own Top 10 rows,
    genuine side prices only, no edge gate, ranked by form strength, one play
    per player (strongest qualifying market)."""
    q = (query or "").lower()
    cat_terms = {k.lower(): k for k in STAT_CONFIG}
    cat_terms.update({v["label"].lower(): k for k, v in STAT_CONFIG.items()})
    wanted_cat = next((v for k, v in sorted(cat_terms.items(), key=lambda kv: -len(kv[0]))
                       if k in q), None)
    pool = []
    for r in hot_cold or []:
        if r.get("form_key") != form_key or r.get("is_overflow"):
            continue
        if wanted_cat and r.get("stat") != wanted_cat:
            continue
        odds = _nba_hc_price(r.get("odds"))
        implied = _nba_coach_implied(odds) if odds is not None else None
        if implied is None:
            continue
        model = r.get("app_probability")
        model = model / 100.0 if isinstance(model, (int, float)) else None
        edge = (model - implied) if model is not None else None
        pool.append({
            "player": r.get("player", ""), "team": r.get("team", ""), "stat": r.get("stat"),
            "category": r.get("stat_label") or _NBA_STAT_LABEL.get(r.get("stat"), r.get("stat")),
            "form_category": r.get("category"), "form": r.get("form"),
            "double_cold": bool(r.get("double_cold")),
            "side": r.get("pick"), "line": r.get("line"), "odds": odds,
            "model_probability": round(model, 5) if model is not None else None,
            "implied_probability": implied,
            "edge": round(edge, 5) if edge is not None else None,
            "coach_edge": round(edge * 100, 2) if edge is not None else None,
            "book": r.get("book") or "", "source": r.get("book") or "",
            "recent_hits": r.get("recent_hits"), "recent_total": r.get("recent_total"),
            "recent_average": r.get("recent_avg"),
            "game_log": [g.get("v") for g in (r.get("recent_games") or [])],
            "opponent_history": ",".join(str(g.get("v")) for g in (r.get("opp_games") or [])) or "—",
            "alternate": False, "_rank": r.get("rank") or 999,
            "_rate": (r.get("recent_hits") or 0) / max(1, r.get("recent_total") or 1),
            "_supports": bool(r.get("matchup_supports")),
            "selection_reason": (f"{r.get('form','')} form: {r.get('pick','')} in "
                                 f"{r.get('recent_hits')}/{r.get('recent_total')} of the last "
                                 f"current-season games (avg {r.get('recent_avg')})"
                                 + (f"; {r.get('opp_hits')}/{r.get('opp_total')} vs {r.get('opp','')}"
                                    if r.get("opp_total") else "") + "."),
        })
    pool.sort(key=lambda x: (-x["_rate"], -(x.get("recent_hits") or 0), -x["_supports"],
                             x["_rank"], str(x["player"]).casefold()))
    out, seen = [], set()
    for row in pool:
        key = re.sub(r"[^a-z0-9]+", "", str(row.get("player") or "").lower())
        if not key or key in seen:
            continue
        seen.add(key)
        for k in ("_rank", "_rate", "_supports"):
            row.pop(k, None)
        out.append(row)
    return out[:max(1, min(int(count or 10), 10))]

@app.post("/api/nba/coach-edge")
async def nba_coach_edge(request: Request):
    if not get_user(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Subscription required — please log in via moneypicksarena.com")
    body = await request.json()
    ds = str(body.get("date") or _nba_today().isoformat())
    mode = str(body.get("mode") or "all").lower()
    if mode in ("hot", "cold", "double_cold"):
        board = _cache_get("nba", ds)
        if not board:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Standard NBA result data is unavailable; run or load NBA picks first.")
        if not isinstance(board.get("hot_cold"), list):
            return {"date": ds, "results": [],
                    "message": "This saved run predates the Hot / Cold section. Run picks again to build it."}
        rows = _nba_coach_form_rows(board.get("hot_cold"), mode, str(body.get("query", "")),
                                    min(10, int(body.get("count", 10) or 10)))
        label = {"hot": "Hot", "cold": "Cold", "double_cold": "Double Cold"}[mode]
        if not rows:
            return {"date": ds, "results": [],
                    "message": f"No {label} play has a genuine sportsbook price on this board."}
        return {"date": ds, "results": rows, "form_preset": mode,
                "message": (f"{label} Top 10: strongest current-season form plays with a genuine "
                            f"sportsbook price, one play per player. No edge gate; App Prob/Edge "
                            f"show only when the app has a same-line estimate.")}
    if mode not in ("all", "standard", "alternate", "alternate_minus", "alternate_plus", "safest"): mode = "all"
    query_text = str(body.get("query", ""))
    if mode == "all" and re.search(r"(?:best\s*[-–]\s*alternate|minus|favorite|negative\s+odds)", query_text, re.I):
        mode = "alternate_minus"
    elif mode == "all" and re.search(r"(?:best\s*\+\s*alternate|plus|underdog|positive\s+odds)", query_text, re.I):
        mode = "alternate_plus"
    standard = _cache_get("nba", ds)
    if not standard:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Standard NBA result data is unavailable; run or load NBA picks first.")
    # Historical Coach requests warm the isolated alternate cache only after the
    # standard replay is available.  This is deliberately awaited so the UI
    # cannot remain in an unbounded "Loading" state.
    existing_alternate = _cache_get("nba_alternates", ds)
    alternate_result = None
    is_alt_mode = mode in ("alternate", "alternate_minus", "alternate_plus")
    if is_alt_mode and ds < _nba_today().isoformat():
        saved_alt = _nba_hist_sb_get({
            "app": f"eq.{_NBA_HIST_APP}",
            "date": f"eq.{ds}",
            "category": f"eq.{_NBA_HIST_ALT_COACH_CAT}",
            "side": "eq.ALL",
            "select": "detail",
            "limit": "1",
        }) or []
        saved_rows = (saved_alt[0].get("detail") or []) if saved_alt else []
        wanted_preset = (
            "Best - Alternate Plays" if mode == "alternate_minus"
            else "Best + Alternate Plays" if mode == "alternate_plus"
            else None)
        if wanted_preset:
            saved_rows = [
                row for row in saved_rows
                if row.get("preset") == wanted_preset
            ]
        saved_rows = saved_rows[:max(
            1, min(int(body.get("count", 10) or 10), 10))]
        if saved_rows:
            return {
                "date": ds,
                "results": saved_rows,
                "source": "NBA durable historical alternate Coach snapshot",
            }
    if ((is_alt_mode or mode == "safest") and ds < _nba_today().isoformat()
            and not (isinstance(existing_alternate, dict)
                     and existing_alternate.get("historical_alternates"))):
        games_for_alt = standard.get("games") or []
        if games_for_alt:
            alternate_result = await _nba_historical_alternates(ds, games_for_alt)
    # Keep the result in request scope: failed warmups are intentionally not
    # persisted, and rereading the cache here would otherwise erase their error.
    if isinstance(alternate_result, dict):
        alternate_doc = alternate_result
    elif ds < _nba_today().isoformat():
        alternate_doc = (existing_alternate if isinstance(existing_alternate, dict)
                         and existing_alternate.get("historical_alternates") else {})
    else:
        alternate_doc = existing_alternate or {}
    alternate = alternate_doc.get("props") if isinstance(alternate_doc, dict) else []
    if mode in ("alternate", "alternate_minus", "alternate_plus") and not alternate:
        from fastapi import HTTPException
        detail = (alternate_doc.get("error") if isinstance(alternate_doc, dict)
                  else None) or "NBA alternate lines are unavailable for this date."
        raise HTTPException(status_code=404, detail=detail)
    standard_rows = (standard.get("props_picks") or []) + (standard.get("props_nopick") or [])
    requested_board_keys = body.get("board_keys")
    if isinstance(requested_board_keys, list):
        allowed_board_keys = {
            str(key) for key in requested_board_keys
            if isinstance(key, str) and "|" in key
        }
        standard_rows = [
            row for row in standard_rows
            if (
                re.sub(r"[^a-z0-9]+", "",
                       str(row.get("player") or row.get("name") or "").lower())
                + "|" + str(row.get("stat") or row.get("stat_key") or "").upper()
            ) in allowed_board_keys
        ]
    qcount = re.search(r"\b(?:top|show|count|first)?\s*(\d{1,3})\s*(?:picks?|results?)?\b", str(body.get("query","")), re.I)
    requested_count = min(10, int(qcount.group(1))) if qcount else min(10, int(body.get("count", 10) or 10))
    rows = _nba_coach_rows(standard_rows, alternate, body.get("query",""), mode, requested_count)
    if not rows:
        response = {"date":ds,"results":[],
                    "message":("No player has hit 80%+ against today's opponent at this venue with a genuine sportsbook price."
                               if mode == "safest" else
                               "No eligible positive Coach Edge results for the requested intent.")}
    else:
        response = {
            "date":ds,
            "results":rows,
            "source":("NBA separate nba_alternates cache" if is_alt_mode
                      else "NBA loaded standard data"),
        }
    if (alternate_result and not alternate_result.get("ok", True)
            and mode in ("all", "standard", "safest")):
        response["alternate_error"] = alternate_result.get(
            "error", "Archived alternate lines are unavailable for this date.")
    return response

# ─── Separate NBA Coach Track Record ──────────────────────────────────────────
# This namespace is intentionally disjoint from the normal NBA ledger.  A Coach
# row is created only by this server after a complete pregame board was built;
# clients cannot submit custom text or odds to it.
_NBA_COACH_APP = "nba_coach_edge"
_NBA_COACH_SNAP_CAT = "__coach_picks__"
_NBA_COACH_LEDGER_CAT = "__coach_ledger__"
_NBA_COACH_DETAIL_CAT = "__coach_detail__"
_NBA_COACH_PRESETS = (
    ("Safest bets", "safest", "safest"),
    ("Positive Coach Edge", "positive edge", "all"),
    ("Points OVER", "points over", "standard"),
    ("Points UNDER", "points under", "standard"),
    ("Rebounds OVER", "rebounds over", "standard"),
    ("Rebounds UNDER", "rebounds under", "standard"),
    ("Assists OVER", "assists over", "standard"),
    ("Assists UNDER", "assists under", "standard"),
    ("3-Pointers OVER", "3-pointers over", "standard"),
    ("3-Pointers UNDER", "3-pointers under", "standard"),
    ("Pts+Reb+Ast OVER", "pra over", "standard"),
    ("Pts+Reb+Ast UNDER", "pra under", "standard"),
    ("Pts+Reb OVER", "pts+reb over", "standard"),
    ("Pts+Reb UNDER", "pts+reb under", "standard"),
    ("Pts+Ast OVER", "pts+ast over", "standard"),
    ("Pts+Ast UNDER", "pts+ast under", "standard"),
    ("Reb+Ast OVER", "reb+ast over", "standard"),
    ("Reb+Ast UNDER", "reb+ast under", "standard"),
    ("Blocks OVER", "blocks over", "standard"),
    ("Blocks UNDER", "blocks under", "standard"),
    ("Steals OVER", "steals over", "standard"),
    ("Steals UNDER", "steals under", "standard"),
    ("Best - Alternate Plays", "best minus alternate plays", "alternate_minus"),
    ("Best + Alternate Plays", "best plus alternate plays", "alternate_plus"),
)
_NBA_COACH_STAKE = 20.0

def _nba_coach_snapshot_row(date_str):
    rows = _nba_sb_get({"app":f"eq.{_NBA_COACH_APP}",
                        "category":f"eq.{_NBA_COACH_SNAP_CAT}",
                        "side":"eq.ALL","date":f"eq.{date_str}",
                        "select":"detail","limit":"1"})
    return (rows[0].get("detail") or {}) if rows else None

def _nba_coach_tipoff_freeze(result):
    tips = []
    for g in (result.get("games") or []):
        raw = g.get("tipoff")
        if not raw:
            return None
        try:
            tips.append(datetime.fromisoformat(str(raw).replace("Z","+00:00")))
        except Exception:
            return None
    return min(tips) if tips else None

def _nba_coach_complete_pregame(date_str, result):
    """Reject partial/error boards and any run after the slate has started."""
    if not isinstance(result, dict) or result.get("date") != date_str:
        return False
    if not _nba_official_board(result):
        return False
    if not result.get("games") or not result.get("odds_loaded"):
        return False
    if result.get("log") and any(str(x).lower().startswith("error") for x in result["log"]):
        return False
    freeze = _nba_coach_tipoff_freeze(result)
    if freeze is None:
        return False
    now = datetime.now(freeze.tzinfo) if freeze.tzinfo else datetime.utcnow()
    return now < freeze

def _nba_coach_capture_pregame(date_str: str, result: dict):
    """Last eligible pregame run wins; the earliest tipoff freezes the record."""
    if not _nba_coach_complete_pregame(date_str, result):
        return False
    old = _nba_coach_snapshot_row(date_str)
    if isinstance(old, dict) and old.get("frozen"):
        return False
    standard = (result.get("props_picks") or []) + (result.get("props_nopick") or [])
    alt_doc = _cache_get("nba_alternates", date_str) or {}
    alternate = alt_doc.get("props") if isinstance(alt_doc, dict) else []
    presets = {}
    for label, query, mode in _NBA_COACH_PRESETS:
        source_mode = (mode if mode in ("alternate", "alternate_minus", "alternate_plus", "safest")
                       else ("standard" if mode == "standard" else "all"))
        got = _nba_coach_rows(standard, alternate, query, source_mode, 200)
        # Preserve exact server-owned side/line/price, and never record invalid
        # prices. Alternate rows must be genuine cached rows, not inferred lines.
        clean = []
        for p in got:
            try:
                o = float(str(p.get("odds")).replace("+",""))
                line = float(p.get("line"))
            except Exception:
                continue
            if o < -1000 or not line == line:
                continue
            clean.append({**p, "preset": label, "captured_date": date_str})
        presets[label] = clean
    for label, form_key in _NBA_COACH_FORM_PRESETS:
        if not isinstance(result.get("hot_cold"), list):
            break
        presets[label] = [{**p, "preset": label, "captured_date": date_str}
                          for p in _nba_coach_form_rows(result.get("hot_cold"), form_key, "", 10)]
    detail = []
    seen = set()
    for label, items in presets.items():
        for p in items:
            key = (label, p.get("player","").lower(), p.get("stat"), p.get("side"), str(p.get("line")), str(p.get("odds")))
            if key not in seen:
                seen.add(key); detail.append(p)
    if not detail:
        return False
    freeze = _nba_coach_tipoff_freeze(result)
    doc = {"date":date_str, "captured_at":datetime.utcnow().isoformat()+"Z",
           "freeze_at":freeze.isoformat(), "frozen":False,
           "presets":presets, "detail":detail}
    return _nba_sb_upsert([{"app":_NBA_COACH_APP,"date":date_str,
        "category":_NBA_COACH_SNAP_CAT,"side":"ALL","wins":0,"losses":0,
        "locked":False,"detail":doc}], "app,date,category,side")

def _nba_coach_profit(odds, result):
    return _nba_american_profit_trk(odds, _NBA_COACH_STAKE, result)

def _nba_coach_grade_date(date_str, doc):
    box = {name: stats for name, stats in _nba_box_lookup(date_str).items()
           if stats.get("season_type") in (2, 3)}
    if not box:
        return None
    detail = []
    for p in (doc.get("detail") or []):
        actual = (box.get((p.get("player") or "").lower().strip()) or {}).get(p.get("stat"))
        final = (box.get((p.get("player") or "").lower().strip()) or {}).get("final")
        result = None
        try:
            if final and actual is not None:
                line = float(p["line"])
                result = "PUSH" if actual == line else ("WIN" if (actual > line) == (p.get("side") == "OVER") else "LOSS")
        except Exception:
            pass
        if result in ("WIN","LOSS"):
            detail.append({**p, "name":p.get("player",""), "result":result,
                "actual":actual,
                "profit":round(_nba_coach_profit(p.get("odds"),result),2)})
    return detail

def _nba_coach_update_track_ledger():
    today = _nba_today().isoformat()
    snaps = _nba_sb_get({"app":f"eq.{_NBA_COACH_APP}","category":f"eq.{_NBA_COACH_SNAP_CAT}",
                         "side":"eq.ALL","select":"date,detail","limit":"365"}) or []
    for row in snaps:
        ds, doc = row.get("date"), row.get("detail") or {}
        if not ds or ds >= today or not isinstance(doc, dict):
            continue
        graded = _nba_coach_grade_date(ds, doc)
        if graded is None:
            continue
        agg = {}
        for p in graded:
            c = agg.setdefault(p.get("preset") or p.get("category","?"), {"wins":0,"losses":0})
            c["wins"] += p["result"]=="WIN"; c["losses"] += p["result"]=="LOSS"
        _nba_sb_upsert([
            {"app":_NBA_COACH_APP,"date":ds,"category":_NBA_COACH_LEDGER_CAT,"side":"ALL",
             "wins":sum(x["wins"] for x in agg.values()),"losses":sum(x["losses"] for x in agg.values()),
             "locked":True,"detail":agg},
            {"app":_NBA_COACH_APP,"date":ds,"category":_NBA_COACH_DETAIL_CAT,"side":"ALL",
             "wins":0,"losses":0,"locked":True,"detail":graded}], "app,date,category,side")
        if isinstance(doc, dict) and not doc.get("frozen"):
            doc["frozen"] = True
            _nba_sb_upsert([{"app":_NBA_COACH_APP,"date":ds,"category":_NBA_COACH_SNAP_CAT,
                "side":"ALL","wins":0,"losses":0,"locked":False,"detail":doc}], "app,date,category,side")

def _nba_historical_coach_rows(date_str, alternate_track_rows=None):
    """Build view-only Coach categories from a saved point-in-time replay."""
    snapshots = _nba_sb_get({"app":f"eq.{_NBA_HIST_APP}",
        "date":f"eq.{date_str}","category":f"eq.{_NBA_HIST_SNAP_CAT}",
        "side":"eq.ALL","select":"detail","limit":"1"}) or []
    if snapshots:
        source = _nba_historical_grade_rows(
            date_str, snapshots[0].get("detail") or [])
        _nba_sb_upsert([{"app":_NBA_HIST_APP,"date":date_str,
            "category":_NBA_HIST_GRADED_CAT,"side":"ALL","wins":0,
            "losses":0,"locked":False,"detail":source}],
            "app,date,category,side")
    else:
        saved = _nba_sb_get({"app":f"eq.{_NBA_HIST_APP}",
            "date":f"eq.{date_str}","category":f"eq.{_NBA_HIST_GRADED_CAT}",
            "side":"eq.ALL","select":"detail","limit":"1"}) or []
        source = saved[0].get("detail") or [] if saved else []
    candidates = []
    safest_candidates = []
    for row in source:
        try:
            implied = _nba_coach_implied(row.get("odds"))
            model = float(row.get("pct")) / 100.0
            edge = model - implied
            american = float(str(row.get("odds")).replace("+", ""))
        except Exception:
            continue
        if implied is None:
            continue
        # Saved replays keep only the posted line's H/A record, so Safest can
        # apply its 80% rule there but cannot re-test lower/higher lines.
        if model >= _NBA_SAFEST_MIN_RATE and american >= -1000:
            safest_candidates.append({**row, "name":row.get("player",""),
                "model_probability":model, "implied_probability":implied,
                "coach_edge":edge * 100, "edge":edge,
                "category":row.get("stat_label") or row.get("stat",""),
                "result":row.get("result") or "PENDING",
                "profit":row.get("profit")})
        if edge <= 0:
            continue
        candidates.append({**row, "name":row.get("player",""),
            "model_probability":model, "implied_probability":implied,
            "coach_edge":edge * 100, "edge":edge,
            "category":row.get("stat_label") or row.get("stat",""),
            "result":row.get("result") or "PENDING",
            "profit":row.get("profit")})
    grouped = {}
    def add(label, rows, key):
        seen = set()
        for row in sorted(rows, key=key, reverse=True):
            player = _nba_hist_norm_name(row.get("player"))
            if not player or player in seen:
                continue
            seen.add(player)
            grouped.setdefault(label, []).append({**row, "preset":label})
            if len(grouped[label]) >= 10:
                break
    add("Positive Coach Edge", candidates, lambda r:r["edge"])
    add("Safest bets", safest_candidates, lambda r:(r["model_probability"], int(r.get("games") or 0)))
    for label, _, _ in _NBA_COACH_PRESETS:
        if label in ("Positive Coach Edge", "Safest bets",
                     "Best - Alternate Plays", "Best + Alternate Plays"):
            continue
        parts = label.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        market, side = parts
        add(label, [r for r in candidates if
            str(r.get("category")) == market and str(r.get("side")).upper() == side],
            lambda r:r["edge"])
    for row in alternate_track_rows or []:
        label = row.get("preset")
        if label in ("Best - Alternate Plays", "Best + Alternate Plays"):
            grouped.setdefault(label, []).append(row)
    return [row for rows in grouped.values() for row in rows]

async def _nba_historical_alt_track_rows(date_str):
    """Load or durably build the two exact historical alternate Coach lists."""
    saved = _nba_sb_get({"app":f"eq.{_NBA_HIST_APP}",
        "date":f"eq.{date_str}",
        "category":f"eq.{_NBA_HIST_ALT_COACH_CAT}",
        "side":"eq.ALL","select":"detail","limit":"1"}) or []
    if saved and isinstance(saved[0].get("detail"), list):
        return saved[0]["detail"], None

    standard_doc = _cache_get("nba", date_str) or {}
    if not standard_doc.get("games"):
        standard_doc = await run_analysis(date_str)
    if not standard_doc.get("games"):
        return [], "Historical standard replay is unavailable for this date."

    alternate_doc = _cache_get("nba_alternates", date_str) or {}
    if not (isinstance(alternate_doc, dict)
            and alternate_doc.get("historical_alternates")):
        alternate_doc = await _nba_historical_alternates(
            date_str, standard_doc.get("games") or [])
    alternate_rows = (alternate_doc.get("props") or []
                      if isinstance(alternate_doc, dict) else [])
    if not alternate_rows:
        return [], ((alternate_doc.get("error") if isinstance(
            alternate_doc, dict) else None)
            or "Archived alternate lines are unavailable for this date.")

    standard_rows = ((standard_doc.get("props_picks") or [])
                     + (standard_doc.get("props_nopick") or []))
    box = _nba_box_lookup(date_str)
    by_name = {_nba_hist_norm_name(name): values
               for name, values in (box or {}).items()}
    detail = []
    for label, mode, query in (
            ("Best - Alternate Plays", "alternate_minus",
             "best minus alternate plays"),
            ("Best + Alternate Plays", "alternate_plus",
             "best plus alternate plays")):
        selected = _nba_coach_rows(
            standard_rows, alternate_rows, query, mode, 10)
        for pick in selected:
            actual = (by_name.get(_nba_hist_norm_name(
                pick.get("player"))) or {}).get(pick.get("stat"))
            result = None
            if actual is not None:
                line = float(pick["line"])
                result = ("PUSH" if actual == line else
                          ("WIN" if (actual > line)
                           == (pick.get("side") == "OVER") else "LOSS"))
            detail.append({
                **pick, "name": pick.get("player", ""),
                "preset": label, "actual": actual,
                "result": result or "PENDING",
                "profit": (_nba_american_profit_trk(
                    pick.get("odds"), _NBA_HIST_STAKE, result)
                    if result else None),
            })
    if not _nba_sb_upsert([{"app":_NBA_HIST_APP,"date":date_str,
            "category":_NBA_HIST_ALT_COACH_CAT,"side":"ALL",
            "wins":sum(row.get("result") == "WIN" for row in detail),
            "losses":sum(row.get("result") == "LOSS" for row in detail),
            "locked":True,"detail":detail}],
            "app,date,category,side"):
        return [], "Historical alternate Coach snapshot could not be saved."
    return detail, None

@app.get("/api/nba/coach-track-record")
async def nba_coach_track_record(request: Request, date_str: str = "",
                                 source: str = "official"):
    if not get_user(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Subscription required — please log in via moneypicksarena.com")
    historical = source == "historical"
    if not historical:
        _nba_th.Thread(target=_nba_coach_update_track_ledger, daemon=True).start()
    if historical and date_str:
        alternate_track, alternate_error = (
            await _nba_historical_alt_track_rows(date_str))
        hist_detail = _nba_historical_coach_rows(
            date_str, alternate_track)
        rows = [{"date":date_str,"detail":hist_detail}] if hist_detail else []
    else:
        rows = _nba_sb_get({"app":f"eq.{_NBA_COACH_APP}","category":f"eq.{_NBA_COACH_DETAIL_CAT}",
                            "locked":"eq.true","select":"date,detail","limit":"1000"}) or []
    dates, all_rows = [], []
    for r in rows:
        det = r.get("detail") or []
        wins = sum(x.get("result")=="WIN" for x in det); losses = sum(x.get("result")=="LOSS" for x in det)
        pl = round(sum(x.get("profit") or 0 for x in det),2); staked=(wins+losses)*_NBA_COACH_STAKE
        dates.append({"date":r.get("date"),"wins":wins,"losses":losses,"net_pl":pl,
                      "roi":round(pl/staked*100,1) if staked else 0,"detail":det})
        all_rows.extend(det)
    cats = {}
    for x in all_rows:
        c=cats.setdefault(x.get("preset") or x.get("category","?"),{"wins":0,"losses":0,"net_pl":0.0})
        c["wins"] += x.get("result")=="WIN"; c["losses"] += x.get("result")=="LOSS"; c["net_pl"] += x.get("profit") or 0
    summary=[]
    for k,c in cats.items():
        n=c["wins"]+c["losses"]; st=n*_NBA_COACH_STAKE
        summary.append({"category":k,**c,"net_pl":round(c["net_pl"],2),"rate":round(c["wins"]/n*100,1) if n else 0,"roi":round(c["net_pl"]/st*100,1) if st else 0})
    response = {"app":_NBA_COACH_APP,"source":"historical" if historical else "official",
            "dates":sorted(dates,key=lambda x:x.get("date") or "",reverse=True),
            "by_category":summary,"stake":_NBA_COACH_STAKE}
    if historical and date_str and alternate_error:
        response["alternate_error"] = alternate_error
    return response

# ─── Immutable NBA Historical Track Record ─────────────────────────────────────
# This is deliberately a third ledger namespace.  The replay board is the sole
# writer: browser supplied picks, the live ledger, and Coach Edge cannot enter it.
_NBA_HIST_APP = "nba_historical_replay"
_NBA_HIST_SNAP_CAT = "__snapshot__"
_NBA_HIST_GRADED_CAT = "__graded__"
_NBA_HIST_ALT_COACH_CAT = "__alternate_coach__"
_NBA_HIST_GRADING_VERSION = 2
_NBA_HIST_STATUS_CAT = "__month_status__"
_NBA_HIST_STAKE = 20.0
_NBA_HIST_LOCK = _nba_th.Lock()
_NBA_HIST_TASK = None
_NBA_HIST_PROGRESS = {}

def _nba_hist_sb_get(params):
    """Strict historical read: an unavailable database is not an empty slate."""
    if not _NBA_SB_URL:
        raise RuntimeError("Historical Supabase storage unavailable")
    h={"apikey":_NBA_SB_KEY,"Authorization":f"Bearer {_NBA_SB_KEY}","Accept":"application/json"}
    try:
        r=httpx.get(f"{_NBA_SB_URL}/rest/v1/{_NBA_SB_TBL}",headers=h,params=params,timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Historical Supabase GET failed ({r.status_code})")
        data=r.json()
        if not isinstance(data,list):
            raise RuntimeError("Historical Supabase GET returned malformed data")
        return data
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Historical Supabase GET failed: {exc}") from exc

def _nba_hist_sb_insert_ignore(rows):
    if not _NBA_SB_URL or not rows:
        return False
    url=f"{_NBA_SB_URL}/rest/v1/{_NBA_SB_TBL}?on_conflict=app,date,category,side"
    h={"apikey":_NBA_SB_KEY,"Authorization":f"Bearer {_NBA_SB_KEY}",
       "Content-Type":"application/json",
       "Prefer":"resolution=ignore-duplicates,return=minimal"}
    try:
        r=httpx.post(url,headers=h,json=rows,timeout=20)
        return r.status_code in (200,201,204)
    except Exception:
        return False

def _nba_hist_norm_name(value):
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", value.lower())

def _nba_hist_snapshot_rows(result):
    """Normalize only qualifying props_picks; preserve the replay evidence."""
    out = []
    for p in result.get("props_picks") or []:
        side = str(p.get("side") or p.get("pick") or "").upper()
        if side not in ("OVER", "UNDER"):
            continue
        try:
            odds = float(str(p.get("fd_odds") if p.get("fd_odds") is not None else p.get("odds")).replace("+",""))
            line = float(p.get("line"))
        except Exception:
            continue
        if odds < -1000:
            continue
        out.append({
            "player": p.get("player") or "", "player_id": p.get("player_id"),
            "team": p.get("team") or "", "opp": p.get("opp") or "",
            "location": p.get("location") or "", "stat": p.get("stat") or "",
            "stat_label": p.get("stat_label") or _NBA_STAT_LABEL.get(p.get("stat"), p.get("stat","")),
            "side": side, "line": line, "odds": odds,
            "book": p.get("book") or p.get("bookmaker") or p.get("bookmaker_label") or "",
            "snapshot": p.get("historical_snapshot") or result.get("historical_snapshot"),
            "snapshot_version": 3,
            "mpg": p.get("mpg"), "games": p.get("games") or 0,
            "hits": p.get("hits") or 0, "pct": p.get("pct"),
            "form_badge": p.get("form_badge"),
            "form_change_pct": p.get("form_change_pct"),
            "form_l5_avg": p.get("form_l5_avg"),
            "form_baseline_avg": p.get("form_baseline_avg"),
            "form_games": p.get("form_games"),
            "recent_avg": p.get("recent_avg"),
            "recent_glog": p.get("recent_glog") or [],
            "actual": p.get("actual"), "result": p.get("result"),
            "profit": p.get("profit"),
        })
    return out

def _nba_historical_saved_board(date_str, games, log=None):
    """Rehydrate the main replay board from its durable Supabase snapshot."""
    snapshots = _nba_hist_sb_get({
        "app": f"eq.{_NBA_HIST_APP}",
        "date": f"eq.{date_str}",
        "category": f"eq.{_NBA_HIST_SNAP_CAT}",
        "side": "eq.ALL",
        "select": "detail",
        "limit": "1",
    }) or []
    if not snapshots:
        return None
    snapshot_rows = snapshots[0].get("detail") or []
    if not snapshot_rows or any(row.get("snapshot_version") != 3 for row in snapshot_rows):
        return None
    graded = _nba_hist_sb_get({
        "app": f"eq.{_NBA_HIST_APP}",
        "date": f"eq.{date_str}",
        "category": f"eq.{_NBA_HIST_GRADED_CAT}",
        "side": "eq.ALL",
        "select": "detail",
        "limit": "1",
    }) or []
    source = _nba_historical_current_rows(
        snapshot_rows, (graded[0].get("detail") or []) if graded else [])
    rows = []
    for saved in source:
        side = str(saved.get("side") or "").upper()
        stat = saved.get("stat") or ""
        if side not in ("OVER", "UNDER") or stat not in STAT_CONFIG:
            continue
        pct = float(saved.get("pct") or 0)
        row = {
            **saved,
            "pick": side,
            "side": side,
            "line": saved.get("line"),
            "fd_line": saved.get("line"),
            "fd_odds": saved.get("odds"),
            "dk_line": saved.get("line"),
            "dk_over_odds": saved.get("odds") if side == "OVER" else None,
            "dk_under_odds": saved.get("odds") if side == "UNDER" else None,
            "bookmaker": saved.get("book") or "",
            "bookmaker_label": saved.get("book") or "",
            "emoji": STAT_CONFIG[stat]["emoji"],
            "conf": "STRONG" if pct >= 70 else "LEAN",
            "history": f"{saved.get('hits') or 0}/{saved.get('games') or 0}",
            "matchup": f"{saved.get('team') or ''} vs {saved.get('opp') or ''}",
            "historical_snapshot": saved.get("snapshot"),
            "historical_replay": True,
        }
        rows.append(row)
    if not rows:
        return None
    rows.sort(key=lambda row: (
        -(row.get("pct") or 0), -(row.get("games") or 0),
        row.get("player") or "", row.get("stat") or ""))
    by_stat = {
        stat: [row for row in rows if row.get("stat") == stat]
        for stat in STAT_CONFIG
    }
    top, seen, depth = [], set(), 0
    while len(top) < TOP_N:
        added = False
        for stat in STAT_CONFIG:
            pool = by_stat.get(stat) or []
            if depth >= len(pool):
                continue
            added = True
            candidate = pool[depth]
            player_key = _nn(candidate.get("player", ""))
            if player_key in seen:
                continue
            seen.add(player_key)
            top.append(candidate)
            if len(top) >= TOP_N:
                break
        if not added:
            break
        depth += 1
    snapshot = next((row.get("snapshot") for row in rows
                     if row.get("snapshot")), None)
    saved_log = list(log or [])
    saved_log.extend([
        f"Historical Sportsbook Replay · loaded {len(rows)} qualified plays "
        f"from the durable Supabase snapshot for {date_str}.",
        "No new archived-odds request was made.",
        "Final ESPN grading and exact saved lines, prices, and books were restored.",
    ])
    return {
        "date": date_str,
        "picks": top,
        "all_picks": rows,
        "games": games,
        "log": saved_log,
        "total": len(rows),
        "odds_loaded": True,
        "historical_replay": True,
        "historical_snapshot": snapshot,
        "historical_replay_schema": HISTORICAL_REPLAY_SCHEMA,
        "props_picks": rows,
        "props_nopick": [],
        "saved_historical_replay": True,
    }


def _nba_historical_current_rows(snapshot_rows, graded_rows):
    """The current snapshot owns selections; grades never introduce old picks."""
    def identity(row):
        return (str(row.get("player_id") or row.get("player") or ""),
                row.get("stat"), row.get("side"), row.get("line"),
                row.get("snapshot_version"))
    previous = {identity(row): row for row in graded_rows or []}
    rows = []
    for snapshot in snapshot_rows or []:
        graded = previous.get(identity(snapshot))
        # Reuse only grades whose source evidence still matches this snapshot.
        fields = ("pct", "games", "hits", "odds", "book", "snapshot", "recent_glog")
        if graded and all(graded.get(k) == snapshot.get(k) for k in fields):
            rows.append({**graded, **{k: v for k, v in snapshot.items()
                                     if k not in ("actual", "result", "profit")}})
        else:
            rows.append(dict(snapshot))
    return rows


def _nba_historical_persist_snapshot(date_str, result):
    rows = _nba_hist_snapshot_rows(result)
    if not rows:
        return False
    existing = _nba_hist_sb_get({"app":f"eq.{_NBA_HIST_APP}",
        "date":f"eq.{date_str}","category":f"eq.{_NBA_HIST_SNAP_CAT}",
        "side":"eq.ALL","select":"detail","limit":"1"}) or []
    previous = existing[0].get("detail") or [] if existing else []
    if previous and all(row.get("snapshot_version") == 3 for row in previous):
        return True
    return _nba_sb_upsert([{"app":_NBA_HIST_APP,"date":date_str,
        "category":_NBA_HIST_SNAP_CAT,"side":"ALL","wins":0,"losses":0,
        "locked":False,"detail":rows}], "app,date,category,side")

def _nba_historical_grade_rows(date_str, rows, box=None):
    box = _nba_box_lookup(date_str) if box is None else box
    slate_complete = _NBA_BOX_COMPLETE_CACHE.get(date_str, True)
    by_id = {str(v.get("player_id")):v for v in (box or {}).values() if v.get("player_id")}
    by_name = {_nba_hist_norm_name(k):v for k,v in (box or {}).items()}
    out = []
    for row in rows or []:
        # Terminal result/actual are immutable; recover a missing profit from
        # the immutable odds rather than turning a terminal row into pending.
        if (row.get("result") in ("WIN","LOSS","PUSH","VOID")
                and row.get("grading_version") == _NBA_HIST_GRADING_VERSION):
            terminal = dict(row)
            if terminal.get("profit") is None and terminal["result"] != "VOID":
                terminal["profit"] = _nba_american_profit_trk(
                    terminal.get("odds"), _NBA_HIST_STAKE, terminal["result"])
            out.append(terminal); continue
        p = by_id.get(str(row.get("player_id"))) or by_name.get(_nba_hist_norm_name(row.get("player")))
        actual = p.get(row.get("stat")) if p else None
        graded = dict(row)
        if slate_complete and not p and row.get("player_id"):
            graded["result"], graded["actual"], graded["profit"] = "VOID", None, 0.0
        elif slate_complete and p and p.get("final") and actual is not None:
            line = float(row["line"])
            graded["actual"] = actual
            graded["result"] = ("PUSH" if actual == line else
                ("WIN" if (actual > line) == (row.get("side") == "OVER") else "LOSS"))
            graded["profit"] = _nba_american_profit_trk(row.get("odds"), _NBA_HIST_STAKE, graded["result"])
        else:
            graded["result"], graded["profit"] = None, None
        graded["grading_version"] = _NBA_HIST_GRADING_VERSION
        out.append(graded)
    return out

def _nba_historical_summary(rows):
    cats = {}
    w=l=pu=pend=void=0; net=staked=0.0
    for r in rows or []:
        res=r.get("result")
        key=(r.get("stat_label") or r.get("stat") or "Unknown", r.get("side") or "")
        c=cats.setdefault(key,{"category":key[0],"side":key[1],"wins":0,"losses":0,"push":0,"void":0,"pending":0,"net":0.0,"staked":0.0})
        if res=="WIN": w+=1;c["wins"]+=1
        elif res=="LOSS": l+=1;c["losses"]+=1
        elif res=="PUSH": pu+=1;c["push"]+=1
        elif res=="VOID": void+=1;c["void"]+=1
        else: pend+=1;c["pending"]+=1
        if res in ("WIN","LOSS","PUSH"):
            profit=float(r.get("profit") or 0); net+=profit; staked+=_NBA_HIST_STAKE
            c["net"]+=profit;c["staked"]+=_NBA_HIST_STAKE
    decided=w+l+pu
    def finish(c):
        n=c["wins"]+c["losses"]+c["push"]
        c["net"]=round(c["net"],2); c["win_pct"]=round(c["wins"]/(c["wins"]+c["losses"])*100,1) if c["wins"]+c["losses"] else None
        c["roi"]=round(c["net"]/c["staked"]*100,1) if c["staked"] else None
        c.pop("staked",None); return c
    return {"wins":w,"losses":l,"push":pu,"void":void,"pending":pend,
            "win_pct":round(w/(w+l)*100,1) if w+l else None,
            "net":round(net,2),"roi":round(net/staked*100,1) if staked else None,
            "by_category_side":[finish(v) for v in cats.values()]}

def _nba_hist_month_bounds(month):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}", str(month)):
            return None,None
        first=date.fromisoformat(str(month)+"-01")
        nxt=date(first.year+first.month//12, first.month%12+1, 1)
        last=nxt-timedelta(days=1)
        lo=max(first,date.fromisoformat(HISTORICAL_ODDS_MIN_DATE))
        hi=min(last,_nba_today()-timedelta(days=1))
        if first > _nba_today()-timedelta(days=1) or last < date.fromisoformat(HISTORICAL_ODDS_MIN_DATE):
            return None,None
        return lo,hi
    except Exception:
        return None,None

def _nba_hist_status(month):
    try:
        status_date = date.fromisoformat(str(month)+"-01").isoformat()
    except Exception:
        return {}
    rows=_nba_hist_sb_get({"app":f"eq.{_NBA_HIST_APP}","category":f"eq.{_NBA_HIST_STATUS_CAT}",
                      "side":"eq.ALL","date":f"eq.{status_date}","select":"detail","limit":"1"})
    return (rows[0].get("detail") or {}) if rows else (_NBA_HIST_PROGRESS.get(month) or {})

def _nba_hist_existing_dates(month):
    """Derive resume state from durable snapshots, not only the status row."""
    lo,hi=_nba_hist_month_bounds(month)
    if not lo or not hi:
        return set()
    rows=_nba_hist_sb_get({"app":f"eq.{_NBA_HIST_APP}","category":f"eq.{_NBA_HIST_GRADED_CAT}",
                      "side":"eq.ALL",
                      "and":f"(date.gte.{lo.isoformat()},date.lte.{hi.isoformat()})",
                      "select":"date,detail","limit":"500"})
    terminal={"WIN","LOSS","PUSH","VOID"}
    return {r.get("date") for r in rows or []
            if r.get("date") and isinstance(r.get("detail"),list)
            and r.get("detail") and all(x.get("result") in terminal for x in r["detail"])}

def _nba_hist_save_status(month, status):
    status["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    _NBA_HIST_PROGRESS[month]=status
    try:
        status_date = date.fromisoformat(str(month)+"-01").isoformat()
    except Exception:
        return False
    ok = _nba_sb_upsert([{"app":_NBA_HIST_APP,"date":status_date,"category":_NBA_HIST_STATUS_CAT,
        "side":"ALL","wins":0,"losses":0,"locked":bool(status.get("_locked",False)),"detail":status}],
        "app,date,category,side")
    return bool(ok)

def _nba_hist_claim_month(month):
    """CAS claim in the ledger; process lock alone is not restart-safe."""
    status_date=date.fromisoformat(str(month)+"-01").isoformat()
    seed={"app":_NBA_HIST_APP,"date":status_date,"category":_NBA_HIST_STATUS_CAT,
          "side":"ALL","wins":0,"losses":0,"locked":False,"detail":{"month":month}}
    if not _nba_hist_sb_insert_ignore([seed]):
        return False
    if not _NBA_SB_URL:
        return False
    url=f"{_NBA_SB_URL}/rest/v1/{_NBA_SB_TBL}?app=eq.{_NBA_HIST_APP}&date=eq.{status_date}&category=eq.{_NBA_HIST_STATUS_CAT}&side=eq.ALL&locked=eq.false"
    h={"apikey":_NBA_SB_KEY,"Authorization":f"Bearer {_NBA_SB_KEY}",
       "Content-Type":"application/json","Prefer":"return=representation"}
    try:
        r=httpx.patch(url,headers=h,json={"locked":True},timeout=20)
        return r.status_code in (200,201,204) and (r.status_code != 200 or bool(r.json()))
    except Exception:
        return False

def _nba_hist_recover_stale_claim(month, observed):
    """Release only the exact stale lease observed by this requester."""
    stamp=str((observed or {}).get("updated_at") or "")
    if not stamp:
        return False
    try:
        if (datetime.utcnow()-datetime.fromisoformat(stamp.replace("Z",""))).total_seconds() < 900:
            return False
    except Exception:
        return False
    from urllib.parse import quote
    status_date=date.fromisoformat(str(month)+"-01").isoformat()
    url=(f"{_NBA_SB_URL}/rest/v1/{_NBA_SB_TBL}?app=eq.{_NBA_HIST_APP}"
         f"&date=eq.{status_date}&category=eq.{_NBA_HIST_STATUS_CAT}&side=eq.ALL"
         f"&locked=eq.true&detail->>updated_at=eq.{quote(stamp,safe='')}")
    h={"apikey":_NBA_SB_KEY,"Authorization":f"Bearer {_NBA_SB_KEY}",
       "Content-Type":"application/json","Prefer":"return=representation"}
    try:
        r=httpx.patch(url,headers=h,json={"locked":False},timeout=20)
        return r.status_code in (200,201,204) and (r.status_code != 200 or bool(r.json()))
    except Exception:
        return False

async def _nba_hist_run_month(month):
    lo,hi=_nba_hist_month_bounds(month)
    if not lo or lo>hi: return
    status=_nba_hist_status(month)
    done=set(status.get("completed_dates") or []) | _nba_hist_existing_dates(month)
    status["_locked"]=True
    status.update({"month":month,"running":True,"errors":status.get("errors") or [],"completed_dates":sorted(done)})
    _nba_hist_save_status(month,status)
    cur=lo
    try:
        while cur<=hi:
            ds=cur.isoformat(); status["current_date"]=ds
            if ds not in done:
                try:
                    snap=_nba_hist_sb_get({"app":f"eq.{_NBA_HIST_APP}","category":f"eq.{_NBA_HIST_SNAP_CAT}",
                                           "side":"eq.ALL","date":f"eq.{ds}","select":"detail","limit":"1"})
                    if not snap:
                        result=await run_analysis(ds)       # cache reuse; never force
                        durable = bool(result.get("no_games"))
                        if result.get("historical_unavailable"):
                            status["errors"].append({"date":ds,"error":"unavailable"})
                            durable = False
                        elif result.get("odds_loaded"):
                            durable = _nba_historical_persist_snapshot(ds,result)
                            snap=_nba_hist_sb_get({"app":f"eq.{_NBA_HIST_APP}","category":f"eq.{_NBA_HIST_SNAP_CAT}",
                                              "side":"eq.ALL","date":f"eq.{ds}","select":"detail","limit":"1"})
                        else:
                            durable = False
                    if snap:
                        graded=_nba_historical_grade_rows(ds,snap[0].get("detail") or [])
                        durable = bool(_nba_sb_upsert([{"app":_NBA_HIST_APP,"date":ds,"category":_NBA_HIST_GRADED_CAT,
                          "side":"ALL","wins":0,"losses":0,"locked":False,"detail":graded}],"app,date,category,side"))
                        durable = durable and bool(_NBA_BOX_COMPLETE_CACHE.get(ds, False)) and all(
                            x.get("result") in ("WIN","LOSS","PUSH","VOID") for x in graded)
                    if durable:
                        done.add(ds); status["completed_dates"]=sorted(done)
                    else:
                        status["errors"].append({"date":ds,"error":"historical snapshot write failed"})
                except Exception as exc:
                    status["errors"].append({"date":ds,"error":str(exc)})
            if not _nba_hist_save_status(month,status):
                raise RuntimeError("historical progress storage unavailable")
            cur+=timedelta(days=1)
    finally:
        status["_locked"]=False
        status.update({"running":False,"current_date":None,"complete":False})
        if len(done) == (hi-lo).days+1:
            status["complete"] = True
        _nba_hist_save_status(month,status)

@app.get("/api/nba/historical-track-record")
async def nba_historical_track_record(request: Request, month: str = ""):
    from fastapi import HTTPException
    if not get_user(request):
        raise HTTPException(status_code=401, detail="Subscription required — please log in via moneypicksarena.com")
    lo,hi=_nba_hist_month_bounds(month) if month else (None,None)
    params={"app":f"eq.{_NBA_HIST_APP}","category":f"eq.{_NBA_HIST_SNAP_CAT}",
            "side":"eq.ALL","select":"date,detail","limit":"500"}
    records=[]
    snapshots=_nba_hist_sb_get(params)
    graded_rows=_nba_hist_sb_get({"app":f"eq.{_NBA_HIST_APP}","category":f"eq.{_NBA_HIST_GRADED_CAT}",
                                  "side":"eq.ALL","select":"date,detail","limit":"500"})
    graded_by_date={r.get("date"):r.get("detail") or [] for r in graded_rows}
    for row in snapshots:
        ds=row.get("date","")
        if month and (not lo or not (lo.isoformat()<=ds<=hi.isoformat())): continue
        previous=graded_by_date.get(ds)
        graded=_nba_historical_grade_rows(
            ds, _nba_historical_current_rows(row.get("detail") or [], previous or []))
        if previous != graded:
            if not _nba_sb_upsert([{"app":_NBA_HIST_APP,"date":ds,"category":_NBA_HIST_GRADED_CAT,
              "side":"ALL","wins":0,"losses":0,"locked":False,"detail":graded}],
              "app,date,category,side"):
                raise HTTPException(status_code=503, detail="Historical grade storage unavailable")
        summary=_nba_historical_summary(graded)
        records.append({"date":ds,**summary,"detail":graded})
    all_rows=[x for d in records for x in d["detail"]]
    status=_nba_hist_status(month) if month else {}
    unavailable=[e.get("date") for e in status.get("errors",[]) if e.get("error")=="unavailable"]
    return {"app":_NBA_HIST_APP,"month":month or None,"summary":_nba_historical_summary(all_rows),
            "dates":sorted(records,key=lambda x:x["date"],reverse=True),
            "unavailable_dates":sorted(set(x for x in unavailable if x))}

@app.post("/api/nba/historical-track-record/month")
async def nba_historical_month_start(request: Request):
    from fastapi import HTTPException
    tok=get_user(request)
    if not tok or not _is_admin_token(tok):
        raise HTTPException(status_code=403, detail="Admin authorization required")
    body=await request.json()
    month=str(body.get("month",""))
    lo,hi=_nba_hist_month_bounds(month)
    if not lo or lo>hi: raise HTTPException(status_code=400, detail="Month is outside supported historical bounds")
    global _NBA_HIST_TASK
    with _NBA_HIST_LOCK:
        if _NBA_HIST_TASK is not None and not _NBA_HIST_TASK.done():
            return {"started":False,"status":_nba_hist_status(month),"reason":"already running"}
        status=_nba_hist_status(month)
        if status.get("running"):
            try:
                last=datetime.fromisoformat(str(status.get("updated_at","")).replace("Z","+00:00"))
                age=(datetime.now(last.tzinfo)-last).total_seconds()
            except Exception:
                age=301
            if age < 300:
                return {"started":False,"status":status,"reason":"already running"}
        status.update({"month":month,"running":True,"complete":False,
                       "current_date":None,"errors":status.get("errors") or [],
                       "completed_dates":status.get("completed_dates") or []})
        if not _nba_hist_claim_month(month):
            if _nba_hist_recover_stale_claim(month, status) and _nba_hist_claim_month(month):
                pass
            else:
                raise HTTPException(status_code=409, detail="Historical month is already running; retry after status clears")
        status["_locked"]=True
        if not _nba_hist_save_status(month,status):
            raise HTTPException(status_code=503, detail="Historical progress storage unavailable")
        _NBA_HIST_TASK=asyncio.create_task(_nba_hist_run_month(month))
        return {"started":True,"month":month,"status":status}

@app.get("/api/nba/historical-track-record/month")
async def nba_historical_month_status(request: Request, month: str = ""):
    from fastapi import HTTPException
    tok=get_user(request)
    if not tok or not _is_admin_token(tok):
        raise HTTPException(status_code=403, detail="Admin authorization required")
    return {"month":month,"status":_nba_hist_status(month)}

# ─── Routes ───────────────────────────────────────────────────────────────────
# ─── NBA Game Predictor ─────────────────────────────────────────────────────────
# Team-level winner / spread / total model built only from free ESPN data plus
# Basketball-Reference referee tendencies.  Every adjustment is bounded and shown
# on the card.  Pregame forecasts are saved to their own ledger namespace and are
# never mixed into player-prop, Overflow, or Coach records.
_NBA_GP_ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
_NBA_GP_DIR = _CACHE_DIR / "nba_gp"
_NBA_GP_MEM: Dict[str, Any] = {}
_NBA_GP_APP = "nba_game_predictor"
_NBA_GP_SNAP_CAT = "__gp_snapshot__"
_NBA_GP_GRADED_CAT = "__gp_graded__"
_NBA_GP_STAKE = 100.0
_NBA_GP_LOCK = _nba_th.Lock()
_NBA_GP_SD = 12.5            # NBA final-margin standard deviation (points)
_NBA_GP_LEAGUE_PTS = 114.0   # used only when a team has no usable history
_NBA_GP_ATS_MIN = 1.5        # model-vs-spread points needed for an ATS pick
_NBA_GP_OU_MIN = 2.0         # model-vs-total points needed for an O/U pick
_NBA_GP_ABSENT = {"out": 1.0, "doubtful": 0.8, "questionable": 0.45,
                  "day-to-day": 0.35, "probable": 0.0}
_NBA_GP_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
try:
    _NBA_GP_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


def _nba_gp_clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _nba_gp_et_date(iso) -> str:
    try:
        from zoneinfo import ZoneInfo as _ZI
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(_ZI("America/New_York")).date().isoformat()
    except Exception:
        return str(iso or "")[:10]


def _nba_gp_today_et() -> str:
    try:
        from zoneinfo import ZoneInfo as _ZI
        return datetime.now(_ZI("America/New_York")).date().isoformat()
    except Exception:
        return _nba_today().isoformat()


def _nba_gp_mem_get(key, ttl):
    hit = _NBA_GP_MEM.get(key)
    if hit is not None and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _nba_gp_mem_set(key, value):
    _NBA_GP_MEM[key] = (time.time(), value)
    if len(_NBA_GP_MEM) > 900:
        for k in sorted(_NBA_GP_MEM, key=lambda k: _NBA_GP_MEM[k][0])[:300]:
            _NBA_GP_MEM.pop(k, None)
    return value


def _nba_gp_score(comp) -> Optional[float]:
    s = comp.get("score")
    if isinstance(s, dict):
        s = s.get("value", s.get("displayValue"))
    try:
        return float(s)
    except Exception:
        return None


class _NbaGpRunBusy(Exception):
    pass


def _nba_gp_check_run():
    if _NBA_RUNS_ACTIVE > 0:
        raise _NbaGpRunBusy()


_NBA_GP_BUILD_LOCK = None


def _nba_gp_build_lock():
    global _NBA_GP_BUILD_LOCK
    if _NBA_GP_BUILD_LOCK is None:
        _NBA_GP_BUILD_LOCK = asyncio.Lock()
    return _NBA_GP_BUILD_LOCK


async def _nba_gp_team_games(client, team_id, season, stype, sem):
    """Completed games for one team/season/type (compact, cached)."""
    key = f"tg:{team_id}:{season}:{stype}"
    done = int(season) < _nba_current_espn_season()
    hit = _nba_gp_mem_get(key, 10 ** 9 if done else 600)
    if hit is not None:
        return hit
    path = _NBA_GP_DIR / f"team_{team_id}_{season}_{stype}.json"
    if done:
        try:
            if path.exists():
                return _nba_gp_mem_set(key, json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    data = None
    for attempt in range(2):
        _nba_gp_check_run()
        try:
            async with sem:
                _nba_gp_check_run()
                r = await client.get(f"{_NBA_GP_ESPN}/teams/{team_id}/schedule",
                                     params={"season": season, "seasontype": stype})
            if r.status_code == 200:
                data = r.json()
                break
        except _NbaGpRunBusy:
            raise
        except Exception:
            pass
        await asyncio.sleep(0.5)
    if not isinstance(data, dict):
        raise RuntimeError(f"ESPN schedule unavailable for team {team_id} {season}")
    out = []
    for e in data.get("events") or []:
        c = (e.get("competitions") or [{}])[0]
        if not (((c.get("status") or {}).get("type") or {}).get("completed")):
            continue
        comps = c.get("competitors") or []
        me = next((x for x in comps if str((x.get("team") or {}).get("id")) == str(team_id)), None)
        op = next((x for x in comps if x is not me), None)
        if not me or not op:
            continue
        pf, pa = _nba_gp_score(me), _nba_gp_score(op)
        if pf is None or pa is None or (pf == 0 and pa == 0):
            continue
        out.append({"id": str(e.get("id", "")), "date": _nba_gp_et_date(e.get("date")),
                    "tip": e.get("date", ""), "home": me.get("homeAway") == "home",
                    "neutral": bool(c.get("neutralSite")),
                    "opp_id": str((op.get("team") or {}).get("id", "")),
                    "opp": _norm_abbr((op.get("team") or {}).get("abbreviation", "")),
                    "pf": pf, "pa": pa, "stype": int(stype), "season": int(season)})
    out.sort(key=lambda g: g.get("tip") or "")
    if done:
        try:
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(out), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            pass
    return _nba_gp_mem_set(key, out)


def _nba_gp_parse_refs(html_text: str) -> dict:
    i = html_text.find('id="rs_raw"')
    if i < 0:
        return {}
    j = html_text.find("</table>", i)
    table = html_text[i:j if j > 0 else None]
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
        cells = {k: re.sub(r"<[^>]+>", "", v).strip() for k, v in
                 re.findall(r'data-stat="([a-z_]+)"[^>]*>(.*?)</t[dh]>', row, re.S)}
        name = cells.get("referee") or ""
        if not name or not str(cells.get("g", "")).isdigit():
            continue

        def f(k):
            try:
                return float(str(cells.get(k, "")).replace("+", ""))
            except Exception:
                return None
        out[_nn(name)] = {"name": name, "g": int(cells["g"]), "pf": f("pf_per_g"),
                          "pf_rel": f("pf_per_g_rel"), "fta": f("fta_per_g"),
                          "fta_rel": f("fta_per_g_rel"), "pts": f("pts_per_g"),
                          "pts_rel": f("pts_per_g_rel"), "home_win": f("home_win_loss_pct")}
    return out


async def _nba_gp_ref_season(client, end_year: int):
    key = f"refs:{end_year}"
    done = int(end_year) < _nba_current_espn_season()
    ttl = 10 ** 9 if done else 12 * 3600
    hit = _nba_gp_mem_get(key, ttl)
    if hit is not None:
        return hit or None
    path = _NBA_GP_DIR / f"refs_{end_year}.json"
    try:
        if path.exists() and (done or time.time() - path.stat().st_mtime < ttl):
            return _nba_gp_mem_set(key, json.loads(path.read_text(encoding="utf-8"))) or None
    except Exception:
        pass
    try:
        r = await client.get(f"https://www.basketball-reference.com/referees/{end_year}_register.html",
                             headers=_NBA_GP_UA)
        table = _nba_gp_parse_refs(r.text) if r.status_code == 200 else {}
    except Exception:
        table = {}
    if not table:
        # Short negative cache: retry later instead of hammering the source.
        _NBA_GP_MEM[key] = (time.time() - ttl + 1800, {})
        return None
    try:
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(table), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass
    return _nba_gp_mem_set(key, table)


def _nba_gp_ref_lookup(tables, name):
    nn = _nn(name)
    parts = nn.split()
    found = []
    for t in tables:
        if not t:
            continue
        row = t.get(nn)
        if row is None and len(parts) >= 2:
            row = next((v for k, v in t.items() if _nm(k, nn)), None)
        if row:
            found.append(row)
    return found


def _nba_gp_crew(officials, ref_cur, ref_prev):
    """Crew tendencies: games-weighted across two seasons, shrunk toward average."""
    tables = [ref_cur or {}, ref_prev or {}]
    lg_home = None
    src = ref_prev or ref_cur or {}
    tot_g = sum(v["g"] for v in src.values() if v.get("home_win") is not None)
    if tot_g:
        lg_home = sum(v["home_win"] * v["g"] for v in src.values()
                      if v.get("home_win") is not None) / tot_g
    refs = []
    for name in officials:
        rows = _nba_gp_ref_lookup(tables, name)
        g = sum(r["g"] for r in rows)
        if not rows or g <= 0:
            refs.append({"name": name, "games": 0})
            continue

        def wavg(k):
            vals = [(r[k], r["g"]) for r in rows if r.get(k) is not None]
            n = sum(w for _, w in vals)
            return round(sum(v * w for v, w in vals) / n, 2) if n else None
        refs.append({"name": name, "games": g, "pf": wavg("pf"), "pf_rel": wavg("pf_rel"),
                     "fta": wavg("fta"), "fta_rel": wavg("fta_rel"), "pts": wavg("pts"),
                     "pts_rel": wavg("pts_rel"), "home_win": wavg("home_win")})
    known = [r for r in refs if r.get("games")]
    crew = {"refs": refs, "known": len(known), "league_home_win": round(lg_home, 3) if lg_home else None}
    if not known:
        crew.update({"total_adj": 0.0, "margin_adj": 0.0, "tag": ""})
        return crew

    def shrunk(k):
        vals = [(r[k] * r["games"] / (r["games"] + 25.0)) for r in known if r.get(k) is not None]
        return sum(vals) / len(vals) if vals else 0.0
    pts_rel, pf_rel, fta_rel = shrunk("pts_rel"), shrunk("pf_rel"), shrunk("fta_rel")
    hw = [(r["home_win"], r["games"]) for r in known if r.get("home_win") is not None]
    home_win = sum(v * w for v, w in hw) / sum(w for _, w in hw) if hw else None
    total_adj = _nba_gp_clamp(0.6 * pts_rel, -3.5, 3.5)
    margin_adj = 0.0
    if home_win is not None and lg_home is not None:
        n = sum(w for _, w in hw)
        margin_adj = _nba_gp_clamp((home_win - lg_home) * 8.0 * n / (n + 60.0), -1.0, 1.0)
    tag = ("High-foul crew" if pf_rel >= 1.2 else "Lets them play" if pf_rel <= -1.2 else "Neutral crew")
    crew.update({"pts_rel": round(pts_rel, 2), "pf_rel": round(pf_rel, 2), "fta_rel": round(fta_rel, 2),
                 "home_win": round(home_win, 3) if home_win is not None else None,
                 "total_adj": round(total_adj, 2), "margin_adj": round(margin_adj, 2), "tag": tag})
    return crew


def _nba_gp_split(games):
    n = len(games)
    if not n:
        return {"games": 0, "pf": None, "pa": None, "w": 0, "l": 0}
    return {"games": n, "pf": round(sum(g["pf"] for g in games) / n, 1),
            "pa": round(sum(g["pa"] for g in games) / n, 1),
            "w": sum(g["pf"] > g["pa"] for g in games), "l": sum(g["pf"] < g["pa"] for g in games)}


def _nba_gp_blend(cur, prev, k=8.0):
    """Current season gains weight game by game; last season anchors early."""
    if cur["games"] and prev["games"] >= 5:
        w = cur["games"] / (cur["games"] + k)
        return (w * cur["pf"] + (1 - w) * prev["pf"], w * cur["pa"] + (1 - w) * prev["pa"], round(w, 2))
    if cur["games"]:
        return cur["pf"], cur["pa"], 1.0
    if prev["games"]:
        return prev["pf"], prev["pa"], 0.0
    return _NBA_GP_LEAGUE_PTS, _NBA_GP_LEAGUE_PTS, 0.0


def _nba_gp_team_profile(games_by_season, season, venue, ds, event_id):
    """games_by_season: {(season, stype): [compact games]} for one team."""
    def before(rows):
        return [g for g in rows if g["date"] < ds and g["id"] != str(event_id)]
    cur = before(games_by_season.get((season, 2), []))
    cur_po = before(games_by_season.get((season, 3), []))
    prev = games_by_season.get((season - 1, 2), [])
    is_home = venue == "home"
    cur_v = [g for g in cur if g["home"] == is_home]
    prev_v = [g for g in prev if g["home"] == is_home]
    reg_all = sorted(prev + cur, key=lambda g: g["tip"])
    l10 = reg_all[-10:]
    recent_any = sorted(cur + cur_po, key=lambda g: g["tip"])
    streak = ""
    if recent_any or reg_all:
        seq = recent_any or reg_all
        last_w = seq[-1]["pf"] > seq[-1]["pa"]
        n = 0
        for g in reversed(seq):
            if (g["pf"] > g["pa"]) != last_w:
                break
            n += 1
        streak = ("W" if last_w else "L") + str(n)
    rest_days, b2b, three_in_four = None, False, False
    if recent_any:
        try:
            d0 = date.fromisoformat(ds)
            last = date.fromisoformat(recent_any[-1]["date"])
            rest_days = (d0 - last).days
            b2b = rest_days == 1
            in4 = [g for g in recent_any if 0 < (d0 - date.fromisoformat(g["date"])).days <= 3]
            three_in_four = len(in4) >= 2
        except Exception:
            pass
    return {"cur": _nba_gp_split(cur), "prev": _nba_gp_split(prev),
            "cur_venue": _nba_gp_split(cur_v), "prev_venue": _nba_gp_split(prev_v),
            "l10": _nba_gp_split(l10),
            "l10_games": [{"date": g["date"], "opp": g["opp"], "home": g["home"],
                           "pf": int(g["pf"]), "pa": int(g["pa"]), "season": g["season"]} for g in l10],
            "l10_spans_prev": any(g["season"] != season for g in l10),
            "streak": streak, "rest_days": rest_days, "b2b": b2b, "three_in_four": three_in_four}


def _nba_gp_spread_home(pc, home_abbr, away_abbr):
    details = str(pc.get("details") or "").strip()
    m = re.match(r"^([A-Za-z]+)\s*([+-]?\d+(?:\.\d+)?)$", details)
    if m:
        ab, val = _norm_abbr(m.group(1).upper()), abs(float(m.group(2)))
        if ab == home_abbr:
            return -val
        if ab == away_abbr:
            return val
    if details.upper() in ("EVEN", "PK", "PICK"):
        return 0.0
    try:
        return float(pc.get("spread"))
    except Exception:
        return None


def _nba_gp_num(v):
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None


def _nba_gp_odds_ok(o):
    v = _nba_gp_num(o)
    return v is not None and v >= -1000 and (v >= 100 or v <= -100)


def _nba_gp_implied(o):
    v = _nba_gp_num(o)
    if v is None or not (v >= 100 or v <= -100):
        return None
    return 100.0 / (v + 100.0) if v > 0 else -v / (-v + 100.0)


def _nba_gp_summary_bits(summ, home_abbr, away_abbr, home_id, away_id):
    gi = summ.get("gameInfo") or {}
    officials = [o.get("displayName") or o.get("fullName") for o in (gi.get("officials") or [])
                 if o.get("displayName") or o.get("fullName")]
    pcs = [p for p in (summ.get("pickcenter") or []) if isinstance(p, dict)]
    pcs.sort(key=lambda p: ((p.get("provider") or {}).get("priority") or 99))
    pc = pcs[0] if pcs else {}
    ho, ao = pc.get("homeTeamOdds") or {}, pc.get("awayTeamOdds") or {}
    book = {"book": (pc.get("provider") or {}).get("name") or "",
            "total": _nba_gp_num(pc.get("overUnder")),
            "over_odds": _nba_gp_num(pc.get("overOdds")), "under_odds": _nba_gp_num(pc.get("underOdds")),
            "home_spread": _nba_gp_spread_home(pc, home_abbr, away_abbr) if pc else None,
            "home_spread_odds": _nba_gp_num(ho.get("spreadOdds")),
            "away_spread_odds": _nba_gp_num(ao.get("spreadOdds")),
            "home_ml": _nba_gp_num(ho.get("moneyLine")), "away_ml": _nba_gp_num(ao.get("moneyLine"))}
    for k in ("over_odds", "under_odds", "home_spread_odds", "away_spread_odds", "home_ml", "away_ml"):
        if book.get(k) is not None and book[k] < -1000:
            book[k] = None          # never surface prices shorter than -1000
            book["hidden_short"] = True
    pred = summ.get("predictor") or {}
    bpi = None
    try:
        hp = (pred.get("homeTeam") or {}).get("gameProjection")
        bpi = round(float(hp), 1) if hp not in (None, "") else None
    except Exception:
        bpi = None
    inj = {home_id: [], away_id: []}
    for t in summ.get("injuries") or []:
        tid = str((t.get("team") or {}).get("id", ""))
        if tid not in inj:
            continue
        for i in t.get("injuries") or []:
            a = i.get("athlete") or {}
            status = str(i.get("status") or (i.get("type") or {}).get("description") or "")
            det = i.get("details") or {}
            inj[tid].append({"id": str(a.get("id", "")), "name": a.get("displayName", ""),
                             "pos": (a.get("position") or {}).get("abbreviation", ""),
                             "status": status,
                             "detail": " ".join(x for x in [det.get("side"), det.get("type"), det.get("detail")] if x)
                                       or (i.get("shortComment") or "")})
    venue = (gi.get("venue") or {}).get("fullName", "")
    return officials, book, bpi, inj, venue


def _nba_gp_injury_rows(rows, logs_cur, logs_prev, team_cur_games, ds):
    out, total = [], 0.0
    for r in rows:
        key = r["status"].lower()
        p = next((v for k, v in _NBA_GP_ABSENT.items() if k in key), 0.3)
        cur = [g for g in (logs_cur.get(r["id"]) or []) if _nba_gp_et_date(g.get("date")) < ds]
        prev = logs_prev.get(r["id"]) or []
        use, label = (cur, "this season") if len(cur) >= 3 else (prev, "last season")
        n = len(use)
        ppg = round(sum(g.get("PTS", 0) for g in use) / n, 1) if n else None
        mpg = round(sum(g.get("MIN", 0) for g in use) / n, 1) if n else None
        counted, reason, lost = False, "", 0.0
        if not n or n < 3:
            reason = "no recent role data"
        elif mpg < 15:
            reason = "minor role"
        elif team_cur_games >= 10 and not cur:
            reason = "out all season — already in team numbers"
        elif p <= 0:
            reason = "expected to play"
        else:
            lost = round(ppg * 0.45 * p, 1)
            counted = True
            reason = f"~{int(p * 100)}% chance out · {ppg} ppg {label}"
        total += lost
        out.append({**r, "ppg": ppg, "mpg": mpg, "games": n, "p_absent": p,
                    "lost": lost, "counted": counted, "reason": reason})
    out.sort(key=lambda x: (-(x["lost"] or 0), -(x["ppg"] or 0)))
    return out, round(min(9.0, total), 1)


def _nba_gp_predict_one(g, hp, ap, meetings, crew, inj_h, inj_a, lost_h, lost_a, book, bpi):
    factors = []
    hb_pf, hb_pa, hw = _nba_gp_blend(hp["cur_venue"], hp["prev_venue"])
    ab_pf, ab_pa, aw = _nba_gp_blend(ap["cur_venue"], ap["prev_venue"])
    ph = (hb_pf + ab_pa) / 2.0
    pa_ = (ab_pf + hb_pa) / 2.0
    base_h, base_a = ph, pa_
    factors.append({"name": "Home/road scoring baseline", "home": round(ph, 1), "away": round(pa_, 1),
                    "note": f"{g['home']} home PF/PA + {g['away']} road PF/PA · last season blended with "
                            f"this season ({int(hw * 100)}% / {int(aw * 100)}% current)"})
    # Last-10 form against each team's own season level.
    hs_pf, hs_pa, _ = _nba_gp_blend(hp["cur"], hp["prev"])
    as_pf, as_pa, _ = _nba_gp_blend(ap["cur"], ap["prev"])
    fh = fa = 0.0
    if hp["l10"]["games"] >= 5 and ap["l10"]["games"] >= 5:
        fh = _nba_gp_clamp(0.25 * (hp["l10"]["pf"] - hs_pf), -2.5, 2.5) + \
             _nba_gp_clamp(0.25 * (ap["l10"]["pa"] - as_pa), -2.5, 2.5)
        fa = _nba_gp_clamp(0.25 * (ap["l10"]["pf"] - as_pf), -2.5, 2.5) + \
             _nba_gp_clamp(0.25 * (hp["l10"]["pa"] - hs_pa), -2.5, 2.5)
        fh, fa = _nba_gp_clamp(fh, -3.5, 3.5), _nba_gp_clamp(fa, -3.5, 3.5)
    ph += fh; pa_ += fa
    factors.append({"name": "Last 10 form", "home": round(fh, 1), "away": round(fa, 1),
                    "note": f"{g['home']} L10 {hp['l10']['w']}-{hp['l10']['l']} · {g['away']} L10 {ap['l10']['w']}-{ap['l10']['l']}"})
    # Rest / back-to-back.
    rh = ra = 0.0
    notes = []
    for side, prof, abbr in (("h", hp, g["home"]), ("a", ap, g["away"])):
        own = opp = 0.0
        if prof["b2b"]:
            own, opp = -1.2, 0.8
            notes.append(f"{abbr} on a back-to-back")
        if prof["three_in_four"]:
            own -= 0.5
            notes.append(f"{abbr} 3rd game in 4 nights")
        if side == "h":
            rh += own; ra += opp
        else:
            ra += own; rh += opp
    ph += rh; pa_ += ra
    factors.append({"name": "Rest / back-to-back", "home": round(rh, 1), "away": round(ra, 1),
                    "note": "; ".join(notes) or f"Rest days {g['home']} {hp['rest_days'] if hp['rest_days'] is not None else '—'} · {g['away']} {ap['rest_days'] if ap['rest_days'] is not None else '—'}"})
    # Head-to-head (same building weighted double).
    hh = ha = 0.0
    model_meet = meetings[:10]
    same = [m for m in model_meet if m["home"]]
    rev = [m for m in model_meet if not m["home"]]
    if same or rev:
        wsum = len(same) + 0.5 * len(rev)
        tot_avg = (sum(m["pf"] + m["pa"] for m in same) + 0.5 * sum(m["pf"] + m["pa"] for m in rev)) / wsum
        mar_avg = (sum(m["pf"] - m["pa"] for m in same) + 0.5 * sum(m["pf"] - m["pa"] + 5.0 for m in rev)) / wsum
        w = min(0.20, 0.04 * len(same) + 0.02 * len(rev))
        t_adj = _nba_gp_clamp((tot_avg - (ph + pa_)) * w, -4.0, 4.0)
        m_adj = _nba_gp_clamp((mar_avg - (ph - pa_)) * w, -3.0, 3.0)
        tot_now = ph + pa_
        hh = t_adj * ph / tot_now + m_adj / 2.0
        ha = t_adj * pa_ / tot_now - m_adj / 2.0
        ph += hh; pa_ += ha
    factors.append({"name": "Head-to-head history", "home": round(hh, 1), "away": round(ha, 1),
                    "note": (f"{len(same)} at {g['home']} + {len(rev)} at {g['away']} in the last "
                             f"{len(model_meet)} meetings" if model_meet else "No recent meetings")})
    # Injuries.
    ph -= lost_h; pa_ -= lost_a
    factors.append({"name": "Injuries", "home": -lost_h, "away": -lost_a,
                    "note": f"{sum(1 for x in inj_h if x['counted'])} {g['home']} / "
                            f"{sum(1 for x in inj_a if x['counted'])} {g['away']} rotation players flagged"})
    # Referees.
    t_adj, m_adj = crew.get("total_adj", 0.0), crew.get("margin_adj", 0.0)
    tot_now = ph + pa_
    rh2 = t_adj * ph / tot_now + m_adj / 2.0 if tot_now else 0.0
    ra2 = t_adj * pa_ / tot_now - m_adj / 2.0 if tot_now else 0.0
    ph += rh2; pa_ += ra2
    factors.append({"name": "Referee crew", "home": round(rh2, 1), "away": round(ra2, 1),
                    "note": (f"{crew.get('tag')} · {crew['known']}/{len(crew['refs'])} refs with history"
                             if crew.get("known") else
                             ("Crew not posted yet" if not crew.get("refs") else "No referee history found"))})
    ph, pa_ = round(ph, 1), round(pa_, 1)
    margin = round(ph - pa_, 1)
    total = round(ph + pa_, 1)
    wp = 0.5 * (1.0 + math.erf(margin / (_NBA_GP_SD * math.sqrt(2.0))))
    wp = round(_nba_gp_clamp(wp, 0.03, 0.97), 3)
    pick = g["home"] if wp >= 0.5 else g["away"]
    pick_prob = round((wp if wp >= 0.5 else 1 - wp) * 100, 1)
    # Book comparisons.
    ou_pick = ou_lean = None
    ou_edge = None
    if book.get("total") is not None:
        ou_edge = round(total - book["total"], 1)
        ou_lean = "OVER" if ou_edge > 0 else "UNDER" if ou_edge < 0 else None
        side_odds = book.get("over_odds") if ou_lean == "OVER" else book.get("under_odds")
        if ou_lean and abs(ou_edge) >= _NBA_GP_OU_MIN and (side_odds is None or _nba_gp_odds_ok(side_odds)):
            ou_pick = ou_lean
    ats_pick = ats_lean = None
    ats_edge = None
    if book.get("home_spread") is not None:
        ats_edge = round(margin + book["home_spread"], 1)
        ats_lean = g["home"] if ats_edge > 0 else g["away"] if ats_edge < 0 else None
        if ats_lean and abs(ats_edge) >= _NBA_GP_ATS_MIN:
            ats_pick = ats_lean
    pick_ml = book.get("home_ml") if pick == g["home"] else book.get("away_ml")
    ml_edge = None
    if _nba_gp_odds_ok(pick_ml):
        ml_edge = round((pick_prob / 100.0 - _nba_gp_implied(pick_ml)) * 100, 1)
    else:
        pick_ml = None   # missing, or shorter than -1000 (never surfaced)
    return {"projHome": ph, "projAway": pa_, "projMargin": margin, "projTotal": total,
            "baseHome": round(base_h, 1), "baseAway": round(base_a, 1),
            "winProbHome": wp, "pickTeam": pick, "pickProb": pick_prob,
            "ouPick": ou_pick, "ouLean": ou_lean, "ouEdge": ou_edge,
            "atsPick": ats_pick, "atsLean": ats_lean, "atsEdge": ats_edge,
            "pickMl": pick_ml, "mlEdge": ml_edge, "factors": factors, "bpiHome": bpi}


async def _nba_gp_build(ds: str) -> dict:
    target = date.fromisoformat(ds)
    season = _nba_current_espn_season(target)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        sbd = None
        for attempt in range(2):
            try:
                r = await client.get(f"{_NBA_GP_ESPN}/scoreboard", params={"dates": ds.replace("-", "")})
                if r.status_code == 200:
                    sbd = r.json()
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        if not isinstance(sbd, dict) or not isinstance(sbd.get("events"), list):
            raise RuntimeError("ESPN schedule is unavailable right now.")
        games = []
        for e in sbd["events"]:
            c = (e.get("competitions") or [{}])[0]
            comps = c.get("competitors") or []
            h = next((x for x in comps if x.get("homeAway") == "home"), None)
            a = next((x for x in comps if x.get("homeAway") == "away"), None)
            if not h or not a:
                continue
            st = (c.get("status") or {}).get("type") or {}
            stype = int(((e.get("season") or {}).get("type")) or 2)
            games.append({"event_id": str(e.get("id", "")), "tip": e.get("date", ""), "stype": stype,
                          "home": _norm_abbr(h["team"].get("abbreviation", "")),
                          "away": _norm_abbr(a["team"].get("abbreviation", "")),
                          "home_id": str(h["team"].get("id", "")), "away_id": str(a["team"].get("id", "")),
                          "home_name": h["team"].get("displayName", ""), "away_name": a["team"].get("displayName", ""),
                          "home_logo": h["team"].get("logo", ""), "away_logo": a["team"].get("logo", ""),
                          "state": st.get("state", "pre"), "status": st.get("shortDetail") or st.get("description", ""),
                          "completed": bool(st.get("completed")),
                          "home_score": _nba_gp_score(h), "away_score": _nba_gp_score(a),
                          "home_rec": next((x.get("summary") for x in (h.get("records") or []) if x.get("type") == "total"), ""),
                          "away_rec": next((x.get("summary") for x in (a.get("records") or []) if x.get("type") == "total"), "")})
        if not games:
            return {"date": ds, "games": [], "message": "No NBA games are scheduled for this date."}
        _nba_gp_check_run()
        sem = asyncio.Semaphore(4)
        need = set()
        for g in games:
            for s in range(season - 4, season + 1):
                need.add((g["home_id"], s, 2)); need.add((g["home_id"], s, 3))
            need.update({(g["away_id"], season, 2), (g["away_id"], season, 3), (g["away_id"], season - 1, 2)})
        need = sorted(need)
        res = await asyncio.gather(*[_nba_gp_team_games(client, t, s, ty, sem) for t, s, ty in need],
                                   return_exceptions=True)
        if any(isinstance(v, _NbaGpRunBusy) for v in res):
            raise _NbaGpRunBusy()
        tg: Dict[str, Dict] = {}
        missing = 0
        for (t, s, ty), v in zip(need, res):
            if isinstance(v, Exception):
                missing += 1
                v = []
            tg.setdefault(t, {})[(s, ty)] = v

        async def _summary(g):
            # Keep only the extracted pieces in memory, never the full summary page.
            key = f"sumbits:{g['event_id']}"
            hit = _nba_gp_mem_get(key, 240)
            if hit is not None:
                return hit
            _nba_gp_check_run()
            async with sem:
                _nba_gp_check_run()
                r = await client.get(f"{_NBA_GP_ESPN}/summary", params={"event": g["event_id"]})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            b = _nba_gp_summary_bits(r.json(), g["home"], g["away"], g["home_id"], g["away_id"])
            del r
            return _nba_gp_mem_set(key, b)
        sums = await asyncio.gather(*[_summary(g) for g in games], return_exceptions=True)
        if any(isinstance(v, _NbaGpRunBusy) for v in sums):
            raise _NbaGpRunBusy()
        ref_cur, ref_prev = await asyncio.gather(_nba_gp_ref_season(client, season),
                                                 _nba_gp_ref_season(client, season - 1))
        bits = {}
        pids = set()
        for g, s in zip(games, sums):
            if isinstance(s, Exception) or not isinstance(s, tuple):
                bits[g["event_id"]] = ([], {}, None, {g["home_id"]: [], g["away_id"]: []}, "")
                continue
            b = s
            bits[g["event_id"]] = b
            for rows in b[3].values():
                pids.update(x["id"] for x in rows if x["id"])
        _nba_gp_check_run()
        psem = asyncio.Semaphore(4)
        pids = sorted(pids)
        logs = await asyncio.gather(*[get_player_gamelogs_espn(p, s, psem, client)
                                      for p in pids for s in (season, season - 1)], return_exceptions=True)
        logs_cur, logs_prev = {}, {}
        for i, p in enumerate(pids):
            lc, lp = logs[2 * i], logs[2 * i + 1]
            logs_cur[p] = [] if isinstance(lc, Exception) else lc
            logs_prev[p] = [] if isinstance(lp, Exception) else lp

    preds = []
    for g in games:
        th, ta = tg.get(g["home_id"], {}), tg.get(g["away_id"], {})
        hp = _nba_gp_team_profile(th, season, "home", ds, g["event_id"])
        ap = _nba_gp_team_profile(ta, season, "away", ds, g["event_id"])
        meet_all = []
        for (s, ty), rows in th.items():
            meet_all.extend(m for m in rows if m["opp_id"] == g["away_id"] and m["date"] < ds
                            and m["id"] != g["event_id"])
        meet_all.sort(key=lambda m: m["tip"], reverse=True)
        meetings = meet_all[:10]
        officials, book, bpi, inj, venue = bits[g["event_id"]]
        crew = _nba_gp_crew(officials, ref_cur, ref_prev)
        inj_h, lost_h = _nba_gp_injury_rows(inj.get(g["home_id"], []), logs_cur, logs_prev, hp["cur"]["games"], ds)
        inj_a, lost_a = _nba_gp_injury_rows(inj.get(g["away_id"], []), logs_cur, logs_prev, ap["cur"]["games"], ds)
        model = _nba_gp_predict_one(g, hp, ap, meetings, crew, inj_h, inj_a, lost_h, lost_a, book, bpi)
        preds.append({**{k: g[k] for k in ("event_id", "tip", "stype", "home", "away", "home_id", "away_id",
                                           "home_name", "away_name", "home_logo", "away_logo", "state",
                                           "status", "completed", "home_score", "away_score",
                                           "home_rec", "away_rec")},
                      "venue": venue, "season": season, **model, "book": book,
                      "homeProfile": hp, "awayProfile": ap, "crew": crew,
                      "injHome": inj_h, "injAway": inj_a, "lostHome": lost_h, "lostAway": lost_a,
                      "meetings": [{"date": m["date"], "home": g["home"] if m["home"] else g["away"],
                                    "away": g["away"] if m["home"] else g["home"],
                                    "homePts": int(m["pf"] if m["home"] else m["pa"]),
                                    "awayPts": int(m["pa"] if m["home"] else m["pf"]),
                                    "winner": g["home"] if m["pf"] > m["pa"] else g["away"],
                                    "total": int(m["pf"] + m["pa"]), "sameVenue": bool(m["home"]),
                                    "playoffs": m["stype"] == 3, "season": m["season"]} for m in meetings]})
    preds.sort(key=lambda p: p.get("tip") or "")
    return {"date": ds, "season": season, "games": preds,
            "refsAvailable": bool(ref_cur or ref_prev),
            "partialHistory": missing > 0}


def _nba_gp_compact(p):
    keep = ("event_id", "tip", "stype", "home", "away", "home_id", "away_id", "projHome", "projAway",
            "projMargin", "projTotal", "winProbHome", "pickTeam", "pickProb", "ouPick", "ouLean", "ouEdge",
            "atsPick", "atsLean", "atsEdge", "pickMl", "mlEdge", "factors", "bpiHome", "book")
    out = {k: p.get(k) for k in keep}
    out["crew"] = [r.get("name") for r in (p.get("crew") or {}).get("refs", [])]
    out["saved_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    return out


def _nba_gp_snapshot_doc(ds):
    rows = _nba_sb_get({"app": f"eq.{_NBA_GP_APP}", "category": f"eq.{_NBA_GP_SNAP_CAT}",
                        "side": "eq.ALL", "date": f"eq.{ds}", "select": "detail", "limit": "1"})
    det = (rows[0].get("detail") if rows else None) or {}
    return det if isinstance(det, dict) else {}


def _nba_gp_save_snapshot(ds, preds):
    """Latest pregame forecast per game wins; a game freezes at its tip-off."""
    now = datetime.utcnow()
    fresh = {}
    for p in preds:
        if p.get("stype") not in (2, 3) or p.get("state") != "pre":
            continue
        try:
            tip = datetime.fromisoformat(str(p.get("tip")).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
        if now >= tip:
            continue
        fresh[str(p["event_id"])] = _nba_gp_compact(p)
    if not fresh:
        return
    with _NBA_GP_LOCK:
        doc = _nba_gp_snapshot_doc(ds)
        for eid, row in fresh.items():
            old = doc.get(eid)
            if old:
                try:
                    tip = datetime.fromisoformat(str(old.get("tip")).replace("Z", "+00:00")).replace(tzinfo=None)
                    if now >= tip:
                        continue
                except Exception:
                    pass
            doc[eid] = row
        ok = _nba_sb_upsert([{"app": _NBA_GP_APP, "date": ds, "category": _NBA_GP_SNAP_CAT, "side": "ALL",
                              "wins": 0, "losses": 0, "locked": False, "detail": doc}],
                            "app,date,category,side")
    print(f"[nba_gp] snapshot {'saved' if ok else 'FAILED'}: {len(fresh)} games -> {ds}")


def _nba_gp_profit(odds, result, default=-110.0):
    o = _nba_gp_num(odds)
    if o is None:
        o = default
    if result == "WIN":
        return round(_NBA_GP_STAKE * (o / 100.0 if o > 0 else 100.0 / -o), 2)
    if result == "LOSS":
        return -_NBA_GP_STAKE
    return 0.0


def _nba_gp_grade(p, hs, as_):
    book = p.get("book") or {}
    home, away = p.get("home"), p.get("away")
    out = {"event_id": p.get("event_id"), "home": home, "away": away, "home_score": hs, "away_score": as_,
           "pickTeam": p.get("pickTeam"), "pickProb": p.get("pickProb"),
           "projHome": p.get("projHome"), "projAway": p.get("projAway"), "projTotal": p.get("projTotal")}
    won = home if hs > as_ else away
    out["winner"] = "WIN" if p.get("pickTeam") == won else "LOSS"
    ml = p.get("pickMl")
    out["winner_odds"] = ml
    out["winner_profit"] = _nba_gp_profit(ml, out["winner"]) if _nba_gp_odds_ok(ml) else None
    if p.get("atsPick") and book.get("home_spread") is not None:
        adj = hs + float(book["home_spread"]) - as_
        if adj == 0:
            r = "PUSH"
        else:
            r = "WIN" if (adj > 0) == (p["atsPick"] == home) else "LOSS"
        odds = book.get("home_spread_odds") if p["atsPick"] == home else book.get("away_spread_odds")
        out.update({"ats": r, "ats_pick": p["atsPick"], "ats_line": book["home_spread"] if p["atsPick"] == home
                    else -float(book["home_spread"]), "ats_odds": odds, "ats_profit": _nba_gp_profit(odds, r)})
    if p.get("ouPick") and book.get("total") is not None:
        tot = hs + as_
        line = float(book["total"])
        r = "PUSH" if tot == line else ("WIN" if (tot > line) == (p["ouPick"] == "OVER") else "LOSS")
        odds = book.get("over_odds") if p["ouPick"] == "OVER" else book.get("under_odds")
        out.update({"ou": r, "ou_pick": p["ouPick"], "ou_line": line, "ou_total": tot, "ou_odds": odds,
                    "ou_profit": _nba_gp_profit(odds, r)})
    return out


async def _nba_gp_finals(client, ds):
    r = await client.get(f"{_NBA_GP_ESPN}/scoreboard", params={"dates": ds.replace("-", "")})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    out = {}
    for e in r.json().get("events") or []:
        c = (e.get("competitions") or [{}])[0]
        st = (c.get("status") or {}).get("type") or {}
        comps = c.get("competitors") or []
        h = next((x for x in comps if x.get("homeAway") == "home"), None)
        a = next((x for x in comps if x.get("homeAway") == "away"), None)
        if not h or not a:
            continue
        name = str(st.get("name") or "").upper()
        out[str(e.get("id"))] = {"completed": bool(st.get("completed")),
                                 "void": any(x in name for x in ("POSTPONED", "CANCELED", "CANCELLED")),
                                 "hs": _nba_gp_score(h), "as": _nba_gp_score(a)}
    return out


async def _nba_gp_record() -> dict:
    snaps = _nba_sb_get({"app": f"eq.{_NBA_GP_APP}", "category": f"eq.{_NBA_GP_SNAP_CAT}",
                         "select": "date,detail", "limit": "2000"}) or []
    graded = {r.get("date"): r for r in (_nba_sb_get({"app": f"eq.{_NBA_GP_APP}",
                                                       "category": f"eq.{_NBA_GP_GRADED_CAT}",
                                                       "select": "date,detail,locked", "limit": "2000"}) or [])}
    today = _nba_gp_today_et()
    todo = [s for s in snaps if s.get("date") and s["date"] <= today
            and not (graded.get(s["date"]) or {}).get("locked")]
    if todo:
        async with httpx.AsyncClient(timeout=15) as client:
            for s in sorted(todo, key=lambda x: x["date"])[-21:]:
                ds, doc = s["date"], s.get("detail") or {}
                try:
                    finals = await _nba_gp_finals(client, ds)
                except Exception as exc:
                    print(f"[nba_gp] grading {ds} skipped: {exc}")
                    continue
                rows, all_done = [], True
                for eid, p in doc.items():
                    f = finals.get(str(eid))
                    if not f or f["void"]:
                        if not f or not f["void"]:
                            all_done = False
                        continue
                    if not f["completed"] or f["hs"] is None or f["as"] is None:
                        all_done = False
                        continue
                    rows.append(_nba_gp_grade(p, f["hs"], f["as"]))
                old_days = (date.fromisoformat(today) - date.fromisoformat(ds)).days >= 3
                locked = all_done or old_days
                _nba_sb_upsert([{"app": _NBA_GP_APP, "date": ds, "category": _NBA_GP_GRADED_CAT, "side": "ALL",
                                 "wins": sum(r["winner"] == "WIN" for r in rows),
                                 "losses": sum(r["winner"] == "LOSS" for r in rows),
                                 "locked": locked, "detail": rows}], "app,date,category,side")
                graded[ds] = {"date": ds, "detail": rows, "locked": locked}
    pending = {s["date"]: len((s.get("detail") or {})) for s in snaps if s.get("date")}

    def blank():
        return {"wins": 0, "losses": 0, "pushes": 0, "profit": 0.0, "priced": 0}
    tot = {"winner": blank(), "ats": blank(), "ou": blank()}
    days = []
    for ds in sorted(set(pending) | set(graded), reverse=True):
        rows = (graded.get(ds) or {}).get("detail") or []
        d = {"date": ds, "games": rows, "saved": pending.get(ds, 0),
             "locked": bool((graded.get(ds) or {}).get("locked")),
             "winner": blank(), "ats": blank(), "ou": blank()}
        for r in rows:
            for k, rk, pk in (("winner", "winner", "winner_profit"), ("ats", "ats", "ats_profit"),
                              ("ou", "ou", "ou_profit")):
                res = r.get(rk)
                if not res:
                    continue
                for bucket in (d[k], tot[k]):
                    bucket["wins"] += res == "WIN"
                    bucket["losses"] += res == "LOSS"
                    bucket["pushes"] += res == "PUSH"
                    if r.get(pk) is not None:
                        bucket["profit"] = round(bucket["profit"] + r[pk], 2)
                        bucket["priced"] += res in ("WIN", "LOSS")
        days.append(d)
    for b in tot.values():
        n = b["wins"] + b["losses"]
        b["pct"] = round(b["wins"] / n * 100, 1) if n else None
        b["roi"] = round(b["profit"] / (b["priced"] * _NBA_GP_STAKE) * 100, 1) if b["priced"] else None
    return {"stake": _NBA_GP_STAKE, "totals": tot, "days": days}


def _nba_gp_auth(request: Request) -> bool:
    adm = request.query_params.get("admin", "")
    return bool(get_user(request)) or (bool(adm) and adm == os.environ.get("INTERNAL_API_TOKEN", "__none__"))


@app.get("/api/nba/game-predictor")
async def nba_game_predictor(request: Request, date_str: str = ""):
    from fastapi import HTTPException
    if not _nba_gp_auth(request):
        raise HTTPException(status_code=401, detail="Subscription required — please log in via moneypicksarena.com")
    ds = (date_str or _nba_gp_today_et()).strip()
    try:
        date.fromisoformat(ds)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date")
    cache_key = f"gp:{ds}"
    data = _nba_gp_mem_get(cache_key, 180)
    if data is None:
        if _NBA_RUNS_ACTIVE > 0:
            raise HTTPException(status_code=409, detail="Run in progress. Load the Game Predictor after the run finishes.")
        try:
            async with _nba_gp_build_lock():
                data = _nba_gp_mem_get(cache_key, 180)
                if data is None:
                    data = await asyncio.wait_for(_nba_gp_build(ds), timeout=100)
        except _NbaGpRunBusy:
            raise HTTPException(status_code=409, detail="A run started, so the Game Predictor stepped aside. Load it again after the run finishes.")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Game Predictor data took too long to load. Try again in a minute.")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Game Predictor data unavailable: {exc}")
        _nba_gp_mem_set(cache_key, data)
        if ds >= _nba_gp_today_et() and data.get("games"):
            _nba_th.Thread(target=_nba_gp_save_snapshot, args=(ds, data["games"]), daemon=True).start()
    out = json.loads(json.dumps(data))
    # Overlay the saved pregame forecast for any game that has already started,
    # so a started or past game always shows what was actually predicted.
    try:
        doc = await asyncio.to_thread(_nba_gp_snapshot_doc, ds)
    except Exception:
        doc = {}
    for p in out.get("games") or []:
        saved = doc.get(str(p.get("event_id")))
        if saved and p.get("state") != "pre":
            for k, v in saved.items():
                if k not in ("crew", "saved_at"):
                    p[k] = v
            p["savedPrediction"] = True
            p["savedAt"] = saved.get("saved_at")
        if p.get("completed") and p.get("home_score") is not None and p.get("away_score") is not None:
            p["result"] = _nba_gp_grade(p, p["home_score"], p["away_score"])
        p["retro"] = (not saved) and ds < _nba_gp_today_et()
    return out


@app.get("/api/nba/game-predictor/record")
async def nba_game_predictor_record(request: Request):
    from fastapi import HTTPException
    if not _nba_gp_auth(request):
        raise HTTPException(status_code=401, detail="Subscription required — please log in via moneypicksarena.com")
    try:
        return await asyncio.wait_for(_nba_gp_record(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Game Predictor record took too long to load.")


@app.get("/api/verify-token")
async def verify_token_nba(request: Request):
    from fastapi import HTTPException
    auth = request.headers.get("Authorization", "")
    tok = auth.replace("Bearer ", "").strip()
    if not tok or len(tok.split(".")) != 3:
        raise HTTPException(status_code=401, detail="Invalid token")
    from fastapi.responses import JSONResponse
    return JSONResponse({"ok": True})


@app.get("/api/nba/player-pre-game-log")
async def nba_player_pre_game_log(request: Request, date: str = "",
                                  player_id: str = "", stat: str = "",
                                  team: str = "", opp: str = "",
                                  location: str = ""):
    """Read-only game-log hydration for immutable historical snapshots."""
    from fastapi import HTTPException
    if not get_user(request):
        raise HTTPException(status_code=401, detail="Subscription required")
    try:
        replay_date = globals()["date"].fromisoformat(date)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid replay date")
    if not player_id or stat not in STAT_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid player or market")
    replay_season = replay_date.year + (1 if replay_date.month >= 10 else 0)
    seasons = [replay_season - offset for offset in range(7)]
    sem = asyncio.Semaphore(7)
    async with httpx.AsyncClient(timeout=8) as client:
        results = await asyncio.gather(*[
            get_player_gamelogs_espn(player_id, season, sem, client)
            for season in seasons
        ], return_exceptions=True)
    logs = sorted(
        [game for result in results if isinstance(result, list) for game in result
         if str(game.get("date", ""))[:10] < date],
        key=lambda game: str(game.get("date", "")), reverse=True)
    recent = [{"d": game.get("date"), "v": game.get(stat)}
              for game in logs[:10] if game.get(stat) is not None]
    team_abbr, opp_abbr = _norm_abbr(team), _norm_abbr(opp)
    loc = "Home" if str(location).lower() == "home" else "Away"
    matchup = [{"d": game.get("date"), "v": game.get(stat)}
               for game in logs
               if game.get("opp") == opp_abbr
               and game.get("player_team") == team_abbr
               and game.get("location") == loc
               and game.get(stat) is not None][:10]
    return {"date": date, "player_id": player_id, "stat": stat,
            "recent": recent, "matchup": matchup}

@app.get("/api/whoami")
async def whoami_nba(request: Request, token: str = "", admin: str = ""):
    tok = token or request.query_params.get("_tok","") or request.cookies.get("__mpa_token","") or request.headers.get("Authorization","").replace("Bearer ","").strip()
    ok = _is_admin_token(tok) or (bool(admin) and admin == os.environ.get("INTERNAL_API_TOKEN", "__none__"))
    return {"is_admin": ok}

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, admin: str = "", token: str = ""):
    today_iso = _nba_today().isoformat()
    tomorrow_iso = (_nba_today() + timedelta(days=1)).isoformat()
    # Admin turns on via EITHER ?admin=INTERNAL_API_TOKEN OR a hub login token
    # whose email matches the admin (so it just works when the owner logs in).
    is_admin = (bool(admin) and admin == os.environ.get("INTERNAL_API_TOKEN", "__none__")) or _is_admin_token(token)
    js_flag = "true" if is_admin else "false"
    coach_html = NBA_COACH_HTML.replace("__TODAY__", today_iso)
    historical_html = NBA_HISTORICAL_HTML.replace("__TODAY__", today_iso)
    gp_html = NBA_GP_HTML.replace("__TODAY__", today_iso)
    # Keep the full Coach panel adjacent to the primary board, immediately before
    # ALL PICKS.  Historical Track Record remains a bottom-of-page surface.
    html = (MAIN_HTML.replace("__TODAY__", today_iso).replace("__TOMORROW__", tomorrow_iso)
            .replace('<div id="allPicksWrap"', gp_html + coach_html + '<div id="allPicksWrap"', 1)
            .replace("</body>", historical_html + "</body>", 1)
            .replace("</head>", f"<script>window.IS_ADMIN = {js_flag};</script></head>", 1))
    return HTMLResponse(html)

@app.get("/login")
async def login_get():
    return RedirectResponse("https://moneypicksarena.com")

@app.post("/login")
async def login_post(request: Request):
    form = await request.form()
    u = form.get("username", "").strip()
    p = form.get("password", "").strip()
    if USERS.get(u) == p:
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("session", make_token(u), httponly=True, samesite="lax", max_age=86400*7)
        return resp
    return HTMLResponse(LOGIN_HTML.replace('{error}', '<p class="err">⚠️ Invalid username or password</p>'), status_code=401)


@app.get("/api/warm")
async def api_warm_nba():
    """Pre-compute today's picks — called by cron-job.org at 10 AM."""
    today = _nba_today().isoformat()
    cached = _cache_get("nba", today)
    if cached:
        return {"ok": True, "source": "cache", "date": today,
                "picks": len(cached.get("picks", []))}
    try:
        result = await run_analysis(today)
        return {"ok": True, "source": "computed", "date": today,
                "picks": len(result.get("picks", []))}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/run")
async def run(request: Request):
    if not get_user(request):
        return {"error": "Unauthorized"}
    try:
        body = await request.json()
        selected_date = body.get('date', _nba_today().isoformat())
        force = bool(body.get('force', False))
    except Exception:
        selected_date = _nba_today().isoformat()
        force = False
    # Force (cache bypass) is admin-only — independently re-verify the hub token.
    if force:
        _tok = request.query_params.get("_tok","") or request.cookies.get("__mpa_token","") or request.headers.get("Authorization","").replace("Bearer ","").strip()
        _adm = request.query_params.get("admin","")
        if not (_is_admin_token(_tok) or (bool(_adm) and _adm == os.environ.get("INTERNAL_API_TOKEN", "__none__"))):
            force = False
    result = await run_analysis(selected_date, force=force)
    return result

_CRON_BUSY_NBA = False

@app.api_route("/api/cron-run", methods=["GET", "POST"])
async def cron_run_nba(request: Request, date_str: str = ""):
    # Cron-friendly trigger: authed by the static INTERNAL_API_TOKEN secret sent
    # as a header (kept out of the URL so it isn't logged). No expiring hub login
    # needed. Runs the pipeline + caches it so members can pull the picks, and
    # wakes the free-tier app on Render. An in-flight guard blocks overlapping runs.
    global _CRON_BUSY_NBA
    import hmac
    from fastapi import HTTPException
    secret = os.environ.get("INTERNAL_API_TOKEN", "")
    tok = request.headers.get("X-Internal-Token", "") or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not secret or not hmac.compare_digest(tok or "", secret):
        raise HTTPException(status_code=401, detail="Invalid cron token")
    ds = date_str or _nba_today().isoformat()
    if _CRON_BUSY_NBA:
        return {"ran": False, "cached": bool(_cache_get("nba", ds)), "date": ds, "reason": "already running"}
    _CRON_BUSY_NBA = True
    try:
        await run_analysis(ds)
    finally:
        _CRON_BUSY_NBA = False
    return {"ran": True, "cached": bool(_cache_get("nba", ds)), "date": ds}


@app.get("/api/cached")
async def cached_nba(request: Request, target_date: str = None):
    # Read-only: serve picks already saved on file for this date. Never runs the
    # pipeline, so any logged-in member can pull saved picks without a fresh run.
    from fastapi import HTTPException
    if not get_user(request):
        raise HTTPException(status_code=401, detail="Subscription required — please log in via moneypicksarena.com")
    key = target_date or _nba_today().isoformat()
    data = _cache_get("nba", key, ignore_ttl=True)
    if data is None:
        # Server file gone (restart/redeploy) -> durable saved live board.
        data = await asyncio.to_thread(_nba_read_live_board, key)
    # Past dates: a saved live run is still that day's run; only retired
    # non-sportsbook replays are refused.
    if (data and key < _nba_today().isoformat() and data.get("historical_replay")
            and data.get("historical_replay_schema") != HISTORICAL_REPLAY_SCHEMA):
        data = None
    if data:
        return await _nba_refresh_season_scope(key, data)
    raise HTTPException(status_code=404, detail="No saved picks for this date.")

@app.get("/clear-cache")
async def clear_cache(request: Request):
    global _cache
    _cache = {}
    _cache_clear('nba')   # wipe disk-cached picks file too
    return {"status": "cleared"}

def _nba_track_record_response(overflow: bool = False) -> dict:
    """Split saved selections at read time; never migrate settled ledger rows."""
    det_rows = _nba_hist_sb_get({"app":f"eq.{_NBA_TRK_APP}","category":"eq.__detail__",
                            "locked":"eq.true","select":"date,detail","limit":"365"})
    detail_by_date = {r["date"]:(r.get("detail") or []) for r in (det_rows or [])}
    # Saved pregame picks that are not graded/locked yet show as PENDING
    # (never counted in W/L, profit or ROI) instead of being invisible.
    pending_dates = set()
    try:
        cutoff = (_nba_today() - timedelta(days=10)).isoformat()
        for d in _nba_list_snap_dates():
            if d in detail_by_date or d < cutoff:
                continue
            snap = _nba_load_picks_snapshot(d)
            if not snap:
                continue
            detail_by_date[d] = [{**{k: p.get(k) for k in
                ("name","team","category","side","stat_key","line","odds","book",
                 "rank","is_overflow","capture_scope")}, "result": "PENDING",
                 "actual": None, "profit": None} for p in snap]
            pending_dates.add(d)
    except Exception as exc:
        print(f"[nba_track] pending snapshot read failed: {exc}")
    dates = sorted(detail_by_date.keys(), reverse=True)
    result = []
    for d in dates:
        det = []
        for saved in detail_by_date[d]:
            is_overflow = bool(saved.get("is_overflow")) or saved.get("category") == "NBA Overflow"
            if is_overflow != overflow:
                continue
            row = dict(saved)
            if overflow:
                row["category"] = _NBA_STAT_LABEL.get(
                    row.get("stat_key"), row.get("stat_key") or "Market unavailable")
            row["is_overflow"] = is_overflow
            row["stake"] = _NBA_TRK_STAKE if _nba_trk_priced(row.get("odds")) else None
            if row["stake"] is None:
                row["odds"] = None
                row["profit"] = None
            det.append(row)
        if not det:
            continue
        decided = [r for r in det if r.get("result") in ("WIN","LOSS")]
        priced = [r for r in decided if _nba_trk_priced(r.get("odds"))]
        wins   = sum(1 for r in decided if r["result"]=="WIN")
        losses = len(decided)-wins
        net_pl = round(sum(_nba_american_profit_trk(r["odds"], _NBA_TRK_STAKE, r["result"])
                           for r in priced),2)
        staked = len(priced)*_NBA_TRK_STAKE
        roi    = round(net_pl/staked*100,1) if staked else None
        cats: dict = {}
        for r in decided:
            cat = r.get("category","?")
            side = r.get("side") or "OVER"
            e = cats.setdefault((cat,side),{"wins":0,"losses":0,"pl":0.0,"staked":0.0})
            if r["result"]=="WIN": e["wins"]+=1
            else: e["losses"]+=1
            if _nba_trk_priced(r.get("odds")):
                e["pl"] = round(e["pl"] + _nba_american_profit_trk(
                    r["odds"], _NBA_TRK_STAKE, r["result"]),2)
                e["staked"] += _NBA_TRK_STAKE
        by_cat=[]
        for (cat,side),e in cats.items():
            total=e["wins"]+e["losses"]
            by_cat.append({"category":cat,"side":side,"wins":e["wins"],"losses":e["losses"],
                           "net_pl":e["pl"],
                           "roi":round(e["pl"]/e["staked"]*100,1) if e["staked"] else None,
                           "rate":round(e["wins"]/total*100,1) if total else None})
        by_cat.sort(key=lambda x:(x.get("roi") or -999),reverse=True)
        result.append({"date":d,"wins":wins,"losses":losses,
                       "pending": d in pending_dates,
                       "net_pl":net_pl,"roi":roi,"staked":staked,
                       "priced_plays":len(priced),"by_cat":by_cat,"detail":det})
    return {"dates":result,"stake":_NBA_TRK_STAKE,
            "record":"overflow" if overflow else "main"}


@app.get("/api/track-record")
async def nba_track_record():
    """Ordinary Main only; overflow never contributes to these totals."""
    from fastapi.responses import JSONResponse as _JR
    # Grade finished slates before answering (bounded) so yesterday appears
    # on the first open; a slow ESPN keeps grading in the background.
    try:
        await asyncio.wait_for(asyncio.to_thread(_nba_update_track_ledger), timeout=40)
    except Exception as exc:
        print(f"[nba_track] grading still running/failed on open: {exc!r}")
    try:
        return _JR(await asyncio.to_thread(_nba_track_record_response, False))
    except RuntimeError:
        return _JR({"detail":"Saved NBA Main record is unavailable. Please retry."}, status_code=503)


@app.get("/api/hot-cold-track-record")
async def nba_hot_cold_track_record():
    """Hot / Cold spotlight record (Top 10 + ranks 11-20 per form x market).
    Kept out of the Main and Overflow totals."""
    from fastapi.responses import JSONResponse as _JR
    try:
        await asyncio.wait_for(asyncio.to_thread(_nba_update_hc_ledger), timeout=40)
    except Exception as exc:
        print(f"[nba_hc_track] grading still running/failed on open: {exc!r}")
    try:
        return _JR(await asyncio.to_thread(_nba_hc_record_response))
    except RuntimeError:
        return _JR({"detail":"Saved Hot / Cold record is unavailable. Please retry."}, status_code=503)


@app.get("/api/overflow-track-record")
async def nba_overflow_track_record():
    """Read saved overflow grades with original markets; no grading on open."""
    from fastapi.responses import JSONResponse as _JR
    try:
        return _JR(await asyncio.to_thread(_nba_track_record_response, True))
    except RuntimeError:
        return _JR({"detail":"Saved NBA Overflow record is unavailable. Please retry."}, status_code=503)

@app.get("/api/nba/day-of-week-record")
async def nba_day_of_week_record(request: Request, lane: str = "all"):
    """Read-only weekday report source: saved graded Main and/or Overflow
    rows, or the separate Edge Coach record. Never starts grading and never
    feeds picks."""
    from fastapi.responses import JSONResponse as _JR
    lane = lane if lane in ("main", "overflow", "coach", "hotcold") else "all"
    if lane == "hotcold":
        try:
            data = await asyncio.to_thread(_nba_hc_record_response)
        except RuntimeError:
            return _JR({"detail": "Saved Hot / Cold record is unavailable. Please retry."},
                       status_code=503)
        dates = [{"date": d["date"],
                  "detail": [r for r in d.get("detail") or [] if r.get("result") != "PENDING"]}
                 for d in data.get("dates") or []]
        return _JR({"dates": [d for d in dates if d["detail"]], "lane": "hotcold"})
    if lane == "coach":
        if not get_user(request):
            return _JR({"detail": "Subscription required — please log in via moneypicksarena.com"},
                       status_code=401)
        try:
            saved = await asyncio.to_thread(_nba_hist_sb_get, {
                "app": f"eq.{_NBA_COACH_APP}", "category": f"eq.{_NBA_COACH_DETAIL_CAT}",
                "locked": "eq.true", "select": "date,detail", "limit": "1000"})
        except RuntimeError:
            return _JR({"detail": "Saved Edge Coach record is unavailable. Please retry."},
                       status_code=503)
        dates = []
        for r in saved or []:
            det = []
            for x in r.get("detail") or []:
                row = dict(x)
                row["market"] = x.get("category") or x.get("stat_label") or x.get("stat") or ""
                row["category"] = x.get("preset") or x.get("category") or "Coach Edge"
                det.append(row)
            if r.get("date") and det:
                dates.append({"date": r["date"], "detail": det})
        dates.sort(key=lambda d: d["date"], reverse=True)
        return _JR({"dates": dates, "lane": "coach"})
    try:
        if lane != "all":
            data = await asyncio.to_thread(_nba_track_record_response, lane == "overflow")
            return _JR({"dates": data["dates"], "lane": lane})
        main_rec = await asyncio.to_thread(_nba_track_record_response, False)
        ovf_rec = await asyncio.to_thread(_nba_track_record_response, True)
    except RuntimeError:
        return _JR({"detail": "Saved NBA records are unavailable. Please retry."}, status_code=503)
    merged: dict = {}
    for rec in (main_rec, ovf_rec):
        for day in rec.get("dates") or []:
            merged.setdefault(day["date"], []).extend(day.get("detail") or [])
    dates = [{"date": d, "detail": merged[d]} for d in sorted(merged, reverse=True)]
    return _JR({"dates": dates, "lane": "all"})


@app.get("/health")
async def health():
    return {"status": "ok", "date": _nba_today().isoformat()}

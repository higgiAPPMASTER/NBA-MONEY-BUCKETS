
# NBA Stats API blocks server IPs. ESPN gives schedule + rosters + player game logs free.

import asyncio, pathlib, time
import json
import os
import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Any

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
    return 'higgi'
    return None

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
}

HIT_RATE_MIN  = 0.70
MIN_GAMES     = 2
MIN_MINUTES   = 10.0
ESPN_SEASONS  = [2026, 2025, 2024, 2023, 2022, 2021, 2020]
TOP_N         = 12

ODDS_API_BASE   = "https://api.the-odds-api.com/v4"
ODDS_MARKET_MAP = {
    "player_points":                    "PTS",
    "player_rebounds":                   "REB",
    "player_assists":                    "AST",
    "player_threes":                     "FG3M",
    "player_points_rebounds_assists":    "PRA",
}
MIN_GAMES     = 3
MIN_MINUTES   = 10.0
ESPN_SEASONS  = [2026, 2025, 2024, 2023, 2022, 2021, 2020]   # ESPN uses season END year — 7 seasons for full career H/A history
TOP_N         = 12

# ─── Cache ────────────────────────────────────────────────────────────────────
_cache: Dict[str, Any] = {}  # kept for compat
# ── File-based Picks Cache ────────────────────────────────────────────────────
import pathlib
_CACHE_DIR = pathlib.Path("/tmp/mpa_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_TTL = 6 * 3600  # 6 hours

def _cache_path(app: str, date_key: str) -> pathlib.Path:
    return _CACHE_DIR / f"{app}_{date_key}.json"

def _cache_get(app: str, date_key: str):
    p = _cache_path(app, date_key)
    try:
        if p.exists() and (time.time() - p.stat().st_mtime) < _CACHE_TTL:
            data = json.loads(p.read_text(encoding="utf-8"))
            print(f"[Cache] FILE HIT {app}/{date_key}")
            return data
    except Exception as e:
        print(f"[Cache] Read error: {e}")
    return None

def _cache_set(app: str, date_key: str, result: dict):
    try:
        _cache_path(app, date_key).write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8")
        print(f"[Cache] FILE SET {app}/{date_key}")
    except Exception as e:
        print(f"[Cache] Write error: {e}")

def _cache_clear(app: str = None):
    for p in _CACHE_DIR.glob("*.json"):
        if app is None or p.name.startswith(app + "_"):
            p.unlink(missing_ok=True)

async def get_today_games(date_str: str = None) -> List[Dict]:
    if date_str:
        today_fmt = datetime.strptime(date_str, '%Y-%m-%d').strftime('%Y%m%d')
    else:
        today_fmt = date.today().strftime('%Y%m%d')

    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={today_fmt}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        data = r.json()

    games = []
    for event in data.get('events', []):
        comps = event['competitions'][0]['competitors']
        home = next((c for c in comps if c['homeAway'] == 'home'), None)
        away = next((c for c in comps if c['homeAway'] == 'away'), None)
        if not home or not away:
            continue
        games.append({
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
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await asyncio.sleep(0.1)
            r = await c.get(url)
            data = r.json()
        return [{'id': p['id'], 'name': p.get('displayName', ''),
                 'jersey': p.get('jersey', ''),
                 'position': (p.get('position') or {}).get('abbreviation', '')}
                for p in data.get('athletes', [])]
    except Exception as e:
        print(f"  Roster error {team_id}: {e}")
        return []


async def get_player_gamelogs_espn(player_id: str, season: int,
                                    sem: asyncio.Semaphore) -> List[Dict]:
    """Fetch one player's game logs for one season from ESPN."""
    url = (f"https://site.web.api.espn.com/apis/common/v3/sports/"
           f"basketball/nba/athletes/{player_id}/gamelog")
    async with sem:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(url, params={'season': season})
                if r.status_code != 200:
                    return []
                gl = r.json()
        except Exception:
            return []

    events = gl.get('events', {})

    # Build eventId → stats map from seasonTypes → categories → events
    stats_map: Dict[str, List] = {}
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
                    stats_map[eid] = ev['stats']

    games = []
    for eid, ev_info in events.items():
        if eid not in stats_map:
            continue
        stats = stats_map[eid]
        if len(stats) < 14:
            continue

        # Skip garbage time / DNP games
        if parse_min(stats[0]) < MIN_MINUTES:
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
            'PTS':         parse_stat(stats[13]),
            'REB':         parse_stat(stats[7]),
            'AST':         parse_stat(stats[8]),
            'FG3M':        parse_stat(stats[3]),
            'PRA':         parse_stat(stats[13]) + parse_stat(stats[7]) + parse_stat(stats[8]),
        })
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

async def get_odds_lines(today_str):
    api_key = os.environ.get('ODDS_API_KEY', '')
    if not api_key:
        return []
    props = []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            # Time-window filter (UTC): include any event starting between
            # 6 hours ago and 48 hours from now. This covers every ET/UTC
            # edge case (e.g. a 10pm ET game = next-day UTC).
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            now_utc = _dt.now(_tz.utc)
            window_start = now_utc - _td(hours=6)
            window_end   = now_utc + _td(hours=48)

            def _in_window(iso_ts: str) -> bool:
                try:
                    t = _dt.fromisoformat(iso_ts.replace('Z', '+00:00'))
                    return window_start <= t <= window_end
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
                        print(f'[OddsAPI] {len(events)} NBA events ({sport_key}) within 48h window')
                        break
                else:
                    print(f'[OddsAPI] events {r.status_code} for {sport_key}: {r.text[:150]}')
            if not events:
                print(f'[OddsAPI] No NBA events found within 48h window (now_utc={now_utc.isoformat()})')
                return []
            markets = ','.join(ODDS_MARKET_MAP.keys())
            for ev in events:
                r2 = await c.get(
                    f"{ODDS_API_BASE}/sports/{active_key}/events/{ev['id']}/odds",
                    params={'apiKey': api_key, 'regions': 'us',
                            'markets': markets, 'oddsFormat': 'american'})
                if r2.status_code != 200:
                    print(f'[OddsAPI] props {r2.status_code} for {ev.get("home_team","?")} game: {r2.text[:150]}')
                    continue
                data = r2.json()
                seen = set()
                for book in data.get('bookmakers', []):
                    for mkt in book.get('markets', []):
                        stat = ODDS_MARKET_MAP.get(mkt.get('key', ''))
                        if not stat:
                            continue
                        for oc in mkt.get('outcomes', []):
                            if oc.get('name') != 'Over':
                                continue
                            player = oc.get('description', '').strip()
                            line   = float(oc.get('point') or 0)
                            key    = f"{player}|{stat}"
                            if player and line > 0 and key not in seen:
                                seen.add(key)
                                props.append({
                                    'player': player, 'stat': stat, 'line': line,
                                    'odds': str(oc.get('price', '')),
                                    'home': data.get('home_team', ''),
                                    'away': data.get('away_team', ''),
                                })
                    # check all bookmakers for best coverage
    except Exception as e:
        print(f'[OddsAPI] error: {e}')
    print(f'[OddsAPI] {len(props)} NBA prop lines fetched')
    return props


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
    if not line or len(recent10) < 6:
        return None, None, None
    evens = recent10[0::2][:5]  # idx 0,2,4,6,8
    odds  = recent10[1::2][:5]  # idx 1,3,5,7,9
    e_hits = sum(1 for g in evens if float(g[sk]) > line)
    o_hits = sum(1 for g in odds  if float(g[sk]) > line)
    e_n, o_n = len(evens), len(odds)
    if e_n < 3 or o_n < 3:
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

async def run_analysis(selected_date: str = None) -> Dict:
    today_str = selected_date if selected_date else date.today().isoformat()
    # File cache check first
    _fc = _cache_get('nba', today_str)
    if _fc:
        _cache.update(_fc)
        return _fc
    if _cache.get('date') == today_str and _cache.get('picks') is not None and _cache.get('odds_loaded'):
        return _cache

    log = []
    log.append(f"Fetching schedule + sportsbook lines for {today_str}...")

    # Fetch games + Odds API lines concurrently
    try:
        odds_raw = await get_odds_lines(today_str)
        # The Odds API is the sole sportsbook line source.
        odds_props = odds_raw
        games = await get_today_games(today_str)
        log.append(f"OddsAPI: {len(odds_raw)} lines")
    except Exception as e:
        return {'date': today_str, 'picks': [], 'all_picks': [], 'games': [],
                'log': [f'Error: {e}'], 'total': 0}

    if not games:
        return {'date': today_str, 'picks': [], 'all_picks': [], 'games': [],
                'log': [f'No NBA games found for {today_str}.'], 'total': 0}

    log.append("Games: " + " | ".join(f"{g['away']} @ {g['home']}" for g in games))
    log.append(f"{len(odds_props)} sportsbook prop lines loaded")

    # Build lookups — odds_lookup is last-seen (compute uses bet365/us2 line
    # when available) which is the behavior the picks have been calibrated
    # against. dk_lookup below is first-seen for display.
    odds_lookup: Dict[tuple, Dict] = {}
    for prop in odds_props:
        key = (_nn(prop['player']), prop['stat'])
        odds_lookup[key] = {'line': prop['line'], 'odds': str(prop.get('odds', ''))}

    # dk_lookup uses Odds API lines as the sole sportsbook source
    dk_lookup: Dict[tuple, Dict] = {}
    for prop in odds_raw:
        key = (_nn(prop['player']), prop['stat'])
        if key not in dk_lookup:
            dk_lookup[key] = {'line': prop['line'], 'over_odds': '', 'under_odds': ''}

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
    total_players = sum(len(v) for v in rosters.values())
    log.append(f"{total_players} players loaded")

    # Fetch game logs (all players, 3 seasons)
    all_player_ids = list({p['id'] for players in rosters.values() for p in players})
    log.append(f"Fetching game logs for {len(all_player_ids)} players x {len(ESPN_SEASONS)} seasons...")
    sem = asyncio.Semaphore(10)

    async def fetch_player_logs(pid: str):
        season_results = await asyncio.gather(
            *[get_player_gamelogs_espn(pid, s, sem) for s in ESPN_SEASONS],
            return_exceptions=True)
        all_logs = [g for res in season_results if isinstance(res, list) for g in res]
        return pid, all_logs

    log_results = await asyncio.gather(*[fetch_player_logs(pid) for pid in all_player_ids])
    # Sort every player's games by date DESCENDING (most recent first).
    # The whole algorithm now uses "last N games in the moment" instead of
    # vs-specific-opponent history, so playoff + recent regular season games
    # flow naturally into picks.
    logs_by_player = {pid: sorted(logs, key=lambda l: l.get('date',''), reverse=True)
                      for pid, logs in log_results}
    total_entries = sum(len(v) for v in logs_by_player.values())
    log.append(f"{total_entries:,} historical game entries loaded")

    # Pattern analysis — original algorithm (find best threshold >=75%)
    log.append("Scanning matchup patterns (70%+ threshold)...")
    picks = []

    for game in games:
        h, a = game['home'], game['away']
        h_name, a_name = game['home_name'], game['away_name']

        for player in rosters.get(game['home_id'], []):
            pid, pname = player['id'], player['name']
            # STARTER/ACTIVE FILTER: only consider players who have a sportsbook
            # line posted today. Books drop lines for inactives and rarely post
            # lines for deep bench players. This solves "pick 1 isn't playing"
            # and the "more starters please" requests in one shot.
            has_any_line = any((_nn(pname), s) in odds_lookup for s in STAT_CONFIG)
            if not has_any_line:
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
                result = find_best_threshold(vals, sc['thresholds'])
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
                # All signals (Line / Streak / MPA Special) are matchup-only
                # vs the opponent. recent10/recent_vals kept for any other use.
                recent10 = all_logs_player[:10]
                recent_vals = [float(l[sk]) for l in recent10]
                line_rec, line_rec_pct, line_rec_hits = _line_pick(dk_line, [float(l[sk]) for l in opp_logs_all[:10]], opp_logs_all[:10], sk)
                streak_rec, streak_n = _streak_pick(dk_line, opp_logs_all, sk)
                alt_rec, alt_evens, alt_odds = _alt_pick(dk_line, opp_logs_all, sk)
                # Conflict resolution: streak (matchup-specific) beats MPA Special (rhythm) when they disagree
                if streak_rec and alt_rec and streak_rec != alt_rec:
                    alt_rec = None
                # Include pick if EITHER consistency pattern OR streak OR MPA Special
                if not result and not streak_rec and not alt_rec:
                    continue
                base = result or {'threshold': 0, 'hits': 0, 'games': len(last10),
                                  'hit_rate': 0.0, 'pct': 0.0}
                l10h = sum(1 for l in last10 if float(l[sk]) >= base['threshold']) if base['threshold'] else 0
                picks.append({**base, 'player': pname, 'player_id': pid, 'team': h,
                              'team_id': game['home_id'],
                              'jersey': player.get('jersey',''), 'position': player.get('position',''),
                              'tipoff': game.get('tipoff',''),
                              'team_name': h_name, 'stat': sk,
                              'stat_label': sc['label'], 'emoji': sc['emoji'],
                              'location': 'Home', 'opp': a, 'opp_name': a_name,
                              'matchup': f"{a_name} @ {h_name}",
                              'l10_hits': l10h, 'l10_games': len(last10),
                              'fd_line': fd_line, 'fd_odds': fd_odds,
                              'l10_sb_hits': l10_sb_hits,
                              'dk_line': dk_line, 'dk_over_odds': dk_over_odds, 'dk_under_odds': dk_under_odds, 'dk_hits': dk_hits,
                              'line_rec': line_rec, 'line_rec_pct': line_rec_pct, 'line_rec_hits': line_rec_hits,
                              'streak_rec': streak_rec, 'streak_n': streak_n,
                              'alt_rec': alt_rec, 'alt_evens': alt_evens, 'alt_odds': alt_odds,
                              'has_consistency': result is not None,
                              'recent_avg': round(sum(recent_vals)/len(recent_vals), 1) if recent_vals else None,
                              'gap': round((sum(recent_vals)/len(recent_vals)) - dk_line, 1) if recent_vals and dk_line else None})

        for player in rosters.get(game['away_id'], []):
            pid, pname = player['id'], player['name']
            has_any_line = any((_nn(pname), s) in odds_lookup for s in STAT_CONFIG)
            if not has_any_line:
                continue
            all_logs_player = logs_by_player.get(pid, [])
            # Last 10 games vs THIS opponent at THIS location (AWAY).
            opp_logs_all = [l for l in all_logs_player
                            if l['opp'] == h and l.get('player_team') == a
                            and l.get('location') == 'Away']
            opp_logs = opp_logs_all[:10]
            for sk, sc in STAT_CONFIG.items():
                vals = [float(l[sk]) for l in opp_logs]
                result = find_best_threshold(vals, sc['thresholds'])
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
                recent10 = all_logs_player[:10]
                recent_vals = [float(l[sk]) for l in recent10]
                line_rec, line_rec_pct, line_rec_hits = _line_pick(dk_line, [float(l[sk]) for l in opp_logs_all[:10]], opp_logs_all[:10], sk)
                streak_rec, streak_n = _streak_pick(dk_line, opp_logs_all, sk)
                alt_rec, alt_evens, alt_odds = _alt_pick(dk_line, opp_logs_all, sk)
                # Conflict resolution: streak (matchup-specific) beats MPA Special (rhythm) when they disagree
                if streak_rec and alt_rec and streak_rec != alt_rec:
                    alt_rec = None
                if not result and not streak_rec and not alt_rec:
                    continue
                base = result or {'threshold': 0, 'hits': 0, 'games': len(last10),
                                  'hit_rate': 0.0, 'pct': 0.0}
                l10h = sum(1 for l in last10 if float(l[sk]) >= base['threshold']) if base['threshold'] else 0
                picks.append({**base, 'player': pname, 'player_id': pid, 'team': a,
                              'team_id': game['away_id'],
                              'jersey': player.get('jersey',''), 'position': player.get('position',''),
                              'tipoff': game.get('tipoff',''),
                              'team_name': a_name, 'stat': sk,
                              'stat_label': sc['label'], 'emoji': sc['emoji'],
                              'location': 'Away', 'opp': h, 'opp_name': h_name,
                              'matchup': f"{a_name} @ {h_name}",
                              'l10_hits': l10h, 'l10_games': len(last10),
                              'fd_line': fd_line, 'fd_odds': fd_odds,
                              'l10_sb_hits': l10_sb_hits,
                              'dk_line': dk_line, 'dk_over_odds': dk_over_odds, 'dk_under_odds': dk_under_odds, 'dk_hits': dk_hits,
                              'line_rec': line_rec, 'line_rec_pct': line_rec_pct, 'line_rec_hits': line_rec_hits,
                              'streak_rec': streak_rec, 'streak_n': streak_n,
                              'alt_rec': alt_rec, 'alt_evens': alt_evens, 'alt_odds': alt_odds,
                              'has_consistency': result is not None,
                              'recent_avg': round(sum(recent_vals)/len(recent_vals), 1) if recent_vals else None,
                              'gap': round((sum(recent_vals)/len(recent_vals)) - dk_line, 1) if recent_vals and dk_line else None})

    # Sort: consistency picks first (by hit rate), then non-consistency
    # streak/MPA picks. None-safe.
    picks.sort(key=lambda x: (x.get('has_consistency', False), x.get('hit_rate') or 0, x.get('threshold') or 0), reverse=True)
    # Take enough picks to surface TOP_N distinct players (cards group by player,
    # so 12 picks from 11 unique players = only 11 cards). Walk the sorted list
    # collecting picks until we hit TOP_N distinct names.
    top_picks = []
    _seen_players = set()
    for _pk in picks:
        top_picks.append(_pk)
        _seen_players.add(_pk['player'])
        if len(_seen_players) >= TOP_N:
            break
    log.append(f"{len(picks)} qualifying patterns -> {len(top_picks)} picks across top {len(_seen_players)} players shown")
    if odds_props:
        with_lines = sum(1 for p in picks if p.get('fd_line'))
        log.append(f"{with_lines} picks have sportsbook lines attached")

    odds_loaded = bool(odds_props)
    props_picks, props_nopick = [], []
    for game in games:
        h,a = game['home'],game['away']
        h_name,a_name = game['home_name'],game['away_name']
        for loc,tid,opp_id,opp_name,side in [('Home',game['home_id'],a,a_name,'HOME'),('Away',game['away_id'],h,h_name,'AWAY')]:
            for player in rosters.get(tid,[]):
                pname,pid = player['name'],player['id']
                for sk,sc in STAT_CONFIG.items():
                    ob = odds_lookup.get((_nn(pname),sk),{})
                    if not ob or ob.get('line') is None: continue
                    line = float(ob['line'])
                    # Same trade-aware filter: only games with current team vs today's opp at this location
                    cur_team = tid_to_abbr.get(tid, '')
                    opp_logs = [l for l in logs_by_player.get(pid, [])
                                if l['opp'] == opp_id and l['location'] == loc and l.get('player_team') == cur_team][:10]
                    if not opp_logs:
                        props_nopick.append({'player':pname,'stat':sk,'stat_label':sc['label'],'emoji':sc['emoji'],'side':side,'opp_name':opp_name,'line':line,'avg':None,'games':0,'history':'—','gap':None,'pick':None,'fd_odds':ob.get('odds','')})
                        continue
                    vals = [float(l[sk]) for l in opp_logs]
                    avg = round(sum(vals)/len(vals),1)
                    gap = round(avg-line,1)
                    pick = 'OVER' if avg>line else ('UNDER' if avg<line else None)
                    entry = {'player':pname,'stat':sk,'stat_label':sc['label'],'emoji':sc['emoji'],'side':side,'opp_name':opp_name,'line':line,'avg':avg,'games':len(vals),'history':','.join(str(int(v)) for v in vals[:8]),'gap':gap,'pick':pick,'fd_odds':ob.get('odds','')}
                    (props_picks if pick else props_nopick).append(entry)
    props_picks.sort(key=lambda x:abs(x.get('gap') or 0),reverse=True)
    log.append(f"Props: {len(props_picks)} picks")
    result = {'date':today_str,'picks':top_picks,'all_picks':picks,'games':games,'log':log,'total':len(picks),'odds_loaded':odds_loaded,'props_picks':props_picks,'props_nopick':props_nopick}
    _cache.update(result)
    # Only cache if we actually got prop lines from the Odds API.
    # Otherwise the empty result gets pinned for 6h even after sportsbooks post lines.
    has_lines = bool(props_picks) or bool(props_nopick)
    if has_lines:
        _cache_set("nba", today_str, result)
    else:
        print(f"[Cache] SKIP write — no prop lines yet for {today_str} (will retry on next request)")
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
  var HUB='https://www.moneypicksarena.com';
  var KEY='__mpa_token';
  var p=new URLSearchParams(window.location.search);
  var t=p.get('token');
  if(t){localStorage.setItem(KEY,t);window.history.replaceState({},'',window.location.pathname);}
  // no redirect
})();
</script>

<div class="spin-ball"></div>
<div class="card">
  <div class="logo-line">
    <h1>Money Buckets</h1>
  </div>
  <p class="sub">Pattern-Based Matchup Intelligence</p>
  <form method="post" action="/login">
    <div class="field"><span class="fi">👤</span><input name="username" type="text" placeholder="Username" required autocomplete="username"></div>
    <div class="field"><span class="fi">🔒</span><input name="password" type="password" placeholder="Password" required autocomplete="current-password"></div>
    <button class="btn-in" type="submit">Access Picks →</button>
    {error}
  </form>
  <p class="tagline">No Lines · Just Patterns · 70% Threshold</p>
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
.picks-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin-bottom:10px}
.pick-card{background:#161616;border:1px solid #262626;border-radius:20px;padding:22px;position:relative;overflow:hidden;transition:border-color .25s,transform .22s}
.pick-card:hover{border-color:rgba(245,158,11,.4);transform:translateY(-3px);box-shadow:0 14px 40px rgba(0,0,0,.5)}
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
</style>
</head>
<body>
<div class="bg-glow"></div>
<nav><div class="logo">Money <span>Picks</span> Arena</div></nav>
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
  <div style="text-align:center"><button class="btn btn-run" id="runBtn" onclick="runPicks()">Run Picks</button></div>
</div>
<div class="games-bar" id="gamesBar"></div>
<div id="filterBar" style="display:none" class="filter-bar">
  <button class="filter-btn active" onclick="filterStat('ALL')">All Stats</button>
  <button class="filter-btn" onclick="filterStat('PTS')">Points</button>
  <button class="filter-btn" onclick="filterStat('REB')">Rebounds</button>
  <button class="filter-btn" onclick="filterStat('AST')">Assists</button>
  <button class="filter-btn" onclick="filterStat('FG3M')">3-Pointers</button>
  <button class="filter-btn" onclick="filterStat('PRA')">🃏 PRA</button>
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
        <div class="tb-title">All Qualifying Patterns</div>
        <div class="tb-sub">Every player hitting 70%+ · Grouped by game</div>
      </div>
    </div>
    <div class="tb-count" id="totalCount">0</div>
  </div>
  <div class="all-section-hdr">
    <div class="all-section-title">🎯 All Patterns by Game</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap" id="allFilterBar">
      <button class="filter-btn active" onclick="filterAll('ALL')">All</button>
      <button class="filter-btn" onclick="filterAll('PTS')">🏀 Pts</button>
      <button class="filter-btn" onclick="filterAll('REB')">📊 Reb</button>
      <button class="filter-btn" onclick="filterAll('AST')">🎯 Ast</button>
      <button class="filter-btn" onclick="filterAll('FG3M')">🔥 3PM</button>
      <button class="filter-btn" onclick="filterAll('PRA')">🃏 PRA</button>
      <button class="filter-btn" id="oversBtn" onclick="toggleSide('OVER')" style="margin-left:8px">⬆ Overs only</button>
      <button class="filter-btn" id="undersBtn" onclick="toggleSide('UNDER')">⬇ Unders only</button>
    </div>
  </div>
  <div id="allPicksSection"></div>
</div>

<div id="props-section" style="display:none;max-width:1400px;margin:28px auto 0;padding:0 24px 40px">
  <div style="font-size:.85rem;font-weight:900;letter-spacing:2px;text-transform:uppercase;display:flex;align-items:center;gap:10px;font-size:.78rem;font-weight:700;color:#f59e0b;text-transform:uppercase;letter-spacing:.15em;margin:0 0 12px">⚡ Player Props vs Opponent History</div>
  <div style="overflow-x:auto;border-radius:14px;border:1px solid #262626">
    <table style="width:100%;border-collapse:collapse;font-size:.82rem;background:#161616">
      <thead><tr style="border-bottom:1px solid rgba(245,158,11,.2)">
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">#</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">Player</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">Stat</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">H/A</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">Opponent</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">Line</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">Avg vs Opp</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">Games</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">History</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">Pick</th>
      </tr></thead>
      <tbody id="props-body"></tbody>
    </table>
  </div>
  <p style="font-size:.72rem;color:#555;margin-top:8px">
    <strong style="color:#f59e0b">Avg vs Opp</strong> = average vs today's opponent at home/away (incl. playoffs) &nbsp;|&nbsp;
    <strong style="color:#f59e0b">Pick</strong> = O (Over) if avg &gt; line, U (Under) if avg &lt; line
  </p>
</div>
</div><!-- /wrap -->

<footer>
  <div class="ft-logo">Money Picks Arena</div>
  <div>NBA Money Buckets &middot; Pts &middot; Reb &middot; Ast &middot; 3PM</div>
  <div style="margin-top:8px;font-size:.7rem">For entertainment only. Not a betting service. Must be 18+.</div>
</footer>
<script>
// Hub Access Gate - client side only, no server round-trip
(function(){
  var HUB='https://www.moneypicksarena.com';
  var KEY='__mpa_token';
  var p=new URLSearchParams(window.location.search);
  var t=p.get('token');
  if(t){localStorage.setItem(KEY,t);window.history.replaceState({},'',window.location.pathname);}
  var tok=localStorage.getItem(KEY);
  // gate removed — hub handles auth
})();
let top10=[], allPicksData=[], activeTopStat='ALL', activeAllStat='ALL', sideFilter=null;

function pctClass(p){return p>=90?['pct-green','bar-green']:p>=80?['pct-yellow','bar-yellow']:['pct-orange','bar-orange']}
function statTag(s){
  const m={PTS:['tag-pts','Points'],REB:['tag-reb','Rebounds'],AST:['tag-ast','Assists'],FG3M:['tag-fg3m','3-Pointers'],PRA:['tag-pra','Pts+Reb+Ast']};
  const [c,l]=m[s]||['',''];
  return `<span class="stat-tag ${c}">${l}</span>`;
}
function rankClass(i){return i===0?'rank-1':i===1?'rank-2':i===2?'rank-3':'rank-other'}

function filterStat(stat){
  activeTopStat=stat;
  document.querySelectorAll('#filterBar .filter-btn').forEach(b=>{
    const t=b.textContent;
    b.classList.toggle('active',
      stat==='ALL'?t.includes('All'):stat==='PTS'?t.includes('Point'):
      stat==='REB'?t.includes('Rebound'):stat==='AST'?t.includes('Assist'):
      stat==='PRA'?t.includes('PRA'):t.includes('3-Point'));
  });
  renderTop10Cards(stat==='ALL'?top10:top10.filter(p=>p.stat===stat));
}

function filterAll(stat){
  activeAllStat=stat;
  document.querySelectorAll('#allFilterBar .filter-btn').forEach(b=>{
    const t=b.textContent;
    b.classList.toggle('active',
      stat==='ALL'?t==='All':stat==='PTS'?t.endsWith('Pts'):
      stat==='REB'?t.includes('Reb')&&!t.includes('PRA'):
      stat==='AST'?t.includes('Ast')&&!t.includes('PRA'):
      stat==='PRA'?t.includes('PRA'):t.includes('3PM'));
  });
  applyAllFilters();
}

function toggleSide(side){
  sideFilter = (sideFilter===side) ? null : side;
  document.getElementById('oversBtn').classList.toggle('active',sideFilter==='OVER');
  document.getElementById('undersBtn').classList.toggle('active',sideFilter==='UNDER');
  applyAllFilters();
}

function applyAllFilters(){
  let filtered = activeAllStat==='ALL' ? allPicksData : allPicksData.filter(p=>p.stat===activeAllStat);
  if(sideFilter){
    filtered = filtered.filter(p => p.line_rec===sideFilter || p.streak_rec===sideFilter || p.alt_rec===sideFilter);
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
    document.getElementById('content').innerHTML='<div class="msg-card"><span class="ico"></span><h2>No patterns</h2><p>Try "All Stats".</p></div>';
    return;
  }
  // Group picks by player so each player gets ONE trading-card; first occurrence wins rank order.
  const byPlayer={},order=[];
  picks.forEach(p=>{ if(!byPlayer[p.player]){byPlayer[p.player]=[];order.push(p.player);} byPlayer[p.player].push(p); });
  const dirColor=d=>d==='OVER'?'#4ade80':d==='UNDER'?'#f87171':'#9ca3af';
  const dirBg=d=>d==='OVER'?'rgba(74,222,128,.14)':d==='UNDER'?'rgba(239,68,68,.14)':'rgba(156,163,175,.1)';
  let html=`<div class="section-hdr"><div class="section-title">Top Picks Today</div><span class="count-pill">${order.length} player${order.length!==1?'s':''}</span></div><div class="picks-grid">`;
  order.forEach((pname,i)=>{
    // Only show the single best pick per player card (highest-ranked, since
    // picks are pre-sorted by has_consistency desc, hit_rate desc, threshold desc).
    const stats=[byPlayer[pname][0]];
    const p=stats[0];
    const teamLogo=`https://a.espncdn.com/i/teamlogos/nba/500/${(p.team||'').toLowerCase()}.png`;
    const headshot=`https://a.espncdn.com/i/headshots/nba/players/full/${p.player_id}.png`;
    const tip=p.tipoff?new Date(p.tipoff).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',timeZoneName:'short'}):'';
    const statBlocks=stats.map(s=>{
      // VERDICT RULES:
      // 1) PATTERN is the strongest signal. If it qualifies, the pick IS the pattern
      //    (e.g. "PATTERN 5+ REB") — MPA/LINE/STREAK do NOT override it with UNDER.
      // 2) If no pattern, fall back to vote-based verdict from LINE/STREAK/MPA.
      let verdict=null, verdictText='', verdictColor='', verdictBg='';
      if(s.has_consistency){
        verdict='PATTERN';
        verdictText=`PATTERN ${s.threshold}+`;
        verdictColor='#FDB827';
        verdictBg='rgba(253,184,39,.18)';
      } else {
        const votes={OVER:0,UNDER:0};
        if(s.line_rec) votes[s.line_rec]++;
        if(s.streak_rec) votes[s.streak_rec]++;
        if(s.alt_rec) votes[s.alt_rec]++;
        const tot=votes.OVER+votes.UNDER;
        if(tot && votes.OVER!==votes.UNDER){
          verdict=votes.OVER>votes.UNDER?'OVER':'UNDER';
          verdictText=`${verdict}${s.dk_line?' '+s.dk_line:''}`;
          verdictColor=dirColor(verdict);
          verdictBg=dirBg(verdict);
        }
      }
      const verdictPill = verdict ? `<span style="background:${verdictBg};color:${verdictColor};border:1px solid ${verdictColor}66;padding:5px 12px;border-radius:7px;font-size:.92rem;font-weight:900;white-space:nowrap">${verdictText}</span>` : '';
      // Suppress any UNDER signal badges when PATTERN is the pick — they'd contradict
      // the pattern. Only show signals that agree (OVER) as reinforcement.
      const patternOverride = s.has_consistency;
      const badges=[];
      if(s.has_consistency) badges.push(`<span style="background:rgba(253,184,39,.18);color:#FDB827;padding:4px 10px;border-radius:6px;font-size:.82rem;font-weight:800">PATTERN ${s.hits}/${s.games} (${s.pct}%) vs ${p.opp} ${(p.location||'').toLowerCase()}</span>`);
      if(s.line_rec && (!patternOverride || s.line_rec==='OVER')) badges.push(`<span style="background:${dirBg(s.line_rec)};color:${dirColor(s.line_rec)};padding:4px 10px;border-radius:6px;font-size:.82rem;font-weight:800">LINE ${s.line_rec} ${s.line_rec_hits} (${s.line_rec_pct}%)</span>`);
      if(s.streak_rec && (!patternOverride || s.streak_rec==='OVER')) badges.push(`<span style="background:${dirBg(s.streak_rec)};color:${dirColor(s.streak_rec)};padding:4px 10px;border-radius:6px;font-size:.82rem;font-weight:800">🔥 ${s.streak_n} STRAIGHT ${s.streak_rec}</span>`);
      if(s.alt_rec && (!patternOverride || s.alt_rec==='OVER')) badges.push(`<span style="background:${dirBg(s.alt_rec)};color:${dirColor(s.alt_rec)};padding:4px 10px;border-radius:6px;font-size:.82rem;font-weight:800">⭐ MPA ${s.alt_rec}</span>`);
      // Data lines: spell out exactly what the user sees on a bet slip
      const lines=[];
      if(s.dk_line!=null) lines.push(`<div style="font-size:.86rem;color:#ddd;margin-bottom:3px"><strong style="color:#fff">Line ${s.dk_line}</strong> ${s.stat_label}</div>`);
      if(s.dk_line!=null && s.dk_hits!=null){
        const over=s.dk_hits, under=10-over;
        lines.push(`<div style="font-size:.8rem;color:#aaa;margin-bottom:3px">vs line last ${s.l10_games||10} (vs ${p.opp} ${(p.location||'').toLowerCase()}): <span style="color:#4ade80;font-weight:700">${over} over</span> · <span style="color:#f87171;font-weight:700">${under} under</span></div>`);
      }
      if(s.threshold) lines.push(`<div style="font-size:.8rem;color:#aaa;margin-bottom:8px">pattern: hit <strong style="color:#FDB827">${s.threshold}+</strong> ${s.stat_label} in <strong style="color:#fff">${s.hits}/${s.games}</strong> vs ${p.opp} ${(p.location||'').toLowerCase()}</div>`);
      return `<div style="background:#0d0d0d;border:1px solid #1f1f1f;border-radius:10px;padding:12px 14px;margin-top:9px">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:7px">
          <div style="font-size:.98rem;min-width:0"><span>${s.emoji}</span> <strong style="color:#fff">${s.stat_label}</strong></div>
          ${verdictPill}
        </div>
        ${lines.join('')}
        <div style="display:flex;flex-wrap:wrap;gap:6px">${badges.join('')}</div>
      </div>`;
    }).join('');
    html+=`
    <div class="pick-card" style="padding:0;overflow:hidden;border-radius:14px;background:linear-gradient(180deg,#161616 0%,#0f0f0f 100%);border:1px solid #262626">
      <div style="background:linear-gradient(135deg,#1e3a5f 0%,#0a1a2e 100%);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid #FDB827">
        <div style="display:flex;align-items:center;gap:10px">
          <div style="width:34px;height:34px;border-radius:50%;background:#FDB827;color:#000;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:1rem">${i+1}</div>
          <div style="font-size:.78rem;letter-spacing:.12em;color:#FDB827;font-weight:800">NBA · ${p.team}</div>
        </div>
        <img src="${teamLogo}" alt="${p.team}" style="height:38px;width:38px;object-fit:contain" onerror="this.style.display='none'"/>
      </div>
      <div style="position:relative;height:160px;background:radial-gradient(ellipse at center top,rgba(253,184,39,.15),transparent 70%),linear-gradient(180deg,#1e3a5f 0%,#0a1a2e 100%);overflow:hidden">
        <img src="${headshot}" alt="${pname}" style="position:absolute;bottom:-6px;left:50%;transform:translateX(-50%);height:170px;object-fit:contain" onerror="this.style.display='none'"/>
        ${p.jersey?`<div style="position:absolute;top:10px;left:12px;background:rgba(0,0,0,.6);color:#FDB827;font-weight:900;font-size:.95rem;padding:4px 10px;border-radius:6px;border:1px solid #FDB827">#${p.jersey}</div>`:''}
        ${p.position?`<div style="position:absolute;top:10px;right:12px;background:rgba(0,0,0,.6);color:#fff;font-weight:800;font-size:.88rem;padding:4px 10px;border-radius:6px;border:1px solid #444">${p.position}</div>`:''}
      </div>
      <div style="background:#FDB827;color:#000;text-align:center;padding:10px 12px;font-weight:900;font-size:1.18rem;letter-spacing:.01em">${pname}</div>
      <div style="padding:12px 14px 14px">
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:.9rem;color:#aaa;margin-bottom:4px">
          <span>vs <strong style="color:#fff">${p.opp}</strong></span>
          ${tip?`<span>⏱ ${tip}</span>`:''}
        </div>
        <div style="font-size:.78rem;color:#666;margin-bottom:4px">${p.matchup}</div>
        ${statBlocks}
      </div>
    </div>`;
  });
  html+='</div>';
  document.getElementById('content').innerHTML=html;
}

function renderAllByGame(picks){
  const el=document.getElementById('allPicksSection');
  if(!picks.length){el.innerHTML='<div class="msg-card" style="padding:30px"><span class="ico"></span><p>No patterns for this filter.</p></div>';return;}
  const groups={},order=[];
  for(const p of picks){if(!groups[p.matchup]){groups[p.matchup]=[];order.push(p.matchup);}groups[p.matchup].push(p);}
  let html='';
  for(const matchup of order){
    const gp=groups[matchup];
    const gameId='g_'+matchup.replace(/[^a-z0-9]/gi,'_');
    html+=`<div class="game-group">
      <div class="game-group-hdr" onclick="toggleGroup('${gameId}',this)">
        <span class="gg-label"> ${matchup}</span>
        <div class="gg-meta"><span class="count-pill">${gp.length} pattern${gp.length!==1?'s':''}</span><span class="gg-chevron"></span></div>
      </div>
      <div class="compact-picks" id="${gameId}">`;
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
            <div style="font-weight:700;color:#fff;font-size:.88rem">${pname} <span style="color:#1e3a5f;font-size:.65rem">${first.team}${first.location==='Home'?' HOME':' AWAY'}</span></div>
            <div style="color:#777;font-size:.7rem;margin-top:2px">${rows.length} pick${rows.length!==1?'s':''} · ${stats}</div>
          </div>
          ${bestPct>0?`<span style="color:#fbbf24;font-weight:700;font-size:.78rem">${bestPct}%</span>`:''}
          <span class="pg-chevron" style="color:#666;font-size:.8rem;transition:transform .2s">▼</span>
        </div>
        <div id="${pid}" style="display:none;flex-direction:column;background:#0e0e0e">`;
      for(const p of rows){
        const [pc,bc]=pctClass(p.pct);
        const badges = [];
        if(p.has_consistency) badges.push(`<span style="background:rgba(245,158,11,.15);color:#fbbf24;padding:2px 7px;border-radius:6px;font-size:.65rem;font-weight:700;margin-right:4px">PATTERN ${p.pct}%</span>`);
        const loc=(p.location||'').toLowerCase();
        if(p.line_rec) badges.push(`<span style="background:${p.line_rec==='UNDER'?'rgba(239,68,68,.15)':'rgba(74,222,128,.15)'};color:${p.line_rec==='UNDER'?'#f87171':'#4ade80'};padding:2px 7px;border-radius:6px;font-size:.65rem;font-weight:700;margin-right:4px">LINE ${p.line_rec} ${p.dk_line} ${p.line_rec_pct}% vs ${p.opp} ${loc}</span>`);
        if(p.streak_rec) badges.push(`<span style="background:rgba(249,115,22,.15);color:#fb923c;padding:2px 7px;border-radius:6px;font-size:.65rem;font-weight:700;margin-right:4px">🔥 ${p.streak_n} in a row ${p.streak_rec} ${p.dk_line} vs ${p.opp} ${loc}</span>`);
        if(p.alt_rec) badges.push(`<span style="background:rgba(168,85,247,.15);color:#c084fc;padding:2px 7px;border-radius:6px;font-size:.65rem;font-weight:700;margin-right:4px">⭐ MPA SPECIAL ${p.alt_rec} ${p.dk_line} vs ${p.opp} ${loc}</span>`);
        const patternLine = p.has_consistency
          ? `${p.threshold}+ ${p.stat_label}  ${p.hits}/${p.games} vs ${p.opp}${p.fd_line ? `  <span class="fd-inline"> ${p.fd_line}</span>` : ''}`
          : `${p.stat_label} vs ${p.opp}${p.dk_line ? `  line ${p.dk_line}` : ''}`;
        html+=`<div style="display:flex;align-items:center;gap:10px;padding:8px 14px 8px 38px;border-top:1px solid #1a1a1a">
          <div style="flex:1;min-width:0">
            <div style="color:#bbb;font-size:.78rem">${patternLine}</div>
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
  const mpas=(picks||[]).filter(p=>p.alt_rec && !(p.alt_rec==='UNDER' && contradicts(p)));
  const sc=document.getElementById('streakCount'); if(sc) sc.textContent=streaks.length;
  const mc=document.getElementById('mpaCount'); if(mc) mc.textContent=mpas.length;
  const dirColor=d=>d==='OVER'?'#4ade80':d==='UNDER'?'#f87171':'#9ca3af';
  const dirBg=d=>d==='OVER'?'rgba(74,222,128,.14)':d==='UNDER'?'rgba(239,68,68,.14)':'rgba(156,163,175,.1)';
  const row=(p,sigHTML)=>`<div style="display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;padding:9px 14px;border-bottom:1px solid #1a1a1a">
    <div style="min-width:0">
      <div style="font-weight:700;color:#fff;font-size:.82rem">${p.emoji} ${p.player} <span style="color:#777;font-size:.7rem">${p.team} vs ${p.opp}</span></div>
      <div style="color:#999;font-size:.7rem;margin-top:2px">${p.stat_label}${p.dk_line?` · line ${p.dk_line}`:''}</div>
    </div>
    ${sigHTML}
  </div>`;
  const sl=document.getElementById('streakList');
  if(sl) sl.innerHTML = streaks.length ? streaks.map(p=>row(p,`<span style="background:${dirBg(p.streak_rec)};color:${dirColor(p.streak_rec)};border:1px solid ${dirColor(p.streak_rec)}55;padding:4px 9px;border-radius:6px;font-weight:900;font-size:.72rem;white-space:nowrap">🔥 ${p.streak_n} ${p.streak_rec}</span>`)).join('') : '<div style="padding:18px;text-align:center;color:#555;font-size:.78rem">No streaks today</div>';
  const ml=document.getElementById('mpaList');
  if(ml) ml.innerHTML = mpas.length ? mpas.map(p=>row(p,`<span style="background:${dirBg(p.alt_rec)};color:${dirColor(p.alt_rec)};border:1px solid ${dirColor(p.alt_rec)}55;padding:4px 9px;border-radius:6px;font-weight:900;font-size:.72rem;white-space:nowrap">⭐ ${p.alt_rec}</span>`)).join('') : '<div style="padding:18px;text-align:center;color:#555;font-size:.78rem">No MPA specials today</div>';
}
function togglePlayer(id,hdr){
  const el=document.getElementById(id);
  if(!el)return;
  const ch=hdr.querySelector('.pg-chevron');
  const hidden=el.style.display==='none';
  el.style.display=hidden?'flex':'none';
  if(ch)ch.style.transform=hidden?'rotate(180deg)':'';
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
  if(!games||!games.length)return;
  var gb=document.getElementById('gamesBar');gb.style.display='flex';
  gb.innerHTML=games.map(g=>
    `<div class="game-chip"><b>${g.away}</b><span class="sep">@</span><b>${g.home}</b></div>`
  ).join('');
}


async function runPicks(){
  const selectedDate=document.getElementById('datePicker').value;
  document.getElementById('content').innerHTML=`
    <div class="msg-card">
      <div class="loading-ball"></div>
      <div class="ball-shadow"></div>
      <h2 style="color:#FDB827">Analyzing Matchup Patterns</h2>
      <p>Pulling data for <strong style="color:#FDB827">${selectedDate}</strong> from NBA Stats API.<br>
      <span style="color:#1e3a5f">This takes ~45 seconds  worth the wait.</span></p>
    </div>`;
  document.getElementById('allPicksWrap').style.display='none';
  try{
    const r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:selectedDate})});
    if(!r.ok)throw new Error('Server error '+r.status);
    const data=await r.json();
    renderGames(data.games);
    top10=data.picks||[];
    allPicksData=data.all_picks||[];
    activeTopStat='ALL';activeAllStat='ALL';
    const log=data.log||[];
    if(!top10.length){
      document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2>No Qualifying Patterns</h2><p>No 70%+ patterns for today matchups.</p></div><div class="log-box">${log.join('<br>')}</div>`;
      renderPropsSection(data.props_picks, data.props_nopick);
      return;
    }
    document.getElementById('filterBar').style.display='flex';
    renderTop10Cards(top10);
    const lb=document.createElement('div');
    lb.className='log-box';
    lb.innerHTML=log.join('<br>')+`<br> ${data.total} total patterns found`;
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

function renderPropsSection(picks, nopick) {
  var sec  = document.getElementById('props-section');
  var body = document.getElementById('props-body');
  if (!sec || !body) return;
  sec.style.display = 'block';
  var all = (picks||[]).concat((nopick||[]).filter(function(p){return p.games>0;}));
  if (all.length === 0) {
    body.innerHTML = '<tr><td colspan="11" style="text-align:center;padding:20px;color:#555">No prop lines available yet  check back closer to tip-off.</td></tr>';
    return;
  }
  // Signal lookup from allPicksData (PATTERN/LINE/STREAK/MPA) keyed by player|stat
  var sigMap = {};
  (allPicksData||[]).forEach(function(s){ sigMap[s.player+'|'+s.stat] = s; });
  function badgesFor(p){
    var s = sigMap[p.player+'|'+p.stat]; if(!s) return '';
    var out = [];
    if (s.has_consistency) out.push('<span style="background:rgba(245,158,11,.15);color:#fbbf24;padding:1px 6px;border-radius:4px;font-size:.6rem;font-weight:700;margin-right:3px">PATTERN '+s.pct+'%</span>');
    if (s.line_rec) out.push('<span style="background:'+(s.line_rec==='UNDER'?'rgba(239,68,68,.15)':'rgba(74,222,128,.15)')+';color:'+(s.line_rec==='UNDER'?'#f87171':'#4ade80')+';padding:1px 6px;border-radius:4px;font-size:.6rem;font-weight:700;margin-right:3px">LINE '+s.line_rec+' '+s.line_rec_pct+'%</span>');
    if (s.streak_rec) out.push('<span style="background:rgba(249,115,22,.15);color:#fb923c;padding:1px 6px;border-radius:4px;font-size:.6rem;font-weight:700;margin-right:3px">🔥 '+s.streak_n+' '+s.streak_rec+'</span>');
    if (s.alt_rec) out.push('<span style="background:rgba(168,85,247,.15);color:#c084fc;padding:1px 6px;border-radius:4px;font-size:.6rem;font-weight:700;margin-right:3px">⭐ MPA '+s.alt_rec+'</span>');
    return out.length ? '<div style="margin-top:3px;display:flex;flex-wrap:wrap;gap:2px">'+out.join('')+'</div>' : '';
  }
  body.innerHTML = all.map(function(p,i) {
    var isOver=p.pick==='OVER'||p.pick==='O', isUnder=p.pick==='UNDER'||p.pick==='U';
    var clr = isOver?'#4ade80':isUnder?'#f87171':'#555';
    var gap = p.gap!=null?(p.gap>0?'+':'')+p.gap:'';
    var sideBg = p.side==='HOME'?'rgba(253,184,39,.15)':'rgba(99,102,241,.15)';
    return '<tr style="border-bottom:1px solid #1a1a1a">' +
      '<td style="padding:9px 12px;color:#555;vertical-align:top">'+(i+1)+'</td>' +
      '<td style="padding:9px 12px;font-weight:700">'+p.player+badgesFor(p)+'</td>' +
      '<td style="padding:9px 12px;color:#FDB827;font-size:.8rem">'+p.emoji+' '+p.stat_label+'</td>' +
      '<td style="padding:9px 12px"><span style="background:'+sideBg+';padding:2px 7px;border-radius:4px;font-size:.75rem">'+p.side+'</span></td>' +
      '<td style="padding:9px 12px;color:#999;font-size:.8rem">'+p.opp_name+'</td>' +
      '<td style="padding:9px 12px;font-family:monospace;font-weight:700">'+p.line+'</td>' +
      '<td style="padding:9px 12px;font-family:monospace;font-weight:700;color:'+clr+';font-size:1rem">'+(p.avg!=null?p.avg:'')+'</td>' +
      '<td style="padding:9px 12px;color:#555">'+p.games+'g</td>' +
      '<td style="padding:9px 12px;font-family:monospace;font-size:.7rem;color:#555;max-width:130px">'+p.history+'</td>' +
      '<td style="padding:9px 12px"><span style="color:'+clr+';font-weight:900;font-size:.95rem">'+(p.pick==='OVER'?'O':p.pick==='UNDER'?'U':(p.pick||''))+'</span></td>' +
      '</tr>';
  }).join('');
}

// Snapshot mode: hub serves this page with picks baked in as
// window.__INITIAL_PICKS__ — skip the /run fetch and render directly.
document.addEventListener('DOMContentLoaded', function(){
  if (!window.__INITIAL_PICKS__) return;
  try {
    var data = window.__INITIAL_PICKS__;
    var dp = document.getElementById('datePicker');
    if (dp && data.date) dp.value = data.date;
    if (data.games) renderGames(data.games);
    top10        = data.picks || [];
    allPicksData = data.all_picks || [];
    activeTopStat = 'ALL'; activeAllStat = 'ALL';
    if (top10.length) {
      var fb = document.getElementById('filterBar');
      if (fb) fb.style.display = 'flex';
      renderTop10Cards(top10);
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
</script>
</body>

</body>
</html>"""

# ─── Routes ───────────────────────────────────────────────────────────────────
@app.get("/api/verify-token")
async def verify_token_nba(request: Request):
    from fastapi import HTTPException
    auth = request.headers.get("Authorization", "")
    tok = auth.replace("Bearer ", "").strip()
    if not tok or len(tok.split(".")) != 3:
        raise HTTPException(status_code=401, detail="Invalid token")
    from fastapi.responses import JSONResponse
    return JSONResponse({"ok": True})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    today_iso = date.today().isoformat()
    tomorrow_iso = (date.today() + timedelta(days=1)).isoformat()
    return HTMLResponse(MAIN_HTML.replace("__TODAY__", today_iso).replace("__TOMORROW__", tomorrow_iso))

@app.get("/login")
async def login_get():
    return RedirectResponse("https://www.moneypicksarena.com")

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
    from datetime import date as _date
    today = _date.today().isoformat()
    cached = _cache_get("nba", today)
    if cached:
        return {"ok": True, "source": "cache", "date": today,
                "picks": len(cached.get("picks", []))}
    try:
        result = await run_logic()
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
        selected_date = body.get('date', date.today().isoformat())
    except Exception:
        selected_date = date.today().isoformat()
    result = await run_analysis(selected_date)
    return result

@app.get("/clear-cache")
async def clear_cache(request: Request):
    global _cache
    _cache = {}
    _cache_clear('nba')   # wipe disk-cached picks file too
    return {"status": "cleared"}

@app.get("/health")
async def health():
    return {"status": "ok", "date": date.today().isoformat()}

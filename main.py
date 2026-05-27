# NBA Money Buckets — main.py (v2 — 100% ESPN API, no NBA Stats API)
# NBA Stats API blocks server IPs. ESPN gives schedule + rosters + player game logs free.

import asyncio, pathlib, time
import json
import os
import hashlib
import re
import unicodedata
from datetime import date, datetime
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
}

HIT_RATE_MIN  = 0.75
MIN_GAMES     = 2
MIN_MINUTES   = 10.0
ESPN_SEASONS  = [2026, 2025, 2024, 2023, 2022, 2021, 2020]
TOP_N         = 10

ODDS_API_BASE   = "https://api.the-odds-api.com/v4"
ODDS_MARKET_MAP = {
    "player_points":        "PTS",
    "player_rebounds":      "REB",
    "player_assists":       "AST",
    "player_threes_scored": "FG3M",
}
MIN_GAMES     = 3
MIN_MINUTES   = 10.0
ESPN_SEASONS  = [2026, 2025, 2024, 2023, 2022, 2021, 2020]   # ESPN uses season END year — 7 seasons for full career H/A history
TOP_N         = 10

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
            'home':      home['team']['abbreviation'],
            'away':      away['team']['abbreviation'],
            'home_id':   home['team']['id'],
            'away_id':   away['team']['id'],
            'home_name': home['team']['displayName'],
            'away_name': away['team']['displayName'],
        })
    return games


async def get_team_roster_espn(team_id: str) -> List[Dict]:
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await asyncio.sleep(0.1)
            r = await c.get(url)
            data = r.json()
        return [{'id': p['id'], 'name': p.get('displayName', '')}
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
        opp_abbr = opp_info.get('abbreviation', '') if isinstance(opp_info, dict) else ''
        location = 'Away' if ev_info.get('atVs', '') == '@' else 'Home'

        games.append({
            'opp':      opp_abbr,
            'location': location,
            'PTS':      parse_stat(stats[13]),
            'REB':      parse_stat(stats[7]),
            'AST':      parse_stat(stats[8]),
            'FG3M':     parse_stat(stats[3]),
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


PRIZEPICKS_STAT_MAP = {"Points":"PTS","Rebounds":"REB","Assists":"AST","3-PT Made":"FG3M"}

async def get_prizepicks_lines():
    props = []
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent":"Mozilla/5.0","Referer":"https://app.prizepicks.com/"}) as client:
            r = await client.get("https://api.prizepicks.com/projections", params={"league_id":7,"per_page":250,"single_stat":"true"})
            if r.status_code != 200:
                return []
            data = r.json()
            player_map = {item["id"]: item["attributes"].get("name","") for item in data.get("included",[]) if item.get("type")=="new_player"}
            for proj in data.get("data",[]):
                attrs = proj.get("attributes",{})
                stat  = PRIZEPICKS_STAT_MAP.get(attrs.get("stat_type",""))
                if not stat: continue
                line  = float(attrs.get("line_score") or 0)
                if line <= 0: continue
                pid  = proj.get("relationships",{}).get("new_player",{}).get("data",{}).get("id","")
                name = player_map.get(pid, attrs.get("description",""))
                if name:
                    props.append({"player":name,"stat":stat,"line":line,"odds":"","home":"","away":""})
    except Exception as e:
        print(f"[PrizePicks] {e}")
    print(f"[PrizePicks] {len(props)} lines")
    return props


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
                    params={'apiKey': api_key, 'regions': 'us,us2',
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
        pp, ud_lines, odds_raw = await asyncio.gather(
            get_prizepicks_lines(),
            get_underdog_lines(),
            get_odds_lines(today_str)
        )
        # PrizePicks = pattern picks source
        # Underdog = real sportsbook O/U lines (shown as DK line)
        # Odds API = extra backup
        # Odds API lines first (real sportsbook lines), PrizePicks as fallback
        seen = {f"{p['player']}|{p['stat']}" for p in odds_raw}
        odds_props = odds_raw + [p for p in pp if f"{p['player']}|{p['stat']}" not in seen]
        games = await get_today_games(today_str)
        log.append(f"PrizePicks: {len(pp)} | Underdog O/U: {len(ud_lines)} | OddsAPI: {len(odds_raw)} lines")
    except Exception as e:
        return {'date': today_str, 'picks': [], 'all_picks': [], 'games': [],
                'log': [f'Error: {e}'], 'total': 0}

    if not games:
        return {'date': today_str, 'picks': [], 'all_picks': [], 'games': [],
                'log': [f'No NBA games found for {today_str}.'], 'total': 0}

    log.append("Games: " + " | ".join(f"{g['away']} @ {g['home']}" for g in games))
    log.append(f"{len(odds_props)} sportsbook prop lines loaded")

    # Build lookups
    odds_lookup: Dict[tuple, Dict] = {}
    for prop in odds_props:
        key = (_nn(prop['player']), prop['stat'])
        odds_lookup[key] = {'line': prop['line'], 'odds': str(prop.get('odds', ''))}

    # PrizePicks lookup (for PP Line column display)
    pp_lookup: Dict[tuple, Dict] = {}
    for prop in pp:
        key = (_nn(prop['player']), prop['stat'])
        pp_lookup[key] = {'line': prop['line']}

    # dk_lookup uses Odds API lines as the sole sportsbook source
    dk_lookup: Dict[tuple, Dict] = {}
    for prop in odds_raw:
        key = (_nn(prop['player']), prop['stat'])
        if key not in dk_lookup:
            dk_lookup[key] = {'line': prop['line'], 'over_odds': '', 'under_odds': ''}

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
    logs_by_player = dict(log_results)
    total_entries = sum(len(v) for v in logs_by_player.values())
    log.append(f"{total_entries:,} historical game entries loaded")

    # Pattern analysis — original algorithm (find best threshold >=75%)
    log.append("Scanning matchup patterns (75%+ threshold)...")
    picks = []

    for game in games:
        h, a = game['home'], game['away']
        h_name, a_name = game['home_name'], game['away_name']

        for player in rosters.get(game['home_id'], []):
            pid, pname = player['id'], player['name']
            opp_logs = [l for l in logs_by_player.get(pid, [])
                        if l['location'] == 'Home' and l['opp'] == a]
            for sk, sc in STAT_CONFIG.items():
                vals = [float(l[sk]) for l in opp_logs]
                result = find_best_threshold(vals, sc['thresholds'])
                if result:
                    last10    = opp_logs[:10]
                    l10h      = sum(1 for l in last10 if float(l[sk]) >= result['threshold'])
                    # Odds API line for this player+stat
                    sb        = odds_lookup.get((_nn(pname), sk), {})
                    fd_line   = sb.get('line')
                    fd_odds   = sb.get('odds', '')
                    # Last 10 vs team over sportsbook line
                    l10_sb_hits = sum(1 for l in last10 if float(l[sk]) > fd_line) if fd_line and last10 else None
                    dk_ob = dk_lookup.get((_nn(pname), sk), {})
                    dk_line = dk_ob.get('line')
                    dk_over_odds  = dk_ob.get('over_odds', '')
                    dk_under_odds = dk_ob.get('under_odds', '')
                    dk_hits = sum(1 for l in last10 if float(l[sk]) > dk_line) if dk_line and last10 else None
                    picks.append({**result, 'player': pname, 'player_id': pid, 'team': h,
                                  'team_name': h_name, 'stat': sk,
                                  'stat_label': sc['label'], 'emoji': sc['emoji'],
                                  'location': 'Home', 'opp': a, 'opp_name': a_name,
                                  'matchup': f"{a_name} @ {h_name}",
                                  'l10_hits': l10h, 'l10_games': len(last10),
                                  'fd_line': fd_line, 'fd_odds': fd_odds,
                                  'l10_sb_hits': l10_sb_hits,
                                  'dk_line': dk_line, 'dk_over_odds': dk_over_odds, 'dk_under_odds': dk_under_odds, 'dk_hits': dk_hits,
                                  'pp_line': pp_lookup.get((_nn(pname), sk), {}).get('line'),
                                  'pp_hits': sum(1 for l in last10 if float(l[sk]) > pp_lookup.get((_nn(pname), sk), {}).get('line', 9999)) if pp_lookup.get((_nn(pname), sk), {}).get('line') and last10 else None})

        for player in rosters.get(game['away_id'], []):
            pid, pname = player['id'], player['name']
            opp_logs = [l for l in logs_by_player.get(pid, [])
                        if l['location'] == 'Away' and l['opp'] == h]
            for sk, sc in STAT_CONFIG.items():
                vals = [float(l[sk]) for l in opp_logs]
                result = find_best_threshold(vals, sc['thresholds'])
                if result:
                    last10    = opp_logs[:10]
                    l10h      = sum(1 for l in last10 if float(l[sk]) >= result['threshold'])
                    sb        = odds_lookup.get((_nn(pname), sk), {})
                    fd_line   = sb.get('line')
                    fd_odds   = sb.get('odds', '')
                    l10_sb_hits = sum(1 for l in last10 if float(l[sk]) > fd_line) if fd_line and last10 else None
                    dk_ob = dk_lookup.get((_nn(pname), sk), {})
                    dk_line = dk_ob.get('line')
                    dk_over_odds  = dk_ob.get('over_odds', '')
                    dk_under_odds = dk_ob.get('under_odds', '')
                    dk_hits = sum(1 for l in last10 if float(l[sk]) > dk_line) if dk_line and last10 else None
                    picks.append({**result, 'player': pname, 'player_id': pid, 'team': a,
                                  'team_name': a_name, 'stat': sk,
                                  'stat_label': sc['label'], 'emoji': sc['emoji'],
                                  'location': 'Away', 'opp': h, 'opp_name': h_name,
                                  'matchup': f"{a_name} @ {h_name}",
                                  'l10_hits': l10h, 'l10_games': len(last10),
                                  'fd_line': fd_line, 'fd_odds': fd_odds,
                                  'l10_sb_hits': l10_sb_hits,
                                  'dk_line': dk_line, 'dk_over_odds': dk_over_odds, 'dk_under_odds': dk_under_odds, 'dk_hits': dk_hits,
                                  'pp_line': pp_lookup.get((_nn(pname), sk), {}).get('line'),
                                  'pp_hits': sum(1 for l in last10 if float(l[sk]) > pp_lookup.get((_nn(pname), sk), {}).get('line', 9999)) if pp_lookup.get((_nn(pname), sk), {}).get('line') and last10 else None})

    picks.sort(key=lambda x: (x['hit_rate'], x['threshold']), reverse=True)
    top_picks = picks[:TOP_N]
    log.append(f"{len(picks)} qualifying patterns -> top {TOP_N} shown")
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
                    opp_logs = [l for l in logs_by_player.get(pid,[]) if l['location']==loc and l['opp']==opp_id]
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
    # Only cache if we actually got prop lines from PrizePicks or Odds API.
    # Otherwise the empty result gets pinned for 6h even after sportsbooks post lines.
    has_lines = bool(props_picks) or bool(props_nopick)
    if has_lines:
        _cache_set("nba", today_str, result)
    else:
        print(f"[Cache] SKIP write — no prop lines yet for {today_str} (will retry on next request)")
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
  <p class="tagline">No Lines · Just Patterns · 75% Threshold</p>
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
    <input type="date" id="datePicker" value="__TODAY__">
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
</div>
<div id="content"></div>
<div id="allPicksWrap" style="display:none">
  <div class="total-banner">
    <div class="tb-left">
      <div class="tb-ico">📋</div>
      <div>
        <div class="tb-title">All Qualifying Patterns</div>
        <div class="tb-sub">Every player hitting 75%+ · Grouped by game</div>
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
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">Gap</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">Games</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">History</th>
        <th style="padding:12px 14px;text-align:left;color:#f59e0b;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;background:#1a1a1a;white-space:nowrap">Pick</th>
      </tr></thead>
      <tbody id="props-body"></tbody>
    </table>
  </div>
  <p style="font-size:.72rem;color:#555;margin-top:8px">
    <strong style="color:#f59e0b">Avg vs Opp</strong> = career H/A avg vs today's opponent &nbsp;|&nbsp;
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
let top10=[], allPicksData=[], activeTopStat='ALL', activeAllStat='ALL';

function pctClass(p){return p>=90?['pct-green','bar-green']:p>=80?['pct-yellow','bar-yellow']:['pct-orange','bar-orange']}
function statTag(s){
  const m={PTS:['tag-pts','Points'],REB:['tag-reb','Rebounds'],AST:['tag-ast','Assists'],FG3M:['tag-fg3m','3-Pointers']};
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
      stat==='REB'?t.includes('Rebound'):stat==='AST'?t.includes('Assist'):t.includes('3-Point'));
  });
  renderTop10Cards(stat==='ALL'?top10:top10.filter(p=>p.stat===stat));
}

function filterAll(stat){
  activeAllStat=stat;
  document.querySelectorAll('#allFilterBar .filter-btn').forEach(b=>{
    const t=b.textContent;
    b.classList.toggle('active',
      stat==='ALL'?t==='All':stat==='PTS'?t.includes('Pt'):
      stat==='REB'?t.includes('Reb'):stat==='AST'?t.includes('Ast'):t.includes('3PM'));
  });
  const filtered=stat==='ALL'?allPicksData:allPicksData.filter(p=>p.stat===stat);
  document.getElementById('totalCount').textContent=filtered.length;
  renderAllByGame(filtered);
}

function renderTop10Cards(picks){
  if(!picks.length){
    document.getElementById('content').innerHTML='<div class="msg-card"><span class="ico"></span><h2>No patterns</h2><p>Try "All Stats".</p></div>';
    return;
  }
  let html=`<div class="section-hdr"><div class="section-title"> Top 10 Picks Today</div><span class="count-pill">${picks.length} pick${picks.length!==1?'s':''}</span></div><div class="picks-grid">`;
  picks.forEach((p,i)=>{
    const [pc,bc]=pctClass(p.pct);
    html+=`
    <div class="pick-card">
      <div class="pick-rank ${rankClass(i)}">${i+1}</div>
      <span class="pick-emoji">${p.emoji}</span>
      <div class="pick-player">${p.player}</div>
      <div class="pick-team">${p.team_name} <span class="loc-badge">${p.location==='Home'?' Home':' Away'}</span></div>
      <div class="stat-strip">${statTag(p.stat)}</div>
      <div class="pick-pattern">${p.threshold}+ ${p.stat_label} in ${p.hits} of ${p.games} ${p.location.toLowerCase()} games vs ${p.opp}</div>
      ${p.l10_games > 0 ? `<div class="l10vthr-desc">${p.player.split(" ").pop()} hit ${p.threshold}+ ${p.stat_label} ${p.l10_hits} of ${p.l10_games} last 10 games vs ${p.opp}</div>` : ""}
      ${p.dk_line ? `<div class="fd-line-badge" style="background:rgba(99,102,241,.15);border-color:rgba(99,102,241,.3);margin-top:4px">♠️ Sportsbook Line: <strong>${p.dk_line}</strong> ${p.dk_over_odds ? "O " + p.dk_over_odds : ""} ${p.dk_under_odds ? "U " + p.dk_under_odds : ""} | Hit ${p.dk_line}+: ${p.dk_hits !== null && p.dk_hits !== undefined ? p.dk_hits + "/" + p.l10_games + " vs " + p.opp : ""}</div>` : ""}
      ${p.pp_line ? `<div class="fd-line-badge" style="background:rgba(255,165,0,.12);border-color:rgba(255,165,0,.3);margin-top:4px">🟠 PrizePicks: <strong>${p.pp_line}</strong> | Hit ${p.pp_line}+: ${p.pp_hits !== null && p.pp_hits !== undefined ? p.pp_hits + "/" + p.l10_games + " vs " + p.opp : ""}</div>` : ""}
      <div class="pick-matchup"> Today: ${p.matchup}</div>
      <div class="bar-wrap"><div class="bar-fill ${bc}" style="width:${Math.min(p.pct,100)}%"></div></div>
      <div class="stats-row"><span class="games-chip">${p.hits}/${p.games} games</span><span class="pct ${pc}">${p.pct}%</span></div>
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
    for(const p of gp){
      const [pc,bc]=pctClass(p.pct);
      html+=`<div class="compact-row">
        <span class="cr-emoji">${p.emoji}</span>
        <div class="cr-info">
          <div class="cr-player">${p.player} <span style="color:#1e3a5f;font-size:.65rem">${p.team}${p.location==='Home'?'':''}</span></div>
          <div class="cr-pattern">${p.threshold}+ ${p.stat_label}  ${p.hits}/${p.games} ${p.location.toLowerCase()} vs ${p.opp}${p.fd_line ? `  <span class="fd-inline"> ${p.fd_line}</span>` : ''}</div>
          ${(p.fd_line !== null && p.fd_line !== undefined && p.l10vthr_hits !== null && p.l10vthr_hits !== undefined) ? `<div class="l10vthr-desc" style="font-size:.76rem;margin-top:2px">${Math.ceil(p.fd_line)}+ ${p.stat_label}: ${p.l10vthr_hits}/${p.l10vthr_games} vs ${p.opp}</div>` : ''}
        </div>
        <div class="cr-right">
          <div class="cr-bar-wrap"><div class="cr-bar-fill ${bc}" style="width:${Math.min(p.pct,100)}%"></div></div>
          <div class="cr-pct ${pc}">${p.pct}%</div>
          <div class="cr-sample">${p.hits}/${p.games}</div>
        </div>
      </div>`;
    }
    html+='</div></div>';
  }
  el.innerHTML=html;
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
      document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico"></span><h2>No Qualifying Patterns</h2><p>No 75%+ patterns for today matchups.</p></div><div class="log-box">${log.join('<br>')}</div>`;
      renderPropsSection(data.props_picks, data.props_nopick);
      return;
    }
    document.getElementById('filterBar').style.display='flex';
    renderTop10Cards(top10);
    const lb=document.createElement('div');
    lb.className='log-box';
    lb.innerHTML=log.join('<br>')+`<br> ${data.total} total patterns found`;
    document.getElementById('content').appendChild(lb);
    document.getElementById('totalCount').textContent=allPicksData.length;
    document.getElementById('allPicksWrap').style.display='block';
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
  body.innerHTML = all.map(function(p,i) {
    var isOver=p.pick==='OVER'||p.pick==='O', isUnder=p.pick==='UNDER'||p.pick==='U';
    var clr = isOver?'#4ade80':isUnder?'#f87171':'#555';
    var gap = p.gap!=null?(p.gap>0?'+':'')+p.gap:'';
    var sideBg = p.side==='HOME'?'rgba(253,184,39,.15)':'rgba(99,102,241,.15)';
    return '<tr style="border-bottom:1px solid #1a1a1a">' +
      '<td style="padding:9px 12px;color:#555">'+(i+1)+'</td>' +
      '<td style="padding:9px 12px;font-weight:700">'+p.player+'</td>' +
      '<td style="padding:9px 12px;color:#FDB827;font-size:.8rem">'+p.emoji+' '+p.stat_label+'</td>' +
      '<td style="padding:9px 12px"><span style="background:'+sideBg+';padding:2px 7px;border-radius:4px;font-size:.75rem">'+p.side+'</span></td>' +
      '<td style="padding:9px 12px;color:#999;font-size:.8rem">'+p.opp_name+'</td>' +
      '<td style="padding:9px 12px;font-family:monospace;font-weight:700">'+p.line+'</td>' +
      '<td style="padding:9px 12px;font-family:monospace;font-weight:700;color:'+clr+';font-size:1rem">'+(p.avg!=null?p.avg:'')+'</td>' +
      '<td style="padding:9px 12px;font-family:monospace;color:'+clr+';font-weight:700">'+gap+'</td>' +
      '<td style="padding:9px 12px;color:#555">'+p.games+'g</td>' +
      '<td style="padding:9px 12px;font-family:monospace;font-size:.7rem;color:#555;max-width:130px">'+p.history+'</td>' +
      '<td style="padding:9px 12px"><span style="color:'+clr+';font-weight:900;font-size:.95rem">'+(p.pick==='OVER'?'O':p.pick==='UNDER'?'U':(p.pick||''))+'</span></td>' +
      '</tr>';
  }).join('');
}
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
    return HTMLResponse(MAIN_HTML.replace("__TODAY__", today_iso))

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
    user = get_user(request)
    if not user:
        return {"error": "unauthorized"}
    global _cache
    _cache = {}
    return {"status": "cleared"}

@app.get("/health")
async def health():
    return {"status": "ok", "date": date.today().isoformat()}

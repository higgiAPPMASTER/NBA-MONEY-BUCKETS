# NBA Money Buckets — main.py (v2 — 100% ESPN API, no NBA Stats API)
# NBA Stats API blocks server IPs. ESPN gives schedule + rosters + player game logs free.

import asyncio
import json
import os
import hashlib
import re
import unicodedata
from datetime import date, datetime
from typing import Dict, List, Optional, Any

import httpx
from curl_cffi.requests import AsyncSession as CFSession
from playwright.async_api import async_playwright
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="NBA Money Buckets")

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
    return 'higgi'  # Auth handled by Money Picks Arena hub
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
ESPN_SEASONS  = [2026, 2025, 2024]
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
ESPN_SEASONS  = [2026, 2025, 2024]   # ESPN uses season END year
TOP_N         = 10

# ─── Cache ────────────────────────────────────────────────────────────────────
_cache: Dict[str, Any] = {}

# ─── FanDuel Session ──────────────────────────────────────────────────────────
_fd_cookie: Optional[str] = None
_fd_lock    = asyncio.Lock()

async def get_fd_cookie() -> str:
    global _fd_cookie
    async with _fd_lock:
        if not _fd_cookie:
            _fd_cookie = await _fanduel_login()
    return _fd_cookie

async def _fanduel_login() -> str:
    email    = os.environ.get("FD_EMAIL", "")
    password = os.environ.get("FD_PASSWORD", "")
    if not email or not password:
        return ""
    print("[FanDuel] Logging in...")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        ctx  = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"))
        page = await ctx.new_page()
        try:
            await page.goto("https://sportsbook.fanduel.com/",
                            wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.click("text=Sign In", timeout=8_000)
            except:
                await page.goto("https://sportsbook.fanduel.com/login",
                                wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(2)
            await page.fill(
                "input[type='email'], input[name='username'], input[placeholder*='email' i]",
                email, timeout=10_000)
            await asyncio.sleep(0.5)
            await page.fill("input[type='password']", password, timeout=10_000)
            await page.click("button[type='submit']", timeout=8_000)
            await page.wait_for_load_state("networkidle", timeout=25_000)
        except Exception as e:
            print(f"[FanDuel] Login warning: {e}")
        cookies = await ctx.cookies()
        await browser.close()
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    print(f"[FanDuel] Login done — {len(cookies)} cookies")
    return cookie_str

# ─── NBA FanDuel Props ────────────────────────────────────────────────────────
# Maps our stat keys to FanDuel market name fragments (NBA)
FD_MARKET_MAP = {
    "PTS":  ["points", "pts"],
    "REB":  ["rebounds", "rebound", "reb", "total reb"],
    "AST":  ["assists", "assist", "ast", "total ast"],
    "FG3M": ["threes", "three", "3-point", "3pt", "3 point", "fg3", "made"],
}

def _norm_name(n: str) -> str:
    nfd = unicodedata.normalize("NFD", n)
    s   = nfd.encode("ascii","ignore").decode("ascii").lower()
    return re.sub(r"[^a-z ]","",s).strip()

def _match_player(fd_name: str, esp_name: str) -> bool:
    fn, en = _norm_name(fd_name), _norm_name(esp_name)
    if fn == en: return True
    fp = fn.split(); ep = en.split()
    if len(fp)>=2 and len(ep)>=2:
        return fp[0][0]==ep[0][0] and fp[-1]==ep[-1]
    return False

async def fetch_fd_nba_lines() -> Dict[str, Dict]:
    """Use Playwright to browse FanDuel NBA props page and capture live API responses."""
    if not os.environ.get("FD_EMAIL"):
        return {}
    email    = os.environ.get("FD_EMAIL", "")
    password = os.environ.get("FD_PASSWORD", "")
    lines: Dict[str, Dict] = {}
    captured: list = []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
            ctx  = await browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"))
            page = await ctx.new_page()

            # Capture FanDuel API responses that contain player props
            async def on_response(response):
                try:
                    if ("sbapi" in response.url and response.status == 200
                            and "json" in response.headers.get("content-type","")):
                        data = await response.json()
                        if data.get("attachments",{}).get("markets"):
                            captured.append(data)
                except Exception:
                    pass

            page.on("response", on_response)

            # Login
            try:
                await page.goto("https://sportsbook.fanduel.com/",
                                wait_until="domcontentloaded", timeout=30_000)
                try:
                    await page.click("text=Sign In", timeout=8_000)
                except:
                    await page.goto("https://sportsbook.fanduel.com/login",
                                    wait_until="domcontentloaded", timeout=20_000)
                await asyncio.sleep(2)
                await page.fill(
                    "input[type='email'], input[name='username'], input[placeholder*='email' i]",
                    email, timeout=10_000)
                await asyncio.sleep(0.5)
                await page.fill("input[type='password']", password, timeout=10_000)
                await page.click("button[type='submit']", timeout=8_000)
                await page.wait_for_load_state("networkidle", timeout=25_000)
                print("[FanDuel NBA] Logged in")
            except Exception as e:
                print(f"[FanDuel NBA] Login warning: {e}")

            # Navigate to NBA player props page
            try:
                await page.goto(
                    "https://sportsbook.fanduel.com/sports/basketball/nba",
                    wait_until="networkidle", timeout=30_000)
                await asyncio.sleep(3)  # let props load
                print(f"[FanDuel NBA] NBA page loaded, {len(captured)} API responses captured")
            except Exception as e:
                print(f"[FanDuel NBA] Navigation warning: {e}")

            await browser.close()

        # Also update the global cookie
        global _fd_cookie
        # (cookie is captured indirectly via the Playwright session)

        # Parse all captured API responses
        for data in captured:
            batch = _parse_fd_markets(data)
            for k, v in batch.items():
                if k not in lines:
                    lines[k] = v
                else:
                    lines[k].update(v)

        print(f"[FanDuel NBA] {len(lines)} players with prop lines")
        return lines

    except Exception as e:
        print(f"[FanDuel NBA] error: {e}")
        return {}


def _parse_fd_markets(data: Dict) -> Dict[str, Dict]:
    """Parse markets/runners from a FanDuel API response dict."""
    lines: Dict[str, Dict] = {}
    markets = data.get("attachments",{}).get("markets",{})
    runners = data.get("attachments",{}).get("runners",{})

    for mkt in markets.values():
        mkt_name = mkt.get("marketName","").lower()
        stat_key = None
        for sk, fragments in FD_MARKET_MAP.items():
            if any(f in mkt_name for f in fragments):
                stat_key = sk
                break
        if not stat_key:
            continue

        for rid in mkt.get("runnerIds", []):
            runner   = runners.get(str(rid), {})
            rname    = runner.get("runnerName", "")
            handicap = float(runner.get("handicap") or 0)
            if handicap <= 0:
                continue
            # Skip under outcomes
            if "under" in rname.lower() and "over" not in rname.lower():
                continue
            # Extract player name from various FanDuel formats
            player = rname
            player = re.sub(r"[-\u2013]\s*(over|under)[\s\d\.]*", "", player, flags=re.IGNORECASE)
            player = re.sub(r"^(over|under)[\s\d\.]+[-\u2013]?\s*", "", player, flags=re.IGNORECASE)
            player = re.sub(r"\s*(over|under)[\s\d\.]+$", "", player, flags=re.IGNORECASE)
            player = player.strip().strip("-").strip()
            if not player or len(player) < 3 or player.replace(".","").replace(" ","").isdigit():
                continue
            pkey = _norm_name(player)
            if pkey not in lines:
                lines[pkey] = {"_name": player}
            lines[pkey][stat_key] = handicap

    return lines

def attach_fd_lines(picks: List[Dict], fd_lines: Dict[str, Dict]) -> List[Dict]:
    """Attach FanDuel line to each pick where available."""
    for pick in picks:
        fd_line = None
        for pkey, pdata in fd_lines.items():
            if _match_player(pdata.get("_name",""), pick["player"]):
                fd_line = pdata.get(pick["stat"])
                break
        pick["fd_line"] = fd_line
    return picks

# ─── Helpers ──────────────────────────────────────────────────────────────────
def parse_stat(val) -> int:
    """Handle plain numbers AND made-attempted format like '3-11'."""
    s = str(val)
    if '-' in s:
        s = s.split('-')[0]
    try:
        return int(float(s))
    except Exception:
        return 0

def parse_min(val) -> float:
    s = str(val)
    if ':' in s:
        parts = s.split(':')
        try:
            return float(parts[0]) + float(parts[1]) / 60
        except Exception:
            return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0

def find_best_threshold(values: List[float], thresholds: List[int]) -> Optional[Dict]:
    n = len(values)
    if n < MIN_GAMES:
        return None
    for t in thresholds:
        hits = sum(1 for v in values if v >= t)
        rate = hits / n
        if rate >= HIT_RATE_MIN:
            return {'threshold': t, 'hits': hits, 'games': n,
                    'hit_rate': rate, 'pct': round(rate * 100, 1)}
    return None

# ─── ESPN API Functions ───────────────────────────────────────────────────────
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

async def get_odds_lines(today_str):
    api_key = os.environ.get('ODDS_API_KEY', '')
    if not api_key:
        return []
    props = []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{ODDS_API_BASE}/sports/basketball_nba/events",
                            params={'apiKey': api_key, 'dateFormat': 'iso'})
            if r.status_code != 200:
                print(f'[OddsAPI] events status {r.status_code}')
                return []
            events = [e for e in r.json() if e.get('commence_time','')[:10] == today_str]
            print(f'[OddsAPI] {len(events)} NBA events today')
            markets = ','.join(ODDS_MARKET_MAP.keys())
            for ev in events:
                r2 = await c.get(
                    f"{ODDS_API_BASE}/sports/basketball_nba/events/{ev['id']}/odds",
                    params={'apiKey': api_key, 'regions': 'us,us2',
                            'markets': markets, 'oddsFormat': 'american'})
                if r2.status_code != 200:
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
                    break  # first bookmaker only
    except Exception as e:
        print(f'[OddsAPI] error: {e}')
    print(f'[OddsAPI] {len(props)} NBA prop lines fetched')
    return props

async def run_analysis(selected_date: str = None) -> Dict:
    today_str = selected_date if selected_date else date.today().isoformat()
    if _cache.get('date') == today_str and _cache.get('picks') is not None and _cache.get('odds_loaded'):
        return _cache

    log = []
    log.append(f"Fetching schedule + sportsbook lines for {today_str}...")

    # Fetch games + Odds API lines concurrently
    try:
        games, odds_props = await asyncio.gather(
            get_today_games(today_str),
            get_odds_lines(today_str),
        )
    except Exception as e:
        return {'date': today_str, 'picks': [], 'all_picks': [], 'games': [],
                'log': [f'Error: {e}'], 'total': 0}

    if not games:
        return {'date': today_str, 'picks': [], 'all_picks': [], 'games': [],
                'log': [f'No NBA games found for {today_str}.'], 'total': 0}

    log.append("Games: " + " | ".join(f"{g['away']} @ {g['home']}" for g in games))
    log.append(f"{len(odds_props)} sportsbook prop lines loaded")

    # Build Odds API lookup: (normalised_name, stat) -> {line, odds}
    odds_lookup: Dict[tuple, Dict] = {}
    for prop in odds_props:
        key = (_nn(prop['player']), prop['stat'])
        odds_lookup[key] = {'line': prop['line'], 'odds': str(prop.get('odds', ''))}

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
                    picks.append({**result, 'player': pname, 'player_id': pid, 'team': h,
                                  'team_name': h_name, 'stat': sk,
                                  'stat_label': sc['label'], 'emoji': sc['emoji'],
                                  'location': 'Home', 'opp': a, 'opp_name': a_name,
                                  'matchup': f"{a_name} @ {h_name}",
                                  'l10_hits': l10h, 'l10_games': len(last10),
                                  'fd_line': fd_line, 'fd_odds': fd_odds,
                                  'l10_sb_hits': l10_sb_hits})

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
                    picks.append({**result, 'player': pname, 'player_id': pid, 'team': a,
                                  'team_name': a_name, 'stat': sk,
                                  'stat_label': sc['label'], 'emoji': sc['emoji'],
                                  'location': 'Away', 'opp': h, 'opp_name': h_name,
                                  'matchup': f"{a_name} @ {h_name}",
                                  'l10_hits': l10h, 'l10_games': len(last10),
                                  'fd_line': fd_line, 'fd_odds': fd_odds,
                                  'l10_sb_hits': l10_sb_hits})

    picks.sort(key=lambda x: (x['hit_rate'], x['threshold']), reverse=True)
    top_picks = picks[:TOP_N]
    log.append(f"{len(picks)} qualifying patterns -> top {TOP_N} shown")
    if odds_props:
        with_lines = sum(1 for p in picks if p.get('fd_line'))
        log.append(f"{with_lines} picks have sportsbook lines attached")

    odds_loaded = bool(odds_props)
    result = {'date': today_str, 'picks': top_picks, 'all_picks': picks,
              'games': games, 'log': log, 'total': len(picks), 'odds_loaded': odds_loaded}
    _cache.update(result)
    return result

# ─── HTML ─────────────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NBA Money Buckets</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:#050a14;
  background-image:radial-gradient(ellipse at 50% 0%,rgba(249,115,22,.1) 0%,transparent 55%);
  color:#e0e6f0;font-family:'Segoe UI',system-ui,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:0;
}
/* ── Spinning basketball ── */
.spin-ball{
  width:80px;height:80px;border-radius:50%;
  background:radial-gradient(circle at 38% 35%,#fb923c 0%,#ea580c 55%,#7c2d12 100%);
  border:2px solid #7c2d12;
  position:relative;margin-bottom:24px;
  animation:spinBall 6s linear infinite;
  box-shadow:0 0 40px rgba(249,115,22,.5),0 0 80px rgba(249,115,22,.15);
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
  border:1px solid rgba(30,58,95,.8);border-radius:24px;
  padding:40px 40px 36px;width:390px;text-align:center;
  box-shadow:0 30px 80px rgba(0,0,0,.7),0 0 0 1px rgba(249,115,22,.04),inset 0 1px 0 rgba(255,255,255,.03);
  position:relative;overflow:hidden;
}
.card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,transparent,#f59e0b,#ea580c,#f59e0b,transparent);
}
.logo-line{display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:4px}
h1{
  font-size:1.65rem;font-weight:900;letter-spacing:-.5px;
  background:linear-gradient(135deg,#f59e0b 0%,#fb923c 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.sub{color:#374151;font-size:.75rem;margin-bottom:30px;letter-spacing:1.5px;text-transform:uppercase}
.field{position:relative;margin-bottom:13px}
.fi{position:absolute;left:14px;top:50%;transform:translateY(-50%);opacity:.35;font-size:.9rem;pointer-events:none}
input{
  width:100%;background:rgba(15,23,42,.8);
  border:1px solid rgba(30,58,95,.8);color:#d1d5db;
  padding:13px 16px 13px 42px;border-radius:12px;
  font-size:.95rem;outline:none;transition:border-color .2s,box-shadow .2s;
}
input:focus{border-color:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.12)}
input::placeholder{color:#374151}
.btn-in{
  width:100%;margin-top:8px;
  background:linear-gradient(135deg,#f59e0b,#ea580c);color:#050a14;
  border:none;padding:14px;border-radius:12px;
  font-size:1rem;font-weight:900;letter-spacing:.5px;cursor:pointer;
  box-shadow:0 4px 20px rgba(245,158,11,.35);transition:transform .15s,box-shadow .15s;
}
.btn-in:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(245,158,11,.45)}
.btn-in:active{transform:translateY(0)}
.err{color:#f87171;font-size:.83rem;margin-top:14px;background:rgba(127,29,29,.3);padding:10px 14px;border-radius:10px;border:1px solid rgba(239,68,68,.2)}
.tagline{color:#0f1d2e;font-size:.68rem;margin-top:22px;letter-spacing:2px;text-transform:uppercase}
</style>
</head>
<body>
<div class="spin-ball"></div>
<div class="card">
  <div class="logo-line">
    <h1>NBA Money Buckets</h1>
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
<title>NBA Money Buckets</title>
<style>
/* ─── Reset ─── */
*{box-sizing:border-box;margin:0;padding:0}
:root{--orange:#f97316;--gold:#f59e0b;--blue:#3b82f6;--green:#22c55e;--navy:#0a0f1e;--dark:#050a14;--card:#0f172a;--border:#1e3a5f;--text:#e0e6f0;--muted:#4b5563}
body{
  background:var(--dark);
  background-image:
    radial-gradient(ellipse 100% 35% at 50% 0%,rgba(249,115,22,.07) 0%,transparent 70%),
    linear-gradient(rgba(30,58,95,.1) 1px,transparent 1px),
    linear-gradient(90deg,rgba(30,58,95,.1) 1px,transparent 1px);
  background-size:100% 100%,52px 52px,52px 52px;
  color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;padding:20px;min-height:100vh;
}
/* ─── Basketball SVG animation ─── */
.ball-svg{width:54px;height:54px;animation:spinBall 8s linear infinite;filter:drop-shadow(0 0 10px rgba(249,115,22,.5));flex-shrink:0}
@keyframes spinBall{from{transform:rotate(0)}to{transform:rotate(360deg)}}
/* Loading bounce */
.loading-ball{width:58px;height:58px;border-radius:50%;margin:0 auto 6px;animation:ballBounce .65s ease-in-out infinite;background:radial-gradient(circle at 38% 35%,#fb923c 0%,#ea580c 55%,#7c2d12 100%);border:2px solid #7c2d12;box-shadow:0 0 25px rgba(249,115,22,.45);position:relative}
.loading-ball::before{content:'';position:absolute;inset:-1px;border-radius:50%;border:2.5px solid rgba(124,45,18,.85);border-left-color:transparent;border-right-color:transparent;transform:rotate(30deg)}
.loading-ball::after{content:'';position:absolute;inset:14px;border-radius:50%;border:2px solid rgba(124,45,18,.75);border-top-color:transparent;border-bottom-color:transparent}
@keyframes ballBounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-22px)}}
.ball-shadow{width:38px;height:7px;background:rgba(0,0,0,.5);border-radius:50%;margin:0 auto 18px;animation:shadowPulse .65s ease-in-out infinite}
@keyframes shadowPulse{0%,100%{transform:scaleX(1);opacity:.5}50%{transform:scaleX(.55);opacity:.2}}
/* ─── Header ─── */
header{
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:14px;
  margin-bottom:22px;padding:18px 26px;
  background:linear-gradient(135deg,rgba(13,20,38,.97),rgba(8,12,24,.99));
  border-radius:22px;border:1px solid rgba(30,58,95,.8);
  box-shadow:0 10px 50px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.03);
  position:relative;overflow:hidden;
}
header::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,#f59e0b 30%,#ea580c 70%,transparent)}
.brand{display:flex;align-items:center;gap:14px}
.brand-text h1{
  font-size:1.38rem;font-weight:900;letter-spacing:-.5px;
  background:linear-gradient(135deg,#f59e0b,#fb923c);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.brand-text .sub{font-size:.7rem;color:#1e3a5f;letter-spacing:1.5px;text-transform:uppercase;margin-top:3px}
.actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.date-badge{background:rgba(30,58,95,.5);color:#60a5fa;padding:7px 16px;border-radius:20px;font-size:.81rem;font-weight:600;border:1px solid rgba(59,130,246,.2)}
.btn{padding:10px 22px;border-radius:12px;font-size:.875rem;font-weight:800;cursor:pointer;border:none;transition:all .2s;text-decoration:none;display:inline-block;letter-spacing:.3px}
.btn-run{background:linear-gradient(135deg,#f59e0b,#ea580c);color:#050a14;box-shadow:0 4px 18px rgba(245,158,11,.35)}
.btn-run:hover{transform:translateY(-2px);box-shadow:0 8px 28px rgba(245,158,11,.5)}
.btn-refresh{background:rgba(30,58,95,.5);color:#60a5fa;border:1px solid rgba(59,130,246,.3);
  border-radius:10px;padding:7px 14px;font-size:.82rem;font-weight:600;cursor:pointer;transition:all .2s}
.btn-refresh:hover{background:rgba(59,130,246,.2);color:#93c5fd}
.fd-indicator{display:flex;align-items:center;gap:6px;background:rgba(15,23,42,.7);
  border:1px solid rgba(59,130,246,.2);border-radius:20px;padding:5px 12px;cursor:default}
.fd-dot{width:10px;height:10px;border-radius:50%;background:#334155;flex-shrink:0;
  transition:background .4s;box-shadow:0 0 0 0 transparent}
.fd-dot.checking{background:#f59e0b;animation:pulse-gold .8s infinite}
.fd-dot.connected{background:#22c55e;box-shadow:0 0 8px rgba(34,197,94,.6)}
.fd-dot.disconnected{background:#ef4444}
.fd-label{font-size:.72rem;font-weight:700;color:#64748b;letter-spacing:.5px;text-transform:uppercase}
@keyframes pulse-gold{0%,100%{opacity:1}50%{opacity:.4}}
.btn-out{background:rgba(15,23,42,.8);color:#374151;border:1px solid rgba(30,58,95,.5)}
.btn-out:hover{color:#64748b;border-color:#334155}
/* ─── Games bar ─── */
.games-bar{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;margin-bottom:20px;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.game-chip{
  background:linear-gradient(135deg,rgba(13,20,38,.9),rgba(8,12,24,.95));
  border:1px solid rgba(30,58,95,.7);border-radius:12px;
  padding:9px 18px;white-space:nowrap;font-size:.82rem;flex-shrink:0;
  transition:border-color .2s,box-shadow .2s;cursor:default;
}
.game-chip:hover{border-color:rgba(59,130,246,.5);box-shadow:0 0 14px rgba(59,130,246,.1)}
.game-chip b{color:#e0e6f0;font-weight:700}
.game-chip .sep{color:#1e3a5f;margin:0 5px}
/* ─── Filter bar ─── */
.filter-bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.filter-btn{padding:7px 18px;border-radius:20px;border:1px solid rgba(30,58,95,.6);background:rgba(13,20,38,.7);color:#374151;font-size:.81rem;cursor:pointer;transition:all .2s;font-weight:600}
.filter-btn.active,.filter-btn:hover{background:rgba(30,58,95,.9);color:#93c5fd;border-color:rgba(59,130,246,.4);box-shadow:0 0 12px rgba(59,130,246,.1)}
/* ─── Section headers ─── */
.section-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.section-title{font-size:1rem;font-weight:900;letter-spacing:-.3px;display:flex;align-items:center;gap:8px;background:linear-gradient(135deg,#f59e0b,#fb923c);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.count-pill{background:rgba(30,58,95,.5);color:#60a5fa;padding:4px 14px;border-radius:20px;font-size:.78rem;font-weight:700;border:1px solid rgba(59,130,246,.2)}
/* ─── Pick cards ─── */
.picks-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin-bottom:10px}
.pick-card{
  background:linear-gradient(145deg,rgba(13,20,38,.96),rgba(8,12,24,.99));
  border:1px solid rgba(30,58,95,.7);border-radius:20px;padding:22px;
  position:relative;overflow:hidden;
  transition:border-color .25s,transform .22s,box-shadow .25s;
}
.pick-card:hover{border-color:rgba(245,158,11,.55);transform:translateY(-3px);box-shadow:0 14px 45px rgba(0,0,0,.55),0 0 22px rgba(245,158,11,.1)}
.pick-card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(245,158,11,.25),transparent);opacity:0;transition:opacity .25s}
.pick-card:hover::before{opacity:1}
/* Rank medals */
.pick-rank{position:absolute;top:14px;right:15px;width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.82rem;font-weight:900}
.rank-1{background:linear-gradient(135deg,#92400e,#f59e0b);color:#050a14;box-shadow:0 0 14px rgba(245,158,11,.5)}
.rank-2{background:linear-gradient(135deg,#374151,#9ca3af);color:#050a14;box-shadow:0 0 8px rgba(156,163,175,.2)}
.rank-3{background:linear-gradient(135deg,#7c2d12,#c2410c);color:#fff0e0;box-shadow:0 0 10px rgba(194,65,12,.3)}
.rank-other{background:rgba(15,23,42,.8);color:#1e3a5f;font-size:.75rem}
.pick-emoji{font-size:1.6rem;margin-bottom:10px;display:block}
.pick-player{font-size:1.08rem;font-weight:800;color:#f0f6ff;margin-bottom:3px;letter-spacing:-.3px;padding-right:38px}
.pick-team{font-size:.75rem;color:#374151;margin-bottom:12px;display:flex;align-items:center;gap:6px}
.loc-badge{background:rgba(15,23,42,.8);padding:2px 9px;border-radius:10px;font-size:.7rem;color:#4b5563;border:1px solid rgba(30,58,95,.5)}
.stat-strip{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.stat-tag{padding:3px 10px;border-radius:10px;font-size:.7rem;font-weight:700;letter-spacing:.3px}
.tag-pts{background:rgba(109,40,217,.15);color:#a78bfa;border:1px solid rgba(109,40,217,.25)}
.tag-reb{background:rgba(37,99,235,.15);color:#60a5fa;border:1px solid rgba(37,99,235,.25)}
.tag-ast{background:rgba(5,150,105,.15);color:#34d399;border:1px solid rgba(5,150,105,.25)}
.tag-fg3m{background:rgba(220,38,38,.15);color:#f87171;border:1px solid rgba(220,38,38,.25)}
.pick-pattern{font-size:.9rem;color:#7dd3fc;font-weight:700;margin-bottom:4px;line-height:1.4}
.l10vthr-desc{font-size:.88rem;color:#f59e0b;font-weight:700;margin-bottom:5px;line-height:1.4}
.fd-line-badge{display:inline-block;background:#1a2a0a;border:1px solid #22c55e;color:#22c55e;
  border-radius:6px;padding:3px 10px;font-size:.78rem;font-weight:700;margin-bottom:6px}
.fd-inline{color:#22c55e;font-weight:700}
.l10vthr-inline{color:#f59e0b;font-weight:700}
.pick-matchup{font-size:.72rem;color:#1e3a5f;margin-bottom:16px}
.bar-wrap{background:rgba(15,23,42,.7);border-radius:6px;height:8px;overflow:hidden;margin-bottom:10px;border:1px solid rgba(30,58,95,.3)}
.bar-fill{height:100%;border-radius:5px}
.bar-green{background:linear-gradient(90deg,#15803d,#22c55e)}
.bar-yellow{background:linear-gradient(90deg,#b45309,#f59e0b)}
.bar-orange{background:linear-gradient(90deg,#c2410c,#f97316)}
.stats-row{display:flex;justify-content:space-between;align-items:center}
.games-chip{background:rgba(15,23,42,.7);padding:4px 12px;border-radius:20px;font-size:.75rem;color:#1e3a5f;border:1px solid rgba(30,58,95,.3)}
.pct{font-size:1.2rem;font-weight:900;letter-spacing:-.5px}
.pct-green{color:#22c55e;text-shadow:0 0 14px rgba(34,197,94,.45)}
.pct-yellow{color:#f59e0b;text-shadow:0 0 14px rgba(245,158,11,.45)}
.pct-orange{color:#f97316;text-shadow:0 0 12px rgba(249,115,22,.35)}
/* ─── Total Banner ─── */
.total-banner{
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;
  background:linear-gradient(135deg,rgba(5,46,22,.75),rgba(3,28,14,.9));
  border:1px solid rgba(20,83,45,.5);border-radius:18px;padding:18px 24px;margin:32px 0 20px;
  box-shadow:0 0 40px rgba(34,197,94,.06),inset 0 1px 0 rgba(34,197,94,.05);
}
.tb-left{display:flex;align-items:center;gap:12px}
.tb-ico{font-size:1.5rem}
.tb-title{font-size:.95rem;font-weight:800;color:#4ade80;letter-spacing:-.2px}
.tb-sub{font-size:.72rem;color:#14532d;margin-top:2px;letter-spacing:.8px;text-transform:uppercase}
.tb-count{font-size:2.2rem;font-weight:900;color:#22c55e;text-shadow:0 0 20px rgba(34,197,94,.5);letter-spacing:-1.5px}
/* ─── All Patterns ─── */
.all-section-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:10px}
.all-section-title{font-size:.95rem;font-weight:800;color:#60a5fa;display:flex;align-items:center;gap:8px}
.game-group{margin-bottom:14px}
.game-group-hdr{
  display:flex;align-items:center;justify-content:space-between;
  background:linear-gradient(135deg,rgba(13,20,38,.9),rgba(8,12,24,.95));
  border:1px solid rgba(30,58,95,.65);border-radius:13px;
  padding:12px 18px;margin-bottom:6px;cursor:pointer;user-select:none;
  transition:border-color .2s,box-shadow .2s;
}
.game-group-hdr:hover{border-color:rgba(59,130,246,.45);box-shadow:0 0 15px rgba(59,130,246,.08)}
.gg-label{font-size:.88rem;font-weight:800;color:#93c5fd;display:flex;align-items:center;gap:8px}
.gg-meta{display:flex;align-items:center;gap:8px}
.gg-chevron{color:#1e3a5f;font-size:.85rem;transition:transform .2s}
.compact-picks{display:flex;flex-direction:column;gap:5px;margin-bottom:4px}
.compact-row{
  display:flex;align-items:center;gap:12px;
  background:rgba(8,12,24,.8);border:1px solid rgba(20,30,50,.8);border-radius:11px;padding:10px 15px;
  transition:border-color .2s,background .2s;
}
.compact-row:hover{border-color:rgba(59,130,246,.35);background:rgba(13,20,38,.9)}
.cr-emoji{font-size:1.05rem;flex-shrink:0;width:22px;text-align:center}
.cr-info{flex:1;min-width:0}
.cr-player{font-size:.86rem;font-weight:700;color:#94a3b8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cr-pattern{font-size:.76rem;color:#3b82f6;font-weight:600;margin-top:2px}
.cr-right{display:flex;flex-direction:column;align-items:flex-end;gap:3px;flex-shrink:0}
.cr-bar-wrap{background:rgba(15,23,42,.8);border-radius:4px;height:4px;width:68px;overflow:hidden}
.cr-bar-fill{height:100%;border-radius:4px}
.cr-pct{font-size:.9rem;font-weight:900}
.cr-sample{font-size:.65rem;color:#1e3a5f}
/* ─── Messages ─── */
.msg-card{
  background:linear-gradient(145deg,rgba(13,20,38,.95),rgba(8,12,24,.99));
  border:1px solid rgba(30,58,95,.65);border-radius:22px;padding:60px 30px;text-align:center;
  box-shadow:0 20px 70px rgba(0,0,0,.5);
}
.msg-card .ico{font-size:3.8rem;margin-bottom:16px;display:block}
.msg-card h2{color:#e0e6f0;font-size:1.2rem;font-weight:800;margin-bottom:10px}
.msg-card p{color:#374151;font-size:.88rem;line-height:1.75}
/* ─── Log ─── */
.log-box{background:rgba(3,6,14,.8);border:1px solid rgba(20,30,50,.8);border-radius:12px;padding:16px;font-size:.74rem;color:#1e3a5f;font-family:'Courier New',monospace;margin-top:20px;max-height:160px;overflow-y:auto;line-height:1.9;scrollbar-width:thin;scrollbar-color:#0f1d2e transparent}
footer{text-align:center;margin-top:32px;color:#0a1525;font-size:.68rem;padding:10px;letter-spacing:1.5px;text-transform:uppercase}
</style>
</head>
<body>

<header>
  <!-- Animated basketball SVG -->
  <div class="brand">
    <svg class="ball-svg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
      <defs>
        <radialGradient id="ballG" cx="38%" cy="35%" r="65%">
          <stop offset="0%" stop-color="#fb923c"/>
          <stop offset="55%" stop-color="#ea580c"/>
          <stop offset="100%" stop-color="#7c2d12"/>
        </radialGradient>
        <clipPath id="ballC"><circle cx="50" cy="50" r="46"/></clipPath>
      </defs>
      <circle cx="50" cy="50" r="47" fill="url(#ballG)" stroke="#7c2d12" stroke-width="1.5"/>
      <g clip-path="url(#ballC)" fill="none" stroke="rgba(124,45,18,.88)" stroke-width="2.8" stroke-linecap="round">
        <path d="M50 4 C63 20 67 35 67 50 C67 65 63 80 50 96"/>
        <path d="M50 4 C37 20 33 35 33 50 C33 65 37 80 50 96"/>
        <path d="M4 50 C18 37 34 33 50 33 C66 33 82 37 96 50"/>
        <path d="M4 50 C18 63 34 67 50 67 C66 67 82 63 96 50"/>
      </g>
      <ellipse cx="37" cy="37" rx="9" ry="5" fill="rgba(255,255,255,.13)" transform="rotate(-35 37 37)"/>
      <circle cx="50" cy="50" r="46" fill="none" stroke="rgba(124,45,18,.5)" stroke-width="1.5"/>
    </svg>
    <div class="brand-text">
      <h1>NBA Money Buckets</h1>
      <div class="sub">Pattern Picks · Pts · Reb · Ast · 3PM</div>
    </div>
  </div>
  <div class="actions">
    <input type="date" id="datePicker" value="__TODAY__" style="background:rgba(30,58,95,.5);color:#60a5fa;border:1px solid rgba(59,130,246,.3);border-radius:10px;padding:7px 12px;font-size:.82rem;font-weight:600;outline:none;cursor:pointer;">
    <button class="btn btn-run" onclick="runPicks()">⚡ Run Picks</button>
    <button class="btn btn-refresh" onclick="clearAndRun()" title="Clear cache and re-fetch with FanDuel lines">🔄 Refresh</button>
    <a href="/logout" class="btn btn-out">Sign Out</a>
    <div class="fd-indicator" id="fdIndicator" title="FanDuel Status">
      <span class="fd-dot" id="fdDot"></span>
      <span class="fd-label" id="fdLabel">FanDuel</span>
    </div>
  </div>
</header>

<div class="games-bar" id="gamesBar">
  <div class="game-chip" style="color:#0f1d2e">Hit Run Picks to load today's games →</div>
</div>

<div id="filterBar" style="display:none" class="filter-bar">
  <button class="filter-btn active" onclick="filterStat('ALL')">All Stats</button>
  <button class="filter-btn" onclick="filterStat('PTS')">🏀 Points</button>
  <button class="filter-btn" onclick="filterStat('REB')">📊 Rebounds</button>
  <button class="filter-btn" onclick="filterStat('AST')">🎯 Assists</button>
  <button class="filter-btn" onclick="filterStat('FG3M')">🔥 3-Pointers</button>
</div>

<div id="content">
  <div class="msg-card">
    <span class="ico">🏀</span>
    <h2>Welcome to NBA Money Buckets</h2>
    <p>Hit <strong style="color:#f59e0b">Run Picks</strong> to scan today's matchups.<br>
    Finds players hitting <strong style="color:#22c55e">75%+</strong> in Pts, Reb, Ast, or 3PM<br>
    against today's specific opponent — home or away.</p>
  </div>
</div>

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

<footer>NBA Money Buckets · No Lines · Just Patterns · Powered by NBA Stats API &amp; ESPN</footer>

<script>
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
    document.getElementById('content').innerHTML='<div class="msg-card"><span class="ico">🔍</span><h2>No patterns</h2><p>Try "All Stats".</p></div>';
    return;
  }
  let html=`<div class="section-hdr"><div class="section-title">🏆 Top 10 Picks Today</div><span class="count-pill">${picks.length} pick${picks.length!==1?'s':''}</span></div><div class="picks-grid">`;
  picks.forEach((p,i)=>{
    const [pc,bc]=pctClass(p.pct);
    html+=`
    <div class="pick-card">
      <div class="pick-rank ${rankClass(i)}">${i+1}</div>
      <span class="pick-emoji">${p.emoji}</span>
      <div class="pick-player">${p.player}</div>
      <div class="pick-team">${p.team_name} <span class="loc-badge">${p.location==='Home'?'🏠 Home':'✈️ Away'}</span></div>
      <div class="stat-strip">${statTag(p.stat)}</div>
      <div class="pick-pattern">${p.threshold}+ ${p.stat_label} in ${p.hits} of ${p.games} ${p.location.toLowerCase()} games vs ${p.opp}</div>
      ${p.l10_games > 0 ? `<div class="l10vthr-desc">${p.player.split(" ").pop()} hit ${p.threshold}+ ${p.stat_label} ${p.l10_hits} of ${p.l10_games} last 10 games vs ${p.opp}</div>` : ""}
      ${p.fd_line ? `<div class="fd-line-badge">Sportsbook Line: <strong>${p.fd_line}</strong> ${p.fd_odds ? "(" + p.fd_odds + ")" : ""}${p.l10_sb_hits !== null && p.l10_sb_hits !== undefined ? " | Last 10 vs " + p.opp + ": " + p.l10_sb_hits + "/" + p.l10_games : ""}</div>` : ""}
      <div class="pick-matchup">📍 Today: ${p.matchup}</div>
      <div class="bar-wrap"><div class="bar-fill ${bc}" style="width:${Math.min(p.pct,100)}%"></div></div>
      <div class="stats-row"><span class="games-chip">${p.hits}/${p.games} games</span><span class="pct ${pc}">${p.pct}%</span></div>
    </div>`;
  });
  html+='</div>';
  document.getElementById('content').innerHTML=html;
}

function renderAllByGame(picks){
  const el=document.getElementById('allPicksSection');
  if(!picks.length){el.innerHTML='<div class="msg-card" style="padding:30px"><span class="ico">🔍</span><p>No patterns for this filter.</p></div>';return;}
  const groups={},order=[];
  for(const p of picks){if(!groups[p.matchup]){groups[p.matchup]=[];order.push(p.matchup);}groups[p.matchup].push(p);}
  let html='';
  for(const matchup of order){
    const gp=groups[matchup];
    const gameId='g_'+matchup.replace(/[^a-z0-9]/gi,'_');
    html+=`<div class="game-group">
      <div class="game-group-hdr" onclick="toggleGroup('${gameId}',this)">
        <span class="gg-label">🏀 ${matchup}</span>
        <div class="gg-meta"><span class="count-pill">${gp.length} pattern${gp.length!==1?'s':''}</span><span class="gg-chevron">▾</span></div>
      </div>
      <div class="compact-picks" id="${gameId}">`;
    for(const p of gp){
      const [pc,bc]=pctClass(p.pct);
      html+=`<div class="compact-row">
        <span class="cr-emoji">${p.emoji}</span>
        <div class="cr-info">
          <div class="cr-player">${p.player} <span style="color:#1e3a5f;font-size:.65rem">${p.team}·${p.location==='Home'?'🏠':'✈️'}</span></div>
          <div class="cr-pattern">${p.threshold}+ ${p.stat_label} · ${p.hits}/${p.games} ${p.location.toLowerCase()} vs ${p.opp}${p.fd_line ? ` · <span class="fd-inline">🏙️ ${p.fd_line}</span>` : ''}</div>
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
  document.getElementById('gamesBar').innerHTML=games.map(g=>
    `<div class="game-chip"><b>${g.away}</b><span class="sep">@</span><b>${g.home}</b></div>`
  ).join('');
}
// FanDuel status indicator
async function checkFD(){
  const dot   = document.getElementById('fdDot');
  const label = document.getElementById('fdLabel');
  if(!dot) return;
  dot.className = 'fd-dot checking';
  label.textContent = 'FanDuel...';
  try{
    const r = await fetch('/fd-status');
    const d = await r.json();
    if(d.fanduel === 'connected'){
      dot.className = 'fd-dot connected';
      label.style.color = '#22c55e';
      label.textContent = 'FanDuel ✓';
    } else if(d.fanduel === 'disconnected'){
      dot.className = 'fd-dot disconnected';
      label.style.color = '#ef4444';
      label.textContent = 'FanDuel ✗';
    } else {
      dot.className = 'fd-dot';
      label.style.color = '#475569';
      label.textContent = 'FanDuel';
    }
  } catch(e){
    dot.className = 'fd-dot';
    label.textContent = 'FanDuel';
  }
}
document.addEventListener('DOMContentLoaded', checkFD);

async function clearAndRun(){
  const btn = document.querySelector('.btn-refresh');
  if(btn){ btn.textContent = '⏳ Clearing...'; btn.disabled = true; }
  await fetch('/clear-cache');
  await checkFD();
  if(btn){ btn.textContent = '🔄 Refresh'; btn.disabled = false; }
  // Just clears cache — user hits Run Picks when ready
}

async function runPicks(){
  const selectedDate=document.getElementById('datePicker').value;
  document.getElementById('content').innerHTML=`
    <div class="msg-card">
      <div class="loading-ball"></div>
      <div class="ball-shadow"></div>
      <h2 style="color:#f59e0b">Analyzing Matchup Patterns</h2>
      <p>Pulling data for <strong style="color:#60a5fa">${selectedDate}</strong> from NBA Stats API.<br>
      <span style="color:#1e3a5f">This takes ~45 seconds — worth the wait.</span></p>
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
      document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico">🔍</span><h2>No Qualifying Patterns</h2><p>No 75%+ patterns for today's matchups.</p></div><div class="log-box">${log.join('<br>')}</div>`;
      return;
    }
    document.getElementById('filterBar').style.display='flex';
    renderTop10Cards(top10);
    const lb=document.createElement('div');
    lb.className='log-box';
    lb.innerHTML=log.join('<br>')+`<br>📋 ${data.total} total patterns found`;
    document.getElementById('content').appendChild(lb);
    document.getElementById('totalCount').textContent=allPicksData.length;
    document.getElementById('allPicksWrap').style.display='block';
    renderAllByGame(allPicksData);
  }catch(e){
    document.getElementById('content').innerHTML=`<div class="msg-card"><span class="ico">❌</span><h2 style="color:#ef4444">Something went wrong</h2><p>${e.message}</p></div>`;
  }
}
</script>
</body>
</html>"""
# ─── Routes ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Auth removed — no redirect needed
    today_iso = date.today().isoformat()
    return HTMLResponse(MAIN_HTML.replace("__TODAY__", today_iso))

@app.get("/login", response_class=HTMLResponse)
async def login_get():
    return HTMLResponse(LOGIN_HTML.replace('{error}', ''))

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

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login")
    resp.delete_cookie("session")
    return resp

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
    global _cache, _fd_cookie
    _cache = {}
    _fd_cookie = None  # force fresh FD login too
    return {"status": "cleared"}

@app.get("/fd-status")
async def fd_status(request: Request):
    user = get_user(request)
    if not user:
        return {"fanduel": "unauthorized"}
    configured = bool(os.environ.get("FD_EMAIL"))
    connected  = _fd_cookie is not None
    if configured and not connected:
        try:
            await get_fd_cookie()
            connected = _fd_cookie is not None
        except Exception:
            connected = False
    return {
        "fanduel":    "connected" if connected else ("disconnected" if configured else "not_configured"),
        "configured": configured,
    }

@app.get("/health")
async def health():
    return {"status": "ok", "date": date.today().isoformat()}
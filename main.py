# NBA Money Buckets — main.py
# Pattern-based NBA picks: finds players hitting 75%+ in key stats vs today's specific opponent

import asyncio
import json
import os
import hashlib
from datetime import date
from typing import Dict, List, Optional, Any

import httpx
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
    token = request.cookies.get("session")
    for u in USERS:
        if token == make_token(u):
            return u
    return None

# ─── NBA Stats API Headers ────────────────────────────────────────────────────
NBA_HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}

# ─── Team Maps ────────────────────────────────────────────────────────────────
TEAM_ID_MAP: Dict[str, int] = {
    'ATL': 1610612737, 'BOS': 1610612738, 'BKN': 1610612751,
    'CHA': 1610612766, 'CHI': 1610612741, 'CLE': 1610612739,
    'DAL': 1610612742, 'DEN': 1610612743, 'DET': 1610612765,
    'GSW': 1610612744, 'HOU': 1610612745, 'IND': 1610612754,
    'LAC': 1610612746, 'LAL': 1610612747, 'MEM': 1610612763,
    'MIA': 1610612748, 'MIL': 1610612749, 'MIN': 1610612750,
    'NOP': 1610612740, 'NYK': 1610612752, 'OKC': 1610612760,
    'ORL': 1610612753, 'PHI': 1610612755, 'PHX': 1610612756,
    'POR': 1610612757, 'SAC': 1610612758, 'SAS': 1610612759,
    'TOR': 1610612761, 'UTA': 1610612762, 'WAS': 1610612764,
}

TEAM_NAMES: Dict[str, str] = {
    'ATL': 'Atlanta Hawks',       'BOS': 'Boston Celtics',        'BKN': 'Brooklyn Nets',
    'CHA': 'Charlotte Hornets',   'CHI': 'Chicago Bulls',         'CLE': 'Cleveland Cavaliers',
    'DAL': 'Dallas Mavericks',    'DEN': 'Denver Nuggets',        'DET': 'Detroit Pistons',
    'GSW': 'Golden State Warriors','HOU': 'Houston Rockets',      'IND': 'Indiana Pacers',
    'LAC': 'LA Clippers',         'LAL': 'LA Lakers',             'MEM': 'Memphis Grizzlies',
    'MIA': 'Miami Heat',          'MIL': 'Milwaukee Bucks',       'MIN': 'Minnesota Timberwolves',
    'NOP': 'New Orleans Pelicans','NYK': 'New York Knicks',       'OKC': 'OKC Thunder',
    'ORL': 'Orlando Magic',       'PHI': 'Philadelphia 76ers',    'PHX': 'Phoenix Suns',
    'POR': 'Portland Trail Blazers','SAC': 'Sacramento Kings',    'SAS': 'San Antonio Spurs',
    'TOR': 'Toronto Raptors',     'UTA': 'Utah Jazz',             'WAS': 'Washington Wizards',
}

ESPN_TO_NBA: Dict[str, str] = {
    'GS': 'GSW', 'NO': 'NOP', 'NY': 'NYK', 'PHO': 'PHX',
    'SA': 'SAS', 'UTAH': 'UTA', 'WSH': 'WAS', 'CHP': 'CHA', 'NJ': 'BKN',
}

def norm(abbr: str) -> str:
    a = abbr.upper().strip()
    return ESPN_TO_NBA.get(a, a)

# ─── Stat Config ──────────────────────────────────────────────────────────────
STAT_CONFIG = {
    'PTS':  {'label': 'Points',     'emoji': '🏀', 'col': 'PTS',  'thresholds': list(range(45, 4, -1))},
    'REB':  {'label': 'Rebounds',   'emoji': '📊', 'col': 'REB',  'thresholds': list(range(20, 1, -1))},
    'AST':  {'label': 'Assists',    'emoji': '🎯', 'col': 'AST',  'thresholds': list(range(15, 1, -1))},
    'FG3M': {'label': '3-Pointers', 'emoji': '🔥', 'col': 'FG3M', 'thresholds': list(range(8,  0, -1))},
}

HIT_RATE_MIN = 0.75
MIN_GAMES    = 3
MIN_MINUTES  = 10.0
SEASONS      = ['2025-26', '2024-25', '2023-24', '2022-23']
TOP_N        = 10

# ─── Cache ───────────────────────────────────────────────────────────────────
_cache: Dict[str, Any] = {}

# ─── Helpers ──────────────────────────────────────────────────────────────────
def parse_min(val) -> float:
    if val is None: return 0.0
    if isinstance(val, (int, float)): return float(val)
    if isinstance(val, str) and ':' in val:
        parts = val.split(':')
        try: return float(parts[0]) + float(parts[1]) / 60
        except ValueError: return 0.0
    try: return float(val)
    except: return 0.0

def find_best_threshold(values: List[float], thresholds: List[int]) -> Optional[Dict]:
    n = len(values)
    if n < MIN_GAMES: return None
    for t in thresholds:
        hits = sum(1 for v in values if v >= t)
        rate = hits / n
        if rate >= HIT_RATE_MIN:
            return {'threshold': t, 'hits': hits, 'games': n,
                    'hit_rate': rate, 'pct': round(rate * 100, 1)}
    return None

# ─── API Fetch ────────────────────────────────────────────────────────────────
async def get_today_games() -> List[Dict]:
    today = date.today().strftime('%Y%m%d')
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={today}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        data = r.json()
    games = []
    for event in data.get('events', []):
        comps = event['competitions'][0]['competitors']
        home = next((c for c in comps if c['homeAway'] == 'home'), None)
        away = next((c for c in comps if c['homeAway'] == 'away'), None)
        if not home or not away: continue
        h = norm(home['team']['abbreviation'])
        a = norm(away['team']['abbreviation'])
        if h not in TEAM_ID_MAP or a not in TEAM_ID_MAP: continue
        games.append({
            'home': h, 'away': a,
            'home_id': TEAM_ID_MAP[h], 'away_id': TEAM_ID_MAP[a],
            'home_name': TEAM_NAMES.get(h, h), 'away_name': TEAM_NAMES.get(a, a),
        })
    return games

async def get_team_roster(team_id: int) -> List[Dict]:
    url = "https://stats.nba.com/stats/commonteamroster"
    try:
        async with httpx.AsyncClient(timeout=30, headers=NBA_HEADERS) as client:
            await asyncio.sleep(0.3)
            r = await client.get(url, params={'TeamID': team_id, 'Season': '2025-26'})
            data = r.json()
        hdrs = data['resultSets'][0]['headers']
        rows = data['resultSets'][0]['rowSet']
        return [{'id': dict(zip(hdrs, row))['PLAYER_ID'],
                 'name': dict(zip(hdrs, row))['PLAYER']} for row in rows]
    except Exception as e:
        print(f"  Roster error team {team_id}: {e}")
        return []

async def fetch_season_logs(season: str, season_type: str, sem: asyncio.Semaphore) -> List[Dict]:
    url = "https://stats.nba.com/stats/playergamelogs"
    async with sem:
        try:
            async with httpx.AsyncClient(timeout=90, headers=NBA_HEADERS) as client:
                await asyncio.sleep(1.0)
                r = await client.get(url, params={'Season': season, 'SeasonType': season_type})
                data = r.json()
            hdrs = data['resultSets'][0]['headers']
            rows = data['resultSets'][0]['rowSet']
            logs = [dict(zip(hdrs, row)) for row in rows]
            print(f"  OK {season} {season_type}: {len(logs):,} entries")
            return logs
        except Exception as e:
            print(f"  ERR {season} {season_type}: {e}")
            return []

# ─── Analysis ─────────────────────────────────────────────────────────────────
async def run_analysis() -> Dict:
    today_str = date.today().isoformat()
    if _cache.get('date') == today_str and _cache.get('picks') is not None:
        return _cache

    log = []
    log.append("📅 Fetching today's NBA schedule via ESPN...")
    try:
        games = await get_today_games()
    except Exception as e:
        return {'date': today_str, 'picks': [], 'games': [], 'log': [f'ESPN error: {e}'], 'total': 0}

    if not games:
        return {'date': today_str, 'picks': [], 'games': [], 'log': ['No NBA games today.'], 'total': 0}

    log.append(f"🏀 {len(games)} game(s): " + " | ".join(f"{g['away']} @ {g['home']}" for g in games))

    team_ids = list({g['home_id'] for g in games} | {g['away_id'] for g in games})
    log.append(f"👥 Loading rosters for {len(team_ids)} teams...")
    roster_results = await asyncio.gather(*[get_team_roster(tid) for tid in team_ids], return_exceptions=True)
    rosters: Dict[int, List[Dict]] = {}
    for tid, res in zip(team_ids, roster_results):
        rosters[tid] = res if isinstance(res, list) else []
    log.append(f"   → {sum(len(v) for v in rosters.values())} players loaded")

    log.append(f"📊 Loading {len(SEASONS)} seasons of game logs (reg season + playoffs)...")
    sem = asyncio.Semaphore(2)
    log_tasks = [(s, t) for s in SEASONS for t in ['Regular Season', 'Playoffs']]
    log_results = await asyncio.gather(
        *[fetch_season_logs(s, t, sem) for s, t in log_tasks], return_exceptions=True)

    all_logs: List[Dict] = []
    for res in log_results:
        if isinstance(res, list):
            all_logs.extend(res)
    log.append(f"📈 {len(all_logs):,} historical game entries loaded")

    logs_by_player: Dict[int, List[Dict]] = {}
    for gl in all_logs:
        pid = gl.get('PLAYER_ID')
        if pid is not None:
            logs_by_player.setdefault(pid, []).append(gl)

    log.append("🔍 Scanning matchup patterns (75%+ threshold)...")
    picks = []

    for game in games:
        h, a = game['home'], game['away']
        h_name, a_name = game['home_name'], game['away_name']

        # Home players vs today's away opponent
        for player in rosters.get(game['home_id'], []):
            pid, pname = player['id'], player['name']
            p_logs = logs_by_player.get(pid, [])
            opp_logs = [l for l in p_logs
                        if 'vs.' in l.get('MATCHUP', '') and a in l.get('MATCHUP', '')
                        and parse_min(l.get('MIN')) >= MIN_MINUTES]
            for sk, sc in STAT_CONFIG.items():
                vals = [float(l[sc['col']]) for l in opp_logs if l.get(sc['col']) is not None]
                result = find_best_threshold(vals, sc['thresholds'])
                if result:
                    picks.append({**result, 'player': pname, 'team': h, 'team_name': h_name,
                                  'stat': sk, 'stat_label': sc['label'], 'emoji': sc['emoji'],
                                  'location': 'Home', 'opp': a, 'opp_name': a_name,
                                  'matchup': f"{a_name} @ {h_name}"})

        # Away players vs today's home opponent
        for player in rosters.get(game['away_id'], []):
            pid, pname = player['id'], player['name']
            p_logs = logs_by_player.get(pid, [])
            opp_logs = [l for l in p_logs
                        if '@ ' in l.get('MATCHUP', '') and h in l.get('MATCHUP', '')
                        and parse_min(l.get('MIN')) >= MIN_MINUTES]
            for sk, sc in STAT_CONFIG.items():
                vals = [float(l[sc['col']]) for l in opp_logs if l.get(sc['col']) is not None]
                result = find_best_threshold(vals, sc['thresholds'])
                if result:
                    picks.append({**result, 'player': pname, 'team': a, 'team_name': a_name,
                                  'stat': sk, 'stat_label': sc['label'], 'emoji': sc['emoji'],
                                  'location': 'Away', 'opp': h, 'opp_name': h_name,
                                  'matchup': f"{a_name} @ {h_name}"})

    picks.sort(key=lambda x: (x['hit_rate'], x['threshold']), reverse=True)
    top_picks = picks[:TOP_N]
    log.append(f"✅ {len(picks)} qualifying patterns found → top {TOP_N} shown")

    result = {'date': today_str, 'picks': top_picks, 'all_picks': picks,
              'games': games, 'log': log, 'total': len(picks)}
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
    <span class="date-badge">📅 __DATE__</span>
    <button class="btn btn-run" onclick="runPicks()">⚡ Run Picks</button>
    <a href="/logout" class="btn btn-out">Sign Out</a>
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
          <div class="cr-pattern">${p.threshold}+ ${p.stat_label} · ${p.hits}/${p.games} ${p.location.toLowerCase()} vs ${p.opp}</div>
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
async function runPicks(){
  document.getElementById('content').innerHTML=`
    <div class="msg-card">
      <div class="loading-ball"></div>
      <div class="ball-shadow"></div>
      <h2 style="color:#f59e0b">Analyzing Matchup Patterns</h2>
      <p>Pulling 4 seasons of data from NBA Stats API.<br>
      <span style="color:#1e3a5f">This takes ~45 seconds — worth the wait.</span></p>
    </div>`;
  document.getElementById('allPicksWrap').style.display='none';
  try{
    const r=await fetch('/run',{method:'POST'});
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
    if not get_user(request):
        return RedirectResponse("/login")
    today = date.today().strftime("%B %d, %Y")
    return HTMLResponse(MAIN_HTML.replace("__DATE__", today))

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
    result = await run_analysis()
    return result

@app.get("/health")
async def health():
    return {"status": "ok", "date": date.today().isoformat()}

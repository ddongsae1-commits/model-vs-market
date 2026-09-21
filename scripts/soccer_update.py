

#!/usr/bin/env python3
"""
soccer_update.py -- 4대 유럽 축구 리그(EPL/라리가/분데스리가/세리에A) 승무패(1X2) 일일 갱신기.

검증 결과 (2026-08-29, 홀드아웃 2023-24~2025-26 시즌, n=4,310경기, 다이슨-콜스 포아송 모델):
  1X2: '무조건 홈승' 대비 전 리그 +7~12%p, 캘리브레이션 오차 ~2.35%p ECE  -> 통과, 아래에서 냄
  오버/언더 2.5골: 4개리그 중 2곳(EPL, 분데스리가)에서 과거빈도 기준선 대비 개선 거의 0
                   -> 홀드아웃 규칙상 기각. 이 스크립트는 오버언더를 만들지 않음.
                   (참고용으로 시장 배당 라인만 있으면 그대로 노출 가능하지만 지금은 안 함)

사용법:
  python3 soccer_update.py <ODDS_API_KEY> [out.json]
  표준 라이브러리 + numpy/scipy/pandas 필요 (없으면 자동 설치 시도).
  실패해도 크래시하지 말고 가능한 리그만 채우고 나머지는 에러를 JSON에 남길 것.
"""
import sys, os, json, subprocess, math
from datetime import datetime, timedelta, timezone

def ensure_pkgs():
    try:
        import numpy, scipy, pandas  # noqa
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "--break-system-packages", "-q",
                         "numpy", "scipy", "pandas"], check=False)

ensure_pkgs()
import urllib.request
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

XI = 0.0018  # 검증에 쓴 시간감쇠율 그대로 (반감기 ~385일)
SEASONS = ["1819","1920","2021","2122","2223","2324","2425","2526","2627"]
CODES = {
    "E0": {"name": "EPL", "espn": "eng.1", "odds": "soccer_epl"},
    "SP1": {"name": "LaLiga", "espn": "esp.1", "odds": "soccer_spain_la_liga"},
    "D1": {"name": "Bundesliga", "espn": "ger.1", "odds": "soccer_germany_bundesliga"},
    "I1": {"name": "SerieA", "espn": "ita.1", "odds": "soccer_italy_serie_a"},
    # 리그앙: 2026-09-07 홀드아웃 검증 통과 (n=918, '무조건 홈승' 대비 +8.17%p,
    # 95% CI +4.68~+11.66%p, ECE 3.0%p) -- 다른 4개 리그와 같은 절차·같은 기준.
    "F1": {"name": "Ligue1", "espn": "fra.1", "odds": "soccer_france_ligue_one"},
}

# football-data.co.uk 가 죽었을 때 쓰는 미러 (같은 팀명 규칙, 같은 컬럼).
# 커버리지: 1819~2526. 현재 시즌은 없으므로 아래 ESPN 백필로 메웁니다.
MIRROR = {"E0": "premier-league", "SP1": "la-liga", "D1": "bundesliga",
          "I1": "serie-a", "F1": "ligue-1"}

ALIAS = {
  "E0": {"AFC Bournemouth":"Bournemouth","Arsenal":"Arsenal","Aston Villa":"Aston Villa",
    "Brentford":"Brentford","Brighton & Hove Albion":"Brighton","Chelsea":"Chelsea",
    "Coventry City":None,"Crystal Palace":"Crystal Palace","Everton":"Everton",
    "Fulham":"Fulham","Hull City":None,"Ipswich Town":"Ipswich","Leeds United":"Leeds",
    "Liverpool":"Liverpool","Manchester City":"Man City","Manchester United":"Man United",
    "Newcastle United":"Newcastle","Nottingham Forest":"Nott'm Forest","Sunderland":"Sunderland",
    "Tottenham Hotspur":"Tottenham"},
  "SP1": {"Alavés":"Alaves","Athletic Club":"Ath Bilbao","Atlético Madrid":"Ath Madrid",
    "Barcelona":"Barcelona","Celta Vigo":"Celta","Deportivo":None,"Elche":"Elche",
    "Espanyol":"Espanol","Getafe":"Getafe","Levante":"Levante","Málaga":None,
    "Osasuna":"Osasuna","Racing Santander":None,"Rayo Vallecano":"Vallecano",
    "Real Betis":"Betis","Real Madrid":"Real Madrid","Real Sociedad":"Sociedad",
    "Sevilla":"Sevilla","Valencia":"Valencia","Villarreal":"Villarreal"},
  "D1": {"1. FC Union Berlin":"Union Berlin","Bayer Leverkusen":"Leverkusen",
    "Bayern Munich":"Bayern Munich","Borussia Dortmund":"Dortmund",
    "Borussia Mönchengladbach":"M'gladbach","Eintracht Frankfurt":"Ein Frankfurt",
    "FC Augsburg":"Augsburg","FC Cologne":"FC Koln","Hamburg SV":"Hamburg","Mainz":"Mainz",
    "RB Leipzig":"RB Leipzig","SC Freiburg":"Freiburg","SC Paderborn 07":"Paderborn",
    "SV ELVERSBERG":None,"Schalke 04":"Schalke 04","TSG Hoffenheim":"Hoffenheim",
    "VfB Stuttgart":"Stuttgart","Werder Bremen":"Werder Bremen"},
  "F1": {"AJ Auxerre":"Auxerre","AS Monaco":"Monaco","Angers":"Angers","Brest":"Brest",
    "Le Havre AC":"Le Havre","Le Mans":None,"Lens":"Lens","Lille":"Lille","Lorient":"Lorient",
    "Lyon":"Lyon","Marseille":"Marseille","Metz":"Metz","Nantes":"Nantes","Nice":"Nice",
    "Paris FC":"Paris FC","Paris Saint-Germain":"Paris SG","Stade Rennais":"Rennes",
    "Strasbourg":"Strasbourg","Toulouse":"Toulouse","Troyes":"Troyes"},
  "I1": {"AC Milan":"Milan","AS Roma":"Roma","Atalanta":"Atalanta","Bologna":"Bologna",
    "Cagliari":"Cagliari","Como":"Como","Fiorentina":"Fiorentina","Frosinone":"Frosinone",
    "Genoa":"Genoa","Internazionale":"Inter","Juventus":"Juventus","Lazio":"Lazio",
    "Lecce":"Lecce","Monza":"Monza","Napoli":"Napoli","Parma":"Parma","Sassuolo":"Sassuolo",
    "Torino":"Torino","Udinese":"Udinese","Venezia":"Venezia"},
}

def fetch_json(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------------------
# ESPN 날짜 조회 (2026-09-21 수정)
# ESPN 이 dates=YYYYMMDD-YYYYMMDD 범위 조회를 400 으로 막았습니다. 그 바람에
# 일정·결과 수집이 2026-09-15 부터 6일간 조용히 멈춰 있었습니다.
# 월 단위(dates=YYYYMM)는 멀쩡하므로 그걸로 바꿉니다. 하루씩 부르는 것보다
# 호출 수가 30분의 1 입니다.
# ---------------------------------------------------------------------------
def _months_between(d0, d1):
    out, cur = [], d0.replace(day=1)
    while cur <= d1:
        out.append(cur.strftime("%Y%m"))
        cur = (cur.replace(year=cur.year + 1, month=1) if cur.month == 12
               else cur.replace(month=cur.month + 1))
    return out


def espn_events(espn_code, d0, d1, timeout=25):
    """d0~d1(날짜) 사이에 걸친 달들을 받아 이벤트를 합칩니다. 실패한 달은 건너뜁니다."""
    seen, out = set(), []
    for ym in _months_between(d0, d1):
        url = (f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_code}"
               f"/scoreboard?dates={ym}")
        try:
            data = fetch_json(url, timeout=timeout)
        except Exception:
            continue
        for e in data.get("events", []):
            if e.get("id") in seen:
                continue
            seen.add(e.get("id"))
            out.append(e)
    return out


NEED_COLS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]
SOURCE_LOG = []   # 어느 시즌을 어디서 받았는지 -- 보고에 씁니다.
ODDS_SRC = {}     # 리그별 배당을 어디서 받았는지 -- 보고·페이지에 씁니다.
NEXT_DATE = {}    # 예정 경기가 없을 때 다음 경기일 -- 휴식기 표시용.


def _read_csv(url, timeout=15):
    import io
    with urllib.request.urlopen(url, timeout=timeout) as r:
        if r.status != 200:
            return None
        data = r.read()
    df = pd.read_csv(io.BytesIO(data))
    if not all(c in df.columns for c in NEED_COLS):
        return None
    return df[NEED_COLS].copy()


def fetch_csv_df(code, season):
    """원본(football-data.co.uk) 먼저, 죽었으면 GitHub 미러로 넘어갑니다."""
    try:
        df = _read_csv(f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv")
        if df is not None and len(df):
            SOURCE_LOG.append((code, season, "football-data"))
            return df
    except Exception:
        pass
    slug = MIRROR.get(code)
    if slug:
        try:
            df = _read_csv("https://raw.githubusercontent.com/datasets/football-datasets"
                           f"/main/datasets/{slug}/season-{season}.csv", timeout=25)
            if df is not None and len(df):
                SOURCE_LOG.append((code, season, "mirror"))
                return df
        except Exception:
            pass
    return None


def espn_results_since(espn_code, code, since):
    """ESPN 공개 스코어보드에서 since 이후 끝난 경기를 football-data 팀명으로 가져옵니다.
    미러에 아직 없는 현재 시즌을 메우는 용도. 매핑 안 되는 팀(승격팀 등)은 버립니다."""
    alias = ALIAS.get(code, {})
    rows, unmapped = [], set()
    end = datetime.now(timezone.utc).date()
    for e in espn_events(espn_code, since.replace(day=1), end):
        st = e.get("status", {}).get("type", {})
        if not st.get("completed"):
            continue
        comp = e["competitions"][0]
        h = a = hs = as_ = None
        for c in comp["competitors"]:
            if c["homeAway"] == "home":
                h, hs = c["team"]["displayName"], c.get("score")
            else:
                a, as_ = c["team"]["displayName"], c.get("score")
        hfd, afd = alias.get(h), alias.get(a)
        if h not in alias:
            unmapped.add(h)
        if a not in alias:
            unmapped.add(a)
        if not hfd or not afd or hs is None or as_ is None:
            continue
        d = e.get("date", "")[:10]
        if not d or d <= since.isoformat():
            continue
        try:
            rows.append({"Date": d, "HomeTeam": hfd, "AwayTeam": afd,
                         "FTHG": int(hs), "FTAG": int(as_)})
        except (TypeError, ValueError):
            continue
    return pd.DataFrame(rows, columns=NEED_COLS), sorted(unmapped)

def dc_tau(x, y, lam, mu, rho):
    if x==0 and y==0: return 1 - lam*mu*rho
    if x==0 and y==1: return 1 + lam*rho
    if x==1 and y==0: return 1 + mu*rho
    if x==1 and y==1: return 1 - rho
    return 1.0

def fit_dc(train_df):
    teams = sorted(set(train_df.HomeTeam) | set(train_df.AwayTeam))
    n = len(teams)
    idx = {t:i for i,t in enumerate(teams)}
    hi = train_df.HomeTeam.map(idx).values
    ai = train_df.AwayTeam.map(idx).values
    hg = train_df.FTHG.values.astype(int)
    ag = train_df.FTAG.values.astype(int)
    w = train_df["weight"].values

    def unpack(p):
        return p[:n], p[n:2*n], p[2*n], p[2*n+1]

    def neg_ll(p):
        attack, defence, gamma, rho = unpack(p)
        lam = np.exp(attack[hi] + defence[ai] + gamma)
        mu = np.exp(attack[ai] + defence[hi])
        ll = poisson.logpmf(hg, lam) + poisson.logpmf(ag, mu)
        tau = np.ones_like(lam)
        for k in range(len(lam)):
            if hg[k] <= 1 and ag[k] <= 1:
                tau[k] = dc_tau(hg[k], ag[k], lam[k], mu[k], rho)
        tau = np.clip(tau, 1e-6, None)
        ll = ll + np.log(tau)
        reg = 0.001*np.sum(attack**2) + 0.001*np.sum(defence**2)
        return -(np.sum(w*ll)) + reg

    x0 = np.zeros(2*n+2); x0[2*n] = 0.25
    res = minimize(neg_ll, x0, method="L-BFGS-B", options={"maxiter":300})
    attack, defence, gamma, rho = unpack(res.x)
    return {"teams": idx, "attack": attack, "defence": defence, "gamma": float(gamma), "rho": float(rho)}

def predict_with_fallback(model, home_fd, away_fd):
    idx = model["teams"]; attack, defence = model["attack"], model["defence"]
    order = np.argsort(attack); bottom4 = order[:4]
    fb_a, fb_d = attack[bottom4].mean(), defence[bottom4].mean()
    used_fb = False
    hi = idx.get(home_fd) if home_fd else None
    ai = idx.get(away_fd) if away_fd else None
    if hi is None: used_fb = True
    if ai is None: used_fb = True
    a_h = attack[hi] if hi is not None else fb_a
    d_h = defence[hi] if hi is not None else fb_d
    a_a = attack[ai] if ai is not None else fb_a
    d_a = defence[ai] if ai is not None else fb_d
    gamma, rho = model["gamma"], model["rho"]
    lam = math.exp(a_h + d_a + gamma)
    mu = math.exp(a_a + d_h)
    gs = list(range(11))
    ph = [poisson.pmf(k, lam) for k in gs]
    pa = [poisson.pmf(k, mu) for k in gs]
    M = [[ph[x]*pa[y] for y in gs] for x in gs]
    for x in (0,1):
        for y in (0,1):
            M[x][y] *= dc_tau(x, y, lam, mu, rho)
    tot = sum(sum(row) for row in M)
    M = [[v/tot for v in row] for row in M]
    pH = sum(M[x][y] for x in gs for y in gs if x>y)
    pD = sum(M[x][x] for x in gs)
    pA = sum(M[x][y] for x in gs for y in gs if x<y)

    # --- 화면에 보여줄 속내 (2026-09-21 추가). 확률 계산에는 영향 없습니다.
    # 어차피 위에서 다 만든 값이라 공짜로 꺼내는 것뿐입니다.
    flat = sorted(((M[x][y], x, y) for x in gs for y in gs), reverse=True)[:6]
    top = [{"s": f"{x}-{y}", "p": round(v, 4)} for v, x, y in flat]
    # 공격·수비력을 리그 평균 대비 배수로. 1.30 이면 평균보다 30% 더 넣는다는 뜻입니다.
    am, dm = float(attack.mean()), float(defence.mean())
    rate = lambda v, m: round(math.exp(float(v) - m), 3)
    return {"pH": round(pH, 3), "pD": round(pD, 3), "pA": round(pA, 3), "fallback": used_fb,
            "lam": round(lam, 2), "mu": round(mu, 2),
            "top": top,
            "atkH": rate(a_h, am), "defH": rate(d_h, dm),
            "atkA": rate(a_a, am), "defA": rate(d_a, dm),
            "hfa": round(math.exp(gamma), 3),
            # 언더/오버는 검증 실패라 픽으로 안 내지만, 총 득점 분포는 보여줄 만합니다.
            "pOver25": round(sum(M[x][y] for x in gs for y in gs if x + y > 2.5), 3)}

def _parse_dates(col):
    """미러는 ISO(2024-08-16), 원본은 dayfirst(16/08/2024) -- 둘 다 받습니다."""
    iso = col.astype(str).str.match(r"^\d{4}-\d{2}-\d{2}")
    out = pd.Series(pd.NaT, index=col.index, dtype="datetime64[ns]")
    if iso.any():
        out[iso] = pd.to_datetime(col[iso], format="ISO8601", errors="coerce")
    if (~iso).any():
        out[~iso] = pd.to_datetime(col[~iso], dayfirst=True, errors="coerce")
    return out


def fit_production_model(code, espn_code=None):
    frames = []
    for s in SEASONS:
        df = fetch_csv_df(code, s)
        if df is not None and len(df):
            frames.append(df)
    if not frames:
        raise RuntimeError("과거 결과를 한 시즌도 못 받았습니다 (원본·미러 모두 실패)")
    allf = pd.concat(frames, ignore_index=True)
    allf["Date"] = _parse_dates(allf["Date"])
    allf = allf.dropna(subset=["Date", "FTHG", "FTAG"]).sort_values("Date").reset_index(drop=True)

    # CSV 가 현재 시즌까지 못 따라오면(미러는 보통 한 시즌 늦습니다) ESPN 결과로 메웁니다.
    gap_note = None
    if espn_code:
        last = allf["Date"].max().date()
        if (datetime.now(timezone.utc).date() - last).days > 14:
            extra, unmapped = espn_results_since(espn_code, code, last)
            if len(extra):
                extra["Date"] = _parse_dates(extra["Date"])
                allf = pd.concat([allf, extra], ignore_index=True) \
                         .dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)
            gap_note = {"filledFrom": last.isoformat(), "espnMatches": int(len(extra)),
                        "unmappedTeams": unmapped}

    today = pd.Timestamp.now().normalize()
    allf["weight"] = np.exp(-XI * (today - allf.Date).dt.days.clip(lower=0))
    model = fit_dc(allf)
    model["_n"] = int(len(allf))
    model["_lastDate"] = allf["Date"].max().date().isoformat()
    model["_gap"] = gap_note
    # 상세 화면(최근 폼·맞대결)에 쓸 원본 경기표. 이미 받아 둔 자료라 추가 수집 없습니다.
    model["_df"] = allf[["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]]
    return model

def get_fixtures(espn_code, days_ahead=6):
    today = datetime.now(timezone.utc)
    end = today + timedelta(days=days_ahead)
    lo, hi = today.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    out = []
    for e in espn_events(espn_code, today.date(), end.date()):
        if not (lo <= (e.get("date") or "")[:10] <= hi):
            continue
        comp = e["competitions"][0]
        status = e.get("status",{}).get("type",{}).get("name","")
        home = [c["team"]["displayName"] for c in comp["competitors"] if c["homeAway"]=="home"][0]
        away = [c["team"]["displayName"] for c in comp["competitors"] if c["homeAway"]=="away"][0]
        hs = as_ = None
        for c in comp["competitors"]:
            if c["homeAway"]=="home": hs = c.get("score")
            else: as_ = c.get("score")
        out.append({"date": e.get("date"), "home": home, "away": away, "status": status,
                    "started": status != "STATUS_SCHEDULED", "hScore": hs, "aScore": as_})
    return out

def get_results(espn_code, days_back=10):
    """지난 경기 결과. 픽 정산용."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    lo, hi = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    out = {}
    for e in espn_events(espn_code, start.date(), end.date()):
        if not (lo <= (e.get("date") or "")[:10] <= hi):
            continue
        st = e.get("status", {}).get("type", {})
        if not st.get("completed"):
            continue
        comp = e["competitions"][0]
        h = a = hs = as_ = None
        for c in comp["competitors"]:
            if c["homeAway"] == "home":
                h, hs = c["team"]["displayName"], c.get("score")
            else:
                a, as_ = c["team"]["displayName"], c.get("score")
        try:
            hs, as_ = int(hs), int(as_)
        except (TypeError, ValueError):
            continue
        out[f'{e.get("date","")[:10]}|{a}@{h}'] = (hs, as_)
    return out


def recent_form(df, team_fd, n=5, rev=None):
    """해당 팀의 최근 n경기. 확률 계산에는 안 들어가고 보여주기만 합니다."""
    if df is None or not team_fd:
        return None
    m = df[(df.HomeTeam == team_fd) | (df.AwayTeam == team_fd)].tail(n)
    if not len(m):
        return None
    out, w = [], {"W": 0, "D": 0, "L": 0}
    gf = ga = 0
    for _, r in m.iloc[::-1].iterrows():
        home = r.HomeTeam == team_fd
        f, a = (int(r.FTHG), int(r.FTAG)) if home else (int(r.FTAG), int(r.FTHG))
        res = "W" if f > a else ("D" if f == a else "L")
        w[res] += 1
        gf += f
        ga += a
        opp = r.AwayTeam if home else r.HomeTeam
        out.append({"d": r.Date.strftime("%m-%d"), "opp": (rev or {}).get(opp, opp),
                    "ha": "H" if home else "A", "gf": f, "ga": a, "r": res})
    return {"games": out, "w": w["W"], "d": w["D"], "l": w["L"], "gf": gf, "ga": ga}


def h2h_recent(df, home_fd, away_fd, n=6, rev=None):
    """두 팀 맞대결 최근 n경기."""
    if df is None or not home_fd or not away_fd:
        return None
    m = df[((df.HomeTeam == home_fd) & (df.AwayTeam == away_fd)) |
           ((df.HomeTeam == away_fd) & (df.AwayTeam == home_fd))].tail(n)
    if not len(m):
        return None
    out, hw, dr, aw = [], 0, 0, 0
    for _, r in m.iloc[::-1].iterrows():
        hg, ag = int(r.FTHG), int(r.FTAG)
        winner = r.HomeTeam if hg > ag else (r.AwayTeam if ag > hg else None)
        if winner is None:
            dr += 1
        elif winner == home_fd:
            hw += 1
        else:
            aw += 1
        out.append({"d": r.Date.strftime("%y-%m-%d"),
                    "h": (rev or {}).get(r.HomeTeam, r.HomeTeam),
                    "a": (rev or {}).get(r.AwayTeam, r.AwayTeam),
                    "hg": hg, "ag": ag})
    return {"games": out, "hw": hw, "d": dr, "aw": aw}


def pick_of(g):
    """모델이 고른 쪽. 확률 최대값."""
    return max((("H", g["pH"]), ("D", g["pD"]), ("A", g["pA"])), key=lambda x: x[1])[0]


def seed_from_prev(picks, prev, now_iso):
    """예전 형식(픽 기록이 없던 시절)의 soccerData 에서 경기 전 픽을 한 번 옮겨옵니다.
    이미 기록이 있으면 아무것도 안 합니다."""
    n = 0
    for lgname, games in (prev.get("leagues") or {}).items():
        for g in games or []:
            if g.get("started"):
                continue
            d = (g.get("date") or "")[:10]
            if not d or "pH" not in g:
                continue
            k = f'{lgname}|{d}|{g["away"]}@{g["home"]}'
            if k in picks:
                continue
            o = None
            if g.get("oh") and g.get("od") and g.get("oa"):
                o = {"H": g["oh"], "D": g["od"], "A": g["oa"]}
            picks[k] = {"lg": lgname, "date": d, "home": g["home"], "away": g["away"],
                        "pick": pick_of(g), "pH": g["pH"], "pD": g["pD"], "pA": g["pA"],
                        "fallback": g.get("fallback", False),
                        "ts": prev.get("asOf") or now_iso, "n": 1, "seeded": True,
                        **({"oFirst": o, "oLast": o} if o else {})}
            n += 1
    return n


def track_picks(picks, record, lgname, espn_code, games, now_iso):
    """경기 전 픽과 그 시점 배당을 사전에 남기고, 끝난 경기는 성적표로 옮깁니다.
    나중에 맞춰보는 게 아니라 사전 기록이라는 게 핵심입니다."""
    for g in games:
        d = (g.get("date") or "")[:10]
        if not d:
            continue
        k = f'{lgname}|{d}|{g["away"]}@{g["home"]}'
        pk = pick_of(g)
        o = None
        if g.get("oh") and g.get("od") and g.get("oa"):
            o = {"H": g["oh"], "D": g["od"], "A": g["oa"]}
        e = picks.get(k)
        if e is None:
            picks[k] = {"lg": lgname, "date": d, "home": g["home"], "away": g["away"],
                        "pick": pk, "pH": g["pH"], "pD": g["pD"], "pA": g["pA"],
                        "fallback": g.get("fallback", False), "ts": now_iso, "n": 1,
                        **({"oFirst": o, "oLast": o} if o else {})}
        elif not g.get("started"):
            if e["pick"] != pk:
                # 픽이 바뀌면 기준 시점을 다시 잡습니다.
                e.update({"pick": pk, "ts": now_iso, "repick": True, "n": 0,
                          **({"oFirst": o} if o else {})})
            e.update({"pH": g["pH"], "pD": g["pD"], "pA": g["pA"]})
            e["n"] = e.get("n", 0) + 1
            if o:
                e["oLast"] = o
                e.setdefault("oFirst", o)

    res = get_results(espn_code)
    have = {r.get("key") for r in record}
    for k in list(picks.keys()):
        e = picks[k]
        if e.get("lg") != lgname:
            continue
        got = res.get(f'{e["date"]}|{e["away"]}@{e["home"]}')
        if got is None:
            # 10일이 지나도 결과가 안 잡히면 버립니다(연기·중단 경기).
            try:
                old = (datetime.now(timezone.utc).date()
                       - datetime.strptime(e["date"], "%Y-%m-%d").date()).days > 12
            except ValueError:
                old = True
            if old:
                del picks[k]
            continue
        hs, as_ = got
        actual = "H" if hs > as_ else ("A" if hs < as_ else "D")
        row = {"key": k, "lg": lgname, "date": e["date"], "home": e["home"],
               "away": e["away"], "pick": e["pick"], "res": actual,
               "hit": int(e["pick"] == actual), "score": f"{hs}-{as_}",
               "pPick": {"H": e["pH"], "D": e["pD"], "A": e["pA"]}[e["pick"]],
               "pH": e["pH"], "pD": e["pD"], "pA": e["pA"],
               "fallback": bool(e.get("fallback")), "snaps": e.get("n", 1)}
        of, ol = e.get("oFirst"), e.get("oLast")
        if of and ol and of.get(e["pick"]) and ol.get(e["pick"]):
            a0, a1 = float(of[e["pick"]]), float(ol[e["pick"]])
            row.update({"oPick": round(a0, 3), "oLast": round(a1, 3),
                        "clv": round((a0 / a1 - 1) * 100, 2)})
            mh, md, ma = devig3(float(of["H"]), float(of["D"]), float(of["A"]))
            row["pMkt"] = round({"H": mh, "D": md, "A": ma}[e["pick"]], 4)
        if k not in have:
            record.append(row)
            have.add(k)
        del picks[k]
    record.sort(key=lambda r: (r.get("date", ""), r.get("key", "")))


def devig3(h,d,a):
    ih,idd,ia = 1/h,1/d,1/a; s = ih+idd+ia
    return ih/s, idd/s, ia/s

CREDITS = {"left": None}



# ---------------------------------------------------------------------------
# 배당: ESPN 공개 경로 (2026-09-21 추가)
# 예전엔 The Odds API(무료 월 500크레딧)만 썼습니다. 그런데 야구 배당을 받는
# ESPN 공개 경로에서 축구 5개 리그 배당이 그대로 나옵니다 -- 무승부 가격까지.
# 게다가 팀 이름이 일정·결과와 같은 ESPN 표기라 이름 맞추는 작업이 사라집니다.
# 그래서 ESPN 을 1순위로 쓰고, 비면 그때만 유료 API 를 부릅니다.
# ---------------------------------------------------------------------------
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/soccer/leagues"


def _dec(american):
    """미국식 배당(+215, -150)을 소수 배당으로."""
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return round(1 + (a / 100 if a > 0 else 100 / -a), 3)


def _espn_team_name(ref):
    try:
        d = fetch_json(ref, timeout=15)
        return d.get("displayName")
    except Exception:
        return None


def espn_market_odds(espn_code, days_ahead=6):
    """ESPN 에서 승/무/패 배당. {"홈팀@원정팀": {"h2h": {...}, "book": ...}} 로
       get_market_odds 와 같은 모양으로 돌려줍니다."""
    today = datetime.now(timezone.utc)
    out = {}
    for off in range(days_ahead + 1):
        day = (today + timedelta(days=off)).strftime("%Y%m%d")
        try:
            ev = fetch_json(f"{ESPN_CORE}/{espn_code}/events?limit=60&dates={day}", timeout=20)
        except Exception:
            continue
        for item in (ev.get("items") or []):
            try:
                e = fetch_json(item["$ref"], timeout=15)
                comp = (e.get("competitions") or [{}])[0]
                oref = (comp.get("odds") or {}).get("$ref")
                if not oref:
                    continue
                od = fetch_json(oref, timeout=15)
                it = (od.get("items") or [None])[0]
                if not it:
                    continue
                names = {}
                for c in comp.get("competitors", []):
                    nm = _espn_team_name((c.get("team") or {}).get("$ref", ""))
                    if nm:
                        names[c.get("homeAway")] = nm
                h, a = names.get("home"), names.get("away")
                if not h or not a:
                    continue
                h2h = {}
                oh = _dec((it.get("homeTeamOdds") or {}).get("moneyLine"))
                oa = _dec((it.get("awayTeamOdds") or {}).get("moneyLine"))
                dr = _dec((it.get("drawOdds") or {}).get("moneyLine"))
                if oh: h2h[h] = oh
                if oa: h2h[a] = oa
                if dr: h2h["Draw"] = dr
                # 승·무·패 셋이 다 있어야 시장 확률을 제대로 뽑습니다.
                if len(h2h) < 3:
                    continue
                out[f"{h}@{a}"] = {"h2h": h2h,
                                   "book": (it.get("provider") or {}).get("name", "ESPN")}
            except Exception:
                continue
    return out


def get_market_odds(odds_sport, apikey):
    """무료 500크레딧/월이라 지역을 아껴 씁니다 -- eu 먼저, 비면 그때만 uk.
    (지역 하나당 1크레딧. 예전엔 매번 eu,uk 둘 다 불러 2배로 썼습니다.)"""
    if not apikey: return {}
    data = None
    for region in ("eu", "uk"):
        url = (f"https://api.the-odds-api.com/v4/sports/{odds_sport}/odds/"
               f"?apiKey={apikey}&regions={region}&markets=h2h&oddsFormat=decimal")
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=20) as r:
                left = r.headers.get("x-requests-remaining")
                if left is not None:
                    CREDITS["left"] = left
                data = json.load(r)
        except Exception:
            data = None
            continue
        if data:
            break
    if not data:
        return {}
    out = {}
    for ev in data:
        home, away = ev["home_team"], ev["away_team"]
        h2h = {}
        book = None
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk["key"] == "h2h" and not h2h:
                    for o in mk["outcomes"]:
                        h2h[o["name"]] = o["price"]
                    book = bk["key"]
        out[f"{home}@{away}"] = {"h2h":h2h, "book":book}
    return out

def main():
    # 키를 명령행 인자로 받으면 프로세스 목록·로그·역추적에 그대로 남습니다.
    # "-" 를 넘기면 환경변수에서만 읽습니다(권장). 옛 호출 방식도 그대로 받습니다.
    _a1 = sys.argv[1] if len(sys.argv) > 1 else "-"
    apikey = os.environ.get("ODDS_API_KEY", "") if _a1 in ("-", "") else _a1
    outpath = sys.argv[2] if len(sys.argv) > 2 else "soccer_out.json"
    # 3번째 인자: 직전 soccerData 블록(성적표와 진행 중 픽을 이어받습니다). 없어도 됩니다.
    prevpath = sys.argv[3] if len(sys.argv) > 3 else None
    prev = {}
    if prevpath:
        try:
            prev = json.loads(open(prevpath, encoding="utf-8").read()) or {}
        except Exception:
            prev = {}
    picks = prev.get("picks") or {}
    record = prev.get("record") or []
    now_iso = datetime.now(timezone.utc).isoformat()
    seeded = seed_from_prev(picks, prev, now_iso) if prev else 0
    if seeded:
        print(f"  예전 soccerData 에서 경기 전 픽 {seeded}건을 옮겨왔습니다")
    result = {"asOf": now_iso, "leagues": {}, "errors": []}
    for code, meta in CODES.items():
        try:
            model = fit_production_model(code, meta["espn"])
        except Exception as e:
            result["errors"].append(f"{meta['name']} fit failed: {e}")
            continue
        try:
            fixtures = get_fixtures(meta["espn"])
        except Exception as e:
            result["errors"].append(f"{meta['name']} fixtures failed: {e}")
            continue
        # 배당: 무료 ESPN 먼저, 비면 그때만 유료 API. ESPN 은 일정과 같은 팀 표기라
        # 이름 맞출 필요가 없습니다(유료 쪽은 표기가 달라 ALIAS 를 거쳐야 했습니다).
        # 예정 경기가 하나도 없으면 휴식기일 수 있습니다. 다음 경기일을 찾아 둡니다
        # (그래야 화면에서 "쉬는 중"과 "수집이 죽음"을 구분할 수 있습니다).
        if not [f for f in fixtures if not f["started"]]:
            try:
                far = espn_events(meta["espn"], datetime.now(timezone.utc).date(),
                                  (datetime.now(timezone.utc) + timedelta(days=45)).date())
                todaystr = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                nd = sorted({e["date"][:10] for e in far if e["date"][:10] > todaystr})
                if nd:
                    NEXT_DATE[meta["name"]] = nd[0]
            except Exception:
                pass
        odds = espn_market_odds(meta["espn"])
        ODDS_SRC[meta["name"]] = f"ESPN {len(odds)}건"
        if not odds:
            odds = get_market_odds(meta["odds"], apikey)
            ODDS_SRC[meta["name"]] = f"유료API {len(odds)}건 (ESPN 비어서 대체)"
        games = []
        for fx in fixtures:
            home_fd = ALIAS[code].get(fx["home"]); away_fd = ALIAS[code].get(fx["away"])
            pred = predict_with_fallback(model, home_fd, away_fd)
            g = {**fx, "pH":pred["pH"], "pD":pred["pD"], "pA":pred["pA"], "fallback":pred["fallback"]}
            # 상세 화면용 부가 정보. 확률에는 영향 없습니다.
            for _k in ("lam","mu","top","atkH","defH","atkA","defA","hfa","pOver25"):
                if _k in pred:
                    g[_k] = pred[_k]
            _df = model.get("_df")
            try:
                # 상대팀 이름을 ESPN 표기로 되돌려 둡니다. 페이지의 한글 이름표가
                # ESPN 표기 기준이라, football-data 표기 그대로 두면 한글이 안 붙습니다.
                _rev = {v: k for k, v in ALIAS[code].items()}
                g["formH"] = recent_form(_df, home_fd, rev=_rev)
                g["formA"] = recent_form(_df, away_fd, rev=_rev)
                g["h2h"] = h2h_recent(_df, home_fd, away_fd, rev=_rev)
                g["fdH"], g["fdA"] = home_fd, away_fd
            except Exception:
                pass
            key = f'{fx["home"]}@{fx["away"]}'
            mkt = odds.get(key) if not fx["started"] else None
            if mkt and mkt.get("h2h") and fx["home"] in mkt["h2h"] and fx["away"] in mkt["h2h"] and "Draw" in mkt["h2h"]:
                h2h = mkt["h2h"]
                mh,md,ma = devig3(h2h[fx["home"]], h2h["Draw"], h2h[fx["away"]])
                g["mktH"],g["mktD"],g["mktA"] = round(mh,3),round(md,3),round(ma,3)
                g["oh"],g["od"],g["oa"] = h2h[fx["home"]], h2h["Draw"], h2h[fx["away"]]
                g["book"] = mkt.get("book")
            games.append(g)
        result["leagues"][meta["name"]] = games
        try:
            track_picks(picks, record, meta["name"], meta["espn"], games, now_iso)
        except Exception as e:
            result["errors"].append(f"{meta['name']} 픽 정산 실패: {e}")
        result.setdefault("fit", {})[meta["name"]] = {
            "matches": model.get("_n"), "lastResult": model.get("_lastDate"),
            "gap": model.get("_gap")}
    srcs = {}
    for c, s_, where in SOURCE_LOG:
        srcs.setdefault(where, 0)
        srcs[where] += 1
    result["sources"] = srcs
    if CREDITS["left"] is not None:
        result["oddsCreditsLeft"] = CREDITS["left"]
    result["picks"] = picks
    result["record"] = record
    with open(outpath, "w") as f:
        result["oddsSrc"] = ODDS_SRC
        result["nextDate"] = NEXT_DATE
        json.dump(result, f, ensure_ascii=False)
    print(json.dumps({k: (len(v) if isinstance(v,list) else v) for k,v in result["leagues"].items()}))
    if CREDITS["left"] is not None:
        print(f"  배당 크레딧 남음 {CREDITS['left']}")
    hits = [r for r in record if "hit" in r]
    print(f"  픽 추적 중 {len(picks)}건 · 정산 누적 {len(record)}건"
          + (f" · 적중 {sum(r['hit'] for r in hits)}/{len(hits)}" if hits else ""))
    if result["errors"]:
        print("ERRORS:", result["errors"], file=sys.stderr)

if __name__ == "__main__":
    main()



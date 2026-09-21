




"""매일 자동 갱신기 -- 아티팩트 안에 통째로 실려 다니는 스크립트

예약 작업은 빈 컨테이너에서 시작하므로, 과거 데이터를 매번 다시 긁을 수 없습니다.
그래서 '모델 상태'(팀 Elo + 투수 최근 15등판 기록)를 아티팩트 안에 넣어두고,
매일 이 스크립트가:

  1) 어제 결과를 각 리그 공개 API로 받아 상태를 갱신하고
  2) 오늘 경기·선발·(MLB는)라인업·부상자를 받아
  3) 예측해서 games JSON을 뱉습니다.

과거를 다시 안 긁으니 1분 안에 끝납니다.

사용:
    python daily_update.py <state.b64> <출력.json> [이전games.json] [옵션]

옵션:
    --lg=KBO,NPB    이 리그만 갱신 (기본: MLB,KBO,NPB)
    --no-settle     정산 건너뜀 (같은 날 두 번째 실행 -- 라인업 확정 런에서 사용)
    --track=파일     지금까지의 누적 성적(trackRecord) JSON. 이미 정산된 경기를
                    스스로 걸러내므로 며칠 밀린 결과도 안전하게 따라잡습니다.

리그를 골라 돌려도 state 는 통째로 다시 뱉으므로, 고르지 않은 리그의
Elo·투수 기록은 손대지 않고 그대로 보존됩니다. 그래서 MLB 런과 KBO/NPB 런을
다른 시각에 따로 돌려도 서로를 지우지 않습니다.
"""
from __future__ import annotations

import base64
import gzip
import os
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
import html as _html
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
KST = timezone(timedelta(hours=9))
K, HFA, INIT, REG, SPW = 14.0, 24.0, 1500.0, 0.30, 15
TW, PW, LW = 30, 40, 300   # 총득점용 창: 팀 최근 30경기 · 홈구장 40 · 리그 300
KBO_ALIAS = {"넥센": "키움", "SK": "SSG", "우리": "키움", "히어로즈": "키움"}
NPB_NOT_TEAM = {"セ・リーグ", "パ・リーグ", "セ", "パ", "全セ", "全パ"}


def strip(s):
    return _html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def fetch(url, data=None, hdr=None, timeout=30, tries=4):
    h = dict(UA); h.update(hdr or {})
    for i in range(tries):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, data=data, headers=h), timeout=timeout).read()
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.5 + 2 * i)


# ---------------------------------------------------------------- 배당 자동 수집
# ESPN 공개 API. 형님이 손으로 배당을 넣던 걸 없애기 위한 부분입니다.
# MLB와 유럽 축구는 여기서 머니라인·총점(언더오버)까지 한 번에 받아옵니다.
# KBO·NPB는 ESPN이 다루지 않아 그쪽은 여전히 형님 스크린샷이 필요합니다.
CORE = "https://sports.core.api.espn.com/v2/sports"
ESPN2CODE = {"LAA":"ANA","ARI":"ARI","ATL":"ATL","BAL":"BAL","BOS":"BOS","CHW":"CHA",
 "CWS":"CHA","CHC":"CHN","CIN":"CIN","CLE":"CLE","COL":"COL","DET":"DET","HOU":"HOU",
 "KC":"KCA","KCR":"KCA","LAD":"LAN","MIA":"MIA","MIL":"MIL","MIN":"MIN","NYY":"NYA",
 "NYM":"NYN","OAK":"OAK","ATH":"OAK","PHI":"PHI","PIT":"PIT","SD":"SDN","SDP":"SDN",
 "SEA":"SEA","SF":"SFN","SFG":"SFN","STL":"SLN","TB":"TBA","TBR":"TBA","TEX":"TEX",
 "TOR":"TOR","WSH":"WAS","WAS":"WAS"}


def jget(u, timeout=25):
    b = fetch(u.replace("http://", "https://"), timeout=timeout, tries=2)
    try:
        return json.loads(b) if b else None
    except Exception:
        return None


def dec(american):
    """미국식 배당(-192, +178)을 형님이 보시는 소수 배당으로."""
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return round(1 + (a / 100 if a > 0 else 100 / -a), 3)


def _odds_one(ev_ref):
    ev = jget(ev_ref)
    if not ev:
        return None
    short = ev.get("shortName", "")            # "CIN @ CHC"
    if "@" not in short:
        return None
    a, h = [x.strip() for x in short.split("@")[:2]]
    comp = (ev.get("competitions") or [{}])[0]
    oref = (comp.get("odds") or {}).get("$ref")
    if not oref:
        return None
    od = jget(oref)
    if not od or not od.get("items"):
        return None
    it = od["items"][0]
    for cand in od["items"]:
        if cand.get("overUnder") is not None:
            it = cand
            break
    return (f"{ESPN2CODE.get(a, a)}@{ESPN2CODE.get(h, h)}",
            {"book": (it.get("provider") or {}).get("name", ""),
             "oh": dec((it.get("homeTeamOdds") or {}).get("moneyLine")),
             "oa": dec((it.get("awayTeamOdds") or {}).get("moneyLine")),
             "total": it.get("overUnder"),
             "over": dec(it.get("overOdds")), "under": dec(it.get("underOdds"))})


def espn_odds(sport, league, yyyymmdd):
    d = jget(f"{CORE}/{sport}/leagues/{league}/events?limit=60&dates={yyyymmdd}")
    if not d or not d.get("items"):
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_odds_one, [i["$ref"] for i in d["items"]]):
            if r:
                out[r[0]] = r[1]
    return out


# ---------------------------------------------------------------- 결과 수집

def mlb_lineup(pk):
    """확정 타순. 경기 직전에만 나옵니다. [{i:타순, id:선수, pos:포지션}] 형태.
       못 받아도 조용히 빈 값을 돌려줍니다 -- 라인업 없다고 픽을 막을 이유는 없습니다."""
    try:
        b = fetch(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore", timeout=30)
        if not b:
            return {}, {}
        box = json.loads(b)
    except Exception:
        return {}, {}
    out = {}
    for side in ("home", "away"):
        rows = []
        for p in (box.get("teams", {}).get(side, {}).get("players") or {}).values():
            bo = p.get("battingOrder")
            if not bo or int(bo) % 100:      # 100,200,... 만 선발. 101 등은 교체.
                continue
            rows.append({"i": int(bo) // 100, "id": p["person"]["id"],
                         "n": p["person"]["fullName"],
                         "pos": (p.get("position") or {}).get("abbreviation", "")})
        rows.sort(key=lambda r: r["i"])
        out[side] = rows
    return out.get("home", []), out.get("away", [])

def mlb_results(d0, d1):
    u = (f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={d0}"
         f"&endDate={d1}&hydrate=probablePitcher&gameType=R")
    b = fetch(u, timeout=60)
    if not b:
        return []
    NM = {"Los Angeles Angels": "ANA", "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
          "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS", "Chicago White Sox": "CHA",
          "Chicago Cubs": "CHN", "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
          "Colorado Rockies": "COL", "Detroit Tigers": "DET", "Houston Astros": "HOU",
          "Kansas City Royals": "KCA", "Los Angeles Dodgers": "LAN", "Miami Marlins": "MIA",
          "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN", "New York Yankees": "NYA",
          "New York Mets": "NYN", "Oakland Athletics": "OAK", "Athletics": "OAK",
          "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT", "San Diego Padres": "SDN",
          "Seattle Mariners": "SEA", "San Francisco Giants": "SFN", "St. Louis Cardinals": "SLN",
          "Tampa Bay Rays": "TBA", "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR",
          "Washington Nationals": "WAS"}
    out = []
    for dt in json.loads(b).get("dates", []):
        for g in dt.get("games", []):
            t = g["teams"]
            an, hn = t["away"]["team"]["name"], t["home"]["team"]["name"]
            if an not in NM or hn not in NM or g["status"]["detailedState"] != "Final":
                continue
            if t["away"].get("score") is None:
                continue
            out.append({"date": g["officialDate"], "away": NM[an], "home": NM[hn],
                        "as": t["away"]["score"], "hs": t["home"]["score"],
                        "asp": (t["away"].get("probablePitcher") or {}).get("fullName", ""),
                        "hsp": (t["home"].get("probablePitcher") or {}).get("fullName", "")})
    return out


KBO_URL = "https://www.koreabaseball.com/ws/Main.asmx/GetKboGameList"
KBO_HDR = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
           "Referer": "https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx"}


def kbo_day(yyyymmdd):
    b = fetch(KBO_URL, urllib.parse.urlencode(
        {"leId": "1", "srId": "0,1,3,4,5,6,7,8,9", "date": yyyymmdd}).encode(), KBO_HDR)
    return json.loads(b).get("game", []) if b else []


def npb_month(yr, mo):
    b = fetch(f"https://npb.jp/games/{yr}/schedule_{mo:02d}_detail.html", timeout=45)
    if not b:
        return []
    t = b.decode("utf-8", "replace")
    CELL = re.compile(r'class="(team1|team2|score1|score2|pit|time)"[^>]*>(.*?)</div>', re.S)
    DATE = re.compile(r'id="date(\d{2})(\d{2})"')
    cur, out = None, []
    for block in re.split(r"<tr", t)[1:]:
        m = DATE.search(block)
        if m:
            cur = (m.group(1), m.group(2))
        if not cur:
            continue
        d, pits = {}, []
        for k, v in CELL.findall(block):
            if k == "pit":
                pits.append(strip(v).replace("先発：", ""))
            else:
                d.setdefault(k, strip(v))
        if not {"team1", "team2"} <= d.keys():
            continue
        if d["team1"] in NPB_NOT_TEAM or d["team2"] in NPB_NOT_TEAM:
            continue
        out.append({"date": f"{yr}-{cur[0]}-{cur[1]}", "home": d["team1"], "away": d["team2"],
                    "hs": d.get("score1", ""), "as": d.get("score2", ""),
                    "hsp": pits[0] if pits else "", "asp": pits[1] if len(pits) > 1 else "",
                    "time": d.get("time", "")})
    return out



def keys(g):
    """이전 실행이 남긴 게임에서 정규 식별자를 뽑는다.
    새 포맷은 hKey/aKey, 옛 포맷은 home/away(짧은 코드)에 들어 있다."""
    h = g.get("hKey") or g.get("home") or MLB_CODE.get(g.get("homeFull") or "", "")
    a = g.get("aKey") or g.get("away") or MLB_CODE.get(g.get("awayFull") or "", "")
    return h, a


# ---------------------------------------------------------------- 어제 픽 정산
def gkey(lg, date, away, home):
    return f"{lg}|{date}|{away}@{home}"


def settle(prev_games, now, track=()):
    """이전 실행 픽을 실제 결과와 맞춰본다. 형님이 손으로 안 눌러도 되게.

    track 에 이미 정산된 기록을 넘기면 중복을 스스로 걸러냅니다. 그래서
    되돌아보는 날짜를 넉넉히 잡아도(리그 페이지가 하루 늦게 갱신되는 NPB 같은 경우)
    같은 경기가 두 번 들어가지 않습니다."""
    if not prev_games:
        return []
    seen = {t.get("key") or gkey(t.get("lg"), t.get("date"), t.get("away"), t.get("home"))
            for t in track}
    want = {}
    for g in prev_games:
        if g.get("cancelled"):
            continue
        want.setdefault(g.get("lg"), []).append(g)
    out = []

    # --- MLB: 미국 날짜 기준 최근 3일치를 훑어 매칭 ---
    if want.get("MLB"):
        d1 = (now - timedelta(hours=13)).strftime("%Y-%m-%d")
        d0 = (now - timedelta(hours=13) - timedelta(days=3)).strftime("%Y-%m-%d")
        res = {}
        for r in mlb_results(d0, d1):
            res[(r["home"], r["away"])] = r
        for g in want["MLB"]:
            r = res.get(keys(g))
            if not r or r["hs"] == r["as"]:
                continue
            actual = g["homeFull"] if r["hs"] > r["as"] else g["awayFull"]
            pick = g["homeFull"] if g["pHome"] >= .5 else g["awayFull"]
            k = gkey("MLB", r["date"], g["awayFull"], g["homeFull"])
            if k in seen:
                continue
            seen.add(k)
            # ckey 는 미국 날짜, ckeyK 는 한국 날짜 기준입니다. CLV 기록은 한국 날짜로
            # 열리기 때문에 ckey 만으로는 MLB가 하루씩 어긋나 영원히 안 붙습니다.
            out.append({"key": k, "ckey": gkey("MLB", r["date"], keys(g)[1], keys(g)[0]),
                        "ckeyK": gkey("MLB", g.get("date") or r["date"],
                                      keys(g)[1], keys(g)[0]),
                        "date": r["date"], "lg": "MLB", "away": g["awayFull"],
                        "home": g["homeFull"], "pick": pick, "pHome": g["pHome"],
                        "actual": actual, "hit": int(pick == actual),
                        "score": f"{r['as']}-{r['hs']}"})

    # --- KBO ---
    if want.get("KBO"):
        for back in (0, 1, 2, 3):
            day = (now - timedelta(days=back)).strftime("%Y%m%d")
            res = {}
            for g in kbo_day(day):
                a = KBO_ALIAS.get(strip(g["AWAY_NM"]), strip(g["AWAY_NM"]))
                h = KBO_ALIAS.get(strip(g["HOME_NM"]), strip(g["HOME_NM"]))
                try:
                    res[(h, a)] = (int(g["B_SCORE_CN"]), int(g["T_SCORE_CN"]),
                                   f"{day[:4]}-{day[4:6]}-{day[6:]}")
                except (TypeError, ValueError):
                    pass
            for g in want["KBO"]:
                r = res.get(keys(g))
                if not r or r[0] == r[1]:
                    continue
                actual = g["homeFull"] if r[0] > r[1] else g["awayFull"]
                pick = g["homeFull"] if g["pHome"] >= .5 else g["awayFull"]
                k = gkey("KBO", r[2], g["awayFull"], g["homeFull"])
                if k in seen:
                    continue
                seen.add(k)
                out.append({"key": k, "ckey": gkey("KBO", r[2], keys(g)[1], keys(g)[0]),
                        "date": r[2], "lg": "KBO", "away": g["awayFull"],
                            "home": g["homeFull"], "pick": pick, "pHome": g["pHome"],
                            "actual": actual, "hit": int(pick == actual),
                            "score": f"{r[1]}-{r[0]}"})

    # --- NPB ---
    if want.get("NPB"):
        res = {}
        for g in npb_month(now.year, now.month) + npb_month(now.year, max(1, now.month - 1)):
            if g["hs"].isdigit() and g["as"].isdigit():
                res[(g["home"], g["away"], g["date"])] = (int(g["hs"]), int(g["as"]))
        for g in want["NPB"]:
            for back in (0, 1, 2, 3):
                day = (now - timedelta(days=back)).strftime("%Y-%m-%d")
                kh, ka = keys(g)
                r = res.get((kh, ka, day))
                if not r or r[0] == r[1]:
                    continue
                actual = g["homeFull"] if r[0] > r[1] else g["awayFull"]
                pick = g["homeFull"] if g["pHome"] >= .5 else g["awayFull"]
                k = gkey("NPB", day, g["awayFull"], g["homeFull"])
                if k in seen:
                    break
                seen.add(k)
                out.append({"key": k, "ckey": gkey("NPB", day, keys(g)[1], keys(g)[0]),
                        "date": day, "lg": "NPB", "away": g["awayFull"],
                            "home": g["homeFull"], "pick": pick, "pHome": g["pHome"],
                            "actual": actual, "hit": int(pick == actual),
                            "score": f"{r[1]}-{r[0]}"})
                # 언더/오버도 같이 정산 (기준선 7.5 -- 검증 때 쓴 선)
                po = (g.get("pOver") or {}).get("7.5")
                if po is not None:
                    tot = r[0] + r[1]
                    opick = "오버" if po >= .5 else "언더"
                    got = "오버" if tot > 7.5 else "언더"
                    out[-1].update({"ouPick": opick, "ouP": po, "ouActual": got,
                                    "ouHit": int(opick == got), "total": tot,
                                    "totalPred": g.get("total")})
                break
    return out


# ------------------------------------------------- CLV(픽 시점 대비 마지막 배당) 기록
# 목적: "내 픽 시점 배당이 그 뒤 움직임보다 유리했나"를 사전 기록으로 판정한다.
# 경기 결과를 안 기다려도 되고 수백 건이면 통계가 난다 -- 승패 적중률보다 훨씬 빠르다.
# 핵심은 사전 기록이다. 나중에 맞춰보는 건 자기기만이 가능해서 아무 의미가 없다.
def devig2(oh, oa):
    ih, ia = 1.0 / oh, 1.0 / oa
    return ih / (ih + ia)


def clv_update(clv, games, lgs, now, settled):
    op = clv.setdefault("open", {})
    done = clv.setdefault("settled", [])
    ts = now.strftime("%Y-%m-%d %H:%M")
    today = now.strftime("%Y-%m-%d")
    for g in games:
        oh, oa = g.get("oh"), g.get("oa")
        if not oh or not oa or g.get("cancelled"):
            continue
        try:
            oh, oa = float(oh), float(oa)
        except (TypeError, ValueError):
            continue
        if oh <= 1 or oa <= 1:
            continue
        k = gkey(g["lg"], g.get("date", today), g["aKey"], g["hKey"])
        pick = "H" if g["pHome"] >= 0.5 else "A"
        snap = {"ts": ts, "oh": oh, "oa": oa, "o": oh if pick == "H" else oa}
        e = op.get(k)
        if e is None:
            op[k] = {"lg": g["lg"], "date": g.get("date", today), "start": g.get("kst", ""),
                     "away": g["aKey"], "home": g["hKey"], "pick": pick,
                     "pModel": round(g["pHome"] if pick == "H" else 1 - g["pHome"], 4),
                     "first": snap, "last": snap, "n": 1}
        elif not g.get("started"):
            if e["pick"] != pick:
                # 픽이 바뀌면 기준 시점도 새로 잡는다. 안 그러면 반대쪽 가격을
                # "내 픽 시점 가격"이라고 우기게 된다.
                e.update({"pick": pick, "first": snap, "n": 0, "repick": True,
                          "pModel": round(g["pHome"] if pick == "H" else 1 - g["pHome"], 4)})
            e["last"] = snap
            e["n"] = e.get("n", 0) + 1
    # 정산 기록의 key 는 한글 팀명이고 CLV 키는 팀 코드라 서로 안 맞습니다.
    # settle() 이 같이 넣어주는 ckey(코드 기반)로 붙입니다.
    hit_by = {}
    for s in (settled or []):
        for kk in (s.get("ckeyK"), s.get("ckey"), s.get("key")):
            if kk:
                hit_by[kk] = s.get("hit")
    for k in list(op.keys()):
        e = op[k]
        if e.get("lg") not in lgs:
            continue
        # 어제까지의 경기이거나, 방금 결과가 나온 경기(같은 날 마감)면 닫습니다.
        if e.get("date", "") >= today and k not in hit_by:
            continue
        f, l = e["first"], e["last"]
        mf, ml = devig2(f["oh"], f["oa"]), devig2(l["oh"], l["oa"])
        if e["pick"] == "A":
            mf, ml = 1 - mf, 1 - ml
        row = {"key": k, "date": e["date"], "lg": e["lg"], "away": e["away"],
               "home": e["home"], "pick": e["pick"], "pModel": e["pModel"],
               "snaps": e.get("n", 1), "oPick": round(f["o"], 3), "oLast": round(l["o"], 3),
               "pMkt": round(mf, 4), "pMktLast": round(ml, 4),
               "clv": round((f["o"] / l["o"] - 1) * 100, 2),
               # 스냅샷이 하나뿐이면 first==last 라 clv 가 기계적으로 0입니다.
               # 진짜 '안 움직였다'가 아니라 '못 쟀다'이므로 평균에서 빼야 합니다.
               "measured": 1 if e.get("n", 1) >= 2 else 0,
               "tFirst": f["ts"], "tLast": l["ts"]}
        if k in hit_by:
            row["hit"] = hit_by[k]
        done.append(row)
        del op[k]
    done.sort(key=lambda r: (r.get("date", ""), r.get("key", "")))
    return clv


MLB_CODE = {"Los Angeles Angels":"ANA","Arizona Diamondbacks":"ARI","Atlanta Braves":"ATL",
  "Baltimore Orioles":"BAL","Boston Red Sox":"BOS","Chicago White Sox":"CHA",
  "Chicago Cubs":"CHN","Cincinnati Reds":"CIN","Cleveland Guardians":"CLE",
  "Colorado Rockies":"COL","Detroit Tigers":"DET","Houston Astros":"HOU",
  "Kansas City Royals":"KCA","Los Angeles Dodgers":"LAN","Miami Marlins":"MIA",
  "Milwaukee Brewers":"MIL","Minnesota Twins":"MIN","New York Yankees":"NYA",
  "New York Mets":"NYN","Oakland Athletics":"OAK","Athletics":"OAK",
  "Philadelphia Phillies":"PHI","Pittsburgh Pirates":"PIT","San Diego Padres":"SDN",
  "Seattle Mariners":"SEA","San Francisco Giants":"SFN","St. Louis Cardinals":"SLN",
  "Tampa Bay Rays":"TBA","Texas Rangers":"TEX","Toronto Blue Jays":"TOR",
  "Washington Nationals":"WAS"}

# ---------------------------------------------------------------- 상태 갱신
def bump(st, home, away, hs, as_, hsp, asp):
    """경기 하나로 Elo·폼·투수·득점 기록을 갱신 (예측 이후에만 호출)."""
    bump_runs(st, home, away, hs, as_)
    if hs == as_:
        return                      # 무승부는 Elo·폼에 반영하지 않습니다
    elo, form, sp = st["elo"], st["form"], st["sp"]
    for t in (home, away):
        elo.setdefault(t, INIT); form.setdefault(t, [])
    for p in (hsp, asp):
        sp.setdefault(p, [[], []])
    hw = 1 if hs > as_ else 0
    eh, ea = elo[home], elo[away]
    exp = 1 / (1 + 10 ** ((ea - eh - HFA) / 400))
    elo[home] = round(eh + K * (hw - exp), 1)
    elo[away] = round(ea - K * (hw - exp), 1)
    form[home] = (form[home] + [hw])[-60:]
    form[away] = (form[away] + [1 - hw])[-60:]
    sp[hsp][0] = (sp[hsp][0] + [as_])[-SPW:]; sp[hsp][1] = (sp[hsp][1] + [hw])[-SPW:]
    sp[asp][0] = (sp[asp][0] + [hs])[-SPW:]; sp[asp][1] = (sp[asp][1] + [1 - hw])[-SPW:]



def bump_runs(st, home, away, hs, as_):
    """총득점(언더/오버)용 누적 -- NPB만 실전에 쓰지만 세 리그 다 모읍니다.
    MLB·KBO는 지금은 신호가 없지만, 데이터가 쌓인 뒤 다시 재보려고 남겨둡니다."""
    rs, ra, pk = st.setdefault("rs", {}), st.setdefault("ra", {}), st.setdefault("park", {})
    for t in (home, away):
        rs.setdefault(t, []); ra.setdefault(t, [])
    pk.setdefault(home, [])
    rs[home] = (rs[home] + [hs])[-TW:]; ra[home] = (ra[home] + [as_])[-TW:]
    rs[away] = (rs[away] + [as_])[-TW:]; ra[away] = (ra[away] + [hs])[-TW:]
    pk[home] = (pk[home] + [hs + as_])[-PW:]
    st["lgtot"] = (st.get("lgtot", []) + [hs + as_])[-LW:]


def mean(xs, d=None):
    return sum(xs) / len(xs) if xs else d


def predict(st, home, away, hsp, asp, use_sp):
    """상태에서 홈 승리 확률. 계수는 리그별로 학습해 둔 값(아래 COEF)."""
    elo, form, sp = st["elo"], st["form"], st["sp"]
    ed = elo.get(home, INIT) - elo.get(away, INIT) + HFA
    hf, af = form.get(home, []), form.get(away, [])
    c = COEF[st["league"]]
    z = c["b"] + c["elo"] * (ed / 100.0)
    z += c["form"] * ((mean(hf[-25:], .5) or .5) - (mean(af[-25:], .5) or .5))
    ev = {"elo_diff": round(ed, 1)}
    if use_sp:
        hr = mean(sp.get(hsp, [[], []])[0], None)
        ar = mean(sp.get(asp, [[], []])[0], None)
        ev["h_sp_ra"] = round(hr, 2) if hr is not None else None
        ev["a_sp_ra"] = round(ar, 2) if ar is not None else None
        ev["h_sp_n"] = len(sp.get(hsp, [[], []])[0])
        ev["a_sp_n"] = len(sp.get(asp, [[], []])[0])
        if hr is not None and ar is not None:
            z += c["sp"] * (ar - hr)          # 상대 선발이 더 많이 내주면 홈에 유리
    return 1 / (1 + math.exp(-z)), ev



# ---------------------------------------------------------------- 총득점 (언더/오버)
# 홀드아웃으로 재본 결과 NPB만 유의했습니다(BSS +2~4%, 학습비율 60/70/80% 모두).
# MLB와 KBO는 베이스라인을 못 이겼습니다 -- 그래서 숫자를 만들지 않습니다.
TOT_COEF = {
    "NPB": {"coef": {"h_rs": 0.15418, "h_ra": 0.102492, "a_rs": 0.280058, "a_ra": 0.108273,
                     "h_sp": 0.09864, "a_sp": 0.061582, "park": 0.543363,
                     "h_pen": -0.056106, "a_pen": -0.015598, "lgm": -0.058424},
            "b": 0.8402, "sigma": 4.0182},
}
TOT_LINES = [5.5, 6.5, 7.5, 8.5, 9.5, 10.5]


def _ncdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def predict_total(st, home, away, hsp, asp):
    """예상 총득점과 기준선별 오버 확률. 계수 없는 리그는 None."""
    c = TOT_COEF.get(st["league"])
    if not c:
        return None
    rs, ra, pk = st.get("rs", {}), st.get("ra", {}), st.get("park", {})
    spd = st.get("sp", {})
    f = {"h_rs": mean(rs.get(home, [])), "h_ra": mean(ra.get(home, [])),
         "a_rs": mean(rs.get(away, [])), "a_ra": mean(ra.get(away, [])),
         "h_sp": mean(spd.get(hsp, [[], []])[0]), "a_sp": mean(spd.get(asp, [[], []])[0]),
         "park": mean(pk.get(home, [])), "lgm": mean(st.get("lgtot", []))}
    # 선발이 아직 발표 안 됐으면 그 팀 평균 실점으로 대신합니다(불펜 성분 0).
    if f["h_sp"] is None:
        f["h_sp"] = f["h_ra"]
    if f["a_sp"] is None:
        f["a_sp"] = f["a_ra"]
    if any(v is None for v in f.values()):
        return None
    f["h_pen"] = f["h_ra"] - f["h_sp"]
    f["a_pen"] = f["a_ra"] - f["a_sp"]
    sp_known = bool(hsp) and bool(asp)
    mu = c["b"] + sum(c["coef"][k] * f[k] for k in c["coef"])
    sig = c["sigma"]
    return {"total": round(mu, 2), "totalSp": sp_known, "sig": round(sig, 3),
            "pOver": {str(L): round(1 - _ncdf((L - mu) / sig), 4) for L in TOT_LINES}}


# 리그별 계수 (홀드아웃 학습본에서 뽑아 고정 -- 매일 재학습하지 않습니다)
COEF = {
    "MLB": {"b": 0.0546, "elo": 0.4013, "form": -0.3618, "sp": 0.0545},
    "KBO": {"b": -0.0013, "elo": 0.3937, "form": -0.4944, "sp": 0.0691},
    "NPB": {"b": 0.0672, "elo": 0.3418, "form": -0.1939, "sp": 0.0},
}
# 참고: form 계수가 음수인 건 오류가 아닙니다. Elo가 이미 전력을 담고 있어서,
# 그 위에 남은 '최근 몇 경기 잘나감'은 평균으로 되돌아가는 성분이기 때문입니다.
USE_SP = {"MLB": True, "KBO": True, "NPB": False}




# ---------------------------------------------------------------- KBO·NPB 배당
# ESPN이 두 리그를 안 다뤄서, 여기만 The Odds API(무료 월 500크레딧)를 씁니다.
# 키는 환경변수 ODDS_API_KEY 로만 받습니다. 코드에도, 페이지에도 절대 안 남깁니다.
# 키가 없으면 조용히 건너뜁니다 -- 그러면 형님이 화면에서 직접 넣으시면 됩니다.
ODDS_SPORT = {"KBO": "baseball_kbo", "NPB": "baseball_npb"}

# The Odds API 는 영문 팀명을 줍니다. 우리 키로 바꾸기 위한 '결정적인 단어' 표.
ODDS_NAME = {
    "KBO": {"doosan": "두산", "lg": "LG", "kt": "KT", "samsung": "삼성", "lotte": "롯데",
            "kia": "KIA", "nc": "NC", "hanwha": "한화", "ssg": "SSG", "kiwoom": "키움",
            "landers": "SSG", "heroes": "키움", "dinos": "NC", "eagles": "한화",
            "bears": "두산", "twins": "LG", "wiz": "KT", "lions": "삼성", "giants": "롯데"},
    "NPB": {"yomiuri": "巨人", "hanshin": "阪神", "chunichi": "中日", "denA": "DeNA",
            "dena": "DeNA", "baystars": "DeNA", "hiroshima": "広島", "carp": "広島",
            "yakult": "ヤクルト", "swallows": "ヤクルト", "softbank": "ソフトバンク",
            "hawks": "ソフトバンク", "nippon-ham": "日本ハム", "nippon": "日本ハム",
            "fighters": "日本ハム", "marines": "ロッテ", "rakuten": "楽天",
            "seibu": "西武", "orix": "オリックス", "buffaloes": "オリックス",
            "dragons": "中日", "tigers": "阪神", "giants2": "巨人"},
}


def _odds_team(lg, name):
    """영문 팀명에서 우리 키를 뽑는다. 못 찾으면 None (그 경기만 건너뜁니다)."""
    toks = re.split(r"[^A-Za-z\-]+", (name or "").lower())
    tbl = ODDS_NAME.get(lg, {})
    hits = {tbl[t] for t in toks if t in tbl}
    # 롯데는 KBO(자이언츠)와 NPB(마린스) 양쪽에 있어 토큰 하나로는 위험합니다.
    if lg == "KBO" and "lotte" in toks:
        hits = {"롯데"}
    if lg == "NPB" and "lotte" in toks:
        hits = {"ロッテ"}
    return hits.pop() if len(hits) == 1 else None


def kn_odds(lg, today):
    """오늘 경기의 승패 배당 + 총점 기준선. {"원정@홈": {...}} 형태.

    지역(region)마다 취급하는 북메이커가 달라서 KBO·NPB는 한 지역에만 걸려 있을 수
    있습니다. eu → us → au 순으로 비어 있을 때만 다음으로 넘어갑니다(그만큼만 씁니다).
    """
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key or lg not in ODDS_SPORT:
        return {}, None
    data, left, used = [], None, ""
    for region in ("eu", "us", "au"):
        url = (f"https://api.the-odds-api.com/v4/sports/{ODDS_SPORT[lg]}/odds"
               f"?apiKey={key}&regions={region}&markets=h2h,totals&oddsFormat=decimal")
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30)
            left = r.headers.get("x-requests-remaining")
            data = json.loads(r.read())
        except Exception as e:
            code = getattr(e, "code", "")
            print(f"  [{lg}] 배당 요청 실패({region}) {type(e).__name__} {code} — "
                  f"화면에서 직접 넣으시면 됩니다")
            return {}, left
        if data:
            used = region
            break
    print(f"  [{lg}] 배당 응답 {len(data)}경기 (지역 {used or '전부 비어 있음'})")
    if not data:
        return {}, left
    out, unmatched = {}, []
    for ev in data:
        h = _odds_team(lg, ev.get("home_team"))
        a = _odds_team(lg, ev.get("away_team"))
        if not h or not a:
            unmatched.append(f"{ev.get('away_team')} @ {ev.get('home_team')}")
            continue
        kst = datetime.fromisoformat(
            ev["commence_time"].replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d")
        if kst != today:
            continue
        oh = oa = tot = ov = un = None
        book = ""
        for bm in ev.get("bookmakers", []):
            for mk in bm.get("markets", []):
                if mk["key"] == "h2h" and oh is None:
                    for o in mk["outcomes"]:
                        if _odds_team(lg, o["name"]) == h:
                            oh = round(float(o["price"]), 3)
                        elif _odds_team(lg, o["name"]) == a:
                            oa = round(float(o["price"]), 3)
                    if oh and oa:
                        book = bm.get("title", "")
                if mk["key"] == "totals" and tot is None:
                    for o in mk["outcomes"]:
                        if o["name"] == "Over":
                            tot, ov = o.get("point"), round(float(o["price"]), 3)
                        elif o["name"] == "Under":
                            un = round(float(o["price"]), 3)
            if oh and oa and tot is not None:
                break
        rec = {k: v for k, v in (("oh", oh), ("oa", oa), ("mktTotal", tot),
                                 ("over", ov), ("under", un), ("book", book)) if v is not None}
        if rec:
            out[f"{a}@{h}"] = rec
    if unmatched:
        print(f"  [{lg}] 팀명 매칭 실패 {len(unmatched)}건: " + " / ".join(unmatched[:6]))
    print(f"  [{lg}] 오늘 경기 배당 {len(out)}건 확보")
    return out, left

# ---------------------------------------------------------------- 과거 기록 재수집
# 아티팩트를 못 읽는 환경(네트워크 허용목록)에서도 돌아가게 하는 길입니다.
# 저장된 상태 없이, 공개 API에서 최근 두 시즌을 다시 받아 상태를 처음부터 만듭니다.
# 그러면 '어제 픽'도 기억할 필요가 없습니다 -- 그날 이전 데이터만으로 다시 예측해서
# 실제 결과와 맞춰보면 되니까요. 성적표 전체가 매번 재현됩니다.
def hist_mlb(y0, y1):
    out = []
    for y in range(y0, y1 + 1):
        d = jget(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={y}-01-01"
                 f"&endDate={y}-12-31&hydrate=probablePitcher&gameType=R", timeout=120)
        for dt in (d or {}).get("dates", []):
            for g in dt.get("games", []):
                if g["status"]["detailedState"] != "Final":
                    continue
                t = g["teams"]
                hn, an = t["home"]["team"]["name"], t["away"]["team"]["name"]
                if hn not in MLB_CODE or an not in MLB_CODE or t["home"].get("score") is None:
                    continue
                out.append({"date": g["officialDate"], "lg": "MLB",
                            "home": MLB_CODE[hn], "away": MLB_CODE[an],
                            "hs": t["home"]["score"], "as": t["away"]["score"],
                            "hsp": (t["home"].get("probablePitcher") or {}).get("fullName", "") or "(unknown)",
                            "asp": (t["away"].get("probablePitcher") or {}).get("fullName", "") or "(unknown)"})
    return out


def hist_kbo(d0, d1):
    days = []
    d = d0.date() if isinstance(d0, datetime) else d0
    while d <= d1:
        days.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    out = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for day, games in zip(days, ex.map(kbo_day, days)):
            for g in games or []:
                try:
                    hs, as_ = int(g["B_SCORE_CN"]), int(g["T_SCORE_CN"])
                except (TypeError, ValueError):
                    continue
                out.append({"date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "lg": "KBO",
                            "home": KBO_ALIAS.get(strip(g["HOME_NM"]), strip(g["HOME_NM"])),
                            "away": KBO_ALIAS.get(strip(g["AWAY_NM"]), strip(g["AWAY_NM"])),
                            "hs": hs, "as": as_,
                            "hsp": strip(g.get("B_PIT_P_NM")) or "(unknown)",
                            "asp": strip(g.get("T_PIT_P_NM")) or "(unknown)"})
    return out


def hist_npb(y0, y1):
    jobs = [(y, m) for y in range(y0, y1 + 1) for m in range(3, 12)]
    out = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for rows in ex.map(lambda ym: npb_month(*ym), jobs):
            for g in rows or []:
                if not (g["hs"].isdigit() and g["as"].isdigit()):
                    continue
                out.append({"date": g["date"], "lg": "NPB", "home": g["home"], "away": g["away"],
                            "hs": int(g["hs"]), "as": int(g["as"]),
                            # 월별 페이지는 승/패 투수만 줍니다. NPB 승패 모델은 선발을 안 쓰고,
                            # 총점 모델은 선발이 없으면 팀 평균 실점으로 대체합니다.
                            "hsp": "(unknown)", "asp": "(unknown)"})
    return sorted(out, key=lambda x: x["date"])


def fresh(lg):
    return {"league": lg, "last_date": "1900-01-01", "elo": {}, "form": {}, "sp": {},
            "rs": {}, "ra": {}, "park": {}, "lgtot": []}


def rebuild(lgs, since, track_since, now):
    """공개 기록만으로 상태를 다시 만들고, 그 과정에서 성적표까지 재현한다."""
    y0, y1 = since.year, now.year
    hist = []
    if "MLB" in lgs:
        hist += hist_mlb(y0, y1)
    if "KBO" in lgs:
        hist += hist_kbo(since, now.date())
    if "NPB" in lgs:
        hist += hist_npb(y0, y1)
    hist = [g for g in hist if g["date"] >= since.strftime("%Y-%m-%d")]
    hist.sort(key=lambda g: (g["date"], g["lg"], g["home"]))

    state = {lg: fresh(lg) for lg in lgs}
    ts = track_since.strftime("%Y-%m-%d")
    track = []
    for g in hist:
        st = state[g["lg"]]
        if g["date"] >= ts and g["hs"] != g["as"]:
            # 결과를 보기 '전'의 상태로 예측합니다 (누수 없음)
            p, _ = predict(st, g["home"], g["away"], g["hsp"], g["asp"], USE_SP[g["lg"]])
            pick = g["home"] if p >= .5 else g["away"]
            actual = g["home"] if g["hs"] > g["as"] else g["away"]
            # 자리를 아끼려고 actual 은 안 넣습니다 -- pick 과 hit 로 되살릴 수 있습니다.
            rec = {"date": g["date"], "lg": g["lg"], "away": g["away"], "home": g["home"],
                   "pick": pick, "pHome": round(p, 3), "hit": int(pick == actual),
                   "score": f"{g['as']}-{g['hs']}"}
            t = predict_total(st, g["home"], g["away"], g["hsp"], g["asp"])
            if t:
                po = t["pOver"]["7.5"]
                tot = g["hs"] + g["as"]
                rec.update({"ouPick": "오버" if po >= .5 else "언더", "ouP": round(po, 3),
                            "ouHit": int((po >= .5) == (tot > 7.5)),
                            "total": tot, "totalPred": t["total"]})
            track.append(rec)
        bump(st, g["home"], g["away"], g["hs"], g["as"], g["hsp"], g["asp"])
        st["last_date"] = max(st["last_date"], g["date"])
    return state, track, len(hist)


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    lgs = ["MLB", "KBO", "NPB"]
    for f in flags:
        if f.startswith("--lg="):
            lgs = [x.strip().upper() for x in f[5:].split(",") if x.strip()]
    no_settle = "--no-settle" in flags
    # --settle-only: 그날 경기 결과만 마감합니다. 오늘 일정을 새로 안 받아오므로
    # 화면의 경기 목록을 건드리지 않습니다(패처도 --settle-only 로 받으세요).
    settle_only = "--settle-only" in flags
    clv_path = None
    do_rebuild = "--rebuild" in flags
    since = datetime.strptime(next((f[8:] for f in flags if f.startswith("--since=")),
                                   "2025-03-01"), "%Y-%m-%d")
    track_since = datetime.strptime(
        next((f[14:] for f in flags if f.startswith("--track-since=")), "2026-04-01"), "%Y-%m-%d")
    track = []
    for f in flags:
        if f.startswith("--clv="):
            clv_path = f[6:]
        if f.startswith("--track="):
            try:
                _t = json.loads(open(f[8:], encoding="utf-8").read())
                # 성적표 블록은 2026-09-12부터 gzip+base64 로 실립니다.
                # 옛 형식(순수 배열)도 그대로 받습니다.
                if isinstance(_t, dict) and _t.get("gz"):
                    import base64 as _b64, gzip as _gz
                    _t = json.loads(_gz.decompress(_b64.b64decode(_t["gz"])).decode())
                track = _t if isinstance(_t, list) else []
            except Exception:
                track = []

    rebuilt_track, n_hist = None, 0
    if do_rebuild:
        state, rebuilt_track, n_hist = rebuild(lgs, since, track_since, datetime.now(KST))
        no_settle = True                # 재구성이 정산을 대신합니다
    else:
        state = json.loads(gzip.decompress(base64.b64decode(open(argv[0]).read())))
    prev = []
    if len(argv) > 2:
        try:
            prev = json.loads(open(argv[2], encoding="utf-8").read())
        except Exception:
            prev = []
    prev = [g for g in prev if g.get("lg") in lgs]
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    games = []

    # ---------- MLB ----------
    if "MLB" in lgs:
        st = state["MLB"]
        last = st["last_date"]
        res = mlb_results((datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"),
                          now.strftime("%Y-%m-%d"))
        for g in sorted(res, key=lambda x: x["date"]):
            bump(st, g["home"], g["away"], g["hs"], g["as"],
                 g["hsp"] or "(unknown)", g["asp"] or "(unknown)")
            st["last_date"] = g["date"]
        if not settle_only:
            us_date = (now - timedelta(hours=13)).strftime("%Y-%m-%d")   # 미국 기준 오늘
            odds = espn_odds("baseball", "mlb", us_date.replace("-", ""))
            b = fetch(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={us_date}"
                      f"&hydrate=probablePitcher,lineups&gameType=R", timeout=60)
            if b:
                for dt in json.loads(b).get("dates", []):
                    for g in dt.get("games", []):
                        if g["status"]["detailedState"] in ("Final", "Game Over"):
                            continue
                        t = g["teams"]
                        hsp = (t["home"].get("probablePitcher") or {}).get("fullName", "")
                        asp = (t["away"].get("probablePitcher") or {}).get("fullName", "")
                        lu = g.get("lineups") or {}
                        hn, an = t["home"]["team"]["name"], t["away"]["team"]["name"]
                        if hn not in MLB_CODE or an not in MLB_CODE:
                            continue
                        start = datetime.fromisoformat(
                            g["gameDate"].replace("Z", "+00:00")).astimezone(KST)
                        p, ev = predict(st, MLB_CODE[hn], MLB_CODE[an], hsp, asp, True)
                        games.append({"lg": "MLB", "homeFull": hn, "awayFull": an,
                                      "hKey": MLB_CODE[hn], "aKey": MLB_CODE[an],
                                      "date": start.strftime("%Y-%m-%d"),
                                      "kst": start.strftime("%H:%M"),
                                      "minsToStart": int((start - now).total_seconds() // 60),
                                      "started": start <= now,
                                      "kspH": hsp, "kspA": asp, "pHome": round(p, 4),
                                      "lineupH": len(lu.get("homePlayers") or []),
                                      "lineupA": len(lu.get("awayPlayers") or []), **ev})
                        # 시작 4시간 안쪽이고 아직 안 시작한 경기만 타순을 받아옵니다.
                        # (그 전에는 어차피 안 나오고, 경기당 1회 호출이라 낭비를 막습니다)
                        ms = games[-1]["minsToStart"]
                        if not games[-1]["started"] and -30 <= ms <= 240:
                            lh, la = mlb_lineup(g.get("gamePk"))
                            if lh or la:
                                games[-1]["luH"], games[-1]["luA"] = lh, la
                        o = odds.get(f"{MLB_CODE[an]}@{MLB_CODE[hn]}")
                        if o:
                            games[-1].update({k: v for k, v in o.items() if v is not None})

    # ---------- KBO ----------
    if "KBO" in lgs:
        st = state["KBO"]
        ksched = [] if settle_only else kbo_day(today.replace("-", ""))
        # 경기가 없는 날엔 배당을 아예 안 부릅니다 -- 빈 응답에도 크레딧이 나갑니다.
        kodds, kleft = kn_odds("KBO", today) if ksched else ({}, None)
        for g in ksched:
            a, h = KBO_ALIAS.get(strip(g["AWAY_NM"]), strip(g["AWAY_NM"])), \
                   KBO_ALIAS.get(strip(g["HOME_NM"]), strip(g["HOME_NM"]))
            cancel = strip(g.get("CANCEL_SC_NM") or "")
            hsp, asp = strip(g.get("B_PIT_P_NM")), strip(g.get("T_PIT_P_NM"))
            p, ev = predict(st, h, a, hsp, asp, True)
            games.append({"lg": "KBO", "homeFull": h, "awayFull": a, "hKey": h, "aKey": a,
                          "date": today,
                          "kst": g.get("G_TM", ""),
                          "venue": strip(g.get("S_NM")), "kspH": hsp, "kspA": asp,
                          "pHome": round(p, 4), "lineup": int(g.get("LINEUP_CK") or 0),
                          "cancelled": cancel if cancel and cancel != "정상경기" else "", **ev})
            o = kodds.get(f"{a}@{h}")
            if o:
                games[-1].update(o)

    # ---------- NPB ----------
    if "NPB" in lgs:
        st = state["NPB"]
        nsched = [] if settle_only else [g for g in npb_month(now.year, now.month)
                  if g["date"] == today and not g["hs"].isdigit()]
        nodds, nleft = kn_odds("NPB", today) if nsched else ({}, None)
        for g in nsched:
            p, ev = predict(st, g["home"], g["away"], g["hsp"], g["asp"], False)
            games.append({"lg": "NPB", "homeFull": g["home"], "awayFull": g["away"],
                          "hKey": g["home"], "aKey": g["away"],
                          "date": g["date"],
                          "kst": g.get("time", ""), "kspH": g["hsp"], "kspA": g["asp"],
                          "pHome": round(p, 4), **ev})
            t = predict_total(st, g["home"], g["away"], g["hsp"], g["asp"])
            if t:
                games[-1].update(t)
            o = nodds.get(f"{g['away']}@{g['home']}")
            if o:
                games[-1].update(o)

    for tag, left in (("KBO", locals().get("kleft")), ("NPB", locals().get("nleft"))):
        if left:
            print(f"  [{tag}] 배당 수집 완료 · 이번 달 남은 크레딧 {left}")
    done = [] if (no_settle and not settle_only) else settle(prev, now, track)
    clv = {"open": {}, "settled": []}
    if clv_path:
        try:
            clv = json.loads(open(clv_path, encoding="utf-8").read()) or clv
        except Exception:
            pass
        clv_update(clv, games, lgs, now, done)
        nopen = sum(1 for e in clv["open"].values() if e.get("lg") in lgs)
        print(f"  [CLV] 추적 중 {nopen}건 · 누적 마감 {len(clv['settled'])}건")
    out = {"generated_kst": now.strftime("%Y-%m-%d %H:%M"), "leagues": lgs, "games": games,
           "settled": done,
           **({"clv": clv} if clv_path else {}),
           **({"track": rebuilt_track, "trackMode": "replace", "nHist": n_hist}
              if rebuilt_track is not None else {}),
           "state": base64.b64encode(gzip.compress(
               json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode())).decode()}
    open(argv[1], "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False))
    if rebuilt_track is not None:
        hh = sum(t["hit"] for t in rebuilt_track)
        print(f"  재구성: 과거 {n_hist:,}경기로 상태를 다시 만들고 "
              f"{len(rebuilt_track):,}경기 성적표를 재현 → {hh}/{len(rebuilt_track)} "
              f"({hh/max(len(rebuilt_track),1)*100:.1f}%)")
    print(f"{now:%Y-%m-%d %H:%M} KST · 대상 {'/'.join(lgs)} · 경기 {len(games)}건 "
          f"(MLB {sum(1 for g in games if g['lg']=='MLB')} · "
          f"KBO {sum(1 for g in games if g['lg']=='KBO')} · "
          f"NPB {sum(1 for g in games if g['lg']=='NPB')})")
    if done:
        hits = sum(d["hit"] for d in done)
        print(f"  이전 픽 정산: {hits}/{len(done)} 적중 ({hits/len(done)*100:.0f}%)")
        for d in done:
            print(f"    [{d['lg']}] {d['away']} @ {d['home']} {d['score']} "
                  f"· 픽 {d['pick']} → {'적중' if d['hit'] else '실패'}")
    for g in games:
        pick = g["homeFull"] if g["pHome"] >= .5 else g["awayFull"]
        cx = f" [{g['cancelled']}]" if g.get("cancelled") else ""
        if g["lg"] == "MLB":
            lu = "라인업✓" if min(g["lineupH"], g["lineupA"]) >= 9 else "라인업-"
            od = (f", 배당 {g['oa']}/{g['oh']}, 총점 {g.get('total')}"
                  if g.get("oh") else ", 배당-")
            cx += f" ({lu}, 시작 {g['minsToStart']:+d}분{od})"
        elif g["lg"] == "KBO" and g.get("lineup"):
            cx += " (라인업✓)"
        if g.get("total") is not None and g["lg"] == "NPB":
            po = g["pOver"]
            cx += (f" · 예상총점 {g['total']} "
                   f"[6.5오버 {po['6.5']*100:.0f}% · 7.5 {po['7.5']*100:.0f}% "
                   f"· 8.5 {po['8.5']*100:.0f}%]")
        print(f"  [{g['lg']}] {g.get('kst',''):>5} {g['awayFull'][:14]:<14} @ "
              f"{g['homeFull'][:14]:<14} 홈승 {g['pHome']*100:5.1f}% → {pick}{cx}")


if __name__ == "__main__":
    main()






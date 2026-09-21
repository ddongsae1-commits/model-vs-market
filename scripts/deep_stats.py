# -*- coding: utf-8 -*-
"""MLB 심층 스탯 수집기 (deepPy)

경기 카드 밑에 펼쳐 보여줄 재료를 모읍니다. 전부 MLB 공개 API(statsapi)라 크레딧 안 씁니다.
하루 한 번(자정 런)만 돌리세요 -- 시즌 누적 기록이라 몇 시간 단위로 안 변합니다.

    python3 deep_stats.py out.json deep.json [--season=2026]

out.json 의 MLB 경기에서 팀 코드와 선발 이름을 읽어, 필요한 팀만 받아옵니다.
결과는 팀 코드로 묶습니다:
    {"asOf": "...", "season": 2026,
     "teams": {"NYA": {"pen":[...], "bats":[...], "split":{...}}, ...},
     "sp":    {"Will Warren": {"era":..., "whip":..., ...}, ...}}

주의: 표본이 적은 선발(ip 가 작은 경우)은 숫자가 있어도 의미가 없습니다. 화면에서
      경고를 띄우려고 ip·gs 를 반드시 같이 담습니다. 지우지 마세요.
"""
import json, sys, time, urllib.request, datetime

TEAM_ID = {
    108:"ANA",109:"ARI",144:"ATL",110:"BAL",111:"BOS",112:"CHN",113:"CIN",114:"CLE",
    115:"COL",116:"DET",117:"HOU",118:"KCA",119:"LAN",120:"WAS",121:"NYN",133:"OAK",
    134:"PIT",135:"SDN",136:"SEA",137:"SFN",138:"SLN",139:"TBA",140:"TEX",141:"TOR",
    142:"MIN",143:"PHI",145:"CHA",146:"MIA",147:"NYA",158:"MIL",
}
ID_OF = {v: k for k, v in TEAM_ID.items()}


def jget(u, tries=3):
    for i in range(tries):
        try:
            r = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(r, timeout=60) as f:
                return json.loads(f.read().decode())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def ipf(x):
    """'148.2' 같은 이닝 표기를 소수로. .1=1아웃, .2=2아웃."""
    try:
        s = str(x)
        w, _, frac = s.partition(".")
        return int(w) + {"1": 1/3, "2": 2/3}.get(frac, 0.0)
    except Exception:
        return 0.0


def pitcher(pid, season):
    j = jget(f"https://statsapi.mlb.com/api/v1/people/{pid}"
             f"?hydrate=stats(group=[pitching],type=[season],season={season})")
    try:
        p = j["people"][0]; s = p["stats"][0]["splits"][0]["stat"]
    except Exception:
        return None
    return {"id": p.get("id"), "name": p.get("fullName"), "era": s.get("era"), "whip": s.get("whip"),
            "baa": s.get("avg"), "ip": s.get("inningsPitched"), "ipf": round(ipf(s.get("inningsPitched")), 1),
            "gs": int(s.get("gamesStarted") or 0), "k9": s.get("strikeoutsPer9Inn"),
            "bb9": s.get("walksPer9Inn"), "hr": int(s.get("homeRuns") or 0)}


def team_block(tid, season):
    out = {}
    # --- 불펜: 선발 비중 30% 미만 + 15이닝 이상
    j = jget(f"https://statsapi.mlb.com/api/v1/teams/{tid}/roster?rosterType=active"
             f"&hydrate=person(stats(group=[pitching],type=[season],season={season}))")
    pen = []
    for p in j.get("roster", []):
        if (p.get("position") or {}).get("abbreviation") != "P":
            continue
        try: s = p["person"]["stats"][0]["splits"][0]["stat"]
        except Exception: continue
        gp, gs = int(s.get("gamesPlayed") or 0), int(s.get("gamesStarted") or 0)
        ip = ipf(s.get("inningsPitched"))
        if gp and gs / gp < 0.3 and ip >= 15:
            pen.append({"n": p["person"]["fullName"], "era": float(s.get("era") or 99),
                        "ip": s.get("inningsPitched"), "whip": s.get("whip")})
    pen.sort(key=lambda x: x["era"])
    out["pen"] = pen[:6]
    out["penEra"] = round(sum(x["era"] for x in out["pen"]) / len(out["pen"]), 2) if out["pen"] else None

    # --- 주요 타자: 200타수 이상, OPS 순
    j = jget(f"https://statsapi.mlb.com/api/v1/teams/{tid}/roster?rosterType=active"
             f"&hydrate=person(stats(group=[hitting],type=[season],season={season}))")
    bats = []
    for p in j.get("roster", []):
        if (p.get("position") or {}).get("abbreviation") == "P":
            continue
        try: s = p["person"]["stats"][0]["splits"][0]["stat"]
        except Exception: continue
        if int(s.get("atBats") or 0) < 200:
            continue
        bats.append({"n": p["person"]["fullName"], "avg": s.get("avg"), "ops": s.get("ops"),
                     "hr": int(s.get("homeRuns") or 0), "rbi": int(s.get("rbi") or 0),
                     "_o": float(s.get("ops") or 0)})
    bats.sort(key=lambda x: -x["_o"])
    for b in bats: b.pop("_o", None)
    out["bats"] = bats[:5]
    # 라인업에 붙일 용도로 전원을 선수 id 로 색인합니다(타수 제한 없음).
    allb = {}
    for p in j.get("roster", []):
        if (p.get("position") or {}).get("abbreviation") == "P":
            continue
        try: st2 = p["person"]["stats"][0]["splits"][0]["stat"]
        except Exception: continue
        allb[str(p["person"]["id"])] = {"n": p["person"]["fullName"],
                                        "avg": st2.get("avg"), "ops": st2.get("ops"),
                                        "hr": int(st2.get("homeRuns") or 0),
                                        "g": int(st2.get("gamesPlayed") or 0)}
    out["all"] = allb

    # --- 홈/원정 타격 스플릿
    j = jget(f"https://statsapi.mlb.com/api/v1/teams/{tid}/stats"
             f"?stats=statSplits&sitCodes=h,a&group=hitting&season={season}")
    sp = {}
    for st in j.get("stats", []):
        for s in st.get("splits", []):
            code = (s.get("split") or {}).get("code"); v = s["stat"]
            gp = int(v.get("gamesPlayed") or 0)
            if gp and code in ("h", "a"):
                sp[code] = {"rpg": round(int(v.get("runs") or 0) / gp, 2),
                            "avg": v.get("avg"), "ops": v.get("ops"), "g": gp}
    out["split"] = sp
    return out


def main():
    a = sys.argv[1:]
    if len(a) < 2:
        raise SystemExit("사용법: python3 deep_stats.py out.json deep.json [--season=YYYY]")
    src, dst = a[0], a[1]
    season = next((int(f[9:]) for f in a if f.startswith("--season=")),
                  datetime.datetime.utcnow().year)
    raw = json.load(open(src, encoding="utf-8"))
    # out.json({"games":[...]}) 도 되고, 아티팩트의 gamesData(그냥 배열) 도 됩니다.
    games = [g for g in (raw["games"] if isinstance(raw, dict) else raw) if g.get("lg") == "MLB"]
    codes, sps = set(), {}
    for g in games:
        for k in ("hKey", "aKey"):
            if g.get(k) in ID_OF:
                codes.add(g[k])
    print(f"대상 팀 {len(codes)}개 · 시즌 {season}")

    teams = {}
    for c in sorted(codes):
        try:
            teams[c] = team_block(ID_OF[c], season)
            print(f"  {c} 불펜 {len(teams[c]['pen'])}명 · 타자 {len(teams[c]['bats'])}명", flush=True)
        except Exception as e:
            print(f"  {c} 실패: {e}", flush=True)

    # --- 선발: 오늘 예고된 투수만. 이름으로 찾습니다(경기 데이터가 이름만 갖고 있어서).
    want = {}
    for g in games:
        for nk in ("kspH", "kspA"):
            if g.get(nk):
                want[g[nk]] = None
    if want:
        d = datetime.datetime.utcnow() - datetime.timedelta(hours=4)
        sch = jget(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1"
                   f"&date={d:%Y-%m-%d}&hydrate=probablePitcher&gameType=R")
        ids = {}
        for day in sch.get("dates", []):
            for gg in day.get("games", []):
                for side in ("home", "away"):
                    pp = (gg["teams"][side].get("probablePitcher") or {})
                    if pp.get("fullName"):
                        ids[pp["fullName"]] = pp["id"]
        for nm in want:
            pid = ids.get(nm)
            if not pid:
                continue
            try:
                st = pitcher(pid, season)
                if st: sps[nm] = st
            except Exception:
                pass
        print(f"선발 기록 {len(sps)}명 확보 (예고 {len(want)}명)")

    # 좌/우 타석·투구. 한 번 부르면 리그 전체가 옵니다 -- 라인업 매치업 보는 데 씁니다.
    hand = {}
    try:
        pl = jget(f"https://statsapi.mlb.com/api/v1/sports/1/players?season={season}")
        for p in pl.get("people", []):
            hand[str(p["id"])] = ((p.get("batSide") or {}).get("code", "") +
                                  (p.get("pitchHand") or {}).get("code", ""))
        print(f"좌우 정보 {len(hand)}명")
    except Exception as e:
        print(f"좌우 정보 실패: {e}")

    json.dump({"asOf": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
               "season": season, "teams": teams, "sp": sps, "hand": hand},
              open(dst, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"저장 완료 → {dst}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""updater 가 뱉은 out.json 을 data/ 아래 저장소에 반영한다.

    python3 scripts/patch.py <out.json> [--merge-lg|--refresh|--settle-only|--clv-only]

예전(아티팩트 시절) patch_artifact.py 와 로직이 같습니다. 바뀐 건 저장 위치뿐입니다:
HTML 안 <script> 블록 → data/*.json. 병합 규칙·라이브배당 방지·중복 거르기는
그때 고생해서 잡은 것들이라 한 줄도 안 건드렸습니다.

--merge-lg   : out.json 의 leagues 에 없는 리그의 기존 경기는 건드리지 않는다.
               (MLB 런과 KBO/NPB 런이 서로를 지우지 않게 하는 장치)
--refresh    : 이미 올라간 경기의 배당·선발·라인업만 갱신. 아무것도 지우지 않는다.
--settle-only: 결과 마감만. 경기 목록과 날짜배지는 그대로 둔다.
--clv-only   : 배당 스냅샷만. clvLog 만 갈아끼운다.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import store

import base64, gzip, json, re, sys
from datetime import datetime, timedelta, timezone


# 성적표(trackRecord)는 3,300경기가 넘어 483KB였습니다. 아티팩트 전체의 62%였고,
# 예약 런이 매번 통째로 읽는 구조라 런이 죽는 원인이 됐습니다(2026-09-11~12).
# gzip+base64 로 실어 52KB 로 줄였습니다. 옛 형식(순수 배열)도 그대로 읽습니다.
def track_load(text):
    v = json.loads(text)
    if isinstance(v, list):
        return v
    if isinstance(v, dict) and v.get("gz"):
        return json.loads(gzip.decompress(base64.b64decode(v["gz"])).decode())
    return []


def track_dump(rows):
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    return json.dumps({"gz": base64.b64encode(gzip.compress(raw, mtime=0)).decode()})

KST = timezone(timedelta(hours=9))
ORDER = {"MLB": 0, "KBO": 1, "NPB": 2}

KO = {"Los Angeles Angels":"에인절스","Arizona Diamondbacks":"애리조나","Atlanta Braves":"애틀랜타",
 "Baltimore Orioles":"볼티모어","Boston Red Sox":"보스턴","Chicago White Sox":"화이트삭스",
 "Chicago Cubs":"컵스","Cincinnati Reds":"신시내티","Cleveland Guardians":"클리블랜드",
 "Colorado Rockies":"콜로라도","Detroit Tigers":"디트로이트","Houston Astros":"휴스턴",
 "Kansas City Royals":"캔자스시티","Los Angeles Dodgers":"다저스","Miami Marlins":"마이애미",
 "Milwaukee Brewers":"밀워키","Minnesota Twins":"미네소타","New York Yankees":"양키스",
 "New York Mets":"메츠","Oakland Athletics":"애슬레틱스","Athletics":"애슬레틱스",
 "Philadelphia Phillies":"필라델피아","Pittsburgh Pirates":"피츠버그","San Diego Padres":"샌디에이고",
 "Seattle Mariners":"시애틀","San Francisco Giants":"샌프란시스코","St. Louis Cardinals":"세인트루이스",
 "Tampa Bay Rays":"탬파베이","Texas Rangers":"텍사스","Toronto Blue Jays":"토론토",
 "Washington Nationals":"워싱턴",
 "巨人":"요미우리","阪神":"한신","中日":"주니치","DeNA":"DeNA","広島":"히로시마","ヤクルト":"야쿠르트",
 "ソフトバンク":"소프트뱅크","日本ハム":"니혼햄","ロッテ":"지바롯데","楽天":"라쿠텐","西武":"세이부",
 "オリックス":"오릭스"}


def conv(g):
    ko = lambda n: KO.get(n, n)
    d = {"lg": g["lg"], "id": f'{g["aKey"]}@{g["hKey"]}',
         "away": g["aKey"], "home": g["hKey"],
         "awayFull": ko(g["awayFull"]), "homeFull": ko(g["homeFull"]),
         "hKey": g["hKey"], "aKey": g["aKey"],
         "pHome": g["pHome"], "elo": g.get("elo_diff", 0.0),
         "status": g.get("cancelled") or g.get("kst", "")}
    if g.get("date"):
        d["date"] = g["date"]
    if g.get("venue"):
        d["venue"] = g["venue"]
    if g.get("cancelled"):
        d["cancelled"] = g["cancelled"]
    d["kspA"], d["kspH"] = g.get("kspA", ""), g.get("kspH", "")
    if g["lg"] in ("MLB", "KBO"):
        d["aRa"], d["hRa"] = g.get("a_sp_ra"), g.get("h_sp_ra")
        d["aN"], d["hN"] = g.get("a_sp_n", 0), g.get("h_sp_n", 0)
        d["spUsed"] = d["aRa"] is not None and d["hRa"] is not None
    for k in ("oh", "oa", "book", "mktTotal"):
        if g.get(k) is not None:
            d[k] = g[k]
    if g.get("total") is not None:
        d["total"] = g["total"]          # MLB는 시장 기준선, NPB는 모델 예상 총점
        d["over"], d["under"] = g.get("over"), g.get("under")
        if g.get("pOver"):
            d["pOver"] = g["pOver"]      # 기준선별 오버 확률 (NPB)
            d["sig"] = g.get("sig")      # 잔차 표준편차 -- 임의 기준선 확률을 브라우저에서 계산
            d["totalSp"] = g.get("totalSp", False)
    if g["lg"] == "MLB":
        d["lineup"] = 1 if min(g.get("lineupH", 0), g.get("lineupA", 0)) >= 9 else 0
        # 확정 타순. 경기 직전에만 붙습니다. 없으면 그냥 안 넣습니다.
        for k in ("luH", "luA"):
            if g.get(k):
                d[k] = g[k]
        d["started"] = bool(g.get("started"))
    elif g["lg"] == "KBO":
        d["lineup"] = g.get("lineup", 0)
    return d




def already_started(g, now):
    """경기가 이미 시작했나. status 는 'HH:MM' 형식(KST)입니다.
       시작한 경기의 배당은 라이브 배당이라 프리매치 비교에 쓸 수 없습니다."""
    if g.get("started") is True:
        return True
    st, dt = g.get("status") or "", g.get("date") or ""
    if len(st) != 5 or st[2] != ":" or len(dt) != 10:
        return False
    try:
        y, mo, d = int(dt[:4]), int(dt[5:7]), int(dt[8:10])
        hh, mi = int(st[:2]), int(st[3:])
    except ValueError:
        return False
    start = datetime(y, mo, d, hh, mi)
    return now.replace(tzinfo=None) >= start

def kst_now():
    return datetime.now(timezone.utc) + timedelta(hours=9)


def stamp_run(label, now):
    """'마지막 작업' 배지 문구를 저장합니다. 날짜배지를 안 건드리는 모드(결과 마감·
       배당 스냅샷)도 이건 찍어서, 페이지만 봐도 작업이 돌았는지 알 수 있게 합니다."""
    c = store.read_chips()
    c["runChip"] = f'마지막 작업 {now:%m-%d %H:%M} · {label}'
    store.write_chips(c)


def stamp_date(chip):
    c = store.read_chips()
    c["dateChip"] = chip
    store.write_chips(c)


def main():
    out_path = sys.argv[1]
    args = sys.argv[2:]
    merge = "--merge-lg" in args
    clv_only = "--clv-only" in args
    settle_only = "--settle-only" in args
    refresh = "--refresh" in args

    out = json.loads(open(out_path, encoding="utf-8").read())

    def put(bid, text):
        store.write_text(bid, text)

    if clv_only:
        if out.get("clv") is None:
            raise SystemExit("--clv-only 인데 out.json 에 clv 가 없습니다 (--clv= 를 빼먹었나요)")
        put("clvLog", json.dumps(out["clv"], ensure_ascii=False))
        stamp_run("배당 스냅샷", kst_now())
        nopen = len(out["clv"].get("open") or {})
        print(f"배당 스냅샷만 반영 · 추적 중 {nopen}건 · "
              f"누적 마감 {len(out['clv'].get('settled') or [])}건")
        return

    if settle_only:
        track = track_load(store.read_text("trackRecord", "[]"))
        have = {t.get("key") for t in track}
        added = 0
        for t in out.get("settled", []):
            if t.get("key") not in have:
                track.append(t); have.add(t.get("key")); added += 1
        track.sort(key=lambda t: t.get("date", ""))
        put("trackRecord", track_dump(track))
        put("modelState", json.dumps({"b64": out["state"]}))
        if out.get("clv") is not None:
            put("clvLog", json.dumps(out["clv"], ensure_ascii=False))
        stamp_run(f"결과 마감 +{added}", kst_now())
        print(f"결과 마감만 반영 · 정산 +{added} → 누적 {len(track)}건 "
              f"(경기 목록·날짜배지는 그대로)")
        return

    if refresh:
        cur = json.loads(store.read_text("gamesData", "[]"))
        # 날짜까지 키에 넣습니다. 야구는 같은 카드가 며칠 이어지는 시리즈라
        # id 만으로 짝지으면 KST 13시 이후 실행 때 내일 경기가 오늘 자리를 덮어씁니다.
        by = {}
        for i, g in enumerate(cur):
            by[(g.get("lg"), g.get("id"), g.get("date"))] = i
        upd = skip = live = 0
        _now = kst_now()
        for g in out["games"]:
            d = conv(g)
            k = (d["lg"], d["id"], d.get("date"))
            if k in by:
                old_g = cur[by[k]]
                if already_started(old_g, _now):
                    # 이미 시작한 경기입니다. 지금 받은 배당은 스코어가 반영된 라이브
                    # 배당이라 '시장이 경기 전에 뭐라고 했나'와 전혀 다릅니다.
                    live += 1
                    continue
                cur[by[k]] = d; upd += 1
            else:
                skip += 1          # 목록에 없는 경기는 이 모드에서 추가하지 않습니다
        put("gamesData", json.dumps(cur, ensure_ascii=False))
        put("modelState", json.dumps({"b64": out["state"]}))
        if out.get("clv") is not None:
            put("clvLog", json.dumps(out["clv"], ensure_ascii=False))
        stamp_run(f"배당·선발 새로고침 {upd}건", kst_now())
        print(f"배당·선발만 새로고침 · 갱신 {upd}건"
              + (f" · 목록에 없어 건너뜀 {skip}건" if skip else "")
              + (f" · 이미 시작해 라이브 배당이라 건드리지 않음 {live}건" if live else "")
              + f" · 총 {len(cur)}건 (지운 것 없음, 성적표·날짜배지 그대로)")
        return

    new = [conv(g) for g in out["games"]]
    if merge:
        lgs = set(out.get("leagues") or sorted({g["lg"] for g in out["games"]}))
        today_kst = datetime.now(KST).strftime("%Y-%m-%d")
        # 다른 리그(이번 런에 안 낀 리그)의 기존 경기는 보존하되, date 필드가 있고
        # 그게 오늘이 아니면(=아직 오늘자로 안 갱신된 어제 잔여물) 버립니다.
        new = [g for g in json.loads(store.read_text("gamesData", "[]"))
               if g["lg"] not in lgs and (not g.get("date") or g["date"] == today_kst)] + new
    new.sort(key=lambda g: (ORDER.get(g["lg"], 9), g.get("status", "")))

    if out.get("trackMode") == "replace" and out.get("track") is not None:
        # 재구성 모드: 성적표를 통째로 갈아끼웁니다. 과거를 다시 계산해서 만든 것이라
        # 이어붙이면 중복이 됩니다. 고른 리그 것만 교체하고 나머지는 둡니다.
        lgs = set(out.get("leagues") or [])
        keep = [t for t in track_load(store.read_text("trackRecord", "[]"))
                if t.get("lg") not in lgs]
        track = keep + out["track"]
        added = len(out["track"])
    else:
        track = track_load(store.read_text("trackRecord", "[]"))
        have = {t.get("key") for t in track}
        added = 0
        for s in out.get("settled", []):
            if s.get("key") not in have:
                track.append(s); have.add(s.get("key")); added += 1
    track.sort(key=lambda t: t.get("date", ""))

    put("gamesData", json.dumps(new, ensure_ascii=False))
    put("trackRecord", track_dump(track))
    put("modelState", json.dumps({"b64": out["state"]}))
    if out.get("clv") is not None:
        put("clvLog", json.dumps(out["clv"], ensure_ascii=False))

    cnt = {}
    for g in new:
        cnt[g["lg"]] = cnt.get(g["lg"], 0) + 1
    now = datetime.now(KST)
    chip = (f'{now:%Y-%m-%d} (KST) · '
            + ' · '.join(f'{k} {v}' for k, v in sorted(cnt.items(),
                                                       key=lambda x: ORDER.get(x[0], 9)))
            + f' · 갱신 {now:%H:%M}')
    stamp_date(chip)
    stamp_run("픽 생성", now)
    print(f"패치 완료 · 경기 {len(new)}건 (새로 {len(out['games'])}건) · "
          f"정산 +{added} → 누적 {len(track)}건 · {chip}")


if __name__ == "__main__":
    main()

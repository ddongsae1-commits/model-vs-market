# -*- coding: utf-8 -*-
"""15분마다 깨어나서 "지금 할 일이 있나"를 보고, 있으면 그것만 한다.

예전에는 고정 시간표였습니다(00:30, 12:30, 17:30 …). 문제가 둘이었습니다.

  1) 시간표가 경기 시각을 모릅니다. NPB 17:00 경기를 17:30 런이 찍으면 이미 늦습니다.
     라이브 배당만 잡히고 프리매치 스냅샷은 0개가 되는 경기가 실제로 있었습니다.
  2) 깃허브 예약은 5~30분씩 밀립니다. 고정 시각에 매달리면 밀린 스냅샷은 그냥
     날아갑니다. 나중에 만회할 방법이 없습니다.

그래서 시간표를 버리고 둘로 나눴습니다.

  [앵커]   하루 한 번 꼭 해야 하는 무거운 일(목록 만들기·결과 마감·축구·심층스탯).
           "그 시각 이후, 오늘 아직 안 했으면 한다" 방식이라 밀려도 반드시 돕니다.
  [스냅샷] 배당·선발·라인업 새로고침. 시간표가 아니라 **경기 시작 시각**을 보고
           따라갑니다. 곧 시작하는 경기가 있으면 찍고, 없으면 아무것도 안 합니다.

스냅샷이 촘촘해지면 CLV(픽 시점 가격이 그 뒤 움직임보다 유리했나)가 제대로
측정됩니다. 지금 '못 잰' 44건이 전부 스냅샷을 한 번밖에 못 찍은 경기입니다.

    python3 scripts/run.py          # 지금 할 일을 판단해서 실행 (예약이 쓰는 방식)
    python3 scripts/run.py A        # 특정 작업 강제 실행 (손으로 돌릴 때)
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
S = ROOT / "scripts"
TMP = ROOT / ".tmp"
KST = timezone(timedelta(hours=9))

# ── 앵커: 하루 한 번씩 꼭 해야 하는 무거운 일 ──────────────────────────────
# (코드, 설명, 이 시각 이후부터 가능). 오늘 아직 안 했으면 실행합니다.
# 밀려도 다음 15분 체크가 잡아 주므로 "못 하고 지나가는" 일이 없습니다.
# 마지막 칸은 "이 시각을 넘기면 오늘은 포기한다"입니다. 늦게 하느니 안 하는 게
# 나은 일이 있어서입니다 -- 특히 A: 한국시간 13시면 미국 날짜가 넘어가서, 그 뒤에
# MLB 목록을 새로 만들면 오늘 경기 자리에 내일 경기가 들어앉습니다.
ANCHORS = [
    #  코드  설명                                    가능 시각(KST)
    ("A", "MLB 오늘 경기 + 어제 정산 + 심층 스탯",      0, 11),
    ("E", "축구 5대리그",                             9, 23),
    ("F", "KBO·NPB 오늘 경기 + 어제 정산",            12, 23),
    ("G", "MLB 결과 마감",                           15, 23),
    ("I", "KBO·NPB 결과 마감",                       23, 23),
]

# ── 스냅샷: 경기 시작 시각을 보고 따라가는 배당·선발 새로고침 ──────────────
# lead        경기 시작 몇 분 전부터 챙기나
# cool        다시 찍기까지 최소 간격(분)
# perCluster  같은 시각에 몰린 경기 묶음 하나당 최대 몇 번
# perDay      하루 전체 상한(안전장치). None 이면 무제한.
#
# MLB 는 ESPN 공개 배당이라 공짜입니다. 그래서 넉넉히 — 3시간 전부터 40분 간격.
# 라인업이 보통 2~3시간 전에 뜨는데 이 창이 그것까지 같이 잡아 줍니다.
# KBO·NPB 는 The Odds API 유료 크레딧(무료 월 500)을 씁니다. 한 실행에 리그당
# 1크레딧씩 2크레딧이므로 하루 6회면 12크레딧 = 한 달 약 360. 한도 안쪽입니다.
#
# 묶음별 상한을 따로 두는 이유: 하루 상한만 두면 이른 경기(일야 14:00)가 그날 몫을
# 다 써 버려서 정작 저녁 경기(국야 18:30)는 한 번도 못 찍습니다.
SNAP_UNITS = [
    #  unit    리그          lead  cool  perCluster  perDay
    ("MLB",   "MLB",          180,   40,          5,   None),
    ("KN",    "KBO,NPB",       75,   30,          3,      6),
]


def _mask(text):
    """배당 API 키가 로그에 찍히면 안 됩니다. 공개 저장소면 그대로 노출됩니다.
       깃허브가 비밀값을 자동으로 가려 주긴 하지만 거기에만 기대지 않습니다."""
    key = os.environ.get("ODDS_API_KEY", "")
    return text.replace(key, "***") if len(key) >= 8 else text


def sh(*args, **kw):
    """실행하고 출력을 그대로 흘립니다. 실패하면 예외."""
    cmd = [str(a) for a in args]
    print("$ " + _mask(" ".join(cmd)), flush=True)
    subprocess.run(cmd, check=True, cwd=str(ROOT), **kw)


# ── 실제로 하는 일들 ──────────────────────────────────────────────────────
def _store():
    sys.path.insert(0, str(S))
    import store
    return store


def prep():
    """updater 가 읽는 입력 파일들을 data/ 에서 만들어 둡니다."""
    store = _store()
    TMP.mkdir(exist_ok=True)
    (TMP / "prev.json").write_text(store.read_text("gamesData", "[]"), encoding="utf-8")
    (TMP / "track.json").write_text(store.read_text("trackRecord", "[]"), encoding="utf-8")
    (TMP / "clv.json").write_text(store.read_text("clvLog", "{}"), encoding="utf-8")
    st = store.read("modelState") or {}
    p = TMP / "state.b64"
    p.write_text(st.get("b64", ""), encoding="utf-8")
    return p


def baseball(lgs, mode):
    """mode: 'merge'(목록 생성) | 'refresh'(배당·선발만) | 'settle'(결과 마감)"""
    st = prep()
    out = TMP / "out.json"
    extra = {"merge": [], "refresh": ["--no-settle"], "settle": ["--settle-only"]}[mode]
    sh(sys.executable, S / "daily_update.py", st, out, TMP / "prev.json",
       f"--track={TMP/'track.json'}", f"--clv={TMP/'clv.json'}", f"--lg={lgs}", *extra)
    flag = {"merge": "--merge-lg", "refresh": "--refresh", "settle": "--settle-only"}[mode]
    sh(sys.executable, S / "patch.py", out, flag)


def deep_stats():
    """MLB 심층 스탯. 공개 API 라 크레딧 안 듭니다."""
    import base64
    import gzip
    store = _store()
    dst = TMP / "deep.json"
    sh(sys.executable, S / "deep_stats.py", TMP / "out.json", dst,
       f"--season={datetime.now(KST).year}")
    d = json.load(open(dst, encoding="utf-8"))
    if not d.get("teams"):
        # 그 시각에 경기가 없으면 팀이 하나도 안 담깁니다. 그대로 쓰면 멀쩡하던
        # 심층 자료가 빈 껍데기로 덮입니다. 덮지 않고 옛것을 그대로 둡니다.
        print("심층 스탯: 대상 팀이 없어 기존 자료를 그대로 둡니다")
        return
    raw = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()
    store.write_text("deepStats",
                     json.dumps({"gz": base64.b64encode(gzip.compress(raw, mtime=0)).decode()}))
    print(f"심층 스탯 갱신 완료 · {len(d['teams'])}개 팀")


def soccer():
    """축구. 세 번째 인자(직전 soccerData)를 반드시 넘깁니다 -- 빼먹으면 성적표가 날아갑니다."""
    store = _store()
    TMP.mkdir(exist_ok=True)
    prev = TMP / "soccer_prev.json"
    prev.write_text(store.read_text("soccerData", "{}"), encoding="utf-8")
    out = TMP / "soccer_out.json"
    # 키는 환경변수로만 넘깁니다("-" = 환경변수에서 읽어라). 인자로 주면 로그에 남습니다.
    sh(sys.executable, S / "soccer_update.py", "-", out, prev)
    d = json.load(open(out, encoding="utf-8"))
    store.write_text("soccerData", json.dumps(d, ensure_ascii=False))
    n = sum(len(v) for v in (d.get("leagues") or {}).values())
    sys.path.insert(0, str(S))
    import patch
    patch.stamp_run(f"축구 갱신 {n}경기", patch.kst_now())
    print(f"축구 반영 완료 · {n}경기")


JOBS = {
    "A": lambda: (baseball("MLB", "merge"), deep_stats()),
    "B": lambda: baseball("MLB", "refresh"),
    "C": lambda: baseball("MLB", "refresh"),
    "D": lambda: baseball("MLB", "refresh"),
    "E": lambda: soccer(),
    "F": lambda: baseball("KBO,NPB", "merge"),
    "G": lambda: baseball("MLB", "settle"),
    "H": lambda: baseball("KBO,NPB", "refresh"),
    "I": lambda: baseball("KBO,NPB", "settle"),
}


# ── 상태: 오늘 무엇을 이미 했나 ───────────────────────────────────────────
# data/runstate.json 한 파일에 담고 커밋에 같이 올립니다. 런이 매번 새 기계에서
# 시작하기 때문에, 파일로 남기지 않으면 "아까 했다"를 기억할 방법이 없습니다.
def load_state():
    p = ROOT / "data" / "runstate.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            for k in ("anchors", "snaps", "snapCount"):
                d.setdefault(k, {})
            return d
        except Exception:
            pass
    return {"anchors": {}, "snaps": {}, "snapCount": {}}


def save_state(st):
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "runstate.json").write_text(
        json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def game_start(g):
    """경기 시작 시각(KST, naive). date 는 한국 날짜, status 는 'HH:MM' 입니다."""
    st, dt = g.get("status") or "", g.get("date") or ""
    if len(st) != 5 or st[2] != ":" or len(dt) != 10:
        return None
    try:
        return datetime(int(dt[:4]), int(dt[5:7]), int(dt[8:10]), int(st[:2]), int(st[3:]))
    except ValueError:
        return None


def minutes_to_next(lgs, now):
    """아직 시작 안 한 경기 중 가장 가까운 것까지 (남은 분, 시작시각 'HH:MM'). 없으면 None.

    시작시각을 같이 돌려주는 이유: 같은 시각에 몰린 경기(=묶음)별로 횟수를 따로
    세기 위해서입니다."""
    p = ROOT / "data" / "gamesData.json"
    if not p.exists():
        return None
    try:
        games = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    want = set(lgs.split(","))
    best = None
    for g in games:
        if g.get("lg") not in want or g.get("started") or g.get("cancelled"):
            continue
        s0 = game_start(g)
        if not s0:
            continue
        m = (s0 - now.replace(tzinfo=None)).total_seconds() / 60
        if m <= 0:                      # 이미 시작 -- 라이브 배당이라 쓸 수 없습니다
            continue
        if best is None or m < best[0]:
            best = (m, s0.strftime("%H:%M"))
    return best


def decide(now, st):
    """지금 무엇을 할지 고릅니다. (코드, 설명, 상태갱신함수) 또는 None."""
    today = now.strftime("%Y-%m-%d")

    # 1) 앵커가 밀렸으면 그것부터. 여러 개 밀렸으면 이른 것부터 하나씩.
    for code, label, lo, hi in ANCHORS:
        if lo <= now.hour <= hi and st["anchors"].get(code) != today:
            def mark(c=code):
                st["anchors"][c] = today
                st["anchors"] = {k: v for k, v in st["anchors"].items() if v >= today}
            return code, f"[앵커] {label}", mark

    # 2) 곧 시작하는 경기가 있으면 그 리그 배당·선발 새로고침.
    for unit, lgs, lead, cool, per_cluster, per_day in SNAP_UNITS:
        nxt = minutes_to_next(lgs, now)
        if nxt is None or nxt[0] > lead:
            continue
        mins, cluster = nxt
        last = st["snaps"].get(unit)
        if last:
            try:
                gap = (now.replace(tzinfo=None)
                       - datetime.fromisoformat(last)).total_seconds() / 60
                if gap < cool:
                    continue
            except ValueError:
                pass
        day = (st["snapCount"].get(unit) or {}).get(today) or {}
        if day.get(cluster, 0) >= per_cluster:
            continue
        if per_day is not None and sum(day.values()) >= per_day:
            continue

        def mark(u=unit, t=today, c=cluster):
            st["snaps"][u] = now.replace(tzinfo=None).isoformat(timespec="seconds")
            d = st["snapCount"].setdefault(u, {}).setdefault(t, {})
            d[c] = d.get(c, 0) + 1
            # 지난 날짜는 버립니다. 파일이 계속 커지면 안 되니까요.
            st["snapCount"][u] = {k: v for k, v in st["snapCount"][u].items() if k >= t}

        return ("C" if unit == "MLB" else "H",
                f"[스냅샷] {lgs} · {cluster} 경기까지 {int(mins)}분", mark)

    return None


def main():
    now = datetime.now(KST)
    want = (sys.argv[1] if len(sys.argv) > 1 else "").strip().upper()
    TMP.mkdir(exist_ok=True)
    st = load_state()

    if want in ("", "AUTO"):
        pick = decide(now, st)
        if not pick:
            print(f"KST {now:%m-%d %H:%M} — 지금 할 일 없음 "
                  f"(앵커는 다 돌았고, 곧 시작하는 경기도 없습니다)")
            return
        code, label, mark = pick
    else:
        if want not in JOBS:
            raise SystemExit(f"모르는 작업입니다: {want} (A~I 중 하나)")
        code, label, mark = want, "[손으로] 지정 실행", None

    print(f"=== KST {now:%Y-%m-%d %H:%M} · {code} · {label} ===", flush=True)
    JOBS[code]()
    sh(sys.executable, S / "build.py")
    if mark:
        mark()
        save_state(st)
    print(f"=== {code} 완료 ===")


if __name__ == "__main__":
    main()

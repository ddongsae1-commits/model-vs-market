# -*- coding: utf-8 -*-
"""하루치 작업을 시각에 맞춰 하나 골라 실행한다.

예약작업이 열두 개로 불어나 관리가 안 됐던 적이 있어서, 그때 하나로 합쳤습니다.
그 구조를 그대로 가져왔습니다 -- 워크플로 파일은 하나, 시각만 보고 할 일을 고릅니다.

    python3 scripts/run.py            # 지금 KST 시각에 맞는 일
    python3 scripts/run.py A          # 강제로 A 실행 (손으로 돌릴 때)

예전에는 이 판단을 매번 Claude 가 했습니다. 판단할 게 없는 일이라 표로 굳혔습니다 --
사람도 모델도 안 끼니까 같은 시각에 같은 일이 그냥 돕니다.
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
S = ROOT / "scripts"
TMP = ROOT / ".tmp"
KST = timezone(timedelta(hours=9))

# KST 시(hour) → 할 일. 예약이 몇 분 밀려도 같은 시간대면 같은 일을 고릅니다.
SLOTS = {
    0:  ("A", "MLB 오늘 경기 + 어제 정산 + 심층 스탯"),
    1:  ("B", "MLB 이른 경기 라인업"),
    3:  ("C", "MLB 배당 스냅샷"),
    5:  ("C", "MLB 배당 스냅샷 (이른 경기 확정)"),
    7:  ("D", "MLB 본경기 라인업"),
    9:  ("E", "축구 5대리그 + MLB 배당 스냅샷"),
    12: ("F", "KBO·NPB 오늘 경기 + 어제 정산"),
    15: ("G", "MLB 결과 마감"),
    17: ("H", "KBO·NPB 라인업 확정"),
    23: ("I", "KBO·NPB 결과 마감"),
}


def _mask(text):
    """배당 API 키가 로그에 찍히면 안 됩니다. 공개 저장소면 그대로 노출됩니다.
       GitHub 이 비밀값을 자동으로 가려 주긴 하지만, 거기에만 기대지 않습니다."""
    key = os.environ.get("ODDS_API_KEY", "")
    return text.replace(key, "***") if len(key) >= 8 else text


def sh(*args, **kw):
    """실행하고 출력을 그대로 흘립니다. 실패하면 예외."""
    cmd = [str(a) for a in args]
    print("$ " + _mask(" ".join(cmd)), flush=True)
    subprocess.run(cmd, check=True, cwd=str(ROOT), **kw)


def state_b64():
    """modelState 에서 Elo 상태 문자열만 꺼내 파일로. updater 가 그 형식을 받습니다."""
    import json
    sys.path.insert(0, str(S))
    import store
    st = store.read("modelState") or {}
    p = TMP / "state.b64"
    p.write_text(st.get("b64", ""), encoding="utf-8")
    return p


def prep():
    """updater 가 읽는 입력 파일들을 data/ 에서 만들어 둡니다."""
    sys.path.insert(0, str(S))
    import store
    TMP.mkdir(exist_ok=True)
    (TMP / "prev.json").write_text(store.read_text("gamesData", "[]"), encoding="utf-8")
    (TMP / "track.json").write_text(store.read_text("trackRecord", "[]"), encoding="utf-8")
    (TMP / "clv.json").write_text(store.read_text("clvLog", "{}"), encoding="utf-8")
    return state_b64()


def baseball(lgs, mode):
    """mode: 'merge' | 'refresh' | 'settle'"""
    st = prep()
    out = TMP / "out.json"
    extra = {"merge": [], "refresh": ["--no-settle"], "settle": ["--settle-only"]}[mode]
    sh(sys.executable, S / "daily_update.py", st, out, TMP / "prev.json",
       f"--track={TMP/'track.json'}", f"--clv={TMP/'clv.json'}",
       f"--lg={lgs}", *extra)
    flag = {"merge": "--merge-lg", "refresh": "--refresh", "settle": "--settle-only"}[mode]
    sh(sys.executable, S / "patch.py", out, flag)


def deep_stats():
    """MLB 심층 스탯. 공개 API 라 크레딧 안 듭니다. 실패해도 나머지는 그대로 갑니다."""
    import base64, gzip, json
    sys.path.insert(0, str(S))
    import store
    dst = TMP / "deep.json"
    season = datetime.now(KST).year
    sh(sys.executable, S / "deep_stats.py", TMP / "out.json", dst, f"--season={season}")
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
    import json
    sys.path.insert(0, str(S))
    import store
    TMP.mkdir(exist_ok=True)
    prev = TMP / "soccer_prev.json"
    prev.write_text(store.read_text("soccerData", "{}"), encoding="utf-8")
    out = TMP / "soccer_out.json"
    # 키는 환경변수로만 넘깁니다("-" = 환경변수에서 읽어라). 인자로 주면 로그에 남습니다.
    sh(sys.executable, S / "soccer_update.py", "-", out, prev)
    d = json.load(open(out, encoding="utf-8"))
    store.write_text("soccerData", json.dumps(d, ensure_ascii=False))
    n = sum(len(v) for v in (d.get("leagues") or {}).values())
    import patch                                   # 배지 문구만 빌려 씁니다
    patch.stamp_run(f"축구 갱신 {n}경기", patch.kst_now())
    print(f"축구 반영 완료 · {n}경기")


JOBS = {
    "A": lambda: (baseball("MLB", "merge"), deep_stats()),
    "B": lambda: baseball("MLB", "refresh"),
    "C": lambda: baseball("MLB", "refresh"),
    "D": lambda: baseball("MLB", "refresh"),
    "E": lambda: (soccer(), baseball("MLB", "refresh")),
    "F": lambda: baseball("KBO,NPB", "merge"),
    "G": lambda: baseball("MLB", "settle"),
    "H": lambda: baseball("KBO,NPB", "refresh"),
    "I": lambda: baseball("KBO,NPB", "settle"),
}


def main():
    now = datetime.now(KST)
    want = (sys.argv[1] if len(sys.argv) > 1 else "").strip().upper()
    if want in ("", "AUTO"):
        slot = SLOTS.get(now.hour)
        if not slot:
            print(f"KST {now:%H:%M} — 예정된 일 없음")
            return
        want, label = slot
    else:
        label = dict(SLOTS.values()).get(want, want)
    print(f"=== KST {now:%Y-%m-%d %H:%M} · [{want}] {label} ===", flush=True)
    TMP.mkdir(exist_ok=True)
    JOBS[want]()
    sh(sys.executable, S / "build.py")
    print(f"=== [{want}] 완료 ===")


if __name__ == "__main__":
    main()

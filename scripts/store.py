# -*- coding: utf-8 -*-
"""데이터 저장소. 예전에는 HTML 안 <script> 블록이 저장소였습니다.

아티팩트 시절에는 데이터도 코드도 전부 하나의 HTML 안에 들어 있었고, 갱신이란
그 HTML 안의 블록을 문자열 치환하는 일이었습니다. 저장소로 옮기면서 블록 하나당
파일 하나로 풀었습니다. 달라진 건 '어디에 쓰느냐'뿐이고, 담기는 내용은 그때와
글자 하나까지 같습니다 -- 그래서 기존 스크립트 로직을 그대로 옮길 수 있었습니다.

이렇게 하면 git diff 에 "어제와 뭐가 달라졌는지"가 사람이 읽을 수 있게 남습니다.
"""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# 페이지가 읽는 것 + 파이프라인만 쓰는 것(modelState)
KEYS = ["gamesData", "trackRecord", "modelState", "soccerData", "clvLog", "deepStats"]


def path(key):
    return DATA / f"{key}.json"


def read_text(key, default="null"):
    p = path(key)
    return p.read_text(encoding="utf-8") if p.exists() else default


def read(key, default=None):
    p = path(key)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_text(key, text):
    DATA.mkdir(parents=True, exist_ok=True)
    path(key).write_text(text, encoding="utf-8")


def write(key, value):
    write_text(key, json.dumps(value, ensure_ascii=False))


def read_chips():
    p = DATA / "chips.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"dateChip": "", "runChip": ""}


def write_chips(c):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "chips.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")

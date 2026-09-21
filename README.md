# 모델 대 시장 (model-vs-market)

야구(MLB·KBO·NPB)와 유럽 축구 5대리그의 **모델 확률과 시장(배당) 확률을 나란히 놓고,
둘이 엇갈릴 때 어느 쪽이 맞았는지 계속 세어 보는** 도구입니다.

배팅 조언이 아닙니다. 픽을 팔지도, 사설 사이트로 연결하지도 않습니다.

---

## 어떻게 돌아가나

```
  GitHub Actions (하루 10번, cron)
        │
        ├─ scripts/run.py        지금 몇 시인지 보고 할 일을 하나 고름
        │     ├─ daily_update.py   야구: 일정·선발·배당 수집 + 확률 계산 + 어제 정산
        │     ├─ soccer_update.py  축구: 일정·배당 수집 + 다이슨-콜스 모델
        │     ├─ deep_stats.py     MLB 심층 스탯 (하루 한 번)
        │     └─ patch.py          결과를 data/*.json 에 반영
        │
        ├─ scripts/build.py      template.html + data/*.json → docs/index.html
        └─ git commit && push    GitHub Pages 가 docs/ 를 그대로 서빙
```

**돈이 안 듭니다.** GitHub Actions 는 공개 저장소에서 무료이고, Pages 도 무료입니다.
데이터도 전부 공개 API입니다(MLB statsapi, ESPN, NHL, football-data). 유료 The Odds API 는
KBO·NPB 배당에만, 그것도 무료 월 500크레딧 안에서만 씁니다.

### 하루 일과 (전부 한국시간)

| KST | 코드 | 하는 일 |
|-----|------|---------|
| 00:30 | A | MLB 오늘 경기 만들기 + 어제 정산 + 심층 스탯 |
| 01:30 | B | MLB 이른 경기 라인업 확정 |
| 03:30 | C | MLB 배당 스냅샷 |
| 05:30 | C | MLB 배당 스냅샷 (이른 경기 확정 픽) |
| 07:30 | D | MLB 본경기 라인업 확정 |
| 09:30 | E | 축구 5대리그 + MLB 배당 스냅샷 |
| 12:30 | F | KBO·NPB 오늘 경기 + 어제 정산 |
| 15:30 | G | MLB 결과 마감 |
| 17:30 | H | KBO·NPB 라인업 확정 |
| 23:30 | I | KBO·NPB 결과 마감 |

손으로 돌리려면 Actions 탭 → "갱신" → Run workflow → 작업 코드 선택.
터미널에서는 `python3 scripts/run.py A` (비우면 지금 시각에 맞춰 자동).

---

## 처음 설치 (형님이 하실 일, 5분)

1. **저장소 만들기** — GitHub 에서 New repository. 이름은 아무거나(예: `model-vs-market`).
   **Public** 으로 만드세요. 무료 계정은 비공개 저장소에서 Pages 를 못 씁니다.
   저장소에 비밀은 하나도 없습니다(키는 아래 2번에서 따로 넣습니다).

2. **배당 API 키 넣기** — 저장소 → Settings → Secrets and variables → Actions →
   New repository secret.
   - Name: `ODDS_API_KEY`
   - Secret: 형님이 갖고 계신 The Odds API 키
   이 값은 저장소 파일 어디에도 안 들어가고, 실행 로그에도 안 찍힙니다(두 겹으로 막았습니다).
   키가 없어도 돌아갑니다 — KBO·NPB 배당만 비고 나머지는 그대로입니다.

3. **파일 올리기** — 이 폴더 전체를 저장소에 올립니다(드래그 앤 드롭도 됩니다).

4. **Pages 켜기** — Settings → Pages → Source: `Deploy from a branch`,
   Branch: `main` / 폴더: `/docs` → Save.
   1~2분 뒤 `https://<아이디>.github.io/<저장소이름>/` 에서 열립니다.

5. **Actions 켜기** — Actions 탭에 들어가서 초록 버튼 한 번 누르면 켜집니다.
   바로 한 번 돌려 보시려면 "갱신" → Run workflow.

---

## 폴더 구조

```
template.html          페이지 뼈대. 자리표시자 {{gamesData}} 등에 데이터가 들어갑니다
docs/index.html        빌드 결과. GitHub Pages 가 이 파일을 서빙합니다 (손대지 마세요)
data/
  gamesData.json       오늘 야구 경기 + 확률 + 배당
  trackRecord.json     누적 성적표 (gzip+base64. 3,400경기가 넘어 압축했습니다)
  modelState.json      Elo 등 모델 상태. 페이지는 안 읽고 파이프라인만 씁니다
  soccerData.json      축구 경기 + 확률 + 배당 + 성적표
  clvLog.json          배당 흐름(CLV) 기록
  deepStats.json       MLB 심층 스탯 (gzip+base64)
  chips.json           헤더 배지 두 줄 문구
scripts/
  run.py               시각 → 할 일 고르는 디스패처
  daily_update.py      야구 수집·예측·정산
  soccer_update.py     축구 수집·예측·정산
  deep_stats.py        MLB 심층 스탯 수집
  patch.py             out.json → data/*.json 반영 (병합 규칙이 여기 있습니다)
  build.py             data/ + template → docs/index.html
  store.py             data/ 읽고 쓰기
```

---

## 고생해서 알아낸 것들 (건드리기 전에 읽으세요)

이 목록은 실제로 한 번씩 당한 것들입니다. 주석으로도 코드에 박아 뒀습니다.

- **KST 13시에 미국 날짜가 넘어갑니다.** 그 뒤에 `--merge-lg` 를 쓰면 내일 경기가 오늘
  목록에 끼어듭니다. 15:30·23:30 마감 런이 `--settle-only` 인 이유입니다.
- **이미 시작한 경기의 배당은 라이브 배당입니다.** 스코어가 반영된 값이라 "시장이 경기 전에
  뭐라고 했나"와 전혀 다릅니다. `patch.py` 의 `already_started()` 가 막습니다.
  2026-09-19 에 실제로 KBO 4경기가 오염됐습니다(두산@KT 1.44 → 2.27).
- **CLV 조인 키는 한국 날짜입니다.** MLB 정산행은 미국 날짜라 하루 어긋납니다.
  `settle()` 이 `ckeyK`(한국 날짜)를 하나 더 답니다. 이걸 빼면 MLB CLV 가 영원히 안 붙습니다.
- **스냅샷이 하나뿐인 경기는 CLV 가 기계적으로 0입니다.** '안 움직였다'가 아니라 '못 쟀다'라서
  `measured:0` 을 달아 화면 평균에서 뺍니다.
- **축구 `soccer_update.py` 는 세 번째 인자(직전 soccerData)를 반드시 받아야 합니다.**
  빼먹으면 축구 성적표가 통째로 날아갑니다.
- **ESPN 스코어보드는 날짜 범위 조회(`dates=YYYYMMDD-YYYYMMDD`)를 400 으로 막았습니다.**
  월 단위(`dates=YYYYMM`)만 됩니다. 이걸 몰라서 축구 수집이 6일간 조용히 죽어 있었습니다
  (2026-09-15 ~ 09-21). 그래서 페이지에 "마지막 갱신" 배지와 휴식기 안내를 넣었습니다.
- **심층 스탯은 경기가 0건이면 빈 껍데기가 나옵니다.** 그걸 그대로 쓰면 멀쩡한 자료를
  덮어 버립니다. `run.py` 에서 팀이 비면 안 쓰고 넘어갑니다.
- **계수는 스크립트에 고정돼 있습니다.** 재학습하지 마세요.

## 모델에 대해 지키는 선

화면에 보이는 자료 ≠ 모델이 쓰는 자료입니다.

- 야구 모델이 쓰는 것: 팀 Elo · 최근 25·60경기 성적 · 선발 최근 15등판 실점.
  **불펜·타선·라인업은 안 씁니다.** 화면에는 "왜 이런 경기인가"를 보시라고 붙여 놓은 것이고,
  이미 배당에 반영돼 있는 정보입니다.
- 축구 모델이 쓰는 것: 3시즌 경기 결과(다이슨-콜스 포아송)뿐.
  **최근 폼·맞대결은 안 씁니다.**
- 새 지표를 모델에 넣고 싶으면 **홀드아웃 검증을 먼저 통과해야 합니다.** 화면에 보인다고
  슬쩍 넣지 마세요. MLB 오버/언더는 이 관문을 못 넘어서 안 냅니다(9개 설정 전부 실패).

## 알아 둘 점

- GitHub 예약 실행은 종종 5~30분 밀립니다. 정시를 못 맞춰도 정상입니다.
  `run.py` 는 분이 아니라 **시(hour)** 를 보고 할 일을 고르므로 밀려도 같은 일을 합니다.
- 저장소에 60일간 아무 커밋이 없으면 GitHub 이 예약 실행을 끕니다. 이 워크플로가 매일
  커밋하므로 저절로 살아 있습니다.
- MLB statsapi 는 **비상업적 용도만** 허용합니다.

# recommand-jobs

> 한국 채용공고를 **규칙 기반 크롤러**로 모으고, 내 프로필에 맞는 공고를 **원문 인용과 함께** 골라 주는 Claude Code 스킬

![python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![tests](https://img.shields.io/badge/tests-152%20passed-2ea44f)
![license](https://img.shields.io/badge/license-MIT-blue)
![robots](https://img.shields.io/badge/robots.txt-준수-555)

자소설닷컴·잡코리아·사람인·잡알리오를 기본으로 링커리어·캐치·고용24·리멤버까지 수집합니다.
목록을 전부 읽은 다음, 상세 원문과 공식 공고문으로 자격요건을 확인하고
**"A 지금 지원 / A2 넓혀 보면 / B 보완하면 열림 / 확인 필요"** 네 묶음으로 나눈 마감순 보고서를 만듭니다.

```text
"이번 주 마감하는 전기 신입 공고 찾아줘"
   ↓
 수집(8개 소스) → 목록 전수 검토 → 상세·공식 공고문 → 요건 판정(원문 인용) → 보고서
```

---

## 왜 만들었나

공고를 찾다 보면 같은 이유로 자꾸 놓칩니다. 이 스킬은 그 이유를 하나씩 구조로 막습니다.

| 자주 놓치는 이유 | 막는 방법 |
|---|---|
| **검색어 사각지대.** "전기"로 검색하면 "2026 하반기 신입사원 공개채용"처럼 제목에 직무가 없는 공채가 빠집니다. | 키워드 없이 전수 수집하는 소스를 기본에 넣고, 모은 제목은 배치로 **사람처럼 직접 읽습니다** |
| **자격 오판.** 제목만 보고 골랐다가 "OPIc IM2 필수", "기졸업자에 한함"에서 막힙니다. | 상세 원문에서 어학·자격증·졸업 시점·입사 시기·거주지·교대근무를 **문장 인용과 함께** 뽑습니다 |
| **요약본 함정.** 채용 사이트 본문이 요약본이라 거주지 요건 같은 결정적 조건이 빠져 있습니다. | 기관·기업 **공식 공고문(PDF·HWP·HWPX)** 을 받아 다시 판정하고, 못 받으면 `official_unverified`로 내립니다 |
| **추정 보고.** "아마 될 것"이라고 했다가 지원 기회를 날립니다. | 원문에 없는 것은 쓰지 않습니다. 읽지 못한 요건은 '충족'이 아니라 '불명'입니다 |

판정 단위는 **공고가 아니라 모집 직무**입니다. 공채 하나에 전기·생산·영업이 섞여 있고, 요건도 직무마다 다르기 때문입니다.

## 주요 기능

- **8개 소스 수집 + 실패를 숨기지 않는 종료코드.** `0` 성공, `2` 일부 실패·페이지 상한, `3` 차단·로그인 필요입니다. 소스별 상태는 `run_manifest.json`에 남습니다.
- **3층 검토 자료.** 상위 250건 `shortlist`, 순위 밖 `overflow`, 규칙이 뺀 `excluded_index`로 나눕니다. 수천 건이 모여도 놓치지 않고 읽을 수 있습니다.
- **원문 인용 요건 판정** (`requirements.py`). `eligible_by_text` / `remediable` / `ineligible` / `unknown` 네 가지로 판정합니다. 필수와 우대는 섹션 제목과 문장으로 가립니다.
- **공식 공고문 확보** (`crawl.py official`). 기업 채용페이지와 첨부 공고문을 robots.txt가 허용하는 범위에서만 받아 텍스트로 바꿉니다.
- **거주지·지역 제한 판정.** "주민등록상 주소지가 ○○로 되어 있는 사람" 같은 문장을 잡습니다. 반대로 제출서류의 "주민등록초본"은 요건으로 보지 않습니다. 거주지는 시·도 단위로만 다룹니다.
- **재조사 diff 모드.** 지난 조사와 비교해 `NEW`, `DEADLINE_EXTENDED`, `MISSING` 등 바뀐 것만 보여 줍니다. 검색 계획이 달라 비교할 수 없는 소스는 따로 표시합니다.
- **이미 지원한 곳 표시.** 회사가 같아도 직무가 다르면 다른 지원으로 봅니다.

## 수집 소스 (2026-11 실측)

| 소스 | 상태 | 성격 |
|---|---|---|
| 자소설닷컴 | `core` | 대기업·공기업 공채 달력, 키워드 없이 전수 |
| 잡코리아 | `core` | 키워드 검색형 |
| 사람인 | `core` | 키워드 검색형 (공식 API 키가 있으면 API 우선) |
| 잡알리오 | `core` | 중앙 공공기관 채용 전수 |
| 링커리어 | `optional` | 신입·인턴 보완 |
| 캐치 | `optional` | 대기업·중견 보완 |
| 고용24 | `optional` | 지역·중소·공공 확장 (공식 API 키가 있으면 API 우선) |
| 리멤버 | `optional` | 경력·경력무관 |
| 나라일터·인크루트 | `manual` | 동적 로딩 또는 robots 범위 제한으로 어댑터 미구현 |
| 원티드 | `manual` | robots.txt까지 403. **우회하지 않습니다** |
| 블라인드 | `unsupported` | 웹에 공개된 채용 목록이 없습니다 |
| 피플앤잡·하이브레인넷·클린아이 | `excluded` | robots.txt가 전체 경로를 불허합니다 |

근거(robots.txt 원문, API 문서)는 [`references/sources.md`](references/sources.md)에 있습니다. `python3 scripts/crawl.py sources`로 현재 상태를 볼 수 있습니다.

## 설치

Python 3.11 이상이 필요합니다.

```bash
git clone https://github.com/djfksjd/recommand-jobs.git
cd recommand-jobs
python3 -m pip install -r requirements.txt
mkdir -p ~/.claude/skills
ln -s "$(pwd)" ~/.claude/skills/recommand-jobs
```

공고문 첨부를 읽으려면 다음 도구를 추가로 설치합니다(선택).

```bash
brew install poppler      # PDF → pdftotext
pip install pyhwp         # HWP → hwp5txt  (HWPX는 추가 도구 없이 읽음)
```

**API 키 (선택).** 잡알리오를 공공데이터포털(data.go.kr) API로 받으려면 키를 환경변수로 넘깁니다.
키는 저장소에 넣지 않습니다. `.env`와 키 파일은 `.gitignore`가 막습니다.

```bash
export DATA_GO_KR_KEY="발급받은_키"
```

## 사용법

새 Claude Code 세션에서 자연어로 요청하면 됩니다. 사이트 이름을 말하지 않아도 스킬이 실행됩니다.

- "내 스펙으로 갈 수 있는 전기 신입 공고 찾아줘"
- "공기업 채용 뭐 떴어?"
- "이번 주 마감 공고만 보여줘"
- "지난번 이후 새로 올라온 거 있어?" → diff 모드

처음 실행하면 `recommand-jobs-profile.md`([템플릿](references/profile_template.md))를 만듭니다.
직무 키워드, 신입·경력 구분, 졸업(예정) 시기, 입사 가능 시기, 자격증, 어학 점수와 응시일, 교대근무 가능 여부, 거주 시·도를 정확한 값으로 적습니다.
두 번째 실행부터는 "바뀐 것 있나요?"라고 한 번만 묻습니다.

### 스크립트 직접 실행

```bash
S=scripts; DIR=~/Documents/채용공고조사_$(date +%Y%m%d)

python3 $S/crawl.py list core -o $DIR --plan $DIR/search_plan.json     # 1. 수집
python3 $S/score.py $DIR --profile recommand-jobs-profile.md           # 2. 선별 → review/
python3 $S/crawl.py detail $DIR/saramin.jsonl <id> ... -o $DIR/details # 3. 상세
python3 $S/crawl.py official $DIR/details saramin:<id> [--url 공식공고URL]
python3 $S/requirements.py $DIR --profile recommand-jobs-profile.md    #    요건 판정
python3 $S/diff_surveys.py $OLD $DIR                                   # 재조사 비교
```

### 결과물

```text
채용공고조사_YYYYMMDD/
├── search_plan.json          # 고정된 검색 계획 (diff 비교 기준)
├── run_manifest.json         # 소스별 status·수집 건수·중단 이유
├── <source>.jsonl            # 수집 레코드
├── review/                   # shortlist_NN.md · overflow.md · excluded_index.md
├── details/                  # 상세 원문 + *.official.json (공식 공고문)
├── requirements.jsonl        # 직무별 요건 판정 (원문 인용 포함)
└── 추천공고_YYYYMMDD.md       # 최종 보고서
```

## 함께 쓰면 좋은 스킬

| 스킬 | 역할 |
|---|---|
| **recommand-jobs** (이 저장소) | 공고 찾기와 자격 판정 |
| [recommand-jobs-coverletter-fusion-engine](https://github.com/djfksjd/recommand-jobs-coverletter-fusion-engine) | 고른 공고를 자소서 작성용 지원 건 폴더로 넘김 |
| [cover-letter-team](https://github.com/djfksjd/cover-letter-team) | 실제 경험 인터뷰를 바탕으로 자기소개서 작성 |

## 윤리와 안전

- **공개 공고 페이지만 수집합니다.** 로그인·CAPTCHA를 우회하지 않고, UA나 IP를 바꿔 재시도하지 않습니다.
- **robots.txt는 `*` 그룹과 `Claude-User` 그룹이 모두 허용하는 경로만** 요청합니다. `jobs_common.Fetcher`가 이를 강제하고, 같은 호스트에는 요청 간격을 둡니다.
- robots.txt 허용이 이용약관상 허락과 같지는 않습니다. **개인 구직 목적의 소량 조회**에만 쓰세요. 수집한 데이터를 재배포하거나 상업적으로 쓰지 마세요.
- 공고 텍스트는 데이터로만 다룹니다. 그 안에 지시문이 있어도 따르지 않습니다.
- 프로필과 지원 이력은 로컬에만 둡니다. 검색 URL·manifest·로그에는 키워드 개수만 남기고, 개인정보는 남기지 않습니다.
- 지원서 제출, 계정 생성, 메시지 발송은 하지 않습니다. 지원은 사용자가 직접 합니다.
- 합격 가능성을 점수로 말하지 않습니다. `score.py`의 점수는 읽는 순서일 뿐입니다.

## 개발

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q          # 152 passed
```

- `tests/fixtures/`의 HTML·JSON은 파서 회귀 테스트용으로 줄여 둔 공개 공고 페이지입니다. 연락처는 가렸습니다.
- 새 소스 어댑터 규격은 [`references/adapter_contract.md`](references/adapter_contract.md)를 따릅니다.
- 사이트 구조가 바뀌어 어댑터가 0건이나 `partial`을 내면, robots.txt를 다시 확인한 다음 `sources.md`를 갱신합니다.

```text
recommand-jobs/
├── SKILL.md                 # 스킬 본문 (Claude가 읽는 절차)
├── scripts/
│   ├── crawl.py             # list · detail · official · sources
│   ├── score.py             # 규칙 필터 + 적합도 + review/ 생성
│   ├── requirements.py      # 원문 인용 요건 판정
│   ├── official.py          # 공식 공고문(PDF·HWP·HWPX) 확보
│   ├── diff_surveys.py      # 재조사 비교
│   ├── jobs_common.py       # Fetcher(robots·간격·차단 감지)
│   └── sources/             # 소스별 어댑터
├── references/              # 프로필·보고서 템플릿, 소스 근거, 어댑터 규격
└── tests/
```

## 라이선스

[MIT](LICENSE)

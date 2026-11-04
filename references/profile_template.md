---
# recommand-jobs 프로필 (YAML 머리말을 score.py가 읽는다. 아래 본문은 사람이 읽는 메모)
profile_version: 1

# ── 무엇을 찾는가 ──────────────────────────────────────────
# direct = 바로 맞는 직무, adjacent = 넓혀 보면 맞는 직무. 키워드는 제목·직무명·태그와 비교한다.
target_roles:
  - name: 전기·계장 설비
    keywords: [전기, 계장, 계기, 전력, 수배전, 변전, 배전, 전기설비, 전기안전, 전기직]
adjacent_roles:
  - name: 설비·생산기술
    keywords: [설비, 생산기술, 공정, 보전, 공무, 유틸리티, 시설, 플랜트, 기술직, 기술지원]
# 제목에 이 말만 있고 위 키워드가 없으면 감점한다(제외하지 않음 — 종합공채 안에 맞는 직무가 숨어 있을 수 있다)
avoid_keywords: [영업, 마케팅, 디자이너, 간호, 회계, 법무, MD]

# ── 지원 자격 축 (정확한 값으로 적는다. "600점대" 같은 범위 금지) ──────────
career: [신입, 인턴]          # 지원할 구분. 신입이면 '경력무관'은 자동 포함
employment_types_ok: [정규직, 계약직, 인턴]   # 빼면 그 고용형태 공고를 '사용자 조건 불일치'로 표시
education: 학사
graduation: 2027-02          # 졸업(예정) 연-월 (아래 값은 모두 예시 — 전기 신입 지원자 기준)
available_from: 2027-01      # 입사 가능 시기(연-월). 모르면 비워 둔다
major: 전자공학               # 공고의 "전기 관련학과"에 들어가는지는 추정하지 않고 '확인 필요'로 둔다
licenses:
  - {name: 전기기사, acquired: 2026-01-01}   # 예시 값 — 사용자 값으로 바꾼다
languages:                    # 시험명·점수·응시일(유효기간 2년 계산용)
  - {test: TOEIC, score: 700, date: 2026-01-01}  # 예시 값
speaking: none                # none 이면 OPIc/토익스피킹 필수 공고는 'B: 보완 필요'
shift_work_ok: true           # 교대근무 가능 여부. 모르면 비워 둔다
driver_license: 1종보통        # 공공·시설 공고에 자주 붙는 요건. 없으면 비워 둔다
residence: []                 # 주민등록 거주지 시·도(지역인재·지역 제한 판정용, 예: [전북]). 주소 자체는 적지 않는다

# ── 선호 (점수에만 반영, 제외하지 않음) ──────────────────────
locations:
  prefer: []                  # 비우면 전국 동일
  exclude: []                 # 절대 불가 지역만 (예: [제주])
company_types_prefer: [대기업, 중견, 공기업, 공공기관]
salary_prefer_min: 4000       # 희망 연봉 하한(만원/년). 표기 하한이 넘으면 가점, 표기 최대가 못 미치면 감점. 표기 없음=영향 없음
posting_edu_prefer: 대졸       # 대졸(4년) 이상 대상 공고 선호 — 고졸·초대졸 대상 공고는 감점(학력 무관은 그대로)
exclude_companies: []         # 절대 지원 안 할 회사

# ── 이미 지원한 곳 ───────────────────────────────────────
# applied.tsv(회사<TAB>직무<TAB>상태) 경로. 없으면 스킬이 사용자 폴더의 지원 이력 표에서 만든다.
applied_list: recommand-jobs-applied.tsv

# ── 수집 설정 ───────────────────────────────────────────
sources: [core]               # core / public / extra / all 또는 소스 이름
keyword_expansions: []        # 지난 조사에서 LLM이 제안해 사용자가 승인한 확장어(다음 실행부터 고정 반영)
last_survey: null             # 마지막 조사 폴더(재조사 diff 모드가 사용)
---

# 메모
- 이 파일은 로컬에만 둔다. 이름·연락처·생년월일 같은 개인정보는 넣지 않는다.
- 판정 축(career·graduation·licenses·languages·speaking·employment_types_ok·locations.exclude)이 바뀌면
  재조사 때 직전 판정을 승계하지 않고 전부 다시 본다.

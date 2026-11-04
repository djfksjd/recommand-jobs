# 소스별 실측·API 근거 (2026-11-03)

재확인 주기: 어댑터가 0건/구조 변경으로 partial을 내거나, 3개월이 지나면 robots.txt를 다시 받아 이 표를 갱신한다.

| 소스 | robots (`User-agent: *` 기준) | 목록 페이지 | 데이터 형태 | 판정 |
|---|---|---|---|---|
| 자소설닷컴 jasoseol.com | `/` 허용 (`/crt`·`/core`·`/webview` 등만 불허) | `/recruit` 200 | `POST /employment/calendar_list.json` (기간 지정) → JSON 176건(11월분). 상세 `/recruit/{id}` `__NEXT_DATA__`에 직무(field)·경력구분(division)·원 채용페이지 URL | **핵심** — 공채 달력, 키워드 없이 전수 |
| 잡코리아 jobkorea.co.kr | `*` 그룹은 로그인·마이페이지만 불허. `/Search/` 허용. AI 학습봇(ClaudeBot 등) 전면 차단, `Claude-User` 등 사용자 요청형은 공개 콘텐츠 허용 | `/Search/?stext=…` 200 (470KB, Next.js) | 검색 HTML(키워드 필요) | **핵심** — 키워드 검색형 |
| 사람인 saramin.co.kr | `/zf_user/search/recruit` 허용(지원·이력서·인재검색 불허). GPTBot 차단 | 검색 200 (2.3MB) | 검색 HTML + 공식 Open API(키 필요) | **핵심** — 키 있으면 API 우선 |
| 리멤버 career.rememberapp.co.kr | `/job/` 허용, `?seed=`·`/job/private-positions/`·`/job_postings/` 불허 | `/job/postings` 200 | Next.js, 목록은 클라이언트 요청(SSR에는 배너·seed만) | 선택 — 경력 위주 |
| 블라인드 teamblind.com | `/search/`·`/kr/search/` 불허. ClaudeBot·GPTBot 전면 차단 | `/kr/jobs`·`/kr/hire` 404 | 웹에서 채용공고 목록 경로 확인 안 됨. `recruit.teamblind.com`은 팀블라인드 **자사** 채용 홈페이지 | **미지원(unsupported)** — 웹 공개 채용 목록 없음 |
| 원티드 wanted.co.kr | robots.txt 자체가 CloudFront 403 | 403 | — | **수동(manual)** — 우회 금지 |
| 링커리어 linkareer.com | `/` 허용 | `/list/recruit` 200 | `__NEXT_DATA__` 안 `activityItems` + `__APOLLO_STATE__`(RecruitInformation, Activity) | 보완 — 신입·인턴 |
| 캐치 catch.co.kr | `/Search`·`/Company`·`/Apply`·일부 `/api/v1.0/…` 불허, `/NCS/RecruitSearch` 허용 | 200 | `window.__NUXT__` 내장 JSON | 보완 — 대기업·중견 |
| 고용24 work24.go.kr | `/cm/common/`·`/sa/`·`/ei/`·통합검색만 불허 | `/wk/a/b/1200/retriveDtlEmpSrchList.do` 200 (530KB) | 서버 렌더 HTML, `wantedAuthNo` 10건/페이지 | 선택 — 지역·중소·공공 |
| 잡알리오 job.alio.go.kr | robots.txt 404 → 제한 없음 | `/recruit.do` 200 | 서버 렌더 HTML, `recruitview.do?idx=` 10건/페이지 | **핵심(공공 희망자)** |
| 나라일터 gojobs.go.kr | Googlebot 규칙만 있음(`*` 없음) → 제한 없음 | `/apmList.do` 200 | 목록 HTML, 항목은 별도 요청 | 선택 — 공무원·공공 |
| 인크루트 | `www.incruit.com`은 `Disallow: /`, `job.incruit.com`은 파트너 페이지만 불허 | `job.incruit.com/jobdb_list/searchjob.asp` 200 | EUC-KR HTML, `jobpost.asp?job=` 81건 | 선택 — job 하위도메인만 |
| 잡다 jobda.im | `/` 허용(개인 페이지 불허) | `/position` 200 | Next.js 셸 + `api.jobda.im` | 선택 — 검증 필요 |
| 슈퍼루키 superookie.com | 전부 허용 | `/jobs` 200 | 클라이언트 템플릿(`{{_id}}`) + 내부 API | 선택 — 검증 필요 |
| 잡플래닛 jobplanet.co.kr | `/apply`·`/profile`·`/users` 등 불허 | `/job` 200 | 클라이언트 렌더 | 선택 — 검증 필요 |
| 피플앤잡 peoplenjob.com | **`Disallow: /`** | — | — | **제외** |
| 하이브레인넷 hibrain.net | **`*` 전부 불허**(검색엔진만 허용) | — | — | **제외** |
| 클린아이 잡플러스 cleaneye.go.kr | **`Disallow: /`** | — | — | **제외** |

## 공식 API 요약

## 요약표

| 소스 | 공식 API | 개인 키 발급 | 하루 호출 한도 | 신입 필터 | 형식 |
|---|---|---|---|---|---|
| 고용24 채용정보·공채속보·공채기업 | 있음 | **불가(기업회원 전용)**. 담당자 심사 후 발급 | 미확인 | `career=N` / 공채속보 `empWantedCareerCd=30` | XML |
| 잡알리오(재정경제부, data.go.kr 15125273) | 있음 | 가능(data.go.kr 회원). 개발계정 자동승인 | 개발계정 1,000 | `recrutSe=R2010`(신입) | JSON(기본)/XML |
| 나라일터(인사혁신처, data.go.kr 15156780) | 있음 | 가능. 개발·운영 모두 자동승인 | 개발계정 10,000 | 따로 없음(공고유형 `e01` 공개경쟁) | XML |
| 사람인 | 있음 | 신청서에 회사명/학교명 칸이 있음. 승인 심사를 거침(개인 승인 여부 미확인) | **500** | 요청 파라미터 없음. 응답의 `experience-level code=1`로 걸러야 함 | XML/JSON |
| 원티드 | 있음 | **사업자등록번호 필수**라 사실상 개인 불가 | 미확인 | `years=[0,..]` | JSON |
| 잡코리아·인크루트·잡플래닛 | 공개 API 찾지 못함 | 없음 | - | - | - |

---


## 운영 메모
- 잡알리오 API(apis.data.go.kr/1051000/recruitment/list): 신입 R2010, 경력 R2020, 신입+경력 R2030 / NCS 전기·전자 R600019, 기계 R600015 / 개발계정 1,000회/일. 사용자 서비스키가 '등록되지 않은 서비스키'(코드 30)면 data.go.kr 15125273에서 활용신청(자동승인).
- 나라일터 API(apis.data.go.kr/1760000/PblJobService/getList): 자동승인, 10,000회/일, XML. 15156780 활용신청 후 어댑터 추가 가능.
- 사람인 API: 하루 500회, 1회 110건, 'Powered by 취업 사람인' 표기 의무, 경력 필터 없음(응답 experience-level로 거름).
- 고용24 API는 기업회원 전용 — 개인은 웹 목록(상세검색, 주요 기업 공채속보 thmaHrplCd=S00074)만.

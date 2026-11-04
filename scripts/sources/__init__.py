"""소스 레지스트리 — 상태는 2026-11-03 실측 기준(references/sources.md에 근거).

status
  core        : 기본 수집
  optional    : 프로필·요청에 따라 추가
  manual      : 자동 수집 불가(접근 차단) — 사용자가 브라우저에서 확인, 보고서에 '수동 확인' 명시
  unsupported : 웹에 공개 채용 목록이 확인되지 않음
  excluded    : robots.txt가 전부 불허 — 수집하지 않음
"""

REGISTRY = {
    "jasoseol":  {"module": "jasoseol",  "status": "core",     "label": "자소설닷컴",
                  "note": "대기업·공기업 공채 달력, 키워드 없이 전수"},
    "jobkorea":  {"module": "jobkorea",  "status": "core",     "label": "잡코리아",
                  "note": "키워드 검색형 — 검색 계획 키워드 합집합"},
    "saramin":   {"module": "saramin",   "status": "core",     "label": "사람인",
                  "note": "키워드 검색형(공식 API 키가 있으면 API 우선)"},
    "alio":      {"module": "alio",      "status": "core",     "label": "잡알리오(공공기관)",
                  "note": "공공기관 채용 전수 — 공공 희망자 기본"},
    "linkareer": {"module": "linkareer", "status": "optional", "label": "링커리어",
                  "note": "신입·인턴 보완"},
    "catch":     {"module": "catch",     "status": "optional", "label": "캐치",
                  "note": "대기업·중견 보완"},
    "work24":    {"module": "work24",    "status": "optional", "label": "고용24",
                  "note": "지역·중소·공공 확장(공식 API 키가 있으면 API 우선)"},
    "remember":  {"module": "remember",  "status": "optional", "label": "리멤버",
                  "note": "경력 위주 — 경력·경력무관 탐색 시"},
    "gojobs":    {"module": None,        "status": "manual",   "label": "나라일터",
                  "note": "공무원 채용 — 목록이 동적 로딩, 어댑터 미구현"},
    "wanted":    {"module": None,        "status": "manual",   "label": "원티드",
                  "note": "robots.txt까지 CloudFront 403 — 우회하지 않음"},
    "blind":     {"module": None,        "status": "unsupported", "label": "블라인드",
                  "note": "웹 공개 채용 목록 경로 없음(/kr/jobs·/kr/hire 404, recruit.teamblind.com은 자사 채용)"},
    "incruit":   {"module": None,        "status": "manual",   "label": "인크루트",
                  "note": "www는 Disallow: / — job.incruit.com만 허용, 어댑터 미구현"},
    "peoplenjob": {"module": None,       "status": "excluded", "label": "피플앤잡",  "note": "robots Disallow: /"},
    "hibrain":   {"module": None,        "status": "excluded", "label": "하이브레인넷", "note": "robots * 전부 불허"},
    "cleaneye":  {"module": None,        "status": "excluded", "label": "클린아이 잡플러스", "note": "robots Disallow: /"},
}

GROUPS = {
    "core": [k for k, v in REGISTRY.items() if v["status"] == "core"],
    "public": ["alio", "work24"],
    "extra": [k for k, v in REGISTRY.items() if v["status"] == "optional"],
    "all": [k for k, v in REGISTRY.items() if v["status"] in ("core", "optional")],
}

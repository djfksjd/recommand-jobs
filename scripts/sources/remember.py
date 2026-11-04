"""리멤버 커리어(career.rememberapp.co.kr) 어댑터 — 경력 위주, 경력무관 공고도 있다.

실측(2026-11-03):
  * career.rememberapp.co.kr robots `*`: Allow /job/, /sitemap*.xml. Disallow `/*?*seed=`,
    /job/private-positions/, /job/sign-in-guide, /job/posting-group/, /job/apply-related/, /job_postings/.
  * 목록은 클라이언트에서 `POST https://career-api.rememberapp.co.kr/job_postings/search` 로 가져온다
    (API 호스트 robots.txt는 404 → Fetcher 규칙상 전부 허용). 로그인 없이 200 JSON.
      body: {"page": n, "per": 30(최대; 100은 400 "최대 요청 수 초과"),
             "search": {snake_case 필터}, "sort": "ends_at_asc" | "starts_at_desc" | ...}
      - 필터 키는 snake_case여야 적용된다(camelCase는 조용히 무시됨 — meta.logger_info로 확인).
      - career_year=-1 → "경력 무관"(min/max_experience 둘 다 null) 1,188건,
        career_year=0 → 0년차 지원 가능 2건, career_year=N → N년차 가능. min_experience/max_experience 범위도 가능.
      - keywords: ["전기"] (리스트. 문자열은 500).
      - sort "ends_at_asc" 는 [ends_at asc, id asc]로 결정적 → 페이지가 안정적이다.
        기본 정렬(starts_at_desc)은 _score가 앞서고 클라이언트가 seed를 넣는다 — seed는 쓰지 않는다.
      - 전체 13,764건(2026-11-03), 깊은 페이지는 400 "최대 조회 가능 갯수를 초과".
      - 각 항목에 본문 필드가 통째로 있다: job_description(주요업무), qualifications(자격요건),
        preferred_qualifications(우대사항), recruiting_process, additional_information, introduction,
        min/max_experience, education_requirement, addresses, job_categories, organization, ends_at, explicit_due.
  * 마감: 사이트 UI는 `!explicit_due || !ends_at` 이면 "채용 시 마감"으로 표시한다(JS 실측). 그 외 ends_at.
  * 상세: SSR `/job/posting/<id>` 의 __NEXT_DATA__.props.pageProps.dehydratedState.queries
      [queryKey ["/job_postings/<id>"]].state.data.data — 목록 항목과 같은 필드(camelCase).
  * recruiter.name(담당자 이름)은 개인정보라 저장하지 않는다.
"""
from __future__ import annotations

import json
import re

from jobs_common import Blocked, is_expired, make_opening, make_record, norm_text

NAME = "remember"
LABEL = "리멤버"
HOSTS = ("career.rememberapp.co.kr", "career-api.rememberapp.co.kr")
KIND = "full"  # 신입 계획이면 경력무관·0년차 전수, 경력 계획이면 키워드 검색(query)으로 전환
DELAY = 2.5  # 0.8초 간격 연속 31회에서 Cloudflare 429 실측 → 분당 ~24회로 낮춤
BASE = "https://career.rememberapp.co.kr"
API = "https://career-api.rememberapp.co.kr"
PER = 30
FULL_MAX_PAGES = 60  # 전수 모드 필터당 페이지 상한(30건×60=1,800 > 경력무관 1,188건)

EDU = {"none": "학력 무관", "high_school": "고졸 이상", "associate": "전문학사 이상",
       "bachelor": "학사 이상", "master": "석사 이상", "doctor": "박사 이상"}
HEADERS = {"Origin": BASE, "Referer": f"{BASE}/job/postings", "Accept": "application/json"}


# ─────────────────────────────── 파서(네트워크 없음) ───────────────────────────────

def _snake(k: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", k).lower()


def snake_keys(o):
    """SSR(camelCase) 데이터를 API(snake_case)와 같은 모양으로."""
    if isinstance(o, dict):
        return {_snake(k): snake_keys(v) for k, v in o.items()}
    if isinstance(o, list):
        return [snake_keys(x) for x in o]
    return o


def career_text(mn, mx) -> str:
    """min/max_experience → 사람이 읽는 표기. 둘 다 없으면 사이트 기준 '경력 무관'."""
    if mn is None and mx is None:
        return "경력 무관"
    if mn == 0:
        return f"신입~경력 {mx}년" if mx is not None else "신입·경력 무관"
    if mn is not None and mx is not None:
        return f"경력 {mn}년" if mn == mx else f"경력 {mn}~{mx}년"
    if mn is not None:
        return f"경력 {mn}년 이상"
    return f"경력 {mx}년 이하"


def deadline_of(p):
    """(deadline_raw, deadline_iso|None, kind). UI 규칙: explicit_due와 ends_at이 둘 다 있어야 날짜 마감."""
    ends = p.get("ends_at")
    if p.get("explicit_due") and ends:
        iso = ends[:16] + ends[-6:] if re.search(r"[+-]\d\d:\d\d$", ends) else ends[:16] + "+09:00"
        return ends, iso, "date"
    return "채용 시 마감", None, "채용시"


def _locations(p):
    out = []
    for a in p.get("addresses") or []:
        s = " ".join(x for x in (a.get("address_level1"), a.get("address_level2")) if x)
        if s and s not in out:
            out.append(s)
    return out


def _salary(p):
    mn, mx = p.get("min_salary"), p.get("max_salary")
    if mn and mx:
        return f"{mn:,}~{mx:,}만 원" if mn != mx else f"{mn:,}만 원"
    if mn:
        return f"{mn:,}만 원 이상"
    if mx:
        return f"최대 {mx:,}만 원"
    return None


def _categories(p):
    return [f"{c.get('level1')}/{c.get('level2')}" for c in (p.get("job_categories") or []) if c.get("level2")]


def to_record(p, ref=None):
    """검색 API 항목(snake_case) 1건 → schema v1 레코드."""
    pid = p["id"]
    org = p.get("organization") or {}
    craw = career_text(p.get("min_experience"), p.get("max_experience"))
    locs = _locations(p)
    dl_raw, dl, kind = deadline_of(p)
    cats = _categories(p)
    link = (p.get("link") or "").strip() or None
    opening = make_opening(role=p.get("title"), opening_id=pid, career=craw, locations=locs,
                           extra={"job_categories": cats, "job_rank_category": p.get("job_rank_category"),
                                  "job_role": p.get("job_role")})
    return make_record(
        source=NAME, source_id=pid, url=f"{BASE}/job/posting/{pid}",
        company=org.get("name"), title=p.get("title"), openings=[opening],
        career=craw, edu=EDU.get(p.get("education_requirement"), p.get("education_requirement")),
        locations=locs, deadline_raw=dl_raw, deadline=dl, deadline_kind=kind,
        deadline_precision="minute" if dl else None,
        posted_at=p.get("starts_at"), tags=[c.split("/", 1)[1] for c in cats],
        salary_raw=_salary(p),
        apply_url=link if p.get("application_type") != "apply" else None,
        extra={"min_experience": p.get("min_experience"), "max_experience": p.get("max_experience"),
               "job_categories": cats, "headhunter": bool(org.get("headhunter")),
               "leader_position": bool(p.get("leader_position")),
               "application_type": p.get("application_type"), "status": p.get("status"),
               "organization_id": org.get("id"), "company_homepage": org.get("url") or None,
               "explicit_due": p.get("explicit_due"), "ends_at": p.get("ends_at"),
               "has_body_in_list": bool(p.get("job_description") or p.get("qualifications"))},
        ref=ref,
    )


def parse_search(data):
    """검색 응답 → (items, total_count, total_pages). 구조가 다르면 RuntimeError."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), list) or "meta" not in data:
        raise RuntimeError("응답 구조 변경: data/meta 없음")
    m = data["meta"]
    return data["data"], m.get("total_count"), m.get("total_pages") or m.get("total_page")


SECTION_FIELDS = (("주요업무", "job_description"), ("자격요건", "qualifications"),
                  ("우대사항", "preferred_qualifications"), ("채용절차", "recruiting_process"),
                  ("기타안내", "additional_information"), ("회사소개", "introduction"))


def posting_from_next_data(html, pid=None):
    """상세 SSR HTML → 공고 dict(snake_case). 없으면 None."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    for q in (pp.get("dehydratedState") or {}).get("queries") or []:
        key = q.get("queryKey") or []
        if not (key and isinstance(key[0], str) and re.fullmatch(r"/job_postings/\d+", key[0])):
            continue
        d = ((q.get("state") or {}).get("data") or {}).get("data")
        if isinstance(d, dict) and d.get("id") and (pid is None or str(d["id"]) == str(pid)):
            return snake_keys(d)
    return None


def detail_from_posting(p):
    """공고 dict → fetch_detail 반환값."""
    rec = to_record(p)
    sections = {}
    for label, field in SECTION_FIELDS:
        v = (p.get(field) or "").strip()
        if v:
            sections[label] = v
    if rec["locations"]:
        sections["근무지"] = ", ".join(rec["locations"])
    sections["경력"] = rec["career_raw"]
    if rec["edu"]:
        sections["학력"] = rec["edu"]
    text = "\n\n".join(f"[{k}]\n{v}" for k, v in sections.items())
    body_len = sum(len(norm_text(sections.get(k, ""))) for k in ("주요업무", "자격요건", "우대사항"))
    link = (p.get("link") or "").strip() or None
    return {
        "openings": rec["openings"],
        "text": text,
        "text_incomplete": body_len < 80,  # 요건 본문이 사실상 비었으면 원 채용페이지 확인
        "employer_url": link,
        "company_homepage": (p.get("organization") or {}).get("url") or None,
        "deadline_raw": rec["deadline_raw"],
        "deadline": rec["deadline"],
        "deadline_kind": rec["deadline_kind"],
        "career_raw": rec["career_raw"],
        "status": p.get("status"),
        "posted_at": p.get("starts_at"),
        "sections": sections,
    }


# ─────────────────────────────── 수집 ───────────────────────────────

def _career_filters(plan):
    """plan.career → 서버 필터 목록. 신입·인턴만 원하면 경력무관(-1)+0년차(0). 경력 포함/미지정이면 None(필터 없음)."""
    want = set(plan.get("career") or [])
    if want and "경력" not in want and want & {"신입", "인턴", "무관"}:
        return [{"career_year": -1}, {"career_year": 0}]
    return None


def _search(f, search, page, sort):
    body = {"page": page, "per": PER, "sort": sort,
            "search": {"include_applied_job_posting": False, **search}}
    return parse_search(f.post_json(f"{API}/job_postings/search", body, headers=HEADERS))


def list_postings(f, plan):
    filters = _career_filters(plan)
    keywords = [k for k in (plan.get("keywords") or []) if str(k).strip()]
    max_pages = int(plan.get("max_pages") or 5)
    if filters is None and keywords:
        mode = "query"
        queries = [({"keywords": [k]}, max_pages) for k in keywords]
        sort = "starts_at_desc"  # 키워드 검색 시 서버 정렬은 [starts_at desc, id desc](점수 없음)
        scope = f"키워드 {len(keywords)}개 × 최대 {max_pages}p, 경력 필터 없음"
    else:
        mode = "full"
        cap = int(plan.get("full_max_pages") or FULL_MAX_PAGES)
        queries = [(flt, cap) for flt in (filters or [{}])]
        sort = "ends_at_asc"
        scope = (f"경력무관·0년차 전수(필터당 최대 {cap}p)" if filters
                 else f"전체 공고 마감순(최대 {cap}p)")
    recs, seen, dups, errors = [], set(), 0, []
    pages, reported, capped, expired, blocked = 0, 0, False, 0, False
    for search, cap in queries:
        if blocked:
            break
        page, total_pages = 1, None
        while True:
            if page > cap:
                capped = True
                break
            try:
                items, total, total_pages = _search(f, search, page, sort)
            except Blocked as e:
                if not recs:
                    raise  # 첫 요청부터 차단 → manual
                # 이미 모은 것은 버리지 않고 partial로 끝낸다. 재시도·우회하지 않는다.
                errors.append(f"p{page}: 차단으로 중단({e}) — 이후 페이지 미수집")
                blocked = True
                break
            except Exception as e:
                errors.append(f"p{page}: {type(e).__name__}: {e}")
                break
            pages += 1
            if page == 1:
                reported += int(total or 0)
            for p in items:
                pid = p.get("id")
                if pid in seen:
                    dups += 1
                    continue
                seen.add(pid)
                rec = to_record(p)
                if "career_year" in search:  # 서버가 이 경력 연차 지원 가능으로 분류함(extra는 list_hash 밖)
                    rec["extra"]["matched_career_year"] = search["career_year"]
                if is_expired(rec):
                    expired += 1
                    continue
                recs.append(rec)
            if not items or not total_pages or page >= total_pages:
                break
            page += 1
    stop = "blocked-midway" if blocked else "page-cap" if capped else ("error" if errors else ("keywords-exhausted" if mode == "query" else "last-page"))
    return {"records": recs, "pages": pages, "reported_total": reported, "duplicates": dups,
            "stop_reason": stop, "errors": errors, "partial": capped or bool(errors),
            "scope": scope + (f", 마감 지난 {expired}건 제외" if expired else "")}


def fetch_detail(f, rec):
    """SSR 상세 페이지의 dehydratedState에서 본문·자격요건·우대사항을 읽는다."""
    html = f.get(f"{BASE}/job/posting/{rec['source_id']}")
    p = posting_from_next_data(html, rec["source_id"])
    if p is None:
        raise RuntimeError("상세 dehydratedState에 공고 데이터 없음(마감·비공개 또는 구조 변경)")
    return detail_from_posting(p)

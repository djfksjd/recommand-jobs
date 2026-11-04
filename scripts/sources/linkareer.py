"""링커리어(linkareer.com) 어댑터 — 신입·인턴 채용 보완.

실측(2026-11-03):
  * robots.txt: linkareer.com 은 `/` 허용(`/stem/learn/...` 만 불허). api.linkareer.com/robots.txt 는 404
    → robots 명세상 전부 허용(Fetcher도 같은 해석).
  * 목록: 사이트 프런트가 쓰는 공개 GraphQL `POST https://api.linkareer.com/graphql` (로그인 불필요)
      activities(filterBy:{activityTypeID:"5"(채용), status:"OPEN", jobTypes:[NEW|EXPERIENCED|INTERN|CONTRACT]},
                 orderBy:{field:RECENT, direction:DESC}, pagination:{page, pageSize ≤ 100})
      → {totalCount, nodes[Activity]} — 키워드 없이 열린 채용 전수(신입+인턴 필터 시 약 1,700건 = 18요청).
      jobTypes 필터는 '하나라도 포함'이라 신입·경력 무관(NEW+EXPERIENCED) 공고가 함께 들어온다.
  * 대체 경로: SSR `/list/recruit?page=N&filterBy_jobTypes=NEW` 의 __NEXT_DATA__.props.pageProps.__APOLLO_STATE__
      (Activity:*, RecruitInformation:*, Category:*, RegionDistrict:*), 20건/페이지. GraphQL이 구조 변경 등으로
      실패할 때만 쓴다.
  * recruitType: NORMAL=마감일 있음, ASAP=채용 시 마감(이때 recruitCloseAt은 등록 +90일 자리값 → 마감일로 쓰지 않음).
  * recruitCloseAt/recruitStartAt: epoch ms(KST 23:59:59.999 형태).
  * 상세: `/activity/<id>` SSR __APOLLO_STATE__ → Activity:<id>.detailText → ActivityText.text(HTML, 종종 이미지),
      applyDetail(접수 URL), homepageURL, education, organizationType, addresses.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from jobs_common import (KST, Blocked, RobotsDisallowed, log, make_opening, make_record, norm_text, now_kst)

NAME = "linkareer"
LABEL = "링커리어"
HOSTS = ("linkareer.com",)  # api.linkareer.com 포함(서브도메인)
KIND = "full"
PLAN_KEYS = ("career", "days_ahead", "page_cap")  # 키워드는 이 소스의 결과에 영향이 없다
DELAY = 0.8
BASE = "https://linkareer.com"
GQL = "https://api.linkareer.com/graphql"
PAGE_SIZE = 100
PAGE_CAP = 40  # 전수형 자체 상한(4,000건). plan.max_pages(키워드당)와 별개.
SSR_PAGE_CAP = 30

JOB_TYPE = {"NEW": "신입", "EXPERIENCED": "경력", "INTERN": "인턴", "CONTRACT": "계약"}
CAREER_TO_JOBTYPE = {"신입": "NEW", "경력": "EXPERIENCED", "인턴": "INTERN", "계약": "CONTRACT"}
EMPLOYMENT = {"INTERN": "인턴", "CONTRACT": "계약직"}
ORG_SIZE = {"대기업": "대기업", "중견기업": "중견", "중소기업": "중소", "외국계기업": "외국계",
            "공기업": "공기업", "공공기관": "공공기관", "스타트업": "스타트업"}
EDUCATION = {"NO_LIMIT": "학력무관", "HIGH_SCHOOL_GRADUATE": "고졸 이상",
             "COLLEGE_GRADUATE_2_3_YEARS": "대학졸업(2,3년) 이상",
             "COLLEGE_GRADUATE_4_YEARS": "대학졸업(4년) 이상",
             "MASTER": "석사 이상", "DOCTOR": "박사 이상"}

QUERY = """query RecruitList($filterBy: ActivityFilter, $orderBy: ActivityOrder, $pagination: Pagination) {
  activities(filterBy: $filterBy, orderBy: $orderBy, pagination: $pagination) {
    totalCount
    nodes {
      id title organizationName organizationType recruitType recruitStartAt recruitCloseAt createdAt
      jobTypes education
      categories { id name parent { id name } }
      regions { id name }
      regionDistricts { id name region { name } }
      recruitInformations { id jobType internTypes { name } }
    }
  }
}"""


# ─────────────────────────────── 공통 변환 ───────────────────────────────

def _ms_to_iso(ms):
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, KST).isoformat(timespec="minutes")
    except (TypeError, ValueError, OSError):
        return None


def _category_names(cats):
    out = []
    for c in cats or []:
        name = c.get("name")
        parent = (c.get("parent") or {}).get("name")
        if name == "전체" and parent:
            name = f"{parent} 전체"
        if name and name not in out:
            out.append(name)
    return out


def _locations(node):
    locs = []
    for d in node.get("regionDistricts") or []:
        reg = (d.get("region") or {}).get("name")
        s = " ".join(x for x in (reg, d.get("name")) if x)
        if s and s not in locs:
            locs.append(s)
    if not locs:
        locs = [r["name"] for r in node.get("regions") or [] if r.get("name")]
    return locs


def node_to_record(node, ref=None):
    """GraphQL Activity 노드(또는 SSR에서 참조를 풀어 같은 모양으로 만든 dict) → 레코드. 마감 지났으면 None."""
    ref = ref or now_kst()
    aid = str(node["id"])
    rinfos = node.get("recruitInformations") or []
    job_types = node.get("jobTypes") or [ri.get("jobType") for ri in rinfos]
    cats = _category_names(node.get("categories"))
    role = ", ".join(cats) or None
    locs = _locations(node)
    openings = []
    for ri in rinfos:
        jt = ri.get("jobType")
        interns = [t.get("name") for t in ri.get("internTypes") or [] if t.get("name")]
        openings.append(make_opening(
            role=role, opening_id=ri.get("id"),
            career=[JOB_TYPE[jt]] if jt in JOB_TYPE else None,
            locations=locs,
            employment_type=(", ".join(interns) if interns else EMPLOYMENT.get(jt)),
            extra={"job_type": jt, "intern_types": interns}))
    if not openings:  # 상세 SSR에는 recruitInformations가 없고 jobTypes만 있다
        openings = [make_opening(role=role, career=[JOB_TYPE[jt]], locations=locs,
                                 employment_type=EMPLOYMENT.get(jt), extra={"job_type": jt})
                    for jt in job_types if jt in JOB_TYPE] or [make_opening(role=role, locations=locs)]
    close_iso = _ms_to_iso(node.get("recruitCloseAt"))
    rtype = node.get("recruitType")
    if rtype == "ASAP":
        deadline, kind, prec, raw = None, "채용시", None, "채용 시 마감"
    elif close_iso:
        if datetime.fromisoformat(close_iso) < ref:
            return None
        deadline, kind, prec, raw = close_iso, "date", "minute", close_iso
    else:
        deadline, kind, prec, raw = None, "unknown", None, None
    careers = [JOB_TYPE[j] for j in job_types if j in JOB_TYPE]
    emp = sorted({EMPLOYMENT[j] for j in job_types if j in EMPLOYMENT})
    org = node.get("organizationType")
    return make_record(
        source=NAME, source_id=aid, url=f"{BASE}/activity/{aid}",
        company=node.get("organizationName"), title=node.get("title"),
        openings=openings, career=careers or None,
        edu=EDUCATION.get(node.get("education"), node.get("education")) if node.get("education") else None,
        employment_type=", ".join(emp) or None,
        locations=locs,
        deadline=deadline, deadline_kind=kind, deadline_precision=prec, deadline_raw=raw,
        may_close_early=True if rtype == "ASAP" else None,
        posted_at=_ms_to_iso(node.get("recruitStartAt") or node.get("createdAt")),
        company_size=ORG_SIZE.get(org), tags=cats,
        extra={"recruit_type": rtype, "job_types": job_types, "organization_type": org,
               "job_categories": cats,
               "placeholder_close_at": close_iso if rtype == "ASAP" else None,
               "roles_need_detail": True},
    )


# ─────────────────────────────── 파서(네트워크 없음) ───────────────────────────────

def parse_graphql(data):
    """GraphQL 응답 dict → (nodes, totalCount). 오류·구조 변경이면 RuntimeError."""
    if not isinstance(data, dict):
        raise RuntimeError("GraphQL 응답이 dict 아님")
    if data.get("errors"):
        msg = str((data["errors"][0] or {}).get("message", ""))[:120]
        raise RuntimeError(f"GraphQL 오류: {msg}")
    acts = ((data.get("data") or {}).get("activities")) or None
    if acts is None or "nodes" not in acts:
        raise RuntimeError("응답 구조 변경: data.activities.nodes 없음")
    return acts["nodes"] or [], acts.get("totalCount")


def _next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        raise RuntimeError("__NEXT_DATA__ 없음(구조 변경 또는 차단)")
    return json.loads(m.group(1))


def _resolve(ap, v, depth=0):
    """Apollo 정규화 캐시의 {"__ref": "Type:id"} 를 풀어 중첩 dict로."""
    if depth > 6:
        return v
    if isinstance(v, dict):
        if "__ref" in v and len(v) == 1:
            return _resolve(ap, ap.get(v["__ref"]) or {}, depth + 1)
        return {k: _resolve(ap, x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [_resolve(ap, x, depth + 1) for x in v]
    return v


def parse_list_html(html):
    """SSR /list/recruit 페이지 → (nodes, totalCount). 노드 모양은 GraphQL과 같다."""
    pp = _next_data(html).get("props", {}).get("pageProps", {})
    ap = pp.get("__APOLLO_STATE__") or {}
    root = ap.get("ROOT_QUERY") or {}
    conn = None
    for k, v in root.items():
        if k.startswith("activities(") and '"activityTypeID":"5"' in k:
            conn = v
            break
    if conn is None:
        raise RuntimeError("SSR 목록 activities 쿼리 없음(구조 변경)")
    nodes = [_resolve(ap, n) for n in conn.get("nodes") or []]
    return nodes, conn.get("totalCount")


def parse_detail_html(html, aid):
    pp = _next_data(html).get("props", {}).get("pageProps", {})
    ap = pp.get("__APOLLO_STATE__") or {}
    a = ap.get(f"Activity:{aid}")
    if not a:
        raise Blocked("상세 데이터 비어 있음(삭제·비공개 또는 로그인 요구 가능)")
    act = _resolve(ap, {k: v for k, v in a.items()
                        if k in ("detailText", "recruitInformations", "categories", "regionDistricts",
                                 "addresses", "applyTypes", "rootCategories")})
    content = ((act.get("detailText") or {}).get("text")) or ""
    body = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", content, flags=re.S | re.I)
    body = re.sub(r"<(br|/p|/div|/li|/tr|/h\d)[^>]*>", "\n", body, flags=re.I)
    text = "\n".join(filter(None, (norm_text(x) for x in body.split("\n"))))
    imgs = len(re.findall(r"<img\b", content, re.I))
    iframes = len(re.findall(r"<iframe\b", content, re.I))
    node = dict(a)
    for k in ("recruitInformations", "categories", "regionDistricts"):
        node[k] = act.get(k) or []
    rec = node_to_record(node)
    apply_url = a.get("applyDetail") if str(a.get("applyDetail") or "").startswith("http") else None
    addrs = [" ".join(filter(None, [x.get("address"), x.get("detailAddress")]))
             for x in act.get("addresses") or [] if x.get("address")]
    sections = {}
    if a.get("education"):
        sections["학력"] = EDUCATION.get(a["education"], a["education"])
    if addrs:
        sections["근무지"] = " / ".join(addrs)
    if a.get("organizationType"):
        sections["기업형태"] = a["organizationType"]
    m = re.search(r"\[?\s*(자격\s*요건|지원\s*자격)\s*\]?\s*\n(.*?)(?:\n\s*\[|\Z)", text, re.S)
    if m:
        sections["자격요건"] = m.group(2).strip()[:2000]
    m = re.search(r"\[?\s*우대\s*사항\s*\]?\s*\n(.*?)(?:\n\s*\[|\Z)", text, re.S)
    if m:
        sections["우대사항"] = m.group(1).strip()[:2000]
    return {
        "openings": rec["openings"] if rec else [],
        "text": text,
        "text_incomplete": (len(text) < 200 and (imgs > 0 or iframes > 0)) or not text,
        "images": imgs,
        "employer_url": apply_url or a.get("homepageURL"),
        "deadline_raw": rec["deadline_raw"] if rec else ("채용 시 마감" if a.get("recruitType") == "ASAP"
                                                         else _ms_to_iso(a.get("recruitCloseAt"))),
        "deadline": rec["deadline"] if rec else None,
        "deadline_kind": rec["deadline_kind"] if rec else None,
        "posted_at": _ms_to_iso(a.get("recruitStartAt")),
        "status": a.get("status"),
        "sections": sections,
    }


# ─────────────────────────────── 어댑터 인터페이스 ───────────────────────────────

def _job_types(plan):
    wanted = plan.get("career") or []
    jts = [CAREER_TO_JOBTYPE[c] for c in wanted if c in CAREER_TO_JOBTYPE]
    return sorted(set(jts)) or None  # None = 전체


def _list_graphql(f, job_types, cap):
    nodes, total, pages = [], None, 0
    for page in range(1, cap + 1):
        filt = {"activityTypeID": "5", "status": "OPEN"}
        if job_types:
            filt["jobTypes"] = job_types
        try:
            data = f.post_json(GQL, {"operationName": "RecruitList", "query": QUERY,
                                     "variables": {"filterBy": filt,
                                                   "orderBy": {"field": "RECENT", "direction": "DESC"},
                                                   "pagination": {"page": page, "pageSize": PAGE_SIZE}}},
                               headers={"Origin": BASE, "Referer": f"{BASE}/list/recruit",
                                        "Accept": "application/json"})
        except Blocked:
            if page == 1:
                raise
            return nodes, total, pages, "blocked-midway"  # 모은 것은 보존 — partial
        got, total = parse_graphql(data)
        pages += 1
        nodes += got
        if len(got) < PAGE_SIZE or (total is not None and page * PAGE_SIZE >= total):
            return nodes, total, pages, "end"
    return nodes, total, pages, "page-cap"


def _list_ssr(f, job_types, cap):
    nodes, total, pages = [], None, 0
    for page in range(1, cap + 1):
        params = [("page", page)] + [("filterBy_jobTypes", j) for j in (job_types or [])]
        try:
            html = f.get(f"{BASE}/list/recruit", params=params)
        except Blocked:
            if page == 1:
                raise
            return nodes, total, pages, "blocked-midway"
        got, total = parse_list_html(html)
        pages += 1
        nodes += got
        if not got or (total is not None and page * 20 >= total):
            return nodes, total, pages, "end"
    return nodes, total, pages, "page-cap"


def list_postings(f, plan):
    job_types = _job_types(plan)
    errors, via = [], "graphql"
    try:
        nodes, total, pages, stop = _list_graphql(f, job_types, int(plan.get("page_cap") or PAGE_CAP))
    except (Blocked, RobotsDisallowed):
        raise
    except Exception as e:  # API 구조 변경 → SSR 목록으로(20건/페이지, 상한 적용)
        log(f"[{NAME}] GraphQL 실패({type(e).__name__}) → SSR 목록으로 대체")
        errors.append(f"graphql: {type(e).__name__}: {str(e)[:100]}")
        via = "ssr"
        nodes, total, pages, stop = _list_ssr(f, job_types, SSR_PAGE_CAP)
        if stop == "page-cap":
            errors.append("SSR 대체 경로 페이지 상한 도달")
    now = now_kst()
    recs, seen, dup, expired = [], set(), 0, 0
    for n in nodes:
        aid = str(n.get("id"))
        if aid in seen:
            dup += 1
            continue
        seen.add(aid)
        rec = node_to_record(n, now)
        if rec is None:
            expired += 1
            continue
        recs.append(rec)
    label = "+".join(JOB_TYPE[j] for j in job_types) if job_types else "전체"
    return {"records": recs, "pages": pages, "reported_total": total, "duplicates": dup,
            "stop_reason": stop, "errors": errors + (["중간 차단 — 모은 것까지만"] if stop == "blocked-midway" else []),
            "partial": stop in ("page-cap", "blocked-midway"),
            "scope": f"열린 채용 전수({label} 유형 필터, 경력무관 포함, {via}, 마감 {expired}건 제외)"}


def fetch_detail(f, rec):
    aid = rec["source_id"]
    html = f.get(f"{BASE}/activity/{aid}")
    return parse_detail_html(html, aid)

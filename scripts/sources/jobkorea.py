"""잡코리아(jobkorea.co.kr) 어댑터 — 키워드 검색형.

실측(2026-11-03):
  * robots.txt `User-agent: *`·`Claude-User` 그룹 모두 /Search/·/Recruit/GI_Read/·/Recruit/GI_Read_Comt_Ifrm 허용
    (불허는 /login/, /User/, /my/, /corp/, /RecrtMng/, /Recruit/JobList/SpaceClick 등).
  * 목록: `GET /Search/?stext=<kw>&careerType=1,4&Page_No=<1..>` → Next.js App Router HTML(~470KB).
    공고 데이터는 HTML 카드가 아니라 RSC 페이로드(`self.__next_f.push([1,"..."])`) 안의
    react-query 탈수 상태에 있다: queryKey ["JOB_LIST", 20, <page0>, ...] → data
    {pageSize:20, pageNumber, totalElements, totalPages, content:[...]}.
    HTML 카드에는 광고(AD) 카드가 섞여 있지만 JOB_LIST에는 없다 → JSON만 쓴다.
      content[i]: id(=GI_Read 번호), title, companyName, postingCompanyName, careerType, careerRange,
      educationCode(코드표 미확인 → extra에 원값만), employmentTypeCodeList(["1/0"] — 앞자리=CODES.employment),
      areaCodeList(CODES.area 코드), applicationPeriod{start,end}(ISO, 시각 포함), createdAt, isNewcomerJob
  * 같은 페이지의 ["CODES"] 쿼리에 코드표가 있다: career 1=신입, 2=경력, 4=경력무관, (목록에 3=신입·경력이
    나오지만 코드표엔 없음), area(코드→"서울>강남구"), employment(1 정규직, 2 계약직, 3 인턴 ...).
  * careerType 필터: `careerType=1,4`(쉼표) = 신입 OR 경력무관(실측: totalElements 2,599 → 6,030, 4가 섞여 나옴).
    `careerType=1&careerType=4`처럼 반복하면 마지막 값만 쓰인다(실측) — 쉼표를 써야 한다.
  * 페이지: `Page_No=2` → pageNumber 1 (실측). `page=` 는 무시된다. 페이지당 20건 고정.
  * 상세: `/Recruit/GI_Read/<id>` — RSC의 ["JOBS", id] 쿼리에 구조화 데이터(접수기간·경력·근무지 주소·
    홈페이지 접수 URL), 서버 렌더 HTML에 '모집요강/지원자격' 요약(학력·우대조건 문자열).
    본문은 iframe `/Recruit/GI_Read_Comt_Ifrm?Gno=<id>` 의 RSC 텍스트 행(`<ref>:T<hex>,` + HTML)에 있다.
    본문이 통이미지인 공고가 많다 → text_incomplete.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import quote

from jobs_common import (Blocked, make_opening, make_record, norm_text, now_kst)

NAME = "jobkorea"
LABEL = "잡코리아"
HOSTS = ("jobkorea.co.kr",)
KIND = "query"
DELAY = 1.0
BASE = "https://www.jobkorea.co.kr"
PAGE_SIZE = 20

CAREER_TYPE = {"1": "신입", "2": "경력", "3": "신입·경력", "4": "경력무관"}
_APPLY_METHOD_RX = re.compile(r"^(홈페이지|이메일|e-?mail|사람인|잡코리아|온라인|우편|방문)\s*(지원|접수)$", re.I)

_DEC = json.JSONDecoder()


# ─────────────────────────────── RSC 해석 ───────────────────────────────

def rsc_text(html: str) -> str:
    """`self.__next_f.push([1,"..."])` 문자열 조각을 순서대로 이어 붙인 RSC 스트림."""
    out = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html, re.S):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return "".join(out)


def dehydrated_queries(stream: str) -> list[dict]:
    """react-query 탈수 상태의 queries 전부(queryKey, data)."""
    qs = []
    for m in re.finditer(r'\{"state":\s*\{"mutations"', stream):
        try:
            obj, _ = _DEC.raw_decode(stream[m.start():])
        except ValueError:
            continue
        for q in (obj.get("state") or {}).get("queries") or []:
            qs.append({"key": q.get("queryKey") or [], "data": (q.get("state") or {}).get("data")})
    return qs


def _find_query(qs, name):
    for q in qs:
        if q["key"] and q["key"][0] == name:
            return q
    return None


def rsc_text_row(stream: str, ref: str) -> str | None:
    """`<ref>:T<hex>,<본문>` 텍스트 행. 길이는 UTF-8 바이트 수(16진)."""
    m = re.search(r'(?:^|\n|\])' + re.escape(ref) + r':T([0-9a-f]+),', stream)
    if not m:
        return None
    n = int(m.group(1), 16)
    return stream[m.end():].encode("utf-8")[:n].decode("utf-8", "ignore")


# ─────────────────────────────── 목록 ───────────────────────────────

def _area_names(codes_data) -> dict:
    out = {}
    for a in (codes_data or {}).get("area") or []:
        tag = a.get("tagDisplayName") or a.get("displayName") or ""
        out[str(a.get("code"))] = tag.replace(">", " ").strip()
    return out


def _employment_names(codes_data) -> dict:
    return {str(e.get("code")): e.get("displayName") for e in (codes_data or {}).get("employment") or []}


def _career_text(ct, rng):
    base = CAREER_TYPE.get(str(ct))
    if not base:
        return None
    try:
        rng = int(rng or 0)
    except (TypeError, ValueError):
        rng = 0
    if str(ct) in ("2", "3") and 0 < rng < 100:  # 100은 '연차 무관'으로 보인다(카드에 연차 표시 없음)
        return f"{base}{rng}년↑"
    return base


def parse_list(html: str) -> dict:
    """검색 결과 HTML → {"items": [dict], "total": int|None, "page": int|None, "total_pages": int|None}.

    항목은 사이트 원값 위주 dict(정규화는 to_record에서). JOB_LIST 쿼리가 없으면 RuntimeError.
    """
    qs = dehydrated_queries(rsc_text(html))
    jl = _find_query(qs, "JOB_LIST")
    if not jl or not isinstance(jl["data"], dict) or "content" not in jl["data"]:
        raise RuntimeError("JOB_LIST 쿼리 없음(구조 변경 의심)")
    codes = (_find_query(qs, "CODES") or {}).get("data") or {}
    areas, emps = _area_names(codes), _employment_names(codes)
    d = jl["data"]
    items = []
    for x in d.get("content") or []:
        gid = str(x.get("id") or "").strip()
        if not gid.isdigit():
            continue
        period = x.get("applicationPeriod") or {}
        emp_codes = [str(c).split("/", 1)[0] for c in (x.get("employmentTypeCodeList") or [])]
        items.append({
            "id": gid,
            "title": x.get("title"),
            "company": x.get("postingCompanyName") or x.get("companyName"),
            "career_type": str(x.get("careerType")) if x.get("careerType") is not None else None,
            "career": _career_text(x.get("careerType"), x.get("careerRange")),
            "education_code": x.get("educationCode"),
            "employment_types": [emps[c] for c in emp_codes if emps.get(c)],
            "locations": [areas[c] for c in (x.get("areaCodeList") or []) if areas.get(str(c))],
            "area_codes": x.get("areaCodeList") or [],
            "start": period.get("start"),
            "end": period.get("end"),
            "created_at": x.get("createdAt"),
            "is_newcomer": x.get("isNewcomerJob"),
            "subsidiary": x.get("companySubsidiary"),
            "legacy_no": x.get("legacyJobNo"),
        })
    return {"items": items, "total": d.get("totalElements"), "page": d.get("pageNumber"),
            "total_pages": d.get("totalPages")}


def _deadline_from_iso(end):
    """applicationPeriod.end(ISO) → (deadline, kind, precision). 먼 미래(2099~)는 상시로 본다."""
    if not end:
        return None, "unknown", None
    try:
        dt = datetime.fromisoformat(end)
    except ValueError:
        return None, "unknown", None
    if dt.year >= 2099:
        return None, "상시", None
    return dt.isoformat(timespec="minutes"), "date", "minute"


def to_record(it: dict, ref=None) -> dict:
    deadline, kind, prec = _deadline_from_iso(it.get("end"))
    return make_record(
        source=NAME, source_id=it["id"], url=f"{BASE}/Recruit/GI_Read/{it['id']}",
        company=it.get("company"), title=it.get("title"),
        career=it.get("career"), edu=None,
        employment_type=", ".join(it.get("employment_types") or []) or None,
        locations=it.get("locations"),
        deadline_raw=it.get("end"), deadline=deadline, deadline_kind=kind, deadline_precision=prec,
        posted_at=it.get("start") or it.get("created_at"),
        extra={"career_type_code": it.get("career_type"), "education_code": it.get("education_code"),
               "area_codes": it.get("area_codes"), "is_newcomer_job": it.get("is_newcomer"),
               "company_group": it.get("subsidiary"), "roles_need_detail": True},
        ref=ref,
    )


def career_param(plan_career) -> str | None:
    """plan["career"] → careerType 값. 경력이 들어 있으면 필터 없음(전체). 경력무관(4)은 항상 포함."""
    wants = set(plan_career or [])
    if not wants or "경력" in wants:
        return None
    return "1,4"


def search_url(keyword: str, career: str | None, page: int) -> str:
    u = f"{BASE}/Search/?stext={quote(keyword)}"
    if career:
        u += f"&careerType={quote(career, safe='')}"
    if page > 1:
        u += f"&Page_No={page}"
    return u


def list_postings(f, plan):
    keywords = [k for k in (plan.get("keywords") or []) if str(k).strip()]
    if not keywords:
        raise RuntimeError("키워드형 소스인데 plan.keywords가 비어 있음")
    max_pages = int(plan.get("max_pages") or 5)
    career = career_param(plan.get("career"))
    now = now_kst()
    recs, seen = [], set()
    pages = dups = expired = 0
    reported = 0
    errors, capped, blocked = [], 0, False
    for ki, kw in enumerate(keywords, 1):
        kw_seen = set()  # 이 키워드에서 본 id — 페이지 종료 판단용(키워드 간 중복과 분리)
        for page in range(1, max_pages + 1):
            try:
                html = f.get(search_url(kw, career, page), headers={"Referer": f"{BASE}/"})
                parsed = parse_list(html)
            except Blocked as e:
                if not recs:
                    raise
                errors.append(f"키워드#{ki} p{page}: 차단 — {e}")
                blocked = True
                break
            except Exception as e:  # 이 키워드만 포기하고 다음으로
                # 예외 문자열의 URL(검색어·키 포함 가능)은 지운다 — manifest에 검색어를 남기지 않는다
                msg = re.sub(r"https?://\S+|stext=\S+|searchword=\S+|access-key=\S+", "<url>", str(e))
                errors.append(f"키워드#{ki} p{page}: {type(e).__name__}: {msg}"[:200])
                break
            pages += 1
            if page == 1 and parsed["total"] is not None:
                reported += int(parsed["total"])
            new = 0
            kw_new = 0
            for it in parsed["items"]:
                if it["id"] not in kw_seen:
                    kw_seen.add(it["id"])
                    kw_new += 1
                if it["id"] in seen:
                    dups += 1
                    continue
                seen.add(it["id"])
                new += 1
                rec = to_record(it, ref=now)
                if rec["deadline"] and rec["deadline"] < now.isoformat(timespec="minutes"):
                    expired += 1
                    continue
                recs.append(rec)
            tp = parsed.get("total_pages")
            if kw_new == 0 or not parsed["items"] or (tp is not None and page >= tp):
                break
            if page == max_pages:
                capped += 1
        if blocked:
            break
    partial = bool(capped) or blocked
    stop = "blocked" if blocked else ("page-cap" if capped else "exhausted")
    scope = (f"키워드 {len(keywords)}개 × 최대 {max_pages}p(20건/p), "
             + ("신입·경력무관 필터" if career else "경력 필터 없음")
             + (f", 페이지 캡 {capped}개 키워드" if capped else "")
             + (f", 마감 지남 {expired}건 제외" if expired else ""))
    return {"records": recs, "pages": pages, "reported_total": reported, "duplicates": dups,
            "stop_reason": stop, "errors": errors, "partial": partial, "scope": scope}


# ─────────────────────────────── 상세 ───────────────────────────────

def html_to_text(html: str) -> str:
    s = re.sub(r"<!--.*?-->", " ", html or "", flags=re.S)
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<(br|/p|/div|/li|/tr|/h\d|/dd|/dt|/table)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;|&#160;", " ", s)
    s = re.sub(r"&lt;", "<", s)
    s = re.sub(r"&gt;", ">", s)
    s = re.sub(r"&amp;", "&", s)
    lines = [norm_text(x) for x in s.split("\n")]
    return "\n".join(x for x in lines if x)


SECTION_HEADS = {
    "자격요건": r"자격\s*요건|지원\s*자격|필수\s*(?:조건|요건|사항)|자격\s*조건",
    "우대사항": r"우대\s*(?:사항|조건|요건)",
    "담당업무": r"담당\s*업무|주요\s*업무|수행\s*업무",
    "근무지": r"근무\s*(?:지|지역|장소|위치)",
    "접수기간": r"접수\s*기간|지원\s*기간|모집\s*기간",
    "전형절차": r"전형\s*절차|채용\s*절차|전형\s*단계",
}
# 잘라낼 때 '다음 제목'으로만 쓰는 머리글(섹션으로 담지는 않는다)
STOP_HEADS = r"근무\s*조건|복지|혜택|채용\s*절차|제출\s*서류|접수\s*방법|유의\s*사항|기업\s*소개|회사\s*소개"
_ANY_HEAD = re.compile(r"^[^\w\n]{0,3}(?:" + "|".join(SECTION_HEADS.values()) + "|" + STOP_HEADS + r")", re.M)


def split_sections(text: str, limit=1500) -> dict:
    """본문 텍스트에서 줄 머리에 있는 섹션 제목으로 잘라낸다(처음 나온 것만). 못 찾으면 키가 없다."""
    out = {}
    for name, rx in SECTION_HEADS.items():
        m = re.search(r"^[^\w\n]{0,3}(?:" + rx + r")[^\w\n]{0,3}[:：]?[ \t]*(.*)$", text, re.M)
        if not m:
            continue
        start = m.start(1)
        nxt = _ANY_HEAD.search(text, m.end())
        body = text[start: nxt.start() if nxt else len(text)].strip()
        if body:
            out[name] = body[:limit]
    return out


def parse_summary_html(html: str) -> dict:
    """GI_Read 서버 렌더 요약('모집요강'~'지원자격'): label → value 문자열."""
    i = html.find("모집요강")
    if i < 0:
        return {}
    ends = [k for k in (html.find('<template id="B:', i), html.find('id="recommended-section"', i)) if k > 0]
    seg = html[i: min(ends) if ends else i + 20000]
    seg = re.sub(r"<[^>]*$", "", seg)  # 잘린 태그 꼬리
    # 각 라벨은 <span>라벨</span> 다음 형제 요소에 값이 있다 → 줄 단위 텍스트로 풀어 다음 줄들을 값으로.
    seg = re.sub(r"<button\b.*?</button>", " ", seg, flags=re.S)
    seg = re.sub(r"</span>", "\n", seg)
    lines = html_to_text(seg).split("\n")
    labels = ("모집분야", "모집인원", "고용형태", "급여", "근무시간", "근무요일", "근무지주소", "경력", "학력",
              "스킬", "우대조건", "기본우대", "우대전공", "자격증", "외국어", "성별", "나이", "지원자격")
    out, cur = {}, None
    for ln in lines:
        if ln in labels:
            cur = ln
            out.setdefault(cur, [])
            continue
        if cur and ln not in ("모집요강",):
            out[cur].append(ln)
    return {k: " ".join(v).strip() for k, v in out.items() if v}


def parse_detail(main_html: str, ifrm_html: str | None) -> dict:
    """GI_Read 본 페이지 + 본문 iframe → fetch_detail 반환 형식(openings 제외 필드 포함)."""
    qs = dehydrated_queries(rsc_text(main_html))
    jobs = (_find_query(qs, "JOBS") or {}).get("data") or {}
    base = jobs.get("base") or {}
    if not base:
        raise RuntimeError("상세 JOBS 쿼리 없음(구조 변경 또는 차단)")
    rec_ = (base.get("overview") or {}).get("recruitment") or {}
    req = base.get("requirement") or {}
    wp = ((base.get("workCondition") or {}).get("workplace") or {})
    addrs = [a.get("fullAddress") or a.get("address") for a in (wp.get("locationAttributes") or [])]
    addrs = [a for a in addrs if a]
    homepage = None
    for ro in (base.get("reception") or {}).get("receptionOptions") or []:
        if ro.get("type") == "HOMEPAGE" and ro.get("contents"):
            homepage = ro["contents"][0]
            break
    summary = parse_summary_html(main_html)

    body_html = ""
    if ifrm_html:
        st = rsc_text(ifrm_html)
        m = re.search(r'"htmlContent":"\$(\w+)"', st)
        if m:
            body_html = rsc_text_row(st, m.group(1)) or ""
        elif '"htmlContent":"' in st:  # 인라인 문자열인 경우
            m2 = re.search(r'"htmlContent":("(?:[^"\\]|\\.)*")', st)
            body_html = json.loads(m2.group(1)) if m2 else ""
    text = html_to_text(body_html)
    imgs = len(re.findall(r"<img\b", body_html, re.I))

    sections = split_sections(text)
    if summary.get("경력") or summary.get("학력"):
        q = " / ".join(f"{k}: {summary[k]}" for k in ("경력", "학력") if summary.get(k))
        sections["자격요건"] = q + (("\n" + sections["자격요건"]) if sections.get("자격요건") else "")
    pref = " / ".join(f"{k}: {summary[k]}" for k in ("우대조건", "기본우대", "우대전공", "자격증", "외국어")
                      if summary.get(k))
    if pref:
        sections["우대사항"] = pref + (("\n" + sections["우대사항"]) if sections.get("우대사항") else "")
    if addrs:
        sections["근무지"] = " / ".join(addrs)
    elif summary.get("근무지주소"):
        sections["근무지"] = summary["근무지주소"]
    start, end = rec_.get("applicationStartAt"), rec_.get("applicationEndAt")
    if rec_.get("alwaysHire"):
        deadline_raw = "상시채용"
    elif rec_.get("closeOnHire"):
        deadline_raw = "채용시 마감"
    else:
        deadline_raw = end
    if start or end:
        sections["접수기간"] = f"{(start or '')[:16]} ~ {(end or '')[:16]}".strip()

    careers = [c.get("type") for c in (req.get("careers") or [])]
    cmap = {"NEWBIE": "신입", "EXPERIENCED": "경력", "ANY": "경력무관"}
    career_txt = ", ".join(cmap.get(c, str(c)) for c in careers) or summary.get("경력")
    roles = [r for r in (rec_.get("workFields") or []) if r and not _APPLY_METHOD_RX.match(norm_text(r))]
    openings = [make_opening(role=r, career=career_txt, locations=addrs or None) for r in roles]
    return {
        "openings": openings,
        "text": text,
        "text_incomplete": (len(text) < 200 and imgs > 0) or not body_html,
        "images": imgs,
        "employer_url": homepage,
        "deadline_raw": deadline_raw,
        "posted_at": start,
        "edu": summary.get("학력"),
        "career_raw": career_txt,
        "may_close_early": True if rec_.get("closeOnHire") else None,
        "sections": sections,
    }


def fetch_detail(f, rec):
    gid = rec["source_id"]
    main = f.get(f"{BASE}/Recruit/GI_Read/{gid}", headers={"Referer": f"{BASE}/"})
    ifrm = None
    try:
        ifrm = f.get(f"{BASE}/Recruit/GI_Read_Comt_Ifrm?Gno={gid}",
                     headers={"Referer": f"{BASE}/Recruit/GI_Read/{gid}"})
    except Blocked:
        raise
    except Exception:  # 본문 iframe 실패 → 요약만으로 반환, text_incomplete=True
        ifrm = None
    return parse_detail(main, ifrm)

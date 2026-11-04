"""사람인(saramin.co.kr) 어댑터 — 키워드 검색형. 기본은 HTML 검색, SARAMIN_ACCESS_KEY가 있으면 공식 API.

실측(2026-11-03):
  * robots.txt `User-agent: *`: /zf_user/search/recruit, /zf_user/jobs/relay/view, view-detail, view-ajax 허용
    (불허: /zf_user/jobs/recent-contents/company, /zf_user/member/apply/, /zf_user/recruit/view/,
    /zf_user/company/, /zf_user/special-recruit/ 등). Claude-User 그룹 없음.
  * 목록: `GET /zf_user/search/recruit?searchword=<kw>&exp_cd=1&exp_none=y&recruitPage=<n>&recruitPageCount=100`
    → 서버 렌더 HTML(40건 ≈ 2.3MB, 100건 ≈ 2.7MB). `#recruit_info_list` 안의 `.item_recruit[value=rec_idx]` 카드.
      카드: h2.job_tit a[title,href], .job_date .date("~ 11/16(월)", "상시채용", "채용시"),
      .job_condition span들(지역 a 링크 / 경력 / 학력 / 고용형태), .job_sector(직무 태그, "수정일 26/11/02"),
      .corp_name a. 총 건수 "총 9,477건".
  * 경력 필터: 체크박스 `exp_cd=1`(신입), `exp_cd=2`(경력), `exp_none=y`(경력무관). exp_cd=1 결과에 이미
    경력무관·신입·경력 공고가 섞여 나온다(실측 100건: 경력무관 60, 신입·경력 28, 신입 11).
  * 페이지: `recruitPage`(1부터), `recruitPageCount=100` 동작 확인(기본 40).
  * 상세: `/zf_user/jobs/relay/view?rec_idx=` 는 껍데기(384KB)이고 내용은
      - `POST /zf_user/jobs/relay/view-ajax` (rec_idx) → `.jv_summary` dl(경력·학력·근무형태·자격요건·우대사항·
        급여·근무지역), `.info_period`(시작일/마감일 시각까지), `.jv_location[data-address]`
      - `GET /zf_user/jobs/relay/view-detail?rec_idx=` → 본문 iframe(`.user_content`). 통이미지 공고 많음.
  * 공식 API(oapi.saramin.co.kr/job-search, access-key 필요): 미검증(키 없음) — 공식 문서 형식대로 구현.
"""
from __future__ import annotations

import html as htmllib
import os
import re
from datetime import datetime
from urllib.parse import quote

from jobs_common import (KST, Blocked, make_opening, make_record, norm_text, now_kst)

NAME = "saramin"
LABEL = "사람인"
HOSTS = ("saramin.co.kr",)  # oapi.saramin.co.kr 포함(서브도메인)
KIND = "query"
DELAY = 1.2
BASE = "https://www.saramin.co.kr"
API = "https://oapi.saramin.co.kr/job-search"
PAGE_COUNT = 100
API_COUNT = 110  # 공식 문서상 최대 110

_EDU_RX = re.compile(r"학력|졸|고졸|대졸|석사|박사|↑|이상")
_CAREER_RX = re.compile(r"신입|경력|무관")


# ─────────────────────────────── 공통 ───────────────────────────────

def html_to_text(s: str) -> str:
    s = re.sub(r"<!--.*?-->", " ", s or "", flags=re.S)
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<(br|/p|/div|/li|/tr|/h\d|/dd|/dt|/table)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    lines = [norm_text(x) for x in s.split("\n")]
    return "\n".join(x for x in lines if x)


def _txt(s):
    return norm_text(htmllib.unescape(re.sub(r"<[^>]+>", " ", s or "")))


# ─────────────────────────────── 목록(HTML) ───────────────────────────────

def parse_list(html: str) -> dict:
    """검색 결과 HTML → {"items": [dict], "total": int|None}. 목록 영역이 없으면 RuntimeError."""
    i = html.find('id="recruit_info_list"')
    if i < 0:
        raise RuntimeError("recruit_info_list 없음(구조 변경 의심)")
    j = html.find('class="pagination"', i)
    seg = html[i: j if j > 0 else len(html)]
    tot = re.search(r"총\s*(?:<[^>]+>\s*)?([\d,]+)\s*(?:</[^>]+>\s*)?건", html)
    total = int(tot.group(1).replace(",", "")) if tot else None
    parts = re.split(r'<div class="item_recruit"\s+value="', seg)[1:]
    items = []
    for p in parts:
        m = re.match(r"(\d+)\"", p)
        if not m:
            continue
        rid = m.group(1)
        tm = re.search(r'<h2 class="job_tit">\s*<a[^>]*title="([^"]*)"[^>]*href="([^"]*)"', p, re.S)
        title = htmllib.unescape(tm.group(1)) if tm else None
        if not title:
            t2 = re.search(r'<h2 class="job_tit">(.*?)</h2>', p, re.S)
            title = _txt(t2.group(1)) if t2 else None
        corp = re.search(r'<strong class="corp_name">(.*?)</strong>', p, re.S)
        date = re.search(r'<span class="date">(.*?)</span>', p, re.S)
        cond = re.search(r'<div class="job_condition">(.*?)</div>', p, re.S)
        locs, career, edu, emp = [], None, None, []
        if cond:
            for sp in re.findall(r"<span>(.*?)</span>", cond.group(1), re.S):
                if "<a" in sp:
                    a_txt = [_txt(a) for a in re.findall(r"<a[^>]*>(.*?)</a>", sp, re.S)]
                    locs.append(" ".join(x for x in a_txt if x))
                    continue
                t = _txt(sp)
                if not t:
                    continue
                if "학력" in t or (_EDU_RX.search(t) and not _CAREER_RX.search(t)):
                    edu = edu or t
                elif _CAREER_RX.search(t) and career is None:
                    career = t
                else:
                    emp.append(t)
        sector = re.search(r'<div class="job_sector">(.*?)</div>', p, re.S)
        tags, day = [], None
        if sector:
            tags = [_txt(a) for a in re.findall(r"<a[^>]*>(.*?)</a>", sector.group(1), re.S)]
            dm = re.search(r'<span class="job_day">(.*?)</span>', sector.group(1), re.S)
            day = _txt(dm.group(1)) if dm else None
        items.append({
            "id": rid,
            "title": title,
            "company": _txt(corp.group(1)) if corp else None,
            "deadline_raw": _txt(date.group(1)) if date else None,
            "locations": [x for x in locs if x],
            "career": career,
            "edu": edu,
            "employment_type": ", ".join(emp) or None,
            "tags": [t for t in tags if t],
            "day": day,
            "paid": 'paid_fl=y' in p[:3000],
        })
    return {"items": items, "total": total}


def _posted_from_day(day):
    """"수정일 26/11/02" / "등록일 26/11/02" → (posted_at ISO|None, kind)."""
    if not day:
        return None
    m = re.search(r"(등록일|수정일)\s*(\d\d)/(\d\d)/(\d\d)", day)
    if not m:
        return None
    try:
        return datetime(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4)), tzinfo=KST).date().isoformat()
    except ValueError:
        return None


def to_record(it: dict, ref=None) -> dict:
    posted = _posted_from_day(it.get("day"))
    return make_record(
        source=NAME, source_id=it["id"], url=f"{BASE}/zf_user/jobs/relay/view?rec_idx={it['id']}",
        company=it.get("company"), title=it.get("title"),
        career=it.get("career"), edu=it.get("edu"), employment_type=it.get("employment_type"),
        locations=it.get("locations"), deadline_raw=it.get("deadline_raw"),
        posted_at=posted if (it.get("day") or "").startswith("등록일") else None,
        tags=it.get("tags"),
        extra={"list_day": it.get("day"), "paid": it.get("paid"), "roles_need_detail": True},
        ref=ref,
    )


def career_params(plan_career) -> dict:
    """plan["career"] → 사람인 경력 파라미터. 경력이 들어 있거나 비어 있으면 필터 없음."""
    wants = set(plan_career or [])
    if not wants or "경력" in wants:
        return {}
    return {"exp_cd": "1", "exp_none": "y"}


def search_url(keyword: str, cparams: dict, page: int, count=PAGE_COUNT) -> str:
    u = f"{BASE}/zf_user/search/recruit?searchword={quote(keyword)}"
    for k, v in cparams.items():
        u += f"&{k}={quote(v)}"
    u += f"&recruitPage={page}&recruitPageCount={count}"
    return u


# ─────────────────────────────── 목록(공식 API) — 미검증(키 없음) ───────────────────────────────

def parse_api(data: dict, ref=None) -> dict:
    """공식 API JSON → {"items": [record], "total": int|None}.

    미검증(키 없음): 형식은 공식 문서(oapi.saramin.co.kr/guide/job-search) 기준.
      {"jobs": {"count", "start", "total", "job": [{"id", "url", "active", "company": {"detail": {"name", "href"}},
        "position": {"title", "location": {"name"}, "job-type": {"name"}, "experience-level": {"code","min","max","name"},
        "required-education-level": {"name"}}, "keyword", "salary": {"name"},
        "posting-timestamp", "opening-timestamp", "expiration-timestamp", "close-type": {"code","name"}}]}}
      close-type code: 1 접수마감일, 2 채용시, 3 상시, 4 수시.
    """
    jobs = (data or {}).get("jobs")
    if not isinstance(jobs, dict):
        raise RuntimeError("API 응답에 jobs 없음")
    out = []
    for j in jobs.get("job") or []:
        pos = j.get("position") or {}
        comp = ((j.get("company") or {}).get("detail") or {})
        close = (j.get("close-type") or {})
        exp_ts = j.get("expiration-timestamp")
        code = str(close.get("code") or "")
        if code == "2":
            dl, kind, prec, raw = None, "채용시", None, close.get("name") or "채용시"
        elif code in ("3", "4"):
            dl, kind, prec, raw = None, "상시", None, close.get("name") or "상시"
        elif exp_ts:
            dt = datetime.fromtimestamp(int(exp_ts), KST)
            dl, kind, prec, raw = dt.isoformat(timespec="minutes"), "date", "minute", dt.isoformat(timespec="minutes")
        else:
            dl, kind, prec, raw = None, "unknown", None, None
        loc = htmllib.unescape(((pos.get("location") or {}).get("name") or ""))
        locs = [norm_text(x.replace(">", " ")) for x in loc.split(",") if x.strip()]
        open_ts = j.get("opening-timestamp") or j.get("posting-timestamp")
        out.append(make_record(
            source=NAME, source_id=j.get("id"), url=f"{BASE}/zf_user/jobs/relay/view?rec_idx={j.get('id')}",
            company=comp.get("name"), title=htmllib.unescape(pos.get("title") or ""),
            career=(pos.get("experience-level") or {}).get("name"),
            edu=(pos.get("required-education-level") or {}).get("name"),
            employment_type=(pos.get("job-type") or {}).get("name"),
            locations=locs, deadline_raw=raw, deadline=dl, deadline_kind=kind, deadline_precision=prec,
            posted_at=datetime.fromtimestamp(int(open_ts), KST).isoformat(timespec="minutes") if open_ts else None,
            salary_raw=(j.get("salary") or {}).get("name"),
            tags=[t for t in (j.get("keyword") or "").split(",") if t.strip()],
            extra={"via": "api", "active": j.get("active"), "close_type": close.get("name"),
                   "roles_need_detail": True},
            ref=ref,
        ))
    total = jobs.get("total")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return {"items": out, "total": total}


def _api_page(f, key, keyword, cparams, page):
    params = {"access-key": key, "keywords": keyword, "start": page - 1, "count": API_COUNT}
    if cparams:
        params["exp_cd"] = "1"  # 미검증(키 없음): 문서상 1=신입. 경력무관 코드는 문서 확인 필요
    try:
        return f.get_json(API, params=params, headers={"Accept": "application/json"})
    except Blocked as e:
        raise Blocked(f"사람인 API 차단/인증 실패({str(e)[:40]})") from None
    except Exception as e:  # 예외 문자열에 키가 섞이지 않게 형식만 남긴다
        raise RuntimeError(f"사람인 API 실패({type(e).__name__})") from None


# ─────────────────────────────── list_postings ───────────────────────────────

def list_postings(f, plan):
    keywords = [k for k in (plan.get("keywords") or []) if str(k).strip()]
    if not keywords:
        raise RuntimeError("키워드형 소스인데 plan.keywords가 비어 있음")
    max_pages = int(plan.get("max_pages") or 5)
    cparams = career_params(plan.get("career"))
    key = os.environ.get("SARAMIN_ACCESS_KEY") or None
    via = "api" if key else "html"
    now = now_kst()
    now_iso = now.isoformat(timespec="minutes")
    recs, seen = [], set()
    pages = dups = expired = reported = 0
    errors, capped, blocked = [], 0, False
    per_page = API_COUNT if key else PAGE_COUNT
    for ki, kw in enumerate(keywords, 1):
        kw_seen = set()  # 이 키워드에서 본 id — 페이지 종료 판단용(키워드 간 중복과 분리)
        for page in range(1, max_pages + 1):
            try:
                if key:
                    parsed = parse_api(_api_page(f, key, kw, cparams, page), ref=now)
                    page_recs = parsed["items"]
                else:
                    html = f.get(search_url(kw, cparams, page), headers={"Referer": f"{BASE}/"})
                    parsed = parse_list(html)
                    page_recs = [to_record(it, ref=now) for it in parsed["items"]]
            except Blocked as e:
                if not recs:
                    raise
                errors.append(f"키워드#{ki} p{page}: 차단 — {e}")
                blocked = True
                break
            except Exception as e:
                # 예외 문자열의 URL(검색어·키 포함 가능)은 지운다 — manifest에 검색어를 남기지 않는다
                msg = re.sub(r"https?://\S+|stext=\S+|searchword=\S+|access-key=\S+", "<url>", str(e))
                errors.append(f"키워드#{ki} p{page}: {type(e).__name__}: {msg}"[:200])
                break
            pages += 1
            if page == 1 and parsed["total"] is not None:
                reported += parsed["total"]
            new = 0
            kw_new = 0
            for rec in page_recs:
                sid = rec["source_id"]
                if sid not in kw_seen:
                    kw_seen.add(sid)
                    kw_new += 1
                if sid in seen:
                    dups += 1
                    continue
                seen.add(sid)
                new += 1
                if rec["deadline"] and rec["deadline"] < now_iso:
                    expired += 1
                    continue
                recs.append(rec)
            total = parsed["total"]
            if kw_new == 0 or not page_recs or (total is not None and page * per_page >= total):
                break
            if page == max_pages:
                capped += 1
        if blocked:
            break
    partial = bool(capped) or blocked
    stop = "blocked" if blocked else ("page-cap" if capped else "exhausted")
    scope = (f"키워드 {len(keywords)}개 × 최대 {max_pages}p({per_page}건/p, {'공식 API' if key else 'HTML 검색'}), "
             + ("신입·경력무관 필터" if cparams else "경력 필터 없음")
             + (f", 페이지 캡 {capped}개 키워드" if capped else "")
             + (f", 마감 지남 {expired}건 제외" if expired else ""))
    return {"records": recs, "pages": pages, "reported_total": reported, "duplicates": dups,
            "stop_reason": stop, "errors": errors, "partial": partial, "scope": scope, "via": via}


# ─────────────────────────────── 상세 ───────────────────────────────

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
    out = {}
    for name, rx in SECTION_HEADS.items():
        m = re.search(r"^[^\w\n]{0,3}(?:" + rx + r")[^\w\n]{0,3}[:：]?[ \t]*(.*)$", text, re.M)
        if not m:
            continue
        nxt = _ANY_HEAD.search(text, m.end())
        body = text[m.start(1): nxt.start() if nxt else len(text)].strip()
        if body:
            out[name] = body[:limit]
    return out


def parse_summary(ajax_html: str) -> dict:
    """view-ajax HTML → {"summary": {dt: dd}, "start", "end", "address"}."""
    out = {"summary": {}, "start": None, "end": None, "address": None}
    if not ajax_html:
        return out
    i = ajax_html.find("jv_summary")
    if i >= 0:
        j = ajax_html.find('class="jv_cont', i + 20)
        seg = ajax_html[i: j if j > 0 else i + 30000]
        seg = re.sub(r"<button\b.*?</button>", " ", seg, flags=re.S)
        for dt, dd in re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", seg, re.S):
            k = _txt(dt)
            v = html_to_text(dd).replace("\n", " ").strip()
            if k and v and k not in out["summary"]:
                out["summary"][k] = v
    per = re.search(r'<dl class="info_period">(.*?)</dl>', ajax_html, re.S)
    if per:
        pairs = dict((_txt(a), _txt(b)) for a, b in re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>",
                                                                per.group(1), re.S))
        out["start"], out["end"] = pairs.get("시작일"), pairs.get("마감일")
    loc = re.search(r'class="jv_cont jv_location"[^>]*data-address="([^"]*)"', ajax_html)
    if loc:
        out["address"] = htmllib.unescape(loc.group(1)).strip() or None
    return out


def parse_body(detail_html: str) -> dict:
    """view-detail(본문 iframe) HTML → {"text", "images", "roles"}."""
    m = re.search(r'<div class="user_content">(.*)</div>\s*(?:<script|</body>)', detail_html or "", re.S)
    body = m.group(1) if m else (detail_html or "")
    roles = [_txt(r) for r in re.findall(r'<p class="job-title">(.*?)</p>', body, re.S)]
    return {"text": html_to_text(body), "images": len(re.findall(r"<img\b", body, re.I)),
            "roles": [r for r in roles if r], "body_found": bool(m)}


def parse_detail(ajax_html: str | None, detail_html: str | None) -> dict:
    sm = parse_summary(ajax_html or "")
    bd = parse_body(detail_html or "")
    text = bd["text"]
    # 템플릿 이미지·빈 placeholder(blank.png)는 '이미지 본문'으로 세지 않는다
    real_imgs = len([1 for s in re.findall(r'<img\b[^>]*src="([^"]*)"', detail_html or "", re.I)
                     if "blank.png" not in s])
    sections = split_sections(text)
    s = sm["summary"]
    q = " / ".join(f"{k}: {s[k]}" for k in ("경력", "학력") if s.get(k))
    # 본문에 섹션이 있으면 본문 것을, 없으면 요약(view-ajax) 것을 쓴다. 경력·학력 요약은 항상 앞에 붙인다.
    body_q = sections.get("자격요건") or s.get("자격요건")
    if q or body_q:
        sections["자격요건"] = "\n".join(x for x in (q, body_q) if x)
    if not sections.get("우대사항") and s.get("우대사항"):
        sections["우대사항"] = s["우대사항"]
    if sm["address"] or s.get("근무지역"):
        sections["근무지"] = sm["address"] or s.get("근무지역")
    if sm["start"] or sm["end"]:
        sections["접수기간"] = f"{sm['start'] or ''} ~ {sm['end'] or ''}".strip()
    deadline_raw = sm["end"]
    career = s.get("경력")
    openings = [make_opening(role=r, career=career, locations=s.get("근무지역")) for r in bd["roles"]]
    return {
        "openings": openings,
        "text": text,
        "text_incomplete": (len(text) < 200 and real_imgs > 0) or not text,
        "images": real_imgs,
        "employer_url": None,  # 홈페이지 지원 링크는 JS로 채워져 정적 HTML에 없다
        "deadline_raw": deadline_raw,
        "posted_at": sm["start"],
        "edu": s.get("학력"),
        "career_raw": career,
        "sections": sections,
    }


def fetch_detail(f, rec):
    rid = str(rec["source_id"])
    if not rid.isdigit():
        raise RuntimeError("rec_idx 형식 아님")
    view = f"{BASE}/zf_user/jobs/relay/view?rec_idx={rid}"
    detail_html = f.get(f"{BASE}/zf_user/jobs/relay/view-detail?rec_idx={rid}", headers={"Referer": view})
    ajax_html = None
    try:
        ajax_html = f.request("POST", f"{BASE}/zf_user/jobs/relay/view-ajax",
                              data={"rec_idx": rid, "rec_seq": "0", "view_type": "search"},
                              headers={"Referer": view, "X-Requested-With": "XMLHttpRequest"})
    except Blocked:
        raise
    except Exception:  # 요약 실패 → 본문만 반환
        ajax_html = None
    return parse_detail(ajax_html, detail_html)

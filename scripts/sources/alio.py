"""잡알리오(job.alio.go.kr, 공공기관 채용정보시스템) 어댑터 — 공공기관 채용 전수.

실측(2026-11-03):
  * robots.txt 404 → 제한 없음.
  * 목록: `GET /recruit.do` (폼은 POST지만 GET 쿼리도 같은 결과). UTF-8 서버 렌더링 HTML.
      pageNo=N, pageSet=10|20|30|40|50(페이지당 건수), ing=2(진행중)/3(마감),
      s_date/e_date=YYYY.MM.DD(**등록일** 범위 — 비우면 최근 2개월만 나온다. 등록이 오래된
      상시·장기 공고까지 받으려면 s_date를 과거로 넓혀야 한다: 2개월 394건 vs 전체 512건),
      order=REG_DATE|TERM_END, sort=DESC|ASC,
      career=R2010(신입)/R2020(경력)/R2030(신입+경력)/R2040(외국인) — '포함' 검색이라 R2010이
        신입+경력 공고도 돌려준다(R2010 483건 = R2010+R2030 483건),
      work_type=R1010 정규직/R1030 무기계약직/R1040 비정규직/R1060 청년인턴(체험형)/R1070 청년인턴(채용형),
      education=R7010~R7070(+eduType=single|multi 필수), location=R30xx, org_type=A200x, keyword.
    행 컬럼: 체크박스(idxs=idx) | 번호(역순, 1페이지 첫 행 = 총건수) | 채용제목(a href=/recruitview.do?idx=)
      | 기관명 | 근무지("서울 외 1") | 고용형태("비정규직 외 1") | 등록일(YYYY.MM.DD) | 마감일("26.11.10" + D-n) | 상태.
    목록에는 채용구분(신입/경력)·학력 컬럼이 없다 → 상세에서 채운다.
  * 상세: `GET /recruitview.do?idx=<id>`
      - .topInfo h2(기관명), p.titleH2(제목), p.infoLink a(기관 원 공고 URL)
      - .detailTxt 표 th/td: 표준직무(NCS), 학력정보, 근무분야, 채용구분, 고용형태, 대체인력여부, 근무지,
        채용인원, 우대조건, 채용기간("26.11.03 ~ 26.11.10"), 등록일
      - #tab-1 h4/p: 응시자격, 결격사유, 우대내용, 전형절차/방법 — 대개 요약이고 "첨부 공고문 참조"
      - #tab-2 .interviewName: 직렬(모집 단위) 이름 목록
      - #tab-3 표: 공고문 / 입사지원서 / 직무기술서 / 기타 첨부파일 — www.alio.go.kr/download/download.json?fileNo=
        (링크만 기록, 내려받지 않는다)
  * 선택: 공공데이터포털 API `https://apis.data.go.kr/1051000/recruitment/list`
      (serviceKey, resultType=json, pageNo, numOfRows, ongoingYn=Y, recrutSe=R2010 ...).
      키(DATA_GO_KR_KEY 환경변수 → ~/.claude/skills/ir-search/.env → ~/.config/ir-search/data_go_kr_key)가 있고
      실제로 통할 때만 쓴다. 미등록 키(HTTP 403, SERVICE_KEY_IS_NOT_REGISTERED_ERROR / reasonCode 30)나
      어떤 오류든 로그 한 줄 남기고 HTML로 돌아간다. 키는 출력·저장하지 않는다.
      ※ 2026-11-03 현재 사용자 키가 이 API에 미등록이라 응답 구조·sn↔idx 대응은 실측 못 했다(필드명은 API 문서 기준).
"""
from __future__ import annotations

import html as _html
import os
import re

from jobs_common import (Fetcher, is_expired, log, make_opening, make_record, norm_text, now_kst)

NAME = "alio"
LABEL = "잡알리오(공공기관)"
HOSTS = ("job.alio.go.kr",)
KIND = "full"
PLAN_KEYS = ("career", "days_ahead", "page_cap")  # 키워드는 이 소스의 결과에 영향이 없다
DELAY = 1.0
BASE = "https://job.alio.go.kr"
PAGE_SET = 50
MAX_PAGES = 30          # 50건 × 30p = 1,500건. 실측 진행중 공고는 500건 안팎
S_DATE_FLOOR = "2000.01.01"  # 등록일 하한 — 등록이 오래된 상시 공고까지 포함

CAREER_FILTER_SINNIP = "R2010"  # 신입(신입+경력 포함)

API_HOST = "apis.data.go.kr"   # API 경로 전용 — 그 경로에서만 별도 Fetcher로 허용한다
API_LIST = f"https://{API_HOST}/1051000/recruitment/list"
API_ROWS = 100
API_MAX_PAGES = 20


# ─────────────────────────────── 파서 ───────────────────────────────

def _cell_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", x).strip() for x in s.split("\n")]
    return "\n".join(x for x in lines if x)


def _split_more(v: str):
    """'서울 외 1' → ('서울', 1). '서울,충북' → 그대로."""
    v = norm_text(v)
    m = re.match(r"^(.*?)\s*외\s*(\d+)$", v)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return v, 0


def parse_list(html: str) -> dict:
    """목록 HTML → {"rows": [...], "total": int|None, "last_page": int|None}.

    rows: idx, title, org, location, location_more, employment_type, employment_more, posted(YYYY.MM.DD),
          deadline_raw("26.11.10"), dday, status("진행중"/"마감")
    """
    rows = []
    tbody = re.search(r'<table class="tbl type_03">.*?<tbody>(.*?)</tbody>', html, re.S)
    body = tbody.group(1) if tbody else html
    for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        m = re.search(r"recruitview\.do\?idx=(\d+)", tr)
        if len(tds) < 9 or not m:
            continue
        title_m = re.search(r"recruitview\.do\?idx=\d+\"[^>]*>(.*?)</a>", tds[2], re.S)
        loc, loc_more = _split_more(_cell_text(tds[4]).replace("\n", " "))
        emp, emp_more = _split_more(_cell_text(tds[5]).replace("\n", " "))
        dl_lines = _cell_text(tds[7]).split("\n")
        dday = next((x for x in dl_lines if re.match(r"D-\s*\d+|D-day|오늘", x, re.I)), None)
        rows.append({
            "idx": m.group(1),
            "no": norm_text(_cell_text(tds[1])),
            "title": norm_text(_cell_text(title_m.group(1) if title_m else tds[2])),
            "org": norm_text(_cell_text(tds[3])),
            "location": loc,
            "location_more": loc_more,
            "employment_type": emp,
            "employment_more": emp_more,
            "posted": norm_text(_cell_text(tds[6])),
            "deadline_raw": dl_lines[0] if dl_lines else None,
            "dday": dday,
            "status": norm_text(_cell_text(tds[8])),
        })
    total = None
    if rows and rows[0]["no"].isdigit():
        total = int(rows[0]["no"])  # 번호는 역순 → 1페이지 첫 행 = 전체 건수
    lp = re.search(r"goPage\((\d+)\);\s*return false;\"\s*class=\"end\"", html)
    return {"rows": rows, "total": total, "last_page": int(lp.group(1)) if lp else None}


def _alio_date(s):
    """'2026.11.03' → '2026-11-03'."""
    m = re.search(r"(20\d\d)\.(\d\d)\.(\d\d)", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def row_to_record(row: dict, career_filter: str | None = None, ref=None) -> dict:
    loc = row.get("location") or ""
    locations = [x for x in re.split(r"[,/]", loc) if x.strip()] if loc else []
    emp = row.get("employment_type") or None
    career = "신입" if career_filter == CAREER_FILTER_SINNIP else None
    tags = []
    if emp and "인턴" in emp:
        tags.append(emp)
    return make_record(
        source=NAME, source_id=row["idx"], url=f"{BASE}/recruitview.do?idx={row['idx']}",
        company=row.get("org"), title=row.get("title"),
        career=career, employment_type=emp, locations=locations,
        deadline_raw=row.get("deadline_raw"), posted_at=_alio_date(row.get("posted")),
        company_type="공공기관", tags=tags, ref=ref,
        extra={"status": row.get("status"), "dday": row.get("dday"),
               "location_more": row.get("location_more") or 0,
               "employment_more": row.get("employment_more") or 0,
               "career_filter": (f"{career_filter}(신입 포함, 신입+경력 공고도 포함)" if career_filter else None),
               "career_needs_detail": True},
    )


def parse_detail(html: str) -> dict:
    """상세 HTML → fields(표), sections(응시자격 등), roles(직렬), attachments, employer_url, title, org."""
    out = {"fields": {}, "sections": {}, "roles": [], "attachments": [], "employer_url": None,
           "title": None, "org": None}
    m = re.search(r'<div class="topInfo">\s*<h2>(.*?)</h2>', html, re.S)
    if m:
        out["org"] = norm_text(_cell_text(m.group(1)))
    m = re.search(r'<p class="titleH2"[^>]*>(.*?)</p>', html, re.S)
    if m:
        out["title"] = norm_text(_cell_text(m.group(1)))
    m = re.search(r'<p class="infoLink">.*?<a href="([^"]+)"', html, re.S)
    if m and m.group(1).startswith("http"):
        out["employer_url"] = _html.unescape(m.group(1)).strip()
    m = re.search(r'<div class="detailTxt">(.*?)</table>', html, re.S)
    if m:
        for th, td in re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", m.group(1), re.S):
            out["fields"][norm_text(_cell_text(th))] = _cell_text(td)
    m = re.search(r'<div id="tab-1"[^>]*>(.*?)</div>', html, re.S)
    if m:
        for h, p in re.findall(r"<h4>(.*?)</h4>\s*<p>(.*?)</p>", m.group(1), re.S):
            out["sections"][norm_text(_cell_text(h))] = _cell_text(p)
    m = re.search(r'<div id="tab-2"[^>]*>(.*?)<div id="tab-3"', html, re.S)
    if m:
        seen = set()
        for r in re.findall(r'class="interviewName">(?:<span></span>)?(.*?)</th>', m.group(1), re.S):
            r = norm_text(_cell_text(r))
            if r and r not in seen:
                seen.add(r)
                out["roles"].append(r)
    m = re.search(r'<div id="tab-3"[^>]*>(.*?)</table>', html, re.S)
    if m:
        for th, td in re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", m.group(1), re.S):
            kind = norm_text(_cell_text(th))
            if kind == "미첨부사유":
                reason = norm_text(_cell_text(td))
                if reason:
                    out["fields"]["미첨부사유"] = reason
                continue
            for href, name in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', td, re.S):
                out["attachments"].append({"kind": kind, "name": norm_text(_cell_text(name)),
                                           "url": _html.unescape(href).strip()})
    return out


def _detail_text(d: dict) -> str:
    parts = [f"{k}: {v}" for k, v in d["fields"].items()]
    parts += [f"[{k}]\n{v}" for k, v in d["sections"].items()]
    if d["roles"]:
        parts.append("[모집 직렬] " + ", ".join(d["roles"]))
    return "\n".join(parts)


# ─────────────────────────────── 수집 ───────────────────────────────

def _wants_sinnip_filter(plan) -> bool:
    career = [c for c in (plan.get("career") or []) if c]
    if not career:
        return False
    return any(c in ("신입", "인턴") for c in career) and "경력" not in career


def _load_api_key():
    k = os.environ.get("DATA_GO_KR_KEY")
    if k and k.strip():
        return k.strip()
    env = os.path.expanduser("~/.claude/skills/ir-search/.env")
    try:
        with open(env, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"\s*(?:export\s+)?DATA_GO_KR_KEY\s*=\s*['\"]?([^'\"\s#]+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    try:
        with open(os.path.expanduser("~/.config/ir-search/data_go_kr_key"), encoding="utf-8") as fh:
            k = fh.read().strip()
            return k or None
    except OSError:
        return None


def _mask(msg, key):
    msg = str(msg)
    if key:
        from urllib.parse import quote
        for v in {key, quote(key, safe=""), quote(key)}:
            msg = msg.replace(v, "***")
    return msg


def _api_items(data):
    """응답에서 공고 목록과 총건수를 찾는다(문서상 result/items 어느 쪽이든 받는다)."""
    if not isinstance(data, dict):
        return None, None
    hdr = data.get("OpenAPI_ServiceResponse")
    if hdr:  # 게이트웨이 오류 응답
        raise RuntimeError((hdr.get("cmmMsgHeader") or {}).get("errMsg") or "OpenAPI 오류")
    body = data.get("response", {}).get("body") if isinstance(data.get("response"), dict) else data
    total = body.get("totalCount")
    for key in ("result", "items", "item", "data"):
        v = body.get(key)
        if isinstance(v, dict):
            v = v.get("item", v)
        if isinstance(v, list):
            return v, total
    return None, total


def api_item_to_record(it: dict, ref=None):
    sn = it.get("recrutPblntSn") or it.get("sn")
    if not sn:
        return None
    end = it.get("pbancEndYmd") or ""
    end_raw = f"{end[:4]}.{end[4:6]}.{end[6:8]}" if re.fullmatch(r"\d{8}", end) else end
    bgn = it.get("pbancBgngYmd") or ""
    return make_record(
        source=NAME, source_id=sn, url=f"{BASE}/recruitview.do?idx={sn}",
        company=it.get("instNm"), title=it.get("recrutPbancTtl"),
        career=it.get("recrutSeNm"), edu=it.get("acbgCondNmLst"),
        employment_type=it.get("hireTypeNmLst"),
        locations=[x for x in re.split(r"[,/]", it.get("workRgnNmLst") or "") if x.strip()],
        deadline_raw=end_raw or None,
        posted_at=(f"{bgn[:4]}-{bgn[4:6]}-{bgn[6:8]}" if re.fullmatch(r"\d{8}", bgn) else None),
        company_type="공공기관", ref=ref,
        extra={"via": "data.go.kr", "ncs": it.get("ncsCdNmLst"), "src_url": it.get("srcUrl"),
               "aply_qlfc": (it.get("aplyQlfcCn") or "")[:2000] or None, "sn_is_idx_unverified": True},
    )


def _list_via_api(key, career_filter, now):
    af = Fetcher((API_HOST,), delay=0.5)
    recs, seen, pages, total = [], set(), 0, None
    for page in range(1, API_MAX_PAGES + 1):
        params = {"serviceKey": key, "resultType": "json", "pageNo": str(page),
                  "numOfRows": str(API_ROWS), "ongoingYn": "Y"}
        if career_filter:
            params["recrutSe"] = career_filter
        data = af.get_json(API_LIST, params=params)
        items, tot = _api_items(data)
        if items is None:
            raise RuntimeError("API 응답 구조 미확인")
        pages += 1
        total = tot if tot is not None else total
        for it in items:
            rec = api_item_to_record(it, ref=now)
            if not rec or rec["source_id"] in seen or is_expired(rec, now):
                continue
            seen.add(rec["source_id"])
            recs.append(rec)
        if len(items) < API_ROWS:
            break
    else:
        raise RuntimeError("API 페이지 캡")
    if not recs:
        raise RuntimeError("API 0건")
    return {"records": recs, "pages": pages, "reported_total": int(total) if total else None,
            "duplicates": None, "stop_reason": "end", "errors": [], "partial": False,
            "scope": "전수(data.go.kr API, 진행중" + (", 신입 필터 R2010" if career_filter else "") + ")"}


def list_postings(f, plan):
    """진행중 공고 전수(등록일 무제한). plan.career가 신입/인턴이면 서버 측 신입 필터(R2010) 적용."""
    now = now_kst()
    career_filter = CAREER_FILTER_SINNIP if _wants_sinnip_filter(plan) else None
    key = None if plan.get("alio_html_only") else _load_api_key()
    if key:
        try:
            return _list_via_api(key, career_filter, now)
        except Exception as e:  # Blocked(403 미등록 키) 포함 — HTML로 돌아간다
            log(f"[alio] data.go.kr API 사용 불가({type(e).__name__}: {_mask(e, key)[:80]}) → HTML 목록으로 수집")
    params = {"ing": "2", "pageSet": str(PAGE_SET), "order": "REG_DATE", "sort": "DESC",
              "s_date": S_DATE_FLOOR, "e_date": now.strftime("%Y.%m.%d")}
    if career_filter:
        params["career"] = career_filter
    recs, seen, errors = [], set(), []
    total, last_page, pages, expired, stop = None, None, 0, 0, "end"
    page = 1
    while True:
        if page > MAX_PAGES:
            stop = "page-cap"
            break
        try:
            html = f.get(f"{BASE}/recruit.do", params=dict(params, pageNo=str(page)),
                         headers={"Referer": f"{BASE}/recruit.do"})
        except Exception as e:
            if page == 1:
                raise
            errors.append(f"p{page}: {type(e).__name__}: {e}")
            stop = "error"
            break
        pages += 1
        parsed = parse_list(html)
        if page == 1:
            total, last_page = parsed["total"], parsed["last_page"]
            if not parsed["rows"] and "recruitview.do" not in html and "tbl type_03" not in html:
                raise RuntimeError("목록 표 없음(구조 변경 의심)")
        rows = parsed["rows"]
        for row in rows:
            if row["idx"] in seen:
                continue
            seen.add(row["idx"])
            if row.get("status") and "마감" in row["status"]:
                expired += 1
                continue
            rec = row_to_record(row, career_filter, ref=now)
            if is_expired(rec, now):
                expired += 1
                continue
            recs.append(rec)
        if len(rows) < PAGE_SET or (last_page and page >= last_page):
            break
        page += 1
    partial = stop in ("page-cap", "error")
    if total is not None and len(seen) < total and stop == "end":
        # 수집 중 신규 등록으로 행이 밀렸을 수 있다 — 조용히 전수로 보고하지 않는다
        errors.append(f"고유 {len(seen)}건 < 사이트 총 {total}건(수집 중 목록 변동 가능)")
    scope = (f"전수(진행중, 등록일 {S_DATE_FLOOR}~오늘" + (", 서버 신입 필터 R2010" if career_filter else "")
             + f", {PAGE_SET}건×{pages}p" + (f", 마감 {expired}건 제외" if expired else "") + ")")
    return {"records": recs, "pages": pages, "reported_total": total, "duplicates": None,
            "stop_reason": stop, "errors": errors, "partial": partial, "scope": scope}


def fetch_detail(f, rec):
    html = f.get(f"{BASE}/recruitview.do", params={"idx": rec["source_id"]},
                 headers={"Referer": f"{BASE}/recruit.do"})
    d = parse_detail(html)
    if not d["fields"] and not d["title"]:
        raise RuntimeError("상세 표 없음(구조 변경 또는 삭제된 공고)")
    fl = d["fields"]
    career = fl.get("채용구분")
    emp = fl.get("고용형태")
    locs = [x.strip() for x in re.split(r"[,/]", fl.get("근무지") or "") if x.strip()]
    roles = d["roles"] or [fl.get("근무분야") or d["title"] or rec.get("title")]
    openings = [make_opening(role=r, career=career, locations=locs, employment_type=emp,
                             headcount=(fl.get("채용인원") if len(roles) == 1 else None),
                             extra={"ncs": fl.get("표준직무(NCS)"), "field": fl.get("근무분야"),
                                    "edu": fl.get("학력정보")})
                for r in roles]
    text = _detail_text(d)
    body = "\n".join(d["sections"].values())
    refers_attachment = bool(re.search(r"(첨부|공고문).{0,12}(참조|참고|확인)", body))
    has_notice = any(a["kind"] == "공고문" for a in d["attachments"])
    sections = dict(d["sections"])
    sections.update({k: v for k, v in {"근무지": fl.get("근무지"), "학력": fl.get("학력정보"),
                                       "채용구분": career, "고용형태": emp,
                                       "채용인원": fl.get("채용인원"), "우대조건": fl.get("우대조건"),
                                       "대체인력여부": fl.get("대체인력여부")}.items() if v})
    return {
        "openings": openings,
        "text": text,
        # 공공기관 요건(전공·자격·어학·가점)은 거의 항상 첨부 공고문(PDF/HWP)에만 있다
        "text_incomplete": len(body) < 400 or (refers_attachment and has_notice),
        "employer_url": d["employer_url"],
        "deadline_raw": fl.get("채용기간"),
        "posted_at": _alio_date(fl.get("등록일")),
        "attachments": d["attachments"],
        "sections": sections,
        "edu": fl.get("학력정보"),
        "career_raw": career,
        "employment_type": emp,
    }

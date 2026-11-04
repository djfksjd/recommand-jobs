"""고용24(work24.go.kr, 옛 워크넷) 어댑터 — 키워드 검색 + 주요 기업 공채속보 전수.

실측(2026-11-03):
  * robots.txt `User-agent: *` 불허는 /cm/common/, /sa/, /ei/, /cm/f/c/0100/selectUnifySearchPost.do 뿐 → /wk/ 허용.
  * 검색 목록: `GET /wk/a/b/1200/retriveDtlEmpSrchList.do` (폼은 POST지만 GET 쿼리 결과가 같다). UTF-8.
      srcKeyword=검색어, keywordWantedTitle=Y(제목에서만 검색. 미지정 시 본문 전체라 '전기' 5,646건 vs 제목 1,016건),
      careerTypes=N(신입),E(경력),Z(관계없음) 쉼표 결합, academicGbn=05(대졸4년),04,03,00(학력무관)... 쉼표 결합,
      sortField=DATE&sortOrderBy=DESC(최신), resultCnt=10|30|50, pageIndex=N,
      siteClcd=all|WORK(고용24 인증)|OEW(공채속보)|ALI(알리오)|GOJ(나라일터)|CJK|CSI|CIN(민간 사이트 연계)...
      keyword 파라미터는 무시된다(srcKeyword가 실제 검색어).
    행: `<tr id="listN">` — 회사(a.cp_name), 규모 라벨(span.tbl_label), 제목(a[data-emp-detail], 검색어는
      <strong>으로 강조), 상세 href(wantedAuthNo·infoTypeCd·infoTypeGroup), 정보제공처(img alt),
      li.dollar 임금, li.member 경력·학력, li.time 근무형태, li.site 지역, "마감일 : YYYY-MM-DD",
      "등록일 : YYYY-MM-DD", 인라인 스크립트 wantedYn='Y'이면 화면 표시는 '채용시까지'(마감일은 게시 종료일).
  * 공채속보(주요 기업 공채): 같은 목록에 siteClcd=OEW → 259건(2026-11-03). 테마 페이지
      `/wk/a/b/1700/themeEmpInfoSrchList.do?thmaHrplCd=S00074` 와 같은 공고(1페이지 10건 id 일치)인데,
      테마 페이지는 GET에서 resultCnt가 먹지 않아 10건/페이지 → 요청 수를 줄이려 OEW 필터를 쓰고,
      OEW가 0건이면 테마 페이지로 대신 받는다.
  * 상세: `GET /wk/a/b/1500/empDetailAuthView.do?wantedAuthNo=..&infoTypeCd=..&infoTypeGroup=..`
      (세 파라미터가 다 있어야 한다 — wantedAuthNo만 주면 1KB 빈 응답)
      - 고용24 인증(VALIDATION/tb_workinfoworknet): 직무내용, th/td 표(모집 인원·모집 직종·경력·학력·자격 면허·
        고용 형태·임금 조건·근무 예정지·전공·외국어 능력·우대조건...), 전형방법(li.disable=미제공),
        접수 마감일("채용시까지 (마감시간 : 24시)"), 접수 방법, 제출 서류, '채용 담당자' 블록(개인 이름·전화 — 버린다)
      - 공채속보(OEW/tb_workinfoopen): 고용형태, 공통사항(지원자격), 채용부문 표(채용부문·모집분야·담당업무·
        지원자격·근무지), 전형방법(ul.emp_model_step), 접수 기간, 공채사이트 바로가기(기업 원 채용페이지),
        문의사항(담당자 연락처 — 버린다)
  * 공식 Open API는 기업회원 전용이라 개인 키를 받을 수 없다 → 구현하지 않는다(HTML만).
"""
from __future__ import annotations

import html as _html
import re

from jobs_common import (Blocked, RobotsDisallowed, is_expired, make_opening, make_record, norm_text, now_kst)

NAME = "work24"
LABEL = "고용24"
HOSTS = ("www.work24.go.kr",)
KIND = "query"
DELAY = 1.0
BASE = "https://www.work24.go.kr"
LIST_URL = f"{BASE}/wk/a/b/1200/retriveDtlEmpSrchList.do"
THEME_URL = f"{BASE}/wk/a/b/1700/themeEmpInfoSrchList.do"
THEME_GONGCHAE = "S00074"
DETAIL_URL = f"{BASE}/wk/a/b/1500/empDetailAuthView.do"
RESULT_CNT = 50
DEFAULT_MAX_PAGES = 3
GONGCHAE_MAX_PAGES = 20      # 50건 × 20p
THEME_MAX_PAGES = 60         # 테마 페이지 대체 경로(10건/페이지)

CAREER_CODES = {"신입": "N", "인턴": "N", "경력": "E"}
SIZE_LABELS = ("대기업", "중견", "중소", "강소", "공무원/공기업/공공기관", "공공기관", "공기업", "외국계")

_PHONE = re.compile(r"(?<!\d)(0\d{1,2}[-.\s)]\s?\d{3,4}[-.\s]\d{4}|01[016789][-.\s]?\d{3,4}[-.\s]?\d{4})(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


# ─────────────────────────────── 공통 ───────────────────────────────

def _txt(s: str) -> str:
    """HTML 조각 → 줄 단위 텍스트. 인라인 강조(<strong>)는 공백 없이 붙인다."""
    s = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "", s or "", flags=re.S)
    s = re.sub(r"</?(strong|em|b|span|i)\b[^>]*>", "", s, flags=re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</tr>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", x).strip() for x in s.split("\n")]
    return "\n".join(x for x in lines if x)


_UI_NOISE = re.compile(r"^(더보기|접기|지도 보기|길찾기|도움말|닫기)$")


def _clean(v: str | None) -> str | None:
    """UI 버튼 문구(더보기·지도 보기 등) 줄 제거."""
    if not v:
        return v
    v = re.sub(r"\s*(지도 보기|길찾기)\s*", " ", v)
    return "\n".join(x for x in v.split("\n") if x.strip() and not _UI_NOISE.match(x.strip())).strip()


def _career_label(v: str | None) -> str | None:
    """고용24 상세의 '관계없음'은 경력무관이다."""
    if not v:
        return v
    return "경력무관" if norm_text(v) == "관계없음" else v


def _one(s: str) -> str:
    return norm_text(_txt(s).replace("\n", " "))


def scrub(s: str | None) -> str | None:
    """개인 연락처(전화·이메일) 제거 — 상세 텍스트에 담당자 연락처가 섞여 들어온다."""
    if not s:
        return s
    return _EMAIL.sub("[이메일 생략]", _PHONE.sub("[연락처 생략]", s))


# ─────────────────────────────── 목록 ───────────────────────────────

def parse_list(html: str) -> dict:
    """목록 HTML → {"rows": [...], "total": int|None}. 검색 목록·테마(공채속보) 목록 공통."""
    tot = re.search(r'class="txt_total">\s*([\d,]+)\s*<', html)
    total = int(tot.group(1).replace(",", "")) if tot else None
    rows = []
    chunks = re.split(r'<tr id="list\d+"[^>]*>', html)[1:]
    for ch in chunks:
        tr = ch.split("</tr>", 1)[0]
        a = re.search(r'href="(/wk/a/b/1500/empDetailAuthView\.do\?[^"]+)"[^>]*?data-emp-detail[^>]*>(.*?)</a>', tr, re.S)
        if not a:
            continue
        href = _html.unescape(a.group(1))
        q = dict(re.findall(r"([A-Za-z]+)=([^&]*)", href.split("?", 1)[1]))
        if not q.get("wantedAuthNo"):
            continue
        comp = re.search(r'class="cp_name[^"]*"[^>]*>(.*?)</a>', tr, re.S)
        if not comp:  # 회사명 링크가 없는 행
            comp = re.search(r'<div class="box_chk-group">.*?<span>(.*?)<span class="label_box', tr, re.S)
        labels = [_one(x) for x in re.findall(r'<span class="tbl_label[^"]*">(.*?)</span>', tr, re.S)]
        provider = re.search(r'alt="정보제공처\s*([^"]*)"', tr)

        def li(cls):
            m = re.search(rf'<li class="{cls}">(.*?)</li>', tr, re.S)
            if not m:
                return []
            spans = re.findall(r'<span class="item[^"]*">(.*?)</span>', m.group(1), re.S)
            vals = [_one(s) for s in spans] if spans else [_one(m.group(1))]
            return [v for v in vals if v]

        member = li("member")
        close = re.search(r"마감일\s*:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", tr)
        reg = re.search(r"등록일\s*:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", tr)
        wanted = re.search(r"var wantedYn\s*=\s*'([YN]?)'", tr)
        rows.append({
            "auth_no": q["wantedAuthNo"],
            "info_type_cd": q.get("infoTypeCd"),
            "info_type_group": q.get("infoTypeGroup"),
            "href": href,
            "title": _one(a.group(2)),
            "company": _one(comp.group(1)) if comp else None,
            "labels": labels,
            "provider": provider.group(1).strip() if provider else None,
            "salary": " ".join(li("dollar")) or None,
            "career": member[0] if member else None,
            "edu": member[1] if len(member) > 1 else None,
            "worktime": " / ".join(li("time")) or None,
            "location": " ".join(li("site")) or None,
            "close_date": close.group(1) if close else None,
            "reg_date": reg.group(1) if reg else None,
            "wanted_yn": wanted.group(1) if wanted else None,
        })
    return {"rows": rows, "total": total}


def row_to_record(row: dict, mode: str, ref=None) -> dict:
    close = row.get("close_date")
    until_hired = row.get("wanted_yn") == "Y" or (close or "").startswith("2099")
    deadline_raw = "채용시까지" if until_hired else close
    size = next((l for l in row.get("labels") or [] if any(k in l for k in SIZE_LABELS)), None)
    loc = row.get("location") or ""
    loc = re.sub(r"\s+등$", "", loc).strip()
    company_type = "공공기관" if size and "공" in size else None
    return make_record(
        source=NAME, source_id=row["auth_no"],
        url=BASE + row["href"],
        company=row.get("company"), title=row.get("title"),
        career=row.get("career"), edu=row.get("edu"),
        locations=[loc] if loc else [], deadline_raw=deadline_raw,
        posted_at=row.get("reg_date"), company_size=size, company_type=company_type,
        tags=[l for l in row.get("labels") or [] if l != size], salary_raw=row.get("salary"), ref=ref,
        extra={"mode": mode, "provider": row.get("provider"),
               "info_type_cd": row.get("info_type_cd"), "info_type_group": row.get("info_type_group"),
               "close_date": close, "worktime": row.get("worktime"),
               "location_more": bool(re.search(r"\s등$", row.get("location") or ""))},
    )


def _career_param(plan):
    career = [c for c in (plan.get("career") or []) if c]
    codes = sorted({CAREER_CODES[c] for c in career if c in CAREER_CODES})
    return ",".join(codes + ["Z"]) if codes else None  # Z=관계없음은 항상 포함


def _sweep(f, params, max_pages, mode, now, seen, recs, url=LIST_URL, page_size=None):
    """한 검색 조건으로 페이지를 돈다. 반환 (pages, total, stop, errors, dups, expired)."""
    page_size = page_size or RESULT_CNT
    pages, total, stop, errors, dups, expired = 0, None, "end", [], 0, 0
    for page in range(1, max_pages + 1):
        p = dict(params, pageIndex=str(page))
        if url == LIST_URL:
            p["currentPageNo"] = str(page)
        try:
            html = f.get(url, params=p, headers={"Referer": url})
        except Blocked as e:
            if page == 1:
                raise
            errors.append(f"{mode} p{page}: 차단 — {e}")
            stop = "blocked-midway"
            break
        except Exception as e:
            if page == 1:
                raise
            errors.append(f"{mode} p{page}: {type(e).__name__}: {e}")
            stop = "error"
            break
        pages += 1
        parsed = parse_list(html)
        if page == 1:
            total = parsed["total"]
            if total is None and not parsed["rows"]:
                raise RuntimeError("목록 구조 변경 의심(검색건수·행 없음)")
        for row in parsed["rows"]:
            if row["auth_no"] in seen:
                dups += 1
                continue
            seen.add(row["auth_no"])
            rec = row_to_record(row, mode, ref=now)
            if is_expired(rec, now):
                expired += 1
                continue
            recs.append(rec)
        got_so_far = (page - 1) * page_size + len(parsed["rows"])
        if len(parsed["rows"]) < page_size and total is not None and got_so_far < 0.9 * total:
            errors.append(f"{mode} p{page}: 짧은 페이지({len(parsed['rows'])}건)인데 사이트 총 {total}건 — 구조 변경 의심")
            stop = "short-page"
            break
        if len(parsed["rows"]) < page_size or (total is not None and page * page_size >= total):
            break
    else:
        if total is None or max_pages * page_size < total:
            stop = "page-cap"
    return pages, total, stop, errors, dups, expired


def list_postings(f, plan):
    """plan.work24_mode: "both"(기본) | "search" | "gongchae".

    search  : 키워드별(제목 검색) 최신순 × 최대 max_pages, careerTypes=신입/경력 + 관계없음, 고용24 인증 공고만(siteClcd=WORK)
    gongchae: 주요 기업 공채속보 전수(키워드 없음)
    """
    now = now_kst()
    mode = (plan.get("work24_mode") or "both").lower()
    keywords = [k for k in (plan.get("keywords") or []) if str(k).strip()]
    max_pages = int(plan.get("max_pages") or DEFAULT_MAX_PAGES)
    career = _career_param(plan)
    recs, seen, errors = [], set(), []
    pages = dups = expired = 0
    totals, capped, scope_parts = [], False, []
    blocked = False

    if mode in ("both", "search"):
        if not keywords:
            errors.append("검색어 없음 — 키워드 검색 건너뜀")
        for kw in keywords:
            params = {"srcKeyword": kw, "keywordWantedTitle": "Y", "siteClcd": "WORK",
                      "sortField": "DATE", "sortOrderBy": "DESC", "resultCnt": str(RESULT_CNT)}
            if career:
                params["careerTypes"] = career
            try:
                pg, tot, stop, errs, d, ex = _sweep(f, params, max_pages, "search", now, seen, recs)
            except (Blocked, RobotsDisallowed) as e:
                if not recs:
                    raise
                errors.append(f"키워드#{keywords.index(kw) + 1}: 차단 — {e}")
                blocked = True
                break
            except Exception as e:
                errors.append(f"키워드#{keywords.index(kw) + 1}: {type(e).__name__}: {e}")
                continue
            pages += pg
            dups += d
            expired += ex
            errors += errs
            if tot is not None:
                totals.append(tot)
            capped |= stop in ("page-cap", "short-page")
            if stop == "blocked-midway":
                blocked = True
                break
        if keywords:
            scope_parts.append(f"키워드 {len(keywords)}개(제목) × 최대 {max_pages}p×{RESULT_CNT}건, 최신순, 고용24 인증"
                               + (", 경력필터 " + career if career else ""))

    if mode in ("both", "gongchae") and not blocked:
        before = len(recs)
        try:
            pg, tot, stop, errs, d, ex = _sweep(
                f, {"siteClcd": "OEW", "sortField": "DATE", "sortOrderBy": "DESC", "resultCnt": str(RESULT_CNT)},
                GONGCHAE_MAX_PAGES, "gongchae", now, seen, recs)
            via = "OEW 필터"
            if not tot:
                pg2, tot, stop, errs2, d2, ex2 = _sweep(
                    f, {"thmaHrplCd": THEME_GONGCHAE}, THEME_MAX_PAGES, "gongchae", now, seen, recs,
                    url=THEME_URL, page_size=10)
                pg, d, ex, errs, via = pg + pg2, d + d2, ex + ex2, errs + errs2, "테마 페이지"
            pages += pg
            dups += d
            expired += ex
            errors += errs
            if tot is not None:
                totals.append(tot)
            capped |= stop in ("page-cap", "short-page")
            scope_parts.append(f"공채속보 전수({via}, 사이트 {tot}건 중 신규 {len(recs) - before}건)")
        except (Blocked, RobotsDisallowed) as e:
            if not recs:
                raise
            errors.append(f"공채속보: 차단 — {e}")
            blocked = True
        except Exception as e:
            errors.append(f"공채속보: {type(e).__name__}: {e}")

    scope = "; ".join(scope_parts) + (f"; 마감 {expired}건 제외" if expired else "")
    return {"records": recs, "pages": pages, "reported_total": sum(totals) if totals else None,
            "duplicates": dups,
            "stop_reason": "blocked-midway" if blocked else ("page-cap" if capped else ("error" if errors else "end")),
            "errors": errors, "partial": capped or blocked, "scope": scope}


# ─────────────────────────────── 상세 ───────────────────────────────

_SKIP_LABELS = {"문의사항", "채용 담당자", "채용 담당자에게 질문하기"}


def parse_detail(html: str) -> dict:
    """상세 HTML → title, company, fields(th/td), sections(굵은 소제목 블록), steps, positions, employer_url.

    채용 담당자·문의사항 블록(개인 이름·연락처)은 읽지 않는다.
    """
    html = re.sub(r"<!--\s*채용 담당자\s*-->.*?<!--\s*//채용 담당자\s*-->", "", html, flags=re.S)
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    out = {"title": None, "company": None, "fields": {}, "sections": {}, "steps": [], "positions": [],
           "employer_url": None}
    m = re.search(r'<p class="corp_info">\s*<strong>(.*?)</strong>', html, re.S)
    if m:
        out["company"] = _one(m.group(1))
    m = re.search(r'<strong class="title">(.*?)</strong>', html, re.S)
    if m:
        out["title"] = _one(m.group(1))
    m = re.search(r'<a href="(https?://[^"]+)"[^>]*>\s*공채사이트 바로가기', html)
    if m:
        out["employer_url"] = _html.unescape(m.group(1))
    start = html.find('id="tab-panel01"')
    end = html.find("빅데이터 추천 정보")
    body = html[start if start > 0 else 0: end if end > 0 else len(html)]
    for th, td in re.findall(r'<th scope="row"[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>', body, re.S):
        k = norm_text(_txt(th).split("\n")[0])  # th 안의 도움말 툴팁 제외
        if k and k not in out["fields"] and "담당자" not in k and "연락처" not in k:
            out["fields"][k] = _clean(_txt(td))
    for lm in re.finditer(r'<strong class="block b1_sb[^"]*"[^>]*>(.*?)</strong>', body, re.S):
        label = _one(lm.group(1))
        if not label or label in _SKIP_LABELS or label in out["sections"]:
            continue
        rest = body[lm.end(): lm.end() + 30000]
        cut = min([i for i in (rest.find('<strong class="block'), rest.find("<table"),
                               rest.find('<ul class="box_list_area'), rest.find('<ul class="emp_box_items'),
                               rest.find('<div class="box_border_type'), rest.find("<a ")) if i >= 0]
                  or [len(rest)])
        val = _clean(_txt(rest[:cut]))
        if val:
            out["sections"][label] = val
    sp = body.find('id="tab-panel05"')  # 전형방법 패널(복리후생도 같은 ul 클래스를 쓴다)
    m = re.search(r'<ul class="emp_box_items[^"]*">(.*?)</ul>', body[sp:], re.S) if sp >= 0 else None
    if m:
        for cls, item in re.findall(r"<li\s*(class=\"[^\"]*\")?\s*>(.*?)</li>", m.group(1), re.S):
            if "disable" not in (cls or ""):
                out["steps"].append(_one(re.sub(r'<em class="blind">.*?</em>', "", item)))
    m = re.search(r'<ul class="emp_model_step">(.*?)</ul>', body, re.S)
    if m:
        out["steps"] += [_one(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.S) if _one(x)]
    m = re.search(r"<caption>채용부문[^<]*</caption>(.*?)</table>", body, re.S)
    if m:
        heads = [_one(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", m.group(1), re.S)]
        tb = re.search(r"<tbody>(.*?)</tbody>", m.group(1), re.S)
        for tr in re.findall(r"<tr>(.*?)</tr>", tb.group(1) if tb else "", re.S):
            tds = [_txt(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if tds:
                out["positions"].append(dict(zip(heads, tds)))
    return out


def _detail_text(d: dict) -> str:
    parts = [f"[{k}]\n{v}" for k, v in d["sections"].items()]
    parts += [f"{k}: {v}" for k, v in d["fields"].items() if v and v != "-"]
    if d["steps"]:
        parts.append("[전형방법] " + " → ".join(d["steps"]))
    for p in d["positions"]:
        parts.append("[채용부문] " + " | ".join(f"{k}: {v}" for k, v in p.items()))
    return scrub("\n".join(parts)) or ""


def fetch_detail(f, rec):
    ex = rec.get("extra") or {}
    params = {"wantedAuthNo": rec["source_id"], "infoTypeCd": ex.get("info_type_cd") or "VALIDATION",
              "infoTypeGroup": ex.get("info_type_group") or "tb_workinfoworknet"}
    html = f.get(DETAIL_URL, params=params, headers={"Referer": LIST_URL})
    d = parse_detail(html)
    if not d["title"] and not d["fields"]:
        raise RuntimeError("상세 내용 없음(구조 변경·삭제 공고 또는 파라미터 누락)")
    fl, sec = d["fields"], d["sections"]
    emp = fl.get("고용 형태") or sec.get("고용형태")
    openings = []
    for p in d["positions"]:  # 공채속보: 채용부문 표 1행 = 직무 1건
        qual = p.get("지원자격") or ""
        openings.append(make_opening(
            role=p.get("채용부문") or p.get("모집분야"), career=qual or None,
            locations=[x for x in (p.get("근무지") or "").split("\n") if x.strip()],
            employment_type=emp,
            extra={"모집분야": scrub(p.get("모집분야")), "담당업무": scrub(p.get("담당업무")), "지원자격": qual}))
    if not openings:
        role = fl.get("모집 직종") or d["title"] or rec.get("title")
        openings.append(make_opening(role=role, career=_career_label(fl.get("경력")), locations=[fl.get("근무 예정지")] if fl.get("근무 예정지") else [],
                                     headcount=fl.get("모집 인원"), employment_type=emp,
                                     extra={"직종 키워드": norm_text(fl.get("직종 키워드")) or None}))
    deadline_raw = sec.get("접수 마감일") or sec.get("접수 기간")
    if deadline_raw:
        deadline_raw = norm_text(deadline_raw.replace("고용24 입사지원", ""))
        if "~" in deadline_raw:  # 접수 기간 "시작 ~ 마감 23:59 D-8" → 마감 쪽만
            deadline_raw = deadline_raw.split("~", 1)[1].strip()
        deadline_raw = re.sub(r"\s*D\s*-\s*\d+\s*$", "", deadline_raw)
    text = _detail_text(d)
    job_text = sec.get("직무내용") or sec.get("공통사항") or ""
    want = {"직무내용": sec.get("직무내용"), "지원자격": sec.get("공통사항"),
            "경력": fl.get("경력"), "학력": fl.get("학력"), "자격면허": fl.get("자격 면허"),
            "외국어": fl.get("외국어 능력"), "전공": fl.get("전공"), "우대사항": fl.get("우대조건"),
            "기타 우대사항": fl.get("기타 우대사항"), "근무지": fl.get("근무 예정지"), "고용형태": emp,
            "임금": fl.get("임금 조건"), "근무형태": fl.get("근무 형태"),
            "전형방법": " → ".join(d["steps"]) or None, "접수방법": sec.get("접수 방법"),
            "제출서류": sec.get("제출 서류"), "접수마감": deadline_raw}
    sections = {k: scrub(v) for k, v in want.items() if v and v != "-"}
    detail_ref = bool(re.search(r"(채용\s*홈페이지|공채사이트|상세\s*모집요강).{0,10}(참조|참고|확인)", text))
    return {
        "openings": openings,
        "text": text,
        # 고용24 인증 공고는 요건이 정형 필드(경력·학력·자격 면허)로 있다 — 직무내용이 비었을 때만 불완전.
        # 공채속보는 요약만 있고 세부 요건은 기업 채용페이지에 있다.
        "text_incomplete": (len(job_text) < 20 and not fl.get("경력"))
                           or (bool(d["employer_url"]) and detail_ref),
        "employer_url": d["employer_url"],
        "deadline_raw": deadline_raw,
        "sections": sections,
        "employment_type": emp,
    }

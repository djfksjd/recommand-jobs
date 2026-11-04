"""캐치(catch.co.kr) 어댑터 — 대기업·중견 보완.

실측(2026-11-03):
  * robots.txt `*`: `/Search`·`/Company`·`/Apply`·`/Controls`·`/controls`·`/Member`·`/api/v1.0/common`·
    `/api/v1.0/product`·`/api/v1.0/recruit/log`·`/api/v1.0/recruit/detail/log` 등 불허, 그 밖은 `Allow: /`.
  * 목록 페이지 `/NCS/RecruitSearch` 는 Nuxt SSR이지만 공고 목록은 클라이언트가 불러온다(fetchOnServer:false):
      `GET /api/v1.0/recruit/information/getRecruitList` (로그인 불필요, robots 허용 경로)
      params: curpage, pageSize(≤100 확인), onRecruitYN=Y(진행 중), Sort(0 최신/2 마감일/3 인기),
              Career(쉼표: 1 신입, 2 경력, 4 인턴, 0 경력무관), JobCode(직무), Sido, Size, EduLevel, Keyword ...
      → {"recruitData": [...], "intTotalRecordCount": N}. 신입+인턴+무관 필터 시 약 1,260건(13요청).
      항목: RecruitID, RecruitTitle, CompName, CompID, ApplyStartDatetime/ApplyEndDatetime(UTC 'Z'),
            ApplyEndCode(''|'상시채용'|'채용시 마감' — 뒤 둘은 ApplyEndDatetime이 자리값), ApplyEndTimeHide,
            Depth(직무 분류, 쉼표), CareerGubunCode(신입/경력/신입·경력/경력무관), ExperienceRange,
            GubunCode(정규직/계약직/인턴/기타), WorkArea(시도, '/' 구분), RecruitCategory(업종), GroupName
  * 상세: `/NCS/RecruitInfoDetails/<id>` (허용). 본문은 `/controls/recruitDetail/<id>` iframe이지만 그 경로는
    robots 불허 → 요청하지 않는다. 대신 같은 내용이 window.__NUXT__ 의 data[0].recruitInfo.RecruitHTML 에
    SSR로 실려 온다(이미지만인 공고도 있음). ApplyURL = 기업 접수 페이지.
  * window.__NUXT__ 는 `(function(a,b,...){a.x=..; return {...}}(인자...))` 형태 — 아래 parse_nuxt가
    JS를 실행하지 않고 리터럴·인자 치환·단순 대입문만 해석한다.
"""
from __future__ import annotations

import re
from datetime import datetime

from jobs_common import (KST, Blocked, make_opening, make_record, norm_text, now_kst)

NAME = "catch"
LABEL = "캐치"
HOSTS = ("catch.co.kr",)
KIND = "full"
PLAN_KEYS = ("career", "days_ahead", "page_cap")  # 키워드는 이 소스의 결과에 영향이 없다
DELAY = 0.8
BASE = "https://www.catch.co.kr"
LIST_API = f"{BASE}/api/v1.0/recruit/information/getRecruitList"
PAGE_SIZE = 100
PAGE_CAP = 40

CAREER_CODE = {"신입": "1", "경력": "2", "인턴": "4", "무관": "0"}
EDU = {"0": "학력무관", "3": "고졸", "4": "대학 졸업(2,3년제)", "5": "대학교 졸업(4년제)", "6": "석사", "7": "박사"}


# ─────────────────────────────── __NUXT__ 해석 ───────────────────────────────

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<num>-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<id>[A-Za-z_$][\w$]*)
  | (?P<p>[{}\[\](),:;=.])
""", re.X | re.S)

_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _unescape(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c != "\\":
            out.append(c); i += 1; continue
        n = s[i + 1]
        if n == "u":
            if s[i + 2] == "{":
                j = s.index("}", i)
                out.append(chr(int(s[i + 3:j], 16))); i = j + 1
            else:
                out.append(chr(int(s[i + 2:i + 6], 16))); i += 6
        elif n == "x":
            out.append(chr(int(s[i + 2:i + 4], 16))); i += 4
        else:
            out.append(_ESC.get(n, n)); i += 2
    # surrogate pair 결합
    return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16")


class NuxtError(ValueError):
    pass


class _P:
    def __init__(self, src):
        self.toks = []
        pos = 0
        while pos < len(src):
            m = _TOKEN.match(src, pos)
            if not m:
                raise NuxtError(f"토큰 해석 실패 @{pos}: {src[pos:pos+30]!r}")
            pos = m.end()
            k = m.lastgroup
            if k != "ws":
                self.toks.append((k, m.group(k)))
        self.i = 0
        self.env = {}

    def peek(self, o=0):
        return self.toks[self.i + o] if self.i + o < len(self.toks) else (None, None)

    def take(self, val=None):
        t = self.toks[self.i]
        if val is not None and t[1] != val:
            raise NuxtError(f"{val!r} 기대, {t[1]!r} 만남 @tok{self.i}")
        self.i += 1
        return t

    def value(self):
        k, v = self.take()
        if k == "str":
            return _unescape(v[1:-1])
        if k == "num":
            f = float(v)
            return int(f) if f.is_integer() and not re.search(r"[.eE]", v) else f
        if k == "id":
            if v == "true": return True
            if v == "false": return False
            if v == "null": return None
            if v == "void":
                self.take()  # 0
                return None
            if v == "new":  # new Map([...]) / new Set([...]) / new Date(x)
                _, ctor = self.take()
                self.take("(")
                cargs = []
                while self.peek()[1] != ")":
                    cargs.append(self.value())
                    if self.peek()[1] == ",":
                        self.take()
                self.take(")")
                a0 = cargs[0] if cargs else None
                if ctor == "Map":
                    return {str(k): val for k, val in (a0 or [])}
                if ctor == "Set":
                    return list(a0 or [])
                return a0
            if v == "Array" and self.peek()[1] == "(":  # Array(3) = 빈 칸 3개
                self.take("(")
                n = self.value()
                self.take(")")
                return [None] * int(n or 0)
            if v in self.env:
                return self.env[v]
            raise NuxtError(f"모르는 식별자 {v!r}")
        if v == "{":
            obj = {}
            while self.peek()[1] != "}":
                kk, kv = self.take()
                key = _unescape(kv[1:-1]) if kk == "str" else kv
                self.take(":")
                obj[key] = self.value()
                if self.peek()[1] == ",":
                    self.take()
            self.take("}")
            return obj
        if v == "[":
            arr = []
            while self.peek()[1] != "]":
                arr.append(self.value())
                if self.peek()[1] == ",":
                    self.take()
            self.take("]")
            return arr
        raise NuxtError(f"값이 아닌 토큰 {v!r}")


def parse_nuxt(html):
    """HTML에서 window.__NUXT__ 를 찾아 dict로. 순수 JSON 형식도 받는다."""
    i = html.find("window.__NUXT__")
    if i < 0:
        raise NuxtError("__NUXT__ 없음")
    j = html.find("</script>", i)
    src = html[i + len("window.__NUXT__"):j].strip().lstrip("=").strip().rstrip(";")
    if not src.startswith("(function"):
        return _P(src).value()
    p = _P(src)
    p.take("("); p.take("function"); p.take("(")
    params = []
    while p.peek()[1] != ")":
        params.append(p.take()[1])
        if p.peek()[1] == ",":
            p.take()
    p.take(")"); p.take("{")
    body_start = p.i
    # 함수 끝(짝 맞는 '}')을 찾아 인자부터 먼저 평가
    depth, k = 1, p.i
    while depth:
        t = p.toks[k][1] if p.toks[k][0] == "p" else None
        if t == "{": depth += 1
        elif t == "}": depth -= 1
        k += 1
    p.i = k
    p.take("(")
    args = []
    while p.peek()[1] != ")":
        args.append(p.value())
        if p.peek()[1] == ",":
            p.take()
    p.env = {name: (args[n] if n < len(args) else None) for n, name in enumerate(params)}
    p.i = body_start
    while True:
        k, v = p.peek()
        if v == "return":
            p.take()
            return p.value()
        # 대입문: a.b.c=EXPR;  a["x"]=EXPR;
        _, name = p.take()
        if name not in p.env:
            raise NuxtError(f"대입 대상 모름 {name!r}")
        target, key = None, None
        cur = p.env[name]
        while p.peek()[1] in (".", "["):
            if target is not None:
                cur = target[int(key)] if isinstance(target, list) else target[key]
            if p.take()[1] == ".":
                key = p.take()[1]
            else:
                kk, kv = p.take()
                key = _unescape(kv[1:-1]) if kk == "str" else kv
                p.take("]")
            target = cur
        p.take("=")
        val = p.value()
        if isinstance(target, list):
            idx = int(key)
            if idx >= len(target):
                target.extend([None] * (idx + 1 - len(target)))
            target[idx] = val
        else:
            target[key] = val
        if p.peek()[1] == ";":
            p.take()


# ─────────────────────────────── 파서(네트워크 없음) ───────────────────────────────

def _utc_to_kst(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def _split(s, sep):
    return [x.strip() for x in re.split(sep, s or "") if x and x.strip()]


def item_to_record(d, ref=None):
    """getRecruitList 항목 → 레코드. 마감 지났으면 None."""
    ref = ref or now_kst()
    rid = str(d["RecruitID"])
    end_code = norm_text(d.get("ApplyEndCode"))
    end = _utc_to_kst(d.get("ApplyEndDatetime"))
    if end_code:  # 상시채용 / 채용시 마감 — ApplyEndDatetime 은 자리값
        deadline, prec = None, None
        kind = "상시" if "상시" in end_code else ("채용시" if "채용" in end_code else None)
        raw = end_code
    elif end:
        if end < ref:
            return None
        hide = bool(d.get("ApplyEndTimeHide"))
        if hide:
            end = end.replace(hour=23, minute=59)
        deadline, kind, prec = end.isoformat(timespec="minutes"), "date", ("day" if hide else "minute")
        raw = end.strftime("%Y-%m-%d") if hide else end.strftime("%Y-%m-%d %H:%M")
    else:
        deadline, kind, prec, raw = None, "unknown", None, None
    if kind is None:  # 처음 보는 마감 코드 — 원문만 넘겨 파서에 맡긴다
        deadline, kind, prec = None, None, None
    roles = _split(d.get("Depth"), r",")
    locs = _split(d.get("WorkArea"), r"/")
    gubun = norm_text(d.get("GubunCode"))
    career_raw = norm_text(d.get("CareerGubunCode"))
    careers = [career_raw] if career_raw else []
    if "인턴" in gubun:
        careers.append("인턴")
    if "계약" in gubun:
        careers.append("계약")
    rng = norm_text(d.get("ExperienceRange"))
    opening = make_opening(role=", ".join(roles) or None, career=careers or None, locations=locs,
                           employment_type=gubun or None,
                           extra={"experience_range": rng or None})
    kw = dict(deadline=deadline, deadline_kind=kind, deadline_precision=prec) if kind else {}
    return make_record(
        source=NAME, source_id=rid, url=f"{BASE}/NCS/RecruitInfoDetails/{rid}",
        company=d.get("CompName"), title=d.get("RecruitTitle"),
        openings=[opening], career=careers or None,
        edu=EDU.get(str(d.get("EduLevelCode"))) if d.get("EduLevelCode") is not None else None,
        employment_type=gubun or None, locations=locs, deadline_raw=raw,
        posted_at=(_utc_to_kst(d.get("ApplyStartDatetime")).isoformat(timespec="minutes")
                   if _utc_to_kst(d.get("ApplyStartDatetime")) else None),
        tags=roles, salary_raw=d.get("SalaryText"),
        extra={"comp_id": d.get("CompID"), "industry": d.get("RecruitCategory"),
               "group": d.get("GroupName"), "experience_range": rng or None,
               "career_gubun": career_raw or None, "apply_end_code": end_code or None,
               "placeholder_end": (end.isoformat(timespec="minutes") if end_code and end else None),
               "is_product": d.get("IsProduct"), "roles_need_detail": True},
        **kw,
    )


def parse_list(data):
    """getRecruitList JSON → (items, total). 구조가 다르면 RuntimeError."""
    if not isinstance(data, dict) or "recruitData" not in data:
        raise RuntimeError("응답 구조 변경: recruitData 키 없음")
    return data.get("recruitData") or [], data.get("intTotalRecordCount")


def html_to_text(content):
    body = re.sub(r"<(style|script)[^>]*>.*?</\1>|<!--.*?-->", " ", content or "", flags=re.S | re.I)
    body = re.sub(r"<(br|/p|/div|/li|/tr|/h\d|/dt|/dd)[^>]*>", "\n", body, flags=re.I)
    body = re.sub(r"</t[dh]>", " | ", body, flags=re.I)
    return "\n".join(filter(None, (norm_text(x).strip(" |") for x in body.split("\n"))))


def parse_detail_html(html, rid=None):
    try:
        nuxt = parse_nuxt(html)
    except NuxtError as e:
        raise RuntimeError(f"상세 __NUXT__ 해석 실패: {e}")
    data = (nuxt.get("data") or [{}])[0] or {}
    ri = data.get("recruitInfo") or {}
    if not ri or (rid is not None and str(ri.get("RecruitID")) != str(rid)):
        raise Blocked("상세 데이터 비어 있음(삭제·비공개 또는 로그인 요구 가능)")
    content = ri.get("RecruitHTML") or ""
    text = html_to_text(content)
    imgs = len(re.findall(r"<img\b", content, re.I))
    iframes = len(re.findall(r"<iframe\b", content, re.I))
    cg = data.get("careerGubun") or ri.get("careerGubun") or {}
    career_names = norm_text(ri.get("careerGubunNames") or cg.get("ExperienceText"))
    gubun_names = norm_text(ri.get("recruitGubunNames"))
    careers = [career_names] if career_names else []
    if "인턴" in gubun_names:
        careers.append("인턴")
    if "계약" in gubun_names:
        careers.append("계약")
    roles = norm_text(ri.get("majorJobCodeNames") or ri.get("taskNames") or ri.get("TaskName"))
    locs = _split(ri.get("workAreas") or data.get("workAreas"), r",|/")
    addrs = [norm_text(w.get("WorkAddress")) for w in data.get("workArea") or [] if w.get("WorkAddress")]
    opening = make_opening(role=roles or None, opening_id=ri.get("RecruitID"), career=careers or None,
                           locations=locs, employment_type=gubun_names or None,
                           extra={"task_name": ri.get("TaskName"),
                                  "career_more_than": ri.get("CareerMoreThan"),
                                  "career_less_than": ri.get("CareerLessThan"),
                                  "experience_range": cg.get("ExperienceRange")})
    end_code = norm_text(ri.get("ApplyEndCode"))
    end = _utc_to_kst(ri.get("ApplyEndDatetime"))
    sections = {k: v for k, v in {
        "경력": career_names, "고용형태": gubun_names, "학력": norm_text(ri.get("eduLevel")),
        "근무지": " / ".join(addrs) or norm_text(ri.get("workAreas")), "직무": roles,
    }.items() if v}
    m = re.search(r"(자격\s*요건|지원\s*자격)\s*\n(.*?)(?:\n\s*(?:■|\[|우대\s*사항|전형\s*절차)|\Z)", text, re.S)
    if m:
        sections["자격요건"] = m.group(2).strip()[:2000]
    m = re.search(r"우대\s*사항\s*\n(.*?)(?:\n\s*(?:■|\[|전형\s*절차|근무\s*조건)|\Z)", text, re.S)
    if m:
        sections["우대사항"] = m.group(1).strip()[:2000]
    employer = ri.get("ApplyURL") or ri.get("OriginNoticeURL") or None
    return {
        "openings": [opening],
        "text": text,
        "text_incomplete": (len(text) < 200 and (imgs > 0 or iframes > 0)) or not text,
        "images": imgs,
        "employer_url": employer if employer and str(employer).startswith("http") else None,
        "deadline_raw": end_code or (end.strftime("%Y-%m-%d %H:%M") if end else None),
        "posted_at": (_utc_to_kst(ri.get("ApplyStartDatetime")).isoformat(timespec="minutes")
                      if _utc_to_kst(ri.get("ApplyStartDatetime")) else None),
        "valid_through": ri.get("ValidThrough"),
        "sections": sections,
    }


# ─────────────────────────────── 어댑터 인터페이스 ───────────────────────────────

def _career_param(plan):
    wanted = plan.get("career") or []
    codes = [CAREER_CODE[c] for c in wanted if c in CAREER_CODE]
    if not codes:
        return ""  # 전체
    if "0" not in codes:
        codes.append("0")  # 경력무관은 항상 포함
    return ",".join(sorted(set(codes)))


def list_postings(f, plan):
    career = _career_param(plan)
    cap = int(plan.get("page_cap") or PAGE_CAP)
    now = now_kst()
    recs, seen, dup, expired, pages, total, stop = [], set(), 0, 0, 0, None, "page-cap"
    block_err = None
    for page in range(1, cap + 1):
        params = {"Keyword": "", "JobCode": "", "Sido": "", "Career": career, "JCode": "", "Size": "",
                  "EduLevel": "", "WorkPosition": "", "CompID": "", "GroupCode": "", "Sort": 0,
                  "curpage": page, "pageSize": PAGE_SIZE, "onRecruitYN": "Y", "ExceptIDList": ""}
        try:
            data = f.get_json(LIST_API, params=params,
                              headers={"Referer": f"{BASE}/NCS/RecruitSearch",
                                       "Accept": "application/json, text/plain, */*"})
        except Blocked as e:
            if page == 1:
                raise
            stop, block_err = "blocked-midway", f"p{page}: {e}"
            break
        items, total = parse_list(data)
        pages += 1
        for d in items:
            rid = str(d.get("RecruitID"))
            if rid in seen:
                dup += 1
                continue
            seen.add(rid)
            rec = item_to_record(d, now)
            if rec is None:
                expired += 1
                continue
            recs.append(rec)
        if len(items) < PAGE_SIZE or (total is not None and page * PAGE_SIZE >= total):
            stop = "end"
            break
    label = {"": "전체"}.get(career, "+".join(k for k, v in CAREER_CODE.items() if v in career.split(",")))
    return {"records": recs, "pages": pages, "reported_total": total, "duplicates": dup,
            "stop_reason": stop, "errors": [block_err] if block_err else [],
            "partial": stop in ("page-cap", "blocked-midway"),
            "scope": f"진행 중 공고 전수(경력 필터 {label}, 마감 {expired}건 제외)"}


def fetch_detail(f, rec):
    rid = rec["source_id"]
    html = f.get(f"{BASE}/NCS/RecruitInfoDetails/{rid}")
    return parse_detail_html(html, rid)

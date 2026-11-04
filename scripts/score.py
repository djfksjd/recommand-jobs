#!/usr/bin/env python3
"""목록 단계 선별 — 확실한 제외만 하고, 나머지는 적합도로 정렬해 LLM 전수 검토 자료를 만든다.

  python3 score.py SURVEY_DIR --profile recommand-jobs-profile.md [--applied applied.tsv]

입력  : SURVEY_DIR/*.jsonl (crawl.py list 결과)
출력  : SURVEY_DIR/screened.jsonl    — 모든 레코드 + screen{gate, fit, score, reasons, applied, group}
        SURVEY_DIR/screen_summary.json — 건수 요약(검색어·프로필 값은 넣지 않음)
        SURVEY_DIR/review/shortlist_NN.md — 중복을 묶은 상위 후보(기본 250건, 50건씩). LLM이 전부 읽는다.
        SURVEY_DIR/review/overflow.md      — 순위 밖 후보 한 줄 색인(훑어보기·검색용)
        SURVEY_DIR/review/excluded_index.md — 규칙이 뺀 것(경력 전용 등)·weak 한 줄 색인(오판 점검용)

원칙(설계 검토 반영)
  * 자격 판정(eligibility)과 적합도(fit)를 섞지 않는다. 여기서의 gate는 '목록 정보만으로 확실한 것'뿐이다:
      expired / experienced_only / excluded_company / excluded_location / employment_type_mismatch
    그 밖의 자격(어학·자격증·전공·졸업시점)은 상세 원문을 본 뒤 requirements.py + LLM이 판정한다.
  * 우대요건은 제외 사유가 아니다. 미확인 조건을 충족으로 가산하지 않는다.
  * 이미 지원: 같은 회사·같은 직무면 same_opening, 같은 회사 다른 직무면 same_company — 회사 단위로 지우지 않는다.
  * 교차 소스 중복은 삭제·병합하지 않고 group으로만 묶는다(possible_duplicate).
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from jobs_common import (KST, atomic_write, is_expired, norm_company, norm_text,  # noqa: E402
                         now_kst, read_jsonl, write_jsonl)

RULES_VERSION = 2  # 2026-11-03: 공채·직무분류·지원이력·기업유형 규칙 개정
JUDGMENT_AXES = ("salary_prefer_min", "posting_edu_prefer", "career", "employment_types_ok", "education", "graduation", "available_from", "major",
                 "driver_license", "residence",
                 "licenses", "languages", "speaking", "shift_work_ok", "exclude_companies",
                 "target_roles", "adjacent_roles", "avoid_keywords", "company_types_prefer")
# 종합공채형: 제목이 '여러 직무를 한 번에 뽑는 공채'임을 말할 때만. "체험형 인턴"·"채용 공고" 같은 흔한 꼬리말은 제외.
GENERIC_TITLE = re.compile(r"공개\s*채용|공채|신입\s*사원\s*(공개|정기|수시)?\s*(채용|모집)|대졸\s*(신입|공채)|"
                           r"(상|하)반기\s*(신입|신규|정규직|대졸)?\s*(사원\s*)?(채용|모집)|신규\s*채용|신입\s*(사원\s*)?(채용|모집)(\s*공고)?\s*$|"
                           r"(신입|정규)\s*직원\s*(채용|모집)|신규\s*직원|각\s*(부문|분야)|부문별|분야별|"
                           r"(그룹|계열사)\s*(신입|공채)|채용형\s*인턴|청년\s*인턴", re.I)
# 다른 직무가 제목에 명시돼 있으면 종합공채로 보지 않는다(사용자 무관한 일반 직무어 — 사용자의 target 키워드와 겹치면 무시)
SPECIFIC_ROLE = re.compile(r"개발|마케팅|영업|디자인|디자이너|기획|회계|재무|인사|총무|법무|간호|의사|전문의|교수|"
                           r"연구위원|판매|상담|강사|요양|조리|미화|경비|운전|약사|PD|MD|iOS|Android|프론트|백엔드|박사|자금|보건|"
                           r"임상|의학|급식|사서|통번역|의료기사|AI\s*연구|약무|방사선", re.I)
SHORTLIST_MAX = 250
NEWCOMER_IN_TITLE = re.compile(r"신입|인턴|경력\s*무관|무관|졸업\s*예정|대졸|공채|채용연계")
BATCH_SIZE = 50
# 한국어 부분 문자열 오탐 — 키워드 비교 전에 이 말들을 지운다(사이트·사용자 무관한 일반 규칙).
FALSE_FRIENDS = ("운전기사", "사전기획", "전기간", "공무원", "공무직", "공정거래", "공정위", "공정성",
                 "환경보전", "보전처리", "설비투자 분석", "전력투구", "시설물 미화", "편의시설", "시설미화",
                 "시설경비", "복지시설", "보육시설", "의료기술",
                 "전자상거래", "전자결재", "전자금융", "전자문서", "전자정부", "전자출판")


# ─────────────────────────── 프로필 ───────────────────────────

def load_profile(path):
    import yaml
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    if not m:
        raise SystemExit(f"프로필 YAML 머리말이 없음: {path}")
    prof = yaml.safe_load(m.group(1)) or {}
    if prof.get("profile_version") != 1:
        raise SystemExit("profile_version 1 이 아님")
    return prof


def profile_fingerprint(prof) -> str:
    """판정 축만으로 만든 지문. 바뀌면 diff 모드가 승계를 무효화한다."""
    axes = {k: prof.get(k) for k in JUDGMENT_AXES}
    axes["locations_exclude"] = (prof.get("locations") or {}).get("exclude")
    payload = json.dumps(axes, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_applied(path):
    """applied.tsv: 회사<TAB>직무<TAB>상태(선택). '#' 주석 허용."""
    out = []
    if not path or not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        if not line.strip() or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.rstrip("\n").split("\t")]
        if len(parts) > 2 and re.search(r"보류|미지원|취소|포기", parts[2]):
            continue  # 지원하지 않은 건 — 이력으로 치지 않는다
        out.append({"company_norm": norm_company(parts[0]), "role": parts[1] if len(parts) > 1 else "",
                    "status": parts[2] if len(parts) > 2 else ""})
    return out


# ─────────────────────────── 규칙 ───────────────────────────

def _kw_hits(text, keywords):
    t = norm_text(text).lower()
    for ff in FALSE_FRIENDS:
        t = t.replace(ff.lower(), " ")
    return [k for k in keywords or [] if k and k.lower() in t]


def _is_category_role(role, tags):
    """사이트 직무분류를 직무명 자리에 넣은 경우(캐치·링커리어) — 쉼표로 나눈 조각이 전부 태그면 분류다."""
    parts = [p.strip() for p in re.split(r"[,，]", role or "") if p.strip()]
    return bool(parts) and all(p in tags for p in parts)


def _role_text(rec, with_tags=True):
    tags = set(rec.get("tags") or [])
    roles = " ".join(o.get("role") or "" for o in rec.get("openings") or []
                     if not _is_category_role(o.get("role"), tags))
    parts = [rec.get("title") or "", roles]
    if with_tags:
        parts.append(" ".join(rec.get("tags") or []))
    return " ".join(parts)


def _all_keywords(groups):
    kws = []
    for g in groups or []:
        kws += g.get("keywords") or []
    return kws


def screen(rec, prof, applied, ref=None):
    ref = ref or now_kst()
    reasons = []
    gate = "ok"
    text = _role_text(rec)

    # 1) 확실한 제외만
    if is_expired(rec, ref):
        gate = "expired"
    want = set(prof.get("career") or [])
    rc = set(rec.get("career") or [])
    if gate == "ok" and rc and rc <= {"경력"} and "경력" not in want and not NEWCOMER_IN_TITLE.search(text):
        gate = "experienced_only"
        reasons.append("목록상 경력 전용")
    if gate == "ok" and rec.get("company_norm") in {norm_company(c) for c in prof.get("exclude_companies") or []}:
        gate = "excluded_company"
    loc_ex = (prof.get("locations") or {}).get("exclude") or []
    locs = rec.get("locations") or [loc for o in rec.get("openings") or [] for loc in o.get("locations") or []]
    loc_incomplete = any(re.search(r"외\s*\d+|등$|전국|해외", loc) for loc in locs) or \
        bool((rec.get("extra") or {}).get("location_more"))
    if gate == "ok" and locs and loc_ex and not loc_incomplete and \
            all(any(x in loc for x in loc_ex) for loc in locs):
        gate = "excluded_location"
    et_ok = prof.get("employment_types_ok")
    et = rec.get("employment_type") or ""
    et_parts = [p.strip() for p in re.split(r"[,·/|]|\s및\s", et) if p.strip() and not re.search(r"\d", p)]
    def _ok(part):
        return any(a in part or part in a for a in et_ok)
    if gate == "ok" and et_ok and et_parts and not any(_ok(p) for p in et_parts):
        gate = "employment_type_mismatch"  # 모집 형태가 전부 사용자가 뺀 형태일 때만
    if gate == "ok" and rc == {"계약"} and et_ok and not any("계약" in x for x in et_ok) and not et_parts:
        gate = "employment_type_mismatch"

    # 2) 적합도 (점수는 합격 확률이 아니다 — 정렬용)
    core_text = _role_text(rec, with_tags=False)       # 제목 + 직무명
    tkw, akw = _all_keywords(prof.get("target_roles")), _all_keywords(prof.get("adjacent_roles"))
    direct = _kw_hits(core_text, tkw)
    adjacent = _kw_hits(core_text, akw)
    tag_only = [] if (direct or adjacent) else _kw_hits(" ".join(rec.get("tags") or []), tkw + akw)
    avoid = _kw_hits(core_text, prof.get("avoid_keywords"))
    title = rec.get("title") or ""
    specific = [m for m in SPECIFIC_ROLE.findall(title) if not _kw_hits(m, tkw + akw)]
    generic = bool(GENERIC_TITLE.search(title)) and not direct and not specific
    title_avoid = _kw_hits(title, prof.get("avoid_keywords"))
    if direct:
        fit, role_pts = "direct", 40
        reasons.append("직접 직무: " + ", ".join(direct[:4]))
    elif generic and tag_only:
        fit, role_pts = "generic", 25  # 종합공채인데 사이트 모집분야 분류에 희망 직무가 있다 — 가장 유망한 공채
        reasons.append("종합공채 + 모집분야 분류 일치: " + ", ".join(tag_only[:3]))
    elif tag_only and not avoid:
        fit, role_pts = "adjacent", 15
        reasons.append("사이트 직무분류만 일치: " + ", ".join(tag_only[:3]) + " — 제목·직무명 확인 필요")
    elif adjacent:
        fit, role_pts = "adjacent", 25
        reasons.append("인접 직무: " + ", ".join(adjacent[:4]))
    elif generic:
        fit, role_pts = "generic", 15
        reasons.append("종합공채형 제목 — 직무 목록은 상세에서 확인")
    else:
        fit, role_pts = "weak", 0
    if avoid and fit in ("weak", "generic"):
        role_pts -= 15
        reasons.append("비희망 직무어: " + ", ".join(avoid[:3]))
    if fit == "adjacent" and (specific or title_avoid) and not GENERIC_TITLE.search(title):
        role_pts -= 15  # "시설(미화)", "기술직(약무)"처럼 인접어가 다른 직무에 붙은 경우(공채의 여러 직무 중 하나는 제외)
        reasons.append("제목에 다른 직무 명시: " + ", ".join((specific + title_avoid)[:3]))
    cond_pts = 0
    if rc & (want | ({"무관"} if "신입" in want else set())):
        cond_pts += 15
    elif not rc:
        cond_pts += 5  # 미확인 — 충족으로 가산하지 않되 0으로 벌하지도 않는다
    if not et or not et_ok or any(x in et for x in et_ok):
        cond_pts += 10
    lic_pts = 0
    for lic in prof.get("licenses") or []:
        name = lic.get("name") if isinstance(lic, dict) else str(lic)
        if name and name in core_text:
            lic_pts = 10  # 제목에 자격을 적은 공고는 맞지만, 대기업 공채보다 앞설 이유는 아니다
            reasons.append(f"제목·직무명에 보유 자격 언급: {name}")
            break
        if name and name in " ".join(rec.get("tags") or []):
            lic_pts = 5  # 사이트 검색 태그는 공고가 요구한다는 뜻이 아니다
    pref_pts = 0
    prefer_types = prof.get("company_types_prefer") or []
    kinds = {rec.get("company_size"), rec.get("company_type"), rec.get("company_size_inferred")} - {None}
    if rec.get("proxy_posting"):
        kinds = set()  # 기관이 다른 기업 공고를 대신 올림 — 기관의 유형을 가점으로 쓰지 않는다
        reasons.append("다른 기업 공고 대행 게시(실제 채용 기업 확인)")
    if prefer_types and kinds & set(prefer_types):
        pref_pts += 15
        reasons.append("선호 기업 유형: " + "/".join(sorted(kinds & set(prefer_types))))
    prefer = (prof.get("locations") or {}).get("prefer") or []
    if prefer and any(p in loc for p in prefer for loc in locs):
        pref_pts += 7
    if fit == "generic" and rec.get("tags") and not _kw_hits(" ".join(rec["tags"]), tkw + akw):
        role_pts -= 15  # 사이트가 붙인 모집 분야 분류 어디에도 희망 직무가 없다 — 공채라도 해당 직무가 없을 가능성
        reasons.append("모집 분야 분류에 희망 직무 없음")
        pref_pts = -15 if pref_pts else 0  # 아래 공채 가점을 상쇄
    if fit == "generic" and pref_pts > 0:
        pref_pts += 15  # 선호 기업의 종합공채 — 제목엔 직무가 없지만 맞는 직무가 들어 있을 가능성이 가장 큰 묶음
        reasons.append("선호 기업 종합공채 — 상세에서 모집 직무 확인 우선")
    # 연봉·공고 학력 선호(점수만 — 제외하지 않는다. 표기가 없으면 0)
    pay_pts = 0
    smin = prof.get("salary_prefer_min")  # 만원/년
    if smin:
        lo, hi = parse_salary(" ".join(filter(None, [rec.get("salary_raw"), rec.get("employment_type"),
                                                    str((rec.get("extra") or {}).get("salary") or "")])))
        if lo is not None and lo >= smin:
            pay_pts += 10
            reasons.append(f"연봉 표기 {lo:,}만원↑ (기준 {smin:,})")
        elif hi is not None and hi < smin:
            pay_pts -= 15
            reasons.append(f"연봉 표기 최대 {hi:,}만원 < 기준 {smin:,}")
    edu_pts = 0
    want_edu = prof.get("posting_edu_prefer")  # 예: 대졸 — '대졸(4년) 이상 대상' 공고 선호
    if want_edu:
        lvl = posting_edu_level(" ".join(filter(None, [rec.get("edu"), rec.get("title")])))
        if lvl is not None and lvl >= EDU_LEVEL.get(want_edu, 3):
            edu_pts += 8
            reasons.append("대졸 이상 대상 공고")
        elif lvl is not None and 1 <= lvl < EDU_LEVEL.get(want_edu, 3):
            edu_pts -= 8
            reasons.append("학력 요건이 대졸 미만(고졸·초대졸) — 생산·기능직일 수 있음")
        # '학력 무관'(lvl 0)은 감점하지 않는다 — 공공기관 블라인드 채용·대기업 공채도 대부분 학력 무관이다
    score = max(0, role_pts + cond_pts + lic_pts + pref_pts + pay_pts + edu_pts)

    # 3) 지원 이력
    applied_state = None
    for a in applied:
        if a["company_norm"] and a["company_norm"] == rec.get("company_norm"):
            role = norm_text(a.get("role") or "")
            squash = lambda x: re.sub(r"[\s/·,()\[\]-]", "", x)
            toks = [t for t in re.split(r"[\s/·,()\[\]]+", role) if len(t) >= 2]
            whole = bool(role) and squash(role) in squash(core_text)
            sq = squash(core_text)
            all_toks = False
            if len(toks) >= 2 and all(t in sq for t in toks):
                # 단어들이 서로 가까이(직무명 길이+4자 안에) 모여 있을 때만 — '전기 설비보전 및 기계 설계'는 아니다
                starts = [sq.find(t) for t in toks]
                span = max(st + len(t) for st, t in zip(starts, toks)) - min(starts)
                all_toks = span <= len(squash(role)) + 4
            if whole or all_toks:
                # 지원한 직무명이 통째로, 또는 그 단어들이 가까이 모여 있을 때만 같은 직무 — 후보에서 뺀다
                applied_state = "same_opening"
                break
            if len(toks) >= 2 and all(t in squash(core_text) for t in toks):
                applied_state = "possible_same_opening"  # 단어가 흩어져 있다 — 빼지 않고 '같은 공고일 가능성'으로 표시
                continue
            if applied_state not in ("possible_same_opening",):
                applied_state = applied_state or "same_company"

    # 4) 마감 임박
    days_left = None
    if rec.get("deadline"):
        try:
            days_left = (datetime.fromisoformat(rec["deadline"]) - ref).total_seconds() / 86400
            days_left = round(days_left, 1)
        except ValueError:
            pass
    candidate = gate == "ok" and fit in ("direct", "adjacent", "generic") and applied_state != "same_opening"
    return {"gate": gate, "fit": fit, "score": score, "reasons": reasons, "applied": applied_state,
            "days_left": days_left, "candidate": candidate, "rules_version": RULES_VERSION}


DERIVED_JSONL = ("screened.jsonl", "requirements.jsonl")


def load_source_records(survey_dir):
    """crawl.py가 쓴 소스별 jsonl만 읽는다(screened·requirements·diff_* 같은 생성물 제외, source 없는 줄 무시)."""
    recs = []
    for p in sorted(glob.glob(os.path.join(survey_dir, "*.jsonl"))):
        b = os.path.basename(p)
        if b in DERIVED_JSONL or b.startswith(("diff_", "gone_")):
            continue
        recs += [r for r in read_jsonl(p) if r.get("source") and r.get("schema_version")]
    return recs


KIND_FROM_SOURCE = {"alio": "공공기관"}  # 잡알리오는 정의상 공공기관 공고만 있다
# 이름으로 알 수 있는 공공 조직(지방공기업 포함 — 잡알리오에 없는 서울교통공사·도시공사 등)
PUBLIC_NAME = re.compile(r"(공사|공단|진흥원|진흥공단|개발원|관리원|평가원|연구원|공기업|시설관리단|교육청)$")


def infer_company_kind(recs):
    """기업 규모를 주는 소스(자소설닷컴·링커리어·고용24 등)의 값을 같은 회사의 다른 소스 레코드에 전파한다.
    추정이 아니라 '다른 소스가 이 회사를 그렇게 분류했다'는 사실이므로 company_size_inferred로 따로 둔다."""
    kind = {}
    for r in recs:
        k = r.get("company_size") or r.get("company_type") or KIND_FROM_SOURCE.get(r["source"])
        cname = norm_text(r.get("company") or "")
        if not k and PUBLIC_NAME.search(cname) and not re.search(r"\(주\)|㈜|주식회사|\(유\)|유한회사", cname) \
                and not re.search(r"(건설|종합|전기|설비|통신|토목)공사$", cname):
            k = "공공기관"
        if k and r.get("company_norm"):
            kind.setdefault(r["company_norm"], k)
    for r in recs:
        r["proxy_posting"] = _posted_for_other_company(r)
        if not (r.get("company_size") or r.get("company_type")):
            if r["proxy_posting"]:
                r["company_size_inferred"] = None  # 기관이 다른 기업 공고를 대신 올린 경우 — 기관 규모를 붙이지 않는다
                continue
            r["company_size_inferred"] = kind.get(r.get("company_norm"))


def _posted_for_other_company(r):
    t = r.get("title") or ""
    if re.search(r"\[[^\]]*(우수기업|협력사|참여기업|채용대행|파트너)[^\]]*\]", t):
        return True
    for m in re.finditer(r"(?:\(주\)|㈜)\s*([가-힣A-Za-z0-9]{2,15})|([가-힣A-Za-z0-9]{2,15})\s*(?:\(주\)|㈜)", t):
        other = norm_company(m.group(1) or m.group(2))
        if other and other not in (r.get("company_norm") or "") and (r.get("company_norm") or "") not in other:
            return True
    return False


EDU_LEVEL = {"무관": 0, "고졸": 1, "초대졸": 2, "전문대졸": 2, "대졸": 3, "학사": 3, "석사": 4, "박사": 5}


def posting_edu_level(text):
    """공고가 요구하는 최소 학력 단계(0 무관, 1 고졸, 2 초대졸, 3 대졸4년, 4 석사, 5 박사). 표기가 없으면 None."""
    t = norm_text(text)
    if re.search(r"전문\s*학사|초대졸|전문대|대학\s*교?\s*졸업\s*\(?\s*2\s*[,~·]?\s*3?\s*년|대졸\s*\(?\s*2\s*[,~·]?\s*3?\s*년", t):
        return 2
    if re.search(r"박사\s*(이상|학위\s*(소지|이상)|↑)", t):
        return 5
    if re.search(r"석사\s*(이상|↑)", t):
        return 4
    if re.search(r"대졸\s*\(?\s*4\s*년|4\s*년제|대학\s*교?\s*졸업\s*\(?\s*4\s*년|학사\s*(이상|↑|학위)|대졸\s*(이상|↑|신입|공채)", t):
        return 3
    if re.search(r"고졸|고등학교", t):
        return 1
    if re.search(r"학력\s*무관", t):
        return 0
    return None


def parse_salary(text):
    """연봉 표기 → (하한, 상한) 만원/년. 월급은 ×12. '회사내규'·'협의'·표기 없음은 (None, None)."""
    t = norm_text(text).replace(",", "")
    if not t or re.search(r"회사\s*내규|협의|면접\s*후", t) and not re.search(r"\d{3,}\s*만", t):
        return None, None
    vals = []
    for m in re.finditer(r"(월급?|월\s*)?\s*(\d{3,5})\s*(?:~\s*(\d{3,5}))?\s*만\s*원?", t):
        monthly = bool(m.group(1)) or int(m.group(2)) < 1000
        a = int(m.group(2)) * (12 if monthly else 1)
        b = int(m.group(3)) * (12 if monthly else 1) if m.group(3) else a
        vals.append((a, b))
    if not vals:
        return None, None
    return min(v[0] for v in vals), max(v[1] for v in vals)


def group_duplicates(recs):
    """교차 소스 중복 '후보'만 묶는다. 같은 소스 안에서 키가 같으면(직무·지역만 다른 별개 공고일 수 있다) 묶지 않는다.
    그룹 안에서도 마감일이 다르면 묶지 않는다(재공고·다른 차수일 수 있다)."""
    by = defaultdict(list)
    for r in recs:
        by[(r.get("dup_key"), (r.get("deadline") or r.get("deadline_kind") or "")[:10])].append(r)
    for (key, _day), rs in by.items():
        srcs = [r["source"] for r in rs]
        cross = len(rs) > 1 and len(set(srcs)) == len(srcs)  # 소스마다 한 건씩일 때만
        for r in rs:
            r["screen"]["group"] = f"{key}:{_day}" if cross else None
            r["screen"]["possible_duplicate_sources"] = sorted(set(srcs)) if cross else []
            r["screen"]["possible_duplicate_ids"] = [f"{x['source']}:{x['source_id']}" for x in rs if x is not r] if cross else []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("survey_dir")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--applied")
    ap.add_argument("--shortlist", type=int, default=SHORTLIST_MAX,
                    help=f"LLM이 전부 읽을 상위 후보 수(중복 묶은 뒤, 기본 {SHORTLIST_MAX})")
    ap.add_argument("--diff", action="store_true",
                    help="재조사: diff_items.jsonl에 있는 공고만 후보로(직전 판정 승계분은 제외). diff_surveys.py를 먼저 돌린다")
    ap.add_argument("--pref-quota", type=int, default=80,
                    help="shortlist 중 선호 기업 유형 후보에 먼저 배정할 자리 수(기본 80)")
    args = ap.parse_args(argv)
    prof = load_profile(args.profile)
    applied_path = args.applied or prof.get("applied_list")
    if applied_path and not os.path.isabs(applied_path):
        applied_path = os.path.join(os.path.dirname(os.path.abspath(args.profile)), applied_path)
    applied = load_applied(applied_path)
    recs = load_source_records(args.survey_dir)
    infer_company_kind(recs)
    ref = now_kst()
    for r in recs:
        r["screen"] = screen(r, prof, applied, ref)
    group_duplicates(recs)
    if args.diff:
        dp = os.path.join(args.survey_dir, "diff_items.jsonl")
        if not os.path.exists(dp):
            raise SystemExit("--diff: diff_items.jsonl 없음 — diff_surveys.py를 먼저 실행")
        kinds = {f"{d['record']['source']}:{d['record']['source_id']}": d["kind"] for d in read_jsonl(dp)}
        for r in recs:
            k = kinds.get(f"{r['source']}:{r['source_id']}")
            r["screen"]["diff_kind"] = k
            if not k:
                r["screen"]["candidate"] = False  # 변경 없음 — 직전 판정 승계
            elif k == "FIRST_SEEN":
                r["screen"]["score"] -= 10  # 신규인지 모르는 것은 진짜 신규 뒤에 둔다
    order = {"direct": 0, "adjacent": 1, "generic": 2, "weak": 3}
    recs.sort(key=lambda r: (r["screen"]["gate"] != "ok", order[r["screen"]["fit"]], -r["screen"]["score"],
                             r["screen"]["days_left"] if r["screen"]["days_left"] is not None else 9e9))
    write_jsonl(os.path.join(args.survey_dir, "screened.jsonl"), recs)

    # LLM 검토 자료 — 규모가 커도(수천 건) 빠짐없이 '어디까지 읽었는지' 남길 수 있게 3층으로 나눈다.
    rdir = os.path.join(args.survey_dir, "review")
    os.makedirs(rdir, exist_ok=True)
    for old in glob.glob(os.path.join(rdir, "*.md")):
        os.unlink(old)
    reps, seen = [], set()
    # 중복 그룹 대표는 '후보'인 것을 먼저 고른다(--diff에서 변경된 구성원이 변경 없는 구성원에 가려지지 않게)
    for r in sorted(recs, key=lambda x: not x["screen"]["candidate"]):
        g = r["screen"].get("group")
        if g and g in seen:
            continue
        if g:
            seen.add(g)
        reps.append(r)
    cands = [r for r in reps if r["screen"]["candidate"]]
    cands.sort(key=lambda r: (-r["screen"]["score"],
                              r["screen"]["days_left"] if r["screen"]["days_left"] is not None else 9e9))
    # 선호 기업 유형(대기업·공기업 등) 후보에 자리를 먼저 배정한다 — 이들의 종합공채는 제목에 직무가 없어
    # 점수가 낮게 나오지만, 상세를 열면 맞는 직무가 있는 경우가 많다.
    pref = [r for r in cands if any(x.startswith("선호 기업 유형") for x in r["screen"]["reasons"])]
    quota = min(args.pref_quota, len(pref), args.shortlist)
    chosen = {id(r) for r in pref[:quota]}
    rest_c = [r for r in cands if id(r) not in chosen]
    shortlist = pref[:quota] + rest_c[:args.shortlist - quota]
    shortlist.sort(key=lambda r: (-r["screen"]["score"],
                                  r["screen"]["days_left"] if r["screen"]["days_left"] is not None else 9e9))
    overflow = rest_c[args.shortlist - quota:]

    def row(r):
        s = r["screen"]
        roles = ", ".join(o["role"] for o in r.get("openings") or [] if o.get("role"))[:60]
        dl = (r.get("deadline") or r.get("deadline_kind") or "")[:10]
        srcs = "+".join(s.get("possible_duplicate_sources") or [r["source"]])
        if s.get("possible_duplicate_ids"):
            srcs += " (중복 후보: " + ", ".join(s["possible_duplicate_ids"][:10]) + ")"
        tag = f"[{s['diff_kind']}] " if s.get("diff_kind") else ""
        return (f"- {tag}{r['source']}:{r['source_id']} | {r['company']} | {r['title']} | {roles or '-'} | "
                f"{'/'.join(r.get('career') or []) or '-'} | {r.get('employment_type') or '-'} | {dl} | "
                f"{s['fit']}/{s['score']} | {s['applied'] or '-'} | {srcs}")

    nb = 0
    for i in range(0, len(shortlist), BATCH_SIZE):
        nb += 1
        lines = [f"# shortlist_{nb:02d} ({i + 1}~{min(i + BATCH_SIZE, len(shortlist))} / {len(shortlist)}) — 전부 읽는다", "",
                 "형식: id | 회사 | 제목 | 직무 | 경력 | 고용형태 | 마감 | fit/score | 지원이력 | 출처", ""]
        lines += [row(r) for r in shortlist[i:i + BATCH_SIZE]]
        atomic_write(os.path.join(rdir, f"shortlist_{nb:02d}.md"), "\n".join(lines) + "\n")

    def compact(r):
        dl = (r.get("deadline") or r.get("deadline_kind") or "")[:10]
        dup = r["screen"].get("possible_duplicate_ids") or []
        tag = f"[{r['screen']['diff_kind']}] " if r["screen"].get("diff_kind") else ""
        return (f"- {tag}{r['source']}:{r['source_id']} {r['company']} — {r['title'][:70]} ({dl})"
                + (f" · 중복 후보: {', '.join(dup[:3])}" if dup else ""))

    ov = [f"# overflow — 점수 순위 {args.shortlist}위 밖 후보 {len(overflow)}건 (훑어보기·검색용)", ""]
    for fit in ("direct", "adjacent", "generic"):
        part = [r for r in overflow if r["screen"]["fit"] == fit]
        ov += [f"## {fit} ({len(part)})"] + [compact(r) for r in part] + [""]
    atomic_write(os.path.join(rdir, "overflow.md"), "\n".join(ov) + "\n")

    rest = [r for r in reps if not r["screen"]["candidate"] and r["screen"]["gate"] != "expired"]
    ex = [f"# 제외·비후보 색인 {len(rest)}건 — 규칙 오판 점검용", "",
          "경력 전용으로 분류된 것은 전부 훑는다(제목에 신입 가능 신호가 숨어 있을 수 있다). weak는 검색으로 점검한다.", ""]
    for key in ("experienced_only", "excluded_company", "excluded_location", "employment_type_mismatch"):
        part = [r for r in rest if r["screen"]["gate"] == key]
        if part:
            ex += [f"## gate={key} ({len(part)})"] + [compact(r) for r in part] + [""]
    weak = [r for r in rest if r["screen"]["gate"] == "ok"]
    ex += [f"## weak — 직무 키워드·공채 신호 없음 ({len(weak)})"] + [compact(r) for r in weak]
    atomic_write(os.path.join(rdir, "excluded_index.md"), "\n".join(ex) + "\n")

    summary = {
        "generated_at": ref.isoformat(timespec="seconds"),
        "rules_version": RULES_VERSION,
        "profile_fingerprint": profile_fingerprint(prof),
        "total": len(recs),
        "by_source": dict(Counter(r["source"] for r in recs)),
        "gate": dict(Counter(r["screen"]["gate"] for r in recs)),
        "fit_of_ok": dict(Counter(r["screen"]["fit"] for r in recs if r["screen"]["gate"] == "ok")),
        "candidates": sum(r["screen"]["candidate"] for r in recs),
        "applied_same_opening": sum(r["screen"]["applied"] == "same_opening" for r in recs),
        "applied_same_company": sum(r["screen"]["applied"] == "same_company" for r in recs),
        "applied_possible_same_opening": sum(r["screen"]["applied"] == "possible_same_opening" for r in recs),
        "duplicate_groups": len({r["screen"]["group"] for r in recs if r["screen"]["group"]}),
        "shortlist": len(shortlist),
        "shortlist_batches": nb,
        "overflow": len(overflow),
        "shortlist_preferred_company": quota,
        "fit_of_shortlist": dict(Counter(r["screen"]["fit"] for r in shortlist)),
        "shortlist_by_source": dict(Counter(r["source"] for r in shortlist)),
    }
    atomic_write(os.path.join(args.survey_dir, "screen_summary.json"),
                 json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

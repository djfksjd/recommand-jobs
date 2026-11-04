#!/usr/bin/env python3
"""상세 원문 → 요건 근거 추출 + 프로필 대조 (규칙 기반 1차 판정, 최종 판단은 LLM이 원문으로 확인).

  python3 requirements.py SURVEY_DIR --profile recommand-jobs-profile.md

입력  : SURVEY_DIR/details/*.json (crawl.py detail 결과)
출력  : SURVEY_DIR/requirements.jsonl — 공고별
        {id, eligibility, remediation, items:[{type, value, need(필수/우대/불명), status(met/unmet/unknown),
          remediable, quote, offset}], text_incomplete}

eligibility
  eligible_by_text : 추출된 필수요건을 모두 충족(원문 기준). 그래도 LLM이 원문을 다시 읽어 A로 올린다.
  remediable       : 미충족 필수요건이 전부 '보완 가능'(예: 스피킹 점수) — B 후보. 마감 전 충족 가능성은 LLM이 판단.
  ineligible       : 보완 불가능한 필수요건 미충족(예: 경력 3년 이상, 석사 이상, 졸업 시점 불일치)
  unknown          : 본문 불완전(이미지·첨부) 또는 필수요건 판정 불가 — '확인 필요'

need 판정: 문장 주변(앞뒤 60자)에 '우대·가점'이 있으면 우대, '필수·이상인 자·소지자·지원자격'이면 필수, 그 외 불명.
불명은 충족으로 치지 않는다. 우대 미충족은 eligibility에 영향을 주지 않는다.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from jobs_common import norm_text, read_jsonl, write_jsonl  # noqa: E402
from score import load_profile  # noqa: E402

PREF = re.compile(r"우대|가점|가산점|있으면\s*좋|플러스|preferred", re.I)
MUST = re.compile(r"필수|이상\s*(인|인\s*자|소지|보유|취득)|소지자|보유자|취득자|지원\s*자격|자격\s*요건|"
                  r"필요\s*역량|제출\s*(해야|필수)|미제출\s*시|required|must", re.I)

OPIC = re.compile(r"OPIc\s*:?\s*(NL|NM|NH|IL|IM\s*[123]?|IH|AL)?", re.I)
TOS = re.compile(r"(토익\s*스피킹|TOEIC\s*Speaking|TOS)\s*:?\s*(Lv\.?\s*\d|레벨\s*\d|IM\s*[123]?|IH|AL|\d{2,3}\s*점)?", re.I)
TOEIC = re.compile(r"(?<!스피킹\s)(TOEIC|토익)(?!\s*(스피킹|Speaking))\s*:?\s*(\d{3})?\s*점?", re.I)
TEPS = re.compile(r"(TEPS|텝스)\s*:?\s*(\d{3})?", re.I)
LICENSE = re.compile(r"([가-힣A-Za-z]{1,12}\s?(?:산업기사|기사|기능장|기능사|기술사))")
GRAD = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(?:까지\s*)?졸업\s*(?:예정|가능)|졸업\s*예정(?:자|인\s*자|인)?|기\s*졸업자|졸업자에\s*한함")
JOIN = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(?:중\s*)?(?:입사|입사\s*가능|근무\s*가능)")
MAJOR = re.compile(r"([가-힣·/,\s]{1,20}(?:관련\s*)?(?:학과|전공))\s*(?:졸업|전공자|출신)?")
SHIFT = re.compile(r"\d\s*조\s*\d\s*교대|교대\s*근무|교대제|야간\s*근무")
RESIDENCE = re.compile(r"([가-힣]{2,8}(?:시|도|군|구))\s*(?:지역\s*)?(?:에\s*)?(?:주민등록|거주|주소지|소재)\s*(?:자|하는\s*자|한\s*자|요건)?")
# "주민등록상 주소지가 인천광역시로 되어 있는 사람"처럼 지역명이 뒤에 오는 형태 (2026-11 지방공기업 공고 누락 사례)
RESIDENCE_AFTER = re.compile(r"(?:주민등록|주소지|거주지)[^.\n]{0,25}?([가-힣]{2,8}(?:특별자치시|특별자치도|특별시|광역시|도|시|군))"
                             r"\s*(?:로|으로|에|내)\s*(?:되어|등록|두|거주|소재|있)")
# 지역이 문장에 없거나 표·첨부에 있을 때도 "지역 요건이 있다"는 사실만은 잡는다
RESIDENCE_GENERIC = re.compile(r"거주지?\s*(?:요건|제한)|지역\s*제한|지역인재|이전지역")
RESIDENCE_NONE = re.compile(r"(?:거주지?|지역)\s*(?:요건|제한)\s*(?:이|은|는)?\s*없|제한\s*없음|\(전국\)")
DRIVER = re.compile(r"(1종|2종)?\s*(보통|대형|특수)?\s*운전\s*면허")
JOIN_NOW = re.compile(r"(최종\s*)?합격\s*(후|시)\s*즉시\s*(입사|근무)|즉시\s*(입사|근무|출근)\s*가능")
CAREER_MIN = re.compile(r"경력\s*(\d{1,2})\s*년\s*(?:이상|↑)")
DEGREE = re.compile(r"(석사|박사)\s*(?:학위)?\s*(?:이상|소지|취득)")

OPIC_ORDER = ["NL", "NM", "NH", "IL", "IM1", "IM2", "IM3", "IH", "AL"]


HEADER = re.compile(r"(지원\s*자격|자격\s*요건|필수\s*(요건|사항|자격)|응시\s*자격|공통\s*자격|기본\s*자격|"
                    r"우대\s*(사항|요건|조건)|가점\s*(사항)?|우대\s*(?=[\]】>:\n])|"
                    r"근무\s*(형태|조건|환경|지|시간)|전형\s*(절차|방법|일정)|접수\s*(기간|방법)|급여|복리\s*후생|"
                    r"제출\s*서류|기타\s*사항|모집\s*(분야|부문|직무)|담당\s*업무|주요\s*업무|본문)")
NEUTRAL_HEADER = re.compile(r"근무|전형|접수|급여|복리|제출|기타|모집|담당|주요|본문")
SENT_END = re.compile(r"[.。!?]\s|\n|•|·\s|※|\[|【|■|◆|●|▶|- ")


def _sentence(text, start, end):
    a = start
    while a > 0 and not SENT_END.match(text, a - 1) and start - a < 160:
        a -= 1
    b = end
    while b < len(text) and not SENT_END.match(text, b) and b - end < 160:
        b += 1
    return text[a:b]


def _section(text, start):
    """match 앞에서 가장 가까운 섹션 제목(지원자격/우대사항...). 너무 멀면(1,500자) 무시."""
    last = None
    for h in HEADER.finditer(text, max(0, start - 1500), start):
        last = h
    if not last or NEUTRAL_HEADER.match(last.group(0)):
        return None
    return "우대" if re.match(r"우대|가점", last.group(0)) else "필수"


def _need(text, start, end):
    sent = _sentence(text, start, end)
    if PREF.search(sent):
        return "우대", sent
    sec = _section(text, start)
    if sec:
        return sec, sent
    if MUST.search(sent):
        return "필수", sent
    return "불명", sent


def _item(text, m, typ, value):
    need, sent = _need(text, m.start(), m.end())
    return {"type": typ, "value": value, "need": need, "quote": norm_text(sent)[:220], "offset": m.start(),
            "end": m.end(), "status": "unknown", "remediable": False}


def extract(text):
    text = text or ""
    items = []
    for m in OPIC.finditer(text):
        items.append(_item(text, m, "speaking", ("OPIc " + (m.group(1) or "")).strip().replace(" ", "")))
    for m in TOS.finditer(text):
        items.append(_item(text, m, "speaking", ("토익스피킹 " + (m.group(2) or "")).strip()))
    for m in TOEIC.finditer(text):
        items.append(_item(text, m, "toeic", int(m.group(3)) if m.group(3) else None))
    for m in TEPS.finditer(text):
        items.append(_item(text, m, "teps", int(m.group(2)) if m.group(2) else None))
    seen = set()
    for m in LICENSE.finditer(text):
        name = m.group(1).replace(" ", "")
        if name in seen or len(name) < 3 or re.search(r"운전기사|간호|기사님", name):
            continue
        seen.add(name)
        items.append(_item(text, m, "license", name))
    for m in GRAD.finditer(text):
        val = f"{m.group(1)}-{int(m.group(2)):02d}" if m.group(1) else norm_text(m.group(0))
        items.append(_item(text, m, "graduation", val))
    for m in JOIN.finditer(text):
        items.append(_item(text, m, "join_date", f"{m.group(1)}-{int(m.group(2)):02d}"))
    res_spans = []
    for rx in (RESIDENCE, RESIDENCE_AFTER):
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in res_spans):
                continue
            res_spans.append((m.start(), m.end()))
            items.append(_item(text, m, "residence", m.group(1)))
    for m in RESIDENCE_GENERIC.finditer(text):
        if any(abs(m.start() - a) < 200 for a, _b in res_spans):
            continue  # 같은 요건을 지역명과 함께 이미 잡았다
        it = _item(text, m, "residence", "지역 요건(지역명 원문 확인)")
        if RESIDENCE_NONE.search(it["quote"]):
            it["note"] = "일부 분야는 거주 제한 없음 — 지원 분야가 어디에 속하는지 확인"
        res_spans.append((m.start(), m.end()))
        items.append(it)
    for m in DRIVER.finditer(text):
        items.append(_item(text, m, "driver_license", norm_text(m.group(0))))
        break
    for m in JOIN_NOW.finditer(text):
        items.append(_item(text, m, "join_immediate", norm_text(m.group(0))))
        break
    for m in CAREER_MIN.finditer(text):
        items.append(_item(text, m, "career_min_years", int(m.group(1))))
    for m in DEGREE.finditer(text):
        items.append(_item(text, m, "degree", m.group(1)))
    for m in SHIFT.finditer(text):
        items.append(_item(text, m, "shift", norm_text(m.group(0))))
        break
    for m in MAJOR.finditer(text):
        val = norm_text(m.group(1))
        if 2 <= len(val) <= 25:
            items.append(_item(text, m, "major", val))
    return items


OR_WORD = re.compile(r"또는|혹은|\bor\b|중\s*(1|하나|택\s*1)|하나\s*이상|택\s*1|이상\s*/|/\s*(OPIc|토익|TOEIC|TEPS)", re.I)
QUAL_TYPES = ("license", "speaking", "toeic", "teps", "major", "degree")  # '전기기사 또는 관련학과 졸업자'도 대체요건
LEVELS = ["기능사", "산업기사", "기사", "기능장", "기술사"]


def _license_level(name):
    for lv in sorted(LEVELS, key=len, reverse=True):
        if name.endswith(lv):
            return name[: -len(lv)], LEVELS.index(lv)
    return name, None


AND_WORD = re.compile(r"및|그리고|와\s|과\s|모두|함께|동시|,\s*(?!또는)")


TRAIL_OR = re.compile(r"^[^.\n]{0,25}?(중\s*(1|하나|택\s*1)|택\s*1|하나\s*이상|어느\s*하나|중\s*한\s*가지)")


def group_alternatives(items, text=""):
    """인접한 자격·어학 요건을 대체요건(하나만 충족하면 됨)으로 묶는다. 연결 판단은 항목 사이 글로 한다:
      '또는/혹은/or'  → OR 연결     '및/그리고/모두' → 끊김(독립 필수)
      ','만 있음      → 보류 — 같은 사슬에 OR 연결이 있거나 끝에 '중 1/택1'이 오면 OR ("A, B 또는 C", "A, B 중 1")
    확신할 수 없으면 묶지 않는다(각각 판정 → 못 채우면 '확인 필요'). 줄바꿈을 넘어도 된다."""
    quals = sorted([i for i in items if i["type"] in QUAL_TYPES], key=lambda i: i["offset"])
    chains, cur, kinds = [], [], []
    for it in quals:
        if cur and text:
            prev = cur[-1]
            gap = text[prev.get("end", prev["offset"]): it["offset"]]
            gap_core = re.sub(r"(자격증|자격|면허|소지자?|보유자?|취득자?|이상|점|등급|\(.*?\)|\d+|[A-Za-z\-]{1,10}|\s)", "", gap)
            if len(gap) <= 60 and re.search(r"또는|혹은|\bor\b", gap, re.I) and not re.search(r"및|그리고|모두", gap):
                kinds.append("or")
                cur.append(it)
                continue
            if len(gap) <= 25 and re.fullmatch(r"[,，/·]?", gap_core or ""):
                kinds.append("comma")
                cur.append(it)
                continue
            chains.append((cur, kinds))
            cur, kinds = [], []
        cur = [it] if not cur else cur
    if cur:
        chains.append((cur, kinds))
    gid = 0
    for chain, ks in chains:
        if len(chain) < 2:
            continue
        last = chain[-1]
        tail = text[last.get("end", last["offset"]): last.get("end", last["offset"]) + 40] if text else ""
        langs_only = all(i["type"] in ("toeic", "teps", "speaking") for i in chain)
        # 서로 다른 어학 시험을 쉼표로 늘어놓은 것은 거의 항상 '이 중 하나'다(공공기관 기준점수 표)
        if "or" in ks or TRAIL_OR.search(tail) or re.match(r"\s*(또는|혹은)", tail) or \
                (langs_only and len({i["type"] for i in chain} | {str(i["value"])[:4] for i in chain}) > 1):
            gid += 1
            for it in chain:
                it["group"] = gid
    return items


SIDO = {"서울": "서울", "부산": "부산", "대구": "대구", "인천": "인천", "광주광역": "광주", "대전": "대전", "울산": "울산",
        "세종": "세종", "경기": "경기", "강원": "강원", "충청북": "충북", "충북": "충북", "충청남": "충남", "충남": "충남",
        "전라북": "전북", "전북": "전북", "전라남": "전남", "전남": "전남", "경상북": "경북", "경북": "경북",
        "경상남": "경남", "경남": "경남", "제주": "제주"}


def _sido(name):
    """시·도 단위면 약칭, 아니면 None(‘광주시’(경기)처럼 시·군·구는 시·도로 단정하지 않는다)."""
    n = re.sub(r"\s", "", name or "")
    if re.search(r"(특별시|광역시|특별자치시|특별자치도|도)$", n) or n in SIDO.values():
        for k, v in SIDO.items():
            if n.startswith(k):
                return v
    return None


def _ym(s):
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{1,2})", str(s))
    return (int(m.group(1)), int(m.group(2))) if m else None


def check(items, prof, today=None, full_text=None):
    today = today or date.today()
    langs = prof.get("languages") or []
    toeic_rows = [lg for lg in langs if str(lg.get("test", "")).upper() == "TOEIC"]
    toeic = max([lg.get("score") or 0 for lg in toeic_rows if _valid2y(lg.get("date"), today)] or [0])
    toeic_undated = any(not _date(lg.get("date")) for lg in toeic_rows)
    speaking = prof.get("speaking")
    lic_names = {(x.get("name") if isinstance(x, dict) else str(x)) for x in prof.get("licenses") or []}
    want_career = set(prof.get("career") or [])
    for it in items:
        t, v = it["type"], it["value"]
        if t == "speaking":
            if not speaking or speaking == "none":
                it["status"], it["remediable"] = "unmet", True
            else:
                it["status"] = "unknown"  # 등급 비교는 LLM이(점수 체계가 섞여 있다)
        elif t == "toeic" and isinstance(v, int):
            if toeic >= v:
                it["status"] = "met"
            elif toeic_undated:
                it["status"] = "unknown"  # 응시일을 모르면 유효기간을 판단할 수 없다
            else:
                it["status"], it["remediable"] = "unmet", True
        elif t == "license":
            field, lv = _license_level(v)
            above = bool(re.search(re.escape(v) + r"\s*(이상|이상의|급\s*이상)", it["quote"]))
            have = [_license_level(n) for n in lic_names]
            if v in lic_names:
                it["status"] = "met"
            elif above and lv is not None and any(hl is not None and hl >= lv and (not field or hf == field)
                                                  for hf, hl in have):
                it["status"] = "met"  # '전기산업기사 이상' ← 전기기사 보유
                it["note"] = "상위 등급 보유"
            elif lv is not None and any(hl is not None and hl > lv and hf == field for hf, hl in have):
                it["status"] = "unknown"  # 같은 분야 상위 등급 보유 — 대개 인정되지만 공고마다 다르다
                it["note"] = "상위 등급 보유 — 인정 여부 확인"
            else:
                it["status"] = "unmet" if it["need"] == "필수" else "unknown"
            it["remediable"] = False
        elif t == "graduation" and _ym(v):
            g = _ym(prof.get("graduation"))
            it["status"] = "met" if g and g <= _ym(v) else ("unmet" if g else "unknown")
        elif t == "graduation":
            g = _ym(prof.get("graduation"))
            future = g and g > (today.year, today.month)
            if "예정" in str(v):
                it["status"] = "met" if future else "unknown"
            elif "기" in str(v) or "한함" in str(v):
                # '기졸업자'만 있고 '졸업 예정'이 본문 어디에도 없으면 졸업 예정자는 불가
                anywhere = re.search(r"졸업\s*예정", full_text or "") or any(
                    "예정" in str(o["value"]) or _ym(o["value"]) for o in items if o["type"] == "graduation")
                it["status"] = "unmet" if future and not anywhere else "met"
        elif t == "join_date" and _ym(v):
            a = _ym(prof.get("available_from"))
            it["status"] = "met" if a and a <= _ym(v) else ("unmet" if a else "unknown")
        elif t == "residence":
            res = prof.get("residence") or []
            res = [res] if isinstance(res, str) else res
            need_sido = _sido(v)
            have = {_sido(x) or x for x in res}
            if not res or not need_sido:
                it["status"] = "unknown"  # 시·군·구 단위 요건이거나 프로필에 없음 — 원문 기준일로 확인
            else:
                it["status"] = "met" if need_sido in have else "unmet"
            base = "거주지·지역인재 요건 — 지원 분야에 적용되는지, 기준일(공고일 전 N년 등)·합산 기간 조건을 원문으로 확인"
            it["note"] = (it.get("note") + " / " + base) if it.get("note") else base
            # 거주 요건은 '보완'이 불가능하다(마감 전에 주민등록을 옮겨도 기간 조건을 못 채운다)
            it["remediable"] = False
        elif t == "driver_license":
            have = re.sub(r"\s", "", str(prof.get("driver_license") or ""))
            need = re.sub(r"\s", "", v)
            if not have:
                it["status"] = "unknown"
            elif re.search(r"없음|미보유|무", have):
                it["status"] = "unmet"
            elif "대형" in need or "특수" in need:
                kind = "대형" if "대형" in need else "특수"
                it["status"] = "met" if kind in have else "unmet"
            elif "1종" in need:
                it["status"] = "met" if "1종" in have else "unmet"
            else:
                it["status"] = "met"
        elif t == "join_immediate":
            a = _ym(prof.get("available_from"))
            it["status"] = "met" if a and a <= (today.year, today.month) else "unknown"
            it["note"] = "즉시 입사 요구 — 합격 발표일·입사일을 원문에서 보고, 그 시점에 근무 가능한지 사용자에게 확인"
        elif t == "career_min_years":
            q = it["quote"]
            if re.search(r"신입\s*(지원\s*)?(불가|제외|불가능)", q):
                it["status"] = "unmet" if "경력" not in want_career else "unknown"
            elif re.search(r"신입\s*(/|또는|및|·|,|혹은)\s*경력|경력\s*(/|또는|혹은)\s*신입|경력\s*무관|신입\s*(지원\s*)?가능", q):
                it["status"] = "met"  # "신입 / 경력 3년 이상" — 신입으로 지원하는 길이 같은 문장에 있다
                it["note"] = "신입 대안 있음"
            elif re.search(r"신입|졸업\s*예정|경력\s*무관", full_text or "") and not re.search(r"신입\s*(지원\s*)?(불가|제외)", full_text or ""):
                it["status"] = "unknown"  # 본문에 신입 경로가 따로 있다 — 이 경력 요건은 다른 직무·직급 것일 수 있다
                it["note"] = "직무·직급별 요건 확인"
            else:
                it["status"] = "unmet" if "경력" not in want_career and v >= 1 else "unknown"
        elif t == "degree":
            it["status"] = "unmet" if (prof.get("education") or "") not in ("석사", "박사") else "unknown"
        elif t == "shift":
            ok = prof.get("shift_work_ok")
            it["status"] = "met" if ok is True else ("unmet" if ok is False else "unknown")
        elif t == "major":
            it["status"] = "unknown"  # 학과 인정 여부는 추정하지 않는다
    return items


def _date(d):
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(d or ""))
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _valid2y(d, today):
    """어학 성적 유효기간 2년(응시일 기준, 날짜 단위). 응시일을 모르면 유효로 치지 않는다."""
    dd = _date(d)
    if not dd:
        return False
    try:
        exp = dd.replace(year=dd.year + 2)
    except ValueError:  # 2/29
        exp = dd.replace(year=dd.year + 2, day=28)
    return today < exp


def _collapse(items):
    """대체요건 그룹은 하나로 접는다: 하나라도 met → met, 전부 unmet → unmet(하나라도 보완 가능하면 보완 가능)."""
    out, groups = [], {}
    for i in items:
        if i.get("group"):
            groups.setdefault(i["group"], []).append(i)
        else:
            out.append(i)
    for g in groups.values():
        need = "필수" if any(x["need"] == "필수" for x in g) else g[0]["need"]
        if any(x["status"] == "met" for x in g):
            st, rem = "met", False
        elif all(x["status"] == "unmet" for x in g):
            st, rem = "unmet", any(x["remediable"] for x in g)
        else:
            st, rem = "unknown", False
        out.append({"type": "or_group", "need": need, "status": st, "remediable": rem})
    return out


def verdict(items, text_incomplete, n_openings=1):
    lic = [i for i in items if i["type"] == "license" and i["need"] == "필수" and not i.get("group")]
    if any(i["status"] == "met" for i in lic) and any(i["status"] == "unmet" for i in lic):
        for i in lic:
            if i["status"] == "unmet":
                i["status"] = "unknown"  # "전기직: 전기기사 / 기계직: 일반기계기사" — 다른 직무 요건일 수 있다
                i["note"] = "직무별 자격 요건일 수 있음 — 지원 직무 기준으로 확인"
    flat = _collapse(items)
    musts = [i for i in flat if i["need"] == "필수"]
    unmet = [i for i in musts if i["status"] == "unmet"]
    if unmet and not all(i["remediable"] for i in unmet):
        # 모집 직무가 여럿이면 다른 직무의 요건일 수 있다 — 단정하지 않는다
        return ("unknown", "per_opening_check") if n_openings > 1 else ("ineligible", "not_feasible")
    if unmet:
        return "remediable", "check_deadline"
    if text_incomplete:
        return "unknown", None
    if any(i["status"] == "unknown" for i in musts):
        return "unknown", None
    if any(i["need"] == "불명" and i["status"] in ("unmet", "unknown") for i in flat
           if i["type"] not in ("major",)):
        return "unknown", None  # 필수인지 우대인지 모르는 요건을 못 채웠다 — 충족으로 치지 않는다
    return "eligible_by_text", None


# 공공기관·지방공기업·병원·대학 — 분야별 거주지·가점·제한 요건이 공고문(첨부)에만 있는 경우가 많다
PUBLIC_LIKE = re.compile(r"공단|공사|공기업|진흥원|재단|연구원|병원|대학교|위원회|센터|협회|중앙회|"
                         r"시청|군청|구청|도청|교육청|공공|관리원|평가원|개발원")
SUMMARY_CHARS = 1500  # 채용 사이트 본문이 이보다 짧으면 요약본일 가능성이 크다


def _company_map(survey_dir):
    m = {}
    for jp in glob.glob(os.path.join(survey_dir, "*.jsonl")):
        if os.path.basename(jp) in ("requirements.jsonl", "screened.jsonl"):
            continue
        for r in read_jsonl(jp):
            if r.get("source") and r.get("source_id") is not None:
                m[f"{r['source']}:{r['source_id']}"] = r.get("company") or ""
    return m


def _official(detail_path, company=""):
    """('fetched'|'thin'|'none', text). .official.json(crawl.py official)과 .official.txt(에이전트 저장) 모두 읽는다."""
    stem = detail_path[:-len(".json")]
    texts, statuses = [], []
    jp = stem + ".official.json"
    if os.path.exists(jp):
        o = json.load(open(jp, encoding="utf-8"))
        for part in o.get("parts") or []:
            if part.get("why") == "site_is_original" and PUBLIC_LIKE.search(company or ""):
                continue  # 공공기관은 공고문 원본이 따로 있다 — 확인 기록으로 대신하지 않는다
            statuses.append(part.get("status"))
        if o.get("text"):
            texts.append(o["text"])
    tp = stem + ".official.txt"
    if os.path.exists(tp):
        t = open(tp, encoding="utf-8").read().strip()
        if t:
            texts.append("[공식 원문: 에이전트 저장]\n" + t)
            statuses.append("ok" if len(t) >= 300 else "thin")
    if not statuses and not texts:
        return "none", ""
    status = "fetched" if "ok" in statuses else "thin"
    return status, "\n\n".join(texts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("survey_dir")
    ap.add_argument("--profile", required=True)
    args = ap.parse_args(argv)
    prof = load_profile(args.profile)
    companies = _company_map(args.survey_dir)
    out = []
    for p in sorted(glob.glob(os.path.join(args.survey_dir, "details", "*.json"))):
        if p.endswith(".official.json"):
            continue
        d = json.load(open(p, encoding="utf-8"))
        # 섹션(제목 포함)을 앞에 두고 본문을 뒤에 붙인다 — 같은 요건이 두 번 잡히면 섹션 쪽(제목으로 필수/우대가 확실)을 쓴다
        secs = "".join(f"[{name}]\n{sec}\n" for name, sec in (d.get("sections") or {}).items() if sec)
        site_text = secs + "\n[본문]\n" + (d.get("text") or "") if secs else (d.get("text") or "")
        official_status, official_text = _official(
            p, companies.get(f"{d.get('source')}:{d.get('source_id')}", ""))
        # 공식 원문을 앞에 둔다 — 요약본과 겹치는 요건은 공식 원문 쪽 인용이 남는다
        text = (official_text + "\n\n[채용 사이트 본문]\n" + site_text) if official_text else site_text
        raw = extract(text)
        seen_kv, items0 = set(), []
        for it in raw:
            kv = (it["type"], str(it["value"]))
            if kv in seen_kv:
                continue
            seen_kv.add(kv)
            items0.append(it)
        items = check(group_alternatives(items0, text), prof, full_text=text)
        incomplete = bool(d.get("text_incomplete")) or len(text) < 150
        n_open = len([o for o in d.get("openings") or [] if o.get("role")])
        el, rem = verdict(items, incomplete, max(1, n_open))
        rid = f"{d.get('source')}:{d.get('source_id')}"
        company = companies.get(rid, "")
        needs_official = bool(PUBLIC_LIKE.search(company) or d.get("attachments")
                              or len(site_text) < SUMMARY_CHARS)
        if el == "eligible_by_text" and official_status != "fetched" and needs_official:
            # 요약본만 보고 '충족'이라 하지 않는다 — 거주지·분야별 요건은 공식 공고문에만 있는 경우가 많다
            el, rem = "unknown", "official_unverified"
        out.append({"id": rid, "company": company, "url": d.get("url"),
                    "employer_url": d.get("employer_url"), "eligibility": el, "remediation": rem,
                    "official_status": official_status, "needs_official": needs_official,
                    "text_incomplete": incomplete, "text_chars": len(text), "items": items})
    write_jsonl(os.path.join(args.survey_dir, "requirements.jsonl"), out)
    from collections import Counter
    print(json.dumps({"details": len(out), "eligibility": dict(Counter(o["eligibility"] for o in out))},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

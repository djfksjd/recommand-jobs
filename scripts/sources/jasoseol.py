"""자소설닷컴(jasoseol.com) 어댑터 — 대기업·공기업 공채 달력.

실측(2026-11-03):
  * robots.txt `User-agent: *` 는 `/` 허용, `/crt/*`·`/core`·`/webview/` 등만 불허.
  * 목록: `POST /employment/calendar_list.json` {start_time, end_time}
      → {"employment": [...]} — 기간과 겹치는(시작 또는 마감이 창 안) 공고. 키워드 없이 전수.
      각 항목: id, name(회사), title, start_time, end_time, business_size(big_business/middle_market/None),
      business_type(public_institution/None), employments[{id, division, duty_groups}]
  * division 코드(목록 제목으로 대조): 1=신입, 2=경력, 3=인턴, 4=계약직, 5=신입·경력 혼재/수시(불확실 → 원값 보존),
    7=교육·기타(채용 아님).
  * 상세: `/recruit/{id}` 의 __NEXT_DATA__.props.pageProps.initialEmploymentCompany
      → employments[{id, field(직무명), division}], employment_page_url(기업 원 채용페이지), content(HTML, 종종 이미지)
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

from jobs_common import (Blocked, make_opening, make_record, norm_text, now_kst)

NAME = "jasoseol"
LABEL = "자소설닷컴"
HOSTS = ("jasoseol.com",)
KIND = "full"  # 키워드 없이 전수 수집
PLAN_KEYS = ("career", "days_ahead", "page_cap")  # 키워드는 이 소스의 결과에 영향이 없다
BASE = "https://jasoseol.com"

DIVISION = {1: "신입", 2: "경력", 3: "인턴", 4: "계약", 5: None, 7: None}
DIVISION_LABEL = {1: "신입", 2: "경력", 3: "인턴", 4: "계약직", 5: "신입·경력/수시(코드5)", 7: "교육·기타(코드7)"}
SIZE = {"big_business": "대기업", "middle_market": "중견", None: None}


def _career_of(divisions):
    out = []
    for d in divisions:
        c = DIVISION.get(d)
        if c:
            out.append(c)
    return out


def list_postings(f, plan):
    """plan: {"days_ahead": int(기본 120)} — 오늘~N일 창에서 마감이 지나지 않은 공고 전부."""
    now = now_kst()
    days = int(plan.get("days_ahead") or 120)
    body = {
        "start_time": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "end_time": (now + timedelta(days=days)).replace(hour=23, minute=59, second=59, microsecond=0).isoformat(),
    }
    data = f.post_json(f"{BASE}/employment/calendar_list.json", body,
                       headers={"Referer": f"{BASE}/recruit", "Accept": "application/json"})
    if not isinstance(data, dict) or "employment" not in data:
        raise RuntimeError("응답 구조 변경: employment 키 없음")
    items = data["employment"]
    recs, seen, skipped_edu = [], set(), 0
    for e in items:
        if e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        divisions = [em.get("division") for em in (e.get("employments") or [])]
        if divisions and all(d == 7 for d in divisions):
            skipped_edu += 1  # 교육·참여자 모집 — 채용 아님
            continue
        end = e.get("end_time")
        if end and end < now.isoformat():
            continue
        openings = [make_opening(role=None, opening_id=em.get("id"), career=_career_of([em.get("division")]) or None,
                                 extra={"division": em.get("division"),
                                        "duty_group_ids": [g.get("group_id") for g in (em.get("duty_groups") or [])]})
                    for em in (e.get("employments") or [])]
        cg = e.get("company_group") or {}
        rec = make_record(
            source=NAME, source_id=e["id"], url=f"{BASE}/recruit/{e['id']}",
            company=e.get("name") or cg.get("name"), title=e.get("title"),
            openings=openings,
            career=sorted(set(_career_of(divisions))) or None,
            deadline=(end[:16] + "+09:00") if end else None,
            deadline_kind="date" if end else "unknown",
            deadline_precision="minute" if end else None,
            deadline_raw=end, posted_at=e.get("start_time"),
            company_size=SIZE.get(e.get("business_size") or cg.get("business_size")),
            company_type="공공기관" if (e.get("business_type") or cg.get("business_type")) == "public_institution" else None,
            extra={"division_labels": sorted({DIVISION_LABEL.get(d, str(d)) for d in divisions}),
                   "roles_need_detail": True},
        )
        recs.append(rec)
    return {"records": recs, "pages": 1, "reported_total": len(items), "excluded": skipped_edu,
            "stop_reason": "calendar-window", "errors": [],
            "scope": f"전수(오늘~{days}일 창, 교육 {skipped_edu}건 제외)"}


def fetch_detail(f, rec):
    """직무명(field)·원 채용페이지·본문 텍스트. 본문이 이미지뿐이면 text_incomplete=True."""
    html = f.get(f"{BASE}/recruit/{rec['source_id']}")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("상세 __NEXT_DATA__ 없음(구조 변경 또는 차단)")
    pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    ec = pp.get("initialEmploymentCompany") or {}
    if not ec:
        raise Blocked("상세 데이터 비어 있음(로그인 요구 가능)")
    openings = []
    for em in ec.get("employments") or []:
        d = em.get("division")
        openings.append(make_opening(role=em.get("field"), opening_id=em.get("id"),
                                     career=_career_of([d]) or None,
                                     extra={"division": d, "division_label": DIVISION_LABEL.get(d, str(d))}))
    content = ec.get("content") or ""
    text = norm_text(re.sub(r"<(br|/p|/div|/li|/tr)[^>]*>", "\n", content, flags=re.I))
    imgs = len(re.findall(r"<img\b", content, re.I))
    return {
        "openings": openings,
        "text": text,
        "text_incomplete": len(text) < 200 and imgs > 0,  # 본문이 이미지 → 자격요건은 원 채용페이지에서 확인
        "images": imgs,
        "employer_url": ec.get("employment_page_url"),
        "attached_file_url": ec.get("attached_file_url"),
        "deadline_raw": ec.get("end_time"),
        "posted_at": ec.get("start_time"),
    }

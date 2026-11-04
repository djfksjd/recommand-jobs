import os
from datetime import datetime

import pytest

from conftest import FIXTURES
from jobs_common import KST
from sources import saramin as sr

REF = datetime(2026, 11, 3, 10, 0, tzinfo=KST)


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def list_html():
    return _read("saramin_list.html")


def test_parse_list_cards(list_html):
    r = sr.parse_list(list_html)
    assert r["total"] == 9477
    assert [it["id"] for it in r["items"]] == ["55451765", "55362507", "55425275", "55442014", "55375529"]
    it = r["items"][0]
    assert it["title"] == "채비(주) 전기안전관리 담당자 채용"
    assert it["company"] == "채비(주)"
    assert it["locations"] == ["대구 수성구"]
    assert it["career"] == "신입" and it["edu"] == "초대졸↑" and it["employment_type"] == "정규직"
    assert it["deadline_raw"] == "~ 11/16(월)"
    assert "전기설비" in it["tags"]
    assert r["items"][1]["edu"] == "학력무관"  # '무관'이지만 경력으로 오분류하지 않는다


def test_to_record(list_html):
    rec = sr.to_record(sr.parse_list(list_html)["items"][0], ref=REF)
    assert rec["source"] == "saramin" and rec["source_id"] == "55451765"
    assert rec["url"] == "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=55451765"
    assert rec["deadline"] == "2026-11-16T23:59+09:00" and rec["deadline_precision"] == "day"
    assert rec["career"] == ["신입"] and rec["edu"] == "초대졸↑"
    assert rec["posted_at"] is None  # '수정일'은 게시일이 아니다


def test_registered_day_is_posted_at(list_html):
    items = {it["id"]: it for it in sr.parse_list(list_html)["items"]}
    rec = sr.to_record(items["55442014"], ref=REF)  # '등록일 26/10/30'
    assert rec["posted_at"] == "2026-10-30"


def test_parse_list_structure_change_raises():
    with pytest.raises(RuntimeError):
        sr.parse_list("<html>no list</html>")


def test_career_params_and_url():
    assert sr.career_params(["신입", "인턴"]) == {"exp_cd": "1", "exp_none": "y"}
    assert sr.career_params(["경력"]) == {}
    u = sr.search_url("전기", {"exp_cd": "1", "exp_none": "y"}, 3)
    assert "exp_cd=1" in u and "exp_none=y" in u and "recruitPage=3" in u and "recruitPageCount=100" in u


def test_parse_detail_summary_and_body():
    d = sr.parse_detail(_read("saramin_detail.html"), _read("saramin_detail_body.html"))
    assert d["text_incomplete"] is False
    assert "전기안전관리" in d["text"]
    assert d["deadline_raw"] == "2026.11.16 23:59" and d["posted_at"] == "2026.11.02 10:00"
    assert d["edu"] == "대졸(2,3년제) 이상" and d["career_raw"] == "신입"
    s = d["sections"]
    assert s["자격요건"].startswith("경력: 신입 / 학력: 대졸(2,3년제) 이상")
    assert "전기산업기사 또는 전기기사" in s["자격요건"]
    assert s["우대사항"].startswith("• 전기관련 학과 우대")
    assert "근무조건" not in s["우대사항"]  # 다음 머리글에서 끊는다
    assert s["근무지"].startswith("(42250) 대구 수성구")
    assert s["접수기간"] == "2026.11.02 10:00 ~ 2026.11.16 23:59"
    assert [o["role"] for o in d["openings"]] == ["전기 안전관리자"]


def test_parse_detail_body_only_and_image_only():
    d = sr.parse_detail(None, _read("saramin_detail_body.html"))
    assert d["deadline_raw"] is None and "자격요건" in d["sections"]
    img = '<html><body><div class="user_content"><img src="https://x.saramin.co.kr/a.jpg"></div></body></html>'
    d2 = sr.parse_detail(None, img)
    assert d2["text_incomplete"] is True and d2["images"] == 1


def test_parse_api_unverified_shape():
    """미검증(키 없음): 공식 문서 형식으로 만든 응답."""
    data = {"jobs": {"count": 2, "start": 0, "total": "2", "job": [
        {"id": "123", "url": "x", "active": 1,
         "company": {"detail": {"name": "(주)테스트"}},
         "position": {"title": "전기 신입", "location": {"name": "서울 &gt; 강남구,경기 &gt; 수원시"},
                      "job-type": {"name": "정규직"}, "experience-level": {"code": 1, "name": "신입"},
                      "required-education-level": {"name": "대학교졸업(4년)이상"}},
         "keyword": "전기,설비", "salary": {"name": "회사내규에 따름"},
         "opening-timestamp": "1793664000", "expiration-timestamp": "1794581940",
         "close-type": {"code": "1", "name": "접수마감일"}},
        {"id": "124", "company": {"detail": {"name": "B"}}, "position": {"title": "t"},
         "close-type": {"code": "2", "name": "채용시"}}]}}
    r = sr.parse_api(data, ref=REF)
    assert r["total"] == 2
    a, b = r["items"]
    assert a["locations"] == ["서울 강남구", "경기 수원시"] and a["deadline_kind"] == "date"
    assert a["career"] == ["신입"] and a["extra"]["via"] == "api"
    assert b["deadline_kind"] == "채용시" and b["may_close_early"] is True


class FakeFetcher:
    def __init__(self, html):
        self.html = html
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.html


def test_list_postings_dedupe(monkeypatch, list_html):
    monkeypatch.delenv("SARAMIN_ACCESS_KEY", raising=False)
    monkeypatch.setattr(sr, "now_kst", lambda: REF)
    f = FakeFetcher(list_html)
    res = sr.list_postings(f, {"keywords": ["전기", "계장"], "career": ["신입", "인턴"], "max_pages": 5})
    assert len(f.urls) == 4  # kw1 p1(새 5) → p2(이 키워드 새 0) 중단, kw2 p1(교차 중복이지만 이 키워드엔 새것) → p2 중단
    assert res["duplicates"] == 15 and len(res["records"]) == 5
    assert res["stop_reason"] == "exhausted" and res["partial"] is False
    assert "HTML 검색" in res["scope"] and "전기" not in res["scope"]


def test_list_postings_page_cap(monkeypatch, list_html):
    monkeypatch.delenv("SARAMIN_ACCESS_KEY", raising=False)
    monkeypatch.setattr(sr, "now_kst", lambda: REF)

    class Paging(FakeFetcher):
        def get(self, url, **kw):
            self.urls.append(url)
            n = len(self.urls)
            return self.html.replace('<div class="item_recruit" value="55', f'<div class="item_recruit" value="{n}55')

    f = Paging(list_html)
    res = sr.list_postings(f, {"keywords": ["전기"], "career": ["신입"], "max_pages": 2})
    assert len(f.urls) == 2 and res["partial"] is True and res["stop_reason"] == "page-cap"

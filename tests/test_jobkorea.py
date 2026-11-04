import os
from datetime import datetime

import pytest

from conftest import FIXTURES
from jobs_common import KST
from sources import jobkorea as jk

REF = datetime(2026, 11, 3, 10, 0, tzinfo=KST)


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def list_html():
    return _read("jobkorea_list.html")


def test_parse_list_reads_rsc_job_list(list_html):
    r = jk.parse_list(list_html)
    assert r["total"] == 2599 and r["total_pages"] == 130 and r["page"] == 0
    ids = [it["id"] for it in r["items"]]
    assert ids[:2] == ["50248250", "50228443"] and len(ids) == 6
    first = r["items"][0]
    assert first["company"] == "한국남부발전"
    assert first["career"] == "신입"
    assert first["employment_types"] == ["인턴"]
    assert "부산 남구" in first["locations"]          # CODES.area 코드 → 이름
    assert first["end"].startswith("2026-11-11T11:00")


def test_career_range_and_mixed_career(list_html):
    items = {it["id"]: it for it in jk.parse_list(list_html)["items"]}
    assert items["50181032"]["career"] == "신입·경력"
    assert items["50204470"]["career"] == "신입·경력3년↑"


def test_to_record_schema(list_html):
    it = jk.parse_list(list_html)["items"][0]
    rec = jk.to_record(it, ref=REF)
    assert rec["source"] == "jobkorea" and rec["source_id"] == "50248250"
    assert rec["url"] == "https://www.jobkorea.co.kr/Recruit/GI_Read/50248250"
    assert rec["deadline"] == "2026-11-11T11:00+09:00"
    assert rec["deadline_kind"] == "date" and rec["deadline_precision"] == "minute"
    assert rec["career"] == ["신입"]
    assert rec["edu"] is None  # 학력 코드표 미확인 → 추정하지 않는다
    assert rec["extra"]["education_code"] == "0"


def test_parse_list_structure_change_raises():
    with pytest.raises(RuntimeError):
        jk.parse_list("<html><body>nothing</body></html>")


def test_career_param_and_url():
    assert jk.career_param(["신입", "인턴"]) == "1,4"
    assert jk.career_param(["신입", "경력"]) is None
    assert jk.career_param([]) is None
    u = jk.search_url("전기", "1,4", 2)
    assert "careerType=1%2C4" in u and "Page_No=2" in u and "stext=%EC%A0%84%EA%B8%B0" in u
    assert "Page_No" not in jk.search_url("전기", None, 1)


def test_parse_detail_text_and_sections():
    d = jk.parse_detail(_read("jobkorea_detail.html"), _read("jobkorea_detail_ifrm.html"))
    assert d["text_incomplete"] is False
    assert "SFA반도체" in d["text"] and len(d["text"]) > 1000
    assert d["employer_url"] == "http://recruit.sfa.co.kr/sfasemicon"
    assert d["deadline_raw"].startswith("2026-11-08T23:59")
    assert d["edu"].startswith("고졸이상")
    s = d["sections"]
    assert "학력: 고졸이상" in s["자격요건"] and "4년제 대학 졸업" in s["자격요건"]
    assert "기본우대" in s["우대사항"]
    assert s["근무지"] == "충남 천안시 서북구 백석동 555-9"
    assert s["접수기간"].startswith("2026-10-30")
    # 모집분야가 '홈페이지 지원'(접수 방법)뿐이면 직무로 만들지 않는다
    assert d["openings"] == []


def test_parse_detail_image_only_is_incomplete():
    d = jk.parse_detail(_read("jobkorea_detail.html"), _read("jobkorea_detail_ifrm_img.html"))
    assert d["text_incomplete"] is True and d["images"] >= 1 and d["text"] == ""


def test_parse_detail_without_iframe_is_incomplete():
    d = jk.parse_detail(_read("jobkorea_detail.html"), None)
    assert d["text_incomplete"] is True
    assert "자격요건" in d["sections"]  # 요약은 본 페이지에서 나온다


class FakeFetcher:
    def __init__(self, html):
        self.html = html
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.html


def test_list_postings_dedupe_and_stop(monkeypatch, list_html):
    monkeypatch.setattr(jk, "now_kst", lambda: REF)
    f = FakeFetcher(list_html)
    res = jk.list_postings(f, {"keywords": ["전기", "설비"], "career": ["신입"], "max_pages": 3})
    # 같은 픽스처가 매번 돌아오므로: 키워드1 p1 = 새 6건, p2 = 이 키워드에서 새 0건 → 중단.
    # 키워드2 p1 = 교차 키워드 중복이지만 '이 키워드에서는' 새 것 → p2까지 본 뒤 중단(키워드 간 중복을 종료 조건으로 쓰지 않는다).
    assert len(f.urls) == 4
    assert all("careerType=1%2C4" in u for u in f.urls)
    assert res["pages"] == 4
    assert res["duplicates"] == 18
    assert {r["source_id"] for r in res["records"]} <= {"50248250", "50228443", "50181032", "50204470",
                                                        "50254490", "50254822"}
    assert res["partial"] is False and res["stop_reason"] == "exhausted"
    assert "전기" not in res["scope"] and "설비" not in res["scope"]  # 검색어는 scope에 쓰지 않는다
    assert res["reported_total"] == 2599 * 2


def test_list_postings_page_cap(monkeypatch, list_html):
    monkeypatch.setattr(jk, "now_kst", lambda: REF)

    class Paging(FakeFetcher):
        def get(self, url, **kw):  # 페이지마다 id를 바꿔 '새 공고'가 계속 나오게
            self.urls.append(url)
            n = len(self.urls)
            return self.html.replace('\\"id\\":\\"50', f'\\"id\\":\\"{n}0')

    f = Paging(list_html)
    res = jk.list_postings(f, {"keywords": ["전기"], "career": ["신입"], "max_pages": 2})
    assert len(f.urls) == 2
    assert res["partial"] is True and res["stop_reason"] == "page-cap"


def test_list_postings_drops_expired(monkeypatch, list_html):
    late = datetime(2026, 11, 10, 0, 0, tzinfo=KST)
    monkeypatch.setattr(jk, "now_kst", lambda: late)
    res = jk.list_postings(FakeFetcher(list_html), {"keywords": ["전기"], "career": ["신입"], "max_pages": 1})
    ids = {r["source_id"] for r in res["records"]}
    assert "50248250" in ids          # 11/11 마감
    assert "50181032" not in ids      # 11/03 마감 — 지남

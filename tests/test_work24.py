import os
from datetime import datetime

from conftest import FIXTURES
from jobs_common import KST
from sources import work24

REF = datetime(2026, 11, 3, 12, 0, tzinfo=KST)


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def test_parse_list_search():
    p = work24.parse_list(_read("work24_list.html"))
    assert p["total"] == 5646 and len(p["rows"]) == 15
    r = p["rows"][0]
    assert r["auth_no"] == "K151232611030019" and r["info_type_cd"] == "VALIDATION"
    assert r["company"] == "(주)에스제이비엠씨"
    assert r["title"] == "건물 전기,기계,영선, 설비 시설관리원 모집"  # <strong> 강조 사이 공백 없음
    assert r["career"] == "경력무관" and r["edu"] == "학력무관"
    assert r["close_date"] == "2026-11-22" and r["wanted_yn"] == "N"
    assert r["provider"] == "고용24"
    assert any(x["wanted_yn"] == "Y" for x in p["rows"])


def test_row_to_record_deadlines():
    rows = work24.parse_list(_read("work24_list.html"))["rows"]
    rec = work24.row_to_record(rows[0], "search", ref=REF)
    assert rec["deadline"] == "2026-11-22T23:59+09:00" and rec["deadline_kind"] == "date"
    assert {"신입", "경력"} <= set(rec["career"])
    assert "infoTypeGroup=tb_workinfoworknet" in rec["url"]
    y = next(r for r in rows if r["wanted_yn"] == "Y")
    rec_y = work24.row_to_record(y, "search", ref=REF)
    assert rec_y["deadline_kind"] == "채용시" and rec_y["deadline"] is None
    assert rec_y["may_close_early"] is True and rec_y["extra"]["close_date"]


def test_parse_list_gongchae():
    p = work24.parse_list(_read("work24_list_gongchae.html"))
    assert p["total"] == 259 and len(p["rows"]) == 10
    r = p["rows"][0]
    assert r["info_type_cd"] == "OEW" and r["provider"] == "공채속보"
    assert r["company"] == "퍼스텍" and "공개채용" in r["title"]
    rec = work24.row_to_record(r, "gongchae", ref=REF)
    assert rec["company_size"] == "중견" and "코스피" in rec["tags"]
    assert rec["locations"] == ["대전"] and rec["extra"]["location_more"] is True
    assert set(rec["career"]) >= {"신입", "경력"}


def test_parse_detail_validation():
    d = work24.parse_detail(_read("work24_detail.html"))
    assert d["company"] == "주식회사 두잉이앤엠"
    assert d["steps"] == ["서류", "면접"]
    assert "직무내용" in d["sections"] and d["sections"]["접수 마감일"].startswith("채용시까지")
    for k in ("모집 인원", "경력", "학력", "자격 면허", "고용 형태", "근무 예정지", "외국어 능력"):
        assert k in d["fields"]
    assert "구인인증번호" in d["fields"]


def test_parse_detail_gongchae():
    d = work24.parse_detail(_read("work24_detail_gongchae.html"))
    assert d["employer_url"].startswith("https://foosung.careerlink.kr/")
    assert d["positions"] and d["positions"][0]["채용부문"].startswith("[신입]")
    assert "문의사항" not in d["sections"]
    assert d["steps"][0] == "서류전형"


class FakeFetcher:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        return self.fn(url, params or {})


def test_fetch_detail_validation_scrubs_contacts():
    f = FakeFetcher(lambda u, p: _read("work24_detail.html"))
    rec = {"source_id": "K180312611030026", "title": "t",
           "extra": {"info_type_cd": "VALIDATION", "info_type_group": "tb_workinfoworknet"}}
    out = work24.fetch_detail(f, rec)
    assert f.calls[0][1]["infoTypeGroup"] == "tb_workinfoworknet"
    assert out["openings"][0]["headcount"] == "1명"
    assert out["sections"]["경력"] == "관계없음" and out["sections"]["전형방법"] == "서류 → 면접"
    assert out["deadline_raw"].startswith("채용시까지")
    assert out["text_incomplete"] is False
    assert not work24._PHONE.search(out["text"]) and "@" not in out["text"]


def test_fetch_detail_gongchae():
    f = FakeFetcher(lambda u, p: _read("work24_detail_gongchae.html"))
    rec = {"source_id": "183553", "extra": {"info_type_cd": "OEW", "info_type_group": "tb_workinfoopen"}}
    out = work24.fetch_detail(f, rec)
    assert out["employer_url"] and out["deadline_raw"].startswith("2026.11.11")
    assert out["openings"][0]["role"].startswith("[신입]")
    assert out["openings"][0]["locations"] == ["경기 성남시 분당구", "경남 창원시", "대전"]
    assert out["text_incomplete"] is True  # "채용 홈페이지 참조" + 공채사이트


def test_scrub():
    assert work24.scrub("TEL : 031-627-4044 a.b@c.com 010-1234-5678") == \
        "TEL : [연락처 생략] [이메일 생략] [연락처 생략]"


def test_list_postings_both_modes(monkeypatch):
    monkeypatch.setattr(work24, "now_kst", lambda: REF)
    search, gong = _read("work24_list.html"), _read("work24_list_gongchae.html")

    def fn(url, p):
        return gong if p.get("siteClcd") == "OEW" else search
    f = FakeFetcher(fn)
    res = work24.list_postings(f, {"keywords": ["전기", "설비"], "career": ["신입", "인턴"], "max_pages": 2})
    first = f.calls[0][1]
    assert first["srcKeyword"] == "전기" and first["keywordWantedTitle"] == "Y"
    assert first["careerTypes"] == "N,Z" and first["resultCnt"] == "50" and first["siteClcd"] == "WORK"
    # 검색 목록은 15행(<50) → 키워드당 1페이지, 두 번째 키워드는 전부 중복
    assert res["duplicates"] == 15
    assert len(res["records"]) == 25 and res["pages"] == 3
    assert {r["extra"]["mode"] for r in res["records"]} == {"search", "gongchae"}
    assert "키워드 2개" in res["scope"] and "공채속보" in res["scope"]
    assert "전기" not in res["scope"]  # 검색어 자체는 쓰지 않는다


def test_list_postings_page_cap(monkeypatch):
    monkeypatch.setattr(work24, "now_kst", lambda: REF)
    monkeypatch.setattr(work24, "RESULT_CNT", 15)  # 15행 = 꽉 찬 페이지로 취급
    search = _read("work24_list.html")
    f = FakeFetcher(lambda u, p: search)
    res = work24.list_postings(f, {"keywords": ["전기"], "max_pages": 2, "work24_mode": "search"})
    assert res["partial"] is True and res["stop_reason"] == "page-cap"

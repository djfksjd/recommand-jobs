import json
import os
from datetime import datetime

from conftest import FIXTURES
from jobs_common import KST
from sources import remember as rm

REF = datetime(2026, 11, 3, 12, 0, tzinfo=KST)


def _list():
    with open(os.path.join(FIXTURES, "remember_list.json"), encoding="utf-8") as f:
        return json.load(f)


def _detail_html():
    with open(os.path.join(FIXTURES, "remember_detail.html"), encoding="utf-8") as f:
        return f.read()


def test_parse_search_structure():
    items, total, pages = rm.parse_search(_list())
    assert len(items) == 5 and total == 5 and pages == 1


def test_parse_search_rejects_unknown():
    import pytest
    with pytest.raises(RuntimeError):
        rm.parse_search({"foo": 1})


def test_career_text():
    assert rm.career_text(None, None) == "경력 무관"
    assert rm.career_text(3, None) == "경력 3년 이상"
    assert rm.career_text(3, 6) == "경력 3~6년"
    assert rm.career_text(None, 1) == "경력 1년 이하"
    assert rm.career_text(0, 5) == "신입~경력 5년"


def test_records_from_list():
    items, _, _ = rm.parse_search(_list())
    recs = {r["source_id"]: r for r in (rm.to_record(p, ref=REF) for p in items)}
    r = recs["337823"]
    assert r["company"] == "씨에스쉐어링(주)"
    assert r["url"] == "https://career.rememberapp.co.kr/job/posting/337823"
    assert r["career_raw"] == "경력 3년 이상" and r["career"] == ["경력"]
    assert r["locations"] == ["서울특별시 강서구"]
    assert r["deadline_kind"] == "채용시" and r["deadline"] is None and r["may_close_early"] is True
    assert r["edu"] == "학사 이상"
    assert "경영·전략·기획/사업개발(BD)" in r["extra"]["job_categories"]
    assert r["openings"][0]["role"] == r["title"]
    d = recs["267821"]
    assert d["deadline_kind"] == "date" and d["deadline"] == "2026-11-30T23:59+09:00"
    assert d["career_raw"] == "경력 7~10년"
    s = recs["320586"]
    assert "신입" in s["career"]
    # 담당자 이름 등 개인정보는 레코드에 없다
    assert "recruiter" not in json.dumps(list(recs.values()), ensure_ascii=False)


def test_career_filters():
    assert rm._career_filters({"career": ["신입", "인턴"]}) == [{"career_year": -1}, {"career_year": 0}]
    assert rm._career_filters({"career": ["신입", "경력"]}) is None
    assert rm._career_filters({}) is None


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def post_json(self, url, body, **kw):
        self.calls.append(body)
        assert "seed" not in body
        return self.pages[body["page"] - 1]


def test_list_postings_full_mode_dedup_and_cap():
    data = _list()
    page = {"data": data["data"], "meta": {"total_count": 300, "total_pages": 10}}
    f = FakeFetcher([page] * 10)
    res = rm.list_postings(f, {"career": ["신입"], "full_max_pages": 2})
    assert res["stop_reason"] == "page-cap" and res["partial"] is True
    assert all(c["search"]["career_year"] in (-1, 0) for c in f.calls)
    assert len(f.calls) == 4  # 필터 2개 × 2p
    ids = [r["source_id"] for r in res["records"]]
    assert len(ids) == len(set(ids))
    assert res["duplicates"] == 5 * 3


def test_list_postings_query_mode():
    data = _list()
    f = FakeFetcher([data])
    res = rm.list_postings(f, {"career": ["경력"], "keywords": ["마케팅"], "max_pages": 3})
    assert f.calls[0]["search"]["keywords"] == ["마케팅"]
    assert res["stop_reason"] == "keywords-exhausted" and not res["partial"]
    assert "마케팅" not in res["scope"]


def test_detail_from_ssr():
    p = rm.posting_from_next_data(_detail_html(), "337823")
    assert p and p["id"] == 337823 and "qualifications" in p  # camel → snake
    d = rm.detail_from_posting(p)
    assert d["sections"]["자격요건"].startswith("B2B 제휴")
    assert "우대사항" in d["sections"] and "주요업무" in d["sections"]
    assert d["text_incomplete"] is False
    assert d["deadline_kind"] == "채용시"
    assert "[자격요건]" in d["text"]


def test_detail_missing_returns_none():
    assert rm.posting_from_next_data("<html></html>") is None
    assert rm.posting_from_next_data(_detail_html(), "999") is None


def test_list_postings_blocked_midway_keeps_partial():
    import pytest
    from jobs_common import Blocked
    data = _list()
    page = {"data": data["data"], "meta": {"total_count": 300, "total_pages": 10}}

    class F(FakeFetcher):
        def post_json(self, url, body, **kw):
            self.calls.append(body)
            if len(self.calls) >= 2:
                raise Blocked("HTTP 429")
            return page

    res = rm.list_postings(F([]), {"career": ["신입"]})
    assert res["partial"] and res["stop_reason"] == "blocked-midway" and res["records"]
    with pytest.raises(Blocked):
        class G(FakeFetcher):
            def post_json(self, url, body, **kw):
                raise Blocked("HTTP 429")
        rm.list_postings(G([]), {"career": ["신입"]})

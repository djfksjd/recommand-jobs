import json
import os
from datetime import datetime

import pytest

from conftest import FIXTURES
from jobs_common import KST
from sources import linkareer as lk

REF = datetime(2026, 11, 3, 12, 0, tzinfo=KST)


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _nodes():
    return lk.parse_graphql(json.loads(_read("linkareer_list.json")))


def test_parse_graphql_structure():
    nodes, total = _nodes()
    assert len(nodes) == 25 and total == 1704
    assert all(n["id"] and n["title"] for n in nodes)


def test_parse_graphql_errors():
    with pytest.raises(RuntimeError):
        lk.parse_graphql({"errors": [{"message": "bad"}]})
    with pytest.raises(RuntimeError):
        lk.parse_graphql({"data": {}})


def test_asap_is_chaeyongsi_not_placeholder_date():
    nodes, _ = _nodes()
    n = next(x for x in nodes if x["id"] == "361659")
    rec = lk.node_to_record(n, REF)
    assert rec["deadline_kind"] == "채용시" and rec["deadline"] is None
    assert rec["may_close_early"] is True
    assert rec["extra"]["placeholder_close_at"].startswith("2027-02-01")
    assert rec["company"] == "제이시스메디칼"
    assert rec["url"] == "https://linkareer.com/activity/361659"
    assert rec["career"] == ["신입", "경력"]
    assert rec["locations"] == ["서울 금천구"]
    assert rec["company_size"] == "중견"
    assert rec["openings"][0]["role"] == "생산/공정관리"


def test_normal_deadline_and_intern_openings():
    nodes, _ = _nodes()
    recs = [lk.node_to_record(n, REF) for n in nodes]
    r = next(x for x in recs if x and "한국특허정보원" in x["company"])
    assert r["deadline_kind"] == "date" and r["deadline"] == "2026-11-18T23:59+09:00"
    assert r["career"] == ["인턴"]
    assert {o["employment_type"] for o in r["openings"]} == {"체험형 인턴"}
    assert r["locations"] == ["서울 강남구", "대전 서구"]


def test_expired_dropped():
    nodes, _ = _nodes()
    late = datetime(2030, 1, 1, tzinfo=KST)
    normal = [n for n in nodes if n["recruitType"] == "NORMAL"]
    assert normal and all(lk.node_to_record(n, late) is None for n in normal)


def test_ssr_list_fallback_parser():
    nodes, total = lk.parse_list_html(_read("linkareer_list.html"))
    assert total == 2843 and len(nodes) == 20
    rec = lk.node_to_record(nodes[0], REF)
    assert rec["company"] == "제이시스메디칼" and rec["locations"] == ["서울 금천구"]
    assert rec["openings"][0]["opening_id"] == "215678"
    assert "생산/공정관리" in rec["tags"]


def test_detail():
    d = lk.parse_detail_html(_read("linkareer_detail.html"), "361616")
    assert "Pellicle 제조 공정" in d["text"]
    assert d["text_incomplete"] is False
    assert d["employer_url"].startswith("https://fstcareers.ninehire.site/")
    assert d["deadline_kind"] == "채용시"
    assert "신입 지원 가능" in d["sections"]["자격요건"]
    assert d["sections"]["학력"] == "대학졸업(4년) 이상"
    assert {c for o in d["openings"] for c in o["career"]} == {"신입", "경력"}
    assert "<" not in d["text"]


def test_detail_missing_activity_is_blocked():
    from jobs_common import Blocked
    with pytest.raises(Blocked):
        lk.parse_detail_html(_read("linkareer_detail.html"), "999")


def test_job_type_filter_from_plan():
    assert lk._job_types({"career": ["신입", "인턴"]}) == ["INTERN", "NEW"]
    assert lk._job_types({}) is None


class _FakeF:
    def __init__(self, gql=None, html=None):
        self.gql, self.html, self.calls = gql, html, []

    def post_json(self, url, body, **kw):
        self.calls.append(("POST", url, body["variables"]))
        if isinstance(self.gql, Exception):
            raise self.gql
        return self.gql

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw.get("params")))
        return self.html


def test_list_postings_graphql(monkeypatch):
    monkeypatch.setattr(lk, "now_kst", lambda: REF)
    f = _FakeF(gql=json.loads(_read("linkareer_list.json")))
    res = lk.list_postings(f, {"career": ["신입", "인턴"]})
    assert len(f.calls) == 1  # 25건 < pageSize → 끝
    assert f.calls[0][2]["filterBy"]["jobTypes"] == ["INTERN", "NEW"]
    assert res["stop_reason"] == "end" and not res["partial"] and len(res["records"]) == 25
    assert "전기" not in res["scope"]


def test_list_postings_falls_back_to_ssr(monkeypatch):
    monkeypatch.setattr(lk, "now_kst", lambda: REF)
    monkeypatch.setattr(lk, "SSR_PAGE_CAP", 2)
    f = _FakeF(gql=RuntimeError("boom"), html=_read("linkareer_list.html"))
    res = lk.list_postings(f, {"career": ["신입"]})
    assert [c[0] for c in f.calls] == ["POST", "GET", "GET"]
    assert res["partial"] and res["stop_reason"] == "page-cap" and res["errors"]
    assert res["duplicates"] == 20 and len(res["records"]) == 20

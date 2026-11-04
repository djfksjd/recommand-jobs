import os
from datetime import datetime

from conftest import FIXTURES
from jobs_common import KST
from sources import alio

REF = datetime(2026, 11, 3, 12, 0, tzinfo=KST)


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def test_parse_list_rows_and_total():
    p = alio.parse_list(_read("alio_list.html"))
    assert len(p["rows"]) == 50
    assert p["total"] == 509 and p["last_page"] == 11
    r = p["rows"][0]
    assert r["idx"] == "306640"
    assert r["org"] == "학교법인한국폴리텍"
    assert r["employment_type"] == "무기계약직"
    assert r["deadline_raw"] == "26.11.10" and r["status"] == "진행중"
    assert all(x["idx"].isdigit() and x["title"] and x["org"] for x in p["rows"])
    assert any(x["employment_type"].startswith("청년인턴") for x in p["rows"])


def test_location_more_split():
    p = alio.parse_list(_read("alio_list.html"))
    multi = [r for r in p["rows"] if r["location_more"]]
    assert multi and all(" 외 " not in r["location"] for r in multi)


def test_row_to_record():
    row = alio.parse_list(_read("alio_list.html"))["rows"][0]
    rec = alio.row_to_record(row, alio.CAREER_FILTER_SINNIP, ref=REF)
    assert rec["source"] == "alio" and rec["source_id"] == "306640"
    assert rec["url"].endswith("/recruitview.do?idx=306640")
    assert rec["deadline"] == "2026-11-10T23:59+09:00" and rec["deadline_precision"] == "day"
    assert rec["career"] == ["신입"] and rec["company_type"] == "공공기관"
    assert rec["posted_at"] == "2026-11-03" and rec["locations"] == ["충북"]
    rec2 = alio.row_to_record(row, None, ref=REF)
    assert rec2["career"] == []  # 필터 없이 모으면 목록에선 경력 구분을 모른다(추정 금지)


def test_intern_tagged():
    rows = alio.parse_list(_read("alio_list.html"))["rows"]
    intern = next(r for r in rows if "인턴" in r["employment_type"])
    rec = alio.row_to_record(intern, None, ref=REF)
    assert "청년인턴" in rec["employment_type"] and rec["tags"]


def test_parse_detail():
    d = alio.parse_detail(_read("alio_detail.html"))
    assert d["org"] == "서울대학교병원" and d["title"] == "진료교수요원 공개채용"
    assert d["fields"]["채용구분"] == "신입+경력"
    assert d["fields"]["근무지"] == "서울,충북"
    assert d["fields"]["채용기간"] == "26.11.03 ~ 26.11.10"
    assert "응시자격" in d["sections"] and "전형절차/방법" in d["sections"]
    assert d["roles"][0] == "신장내과" and len(d["roles"]) >= 5
    kinds = {a["kind"] for a in d["attachments"]}
    assert {"공고문", "입사지원서"} <= kinds
    assert all(a["url"].startswith("https://www.alio.go.kr/download/") for a in d["attachments"])
    assert d["employer_url"].startswith("https://www.snuh.org/")


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, **kw):
        self.calls.append(dict(params or {}))
        return self.pages(int(params.get("pageNo", 1)))


def test_fetch_detail_marks_incomplete():
    f = FakeFetcher(lambda p: _read("alio_detail.html"))
    out = alio.fetch_detail(f, {"source_id": "306372", "title": "x"})
    assert out["text_incomplete"] is True  # 요약 + "첨부 공고문 참조"
    assert {"신입", "경력"} <= set(out["openings"][0]["career"])
    assert out["openings"][0]["locations"] == ["서울", "충북"]
    assert out["attachments"] and "응시자격" in out["text"]


def test_list_postings_paging_and_filter(monkeypatch):
    monkeypatch.setattr(alio, "_load_api_key", lambda: None)
    monkeypatch.setattr(alio, "now_kst", lambda: REF)
    page1 = _read("alio_list.html")
    f = FakeFetcher(lambda p: page1 if p == 1 else "<table class=\"tbl type_03\"><tbody></tbody></table>")
    res = alio.list_postings(f, {"career": ["신입", "인턴"]})
    assert f.calls[0]["career"] == "R2010" and f.calls[0]["ing"] == "2"
    assert f.calls[0]["s_date"] == alio.S_DATE_FLOOR and f.calls[0]["pageSet"] == "50"
    assert len(res["records"]) == 50 and res["pages"] == 2
    assert res["reported_total"] == 509
    assert res["errors"]  # 509 중 50만 받았는데 끝났다 → 조용히 전수로 보고하지 않는다
    f2 = FakeFetcher(lambda p: page1)
    res2 = alio.list_postings(f2, {"career": ["신입", "경력"]})
    assert "career" not in f2.calls[0]
    assert res2["pages"] == 11  # last_page=11에서 멈춤, 같은 idx는 중복 제거
    assert len(res2["records"]) == 50


def test_api_fallback_to_html(monkeypatch):
    monkeypatch.setattr(alio, "_load_api_key", lambda: "SECRETKEY123")
    monkeypatch.setattr(alio, "now_kst", lambda: REF)

    def boom(*a, **k):
        raise RuntimeError("SERVICE_KEY_IS_NOT_REGISTERED_ERROR serviceKey=SECRETKEY123")
    monkeypatch.setattr(alio, "_list_via_api", boom)
    logs = []
    monkeypatch.setattr(alio, "log", lambda *a: logs.append(" ".join(map(str, a))))
    page1 = _read("alio_list.html")
    f = FakeFetcher(lambda p: page1)
    res = alio.list_postings(f, {})
    assert len(res["records"]) == 50
    assert logs and "SECRETKEY123" not in logs[0] and "***" in logs[0]


def test_api_item_to_record():
    rec = alio.api_item_to_record({"recrutPblntSn": "306640", "instNm": "기관", "recrutPbancTtl": "공고",
                                   "pbancBgngYmd": "20261103", "pbancEndYmd": "20261110",
                                   "recrutSeNm": "신입", "workRgnNmLst": "서울,충북"}, ref=REF)
    assert rec["deadline"] == "2026-11-10T23:59+09:00" and rec["locations"] == ["서울", "충북"]
    assert rec["posted_at"] == "2026-11-03"

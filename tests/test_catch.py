import json
import os
from datetime import datetime

import pytest

from conftest import FIXTURES
from jobs_common import KST, Blocked
from sources import catch as ct

REF = datetime(2026, 11, 3, 12, 0, tzinfo=KST)


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _items():
    return ct.parse_list(json.loads(_read("catch_list.json")))


def test_parse_list_structure():
    items, total = _items()
    assert len(items) == 40 and total == 1259
    with pytest.raises(RuntimeError):
        ct.parse_list({"foo": []})


def test_item_records():
    items, _ = _items()
    recs = {r["source_id"]: r for r in (ct.item_to_record(i, REF) for i in items) if r}
    assert len(recs) == 40
    r = recs["579172"]  # 상시채용 — ApplyEndDatetime은 자리값
    assert r["deadline_kind"] == "상시" and r["deadline"] is None
    assert r["extra"]["placeholder_end"].startswith("2027-01-02")
    assert r["url"] == "https://www.catch.co.kr/NCS/RecruitInfoDetails/579172"
    r = recs["579169"]  # UTC → KST
    assert r["deadline"] == "2026-11-22T23:59+09:00" and r["deadline_kind"] == "date"
    assert r["company"] == "라라스윗" and "인턴" in r["career"] and "무관" in r["career"]
    assert r["locations"] == ["서울"]
    r = recs["579165"]
    assert r["deadline"] == "2026-11-08T23:00+09:00"
    assert r["locations"] == ["경남", "전남광주", "충남"]
    assert r["edu"] == "대학교 졸업(4년제)"
    kinds = {x["deadline_kind"] for x in recs.values()}
    assert "채용시" in kinds


def test_expired_dropped():
    items, _ = _items()
    late = datetime(2030, 1, 1, tzinfo=KST)
    dated = [i for i in items if not i["ApplyEndCode"]]
    assert dated and all(ct.item_to_record(i, late) is None for i in dated)


def test_career_param_always_includes_mugwan():
    assert ct._career_param({"career": ["신입", "인턴"]}) == "0,1,4"
    assert ct._career_param({}) == ""


def test_parse_nuxt_iife():
    html = ('<script>window.__NUXT__=(function(a,b,c,d){c.x=a;c["y z"]=[b,void 0];d[1]=c;'
            'return {k:c,s:"\\u002Fp\\n",n:-1.5,arr:Array(2),d:d,m:new Map([["q",1]]),t:true}}'
            '(1,"two",{},Array(1)));</script>')
    v = ct.parse_nuxt(html)
    assert v["k"] == {"x": 1, "y z": ["two", None]}
    assert v["s"] == "/p\n" and v["n"] == -1.5 and v["arr"] == [None, None]
    assert v["d"] == [None, {"x": 1, "y z": ["two", None]}]
    assert v["m"] == {"q": 1} and v["t"] is True


def test_parse_nuxt_real_page():
    v = ct.parse_nuxt(_read("catch_detail.html"))
    assert v["routePath"] == "/NCS/RecruitInfoDetails/579169"


def test_detail():
    d = ct.parse_detail_html(_read("catch_detail.html"), "579169")
    assert "마케팅 인턴" in d["text"] and len(d["text"]) > 1000
    assert d["text_incomplete"] is False
    assert d["employer_url"] == "https://career.lalasweet.kr/ko/o/242997"
    assert d["sections"]["학력"] == "학력무관" and d["sections"]["경력"] == "경력무관"
    assert d["openings"][0]["role"].startswith("마케팅전략/기획")
    assert "<" not in d["text"] and "{" not in d["text"][:200]


def test_detail_image_only_incomplete():
    d = ct.parse_detail_html(_read("catch_detail_image.html"), "579172")
    assert d["text_incomplete"] is True and d["images"] >= 1
    assert d["employer_url"].startswith("https://recruit.nits-corp.com/")


def test_detail_id_mismatch_blocked():
    with pytest.raises(Blocked):
        ct.parse_detail_html(_read("catch_detail.html"), "1")


def test_list_postings_paging(monkeypatch):
    monkeypatch.setattr(ct, "now_kst", lambda: REF)
    data = json.loads(_read("catch_list.json"))

    class F:
        calls = []

        def get_json(self, url, params=None, **kw):
            self.calls.append(params)
            return data

    monkeypatch.setattr(ct, "PAGE_SIZE", 40)
    f = F()
    res = ct.list_postings(f, {"career": ["신입"], "page_cap": 3})
    assert len(f.calls) == 3 and f.calls[0]["Career"] == "0,1" and f.calls[2]["curpage"] == 3
    assert res["partial"] and res["stop_reason"] == "page-cap"
    assert len(res["records"]) == 40 and res["duplicates"] == 80

"""공통 모듈·규칙 엔진·diff 불변조건 테스트 (네트워크 없음)."""
import json
import os
from datetime import datetime

import pytest

import jobs_common as j
import score
import requirements as req
import diff_surveys as dif

REF = datetime(2026, 11, 3, 12, 0, tzinfo=j.KST)

PROFILE = {
    "profile_version": 1,
    "target_roles": [{"name": "전기", "keywords": ["전기", "계장"]}],
    "adjacent_roles": [{"name": "설비", "keywords": ["설비", "공무", "시설"]}],
    "avoid_keywords": ["영업"],
    "career": ["신입", "인턴"],
    "employment_types_ok": ["정규직", "계약직", "인턴"],
    "graduation": "2027-02", "available_from": "2027-01", "education": "학사",
    "licenses": [{"name": "전기기사"}],
    "languages": [{"test": "TOEIC", "score": 625, "date": "2026-09-20"}],
    "speaking": "none", "shift_work_ok": True,
    "company_types_prefer": ["대기업", "공공기관"],
}


def rec(**kw):
    base = dict(source="t", source_id=kw.pop("sid", "1"), url="https://x", company=kw.pop("company", "(주)테스트"),
                title=kw.pop("title", "신입 채용"), ref=REF)
    base.update(kw)
    return j.make_record(**base)


# ── robots ──
def test_robots_strict_both_groups():
    rb = j.Robots("User-agent: *\nDisallow: /a\n\nUser-agent: Claude-User\nDisallow: /b\n")
    assert not rb.allowed("https://h/a/x") and not rb.allowed("https://h/b/x") and rb.allowed("https://h/c")


def test_robots_wildcard_and_longest_match():
    rb = j.Robots("User-agent: *\nDisallow: /*?*seed=\nDisallow: /job\nAllow: /job/\n")
    assert not rb.allowed("https://h/x?a=1&seed=3")
    assert rb.allowed("https://h/job/1") and not rb.allowed("https://h/jobs")


def test_robots_ignores_training_bot_groups():
    rb = j.Robots("User-agent: ClaudeBot\nDisallow: /\n\nUser-agent: *\nDisallow: /private\n")
    assert rb.allowed("https://h/list") and not rb.allowed("https://h/private")


# ── 마감 ──
@pytest.mark.parametrize("raw,kind,prec", [
    ("~11.15(토)", "date", "day"), ("11/15 23:59", "date", "minute"), ("상시채용", "상시", None),
    ("채용시 마감", "채용시", None), ("", "unknown", None)])
def test_parse_deadline(raw, kind, prec):
    _, k, p = j.parse_deadline(raw, REF)
    assert (k, p) == (kind, prec)


def test_deadline_year_rollover():
    dl, _, _ = j.parse_deadline("1/5", datetime(2026, 12, 20, tzinfo=j.KST))
    assert dl.startswith("2027-01-05")


def test_scrub_hides_query():
    assert "SECRET" not in j.scrub("err https://a.b/c?key=SECRET&q=전기")


# ── 규칙 엔진 불변조건 ──
def test_false_friend_not_direct():
    s = score.screen(rec(title="운전기사 모집", career="신입"), PROFILE, [], REF)
    assert s["fit"] != "direct"


def test_newcomer_title_overrides_experienced_label():
    s = score.screen(rec(title="설비 신입/경력 채용", career=["경력"]), PROFILE, [], REF)
    assert s["gate"] == "ok"


def test_experienced_only_gated():
    s = score.screen(rec(title="전기 설계 과장급", career=["경력"]), PROFILE, [], REF)
    assert s["gate"] == "experienced_only"


def test_expired_gated():
    s = score.screen(rec(title="전기 신입", deadline_raw="2026-11-01T18:00"), PROFILE, [], REF)
    assert s["gate"] == "expired"


def test_applied_same_opening_vs_company():
    applied = [{"company_norm": j.norm_company("테스트"), "role": "전기", "status": "제출"}]
    a = score.screen(rec(title="전기 신입"), PROFILE, applied, REF)
    b = score.screen(rec(title="영업관리 신입"), PROFILE, applied, REF)
    assert a["applied"] == "same_opening" and not a["candidate"]
    assert b["applied"] == "same_company"


def test_tag_license_weaker_than_title():
    t = score.screen(rec(title="전기기사 모집"), PROFILE, [], REF)["score"]
    g = score.screen(rec(title="전기 모집", tags=["전기기사"]), PROFILE, [], REF)["score"]
    assert t > g


def test_category_role_treated_as_tag():
    r = rec(title="품질관리 담당", tags=["전기/전자/제어"],
            openings=[j.make_opening(role="전기/전자/제어")])
    assert score.screen(r, PROFILE, [], REF)["fit"] != "direct"


def test_generic_requires_no_specific_role():
    assert score.screen(rec(title="iOS 개발 인턴 (체험형)"), PROFILE, [], REF)["fit"] == "weak"
    assert score.screen(rec(title="2026 하반기 신입사원 공개채용"), PROFILE, [], REF)["fit"] == "generic"


def test_profile_fingerprint_changes_on_axis_only():
    a = score.profile_fingerprint(PROFILE)
    b = score.profile_fingerprint({**PROFILE, "avoid_keywords": ["x"]})
    c = score.profile_fingerprint({**PROFILE, "speaking": "OPIc IM2"})
    assert a != b and a != c  # avoid_keywords도 적합도 판정 축이다


# ── 요건 추출 ──
TEXT = """[지원자격]
- 2027년 2월 졸업예정자
- 영어 말하기 성적 필수: OPIc IM2 이상
[우대사항]
- 전기기사, TOEIC 900점 이상
근무형태: 4조 3교대"""


def test_requirements_need_and_status():
    items = req.check(req.extract(TEXT), PROFILE)
    by = {(i["type"], str(i["value"])): i for i in items}
    assert by[("speaking", "OPIcIM2")]["need"] == "필수" and by[("speaking", "OPIcIM2")]["status"] == "unmet"
    assert by[("license", "전기기사")]["need"] == "우대"
    assert by[("toeic", "900")]["need"] == "우대"
    assert req.verdict(items, False) == ("remediable", "check_deadline")


def test_preferred_unmet_does_not_make_ineligible():
    items = req.check(req.extract("[우대사항]\n- 경력 3년 이상\n- 석사 학위 소지"), PROFILE)
    assert req.verdict(items, False)[0] != "ineligible"


def test_unverified_text_is_unknown():
    assert req.verdict([], True)[0] == "unknown"


# ── diff 불변조건 ──
def _survey(tmp, name, recs, status="ok", scope="s"):
    d = tmp / name
    d.mkdir()
    j.write_jsonl(d / "t.jsonl", recs)
    (d / "run_manifest.json").write_text(json.dumps({"manifest_schema_version": 1, "runs": [
        {"source": "t", "status": status, "exit_code": 0 if status == "ok" else 2, "scope": scope}]}))
    return str(d)


def test_deadline_extension_is_not_new(tmp_path):
    a = rec(title="전기 신입", deadline_raw="2026-11-20T18:00")
    b = dict(a, deadline="2026-11-30T18:00+09:00")
    old, new = _survey(tmp_path, "o", [a]), _survey(tmp_path, "n", [b])
    dif.main([old, new])
    kinds = [json.loads(l)["kind"] for l in open(os.path.join(new, "diff_items.jsonl"))]
    assert kinds == ["DEADLINE_EXTENDED"]


def test_partial_source_never_marks_missing(tmp_path):
    a = rec(sid="1", title="전기 신입", deadline_raw="2027-01-20T18:00")
    b = rec(sid="2", title="계장 신입", deadline_raw="2027-01-20T18:00")
    old, new = _survey(tmp_path, "o", [a, b]), _survey(tmp_path, "n", [a], status="partial")
    dif.main([old, new])
    assert open(os.path.join(new, "diff_missing.jsonl")).read() == ""


def test_scope_change_not_comparable(tmp_path):
    a = rec(sid="1", title="전기 신입", deadline_raw="2027-01-20T18:00")
    old, new = _survey(tmp_path, "o", [a]), _survey(tmp_path, "n", [], scope="other")
    dif.main([old, new])
    s = json.load(open(os.path.join(new, "diff_summary.json")))
    assert "t" in s["not_comparable_sources"] and s["counts"]["MISSING"] == 0

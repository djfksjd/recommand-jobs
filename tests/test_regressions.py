"""Codex 코드 리뷰(2026-11-03)·eval에서 나온 결함의 회귀 테스트."""
import json
import os
from datetime import date, datetime

import jobs_common as j
import score
import requirements as req
import diff_surveys as dif
from test_core import PROFILE, REF, rec

P = {"licenses": [{"name": "전기기사"}], "languages": [{"test": "TOEIC", "score": 625, "date": "2026-09-20"}],
     "speaking": "none", "career": ["신입"], "graduation": "2027-02", "education": "학사"}
TODAY = date(2026, 11, 3)


def v(text, n_open=1):
    return req.verdict(req.check(req.group_alternatives(req.extract(text), text), P, TODAY), False, n_open)[0]


# requirements
def test_or_license_alternative():
    assert v("[지원자격]\n- 전기산업기사 또는 전기기사 소지자") == "eligible_by_text"


def test_license_level_above():
    assert v("[지원자격]\n- 전기산업기사 이상 소지자") == "eligible_by_text"


def test_newcomer_alternative_to_career_years():
    assert v("[지원자격]\n- 신입 / 경력 3년 이상") == "eligible_by_text"


def test_other_opening_requirement_not_ineligible():
    assert v("[지원자격]\n- 경력 3년 이상", n_open=2) == "unknown"


def test_unknown_need_unmet_is_unknown():
    assert v("TOEIC 700점") == "unknown"


def test_language_validity_exact_date():
    assert not req._valid2y("2024-11-01", date(2026, 11, 30))
    assert req._valid2y("2024-11-01", date(2026, 10, 31))
    assert not req._valid2y(None, TODAY)


# jobs_common
def test_iso_timezone_converted():
    assert j.parse_deadline("2026-11-04T10:00:00Z")[0] == "2026-11-04T19:00+09:00"


def test_period_uses_end_date():
    assert j.parse_deadline("26.11.03 ~ 26.11.10", REF)[0].startswith("2026-11-10")


def test_robots_percent_encoding():
    assert not j.Robots("User-agent: *\nDisallow: /private\n").allowed("https://h/%70rivate")


def test_parenthesized_role_kept_in_title_norm():
    assert j.norm_title("신입 채용(전기)") != j.norm_title("신입 채용(기계)")
    assert j.norm_title("[채용시마감] 신입 채용") == j.norm_title("신입 채용")


# score
def test_mixed_employment_type_kept():
    prof = {**PROFILE, "employment_types_ok": ["정규직"]}
    assert score.screen(rec(title="전기 신입", employment_type="정규직, 계약직"), prof, [], REF)["gate"] == "ok"


def test_incomplete_location_not_excluded():
    prof = {**PROFILE, "locations": {"exclude": ["서울"]}}
    assert score.screen(rec(title="전기 신입", locations=["서울 외 1"]), prof, [], REF)["gate"] == "ok"


def test_applied_word_overlap_is_company_only():
    applied = [{"company_norm": j.norm_company("테스트"), "role": "전기 설계", "status": "제출"}]
    s = score.screen(rec(title="전기 설비보전 신입"), PROFILE, applied, REF)
    assert s["applied"] == "same_company" and s["candidate"]


def test_same_source_same_key_not_grouped():
    rs = [rec(sid="1", title="신입 채용"), rec(sid="2", title="신입 채용")]
    for r in rs:
        r["screen"] = {}
    score.group_duplicates(rs)
    assert all(r["screen"]["group"] is None for r in rs)


def test_load_source_records_skips_derived(tmp_path):
    j.write_jsonl(tmp_path / "t.jsonl", [rec(title="전기 신입")])
    (tmp_path / "requirements.jsonl").write_text(json.dumps({"id": "x"}) + "\n")
    assert len(score.load_source_records(str(tmp_path))) == 1


# diff
def _survey(tmp, name, recs, plan):
    d = tmp / name
    d.mkdir()
    j.write_jsonl(d / "t.jsonl", recs)
    (d / "search_plan.json").write_text(json.dumps(plan))
    (d / "run_manifest.json").write_text(json.dumps({"manifest_schema_version": 1, "runs": [
        {"source": "t", "status": "ok", "exit_code": 0, "scope": "x"}]}))
    return str(d)


def test_plan_change_not_comparable(tmp_path):
    a = rec(sid="1", title="전기 신입", deadline_raw="2027-01-20T18:00")
    old = _survey(tmp_path, "o", [a], {"keywords": ["전기"], "career": ["신입"]})
    new = _survey(tmp_path, "n", [], {"keywords": ["기계"], "career": ["신입"]})
    dif.main([old, new])
    s = json.load(open(os.path.join(new, "diff_summary.json")))
    assert s["counts"]["MISSING"] == 0 and "t" in s["not_comparable_sources"]


def test_same_plan_missing_detected(tmp_path):
    a = rec(sid="1", title="전기 신입", deadline_raw="2027-01-20T18:00")
    plan = {"keywords": ["전기"], "career": ["신입"]}
    old, new = _survey(tmp_path, "o", [a], plan), _survey(tmp_path, "n", [], plan)
    dif.main([old, new])
    assert json.load(open(os.path.join(new, "diff_summary.json")))["counts"]["MISSING"] == 1


def test_reposted_requires_same_comparable_source(tmp_path):
    a = rec(sid="1", title="전기 신입", deadline_raw="2027-01-20T18:00")
    b = rec(sid="2", title="전기 신입", deadline_raw="2027-02-20T18:00")
    plan = {"keywords": ["전기"], "career": ["신입"]}
    old, new = _survey(tmp_path, "o", [a], plan), _survey(tmp_path, "n", [b], plan)
    dif.main([old, new])
    kinds = [json.loads(l)["kind"] for l in open(os.path.join(new, "diff_items.jsonl"))]
    assert kinds == ["REPOSTED"]


class _Resp:
    def __init__(self, code, text="", headers=None):
        self.status_code, self.text, self.headers = code, text, headers or {}

    def json(self):
        return json.loads(self.text)


class _Sess:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.script.pop(0)

    def request(self, method, url, **kw):
        self.calls.append(url)
        return self.script.pop(0)


def _fetcher(script):
    f = j.Fetcher(("example.com",), delay=0)
    f.session = _Sess(script)
    return f


def test_robots_redirect_outside_allowlist_denies():
    f = _fetcher([_Resp(302, headers={"location": "https://evil.test/robots.txt"})])
    assert not f.allowed("https://example.com/list")
    assert f.session.calls == ["https://example.com/robots.txt"]  # 밖으로는 요청하지 않았다


def test_robots_429_denies():
    f = _fetcher([_Resp(429)])
    assert not f.allowed("https://example.com/list")


def test_sticky_block_stops_further_requests():
    import pytest
    f = _fetcher([_Resp(200, "User-agent: *\nDisallow:\n"), _Resp(403)])
    with pytest.raises(j.Blocked):
        f.get("https://example.com/a")
    with pytest.raises(j.Blocked):
        f.get("https://example.com/b")
    assert f.session.calls == ["https://example.com/robots.txt", "https://example.com/a"]


# eval-1 실행에서 나온 결함
def test_public_enterprise_generic_with_matching_category_shortlisted():
    recs = [rec(sid="1", company="서울교통공사", title="2026년 하반기 신입사원(채용형 인턴) 공개채용 #공채",
                tags=["전기/소방/통신", "시설/관리"])]
    score.infer_company_kind(recs)
    s = score.screen(recs[0], PROFILE, [], REF)
    assert recs[0]["company_size_inferred"] == "공공기관"
    assert s["fit"] == "generic" and s["score"] >= 70


def test_applied_scattered_tokens_flagged_not_excluded():
    applied = [{"company_norm": j.norm_company("제일건설"), "role": "기술직 기계/전기", "status": "작성완료"}]
    r = rec(company="제일건설(주)", title="제일건설(주) 기술직 상시채용 [건축/품질/토목/기계/전기/안전]")
    s = score.screen(r, PROFILE, applied, REF)
    assert s["applied"] == "possible_same_opening" and s["candidate"]  # 표시만 하고 빼지 않는다


def test_applied_adjacent_tokens_same_opening():
    applied = [{"company_norm": j.norm_company("제일건설"), "role": "기술직 기계/전기", "status": "작성완료"}]
    r = rec(company="제일건설(주)", title="제일건설(주) 기술직(기계/전기) 채용")
    assert score.screen(r, PROFILE, applied, REF)["applied"] == "same_opening"


def test_salary_and_edu_preferences():
    prof = {**PROFILE, "salary_prefer_min": 4500, "posting_edu_prefer": "대졸"}
    hi = score.screen(rec(title="전기 신입", salary_raw="연봉 4,800만원", edu="대학교 졸업(4년제)"), prof, [], REF)
    lo = score.screen(rec(title="전기 신입", salary_raw="연봉 3,000만원", edu="고졸↑"), prof, [], REF)
    na = score.screen(rec(title="전기 신입", edu="학력무관"), prof, [], REF)
    assert hi["score"] > na["score"] > lo["score"]
    assert lo["gate"] == "ok"  # 선호는 제외 사유가 아니다


def test_parse_salary_monthly_and_unknown():
    assert score.parse_salary("월 300만원") == (3600, 3600)
    assert score.parse_salary("회사내규에 따름") == (None, None)


def test_unicode_robots_path():
    assert not j.Robots("User-agent: *\nDisallow: /비공개\n").allowed("https://h/%EB%B9%84%EA%B3%B5%EA%B0%9C")
    assert j.Robots("User-agent: *\nDisallow: /a%2Fb\n").allowed("https://h/a/b")


def test_license_or_major_alternative_not_ineligible():
    t = "[지원자격]\n- 전기기사 또는 전기 관련학과 졸업자"
    assert vt(t, {**P, "licenses": []}) != "ineligible"


def test_multi_track_license_mixed_is_unknown():
    t = "[지원자격]\n- 전기직: 전기기사 소지자\n- 기계직: 일반기계기사 소지자"
    assert vt(t) != "ineligible"


def test_applied_on_hold_not_counted(tmp_path):
    p = tmp_path / "a.tsv"
    p.write_text("A사\t전기\t제출\nB사\t설비\t보류(스피킹)\n")
    assert [a["company_norm"] for a in score.load_applied(str(p))] == [j.norm_company("A사")]


def test_proxy_posting_not_given_agency_kind():
    recs = [rec(sid="1", company="중소벤처기업진흥공단", title="[중진공우수기업] (주)세우항공 생산기술팀 채용공고")]
    score.infer_company_kind(recs)
    assert recs[0]["company_size_inferred"] is None


def test_join_immediate_checked_against_available_from():
    prof = {**P, "available_from": "2027-01"}
    items = req.check(req.extract("[지원자격]\n- 최종 합격 후 즉시 입사 가능한 자"), prof, TODAY)
    # 합격·입사 일정을 모르면 단정하지 않는다(Codex 2차) — '확인 필요'
    assert items[0]["type"] == "join_immediate" and items[0]["status"] == "unknown"


def test_first_seen_for_noncomparable_source(tmp_path):
    a = rec(sid="1", title="전기 신입", deadline_raw="2027-01-20T18:00")
    b = rec(sid="2", title="계장 신입", deadline_raw="2027-01-20T18:00")
    old = _survey(tmp_path, "o", [a], {"keywords": ["전기"]})
    new = _survey(tmp_path, "n", [a, b], {"keywords": ["전기", "계장"]})  # 계획이 달라 비교 불가
    dif.main([old, new])
    kinds = {json.loads(l)["record"]["source_id"]: json.loads(l)["kind"] for l in open(os.path.join(new, "diff_items.jsonl"))}
    assert kinds["2"] == "FIRST_SEEN"


def test_score_diff_mode_limits_candidates(tmp_path):
    a = rec(sid="1", title="전기 신입", deadline_raw="2027-01-20T18:00")
    b = rec(sid="2", title="계장 신입", deadline_raw="2027-01-20T18:00")
    plan = {"keywords": ["전기"]}
    old, new = _survey(tmp_path, "o", [a], plan), _survey(tmp_path, "n", [a, b], plan)
    dif.main([old, new])
    prof = tmp_path / "p.md"
    import yaml
    prof.write_text("---\n" + yaml.safe_dump(PROFILE, allow_unicode=True) + "---\n")
    score.main([new, "--profile", str(prof), "--diff"])
    rows = [json.loads(l) for l in open(os.path.join(new, "screened.jsonl"))]
    cand = {r["source_id"]: r["screen"]["candidate"] for r in rows}
    assert cand == {"1": False, "2": True}


# Codex 재검토(2차)에서 나온 결함
def vt(text, prof=None, n_open=1):
    p = prof or P
    items = req.extract(text)
    return req.verdict(req.check(req.group_alternatives(items, text), p, TODAY, full_text=text), False, n_open)[0]


def test_or_across_line_break():
    assert vt("[지원자격]\n- 전기산업기사 또는\n전기기사 소지자") == "eligible_by_text"


def test_or_does_not_swallow_and_requirement():
    assert vt("[지원자격]\n- 전기기사 소지자 및 TOEIC 700점 또는 TEPS 300점 보유자") != "eligible_by_text"


def test_newcomer_forbidden_keeps_career_requirement():
    assert vt("[지원자격]\n- 신입 지원 불가, 경력 3년 이상 필수") == "ineligible"
    assert vt("[지원자격]\n- 학력 무관, 경력 3년 이상 필수") == "ineligible"


def test_driver_license_classes():
    big = "[지원자격]\n- 1종 대형 운전면허 소지자"
    assert vt(big, {**P, "driver_license": "1종보통"}) == "ineligible"
    assert vt("[지원자격]\n- 운전면허 소지자", {**P, "driver_license": "없음"}) == "ineligible"


def test_residence_sido_normalized_and_city_unknown():
    assert vt("[지원자격]\n- 전라북도 거주자", {**P, "residence": ["전북"]}) == "eligible_by_text"
    assert vt("[지원자격]\n- 광주시 거주자", {**P, "residence": ["광주광역시"]}) == "unknown"


def test_join_immediate_unknown_not_guessed():
    items = req.check(req.extract("[지원자격]\n- 합격 후 즉시 입사"), {**P, "available_from": "2026-12"}, TODAY)
    assert items[0]["status"] == "unknown"


def test_shift_unknown_need_not_eligible():
    assert vt("교대근무", {**P, "shift_work_ok": False}) == "unknown"


def test_public_name_requires_no_corp_marker():
    recs = [rec(sid="1", company="(주)미래교육개발원", title="강사 모집")]
    score.infer_company_kind(recs)
    assert recs[0]["company_size_inferred"] is None


def test_proxy_posting_drops_existing_agency_type():
    r = rec(company="중소벤처기업진흥공단", title="[중진공우수기업] (주)세우항공 생산기술팀 채용", company_type="공공기관")
    score.infer_company_kind([r])
    s = score.screen(r, PROFILE, [], REF)
    assert not any(x.startswith("선호 기업 유형") for x in s["reasons"])


def test_applied_tokens_ignore_tags():
    applied = [{"company_norm": j.norm_company("테스트"), "role": "전기 설계", "status": "제출"}]
    s = score.screen(rec(title="전기 설비보전 신입", tags=["설계"]), PROFILE, applied, REF)
    assert s["applied"] == "same_company"


def test_first_seen_timezone_aware(tmp_path):
    a = rec(sid="1", title="전기 신입", deadline_raw="2027-01-20T18:00")
    b = rec(sid="2", title="계장 신입", deadline_raw="2027-01-20T18:00", posted_at="2026-11-03T01:00:00Z")
    old = _survey(tmp_path, "o", [a], {"keywords": ["전기"]})
    m = json.load(open(os.path.join(old, "run_manifest.json")))
    m["generated_at"] = "2026-11-03T09:00:00+09:00"
    open(os.path.join(old, "run_manifest.json"), "w").write(json.dumps(m))
    new = _survey(tmp_path, "n", [a, b], {"keywords": ["전기", "계장"]})
    dif.main([old, new])
    kinds = {json.loads(l)["record"]["source_id"]: json.loads(l)["kind"] for l in open(os.path.join(new, "diff_items.jsonl"))}
    assert kinds["2"] == "NEW"  # 01:00Z = 10:00 KST > 09:00 KST


# 실제 공고 문장(2026-11-03 수집분)에서 나온 패턴
def test_real_comma_list_with_or_and_spaced_name():
    t = ("[지원자격]\n- 공인영어시험 기준점수 이상 보유자 - TOEFL(iBT) 69, TOEIC 600, New TEPS 228, G-TELP 50(level 2)\n"
         "- 국가기술자격법 시행규칙 별표2 중직무분야(전기) 전기기사, 전기산업기사 또는 전기 기능장 자격증 취득자")
    assert vt(t, {**P, "languages": [{"test": "TOEIC", "score": 625, "date": "2026-09-20"}]}) == "eligible_by_text"


def test_real_multi_track_career_not_ineligible():
    t = ("[지원자격]\n- (광주 4급) 전기기사 또는 전기기능장 자격 소지자로서 자격취득 후 해당분야 경력 2년 이상\n"
         "- (상주 5급) 전기산업기사 이상과 운전면허 2종 보통 이상 자격을 모두 소지한 자 (신입 가능)")
    assert vt(t, {**P, "driver_license": "1종보통"}) != "ineligible"


def test_higher_license_without_isang_is_unknown():
    items = req.check(req.extract("[지원자격]\n- 전기산업기사 소지자"), P, TODAY)
    assert items[0]["status"] == "unknown"


def test_graduation_expected_phrasing():
    t = "[지원자격]\n- 4년제 대학 기졸업자 또는 2027년 2월 졸업예정인 자"
    assert vt(t) == "eligible_by_text"


# --- 2026-11-04: 거주지 요건·공식 공고 확인 (지방공기업 전기 공고 누락 사례) ---

def test_residence_region_after_keyword():
    from requirements import extract
    t = ("[응시자격]\n공고일 기준 1년 이전부터 최종시험일까지 계속하여 "
         "주민등록상 주소지가 인천광역시로 되어 있는 사람\n")
    res = [i for i in extract(t) if i["type"] == "residence"]
    assert res and res[0]["value"] == "인천광역시"


def test_submission_document_is_not_residence_requirement():
    from requirements import extract
    t = "[제출서류]\n• 주민등록초본 1부\n• 통장사본 1부\n"
    assert not [i for i in extract(t) if i["type"] == "residence"]


def test_residence_unknown_without_profile_and_not_remediable():
    from requirements import check, extract
    t = "[지원자격]\n인천광역시에 주민등록이 되어 있는 자\n"
    items = check(extract(t), {"residence": []})
    r = [i for i in items if i["type"] == "residence"][0]
    assert r["status"] == "unknown" and r["remediable"] is False


def test_residence_unmet_when_profile_region_differs():
    from requirements import check, extract
    t = "[지원자격]\n인천광역시에 주민등록이 되어 있는 자\n"
    items = check(extract(t), {"residence": ["경기"]})
    assert [i for i in items if i["type"] == "residence"][0]["status"] == "unmet"


def _mk_official_survey(tmp_path, company, text, official=None):
    import json
    det = tmp_path / "details"
    det.mkdir()
    (tmp_path / "saramin.jsonl").write_text(json.dumps(
        {"source": "saramin", "source_id": "1", "company": company, "title": "전기"}, ensure_ascii=False) + "\n")
    (det / "saramin_1.json").write_text(json.dumps(
        {"source": "saramin", "source_id": "1", "url": "u", "text": text, "sections": {},
         "openings": [{"role": "전기"}], "attachments": []}, ensure_ascii=False))
    if official:
        (det / "saramin_1.official.txt").write_text(official)
    return tmp_path


def _run_official_req(tmp_path, prof_text):
    import json
    from requirements import main
    prof = tmp_path / "p.md"
    prof.write_text(prof_text)
    main([str(tmp_path), "--profile", str(prof)])
    return json.loads((tmp_path / "requirements.jsonl").read_text().splitlines()[0])


PROF_1104 = "---\nprofile_version: 1\nlanguages:\n  - {test: TOEIC, score: 650, date: 2026-09-20}\n---\n"


def test_public_summary_only_is_not_eligible(tmp_path):
    t = "[지원자격]\nTOEIC 600점 이상 필수\n" + "근무지: 인천광역시 전 지역\n" + "담당업무: 전기설비 운영 관리\n" * 10
    r = _run_official_req(_mk_official_survey(tmp_path, "예시환경공단", t), PROF_1104)
    assert r["eligibility"] == "unknown" and r["remediation"] == "official_unverified"
    assert r["official_status"] == "none"


def test_official_text_reveals_residence(tmp_path):
    t = "[지원자격]\nTOEIC 600점 이상 필수\n"
    off = ("[응시자격]\n" + "TOEIC 600점 이상\n" * 30 +
           "공고일 기준 1년 이전부터 계속하여 주민등록상 주소지가 인천광역시로 되어 있는 사람\n")
    r = _run_official_req(_mk_official_survey(tmp_path, "예시환경공단", t, off), PROF_1104)
    assert r["official_status"] == "fetched"
    assert any(i["type"] == "residence" for i in r["items"])
    assert r["eligibility"] != "eligible_by_text"


def test_site_original_confirmation_counts_as_official(tmp_path):
    import json
    from official import confirm_site_original
    t = "[지원자격]\nTOEIC 600점 이상 필수\n" + "담당업무: 전기설비 운영 관리\n" * 10
    root = _mk_official_survey(tmp_path, "(주)예시전기", t)
    confirm_site_original(str(root / "details"), "saramin:1", "회사 홈페이지에 채용 메뉴 없음, 사람인 공고가 원본")
    r = _run_official_req(root, PROF_1104)
    assert r["official_status"] == "fetched" and r["eligibility"] == "eligible_by_text"

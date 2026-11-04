#!/usr/bin/env python3
"""재조사(diff) — 직전 조사 폴더와 새 조사 폴더를 비교한다.

  python3 diff_surveys.py OLD_DIR NEW_DIR [--old-profile OLD/profile-snapshot.md --new-profile profile.md]

출력 (NEW_DIR 안)
  diff_items.jsonl : 다시 볼 것만 — {"kind", "changed_fields", "record", "old": {...}|None}
      NEW                 처음 보는 공고(비교 가능한 소스, 또는 게시일이 직전 조사 이후)
      FIRST_SEEN          비교 불가 소스에서 처음 본 것 — 신규인지 모름(검색 캡 밖에 있다가 들어온 것일 수 있다)
      REPOSTED            새 ID인데 같은 회사·제목(dup_key)의 직전 공고가 사라졌거나 마감됨 → 재공고 가능성
      UPDATED             제목·직무·경력·근무지 등 의미 필드 변경 (조회수·광고는 비교하지 않음)
      DEADLINE_EXTENDED   마감일만 늦춰짐 (같은 공고 — 새 공고로 세지 않는다)
      DEADLINE_SHORTENED  마감일이 당겨짐 (조기 마감 예고일 수 있음)
      DETAIL_CHANGED      목록은 같은데 상세 본문 해시가 바뀜
  diff_missing.jsonl : 목록에서 사라졌지만 종료 확인 안 된 것 (MISSING) — 직전 추천(A/B)이면 상세로 확인
  diff_summary.json  : 건수, 비교 불가 소스, 승계 무효 여부

승계 규칙
  * UNCHANGED는 직전 판정을 '후보'로만 승계한다. 마감 임박·성적 유효기간·채용시 마감은 시간이 지나면 바뀌므로
    보고서에 다시 올릴 추천 건은 상세를 재확인한다(SKILL.md 참고).
  * 프로필 판정 축 지문 또는 rules_version이 다르면 승계 무효 → 새 조사의 모든 레코드를 diff_items에 NEW로 넣는다.
  * 소스가 한쪽에서 ok가 아니거나 수집 범위(scope)가 다르면 그 소스는 '비교 불가' — MISSING을 만들지 않는다.
    (페이지 캡·검색어 변경·수집 실패로 사라진 것을 종료로 착각하지 않기 위해서다.)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from jobs_common import atomic_write, is_expired, now_kst, read_jsonl, record_id, write_jsonl  # noqa: E402

SEMANTIC_FIELDS = ("company", "title", "openings", "career", "employment_type", "locations", "deadline_kind")


def _load_dir(d):
    recs = {}
    for p in glob.glob(os.path.join(d, "*.jsonl")):
        b = os.path.basename(p)
        if b in ("screened.jsonl", "requirements.jsonl") or b.startswith("diff_"):
            continue
        for r in read_jsonl(p):
            recs[record_id(r)] = r
    man = {}
    mp = os.path.join(d, "run_manifest.json")
    if os.path.exists(mp):
        for run in json.load(open(mp, encoding="utf-8")).get("runs", []):
            man[run["source"]] = run
    return recs, man


PLAN_QUERY_KEYS = ("keywords", "career", "max_pages", "work24_mode", "page_cap", "full_max_pages")
PLAN_FULL_KEYS = ("career", "days_ahead", "page_cap", "full_max_pages", "work24_mode")


def _plan(d):
    p = os.path.join(d, "search_plan.json")
    if not os.path.exists(p):
        return None
    plan = json.load(open(p, encoding="utf-8"))
    norm = {}
    for k, v in plan.items():
        norm[k] = sorted(map(str, v)) if isinstance(v, list) else v
    return norm


def _kind(src):
    try:
        import importlib
        from sources import REGISTRY
        mod = REGISTRY.get(src, {}).get("module")
        return getattr(importlib.import_module(f"sources.{mod}"), "KIND", "query") if mod else "query"
    except Exception:
        return "query"


def _plan_keys(src):
    """어댑터가 PLAN_KEYS로 '결과에 영향을 주는 계획 키'를 밝히면 그것만, 아니면 전부 비교한다(보수적)."""
    try:
        import importlib
        from sources import REGISTRY
        mod = REGISTRY.get(src, {}).get("module")
        if mod:
            return tuple(getattr(importlib.import_module(f"sources.{mod}"), "PLAN_KEYS", PLAN_QUERY_KEYS + PLAN_FULL_KEYS))
    except Exception:
        pass
    return PLAN_QUERY_KEYS + PLAN_FULL_KEYS


def _plan_equal(a, b, src):
    """수집 조건이 같은가 — 표시용 scope 문자열이 아니라 저장된 검색 계획으로 판단한다."""
    if a is None or b is None:
        return False
    return all(a.get(k) == b.get(k) for k in _plan_keys(src))


def _dt(s):
    from datetime import datetime
    from jobs_common import KST
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=KST)


def _openings_sig(r):
    return sorted((o.get("role") or "", tuple(o.get("career") or []), tuple(o.get("locations") or []))
                  for o in r.get("openings") or [])


def compare(old, new):
    changed = []
    for f in SEMANTIC_FIELDS:
        a, b = old.get(f), new.get(f)
        if f == "openings":
            # 목록엔 직무명이 없고 상세에서 채워지는 소스가 있다 — 한쪽이 비어 있으면 비교하지 않는다
            sa, sb = _openings_sig(old), _openings_sig(new)
            if all(x[0] for x in sa) and all(x[0] for x in sb) and sa and sb and sa != sb:
                changed.append(f)
            continue
        if a != b:
            changed.append(f)
    dl_old, dl_new = old.get("deadline"), new.get("deadline")
    kind = None
    if dl_old != dl_new:
        if dl_old and dl_new:
            kind = "DEADLINE_EXTENDED" if dl_new > dl_old else "DEADLINE_SHORTENED"
        else:
            changed.append("deadline")
    if old.get("detail_hash") and new.get("detail_hash") and old["detail_hash"] != new["detail_hash"]:
        changed.append("detail_hash")
    if changed and changed != ["detail_hash"]:
        return "UPDATED", changed + (["deadline"] if kind else [])
    if kind:
        return kind, ["deadline"] + (["detail_hash"] if "detail_hash" in changed else [])
    if changed == ["detail_hash"]:
        return "DETAIL_CHANGED", changed
    return "UNCHANGED", []


def _fingerprint(path):
    if not path or not os.path.exists(path):
        return None
    from score import load_profile, profile_fingerprint
    return profile_fingerprint(load_profile(path))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old_dir")
    ap.add_argument("new_dir")
    ap.add_argument("--old-profile")
    ap.add_argument("--new-profile")
    args = ap.parse_args(argv)
    old, old_man = _load_dir(args.old_dir)
    new, new_man = _load_dir(args.new_dir)
    ref = now_kst()

    fp_old, fp_new = _fingerprint(args.old_profile), _fingerprint(args.new_profile)
    invalid_reasons = []
    if (args.old_profile or args.new_profile) and (fp_old is None or fp_new is None or fp_old != fp_new):
        invalid_reasons.append("프로필 판정 축 변경(또는 한쪽 스냅샷 없음)")
    rv = []
    for d in (args.old_dir, args.new_dir):
        sp = os.path.join(d, "screen_summary.json")
        rv.append(json.load(open(sp)).get("rules_version") if os.path.exists(sp) else None)
    if rv[0] is not None and rv[1] is not None and rv[0] != rv[1]:
        invalid_reasons.append(f"rules_version {rv[0]}→{rv[1]}")

    plan_old, plan_new = _plan(args.old_dir), _plan(args.new_dir)
    old_time = None
    mp_old = os.path.join(args.old_dir, "run_manifest.json")
    if os.path.exists(mp_old):
        old_time = _dt(json.load(open(mp_old, encoding="utf-8")).get("generated_at"))
    comparable, not_comparable = set(), {}
    for src in set(old_man) | set(new_man):
        a, b = old_man.get(src), new_man.get(src)
        if not a or not b:
            not_comparable[src] = "한쪽 조사에 없음"
        elif a["status"] != "ok" or b["status"] != "ok":
            not_comparable[src] = f"status {a['status']}→{b['status']}"
        elif not _plan_equal(plan_old, plan_new, src):
            not_comparable[src] = "검색 계획(search_plan.json) 다름 또는 없음"
        else:
            comparable.add(src)

    items, counts = [], {}
    old_by_dup = {}
    for r in old.values():
        old_by_dup.setdefault(r.get("dup_key"), []).append(r)
    for rid, r in new.items():
        if invalid_reasons:
            kind, fields, o = "NEW", [], old.get(rid)
        elif rid not in old and r["source"] not in comparable:
            # 비교 불가 소스(페이지 캡·계획 변경): '이번에 처음 봤다'일 뿐 새 공고인지 모른다.
            # 게시일이 직전 조사 이후면 NEW로 올리고, 아니면 FIRST_SEEN으로 따로 둔다.
            posted = _dt(r.get("posted_at"))
            kind = "NEW" if posted and old_time and posted > old_time else "FIRST_SEEN"
            fields, o = [], None
        elif rid not in old:
            # 재공고: 같은 소스·같은 회사+제목의 직전 공고가 '믿을 수 있게' 사라졌거나 마감됐을 때만.
            # (비교 불가 소스에서 안 보인 것, 다른 소스의 같은 공고는 근거가 되지 않는다)
            prev = [x for x in old_by_dup.get(r.get("dup_key"), [])
                    if x["source"] == r["source"] and x["source"] in comparable
                    and (record_id(x) not in new or is_expired(x, ref))]
            kind, fields, o = ("REPOSTED", [], prev[0]) if prev else ("NEW", [], None)
        else:
            o = old[rid]
            kind, fields = compare(o, r)
        counts[kind] = counts.get(kind, 0) + 1
        if kind != "UNCHANGED":
            items.append({"kind": kind, "changed_fields": fields, "record": r,
                          "old": {k: o.get(k) for k in ("deadline", "title", "openings", "detail_hash")} if o else None})
    missing = []
    for rid, r in old.items():
        if rid in new or r["source"] not in comparable:
            continue
        if is_expired(r, ref):
            counts["EXPIRED"] = counts.get("EXPIRED", 0) + 1
            continue
        missing.append({"kind": "MISSING", "record": r})
    counts["MISSING"] = len(missing)
    write_jsonl(os.path.join(args.new_dir, "diff_items.jsonl"), items)
    write_jsonl(os.path.join(args.new_dir, "diff_missing.jsonl"), missing)
    summary = {"generated_at": ref.isoformat(timespec="seconds"), "old_dir": os.path.abspath(args.old_dir),
               "counts": counts, "carry_over_invalidated": invalid_reasons,
               "comparable_sources": sorted(comparable), "not_comparable_sources": not_comparable}
    atomic_write(os.path.join(args.new_dir, "diff_summary.json"), json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    if invalid_reasons:
        print("CARRY-OVER INVALIDATED: " + "; ".join(invalid_reasons), file=sys.stderr)
    for src, why in not_comparable.items():
        print(f"WARNING: {src} 비교 불가({why}) — MISSING 판정 안 함", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

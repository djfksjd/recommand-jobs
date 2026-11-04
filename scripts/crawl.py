#!/usr/bin/env python3
"""recommand-jobs 수집 CLI.

  python3 crawl.py sources                                   # 소스 목록·상태
  python3 crawl.py list core -o OUT --plan OUT/search_plan.json [--max-pages 5]
  python3 crawl.py list jasoseol,jobkorea -o OUT --plan ...
  python3 crawl.py detail OUT/jasoseol.jsonl ID [ID ...] -o OUT/details
  python3 crawl.py official OUT/details saramin:12345678 [ID ...] [--url 공식공고URL]

list   : 소스별 OUT/<source>.jsonl + OUT/run_manifest.json. 종료코드 0 ok / 2 partial / 3 manual.
detail : 상세 원문을 OUT/details/<source>_<id>.json 으로 저장하고, 목록 jsonl 해당 레코드에
         detail_hash·detail_checked_at·openings(직무명 보강)·employer_url 을 원자적으로 병합한다.

official : 채용 사이트 요약 대신 기업·기관 공식 공고(employer_url·첨부 공고문 PDF/HWP)를 읽어
         OUT/details/<source>_<id>.official.json 으로 저장한다. 자세한 내용은 official.py.

search_plan.json (프로필에서 만든 고정 검색 계획 — 실행 전에 저장해 두고 diff 때 같은 것을 쓴다):
  {"plan_version": 1, "keywords": ["전기", "설비", ...], "career": ["신입", "인턴"],
   "days_ahead": 120, "max_pages": 5}
  키워드 자체는 manifest에 남기지 않는다(개수만).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from jobs_common import (EXIT_MANUAL, EXIT_OK, EXIT_PARTIAL, Blocked, Fetcher,  # noqa: E402
                         RobotsDisallowed, atomic_write, log, make_run, now_kst, overall_exit,
                         read_jsonl, update_manifest, write_jsonl)
from sources import GROUPS, REGISTRY  # noqa: E402


def load_adapter(name):
    meta = REGISTRY[name]
    if not meta.get("module"):
        return None
    return importlib.import_module(f"sources.{meta['module']}")


def resolve(spec):
    names = []
    for tok in spec.split(","):
        tok = tok.strip()
        if tok in GROUPS:
            names += GROUPS[tok]
        elif tok in REGISTRY:
            names.append(tok)
        else:
            raise SystemExit(f"알 수 없는 소스: {tok} (crawl.py sources 로 확인)")
    return list(dict.fromkeys(names))


def cmd_sources(_args):
    for name, m in REGISTRY.items():
        print(f"{name:12} {m['status']:9} {m['label']}  — {m['note']}")
    print("\n그룹:", ", ".join(f"{k}={'+'.join(v)}" for k, v in GROUPS.items()))
    return 0


def cmd_list(args):
    plan = {}
    if args.plan:
        with open(args.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
    if args.max_pages:
        plan["max_pages"] = args.max_pages
    os.makedirs(args.out, exist_ok=True)
    runs = []
    for name in resolve(args.sources):
        meta = REGISTRY[name]
        if meta["status"] in ("excluded", "manual", "unsupported"):
            runs.append(make_run(name, "skipped", EXIT_PARTIAL, 0, 0, f"{meta['status']}: {meta['note']}"))
            log(f"[{name}] 건너뜀 — {meta['status']}: {meta['note']}")
            continue
        mod = load_adapter(name)
        f = Fetcher(mod.HOSTS, delay=getattr(mod, "DELAY", 0.6))
        log(f"[{name}] 수집 시작")
        try:
            res = mod.list_postings(f, plan)
            recs = res["records"]
            errors = res.get("errors") or []
            status, code = ("partial", EXIT_PARTIAL) if errors or res.get("partial") else ("ok", EXIT_OK)
            stop = res.get("stop_reason")
        except (Blocked, RobotsDisallowed) as e:
            recs, errors, status, code, stop, res = [], [str(e)], "manual", EXIT_MANUAL, "blocked", {}
        except Exception as e:  # 구조 변경 등 — 조용히 0건 성공으로 만들지 않는다
            recs, errors, status, code, stop, res = [], [f"{type(e).__name__}: {e}"], "partial", EXIT_PARTIAL, "error", {}
        if status != "manual" and not recs and not errors:
            status, code, errors = "partial", EXIT_PARTIAL, ["0건 — 구조 변경 의심(성공으로 취급하지 않음)"]
        # 전수형 소스: 사이트가 밝힌 총건수와 실제 수집량이 크게 어긋나면 성공으로 보지 않는다
        rt = res.get("reported_total") if isinstance(res, dict) else None
        if status == "ok" and getattr(mod, "KIND", "") == "full" and rt:
            got = len(recs) + int(res.get("duplicates") or 0)
            got += int(res.get("excluded") or 0)  # 어댑터가 마감·교육 등으로 뺀 건수를 알려 주면 더한다
            if got < 0.8 * rt:
                status, code = "partial", EXIT_PARTIAL
                errors = errors + [f"총건수 불일치: 사이트 {rt}건 / 수집 {got}건"]
        out_path = os.path.join(args.out, f"{name}.jsonl")
        if recs or not os.path.exists(out_path):
            write_jsonl(out_path, recs)
        runs.append(make_run(name, status, code, res.get("pages", 0), len(recs), stop, errors,
                             reported_total=res.get("reported_total"), duplicates=res.get("duplicates"),
                             scope=res.get("scope")))
        log(f"[{name}] {status} — {len(recs)}건, 요청 {f.requests_made}회" + (f", 오류 {len(errors)}" if errors else ""))
    update_manifest(args.out, runs)
    code = overall_exit(runs)
    log(f"종료코드 {code} (0 ok / 2 partial / 3 manual) — {os.path.join(args.out, 'run_manifest.json')}")
    return code


def _merge_detail_deadline(rec, d):
    """상세의 마감 정보가 목록보다 정확하다(목록은 'D-3'만 주거나 상시를 날짜로 채우는 사이트가 있다)."""
    from jobs_common import parse_deadline
    dl, kind, prec = d.get("deadline"), d.get("deadline_kind"), d.get("deadline_precision")
    if not kind or kind == "unknown":
        if not d.get("deadline_raw"):
            return
        dl, kind, prec = parse_deadline(d["deadline_raw"])
        if kind == "unknown":
            return
    if (dl, kind) != (rec.get("deadline"), rec.get("deadline_kind")):
        rec.setdefault("extra", {})["list_deadline"] = [rec.get("deadline"), rec.get("deadline_kind")]
    rec["deadline"], rec["deadline_kind"] = dl, kind
    rec["deadline_precision"] = prec or ("minute" if dl else None)
    if d.get("deadline_raw"):
        rec["deadline_raw"] = d["deadline_raw"]
    if kind == "채용시":
        rec["may_close_early"] = True


def cmd_detail(args):
    recs = read_jsonl(args.jsonl)
    by_id = {r["source_id"]: r for r in recs}
    if not recs:
        raise SystemExit("빈 jsonl")
    name = recs[0]["source"]
    mod = load_adapter(name)
    f = Fetcher(mod.HOSTS, delay=getattr(mod, "DELAY", 0.6))
    os.makedirs(args.out, exist_ok=True)
    ids = args.ids or list(by_id)
    failures, manual = 0, 0
    for sid in ids:
        rec = by_id.get(str(sid))
        if not rec:
            log(f"[{name}] {sid}: 목록에 없음")
            failures += 1
            continue
        try:
            d = mod.fetch_detail(f, rec)
        except (Blocked, RobotsDisallowed) as e:
            log(f"[{name}] {sid}: manual — {e}")
            rec["detail_status"] = "manual"
            manual += 1
            continue
        except Exception as e:
            log(f"[{name}] {sid}: 실패 — {type(e).__name__}: {e}")
            rec["detail_status"] = "error"
            failures += 1
            continue
        d["source"], d["source_id"], d["url"] = name, rec["source_id"], rec["url"]
        d["fetched_at"] = now_kst().isoformat(timespec="seconds")
        atomic_write(os.path.join(args.out, f"{name}_{rec['source_id']}.json"),
                     json.dumps(d, ensure_ascii=False, indent=1))
        if d.get("openings"):
            rec["openings"] = d["openings"]
        if d.get("employer_url"):
            rec["employer_url"] = d["employer_url"]
        _merge_detail_deadline(rec, d)
        if d.get("edu") and not rec.get("edu"):
            rec["edu"] = d["edu"]
        if d.get("may_close_early") is not None:
            rec["may_close_early"] = d["may_close_early"]
        rec["detail_hash"] = hashlib.sha256((d.get("text") or "").encode()).hexdigest()[:16]
        rec["detail_checked_at"] = d["fetched_at"]
        rec["detail_status"] = "incomplete" if d.get("text_incomplete") else "ok"
    write_jsonl(args.jsonl, recs)
    log(f"[{name}] 상세 {len(ids)}건 — 실패 {failures}, manual {manual}")
    if manual and manual == len(ids):
        return EXIT_MANUAL
    return EXIT_PARTIAL if failures or manual else EXIT_OK


def cmd_official(args):
    from official import fetch_official
    if args.url and len(args.ids) != 1:
        raise SystemExit("--url은 공고 하나에만 쓴다")
    if args.site_is_original:
        from official import confirm_site_original
        for spec in args.ids:
            confirm_site_original(args.details, spec, args.site_is_original)
        log(f"[official] {len(args.ids)}건 — 채용 사이트 본문을 원본으로 기록")
        return EXIT_OK
    ok = thin = blocked = 0
    for spec in args.ids:
        res = fetch_official(args.details, spec, args.url or [])
        st = [p["status"] for p in res["parts"]]
        if "ok" in st:
            ok += 1
        elif st and all(s == "manual" for s in st):
            blocked += 1
        else:
            thin += 1
    log(f"[official] {len(args.ids)}건 — 원문 확보 {ok}, 부족·주소 없음 {thin}, 차단 {blocked}")
    if blocked and blocked == len(args.ids):
        return EXIT_MANUAL
    return EXIT_OK if ok == len(args.ids) else EXIT_PARTIAL


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sources")
    p = sub.add_parser("list")
    p.add_argument("sources", help="소스 이름 또는 그룹(core/public/extra/all), 쉼표로 여러 개")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--plan")
    p.add_argument("--max-pages", type=int)
    p = sub.add_parser("detail")
    p.add_argument("jsonl")
    p.add_argument("ids", nargs="*")
    p.add_argument("-o", "--out", required=True)
    p = sub.add_parser("official")
    p.add_argument("details", help="OUT/details 폴더")
    p.add_argument("ids", nargs="+", help="source:id (예: saramin:12345678)")
    p.add_argument("--url", action="append", help="에이전트가 찾은 공식 공고 주소(공고 하나일 때)")
    p.add_argument("--site-is-original", metavar="확인한_근거",
                   help="회사가 그 채용 사이트에 직접 올린 공고가 원본임을 확인했을 때(공공기관에는 쓰지 않는다)")
    args = ap.parse_args(argv)
    return {"sources": cmd_sources, "list": cmd_list, "detail": cmd_detail,
            "official": cmd_official}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

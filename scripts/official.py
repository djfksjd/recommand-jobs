"""공식 공고 원문 확보 — 채용 사이트 요약 대신 기업·기관이 직접 올린 공고와 첨부 공고문을 읽는다.

왜: 채용 사이트 본문은 요약본인 경우가 많다. 2026-11 실측에서 한 지방공기업 전기 공고는 채용 사이트
본문(763자)에 거주지 요건이 없었고, 공식 공고문에만 "공고일 1년 전부터 해당 시·도 주민등록" 요건이 있었다.

  python3 crawl.py official OUT/details saramin:12345678 [--url 공식공고URL ...]

시도 순서: --url(에이전트가 기관 채용 홈페이지에서 찾은 주소) → employer_url → 첨부(공고문 PDF·HWP·HWPX).
결과: OUT/details/<source>_<id>.official.json {parts:[...], text, fetched_at}
      원본 파일은 OUT/details/official/<source>_<id>/ 에 둔다(신뢰하지 않는 데이터 — 실행하지 않는다).
자바스크립트로만 그려지는 채용 페이지는 본문이 비어 'thin'이 된다. 그때는 에이전트가 브라우저·WebFetch로
읽은 원문을 OUT/details/<source>_<id>.official.txt 에 저장한다 — requirements.py가 두 파일을 모두 읽는다.
robots·로그인·CAPTCHA 우회는 하지 않는다(Fetcher가 강제).
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from urllib.parse import urlsplit

from jobs_common import Blocked, Fetcher, RobotsDisallowed, atomic_write, log, now_kst

MIN_TEXT = 300  # 이보다 짧으면 'thin' — 공고 본문을 못 읽은 것으로 본다
ATTACH_HINT = re.compile(r"공고|모집|채용|요강|안내", re.I)
ATTACH_EXT = re.compile(r"\.(pdf|hwp|hwpx)(\?|$)", re.I)
SKIP_EXT = re.compile(r"\.(zip|rar|7z|xlsx?|docx?|pptx?|jpe?g|png)(\?|$)", re.I)


def base_name(spec: str) -> str:
    return spec.replace(":", "_", 1)


def _html_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            t.decompose()
        text = soup.get_text("\n")
    except ImportError:
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", "\n", text)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _run(cmd, timeout=60) -> str:
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def _hwpx_text(data: bytes) -> str:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in sorted(z.namelist()):
            if re.match(r"Contents/section\d+\.xml$", n):
                xml = z.read(n).decode("utf-8", "replace")
                out.append(re.sub(r"<[^>]+>", " ", re.sub(r"</hp:p>", "\n", xml)))
    return re.sub(r"[ \t]+", " ", "\n".join(out))


def to_text(data: bytes, ctype: str, url: str, save_dir: str, idx: int) -> tuple[str, str]:
    """(kind, text). 원본 바이트는 save_dir에 저장한다."""
    os.makedirs(save_dir, exist_ok=True)
    low = url.lower()
    if data[:4] == b"%PDF" or "pdf" in ctype or low.split("?")[0].endswith(".pdf"):
        p = os.path.join(save_dir, f"{idx:02d}.pdf")
        open(p, "wb").write(data)
        if not shutil.which("pdftotext"):
            return "pdf", ""
        return "pdf", _run(["pdftotext", "-layout", "-enc", "UTF-8", p, "-"])
    if data[:4] == b"\xd0\xcf\x11\xe0":  # HWP 5 (OLE)
        p = os.path.join(save_dir, f"{idx:02d}.hwp")
        open(p, "wb").write(data)
        return "hwp", _run(["hwp5txt", p]) if shutil.which("hwp5txt") else ""
    if data[:2] == b"PK" and (low.split("?")[0].endswith(".hwpx") or b"Contents/section" in data[:4000]):
        p = os.path.join(save_dir, f"{idx:02d}.hwpx")
        open(p, "wb").write(data)
        try:
            return "hwpx", _hwpx_text(data)
        except zipfile.BadZipFile:
            return "hwpx", ""
    if data[:2] == b"PK" or data[:4] in (b"Rar!", b"7z\xbc\xaf"):
        return "archive", ""  # 압축 첨부는 풀지 않는다(서식 모음인 경우가 대부분)
    html = data.decode("utf-8", "replace")
    open(os.path.join(save_dir, f"{idx:02d}.html"), "w", encoding="utf-8").write(html)
    return "html", _html_text(html)


def candidate_urls(d: dict, extra: list[str]) -> list[tuple[str, str]]:
    out, seen = [], set()

    def add(u, why):
        if u and u.startswith("http") and u not in seen:
            seen.add(u)
            out.append((u, why))
    for u in extra:
        add(u, "agent_url")
    add(d.get("employer_url"), "employer_url")
    for a in d.get("attachments") or []:
        name, u = str(a.get("name") or ""), a.get("url")
        if SKIP_EXT.search(name):
            continue
        if ATTACH_EXT.search(name) or ATTACH_EXT.search(str(u or "")) or ATTACH_HINT.search(
                str(a.get("kind") or "") + name):
            add(u, "attachment:" + name[:60])
    return out


def fetch_official(details_dir: str, spec: str, extra_urls: list[str]) -> dict:
    base = base_name(spec)
    path = os.path.join(details_dir, base + ".json")
    if not os.path.exists(path):
        raise SystemExit(f"상세 파일 없음: {path} — 먼저 crawl.py detail")
    d = json.load(open(path, encoding="utf-8"))
    save_dir = os.path.join(details_dir, "official", base)
    parts, texts = [], []
    urls = candidate_urls(d, extra_urls)
    if not urls:
        log(f"[official] {spec}: 공식 공고 주소 없음 — 기관·기업 채용 홈페이지에서 찾아 --url로 다시")
    for i, (u, why) in enumerate(urls, 1):
        host = (urlsplit(u).hostname or "").lower()
        part = {"url": u, "why": why, "host": host}
        try:
            f = Fetcher([host], delay=1.5)
            data, ctype, final = f.get_bytes(u)
            kind, text = to_text(data, ctype, final, save_dir, i)
            part.update(kind=kind, chars=len(text.strip()),
                        status="ok" if len(text.strip()) >= MIN_TEXT else "thin")
            if text.strip():
                texts.append(f"[공식 원문 {i}: {why} — {u}]\n{text.strip()}")
        except (Blocked, RobotsDisallowed) as e:
            part.update(status="manual", error=str(e)[:120])
        except Exception as e:  # noqa: BLE001
            part.update(status="error", error=f"{type(e).__name__}: {str(e)[:100]}")
        parts.append(part)
        log(f"[official] {spec} {why[:30]} → {part['status']} {part.get('chars', '')}")
    res = {"source_spec": spec, "fetched_at": now_kst().isoformat(timespec="seconds"),
           "parts": parts, "text": "\n\n".join(texts)}
    atomic_write(os.path.join(details_dir, base + ".official.json"),
                 json.dumps(res, ensure_ascii=False, indent=1))
    return res


def confirm_site_original(details_dir: str, spec: str, reason: str) -> dict:
    """채용 사이트 본문이 곧 원본임을 에이전트가 확인했을 때 기록한다.

    쓰는 때: 회사 채용 홈페이지·첨부가 없고, 회사가 그 채용 사이트에 직접 올린 공고임을 확인한 경우.
    reason에는 무엇을 확인했는지 적는다(예: "회사 홈페이지 채용 메뉴 없음, 사람인 기업 직접 등록").
    공공기관·지방공기업은 공고문 원본이 따로 있으므로 이 기록으로 대신하지 않는다.
    """
    base = base_name(spec)
    p = os.path.join(details_dir, base + ".official.json")
    res = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {
        "source_spec": spec, "parts": [], "text": ""}
    res["parts"].append({"why": "site_is_original", "status": "ok", "kind": "confirmation",
                         "reason": reason[:200], "at": now_kst().isoformat(timespec="seconds")})
    res["fetched_at"] = now_kst().isoformat(timespec="seconds")
    atomic_write(p, json.dumps(res, ensure_ascii=False, indent=1))
    return res

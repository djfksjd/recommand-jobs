#!/usr/bin/env python3
"""recommand-jobs 공통 모듈 — 모든 사이트 어댑터가 공유한다.

담는 것
  * Fetcher      : curl_cffi(TLS 위장) 세션 + 호스트별 robots.txt 검사 + 요청 간격 + 차단 감지
  * Robots       : robots.txt 파서 (와일드카드 `*`·`$`, 최장 일치, 동률이면 Allow)
  * make_record  : 사이트마다 다른 공고 필드를 하나의 레코드 스키마(v1)로 정규화
  * 마감일 파서  : "~11.15(토)", "11/15 23:59", "D-3", "상시채용", "채용시 마감" ...
  * write_jsonl / update_manifest : 원자적 저장, run_manifest.json(schema v1)

종료코드 계약 (ir-search와 같다)
  0 = 전체 수집 성공 / 2 = partial(일부 페이지·소스 실패, 페이지 캡) / 3 = manual(차단·CAPTCHA·로그인 필요 — 우회하지 않음)

robots 정책
  가장 엄격한 해석을 쓴다: URL은 `*` 그룹 **그리고** (있다면) `Claude-User` 그룹 둘 다에서 허용돼야
  요청한다. 이 스크립트가 특정 봇 이름을 내세울 근거는 없으므로 한쪽 그룹만 골라 유리하게 해석하지
  않는다. 학습용 크롤러 차단(ClaudeBot·GPTBot)은 이 도구의 수집 근거로도, 금지 근거로도 쓰지 않는다. robots.txt가 5xx/네트워크
  오류면 보수적으로 "전부 불허"로 보고 manual 처리한다(Google 명세와 같다).

개인정보
  manifest·로그에는 건수·상태만 쓴다. 검색어·프로필·공고 본문은 넣지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

KST = timezone(timedelta(hours=9))
RECORD_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_NAME = "run_manifest.json"
EXIT_OK, EXIT_PARTIAL, EXIT_MANUAL = 0, 2, 3
DEFAULT_DELAY = 0.6  # 같은 호스트 연속 요청 사이 최소 간격(초)
MAX_RAW_BYTES = 30 * 1024 * 1024  # 첨부 공고문 다운로드 상한. 공고문은 보통 수 MB이고, 넘으면 받지 않는다
ROBOTS_AGENT_TOKENS = ("*", "claude-user")  # 존재하는 그룹 모두에서 허용돼야 한다


def now_kst() -> datetime:
    return datetime.now(KST)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ─────────────────────────────── robots.txt ───────────────────────────────

class Robots:
    """Google robots 명세 수준의 파서. 표준 urllib.robotparser는 `*`/`$`를 지원하지 않아 직접 구현."""

    def __init__(self, text: str | None, allow_all=False, deny_all=False):
        self.allow_all = allow_all
        self.deny_all = deny_all
        self.groups: list[tuple[set[str], list[tuple[bool, str]]]] = []
        if text:
            self._parse(text)

    def _parse(self, text: str):
        agents: set[str] = set()
        rules: list[tuple[bool, str]] = []
        last_was_rule = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip().lower(), val.strip()
            if key == "user-agent":
                if last_was_rule:  # 새 그룹 시작
                    self.groups.append((agents, rules))
                    agents, rules = set(), []
                agents.add(val.lower())
                last_was_rule = False
            elif key in ("allow", "disallow"):
                if not agents:
                    continue
                if val:  # 빈 Disallow = 전부 허용 → 규칙 없음과 같다
                    rules.append((key == "allow", val))
                last_was_rule = True
        if agents:
            self.groups.append((agents, rules))

    def _rules_for(self, token):
        """token 그룹의 규칙. 그룹이 없으면 None(=이 토큰 기준 판정 생략)."""
        if not any(token in ags for ags, _ in self.groups):
            return None
        return [r for ags, rs in self.groups if token in ags for r in rs]

    @staticmethod
    def _norm(p: str) -> str:
        """퍼센트 인코딩을 풀어 비교한다(한글 경로 '/비공개' == '/%EB%B9%84...'). 경로 구분에 의미가 있는
        예약 문자(%2F / %3F ? %23 # %25 % %26 & %3D =)만 인코딩된 채로 남겨 경계가 바뀌지 않게 한다."""
        from urllib.parse import unquote
        keep = {"2F", "3F", "23", "25", "26", "3D"}
        parts = re.split(r"(%[0-9A-Fa-f]{2})", p)
        out, buf = [], []
        for part in parts:
            if re.fullmatch(r"%[0-9A-Fa-f]{2}", part) and part[1:].upper() in keep:
                out.append(unquote("".join(buf)))
                buf = []
                out.append(part.upper())
            else:
                buf.append(part)
        out.append(unquote("".join(buf)))
        return "".join(out)

    @staticmethod
    def _match_len(pattern: str, path: str) -> int:
        """패턴이 path 앞부분과 일치하면 패턴 길이, 아니면 -1."""
        anchored = pattern.endswith("$")
        pat = pattern[:-1] if anchored else pattern
        rx = "^" + ".*".join(re.escape(p) for p in pat.split("*")) + ("$" if anchored else "")
        return len(pattern) if re.match(rx, path) else -1

    def allowed(self, url: str, tokens=ROBOTS_AGENT_TOKENS) -> bool:
        if self.deny_all:
            return False
        if self.allow_all:
            return True
        parts = urlsplit(url)
        path = self._norm((parts.path or "/") + (("?" + parts.query) if parts.query else ""))
        if path == "/robots.txt":
            return True
        for tok in tokens:
            rules = self._rules_for(tok)
            if rules is None:
                continue
            best_len, best_allow = -1, True
            for allow, pat in rules:
                n = self._match_len(self._norm(pat), path)
                if n > best_len or (n == best_len and allow and not best_allow):
                    best_len, best_allow = n, allow
            if not best_allow:
                return False
        return True


# ─────────────────────────────── Fetcher ───────────────────────────────

class Blocked(Exception):
    """차단·CAPTCHA·로그인 요구 — 우회하지 않고 manual(exit 3)로 넘긴다."""


class RateLimited(Blocked):
    """429 — Retry-After를 한 번 기다려도 계속되면 올린다. 어댑터는 모은 것까지 partial로 남긴다."""


class TooLarge(Exception):
    """응답이 MAX_RAW_BYTES를 넘는다 — 끝까지 받지 않고 끊는다."""


class RobotsDisallowed(Exception):
    pass


BLOCK_MARKERS = (
    "captcha", "g-recaptcha", "hcaptcha", "cf-chl", "challenge-platform",
    "access denied", "request blocked", "비정상적인 접근", "자동입력 방지", "보안문자",
)


_URL_RX = re.compile(r"https?://\S+")


def scrub(msg) -> str:
    """오류 문자열에서 URL(검색어·API 키가 든 쿼리 포함)을 지운다 — manifest·로그 개인정보 보호."""
    return _URL_RX.sub(lambda m: m.group(0).split("?", 1)[0] + ("?…" if "?" in m.group(0) else ""), str(msg))[:300]


def looks_blocked(text: str) -> bool:
    head = (text or "")[:6000].lower()
    return any(m in head for m in BLOCK_MARKERS)


def read_capped(r, limit: int) -> bytes:
    """응답 본문을 limit 바이트까지만 읽는다. Content-Length가 넘거나 실제로 넘으면 TooLarge."""
    cl = str((r.headers or {}).get("content-length") or "")
    if cl.isdigit() and int(cl) > limit:
        _close(r)
        raise TooLarge(f"응답 {int(cl) // 1048576}MB > 상한 {limit // 1048576}MB")
    if not hasattr(r, "iter_content"):
        body = r.content
    else:
        buf = bytearray()
        for chunk in r.iter_content():
            buf += chunk
            if len(buf) > limit:
                _close(r)
                raise TooLarge(f"응답이 상한 {limit // 1048576}MB를 넘음")
        body = bytes(buf)
    if len(body) > limit:
        raise TooLarge(f"응답이 상한 {limit // 1048576}MB를 넘음")
    return body


def _close(r):
    try:
        r.close()
    except Exception:  # noqa: BLE001
        pass


class Fetcher:
    """사이트 어댑터가 쓰는 유일한 HTTP 창구.

    allowed_hosts 밖으로는 요청하지 않는다(리다이렉트 포함). 모든 URL은 요청 전에 robots 검사.
    """

    def __init__(self, allowed_hosts, delay=DEFAULT_DELAY, impersonate="chrome", timeout=20):
        try:
            from curl_cffi import requests as cr  # type: ignore
        except ImportError:  # pragma: no cover
            log("curl_cffi가 필요합니다: pip install curl_cffi")
            raise
        self.session = cr.Session(impersonate=impersonate)
        self.allowed_hosts = tuple(h.lower() for h in allowed_hosts)
        self.delay = delay
        self.timeout = timeout
        self._robots: dict[str, Robots] = {}
        self._last: dict[str, float] = {}
        self.requests_made = 0
        self.blocked_reason = None

    # -- host / robots --
    def _check_host(self, url):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https":
            raise Blocked(f"https 아님: {url[:80]}")
        if not any(host == h or host.endswith("." + h) for h in self.allowed_hosts):
            raise Blocked(f"허용 호스트 밖: {host}")
        return host

    def robots_for(self, host) -> Robots:
        if host in self._robots:
            return self._robots[host]
        url = f"https://{host}/robots.txt"
        rb = None
        try:
            for _hop in range(4):
                self._sleep(host)
                r = self.session.get(url, timeout=self.timeout, allow_redirects=False)
                self.requests_made += 1
                if r.status_code in (301, 302, 303, 307, 308):
                    from urllib.parse import urljoin
                    nxt = urljoin(url, r.headers.get("location", ""))
                    try:
                        self._check_host(nxt)  # 허용 호스트·https 밖으로는 따라가지 않는다
                    except Blocked:
                        log(f"[robots] {host} robots.txt가 허용 호스트 밖으로 리다이렉트 → 전부 불허")
                        rb = Robots(None, deny_all=True)
                        break
                    url = nxt
                    continue
                if r.status_code == 429:
                    log(f"[robots] {host} robots.txt 429 → 전부 불허(요청 한도)")
                    rb = Robots(None, deny_all=True)
                elif r.status_code in (401, 403):
                    rb = Robots(None, deny_all=True)  # 접근 자체를 거부 — 허용으로 보지 않는다
                elif 400 <= r.status_code < 500:
                    rb = Robots(None, allow_all=True)  # robots.txt 없음(404 등) = 제한 없음
                elif r.status_code >= 500:
                    rb = Robots(None, deny_all=True)
                else:
                    rb = Robots(r.text)
                break
            if rb is None:
                rb = Robots(None, deny_all=True)
        except Exception as e:  # 네트워크 오류 = 불허로 간주
            log(f"[robots] {host} 가져오기 실패({type(e).__name__}) → 보수적으로 전부 불허")
            rb = Robots(None, deny_all=True)
        self._robots[host] = rb
        return rb

    def allowed(self, url) -> bool:
        host = self._check_host(url)
        return self.robots_for(host).allowed(url)

    def _sleep(self, host):
        last = self._last.get(host)
        if last is not None:
            wait = self.delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last[host] = time.monotonic()

    # -- request --
    def request(self, method, url, *, params=None, json_body=None, data=None, headers=None,
                retries=2, expect_json=False, raw=False):
        try:
            return self._request(method, url, params=params, json_body=json_body, data=data,
                                 headers=headers, retries=retries, expect_json=expect_json, raw=raw)
        except Blocked as e:
            self.blocked_reason = self.blocked_reason or str(e)
            raise

    def _request(self, method, url, *, params=None, json_body=None, data=None, headers=None,
                 retries=2, expect_json=False, raw=False):
        if self.blocked_reason:  # 한 번 차단되면 이 소스로는 더 요청하지 않는다
            raise Blocked(f"이미 차단됨 — 추가 요청 중단 ({self.blocked_reason[:60]})")
        host = self._check_host(url)
        full = url
        if params:
            from urllib.parse import urlencode
            full = url + ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        if not self.robots_for(host).allowed(full):
            raise RobotsDisallowed(f"robots 불허: {urlsplit(full).path[:80]}")
        err = None
        for attempt in range(retries + 1):
            self._sleep(host)
            try:
                extra = {"stream": True} if raw else {}  # 첨부는 스트리밍으로 받아 크기 상한을 건다
                r = self.session.request(method, url, params=params, json=json_body, data=data,
                                         headers=headers, timeout=self.timeout, allow_redirects=False,
                                         **extra)
                self.requests_made += 1
            except Exception as e:
                err = scrub(f"{type(e).__name__}: {e}")
                time.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location", "")
                from urllib.parse import urljoin
                nxt = urljoin(url, loc)
                self._check_host(nxt)
                if not self.robots_for(urlsplit(nxt).hostname.lower()).allowed(nxt):
                    raise RobotsDisallowed(f"리다이렉트 대상 robots 불허: {urlsplit(nxt).path[:80]}")
                if "login" in nxt.lower() or "signin" in nxt.lower():
                    raise Blocked(f"로그인 페이지로 리다이렉트: {urlsplit(nxt).path[:60]}")
                url, params, json_body, data = nxt, None, None, None
                method = "GET" if r.status_code in (301, 302, 303) else method
                continue
            if r.status_code == 429:
                ra = r.headers.get("retry-after") or ""
                wait = int(ra) if ra.isdigit() else 30
                if attempt < retries and wait <= 60:
                    log(f"[{host}] 429 — {wait}초 기다린 뒤 한 번 더 (Retry-After 존중)")
                    time.sleep(wait)
                    self.delay = max(self.delay * 2, 2.0)  # 이후 간격도 늘린다
                    continue
                raise RateLimited("HTTP 429 (요청 한도) — 간격을 늘려 나중에 다시")
            if r.status_code in (401, 403):
                raise Blocked(f"HTTP {r.status_code}")
            if r.status_code >= 500:
                err = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            ctype = (r.headers.get("content-type") or "").lower()
            if raw:  # 첨부 공고문(PDF·HWP) — (바이트, content-type, 최종 URL)
                body = read_capped(r, MAX_RAW_BYTES)
                if "html" in ctype and looks_blocked(body.decode("utf-8", "replace")):
                    raise Blocked("200 응답이지만 차단/CAPTCHA 페이지")
                return body, ctype, url
            if "html" in ctype and looks_blocked(r.text):
                raise Blocked("200 응답이지만 차단/CAPTCHA 페이지")
            if expect_json:
                try:
                    return r.json()
                except Exception:
                    raise RuntimeError(f"JSON 아님 (content-type={ctype[:40]})")
            return r.text
        raise RuntimeError(f"재시도 소진: {err}")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def get_bytes(self, url, **kw):
        return self.request("GET", url, raw=True, **kw)

    def get_json(self, url, **kw):
        return self.request("GET", url, expect_json=True, **kw)

    def post_json(self, url, body, **kw):
        return self.request("POST", url, json_body=body, expect_json=True, **kw)


# ─────────────────────────────── 정규화 ───────────────────────────────

_COMPANY_NOISE = re.compile(r"\(주\)|㈜|\(유\)|\(재\)|\(사\)|주식회사|유한회사|재단법인|사단법인|"
                            r"\bco\.?,?\s*ltd\.?|\binc\.?|\bcorp\.?|[\s·\-_.,()\[\]]")


def norm_text(s) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;|&#160;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_company(name) -> str:
    s = norm_text(name).lower()
    return _COMPANY_NOISE.sub("", s)


def norm_title(title) -> str:
    s = norm_text(title).lower()
    noise = r"(채용\s*시\s*마감|마감\s*임박|d\s*-\s*\d+|급구|긴급|재공고|수정|연장|~?\d{1,2}[./]\d{1,2}\s*(까지)?|상시)"
    s = re.sub(r"[\[(【]\s*" + noise + r"\s*[\])】]", "", s)  # 괄호 속 직무명(전기)·(기계)는 남긴다
    s = re.sub(r"20\d\d\s*(년|년도)?", "", s)
    return re.sub(r"[^0-9a-z가-힣]", "", s)


CAREER_VALUES = ("신입", "경력", "인턴", "무관", "계약", "기타")


def norm_career(values) -> list[str]:
    """사이트별 경력 표기를 CAREER_VALUES 부분집합으로."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out = []
    for v in values:
        t = norm_text(v)
        if not t:
            continue
        if "무관" in t:
            out += ["신입", "경력", "무관"]
        else:
            if "신입" in t:
                out.append("신입")
            if "경력" in t or re.search(r"\d+\s*년\s*(이상|↑)", t):
                out.append("경력")
            if "인턴" in t:
                out.append("인턴")
            if "계약" in t:
                out.append("계약")
    return sorted(set(out), key=CAREER_VALUES.index) or ["기타"]


# ─────────────────────────────── 마감일 ───────────────────────────────

DEADLINE_KINDS = ("date", "상시", "채용시", "unknown")


def parse_deadline(raw, ref: datetime | None = None):
    """마감 표기를 (deadline_iso | None, deadline_kind) 로.

    연도가 없는 표기(11.15)는 ref(기본 오늘) 기준으로 가장 가까운 미래/최근 날짜를 고른다:
    ref보다 60일 넘게 과거가 되면 다음 해로 본다(12월에 1월 마감 공고).
    시각이 없으면 비교용으로 23:59를 넣되 precision="day"로 표시한다 — 보고서에는 '시각 불명'으로 쓴다.
    반환: (deadline_iso | None, deadline_kind, precision)  precision ∈ minute/day/None
    """
    ref = ref or now_kst()
    if raw is None:
        return None, "unknown", None
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=KST)
        return dt.astimezone(KST).isoformat(timespec="minutes"), "date", "minute"
    s = norm_text(raw)
    if not s:
        return None, "unknown", None
    if re.search(r"상시|수시\s*채용|연중", s):
        return None, "상시", None
    if re.search(r"채용\s*시|충원\s*시|마감\s*시까지|인재\s*풀", s):
        return None, "채용시", None
    if re.search(r"[~∼〜]", s):  # "26.11.03 ~ 26.11.10" 같은 기간 → 끝 날짜만
        tail = re.split(r"[~∼〜]", s)[-1]
        if re.search(r"\d", tail):
            s = tail
    # ISO 형식(시간대 표기가 있으면 존중해 KST로 변환)
    mi = re.search(r"(20\d\d-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?)(Z|[+-]\d\d:?\d\d)?", s)
    if mi and mi.group(2):
        tz = mi.group(2).replace("Z", "+00:00")
        if re.match(r"[+-]\d{4}$", tz):
            tz = tz[:3] + ":" + tz[3:]
        try:
            dt = datetime.fromisoformat(mi.group(1).replace(" ", "T") + tz)
            return dt.astimezone(KST).isoformat(timespec="minutes"), "date", "minute"
        except ValueError:
            pass
    m = re.search(r"(20\d\d)-(\d\d)-(\d\d)[T ](\d\d):(\d\d)", s)
    if m:
        y, mo, d, hh, mm = map(int, m.groups())
        return datetime(y, mo, d, hh, mm, tzinfo=KST).isoformat(timespec="minutes"), "date", "minute"
    m = re.search(r"D\s*-\s*(\d{1,3})", s, re.I)
    if m and not re.search(r"\d{1,2}[./월]\s*\d{1,2}", s):
        d = (ref + timedelta(days=int(m.group(1)))).replace(hour=23, minute=59, second=0, microsecond=0)
        return d.isoformat(timespec="minutes"), "date", "day"
    if re.search(r"오늘\s*마감|D\s*-\s*day", s, re.I):
        return ref.replace(hour=23, minute=59, second=0, microsecond=0).isoformat(timespec="minutes"), "date", "day"
    if re.search(r"내일\s*마감", s):
        d = (ref + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
        return d.isoformat(timespec="minutes"), "date", "day"
    m = re.search(r"(?:(20\d\d|\d\d)\s*[./년-]\s*)?(\d{1,2})\s*[./월-]\s*(\d{1,2})\s*일?", s)
    if not m:
        return None, "unknown", None
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None, "unknown", None
    hh, mm, prec = 23, 59, "day"
    tm = re.search(r"(\d{1,2})\s*:\s*(\d{2})", s[m.end():])
    if tm:
        hh, mm, prec = int(tm.group(1)), int(tm.group(2)), "minute"
        if hh == 24:
            hh, mm = 23, 59
    if y:
        year = int(y) + (2000 if len(y) == 2 else 0)
    else:
        year = ref.year
        try:
            cand = datetime(year, mo, d, tzinfo=KST)
        except ValueError:
            return None, "unknown", None
        if cand < ref - timedelta(days=60):
            year += 1
    try:
        dt = datetime(year, mo, d, hh, mm, tzinfo=KST)
    except ValueError:
        return None, "unknown", None
    return dt.isoformat(timespec="minutes"), "date", prec


def is_expired(record, ref: datetime | None = None) -> bool:
    ref = ref or now_kst()
    dl = record.get("deadline")
    if not dl:
        return False
    try:
        return datetime.fromisoformat(dl) < ref
    except ValueError:
        return False


# ─────────────────────────────── 레코드 ───────────────────────────────

LIST_HASH_FIELDS = ("company", "title", "openings", "career", "deadline", "deadline_kind", "locations")


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [norm_text(x) for x in v if norm_text(x)]
    t = norm_text(v)
    return [t] if t else []


def make_opening(*, role, opening_id=None, career=None, locations=None, headcount=None,
                 employment_type=None, extra=None) -> dict:
    """모집 직무 1건. 판정 단위는 공고가 아니라 이것이다(한 공채 안에 전기·생산·영업이 섞인다)."""
    return {
        "opening_id": str(opening_id) if opening_id is not None else None,
        "role": norm_text(role),
        "career": norm_career(career) if career is not None else [],
        "locations": _as_list(locations),
        "headcount": norm_text(headcount) or None,
        "employment_type": norm_text(employment_type) or None,
        "extra": extra or {},
    }


def make_record(*, source, source_id, url, company, title, openings=None, roles=None, career=None,
                edu=None, employment_type=None, locations=None, deadline_raw=None, deadline=None,
                deadline_kind=None, deadline_precision=None, may_close_early=None, posted_at=None,
                company_size=None, company_type=None, tags=None, salary_raw=None, apply_url=None,
                extra=None, ref=None) -> dict:
    """사이트 원본 필드를 schema v1 레코드(=출처 공고 1건)로. 모르는 값은 None/[] — 추정하지 않는다.

    openings: make_opening() 목록. 사이트가 직무 목록을 주지 않으면 roles(문자열 목록)로 대신 만든다.
    may_close_early: "채용 시 마감"·"조기 마감 가능" 문구가 있으면 True, 확인 못 했으면 None.
    """
    if deadline is None and deadline_kind is None:
        deadline, deadline_kind, deadline_precision = parse_deadline(deadline_raw, ref)
    elif deadline is not None and deadline_precision is None:
        deadline_precision = "minute"
    if openings is None:
        openings = [make_opening(role=r) for r in _as_list(roles)]
    raw_all = " ".join(filter(None, [norm_text(deadline_raw), norm_text(title)]))
    if may_close_early is None and re.search(r"채용\s*시\s*마감|조기\s*마감", raw_all):
        may_close_early = True
    if deadline_kind == "채용시":
        may_close_early = True
    rec = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "source": source,
        "source_id": str(source_id),
        "url": url,
        "company": norm_text(company),
        "company_norm": norm_company(company),
        "title": norm_text(title),
        "openings": openings,
        "career": norm_career(career) if career is not None else
                  sorted({c for o in openings for c in o["career"]}, key=CAREER_VALUES.index),
        "career_raw": (norm_text(career if isinstance(career, str) else ", ".join(_as_list(career)))
                       if career is not None else None) or None,
        "edu": norm_text(edu) or None,
        "employment_type": norm_text(employment_type) or None,
        "locations": _as_list(locations),
        "deadline": deadline,
        "deadline_kind": deadline_kind or "unknown",
        "deadline_precision": deadline_precision,
        "deadline_raw": norm_text(deadline_raw) or None,
        "may_close_early": may_close_early,
        "posted_at": posted_at,
        "company_size": company_size,
        "company_type": company_type,
        "tags": _as_list(tags),
        "salary_raw": norm_text(salary_raw) or None,
        "apply_url": apply_url,
        "extra": extra or {},
        "collected_at": now_kst().isoformat(timespec="seconds"),
    }
    rec["list_hash"] = list_hash(rec)
    rec["dup_key"] = dup_key(rec)
    return rec


def list_hash(rec) -> str:
    """목록 단계 의미 필드 해시(조회수·광고 등은 넣지 않는다). diff의 CHANGED 판단 재료."""
    payload = json.dumps([rec.get(k) for k in LIST_HASH_FIELDS], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def dup_key(rec) -> str:
    """교차 소스 '중복 후보' 키: 회사 + 정규화 제목. 식별키가 아니다.

    마감일은 넣지 않는다 — 마감 연장은 같은 공고인데 키가 바뀌면 안 된다. 같은 키라도 마감·직무가
    다르면 재공고일 수 있으므로 dedupe.py는 삭제·병합하지 않고 possible_duplicate로 묶기만 한다.
    """
    raw = f"{rec.get('company_norm', '')}|{norm_title(rec.get('title'))}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def record_id(rec) -> str:
    return f"{rec['source']}:{rec['source_id']}"


# ─────────────────────────────── 저장 ───────────────────────────────

def atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_jsonl(path, records):
    atomic_write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


VALID_STATUS = ("ok", "partial", "manual", "skipped")


def make_run(source, status, exit_code, pages_fetched, collected, stop_reason, errors=None,
             reported_total=None, duplicates=None, scope=None):
    """manifest 한 줄. scope: 이 소스가 무엇을 기준으로 모았는지(전수/키워드N개/최근분) — 검색어 자체는 넣지 않는다."""
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status {status!r}")
    if (status == "ok") != (int(exit_code) == 0):
        raise ValueError(f"status={status}와 exit_code={exit_code} 모순")
    run = {"source": source, "status": status, "exit_code": int(exit_code),
           "pages_fetched": int(pages_fetched), "collected": int(collected),
           "stop_reason": stop_reason, "scope": scope, "errors": [scrub(e)[:200] for e in (errors or [])][:20]}
    if reported_total is not None:
        run["reported_total"] = int(reported_total)
    if duplicates is not None:
        run["duplicates"] = int(duplicates)
    return run


def update_manifest(out_dir, new_runs):
    path = os.path.join(out_dir, MANIFEST_NAME)
    runs = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("manifest_schema_version") == MANIFEST_SCHEMA_VERSION:
                runs = old.get("runs", [])
        except Exception:
            os.replace(path, path + ".corrupt")
    new_sources = {r["source"] for r in new_runs}
    runs = [r for r in runs if r.get("source") not in new_sources] + list(new_runs)
    doc = {"manifest_schema_version": MANIFEST_SCHEMA_VERSION,
           "generated_at": now_kst().isoformat(timespec="seconds"), "runs": runs}
    atomic_write(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return path


def overall_exit(runs) -> int:
    codes = [r["exit_code"] for r in runs if r["status"] != "skipped"]
    if not codes:
        return EXIT_PARTIAL
    if any(c == EXIT_MANUAL for c in codes):
        return EXIT_MANUAL if all(c == EXIT_MANUAL for c in codes) else EXIT_PARTIAL
    return max(codes)

"""밖으로 나가는 글(웹 검색어, 읽을 주소)에 개인 정보나 이 컴퓨터의 파일 내용이 들어 있는지: 코드 규칙.

걸리면 정책이 매번 승인을 받는다 (safety/policy.py). 모델에게 판단을 맡기지 않는다 (안전은 코드로).
기준 (설계서 열린 질문 "개인 정보 검사 기준: 이름, 연락처, 파일 경로, 문서 본문 인용"에 대한 v0 답)
- 연락처·식별 번호: 이메일, 전화번호, 주민등록번호, 카드 번호(Luhn으로 확인)
- 비밀: API 키·토큰 모양(sk-, tvly-, ghp_, AKIA...), "password=..." 같은 글, 개인 키 머리
- 파일 경로: C:\\Users\\..., D:\\..., \\\\서버\\공유, /home/..., /Users/..., ~/... (Windows·Program Files 같은 공통 경로는 뺌)
- 이름: glados.toml [privacy] names와 [user] name
- 문서 본문: 이번 대화에서 이 컴퓨터의 도구(fs.*, shell.run, !명령)가 돌려준 글과 연속 다섯 낱말(한국어·한자는 네
  낱말) 이상 그대로 겹치면. 연구원님이 직접 쓴 글과 겹치는 부분은 뺀다 (그렇게 찾으라고 한 것이므로).
  파일 이름(…pdf, …docx)이 그 글에 있으면 그것도
- 주소(web.fetch)는 이메일·전화·특정 비밀 모양은 주소 전체를, 나머지는 질의 부분(? 뒤와 # 뒤)만 본다
  (주소의 경로는 원래 파일 경로처럼 생겼고, 공개 문서 ID는 긴 무작위 글자이기 때문)
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote_plus, urlsplit

WINDOW = 5            # 겹침으로 칠 연속 낱말 수 (영어 등)
WINDOW_CJK = 4        # 한국어·한자·가나가 섞인 글 (낱말 하나에 뜻이 많음)
CONTENT_WORDS = 3     # 그 창 안에 내용 낱말(영어 4자 이상, 한국어 2자 이상)이 이만큼은 있어야 (흔한 구절 빼기)
SAMPLE = 60

EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE = re.compile(
    r"(?<![\d.])(?:"
    r"\+\d{1,3}[ .-]?\(?\d{1,4}\)?[ .-]?\d{3,4}[ .-]?\d{4}"   # +82 10-1234-5678, +1 (415) 555-0134
    r"|01[016789][ .-]?\d{3,4}[ .-]?\d{4}"                     # 010-1234-5678, 01012345678
    r"|0\d{1,2}[ .-]\d{3,4}[ .-]\d{4}"                         # 02-123-4567, 031-1234-5678
    r")(?![\d.])"
)
RRN = re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-?[1-8]\d{6}(?!\d)")
CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
SECRET = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{30,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r")|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
ASSIGN = re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd|pwd|token)"
                    r"\s*[:=]\s*[^\s&]{4,}")
RANDOM = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_\-+/]{32,}(?![A-Za-z0-9])")
WIN_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"'<>|]*|\\\\[A-Za-z0-9._$-]+\\[^\s\"'<>|]+")
WIN_COMMON = re.compile(r"(?i)^[a-z]:\\(?:windows|program files(?: \(x86\))?|programdata)(?:\\|$)")
POSIX_PATH = re.compile(r"(?<![\w.~])(?:~[\\/]|/(?:home|Users|root)/)[^\s\"'<>|]*")
FILE_NAME = re.compile(r"[^\s\\/:*?\"<>|]+\.(?:pdf|docx?|hwpx?|xlsx?|pptx?|txt|md|csv|tsv|json|ya?ml|py|ipynb|tex|bib"
                       r"|zip|png|jpe?g)(?![\w])", re.IGNORECASE)
WORD = re.compile(r"\w+")
CJK = re.compile(r"[\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".intranet", ".home.arpa", ".corp", ".home")


@dataclass(frozen=True)
class Finding:
    kind: str       # email | phone | rrn | card | secret | path | name | local_text | file_name
    sample: str     # 보여 줄 조각 (비밀·번호는 가림)


# ---------------------------------------------------------------------- 내부 주소
def url_host(url: str) -> str:
    """주소의 호스트 (없거나 못 읽으면 "")."""
    raw = url.strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    try:
        return (urlsplit(raw).hostname or "").rstrip(".")
    except ValueError:
        return ""


def is_local_host(host: str) -> bool:
    """이 컴퓨터나 내부 네트워크를 가리키는 이름·주소인가 (인터넷에 공개된 주소가 아님).
    이름만으로 판단한다 (이름을 주소로 바꿔 보는 일은 손이 실제로 가져오기 직전에 한다)."""
    h = host.strip().strip("[]").rstrip(".").casefold()
    if not h:
        return False
    if h == "localhost" or h.endswith(LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        return "." not in h                      # 점 없는 이름 (router, nas): 내부 네트워크에서만 풀린다
    return not ip.is_global


# ---------------------------------------------------------------------- 낱말 겹침
def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _tokens(text: str) -> list[str]:
    return WORD.findall(_fold(text))


def _content(token: str) -> bool:
    return len(token) >= (2 if CJK.search(token) else 4)


def _windows(tokens: list[str], k: int) -> set[str]:
    return {" ".join(tokens[i:i + k]) for i in range(len(tokens) - k + 1)}


def _overlap(text: str, local: str, user: str) -> str:
    """text 안에서 local과 연속으로 겹치는 첫 구절 (user에도 있는 구절은 뺌). 없으면 ""."""
    words = _tokens(text)
    k = WINDOW_CJK if any(CJK.search(w) for w in words) else WINDOW
    if len(words) < k or not local:
        return ""
    have = _windows(_tokens(local), k)
    mine = _windows(_tokens(user), k) if user else set()
    for i in range(len(words) - k + 1):
        win = words[i:i + k]
        joined = " ".join(win)
        if sum(_content(w) for w in win) >= CONTENT_WORDS and joined in have and joined not in mine:
            return joined
    return ""


# ---------------------------------------------------------------------- 검사
def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _mask(s: str, keep_head: int = 4, keep_tail: int = 2) -> str:
    if len(s) <= keep_head + keep_tail + 2:
        return s[:2] + "…"
    return s[:keep_head] + "…" + s[-keep_tail:]


def _random_secret(s: str) -> bool:
    return bool(re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and len(re.findall(r"\d", s)) >= 2)


def _contacts(text: str) -> list[Finding]:
    out: list[Finding] = []
    if m := EMAIL.search(text):
        out.append(Finding("email", m.group(0)))
    if m := RRN.search(text):
        out.append(Finding("rrn", m.group(0)[:8] + "******"))
    if m := PHONE.search(text):
        out.append(Finding("phone", m.group(0).strip()))
    for m in CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits) and not RRN.fullmatch(m.group(0).strip()):
            out.append(Finding("card", "**** " + digits[-4:]))
            break
    return out


def _secrets(text: str, random_too: bool) -> list[Finding]:
    m = SECRET.search(text) or ASSIGN.search(text)
    if m:
        return [Finding("secret", _mask(m.group(0)))]
    if random_too:
        for m in RANDOM.finditer(text):
            if _random_secret(m.group(0)):
                return [Finding("secret", _mask(m.group(0)))]
    return []


def _paths(text: str, home: str) -> list[Finding]:
    for m in WIN_PATH.finditer(text):
        if not WIN_COMMON.match(text[m.start():]):          # C:\Program Files ... (띄어쓰기가 있어 따로 봄)
            return [Finding("path", m.group(0)[:SAMPLE])]
    if m := POSIX_PATH.search(text):
        return [Finding("path", m.group(0)[:SAMPLE])]
    if home and len(home) > 3 and _fold(home) in _fold(text):
        return [Finding("path", home)]
    return []


def _names(text: str, names: tuple[str, ...]) -> list[Finding]:
    folded = _fold(text)
    for name in names:
        n = _fold(name.strip())
        if len(n) < 2:
            continue
        if CJK.search(n):
            hit = n in folded
        else:
            hit = re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", folded) is not None
        if hit:
            return [Finding("name", name.strip())]
    return []


def _local(text: str, local: str, user: str) -> list[Finding]:
    if not local:
        return []
    folded_local = _fold(local)
    folded_user = _fold(user)
    for m in FILE_NAME.finditer(text):
        name = _fold(m.group(0))
        if len(name) > 6 and name in folded_local and name not in folded_user:
            return [Finding("file_name", m.group(0)[:SAMPLE])]
    phrase = _overlap(text, local, user)
    return [Finding("local_text", phrase[:SAMPLE])] if phrase else []


def _dedupe(found: list[Finding]) -> list[Finding]:
    seen: set[str] = set()
    out = []
    for f in found:
        if f.kind not in seen:
            seen.add(f.kind)
            out.append(f)
    return out


def scan_text(text: str, *, names: tuple[str, ...] = (), home: str = "", local: str = "",
              user: str = "") -> list[Finding]:
    """검색어 같은 글. local: 이 컴퓨터의 도구가 돌려준 글, user: 연구원님이 쓴 글."""
    return _dedupe(_contacts(text) + _secrets(text, True) + _paths(text, home) + _names(text, names)
                   + _local(text, local, user))


def scan_url(url: str, *, names: tuple[str, ...] = (), home: str = "", local: str = "",
             user: str = "") -> list[Finding]:
    """읽을 주소. 연락처·특정 비밀 모양은 주소 전체, 나머지는 질의 부분(? 뒤, # 뒤)만."""
    raw = url.strip()
    try:
        parts = urlsplit(raw if "://" in raw else "https://" + raw)
        query = unquote_plus(" ".join(p for p in (parts.query, parts.fragment) if p))
    except ValueError:
        query = ""
    whole = unquote_plus(raw)
    found = _contacts(whole) + _secrets(whole, False)
    if query:
        found += _secrets(query, True) + _paths(query, home) + _names(query, names) + _local(query, local, user)
    return _dedupe(found)

"""영어로 통일하기 (M2.5, 연구원님 결정): 안에서는 영어, 밖으로 나갈 때만 번역.

- 모델은 영어로 생각하고(thought), 영어로 도구를 쓰고, 영어로 답한다. 도구 결과와 안내도 영어다
- Glossary: 파일·폴더 이름 속 영어가 아닌 낱말을 영어 검색 키워드로 옮긴다 (학위논문 → thesis, dissertation, paper).
  찾기는 검색어와 이름을 영어끼리 비교한다. 손으로 만든 낱말 표가 없으니 어떤 낱말이든, 어떤 언어든 된다.
  한 번 옮긴 낱말은 GLADOS_HOME/cache/names-en.json에 남겨 다시 쓴다
- Translator: 모델이 연구원님의 언어가 아닌 말로 답했을 때만 화면에 내기 직전에 옮긴다 (M8부터는 연구원님의 언어로
  바로 쓰므로 대비책). 실패하면 None을 돌려주고, 부르는 쪽이 쓴 그대로 보여 준다

둘 다 같은 모델을 같은 설정으로 부른다 (num_ctx가 다르면 Ollama가 모델을 다시 올린다).
언어 판단은 글자 종류로 한다 (한글·영문·한자). 낱말 목록에 기대지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Protocol

from glados.core.textfix import SURROGATE

HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
LATIN = re.compile(r"[A-Za-z]")
HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")            # 한자 (한국어 답에 섞이면 안 됨)
# 경로·파일 이름은 언어를 셀 때 뺀다 ("Found 학위논문.pdf"는 영어 글)
PATHLIKE = re.compile(r"\S*[\\/]\S*|\S+\.[A-Za-z0-9]{1,5}\b")

LANGUAGES = {"ko": "Korean", "en": "English"}                             # 답을 옮길 수 있는 언어
STYLE = {
    "ko": ('Use formal polite Korean (합니다체: sentences end in ~습니다/~입니다). Translate "Researcher" as 연구원님. '
           "Never use Chinese characters (Hanja) or Japanese."),
    "en": "Use plain, precise English.",
}


class ChatModel(Protocol):
    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any], validate: Any,
                  retries: int = 2, temperature: float = 0.2) -> tuple[Any, str]: ...


# ---------------------------------------------------------------------- 언어 판단 (글자 종류로)
def is_foreign(token: str) -> bool:
    """영어 글자(ASCII)가 아닌 글자가 든 낱말인가 (영어로 옮겨야 하는 낱말)."""
    return any(not ch.isascii() and unicodedata.category(ch)[0] in "LM" for ch in token)


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _words(text: str) -> tuple[int, int, int]:
    """(한글이 든 낱말 수, 영문만 든 낱말 수, 그 밖의 글자(한자·가나 등)가 든 낱말 수).
    경로·파일 이름은 빼고 센다."""
    hangul = latin = other = 0
    for word in PATHLIKE.sub(" ", text).split():
        if HANGUL.search(word):
            hangul += 1
        elif is_foreign(word):
            other += 1
        elif LATIN.search(word):
            latin += 1
    return hangul, latin, other


def reply_language(request: str, default: str = "ko") -> str:
    """연구원님이 쓴 언어: 한글이 있으면 "ko", 한글이 없고 영어 낱말이 있으면 "en", 알 수 없으면 default."""
    hangul, latin, _ = _words(request)
    if hangul:
        return "ko"
    if latin and len(LATIN.findall(PATHLIKE.sub(" ", request))) >= 3:
        return "en"
    return default if default in LANGUAGES else "ko"


def in_language(text: str, lang: str) -> bool:
    """글이 그 언어로 쓰였는가. 한국어 글의 영어 전문 용어, 영어 글 속 한국어 제목은 봐준다 (낱말 수로 비교).
    Qwen이 가끔 통째로 중국어로 쓰는 일도 잡는다 (한자 낱말은 한국어도 영어도 아님)."""
    hangul, latin, other = _words(text)
    if lang == "ko":
        return hangul >= 1 and hangul >= latin and other == 0 and not HAN.search(text)
    if lang == "en":
        return hangul + latin + other == 0 or latin > hangul + other      # 글자가 없으면(숫자만) 어느 언어든 맞음
    return True


# ---------------------------------------------------------------------- 이름을 낱말로
def _script(ch: str) -> str:
    """글자의 문자 체계 (latin, hangul, cjk, hiragana, ...). 글자가 아니면 ""."""
    cat = unicodedata.category(ch)
    if cat[0] == "M":
        return "mark"                                      # 앞 글자에 붙는 표시: 앞 낱말을 이어 감
    if cat[0] != "L":
        return ""
    if ch.isascii():
        return "latin"
    return unicodedata.name(ch, "OTHER").split(" ")[0].lower()


def split_name(text: str) -> tuple[list[str], list[str]]:
    """이름·경로를 (영어 낱말들, 영어가 아닌 낱말들)로. 문자 체계가 바뀌는 곳과 숫자·기호에서 끊는다.
    ASCII 글자만 든 라틴 낱말은 영어로 치고, 그 밖(한글, 한자, 가나, 움라우트가 든 낱말 등)은 옮길 낱말이다.

    "2025_외주개발_계약서 final.pdf" -> (["final", "pdf"], ["외주개발", "계약서"])
    """
    english: list[str] = []
    foreign: list[str] = []
    cur: list[str] = []
    kind = ""

    def flush() -> None:
        if cur:
            word = "".join(cur).casefold()
            if is_foreign(word):
                foreign.append(word)
            elif len(word) >= 2:
                english.append(word)
            cur.clear()

    for ch in unicodedata.normalize("NFKC", text):          # 전각 글자·합자도 보통 글자로 (Ｒｅｐｏｒｔ -> Report)
        k = _script(ch)
        if k == "mark" and cur:
            cur.append(ch)
            continue
        if k == "mark":
            k = ""
        if k != kind:
            flush()
            kind = k
        if k:
            cur.append(ch)
    flush()
    return english, foreign


def stem(word: str) -> str:
    """영어 낱말의 아주 단순한 원형 (papers -> paper, studies -> study). 양쪽에 똑같이 적용해 비교한다."""
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


# ---------------------------------------------------------------------- 낱말 -> 영어 키워드
GLOSS_SYSTEM = """\
You turn words taken from file and folder names into English search keywords.
For each word give 2 to 5 lowercase English keywords: the literal meaning first, then common synonyms or broader \
words a person might type when looking for that file.
Rules: English only. Prefer single words. Romanize personal and brand names instead of translating them. No explanations.
Examples:
학위논문 -> thesis, dissertation, paper
계약서 -> contract, agreement
회의록 -> minutes, meeting, notes
請求書 -> invoice, bill
Rechnung -> invoice, bill
Reply as JSON: {"items": [{"i": <number>, "keywords": ["...", "..."]}]} with one item per input line."""

GLOSS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "keywords": {"type": "array", "items": {"type": "string"}}},
                "required": ["i", "keywords"],
            },
        },
    },
    "required": ["items"],
}
KEYWORD = re.compile(r"[a-z0-9]+")
MAX_KEYWORDS = 8


def _clean_keywords(raw: Any) -> tuple[str, ...]:
    words: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        for w in KEYWORD.findall(str(item).lower()):
            if len(w) >= 2 and w not in words:
                words.append(w)
    return tuple(words[:MAX_KEYWORDS])


class Glossary:
    """영어가 아닌 낱말 -> 영어 키워드. 캐시에 없으면 모델에게 묻고, 답을 파일에 남긴다."""

    def __init__(self, llm: ChatModel | None, cache_path: Path | None = None,
                 batch: int = 25, max_new: int = 75):
        self.llm = llm
        self.cache_path = cache_path
        self.batch = batch
        self.max_new = max_new            # 한 번 부를 때 새로 옮길 낱말 수 한도 (첫 검색이 너무 느려지지 않게)
        self._lock = threading.Lock()
        self._words: dict[str, tuple[str, ...]] = {}
        self._load()

    # -- 캐시
    def _load(self) -> None:
        if self.cache_path is None:
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._words = {str(k): _clean_keywords(v) for k, v in data.get("words", {}).items()}
        except (OSError, ValueError, AttributeError):
            self._words = {}

    def _save(self) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            body = {"version": 1, "words": {k: list(v) for k, v in sorted(self._words.items())}}
            tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except (OSError, ValueError):
            pass                           # 캐시를 못 써도 찾기는 계속한다

    def known(self, token: str) -> tuple[str, ...] | None:
        return self._words.get(nfc(token).casefold())

    # -- 옮기기
    def english(self, tokens: Iterable[str], deadline: float | None = None) -> dict[str, tuple[str, ...]]:
        """낱말들의 영어 키워드. 캐시에 없는 것은 한도(max_new)와 시간(deadline) 안에서 모델에게 묻는다.
        못 옮긴 낱말은 결과에 없다."""
        wanted = list(dict.fromkeys(nfc(tok).casefold() for tok in tokens
                                    if tok and is_foreign(tok) and not SURROGATE.search(tok)))   # 깨진 글자 (M6.2)
        with self._lock:
            out = {tok: self._words[tok] for tok in wanted if tok in self._words}
            missing = [tok for tok in wanted if tok not in self._words]
            if not missing or self.llm is None:
                return out
            asked = 0
            learned = False
            for i in range(0, len(missing), self.batch):
                if asked >= self.max_new or (deadline is not None and time.monotonic() > deadline):
                    break
                chunk = missing[i: i + min(self.batch, self.max_new - asked)]
                asked += len(chunk)
                got = self._ask(chunk)
                if got is None:            # 모델을 못 부름: 이번에는 그만
                    break
                for tok, words in got.items():
                    self._words[tok] = words
                    out[tok] = words
                    learned = True
            if learned:
                self._save()
            return out

    def _ask(self, chunk: list[str]) -> dict[str, tuple[str, ...]] | None:
        assert self.llm is not None
        lines = "\n".join(f"{i}: {tok}" for i, tok in enumerate(chunk))
        messages = [{"role": "system", "content": GLOSS_SYSTEM}, {"role": "user", "content": lines}]

        def validate(data: dict[str, Any]) -> dict[str, tuple[str, ...]]:
            found: dict[str, tuple[str, ...]] = {}
            for item in data.get("items", []):
                try:
                    i = int(item["i"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= i < len(chunk):
                    found[chunk[i]] = _clean_keywords(item.get("keywords"))
            return found

        try:
            found, _ = self.llm.chat_json(messages, GLOSS_SCHEMA, validate, retries=1, temperature=0.0)
        except KeyboardInterrupt:
            raise
        except Exception:
            return None
        return found


# ---------------------------------------------------------------------- 답을 연구원님의 언어로
TRANSLATE_SYSTEM = """\
You are the translator for GLaDOS, a desktop assistant. Translate the values of the JSON object into {language}.
Rules:
- Translate faithfully. Do not add, remove, answer, or explain anything.
- Keep file names, paths, URLs, numbers, code, and commands exactly as written.
- Keep technical terms (model, metric, and technology names such as BLEU-1, baseline, diffusion, fine-tuning) in English.{names}
- {style}
- An empty value stays empty.
Reply as JSON with the same keys: {{"fact": "...", "wit": "..."}}"""

TRANSLATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"fact": {"type": "string"}, "wit": {"type": "string"}},
    "required": ["fact", "wit"],
}
RETRY_NOTE = "Your last reply was not written in {language} only. Translate again into {language}."
NAMES_RULE = "\n- These are names (servers), not words: {names}. Copy them exactly as written; never translate them."
NAMES_NOTE = "Your last reply changed the names {names}. Translate again and copy them exactly as written."


class Translator:
    """모델의 답(fact, wit)을 다른 언어로. 실패하면 None (부르는 쪽이 원문을 그대로 쓴다).
    keep: 낱말처럼 보여도 옮기면 안 되는 이름 (서버 별칭 "temp"가 "임시 서버"가 되지 않게).
    답에 든 이름이 번역에서 빠지면 한 번 다시, 그래도 빠지면 None (틀린 이름보다 영어 원문이 낫다)."""

    def __init__(self, llm: ChatModel, keep: Iterable[str] = ()):
        self.llm = llm
        self.keep = [k for k in keep if k.strip()]

    def translate(self, fact: str, wit: str, lang: str) -> tuple[str, str] | None:
        if lang not in LANGUAGES:
            return None
        language = LANGUAGES[lang]
        source = f"{fact} {wit}"
        names = [n for n in self.keep if re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w-])", source)]
        rule = NAMES_RULE.format(names=", ".join(names)) if names else ""
        messages = [
            {"role": "system", "content": TRANSLATE_SYSTEM.format(language=language, style=STYLE[lang], names=rule)},
            {"role": "user", "content": json.dumps({"fact": fact, "wit": wit}, ensure_ascii=False)},
        ]

        def validate(data: dict[str, Any]) -> tuple[str, str]:
            return str(data["fact"]).strip(), str(data["wit"]).strip()

        for attempt in range(2):
            try:
                (new_fact, new_wit), raw = self.llm.chat_json(messages, TRANSLATE_SCHEMA, validate, retries=1, temperature=0.0)
            except KeyboardInterrupt:
                raise
            except Exception:
                return None
            if not wit.strip():
                new_wit = ""
            said = f"{new_fact} {new_wit}"
            fluent = bool(new_fact) and in_language(said, lang)
            lost = [n for n in names if n not in said]
            if fluent and not lost:
                return new_fact, new_wit
            if attempt == 0:               # 다른 언어가 섞였거나 이름이 바뀌었으면 한 번만 다시
                note = RETRY_NOTE.format(language=language) if not fluent else NAMES_NOTE.format(names=", ".join(lost))
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": note},
                ]
        return None

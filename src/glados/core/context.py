"""대화 맥락: 지난 작업을 기억하고, 모델의 맥락 길이 안에 맞춘다.

작업 하나가 한 턴이다: 연구원님 말, 모델의 걸음(JSON), 도구 결과, 마지막 답.
다음 작업에는 지난 턴을 있는 그대로 넣는다 (찾은 경로, 읽은 내용까지).
맥락 길이를 넘으면 오래된 턴부터 줄인다.

    전체 → 줄임(도구 결과는 앞부분만) → 요약(답과 작업 메모만) → 뺌

길이는 글자 수로 어림한다. Qwen 토크나이저로 재 보면 토큰 하나가 한국어 문서는 약 1.5자,
프롬프트는 1.75자, JSON은 2.6자, 영어는 3.5자다. 1.3자로 잡으면 넉넉히 안전하다.
넘치면 Ollama가 앞 대화를 몰래 잘라 버리므로, 보내기 전에 여기서 먼저 줄인다.

이 파일은 화면과 상관없다. 터미널 화면, 나중의 glados serve가 똑같이 쓴다.
기억 속 글(걸음, 도구 결과, 답, 작업 메모)은 영어다 (엔진 안쪽은 영어로 통일, M2.5).
연구원님이 쓴 말만 쓴 그대로 둔다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

CHARS_PER_TOKEN = 1.3        # 토큰 하나를 몇 글자로 잡을지 (실측 1.5~3.5, 작게 잡아 안전하게)
OUTPUT_RESERVE_TOKENS = 2048  # 모델의 답(JSON, 새 파일 내용 포함) 몫
MESSAGE_OVERHEAD = 10        # 메시지마다 붙는 역할 표시 (<|im_start|>user ... <|im_end|>)
TASK_RESERVE = 4000          # 작업을 시작할 때 이번 작업 몫으로 남겨 둘 글자 수. 모자라면 fit()이 줄인다
CLIP_OBS = 800               # 줄일 때 남길 도구 결과 앞부분
MEMO_LIMIT = 900             # 작업 메모 한 개의 최대 길이 (줄 단위로 자름)
MEMO_HITS = 5                # 찾기 결과에서 메모에 남길 파일 수

FULL, CLIPPED, BRIEF, DROPPED = "full", "clipped", "brief", "dropped"   # 화면 이름은 i18n "level.*"

OBS_PREFIX = "[result"         # 도구 결과 메시지: "[result: fs.read] ..."
MEMO_MARK = "[work notes]"     # 앞 작업에서 쓴 도구와 결과 요약
STOP_MARK = "[stopped]"        # 멈춘 작업의 이유
CHECK_MARK = "[check]"         # 안전망이 되돌릴 때의 안내

Message = dict[str, str]


def size(messages: list[Message]) -> int:
    return sum(len(m["content"]) + MESSAGE_OVERHEAD for m in messages)


def limit_chars(num_ctx: int) -> int:
    """맥락 길이(토큰)에서 답 몫을 빼고 글자 수로."""
    return int(max(num_ctx - OUTPUT_RESERVE_TOKENS, 1024) * CHARS_PER_TOKEN)


def observation(tool: str, text: str) -> Message:
    """도구 결과를 모델에게 돌려주는 메시지."""
    return {"role": "user", "content": f"{OBS_PREFIX}: {tool}] {text}"}


def is_observation(m: Message) -> bool:
    return m["role"] == "user" and m["content"].startswith(OBS_PREFIX)


def is_engine_note(m: Message) -> bool:
    """연구원님 말이 아니라 엔진이 넣은 메시지인가 (도구 결과, 멈춤, 되돌림 안내)."""
    return m["role"] == "user" and m["content"].startswith((OBS_PREFIX, STOP_MARK, CHECK_MARK))


def clip_observation(content: str, keep: int = CLIP_OBS) -> str:
    if len(content) <= keep:
        return content
    return content[:keep] + f"\n...(clipped to save context: {len(content)} chars in total. Read it again if you need the details)"


# ---------------------------------------------------------------------- 작업 메모
def _brief_args(args: dict[str, Any], limit: int = 80) -> str:
    parts = []
    for k, v in args.items():
        text = str(v).replace("\n", " ")
        parts.append(f"{k}={text[:40] + '...' if len(text) > 40 else text}")
    line = " ".join(parts)
    return line if len(line) <= limit else line[:limit] + "..."


def work_memo(trail: list[dict[str, Any]]) -> str:
    """무엇을 찾고 읽고 만들었는지. 요약으로 줄인 턴에서도 경로를 잃지 않게 한다.

    경로가 잘리지 않게 줄 단위로 자른다. 읽은 내용은 남기지 않는다.
    """
    lines: list[str] = []
    for t in trail:
        out = [ln for ln in str(t.get("head", "")).splitlines() if ln.strip()]
        if not out:
            continue
        lines.append(f"- {t.get('tool')} {_brief_args(t.get('args', {}))} → {out[0][:200]}")
        if t.get("tool") in ("fs.search", "web.search"):                # 찾은 파일·페이지 (경로, 주소)
            lines += [f"  {ln}" for ln in out[1:] if ln.startswith("- ")][:MEMO_HITS]
    if not lines:
        return ""
    kept: list[str] = []
    used = len(MEMO_MARK)
    for i, ln in enumerate(lines):
        if used + len(ln) + 1 > MEMO_LIMIT:
            kept.append(f"- ...({len(lines) - i} more lines omitted)")
            break
        kept.append(ln)
        used += len(ln) + 1
    return MEMO_MARK + "\n" + "\n".join(kept)


# ---------------------------------------------------------------------- 턴
@dataclass
class Turn:
    request: str
    messages: list[Message] = field(default_factory=list)   # 이 작업의 걸음과 관찰 (되돌린 답은 뺌)
    answer: str = ""                                        # 답 + 출처 (영어, 없으면 빈 글)
    stop_note: str = ""                                     # 멈췄으면 "(stopped: ...)"
    memo: str = ""                                          # 작업 메모

    @classmethod
    def from_result(cls, request: str, result: Any) -> "Turn":
        """TaskResult에서 (순환 참조를 피하려고 타입은 느슨하게)."""
        answer = ""
        said = getattr(result, "internal", None) or result.answer      # 기억에는 모델이 쓴 영어 답 (화면에 낸 번역 말고)
        if said is not None:
            answer = said.fact + (f" {said.wit}" if said.wit else "")
            if result.sources:
                answer += "\nSources: " + ", ".join(result.sources)
        note = f"(stopped: {result.message})" if result.status != "done" else ""
        return cls(request, list(result.transcript), answer, note, work_memo(result.trail))

    def closing(self) -> str:
        return "\n".join(p for p in (self.answer, self.stop_note) if p)

    def brief(self) -> str:
        return "\n".join(p for p in (self.answer, self.stop_note, self.memo) if p) or "(no answer)"

    def render(self, level: str) -> list[Message]:
        head: list[Message] = [{"role": "user", "content": self.request}]
        if level == BRIEF or not self.messages:
            return head + [{"role": "assistant", "content": self.brief()}]
        msgs = self.messages
        if level == CLIPPED:
            msgs = [{**m, "content": clip_observation(m["content"])} if is_observation(m) else m for m in msgs]
        if msgs[-1]["role"] != "assistant":                 # 멈춘 작업: 마지막 보고로 마무리
            msgs = msgs + [{"role": "assistant", "content": self.closing() or self.brief()}]
        return head + list(msgs)


# ---------------------------------------------------------------------- 대화
class Conversation:
    """한 세션의 대화. 지난 턴을 모두 갖고 있다가, 보낼 때 길이에 맞게 고른다."""

    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self.last_levels: list[str] = []      # 마지막으로 보낸 맥락의 턴별 단계 (오래된 것부터)
        self.last_budget: int | None = None

    def add(self, request: str, result: Any) -> Turn:
        turn = Turn.from_result(request, result)
        self.turns.append(turn)
        return turn

    def add_direct(self, command: str, output: str) -> Turn:
        """연구원님이 채팅에서 직접 실행한 '!명령'과 그 결과. 다음 질문("이 결과 설명해줘")이 볼 수 있게."""
        turn = Turn(
            f"!{command}",
            [observation("shell.run", output)],
            answer="(This is the output of a command the Researcher ran directly.)",
            memo=work_memo([{"tool": "shell.run", "args": {"command": command}, "head": output[:600]}]),
        )
        self.turns.append(turn)
        return turn

    def clear(self) -> None:
        self.turns.clear()
        self.last_levels = []
        self.last_budget = None

    def _choose(self, budget: int | None) -> list[tuple[str, list[Message]]]:
        """턴마다 (단계, 메시지). 최근 턴부터 담을 수 있는 가장 자세한 단계로 담고,
        하나라도 못 담으면 그보다 오래된 턴은 뺀다 (중간이 빈 대화가 되지 않게)."""
        if budget is None:
            return [(FULL, t.render(FULL)) for t in self.turns]
        chosen: list[tuple[str, list[Message]]] = []
        used = 0
        for turn in reversed(self.turns):
            for level in (FULL, CLIPPED, BRIEF):
                msgs = turn.render(level)
                need = size(msgs)
                if used + need <= budget:
                    chosen.append((level, msgs))
                    used += need
                    break
            else:
                break
        chosen.reverse()
        return [(DROPPED, [])] * (len(self.turns) - len(chosen)) + chosen

    def render(self, budget: int | None) -> list[Message]:
        """보낼 지난 대화. budget(글자 수)이 없으면 모두 그대로."""
        chosen = self._choose(budget)
        self.last_levels = [lv for lv, _ in chosen]
        self.last_budget = budget
        return [m for _, msgs in chosen for m in msgs]

    def preview(self, budget: int | None) -> list[tuple[Turn, str, int]]:
        """다음 작업에 넣을 모습 (턴, 단계, 글자 수). 기록(last_*)은 바꾸지 않는다. /context 화면용."""
        return [(turn, lv, size(msgs)) for turn, (lv, msgs) in zip(self.turns, self._choose(budget))]


# ---------------------------------------------------------------------- 보내기 전에 맞추기
def fit(
    system: Message,
    history: list[Message],
    task: list[Message],
    limit: int,
    rerender: Callable[[int], list[Message]] | None = None,
) -> tuple[list[Message], list[Message]]:
    """전체가 limit을 넘으면 줄인다: 지난 대화를 더 줄이고, 그래도 넘으면 이번 작업의 오래된 도구 결과를 줄인다.

    이번 작업의 마지막 도구 결과와 연구원님 말은 줄이지 않는다.
    """
    def total(h: list[Message], t: list[Message]) -> int:
        return size([system]) + size(h) + size(t)

    if total(history, task) <= limit:
        return history, task
    room = max(0, limit - size([system]) - size(task))
    history = rerender(room) if rerender is not None else []
    if size(history) > room:
        history = []
    if total(history, task) <= limit:
        return history, task
    task = list(task)
    observations = [i for i, m in enumerate(task) if is_observation(m)]
    for i in observations[:-1]:
        task[i] = {**task[i], "content": clip_observation(task[i]["content"])}
        if total(history, task) <= limit:
            break
    return history, task


# ---------------------------------------------------------------------- 근거
_SPLIT = re.compile(r"[\\/]")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _name(path: str) -> str:
    return _fold(_SPLIT.split(path.strip().rstrip("\\/"))[-1])


def evidence_text(messages: list[Message]) -> str:
    """도구가 실제로 돌려준 글: 관찰 메시지와 작업 메모 (모델이 쓴 답은 빼고)."""
    parts = []
    for m in messages:
        if is_observation(m):
            parts.append(m["content"])
        elif m["role"] == "assistant" and MEMO_MARK in m["content"]:
            parts.append(m["content"].split(MEMO_MARK, 1)[1])
    return _fold("\n".join(parts))


def grounded(sources: list[str], messages: list[Message]) -> bool:
    """답의 출처가 모두 앞에서 도구로 확인한 자료에 있는가 (파일 이름으로 비교)."""
    names = [_name(s) for s in sources if s.strip()]
    if not names:
        return False
    seen = evidence_text(messages)
    return all(n and n in seen for n in names)


URL = re.compile(r"https?://[^\s<>\"'|)\]]+", re.IGNORECASE)


def _url_key(url: str) -> str:
    """주소 비교용: 앞의 http(s)://·www.·끝의 /·? 뒤·# 뒤를 뺀 호스트 + 경로 (소문자)."""
    try:
        parts = urlsplit(url.strip().rstrip(".,;"))
    except ValueError:
        return ""
    host = (parts.hostname or "").casefold().removeprefix("www.")
    return host + parts.path.rstrip("/").casefold() if host else ""


def unseen_urls(sources: list[str], messages: list[Message]) -> list[str]:
    """출처 가운데 도구 결과(검색 결과, 읽은 페이지)에 나온 적 없는 주소. 작은 모델이 지어낸 주소를 잡는다.
    같은 주소의 판 차이(arxiv …v2)와 http/https·www·끝의 / 차이는 같은 것으로 본다."""
    cited = [s for s in sources if s.strip().lower().startswith(("http://", "https://"))]
    if not cited:
        return []
    seen = {k for k in (_url_key(u) for u in URL.findall(evidence_text(messages))) if k}
    out = []
    for url in cited:
        key = _url_key(url)
        if not key:
            out.append(url)
            continue
        ok = any(
            key == s
            or ("/" in s and key.startswith(s) and len(key) - len(s) <= 3)
            or ("/" in key and s.startswith(key) and len(s) - len(key) <= 3)
            for s in seen
        )
        if not ok:
            out.append(url)
    return out


def local_texts(messages: list[Message]) -> tuple[str, str]:
    """(이 컴퓨터의 도구가 돌려준 글, 연구원님이 쓴 글). 웹으로 나가는 글에 파일 내용이 들었는지 볼 때 쓴다
    (safety/privacy.py). 웹 도구의 결과는 뺀다 (원래 밖에 있던 글)."""
    local: list[str] = []
    user: list[str] = []
    web_obs = (f"{OBS_PREFIX}: web.", f"{OBS_PREFIX}: papers.")     # 원래 밖에 있던 글
    for m in messages:
        content = m["content"]
        if is_observation(m):
            if not content.startswith(web_obs):
                local.append(content)
        elif m["role"] == "user" and not is_engine_note(m):
            user.append(content)
        elif m["role"] == "assistant" and MEMO_MARK in content:
            keep = True
            for line in content.split(MEMO_MARK, 1)[1].splitlines():
                if line.startswith("- "):
                    keep = not line.startswith(("- web.", "- papers."))
                if keep:
                    local.append(line)
    return "\n".join(local), "\n".join(user)

"""영어로 통일한 루프의 안전망과 번역 시험 (실제 qwen3.5:4b 기록에서).

- 모델은 영어로 생각하고 답한다. 영어가 아니면 한 번 되돌린다
- 하겠다고만 하고 끝낸 답은 M9.3부터 답 점검이 본다 (test_loop)
- 답은 연구원님이 쓴 언어로 번역해 화면에 낸다. 기억에는 영어 답이 남는다
"""

from __future__ import annotations

import io
import json

from conftest import FakeLLM, act, answer, base_layers, make_ctx
from glados import cli
from glados.config import LoopLimits, ensure_home, load_config
from glados.core import events as ev
from glados.core.context import Conversation
from glados.core.loop import NUDGE_ENGLISH, run_task
from glados.core.translate import Translator
from glados.hands import Tool, ToolRegistry
from glados.safety.policy import Policy


def translation(fact: str, wit: str = "") -> dict:
    """가짜 모델이 번역 호출에 돌려줄 JSON."""
    return {"fact": fact, "wit": wit}






# ---------------------------------------------------------------- 루프와 함께
def run(llm, request, tools=None, reply_lang="en", memory=None):
    tools = tools if tools is not None else ToolRegistry([Tool("fs.search", "find", run=lambda a: "0 found")])
    events = []
    result = run_task(
        request, llm=llm, policy=Policy(base_layers()),
        ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
        limits=LoopLimits(), system_prompt="test", emit=events.append,
        translator=Translator(llm), reply_lang=reply_lang, memory=memory,
    )
    return result, events


def test_non_english_answer_is_sent_back_once():
    """M8.3 연구원님 "그냥 전부 영어로": 영어가 아닌 답은 한 번 되돌린다. 화면에는 번역해서."""
    llm = FakeLLM([act("fs.search", query="paper"), answer("다운로드에 논문이 없습니다."),
                   answer("There is no paper in Downloads."), translation("다운로드에 논문이 없습니다.")])
    result, events = run(llm, "다운로드에서 논문 찾아줘", reply_lang="ko")
    assert result.answer.fact == "다운로드에 논문이 없습니다." and result.internal.fact == "There is no paper in Downloads."
    assert [e.data["key"] for e in events if e.kind == ev.GUARD] == ["guard.english"]
    assert llm.calls[2][0][-1]["content"] == NUDGE_ENGLISH
    assert not any("다운로드에 논문이 없습니다" in m["content"] for m in result.transcript)   # 되돌린 답은 기억 안 함




def test_answer_in_another_language_is_translated_as_a_fallback():
    """연구원님의 언어로 쓰지 않은 답만 옮긴다 (M8). 기억에는 모델이 쓴 그대로."""
    llm = FakeLLM([
        act("fs.search", query="paper"),
        answer("Found one paper: 학위논문.pdf.", "Small sample. Still a sample.", ["/d/학위논문.pdf"]),
        translation("논문 1개를 찾았습니다: 학위논문.pdf."),
    ])
    memory = Conversation()
    result, events = run(llm, "다운로드에서 논문 찾아줘", reply_lang="ko", memory=memory)
    assert result.answer.fact == "논문 1개를 찾았습니다: 학위논문.pdf."
    assert result.answer.wit == "Small sample. Still a sample."                    # 위트는 영어 그대로 (M6.3)
    assert result.internal.fact == "Found one paper: 학위논문.pdf."
    shown = next(e for e in events if e.kind == ev.ANSWER)
    assert shown.data["translated"] and shown.data["original"]["fact"].startswith("Found one paper")
    system, user = llm.calls[2][0]                                                 # 번역 호출
    assert "Korean" in system["content"] and "합니다체" in system["content"]
    assert json.loads(user["content"]) == {"fact": "Found one paper: 학위논문.pdf.", "wit": ""}     # 사실만 옮긴다
    turn = memory.add("다운로드에서 논문 찾아줘", result)
    assert "Found one paper" in turn.answer and "논문 1개" not in turn.answer         # 기억에는 영어


def test_translation_retry_and_fallback():
    llm = FakeLLM([
        answer("Hello, Researcher."),
        translation("Hello, Researcher."),                                           # 번역이 안 됨 -> 한 번 더
        translation("안녕하세요, 연구원님."),
    ])
    result, _ = run(llm, "안녕", tools=ToolRegistry(), reply_lang="ko")
    assert result.answer.fact == "안녕하세요, 연구원님."
    llm = FakeLLM([answer("Hello, Researcher."), translation("Hello."), translation("Hi.")])
    result, events = run(llm, "안녕", tools=ToolRegistry(), reply_lang="ko")
    shown = next(e for e in events if e.kind == ev.ANSWER)
    assert result.answer.fact == "Hello, Researcher." and shown.data["untranslated"]   # 끝내 못 하면 영어 그대로


def test_translation_keeps_server_names():
    """서버 별칭은 낱말이 아니라 이름: "temp"가 "임시 서버"가 되면 다시, 끝내 바뀌면 영어 원문."""
    fact = "Cannot log in to temp with a key. Add your key with ssh-add."
    llm = FakeLLM([translation("임시 서버에 키로 로그인할 수 없습니다."), translation("temp에 키로 로그인할 수 없습니다.")])
    tr = Translator(llm, keep=["temp", "server-a"])
    assert tr.translate(fact, "", "ko") == ("temp에 키로 로그인할 수 없습니다.", "")
    system = llm.calls[0][0][0]["content"]
    assert "temp" in system and "server-a" not in system                         # 답에 나온 이름만 알려 줌
    assert "temp" in llm.calls[1][0][-1]["content"]                                # 다시 할 때 바뀐 이름을 짚음
    llm = FakeLLM([translation("임시 서버에 로그인할 수 없습니다."), translation("임시 서버입니다.")])
    assert Translator(llm, keep=["temp"]).translate(fact, "", "ko") is None
    llm = FakeLLM([translation("파일을 찾지 못했습니다.")])                          # 이름이 없으면 그대로
    assert Translator(llm, keep=["temp"]).translate("No files in temperature_logs.", "", "ko") == ("파일을 찾지 못했습니다.", "")


def test_translation_rejects_chinese_characters():
    llm = FakeLLM([answer("No files found."), translation("파일을放宽 찾지 못했습니다."), translation("파일을 찾지 못했습니다.")])
    result, _ = run(llm, "파일 있어?", tools=ToolRegistry(), reply_lang="ko")
    assert result.answer.fact == "파일을 찾지 못했습니다."


def test_english_request_needs_no_translation():
    llm = FakeLLM([answer("Hello, Researcher.")])
    result, _ = run(llm, "Hello", tools=ToolRegistry(), reply_lang="en")
    assert result.answer.fact == "Hello, Researcher." and len(llm.calls) == 1


def test_stop_report_is_translated():
    tools = ToolRegistry([Tool("fs.read", "read", run=lambda a: "same")])
    llm = FakeLLM([act("fs.read", path="/a")] * 3 + [answer("I read /a once and stopped."), translation("/a를 한 번 읽고 멈췄습니다.")])
    result, events = run(llm, "/a 읽어줘", tools, reply_lang="ko")
    assert result.status == "stopped" and result.answer.fact == "/a를 한 번 읽고 멈췄습니다."
    assert result.internal.fact == "I read /a once and stopped."
    assert result.transcript[-2]["content"].startswith("[stopped] Stopped: the same call (fs.read) repeated 3 times.")


# ---------------------------------------------------------------- 화면
def make_app(tmp_path, steps, language="ko"):
    ensure_home(tmp_path)
    cfg = load_config(tmp_path)
    cfg.language = language
    out = io.StringIO()
    a = cli.App(cfg, cli.Screen(ansi=False, out=out), llm=FakeLLM(steps))
    return a, out


def test_screen_shows_translation_and_guards(tmp_path):
    a, out = make_app(tmp_path, [
        act("fs.search", query="paper"), answer("파일이 없습니다."), answer("No paper found."),
        translation("논문을 찾지 못했습니다."),
    ])
    a.tools = ToolRegistry([Tool("fs.search", "find", run=lambda args: "0 found")])
    a.ask_task("다운로드에서 논문 찾아줘")
    text = out.getvalue()
    assert "... 모델이 영어로 쓰지 않아 다시 쓰게 했습니다" in text and "논문을 찾지 못했습니다." in text
    assert "파일이 없습니다" not in text and "No paper found." not in text
    a.close()


def test_screen_language_and_reply_language_are_separate(tmp_path):
    a, out = make_app(tmp_path, [answer("Hello, Researcher."), translation("안녕하세요, 연구원님.")], language="en")
    a.tools = ToolRegistry()
    a.ask_task("안녕")                                                            # 영어 화면, 한국어 질문 -> 한국어 답
    assert "Working..." in out.getvalue() and "안녕하세요, 연구원님." in out.getvalue()
    a.close()


def test_untranslated_note_on_screen(tmp_path):
    a, out = make_app(tmp_path, [answer("Hello, Researcher."), translation("Hello."), translation("Hello.")])
    a.tools = ToolRegistry()
    a.ask_task("안녕")
    assert "Hello, Researcher." in out.getvalue() and "번역하지 못해 영어 그대로" in out.getvalue()
    a.close()


def test_sources_are_capped_on_screen(tmp_path):
    many = [f"/d/file{i}.md" for i in range(14)]
    a, out = make_app(tmp_path, [answer("There are 14.", "", many)], language="en")
    a.tools = ToolRegistry()
    a.ask_task("list them")
    assert "Sources: /d/file0.md, /d/file1.md, /d/file2.md (+11 more)" in out.getvalue()
    a.close()


def test_stubborn_non_english_answer_is_taken_as_written():
    """두 번째도 영어가 아니면 그대로 받는다 (옮기는 호출 없이). 낱말 안전망은 영어 답에만."""
    llm = FakeLLM([act("fs.search", query="x"), answer("조건을 넓혀 보겠습니다."), answer("다운로드에는 없습니다.")])
    result, events = run(llm, "논문 찾아줘", reply_lang="ko")
    assert result.answer.fact == "다운로드에는 없습니다." and len(llm.calls) == 3
    assert [e.data["key"] for e in events if e.kind == ev.GUARD] == ["guard.english"]


def test_english_request_answered_in_korean_is_sent_back():
    llm = FakeLLM([answer("안녕하세요, 연구원님."), answer("Hello, Researcher.")])
    result, _ = run(llm, "Hello there", tools=ToolRegistry(), reply_lang="en")
    assert result.answer.fact == "Hello, Researcher." and len(llm.calls) == 2






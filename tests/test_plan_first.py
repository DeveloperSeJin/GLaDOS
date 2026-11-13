"""계획 먼저 (M7.7) -> 대화 차례 (M8): 도구가 있으면 움직이기 전에 한 번 말하고 계획한다.

- M8: 그 호출이 GLaDOS의 첫 말이다. say는 연구원님의 언어로 바로 (번역 없이), 목표·계획·확인은 영어
- 일하다 도구를 6번 쓰면 돌아와 지금까지를 말하고 계속할지 묻는다 (중간 보고)

- 계획 호출: 시스템 메시지 끝에 이 컴퓨터의 지금 사실(situation) + 계획 지시. 언어 안내는 그래도 맨 끝
- 도구가 필요 없으면 그 답을 바로 낸다 (모델을 한 번만 부른다)
- 계획은 화면에 (연구원님의 언어로) 보여 주고 승인 없이 바로 진행한다. 이번 작업에만 넣고 기억에는 남기지 않는다
- 계획 답이 형식을 못 맞추면 예전처럼 첫 걸음에 계획을 받는다
"""

from __future__ import annotations

import io
from pathlib import Path

from conftest import FakeLLM, act, answer, base_layers, make_ctx, verdict
from glados import cli
from glados.config import LoopLimits, ensure_home, load_config
from glados.core import events as ev
from glados.core.llm import Cancelled, FormatError
from glados.core.loop import NUDGE_PLANNED, asks, run_task
from glados.core.prompts import reply_note
from glados.core.schema import plan_schema
from glados.hands import Tool, ToolRegistry
from glados.hands import index as ci
from glados.safety.policy import Policy

GOAL = "Find the Researcher's driver's license photo."
STEPS = ["Search all drives for images named license with fs.search.",
         "If none, search photo contents for 'driver license' with content=true.",
         "Look at the likely images with fs.inspect."]
CHECK = "The image shows a driver's license card; stop when one is found."
SAY = "I will look in Pictures and Google Drive (G:) first."


def planned(goal=GOAL, steps=STEPS, check=CHECK, say=SAY):
    return {"goal": goal, "next": "act", "plan": list(steps), "check": check, "say": say, "wit": "", "sources": []}


def said(text, next_="answer", goal="Greet the Researcher."):
    return {"goal": goal, "next": next_, "plan": [], "check": "", "say": text, "wit": "", "sources": []}


def finder(calls=None, text="0 found (scope: C:\\, G:\\)"):
    def _run(args):
        if calls is not None:
            calls.append(args)
        return text
    return Tool("fs.search", "find files", run=_run)


def run(llm, *, situation="", translator=None, reply_lang="en", final_note="", plan_first=True, cancelled=None,
        tools=None):
    tools = tools or ToolRegistry([finder()])
    events = []
    result = run_task(
        "운전면허증 사진 찾아줘", llm=llm, policy=Policy(base_layers()),
        ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools, limits=LoopLimits(),
        system_prompt="test", emit=events.append, translator=translator, reply_lang=reply_lang,
        final_note=final_note, plan_first=plan_first, situation=situation, cancelled=cancelled,
    )
    return result, events


class SayKorean:
    """영어로 쓴 말만 받는 가짜 번역."""

    def __init__(self, text="한국어로 옮긴 말"):
        self.calls = []
        self.text = text

    def translate(self, fact, wit, lang):
        self.calls.append((fact, lang))
        return (self.text, "") if lang == "ko" else None


def test_plans_before_acting():
    calls = []
    llm = FakeLLM([planned(), act("fs.search", query="license", kind="image"), answer("Not found.")])
    result, events = run(llm, situation="- fs.search with roots left out searches: C:\\, G:\\ (Google Drive)",
                         tools=ToolRegistry([finder(calls)]))
    assert result.status == "done" and result.steps == 2                 # 계획 호출은 걸음 수에 세지 않는다
    assert llm.calls[0][1] == plan_schema()
    system = llm.calls[0][0][0]["content"]
    assert "This is your turn in the conversation." in system and "G:\\ (Google Drive)" in system
    assert "never narrow to a place the Researcher did not name" in system
    assert "Before you act" not in llm.calls[1][0][0]["content"]          # 계획 지시는 계획 호출에만
    assert llm.calls[1][1]["required"] == ["progress", "thought", "action", "done"]   # 계획이 있으니 걸음은 보통 형식
    sent = llm.calls[1][0]
    assert sent[-2]["role"] == "assistant" and GOAL in sent[-2]["content"]  # 다음 걸음이 계획을 본다
    assert sent[-1]["content"] == NUDGE_PLANNED.format(goal=GOAL)
    assert "narrower" not in sent[-1]["content"]                           # 범위는 M9.3부터 답 점검이 본다
    plan_events = [e for e in events if e.kind == ev.PLAN]
    assert len(plan_events) == 1
    data = plan_events[0].data
    assert data["step"] == 0 and data["goal"] == GOAL and data["plan"] == STEPS and data["check"] == CHECK
    assert data["say"] == SAY and data["translated"] is False                # 번역기가 없으면 쓴 그대로
    assert [e.kind for e in events][:2] == [ev.START, ev.PLAN]           # 움직이기 전에 계획부터 보인다
    assert calls == [{"query": "license", "kind": "image"}]
    assert not any(GOAL in m["content"] for m in result.transcript)       # 기억에는 걸음들만
    assert not any(m["content"].startswith("[check] Plan noted.") for m in result.transcript)


def test_first_words_show_in_chat_and_the_plan_goes_to_the_process_panel():
    """M8: 대화에는 GLaDOS의 첫 말, 계획 단계(영어)는 맥락 패널에만 (TUI가 그린다)."""
    from glados.cli import event_lines

    llm = FakeLLM([planned(), act("fs.search", query="license"), answer("Not found.")])
    _, events = run(llm)
    plan = next(e for e in events if e.kind == ev.PLAN)
    assert event_lines(plan) == [("speaker", "GLaDOS"), ("plain", SAY)]
    llm = FakeLLM([planned(say=""), act("fs.search", query="license"), answer("Not found.")])
    _, events = run(llm)
    plan = next(e for e in events if e.kind == ev.PLAN)
    assert event_lines(plan)[0] == ("dim", "계획") and ("dim", f"  1. {STEPS[0]}") in event_lines(plan)   # 첫 말이 없으면 계획


def test_no_tools_needed_answers_from_the_plan_call():
    llm = FakeLLM([said("Hello, Researcher.")])
    result, events = run(llm)
    assert result.status == "done" and result.answer.fact == "Hello, Researcher." and result.steps == 1
    assert len(llm.calls) == 1 and not [e for e in events if e.kind == ev.PLAN]
    assert '"done":true' in result.transcript[-1]["content"].replace(" ", "")   # 기억에는 보통 걸음 모양


def test_direct_answer_is_still_checked_with_the_planning_message():
    """대화 차례가 도구 없이 바로 답해도 답 점검을 거친다 (M9.3). 그 차례의 시스템 메시지 그대로 뒤에 붙인다."""
    from glados.core.checker import Checker

    tools = ToolRegistry([finder()])
    llm = FakeLLM([said("There is no such file.", goal="Find the file."),
                   verdict(claims_supported="No search was done."),
                   act("fs.search", query="license"), answer("There is no license photo in the searched drives."),
                   verdict()])
    events = []
    result = run_task("Find my license photo file", llm=llm, policy=Policy(base_layers()),
                      ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
                      limits=LoopLimits(), system_prompt="test", emit=events.append, checker=Checker(llm))
    assert result.status == "done" and len(llm.calls) == 5
    assert [e.data["key"] for e in events if e.kind == ev.GUARD] == ["guard.check"]
    assert llm.calls[1][0][0] == llm.calls[0][0][0]                            # 대화 차례의 시스템 메시지
    assert llm.calls[4][0][0] == llm.calls[2][0][0]                            # 걸음의 시스템 메시지


def test_first_words_are_translated_for_the_screen():
    """M8.3: 첫 말도 영어로 쓰고 화면에 낼 때 옮긴다. 이미 한국어로 썼으면 그대로."""
    tr = SayKorean("사진 폴더와 G: 구글 드라이브에서 먼저 찾아보겠습니다.")
    llm = FakeLLM([planned(), act("fs.search", query="license"), answer("Not found.")])
    _, events = run(llm, translator=tr, reply_lang="ko")
    data = next(e for e in events if e.kind == ev.PLAN).data
    assert data["say"] == "사진 폴더와 G: 구글 드라이브에서 먼저 찾아보겠습니다." and data["translated"] is True
    assert tr.calls[0] == (SAY, "ko") and data["original"]["say"] == SAY
    assert GOAL in llm.calls[1][0][-2]["content"]                         # 모델은 영어 계획을 본다
    tr = SayKorean()
    llm = FakeLLM([planned(say="사진 폴더부터 찾겠습니다."), act("fs.search", query="license"), answer("Not found.")])
    _, events = run(llm, translator=tr, reply_lang="ko")
    assert next(e for e in events if e.kind == ev.PLAN).data["say"] == "사진 폴더부터 찾겠습니다."
    assert all(c[0] != "사진 폴더부터 찾겠습니다." for c in tr.calls)


def test_language_note_stays_last_in_the_planning_call():
    note = reply_note("ko")
    llm = FakeLLM([planned(), act("fs.search", query="license"), answer("Not found.")])
    run(llm, situation="- fact one", final_note=note)
    system = llm.calls[0][0][0]["content"].rstrip()
    assert system.endswith(note.rstrip()) and "- fact one" in system


def test_bad_planning_reply_falls_back_to_planning_in_the_first_step():
    plan = ["Search by name."]
    llm = FakeLLM([FormatError("bad json"), {**act("fs.search", query="license"), "plan": plan}, answer("Not found.")])
    result, events = run(llm)
    assert result.status == "done" and result.steps == 2
    assert llm.calls[1][1]["required"] == ["progress", "plan", "thought", "action", "done"]   # 예전처럼 첫 걸음에 계획
    assert [e.data["plan"] for e in events if e.kind == ev.PLAN] == [plan]


def test_plan_first_can_be_turned_off():
    llm = FakeLLM([{**act("fs.search", query="license"), "plan": ["Search."]}, answer("Not found.")])
    result, _ = run(llm, plan_first=False)
    assert result.status == "done" and llm.calls[0][1]["required"] == ["progress", "plan", "thought", "action", "done"]


def test_no_planning_call_without_tools():
    llm = FakeLLM([answer("Hello, Researcher.")])
    result, _ = run(llm, tools=ToolRegistry())
    assert result.status == "done" and len(llm.calls) == 1 and llm.calls[0][1]["properties"]["done"]["enum"] == [True]


def test_stop_while_planning():
    llm = FakeLLM([Cancelled()])
    result, events = run(llm)
    assert result.status == "stopped" and result.reason == "user" and result.steps == 0
    flag = {"stop": True}
    llm2 = FakeLLM([])
    result2, _ = run(llm2, cancelled=lambda: flag["stop"])
    assert result2.status == "stopped" and llm2.calls == []


# ---------------------------------------------------------------- 엔진: 계획할 때 볼 사실
def open_app(tmp_path):
    home = tmp_path / "gh"
    ensure_home(home)
    out = io.StringIO()
    return cli.App(load_config(home), cli.Screen(ansi=False, out=out), llm=FakeLLM([]), os_name="linux"), out


def test_planning_facts_name_the_scope_and_the_index(tmp_path):
    app, _ = open_app(tmp_path)
    try:
        facts = app.planning_facts()
        assert facts.startswith("- fs.search with roots left out searches: ")
        assert "background index has not been built yet" in facts
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("penalty", encoding="utf-8")
        (docs / "photo.jpg").write_bytes(b"\xff\xd8" + b"0" * 30000)
        idx = ci.FileIndex(app.cfg.home)
        try:
            ci.run_pass(idx, [str(docs)], app.places)
        finally:
            idx.close()
        facts = app.planning_facts()
        assert "Background index: 2 files known by name (the file list is complete); text of 1 documents" in facts
        assert "Photos and scans already looked at" in facts and "not yet: 1 (findable by name only" in facts
        assert "Search by meaning is ready for 0 of 1 documents." in facts
    finally:
        app.close()


def test_engine_sends_the_facts_when_planning(tmp_path):
    app, _ = open_app(tmp_path)
    try:
        app.llm.steps = [said("Hello, Researcher.", goal="Greet.")]
        result = app.ask_task("hello")
        assert result is not None and result.status == "done"
        assert "Facts about this computer now:\n- fs.search with roots left out searches" in app.llm.calls[0][0][0]["content"]
    finally:
        app.close()


def test_plan_first_setting(tmp_path):
    home = tmp_path / "gh"
    ensure_home(home)
    assert load_config(home).limits.plan_first is True
    text = (home / "glados.toml").read_text(encoding="utf-8").replace("plan_first = true", "plan_first = false")
    (home / "glados.toml").write_text(text, encoding="utf-8")
    assert load_config(home).limits.plan_first is False


# ---------------------------------------------------------------- 되묻기·목표 범위 (M7.8)
QUESTION = ("Do you want research papers on this topic, health information, or something else? "
            "Tell me which and I will look.")


def ask_back(question=QUESTION):
    return said(question, "ask", "Unclear: the request is one word and could mean several things.")


class ToKorean:
    def __init__(self):
        self.calls = []

    def translate(self, fact, wit, lang):
        self.calls.append((fact, lang))
        return ("이 주제의 논문, 건강 정보, 다른 것 중 무엇을 원하시나요?", "") if lang == "ko" else None


def test_unclear_request_is_asked_back_without_tools():
    """실제로 난 일 (2026-11-12): 한 단어 요청에 모델이 연구 과제를 지어내 논문·웹 검색을 12걸음 하고 멈춤 (밖으로 18번).
    이제는 무엇을 원하는지 모르면 짐작하지 않고 묻고 끝낸다. 도구도, 밖으로 나가는 것도 없다."""
    from glados.core.checker import Checker

    calls = []
    tools = ToolRegistry([finder(calls), Tool("web.search", "search the web", run=lambda a: calls.append(a) or "")])
    llm = FakeLLM([ask_back()])
    events = []
    result = run_task("word", llm=llm, policy=Policy(base_layers()),
                      ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
                      limits=LoopLimits(), system_prompt="test", emit=events.append,
                      checker=Checker(llm))                         # 점검이 있어도 되묻기는 그대로 (기다린다)
    assert result.status == "done" and result.reason == "ask" and result.steps == 0
    assert result.answer.fact == QUESTION and len(llm.calls) == 1 and calls == []
    kinds = [e.kind for e in events]
    assert kinds == [ev.START, ev.ANSWER]                                  # 계획·도구·되돌림 없이 질문만
    assert result.transcript[-1]["role"] == "assistant" and QUESTION in result.transcript[-1]["content"]


def test_question_is_shown_in_the_researchers_language():
    tr = ToKorean()
    llm = FakeLLM([ask_back()])
    result, events = run(llm, translator=tr, reply_lang="ko")
    shown = next(e for e in events if e.kind == ev.ANSWER).data
    assert shown["fact"] == "이 주제의 논문, 건강 정보, 다른 것 중 무엇을 원하시나요?" and shown["translated"] is True
    assert result.internal.fact == QUESTION                               # 기억에는 영어
    assert tr.calls == [(QUESTION, "ko")]


def test_the_reply_to_the_question_continues_the_same_conversation():
    from glados.core.context import Conversation

    memory = Conversation()
    tools = ToolRegistry([finder()])
    first = run_task("word", llm=FakeLLM([ask_back()]), policy=Policy(base_layers()),
                     ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
                     limits=LoopLimits(), system_prompt="test", memory=memory)
    memory.add("word", first)
    llm = FakeLLM([planned(goal="Find recent research papers on the topic."), act("fs.search", query="x"),
                   answer("Found nothing.")])
    run_task("research papers", llm=llm, policy=Policy(base_layers()),
             ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
             limits=LoopLimits(), system_prompt="test", memory=memory)
    sent = [m["content"] for m in llm.calls[0][0]]
    assert any(QUESTION in c for c in sent) and sent[-1] == "research papers"   # 물은 것을 보고 계획한다


def test_planning_keeps_the_researchers_scope():
    from glados.core.prompts import CONTRACT, PLANNING
    from glados.core.schema import Plan

    assert "Do not add details they did not give" in PLANNING and '"ask" if you cannot tell' in PLANNING
    assert "do not guess" in PLANNING and "conversation does not settle it" in PLANNING
    from glados.core.checker import CHECK_PROMPT
    assert "{rubric}" in CONTRACT and "A narrower question swapped in for the request does not count" in CHECK_PROMPT
    assert plan_schema()["properties"]["next"]["enum"] == ["ask", "answer", "act"]
    assert Plan.model_validate({"next": "act", "plan": []}).next == "act"         # 계획이 없어도 받는다 (M8)
    for bad in ({"next": "ask", "say": ""}, {"next": "answer"}, {"next": "maybe", "say": "x"}):
        try:
            Plan.model_validate(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


# ---------------------------------------------------------------- 중간 보고 (M8)
def test_after_six_tools_it_comes_back_and_asks():
    """연구원님 결정: 모델 판단 + 도구 6번. 6번을 쓰면 도구 없이 지금까지를 말하고 계속할지 묻는다."""
    calls = []
    tools = ToolRegistry([finder(calls)])
    report = answer("사진 폴더와 다운로드, 문서에서 6번 찾았지만 없었습니다. G: 구글 드라이브도 찾아볼까요?")
    llm = FakeLLM([planned()] + [act("fs.search", query=f"license {i}") for i in range(6)] + [report])
    result, events = run(llm, tools=tools)
    assert result.status == "done" and result.reason == "checkin" and result.steps == 6 and len(calls) == 6
    assert result.answer.fact.endswith("찾아볼까요?")
    last = llm.calls[-1]
    assert last[0][-1]["content"].startswith("[check] You have used 6 tools in this task. Do not call more tools now.")
    assert last[1]["properties"]["done"]["enum"] == [True]                 # 도구 없이 말만
    assert not any(e.kind == ev.STOP for e in events)                      # 멈춤이 아니라 대화
    assert not any(m["content"].startswith("[check] You have used") for m in result.transcript)   # 지시는 기억에 안 남김
    assert result.transcript[-1]["role"] == "assistant" and "찾아볼까요" in result.transcript[-1]["content"]


def test_checkin_can_be_turned_off():
    tools = ToolRegistry([finder()])
    llm = FakeLLM([planned()] + [act("fs.search", query=f"license {i}") for i in range(7)] + [answer("Not found.")])
    events = []
    result = run_task("운전면허증 사진 찾아줘", llm=llm, policy=Policy(base_layers()),
                      ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
                      limits=LoopLimits(checkin_steps=0), system_prompt="test", emit=events.append)
    assert result.status == "done" and result.reason == "" and result.steps == 8


def test_rules_keep_the_researcher_in_the_loop():
    from glados.core.prompts import CONTRACT, reply_note

    assert "Work in short stretches and keep the Researcher in the loop." in CONTRACT
    assert "a much bigger place than planned" in CONTRACT and "Do not keep trying small variations." in CONTRACT
    assert "truly fits; usually leave it out" in CONTRACT and "Never guess at the Researcher's intentions." in CONTRACT
    assert "Still write everything in English (say, answer, thought, plan)" in reply_note("ko")
    assert LoopLimits().checkin_steps == 6


def test_checkin_setting(tmp_path):
    home = tmp_path / "gh"
    ensure_home(home)
    assert load_config(home).limits.checkin_steps == 6
    text = (home / "glados.toml").read_text(encoding="utf-8").replace("checkin_steps = 6", "checkin_steps = 0")
    (home / "glados.toml").write_text(text, encoding="utf-8")
    assert load_config(home).limits.checkin_steps == 0


# ---------------------------------------------------------------- 물으면 기다리기·위트·역할 (M8.2)
def test_a_question_in_the_first_words_waits_for_the_answer():
    """실제로 난 일 (2026-11-12): "간단한 실험 하나 하자"에 "목적과 가설을 먼저 설정해 주시겠습니까?"라고 묻고는
    next=act로 기다리지 않고 예제 데이터를 읽고 명령을 돌렸다. 이제 물었으면 도구 없이 답을 기다린다."""
    calls = []
    question = "연구원님, 실험의 목적과 가설을 먼저 설정해 주시겠습니까?"
    llm = FakeLLM([planned(say=question)])
    result, events = run(llm, tools=ToolRegistry([finder(calls)]))
    assert result.status == "done" and result.reason == "ask" and result.answer.fact == question
    assert calls == [] and len(llm.calls) == 1 and not [e for e in events if e.kind == ev.PROPOSAL]
    assert [e.kind for e in events] == [ev.START, ev.ANSWER]
    llm = FakeLLM([planned(say="I will check Pictures first."), act("fs.search", query="license"), answer("Not found.")])
    result, _ = run(llm)                                                     # 묻지 않았으면 그대로 진행
    assert result.reason == "" and len(llm.calls) == 3
    assert asks('괜찮을까요?"') and asks("Shall I look in G:?") and not asks("G:를 보겠습니다.")


def test_wit_is_optional_and_only_english():
    from glados.core.schema import ANSWER_SCHEMA

    assert ANSWER_SCHEMA["required"] == ["fact"] and "wit" not in plan_schema()["required"]
    korean_wit = {"thought": "", "done": True, "answer": {"fact": "Are you asking about coffee?", "wit": "인간은 데이터보다 물리적 필요를 먼저 처리합니다."},
                  "sources": []}
    llm = FakeLLM([korean_wit])
    result, events = run(llm, tools=ToolRegistry())
    shown = next(e for e in events if e.kind == ev.ANSWER).data
    assert shown["wit"] == "" and result.answer.wit == "" and result.internal.wit == ""
    assert "인간은" not in result.transcript[-1]["content"]                    # 기억에서도 뺀다
    llm = FakeLLM([answer("Hypothesis B was rejected. p = 0.41.", "Not a failure. A very precise way of being wrong.")])
    result, _ = run(llm, tools=ToolRegistry())
    assert result.answer.wit == "Not a failure. A very precise way of being wrong."
    llm = FakeLLM([said("Hello, Researcher.") | {"wit": "인사는 변수가 아닙니다."}])
    result, _ = run(llm)
    assert result.answer.wit == ""                                            # 대화 차례의 위트도


def test_role_and_rules_do_not_frame_everything_as_an_experiment():
    from glados.core.prompts import PLANNING, ROLE, persona, reply_note

    assert "lab assistant" in ROLE and "Small talk gets a short, natural reply" in ROLE and "scientist" not in ROLE
    assert "settle its goal and hypothesis with them before running anything" in ROLE
    assert persona().splitlines()[1].startswith("For experiments, you form a hypothesis")
    from glados.core.checker import RUBRIC
    assert "is every claim about files, data, results, this computer, or the current world backed" in RUBRIC
    assert 'If say asks the Researcher anything, next is "ask"' in PLANNING
    note = reply_note("ko")
    assert "Still write everything in English" in note and "안녕" not in note             # M8.3: 한국어 예는 뺐다


# ---------------------------------------------------------------- 실험은 스크립트로, 앞 결과 이어 쓰기 (M8.3)
def test_experiment_rules_name_the_experiments_folder(tmp_path):
    """연구원님이 고른 ②: 정리하라고 하자 새 난수로 다시 돌려 보고서 숫자가 달라짐 -> 스크립트 파일 + 고정 seed,
    정리할 때는 앞 결과를 쓴다, 보여 달라는 것은 답에 넣는다."""
    from glados.core.prompts import FILE_RULES

    app, _ = open_app(tmp_path)
    try:
        prompt = app.system_prompt()
        folder = str(Path(app.workspace()) / "experiments")
        assert f"Write an experiment's code to a new script file in the experiments folder ({folder})" in prompt
        assert "a fixed random seed" in prompt and "do not run it again" in prompt
        assert "use the same script and seed so the numbers stay the same" in prompt
    finally:
        app.close()
    from glados.core.checker import RUBRIC
    assert "anything they asked to see goes in answer.fact itself" in RUBRIC
    assert "or for an experiment's script (see Experiments)" in FILE_RULES


def test_no_experiment_rules_without_commands():
    from glados.core.prompts import build_system_prompt

    prompt = build_system_prompt("Windows 11", "qwen3.5:4b", tool_names=["fs.search", "fs.write_new"])
    assert "Experiments:" not in prompt


# ---------------------------------------------------------------- 이상한 결과·빠진 정보·중간 보고 질문 (M8.5)
def test_checkin_report_always_ends_with_a_question():
    tools = ToolRegistry([finder()])
    report = answer("The script was fixed but not run yet.")
    llm = FakeLLM([planned()] + [act("fs.search", query=f"license {i}") for i in range(6)] + [report])
    result, _ = run(llm, tools=tools)
    assert result.reason == "checkin" and result.answer.fact == "The script was fixed but not run yet. Shall I go on, and how?"
    assert "Shall I go on, and how?" in result.transcript[-1]["content"]
    asked = answer("I searched 6 places. Shall I look in G:?")
    llm = FakeLLM([planned()] + [act("fs.search", query=f"license {i}") for i in range(6)] + [asked])
    result, _ = run(llm, tools=ToolRegistry([finder()]))
    assert result.answer.fact == "I searched 6 places. Shall I look in G:?"                    # 이미 물었으면 그대로


def test_rules_for_odd_results_and_missing_details(tmp_path):
    from glados.core.prompts import CONTRACT, PLANNING

    from glados.core.checker import CHECK_PROMPT, RUBRIC               # M9.3: 판단은 답 점검이 (기준은 한 곳)
    assert "A p-value of exactly 1 or 0, a test statistic of exactly 0" in CHECK_PROMPT
    assert "could the method used give the reported results" in CONTRACT.replace("{rubric}", RUBRIC)
    assert "if it needs a detail you do not have and the notes and conversation do not give it" in PLANNING
    app, _ = open_app(tmp_path)
    try:
        assert "name the test and what it assumes, and say what the result shows and what it does not show" in app.system_prompt()
    finally:
        app.close()


# ---------------------------------------------------------------------- 막혔을 때 (M9.4)
def stuck(tool="fs.search", reason="Same empty result again.", **args):
    step = act(tool, **(args or {"query": "x"}))
    step["progress"] = {"reason": reason, "answer": False}
    return step


def moving(tool="fs.search", **args):
    step = act(tool, **(args or {"query": "y"}))
    step["progress"] = {"reason": "New results.", "answer": True}
    return step


def test_stalled_work_is_planned_again_from_what_was_learned():
    """Magentic-One 진행 장부처럼: 걸음마다 모델이 나아가고 있는지 적고, 아니라는 걸음이 max_stalls번 쌓이면
    대화 차례로 돌아가 계획을 다시 세운다. 막힌 걸음은 하지 않는다."""
    calls = []
    tools = ToolRegistry([finder(calls)])
    llm = FakeLLM([planned(), act("fs.search", query="a"), stuck(query="b"), stuck(query="c"),
                   stuck(query="d"),                                    # 3번째 "아님" -> 다시 계획 (d는 하지 않음)
                   planned(goal="Find it another way.", steps=["Look inside photos with fs.inspect."],
                           say="Names did not help; I will look inside the photos."),
                   act("fs.search", query="f"), answer("Not found.")])
    result, events = run(llm, tools=tools)
    assert result.status == "done" and [c["query"] for c in calls] == ["a", "b", "c", "f"]
    assert [e.data["stalls"] for e in events if e.kind == ev.STALL] == [1, 2, 3]
    plans = [e for e in events if e.kind == ev.PLAN and "say" in e.data]
    assert [p.data["step"] for p in plans] == [0, 3] and plans[1].data["goal"] == "Find it another way."
    replan_msgs = llm.calls[5][0]
    assert "Before you act" in replan_msgs[0]["content"]                          # 대화 차례의 시스템 메시지
    assert replan_msgs[-1]["content"].startswith("[check] The work is not moving forward (3 steps in a row; last: "
                                                 "Same empty result again.)")
    assert not any('"d"' in m["content"] for m in replan_msgs)                  # 막힌 걸음은 대화에서 뺀다


def test_still_stuck_after_a_new_plan_comes_back_and_asks():
    tools = ToolRegistry([finder()])
    report = answer("I searched names and photos and found nothing. Shall I try G:?")
    llm = FakeLLM([planned(), act("fs.search", query="a"), stuck(query="b"), stuck(query="c"), stuck(query="d"),
                   planned(goal="Try again."), stuck(query="f"), stuck(query="g"), stuck(query="h"), report])
    result, events = run(llm, tools=tools)
    assert result.reason == "checkin" and result.answer.fact.endswith("Shall I try G:?")
    assert llm.calls[-1][0][-1]["content"].startswith("[check] The work is still not moving forward after a new plan.")


def test_a_new_plan_can_also_ask_the_researcher():
    tools = ToolRegistry([finder()])
    llm = FakeLLM([planned(), act("fs.search", query="a"), stuck(query="b"), stuck(query="c"), stuck(query="d"),
                   said("Names and contents found nothing. Is the photo on your phone?", next_="ask")])
    result, events = run(llm, tools=tools)
    assert result.reason == "ask" and result.steps == 3 and "phone" in result.answer.fact


def test_progress_is_not_counted_on_the_first_step_or_when_off():
    tools = ToolRegistry([finder()])
    llm = FakeLLM([planned(), stuck(query="a"), answer("Not found.")])
    result, events = run(llm, tools=tools)
    assert not any(e.kind == ev.STALL for e in events)                        # 첫 걸음은 볼 결과가 없다
    events = []
    llm = FakeLLM([planned(), act("fs.search", query="a"), stuck(query="b"), moving(query="c"), stuck(query="d"),
                   stuck(query="e"), answer("Not found.")])
    run_task("find it", llm=llm, policy=Policy(base_layers()),
             ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
             limits=LoopLimits(max_stalls=0), system_prompt="test", emit=events.append)
    assert len(llm.calls) == 7 and not any(e.kind == ev.PLAN and e.data.get("step") for e in events)
    assert [e.data["stalls"] for e in events if e.kind == ev.STALL] == [1, 1, 2]   # 나아가면 하나 줄인다 (Magentic-One)


# ---------------------------------------------------------------------- 판단 모델 (M9.6)
def test_lead_model_takes_the_conversation_turns_and_failed_steps():
    """Goose lead/worker처럼: 판단(대화 차례·답 점검·단계 확인)은 lead, 걸음은 worker. worker가 형식을 못 맞추면
    그 걸음을 lead가 한 번 더."""
    from glados.core.checker import Checker

    tools = ToolRegistry([finder()])
    lead = FakeLLM([planned(), verdict()])
    worker = FakeLLM([FormatError("bad json"), act("fs.search", query="license"), answer("Not found.")])
    lead.steps.insert(1, act("fs.search", query="x"))       # 형식이 깨진 첫 걸음은 lead가
    events = []
    result = run_task("find my license photo", llm=worker, policy=Policy(base_layers()),
                      ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
                      limits=LoopLimits(), system_prompt="test", emit=events.append, lead=lead,
                      checker=Checker(lead))
    assert result.status == "done" and result.answer.fact == "Not found."
    assert len(lead.calls) == 3 and "Before you act" in lead.calls[0][0][0]["content"]      # 대화 차례, 걸음 1, 점검
    assert lead.calls[2][0][-1]["content"].startswith("[check] Before your draft answer")
    assert len(worker.calls) == 3


def test_app_lead_model_setting(tmp_path):
    home = tmp_path / "gh"
    ensure_home(home)
    text = (home / "glados.toml").read_text(encoding="utf-8")
    (home / "glados.toml").write_text(text.replace('name = "qwen3.5:9b"              # 맥은', 'lead = "qwen3.5:27b"\nname = "qwen3.5:9b"              # 맥은', 1).replace('lead = ""                        # 판단(', '# lead (', 1), encoding="utf-8")
    cfg = load_config(home)
    assert cfg.model.lead == "qwen3.5:27b"
    app = cli.App(cfg, cli.Screen(ansi=False, out=io.StringIO()), os_name="linux")
    try:
        assert app.lead is not None and app.lead.cfg.name == "qwen3.5:27b"
    finally:
        app.close()

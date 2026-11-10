"""에이전트 루프 한도 시험 (가짜 모델)."""

from __future__ import annotations

import time

from conftest import FakeLLM, act, answer, base_layers, make_ctx
from glados.config import LoopLimits
from glados.core import events as ev
from glados.core.llm import FormatError
from glados.core.loop import OBS_LIMIT, run_task
from glados.hands import Tool, ToolRegistry
from glados.safety.policy import Policy


def run(llm, tools=None, limits=None, approve=None, audit=None, overflow_dir=None, ctx=None):
    tools = tools or ToolRegistry()
    events = []
    ctx = ctx or make_ctx("linux", registered_tools=frozenset(tools.names()))
    result = run_task(
        "시험 작업",
        llm=llm,
        policy=Policy(base_layers()),
        ctx=ctx,
        tools=tools,
        limits=limits or LoopLimits(),
        system_prompt="시험",
        emit=events.append,
        approve=approve,
        audit=audit,
        overflow_dir=overflow_dir,
        task_id="T1",
    )
    return result, events


def reader(calls=None, text="내용"):
    def _run(args):
        if calls is not None:
            calls.append(args)
        return text
    return Tool("fs.read", "파일 읽기", run=_run)


def test_answers_without_tools():
    llm = FakeLLM([answer("위약금 조항은 3개입니다.", "2019년 것은 만료돼서 뺐습니다.")])
    result, events = run(llm)
    assert result.status == "done" and result.answer.fact.startswith("위약금")
    schema = llm.calls[0][1]
    assert schema["properties"]["done"]["enum"] == [True]          # 도구가 없으면 바로 답만
    assert "도구가 없습니다" in llm.calls[0][0][0]["content"]
    assert [e.kind for e in events][0] == ev.START and events[-1].kind == ev.ANSWER


def test_tool_then_answer():
    calls = []
    llm = FakeLLM([act("fs.read", path="/tmp/a.txt"), answer()])
    result, events = run(llm, tools=ToolRegistry([reader(calls)]))
    assert result.status == "done" and result.steps == 2
    assert calls == [{"path": "/tmp/a.txt"}]
    second_messages = llm.calls[1][0]
    assert second_messages[-1]["content"].startswith("[관찰: fs.read] 내용")
    assert llm.calls[0][1]["properties"]["action"]["anyOf"][0]["properties"]["tool"]["enum"] == ["fs.read"]


def test_max_steps_stops():
    llm = FakeLLM([act("fs.read", path=f"/tmp/{i}.txt") for i in range(3)])
    result, events = run(llm, tools=ToolRegistry([reader()]), limits=LoopLimits(max_steps=3))
    assert result.status == "stopped" and result.reason == "max_steps" and result.steps == 3
    assert events[-1].kind == ev.STOP


def test_same_call_three_times_stops():
    calls = []
    llm = FakeLLM([act("fs.read", path="/tmp/a.txt")] * 3)
    result, _ = run(llm, tools=ToolRegistry([reader(calls)]))
    assert result.reason == "same_call"
    assert len(calls) == 2            # 세 번째는 실행하지 않음


def test_tool_error_twice_stops():
    def boom(args):
        raise RuntimeError("디스크 없음")
    tools = ToolRegistry([Tool("fs.read", "읽기", run=boom)])
    llm = FakeLLM([act("fs.read", path="/tmp/a"), act("fs.read", path="/tmp/b")])
    result, events = run(llm, tools=tools)
    assert result.reason == "tool_errors"
    assert any(e.kind == ev.OBSERVATION and "디스크 없음" in e.data["text"] for e in events)


def test_format_error_stops():
    llm = FakeLLM([FormatError("형식 오류가 3번 이어졌습니다")])
    result, _ = run(llm)
    assert result.status == "stopped" and result.reason == "format"


def test_denied_call_is_not_executed():
    calls = []
    tools = ToolRegistry([reader(calls)])
    ctx = make_ctx("linux", registered_tools=frozenset(tools.names()))
    llm = FakeLLM([act("shell.run", command="rm -rf /"), answer()])   # 등록되지 않은 도구
    result, events = run(llm, tools=tools, ctx=ctx)
    assert result.status == "done"
    assert any(e.kind == ev.DECISION and e.data["verdict"] == "deny" for e in events)
    assert "차단" in llm.calls[1][0][-1]["content"]
    assert calls == []


def test_ask_once_executes():
    ran = []
    shell = Tool("shell.run", "명령 실행", run=lambda a: ran.append(a) or "ok")
    asked = []
    llm = FakeLLM([act("shell.run", command="make test"), answer()])
    result, _ = run(llm, tools=ToolRegistry([shell]), approve=lambda call, d: asked.append(d) or "once")
    assert result.status == "done" and ran == [{"command": "make test"}] and len(asked) == 1


def test_task_grant_skips_second_question():
    ran = []
    shell = Tool("shell.run", "명령 실행", run=lambda a: ran.append(a) or "ok")
    asked = []
    llm = FakeLLM([act("shell.run", command="make a"), act("shell.run", command="make b"), answer()])
    result, _ = run(llm, tools=ToolRegistry([shell]), approve=lambda call, d: asked.append(d) or "task")
    assert result.status == "done" and len(ran) == 2 and len(asked) == 1


def test_denied_by_researcher_feeds_back():
    ran = []
    shell = Tool("shell.run", "명령 실행", run=lambda a: ran.append(a) or "ok")
    llm = FakeLLM([act("shell.run", command="make clean"), answer("하지 않았습니다.")])
    result, _ = run(llm, tools=ToolRegistry([shell]), approve=lambda call, d: "deny")
    assert ran == [] and "거절" in llm.calls[1][0][-1]["content"]


def test_admin_task_grant_is_treated_as_once():
    shell = Tool("shell.run", "명령 실행", run=lambda a: "ok")
    asked = []
    llm = FakeLLM([
        act("shell.run", command="winget install x", admin=True),
        act("shell.run", command="winget install y", admin=True),
        answer(),
    ])
    ctx = make_ctx("windows", registered_tools=frozenset({"shell.run"}))
    result, _ = run(llm, tools=ToolRegistry([shell]), ctx=ctx, approve=lambda call, d: asked.append(d) or "task")
    assert result.status == "done" and len(asked) == 2      # 관리자 작업은 매번 묻는다


def test_long_output_is_clipped_and_saved(tmp_path):
    big = "가" * (OBS_LIMIT * 3)
    llm = FakeLLM([act("fs.read", path="/tmp/big.txt"), answer()])
    result, _ = run(llm, tools=ToolRegistry([reader(text=big)]), overflow_dir=tmp_path)
    observation = llm.calls[1][0][-1]["content"]
    assert len(observation) < OBS_LIMIT + 300 and "중략" in observation
    saved = tmp_path / "T1-1.txt"
    assert saved.read_text(encoding="utf-8") == big


def test_audit_records_who_decided():
    log = []
    shell = Tool("shell.run", "명령 실행", run=lambda a: "ok")
    llm = FakeLLM([act("shell.run", command="git status"), act("shell.run", command="make x"), answer()])
    run(llm, tools=ToolRegistry([shell]), approve=lambda c, d: "once", audit=lambda c, d, who: log.append((c.args["command"], d.verdict, who)))
    assert log == [("git status", "allow", "정책"), ("make x", "ask", "연구원님:이번만")]


def test_tool_timeout_counts_as_error():
    def slow(args):
        time.sleep(2)
        return "늦음"
    tools = ToolRegistry([Tool("fs.read", "읽기", run=slow, timeout_s=1)])
    llm = FakeLLM([act("fs.read", path="/tmp/a"), act("fs.read", path="/tmp/b")])
    result, _ = run(llm, tools=tools, limits=LoopLimits(local_timeout_s=1))
    assert result.reason == "tool_errors"

"""에이전트 루프 한도 시험 (가짜 모델)."""

from __future__ import annotations

import time

from conftest import FakeLLM, act, answer, base_layers, make_ctx
from glados.config import LoopLimits
from glados.core import events as ev
from glados.core.llm import FormatError
from glados.core.loop import HINT_REPEAT, NUDGE_EVIDENCE, OBS_LIMIT, run_task
from glados.hands import Tool, ToolError, ToolRegistry
from glados.safety.policy import Policy


def run(llm, tools=None, limits=None, approve=None, audit=None, overflow_dir=None, ctx=None,
        evidence_check=None, request="test task"):
    tools = tools or ToolRegistry()
    events = []
    ctx = ctx or make_ctx("linux", registered_tools=frozenset(tools.names()))
    result = run_task(
        request,
        llm=llm,
        policy=Policy(base_layers()),
        ctx=ctx,
        tools=tools,
        limits=limits or LoopLimits(),
        system_prompt="test",
        emit=events.append,
        approve=approve,
        audit=audit,
        overflow_dir=overflow_dir,
        task_id="T1",
        evidence_check=evidence_check,
    )
    return result, events


def reader(calls=None, text="content"):
    def _run(args):
        if calls is not None:
            calls.append(args)
        return text
    return Tool("fs.read", "read a file", run=_run)


def test_answers_without_tools():
    llm = FakeLLM([answer("There are 3 penalty clauses.", "The 2019 one expired, so it was left out.")])
    result, events = run(llm)
    assert result.status == "done" and result.answer.fact.startswith("There are 3")
    schema = llm.calls[0][1]
    assert schema["properties"]["done"]["enum"] == [True]          # 도구가 없으면 바로 답만
    assert "No tools are available" in llm.calls[0][0][0]["content"]
    assert [e.kind for e in events][0] == ev.START and events[-1].kind == ev.ANSWER


def test_tool_then_answer():
    calls = []
    llm = FakeLLM([act("fs.read", path="/tmp/a.txt"), answer()])
    result, events = run(llm, tools=ToolRegistry([reader(calls)]))
    assert result.status == "done" and result.steps == 2
    assert calls == [{"path": "/tmp/a.txt"}]
    second_messages = llm.calls[1][0]
    assert second_messages[-1]["content"].startswith("[result: fs.read] content")
    assert llm.calls[0][1]["properties"]["action"]["anyOf"][0]["properties"]["tool"]["enum"] == ["fs.read"]


def test_first_step_writes_a_plan_when_tools_exist():
    """질문을 받으면 어떻게 할지 모델이 먼저 생각한다 (M2.7): 첫 걸음의 스키마가 계획을 요구한다."""
    from glados.cli import event_lines

    plan = ["Read the file.", "Answer from what it says."]
    llm = FakeLLM([{**act("fs.read", path="/tmp/a.txt"), "plan": plan}, answer()])
    result, events = run(llm, tools=ToolRegistry([reader()]))
    first, second = llm.calls[0][1], llm.calls[1][1]
    assert first["required"] == ["plan", "thought", "action", "done"] and list(first["properties"])[0] == "plan"
    assert first["properties"]["plan"]["minItems"] == 1 and first["properties"]["plan"]["maxItems"] == 6
    assert second["required"] == ["thought", "action", "done"]         # 뒤 걸음은 바꿀 때만. action은 늘 (없으면 null)
    planned = [e for e in events if e.kind == ev.PLAN]
    assert len(planned) == 1 and planned[0].data == {"step": 1, "plan": plan}
    assert event_lines(planned[0]) == [("dim", "계획"), ("dim", "  1. Read the file."),
                                       ("dim", "  2. Answer from what it says.")]
    assert '"plan": ["Read the file."' in llm.calls[1][0][-2]["content"]   # 계획은 대화에 남아 다음 걸음이 본다


def test_plan_only_reply_is_sent_back_once():
    """실제로 난 일 (2026-11-11, qwen3.5:4b): 계획만 쓰고 done:false에 action을 빼먹음 -> 형식 오류로 멈췄다.
    이제는 계획을 받아 두고 "첫 걸음을 떼라"고 한 번 되돌린다."""
    calls = []
    plan_only = {"plan": ["Search Downloads for PDFs.", "Report them."], "thought": "Plan first.", "action": None,
                 "done": False, "answer": {"fact": "I will search.", "wit": ""}, "sources": []}
    llm = FakeLLM([plan_only, act("fs.read", path="/tmp/a.txt"), answer()])
    result, events = run(llm, tools=ToolRegistry([reader(calls)]))
    assert result.status == "done" and calls == [{"path": "/tmp/a.txt"}]
    assert llm.calls[1][0][-1]["content"].startswith("[check] Your plan is noted. Now take its first step")
    assert [e.data["key"] for e in events if e.kind == ev.GUARD] == ["guard.act"]
    assert [e.data["plan"] for e in events if e.kind == ev.PLAN] == [plan_only["plan"]]
    assert not any('"I will search."' in m["content"] for m in result.transcript)   # 계획만 쓴 답은 기억에 안 남김


def test_plan_only_twice_stops():
    plan_only = {"plan": ["Think."], "thought": "", "action": None, "done": False}
    llm = FakeLLM([plan_only, plan_only])
    result, _ = run(llm, tools=ToolRegistry([reader()]))
    assert result.status == "stopped" and result.reason == "format"


def test_no_plan_is_required_without_tools():
    llm = FakeLLM([answer("Hello, Researcher.")])
    run(llm)
    assert llm.calls[0][1]["required"] == ["thought", "done", "answer", "sources"]


def test_max_steps_stops():
    llm = FakeLLM([act("fs.read", path=f"/tmp/{i}.txt") for i in range(3)])
    result, events = run(llm, tools=ToolRegistry([reader()]), limits=LoopLimits(max_steps=3))
    assert result.status == "stopped" and result.reason == "max_steps" and result.steps == 3
    assert events[-1].kind == ev.STOP and events[-1].data["answered"] is False   # 마지막 보고도 실패하면 멈춤만
    assert result.answer is None and len(result.trail) == 3


def test_same_call_three_times_stops():
    calls = []
    llm = FakeLLM([act("fs.read", path="/tmp/a.txt")] * 3)
    result, _ = run(llm, tools=ToolRegistry([reader(calls)]))
    assert result.reason == "same_call"
    assert len(calls) == 2            # 세 번째는 실행하지 않음


# ---------------------------------------------------------------- 작은 모델을 위한 안전망
def test_stop_gives_final_report_without_tools():
    llm = FakeLLM([act("fs.read", path="/tmp/a.txt")] * 3 + [answer("a.txt only says 'content'.", "", ["/tmp/a.txt"])])
    result, events = run(llm, tools=ToolRegistry([reader()]))
    assert result.status == "stopped" and result.reason == "same_call"
    assert result.answer is not None and result.answer.fact.startswith("a.txt") and result.sources == ["/tmp/a.txt"]
    last_messages, last_schema = llm.calls[-1]
    assert last_schema["properties"]["done"]["enum"] == [True]          # 마지막 보고는 도구 없이
    assert last_messages[-1]["content"].startswith("[stopped] Stopped: the same call (fs.read) repeated 3 times.")
    kinds = [e.kind for e in events]
    assert kinds[-2:] == [ev.ANSWER, ev.STOP] and events[-1].data["answered"] is True


def test_max_steps_final_report():
    llm = FakeLLM([act("fs.read", path=f"/tmp/{i}.txt") for i in range(2)] + [answer("I read both files.")])
    result, _ = run(llm, tools=ToolRegistry([reader()]), limits=LoopLimits(max_steps=2))
    assert result.reason == "max_steps" and result.answer.fact == "I read both files."


def test_tool_errors_final_report_sees_the_error():
    def boom(args):
        raise ToolError("No such file: /tmp/b")
    tools = ToolRegistry([Tool("fs.read", "read", run=boom)])
    llm = FakeLLM([act("fs.read", path="/tmp/a"), act("fs.read", path="/tmp/b"), answer("Neither file exists.")])
    result, _ = run(llm, tools=tools)
    assert result.reason == "tool_errors" and result.answer.fact == "Neither file exists."
    report_messages = llm.calls[-1][0]
    assert report_messages[-2]["content"] == "[result: fs.read] Error: No such file: /tmp/b"   # 마지막 오류를 보고 답함


def test_file_answer_without_tools_is_sent_back_once():
    llm = FakeLLM([
        answer("There are 0 PDF files in Downloads."),             # 확인 없이 지어낸 답
        act("fs.read", path="/tmp/학위논문.pdf"),
        answer("The thesis is about turning EEG into text.", "", ["/tmp/학위논문.pdf"]),
    ])
    seen = []
    result, events = run(llm, tools=ToolRegistry([reader()]), evidence_check=lambda text: seen.append(text) or True,
                         request="찾은 PDF 하나 읽고 요약해줘")
    assert result.status == "done" and result.steps == 3 and result.answer.fact.startswith("The thesis")
    assert llm.calls[1][0][-1]["content"] == NUDGE_EVIDENCE
    assert seen[0] == "찾은 PDF 하나 읽고 요약해줘\nAnswer now.\nThere are 0 PDF files in Downloads. "   # 요청 + 영어 생각과 답
    guards = [e for e in events if e.kind == ev.GUARD]
    assert len(guards) == 1
    assert not any(e.kind == ev.ANSWER and "0 PDF" in e.data["fact"] for e in events)   # 지어낸 답은 화면에 안 나감


def test_evidence_nudge_only_once():
    llm = FakeLLM([answer("There is none."), answer("There really is none.")])
    result, events = run(llm, tools=ToolRegistry([reader()]), evidence_check=lambda r: True)
    assert result.status == "done" and result.answer.fact == "There really is none."
    assert sum(e.kind == ev.GUARD for e in events) == 1


def test_no_nudge_for_plain_questions_or_after_tools():
    llm = FakeLLM([answer("Hello, Researcher.")])
    result, events = run(llm, tools=ToolRegistry([reader()]), evidence_check=lambda r: False)
    assert result.steps == 1 and not any(e.kind == ev.GUARD for e in events)
    llm = FakeLLM([act("fs.read", path="/tmp/a"), answer("I read it.")])
    result, events = run(llm, tools=ToolRegistry([reader()]), evidence_check=lambda r: True)
    assert result.steps == 2 and not any(e.kind == ev.GUARD for e in events)


def test_no_nudge_without_tools():
    llm = FakeLLM([answer("I have no file tools.")])
    result, _ = run(llm, evidence_check=lambda r: True)
    assert result.steps == 1


def test_same_result_again_gets_hint():
    search = Tool("fs.search", "find", run=lambda a: "0 found (scope: ~/Downloads)\nNo file matches.")
    llm = FakeLLM([act("fs.search", query="paper"), act("fs.search", query="thesis"), answer("Nothing found.")])
    run(llm, tools=ToolRegistry([search]))
    first = llm.calls[1][0][-1]["content"]
    second = llm.calls[2][0][-1]["content"]
    assert HINT_REPEAT not in first and second.endswith(HINT_REPEAT)


def test_trail_keeps_what_ran():
    llm = FakeLLM([act("fs.read", path="/tmp/a.txt"), act("shell.run", command="rm -rf /"), answer()])
    result, _ = run(llm, tools=ToolRegistry([reader(text="line 1\nline 2")]))
    assert result.trail == [{"tool": "fs.read", "args": {"path": "/tmp/a.txt"}, "head": "line 1\nline 2"}]   # 막힌 호출은 빠짐


def test_tool_error_twice_stops():
    def boom(args):
        raise RuntimeError("disk missing")
    tools = ToolRegistry([Tool("fs.read", "read", run=boom)])
    llm = FakeLLM([act("fs.read", path="/tmp/a"), act("fs.read", path="/tmp/b")])
    result, events = run(llm, tools=tools)
    assert result.reason == "tool_errors"
    assert any(e.kind == ev.OBSERVATION and e.data["text"] == "Error: fs.read failed: RuntimeError: disk missing" for e in events)


def test_format_error_stops():
    llm = FakeLLM([FormatError("Malformed output 3 times in a row")])
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
    assert llm.calls[1][0][-1]["content"] == ("[result: shell.run] Blocked: Unregistered tool (shell.run). "
                                             "Find another way, or report that it cannot be done.")
    assert calls == []


def test_ask_once_executes():
    ran = []
    shell = Tool("shell.run", "run a command", run=lambda a: ran.append(a) or "ok")
    asked = []
    llm = FakeLLM([act("shell.run", command="make test"), answer()])
    result, _ = run(llm, tools=ToolRegistry([shell]), approve=lambda call, d: asked.append(d) or "once")
    assert result.status == "done" and ran == [{"command": "make test"}] and len(asked) == 1


def test_task_grant_skips_second_question():
    ran = []
    shell = Tool("shell.run", "run a command", run=lambda a: ran.append(a) or "ok")
    asked = []
    llm = FakeLLM([act("shell.run", command="make a"), act("shell.run", command="make b"), answer()])
    result, _ = run(llm, tools=ToolRegistry([shell]), approve=lambda call, d: asked.append(d) or "task")
    assert result.status == "done" and len(ran) == 2 and len(asked) == 1


def test_denied_by_researcher_feeds_back():
    ran = []
    shell = Tool("shell.run", "run a command", run=lambda a: ran.append(a) or "ok")
    llm = FakeLLM([act("shell.run", command="make clean"), answer("I did not run it.")])
    result, _ = run(llm, tools=ToolRegistry([shell]), approve=lambda call, d: "deny")
    assert ran == [] and "The Researcher denied this." in llm.calls[1][0][-1]["content"]


def test_admin_task_grant_is_treated_as_once():
    shell = Tool("shell.run", "run a command", run=lambda a: "ok")
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
    assert len(observation) < OBS_LIMIT + 300 and "(clipped: 12000 chars in total; full text:" in observation
    saved = tmp_path / "T1-1.txt"
    assert saved.read_text(encoding="utf-8") == big


def test_audit_records_who_decided():
    log = []
    shell = Tool("shell.run", "run a command", run=lambda a: "ok")
    llm = FakeLLM([act("shell.run", command="git status"), act("shell.run", command="make x"), answer()])
    run(llm, tools=ToolRegistry([shell]), approve=lambda c, d: "once", audit=lambda c, d, who: log.append((c.args["command"], d.verdict, who)))
    assert log == [("git status", "allow", "정책"), ("make x", "ask", "연구원님:이번만")]


def test_tool_timeout_counts_as_error():
    def slow(args):
        time.sleep(2)
        return "late"
    tools = ToolRegistry([Tool("fs.read", "read", run=slow, timeout_s=1)])
    llm = FakeLLM([act("fs.read", path="/tmp/a"), act("fs.read", path="/tmp/b")])
    result, _ = run(llm, tools=tools, limits=LoopLimits(local_timeout_s=1))
    assert result.reason == "tool_errors"


# ---------------------------------------------------------------- 연구원님이 멈춤 (TUI)
from glados.core.llm import Cancelled  # noqa: E402


def test_cancel_flag_stops_before_next_step():
    flag = {"stop": False}

    def search(args):
        flag["stop"] = True                       # 도구가 도는 동안 멈춤을 누름
        return "0 found"

    tools = ToolRegistry([Tool("fs.search", "find", run=search)])
    llm = FakeLLM([act("fs.search", query="x"), answer("never")])
    events = []
    result = run_task("find x", llm=llm, policy=Policy(base_layers()),
                      ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
                      limits=LoopLimits(), system_prompt="test", emit=events.append,
                      cancelled=lambda: flag["stop"])
    assert result.status == "stopped" and result.reason == "user" and result.steps == 1
    assert len(llm.calls) == 1 and result.answer is None
    assert result.transcript[-1]["content"] == "[stopped] Stopped by the Researcher."
    stop = [e for e in events if e.kind == ev.STOP][0]
    assert stop.data["reason"] == "user" and not stop.data["answered"]


def test_cancelled_model_call_stops_task():
    llm = FakeLLM([act("fs.read", path="/a"), Cancelled()])
    result, events = run(llm, ToolRegistry([reader()]))
    assert result.status == "stopped" and result.reason == "user" and result.steps == 2
    assert [e.kind for e in events][-1] == ev.STOP


def test_cancelled_inside_tool_stops_task():
    def stopped(args):
        raise Cancelled()                          # 예: 찾기 안의 Glossary가 모델을 부르다 멈춤

    tools = ToolRegistry([Tool("fs.search", "find", run=stopped)])
    result, _ = run(FakeLLM([act("fs.search", query="x"), answer("never")]), tools)
    assert result.status == "stopped" and result.reason == "user" and result.steps == 1


def test_cancelled_final_report_still_stops():
    llm = FakeLLM([act("fs.read", path="/a")] * 3 + [Cancelled()])
    result, _ = run(llm, ToolRegistry([reader()]))
    assert result.reason == "user"                 # 멈춤 보고를 쓰다가 멈춤


def test_stop_does_not_wait_for_slow_tool():
    flag = {"stop": False}

    def slow(args):
        flag["stop"] = True
        time.sleep(3)                              # 멈춤을 누른 뒤에도 한참 걸리는 도구
        return "late"

    tools = ToolRegistry([Tool("fs.read", "read", run=slow)])
    start = time.monotonic()
    result = run_task("read", llm=FakeLLM([act("fs.read", path="/a"), answer("never")]), policy=Policy(base_layers()),
                      ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
                      limits=LoopLimits(), system_prompt="test", cancelled=lambda: flag["stop"])
    assert result.reason == "user" and time.monotonic() - start < 1.5

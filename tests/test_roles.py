"""역할 (M9.2): 대화 차례가 일의 종류를 고르고, 그 역할의 지시와 도구만으로 일한다. 다른 도구가 필요하면 handoff.

- 역할 파일: 패키지 agents/*.md + GLADOS_HOME/agents/*.md (같은 이름이면 연구원님 것)
- 대화 차례: 짧은 프롬프트 + 역할 목록, 형식의 role은 역할 이름 중 하나
- 일하는 걸음: 그 역할의 도구 + 늘 쓰는 도구 + handoff만 형식에 (고를 수 없는 도구는 가린다)
- 없는 역할 이름이면 예전처럼 모든 도구로
"""

from __future__ import annotations

from conftest import FakeLLM, act, answer, base_layers, make_ctx
from glados.config import LoopLimits
from glados.core import events as ev
from glados.core import roles as rk
from glados.core.loop import run_task
from glados.core.prompts import build_system_prompt
from glados.hands import Tool, ToolRegistry
from glados.safety.policy import Policy


def tool(name, calls=None, text="ok"):
    return Tool(name, f"{name} tool", {"type": "object", "properties": {"q": {"type": "string"}}},
                run=lambda a: (calls.append((name, a)) if calls is not None else None) or text)


def registry(calls=None, remote=False):
    names = ["fs.search", "fs.read", "fs.write_new", "shell.run", "web.search", "web.fetch", "papers.search",
             "session.resume", "memory.note"] + (["remote.gpu"] if remote else [])
    return ToolRegistry([tool(n, calls) for n in names])


def planned(role, goal="Find the file.", say="I will look for it."):
    return {"goal": goal, "next": "act", "role": role, "plan": ["Search."], "check": "Found.", "say": say,
            "sources": []}


def run(llm, roles, tools, prompts=None):
    events = []
    prompts = prompts if prompts is not None else []

    def prompt_for(names, text):
        prompts.append((list(names), text))
        return f"PROMPT tools={','.join(names)} role={text}"

    result = run_task(
        "find my notes", llm=llm, policy=Policy(base_layers()),
        ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools, limits=LoopLimits(),
        system_prompt="FULL", emit=events.append, roles=roles, prompt_for=prompt_for)
    return result, events


def tools_in(schema):
    """걸음 형식에서 고를 수 있는 도구 이름."""
    names = set()
    for branch in schema["properties"]["action"].get("anyOf", [schema["properties"]["action"]]):
        if branch.get("type") == "null":
            continue
        names.update(branch["properties"]["tool"].get("enum", []))
    return names


def test_package_roles_and_the_researchers_file_wins(tmp_path):
    names = [r.name for r in rk.load_roles(tmp_path)]
    assert {"files", "web", "experiment", "command", "server"} <= set(names)
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "files.md").write_text(
        "---\nname: files\ndescription: My own file helper.\ntools: [\"fs.search\"]\n---\nOnly search.\n",
        encoding="utf-8")
    (tmp_path / "agents" / "broken.md").write_text("no front matter", encoding="utf-8")
    (tmp_path / "agents" / "notes.md").write_text("---\nname: notes\ndescription: Notes.\ntools: [memory.note]\n---\n",
                                                  encoding="utf-8")
    roles = {r.name: r for r in rk.load_roles(tmp_path)}
    assert roles["files"].description == "My own file helper." and roles["files"].instructions == "Only search."
    assert roles["files"].allows("fs.search") and not roles["files"].allows("fs.read")
    assert roles["files"].allows("memory.note")                       # 늘 쓰는 도구
    assert "notes" in roles and "broken" not in roles


def test_usable_subset_and_menu():
    roles = rk.load_roles(None)
    tools = registry()
    found = [r.name for r in rk.usable(roles, tools)]
    assert "server" not in found and {"files", "web", "experiment", "command"} <= set(found)  # 서버 도구가 없다
    files = rk.find(roles, "files")
    sub = rk.subset(files, tools, rk.usable(roles, tools))
    assert set(sub.names()) == {"fs.search", "fs.read", "fs.write_new", "session.resume", "memory.note", rk.HANDOFF}
    handoff = sub.get(rk.HANDOFF)
    assert "files" not in handoff.params["properties"]["role"]["enum"]
    assert "web" in handoff.params["properties"]["role"]["enum"]
    text = rk.menu(rk.usable(roles, tools), tools)
    assert text.startswith("Roles (pick one in role):")
    assert "- web: " in text and "Tools: fs.write_new, web.search, web.fetch, papers.search." in text
    assert "session.resume" not in text                                # 늘 쓰는 도구는 목록에 없다


def test_planner_picks_a_role_and_steps_see_only_its_tools():
    calls = []
    tools = registry(calls)
    roles = rk.usable(rk.load_roles(None), tools)
    llm = FakeLLM([planned("files"), act("fs.search", q="notes"), answer("Found notes.txt.")])
    prompts = []
    result, events = run(llm, roles, tools, prompts)
    assert result.status == "done" and calls == [("fs.search", {"q": "notes"})]
    planner_msgs, planner_schema = llm.calls[0]
    assert planner_msgs[0]["content"].startswith("PROMPT tools= role=")          # 짧은 프롬프트 (도구 설명 없음)
    assert "Roles (pick one in role):" in planner_msgs[0]["content"]
    assert "- role (for \"act\"" in planner_msgs[0]["content"]
    assert planner_schema["properties"]["role"]["enum"] == [r.name for r in roles]
    assert planner_schema["required"][:3] == ["goal", "next", "role"]
    step_msgs, step_schema = llm.calls[1]
    assert step_msgs[0]["content"].startswith("PROMPT tools=fs.search,fs.read,fs.write_new,session.resume,memory.note")
    assert "You are working with the Researcher's files" in step_msgs[0]["content"]
    assert "- handoff(role, reason)" in step_msgs[0]["content"]
    assert tools_in(step_schema) == {"fs.search", "fs.read", "fs.write_new", "session.resume", "memory.note",
                                     rk.HANDOFF}
    plan = next(e for e in events if e.kind == ev.PLAN and e.data.get("step") == 0)
    assert plan.data["role"] == "files"


def test_handoff_switches_role_and_keeps_the_conversation():
    calls = []
    tools = registry(calls)
    roles = rk.usable(rk.load_roles(None), tools)
    llm = FakeLLM([planned("files"), act("fs.search", q="paper"),
                   act(rk.HANDOFF, role="web", reason="The paper is not on this computer; search online."),
                   act("web.search", q="paper"), answer("Found it online.")])
    result, events = run(llm, roles, tools)
    assert result.status == "done" and [c[0] for c in calls] == ["fs.search", "web.search"]
    role_events = [e for e in events if e.kind == ev.ROLE]
    assert len(role_events) == 1 and role_events[0].data["role"] == "web"
    assert not any(e.kind == ev.PROPOSAL and e.data["tool"] == rk.HANDOFF for e in events)   # 도구로 세지 않는다
    msgs, schema = llm.calls[3]
    assert "web.search" in tools_in(schema) and "fs.search" not in tools_in(schema)
    assert any("[result: handoff]" in m["content"] and "Now working as web." in m["content"] for m in msgs)
    assert any("fs.search" in m["content"] for m in msgs if m["role"] == "assistant")    # 같은 대화를 이어 간다


def test_a_tool_outside_the_role_is_refused_and_bad_handoffs_explained():
    calls = []
    tools = registry(calls)
    roles = rk.usable(rk.load_roles(None), tools)
    llm = FakeLLM([planned("files"), act("web.search", q="x"), act(rk.HANDOFF, role="files", reason="same"),
                   answer("I could not search the web from here.")])
    result, events = run(llm, roles, tools)
    assert calls == [] and result.status == "done"
    texts = [m["content"] for m in llm.calls[3][0]]
    assert any("web.search is not one of this role's tools" in x for x in texts)
    assert any("no other role named 'files'" in x for x in texts)


def test_unknown_role_falls_back_to_all_tools():
    calls = []
    tools = registry(calls)
    roles = rk.usable(rk.load_roles(None), tools)
    llm = FakeLLM([planned("cooking"), act("web.search", q="x"), answer("Done.")])
    result, events = run(llm, roles, tools)
    assert calls == [("web.search", {"q": "x"})]
    assert llm.calls[1][0][0]["content"].startswith("FULL")              # 예전처럼 전체 프롬프트
    assert next(e for e in events if e.kind == ev.PLAN).data["role"] == ""


def test_role_prompt_moves_experiment_rules_into_the_role():
    full = build_system_prompt("Linux", "m", tool_names=["shell.run"], experiments="/x/experiments")
    assert "Experiments:" in full
    role = rk.find(rk.load_roles(None), "experiment")
    text = role.instructions.replace("{experiments}", "/x/experiments")
    scoped = build_system_prompt("Linux", "m", tool_names=["shell.run", "fs.write_new"], experiments="/x/experiments",
                                 role_text=text)
    assert "Experiments:\n" not in scoped and "You are running an experiment" in scoped
    filled = rk.fill(role, {"experiments": "/x/experiments"})
    assert "experiments folder (/x/experiments)" in rk.stage_text(filled, 2)
    assert "{experiments}" not in rk.marker(filled, 2, "now")
    planner = build_system_prompt("Linux", "m", tool_names=[], servers=["gpu1 (4 GPUs)"], role_text="")
    assert "GPU servers: gpu1 (4 GPUs)." in planner and "No GPU servers" not in planner


def test_app_uses_roles_and_the_switch_turns_them_off(tmp_path):
    import io

    from glados import cli
    from glados.config import ensure_home, load_config

    home = tmp_path / "gh"
    ensure_home(home)
    cfg = load_config(home)
    app = cli.App(cfg, cli.Screen(ansi=False, out=io.StringIO()), llm=FakeLLM([]), os_name="linux")
    try:
        names = {r.name for r in app.roles()}
        assert {"files", "command", "experiment"} <= names and "server" not in names
        exp = app.system_prompt(["shell.run", "fs.write_new"], rk.find(app.roles(), "experiment").instructions)
        assert "{experiments}" not in exp and "GLaDOS" in exp and "You are running an experiment" in exp
        cfg.limits.roles = False
        assert app.roles() == []
    finally:
        app.close()


# ---------------------------------------------------------------------- 단계 (M9.5)
def staged_role():
    return rk.Role("lab", "Experiments.", ["fs.", "shell.run"], "Do the experiment.", [
        rk.Stage("hypothesis", "State it.", "A hypothesis is stated.", ["fs.read"], approve=True),
        rk.Stage("implement", "Write the script.", "The script is saved.", ["fs.write_new"]),
        rk.Stage("run", "Run it.", "The output is shown.", ["shell.run"]),
    ])


def staged_tools(calls=None):
    return ToolRegistry([tool(n, calls) for n in ("fs.read", "fs.write_new", "shell.run", "web.search")])


def done_stage(said, tool_name=None, **args):
    step = act(tool_name, **args) if tool_name else answer("Done.")
    step["stage_done"] = {"reason": said, "answer": True}
    return step


def gate(met=True, approved=None, reason="Checked."):
    out = {"met": {"reason": reason, "answer": met}}
    if approved is not None:
        out["approved"] = {"reason": reason, "answer": approved}
    return out


def run_staged(llm, roles, tools, request="test whether x is normal", stages="auto", memory=None):
    events = []

    def prompt_for(names, text):
        return f"PROMPT tools={','.join(names)}\n{text}"

    result = run_task(
        request, llm=llm, policy=Policy(base_layers()),
        ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools, limits=LoopLimits(),
        system_prompt="FULL", emit=events.append, roles=roles, prompt_for=prompt_for, stages=stages, memory=memory,
        approve=lambda call, decision: "once")
    return result, events


def test_stages_run_one_at_a_time_with_gates_and_their_own_tools():
    """설계서의 실험 단계 + prompt chaining with gates: 단계마다 그 단계의 도구만, 끝났다고 하면 조건을 확인하고 넘어간다."""
    calls = []
    lab = staged_role()
    roles = [lab, rk.Role("web", "Web.", ["web."], "Search.")]
    llm = FakeLLM([
        planned("lab", goal="Test whether x is normal."),
        done_stage("H: x ~ N(0,1); reject if p < 0.05.", "fs.read", q="x"),   # 가설 단계를 끝냈다 (이 걸음은 하지 않음)
        gate(met=True, approved=True),                                      # 연구원님이 이미 그렇게 말함
        act("fs.write_new", q="script"),
        done_stage("Saved test_x.py.", "shell.run", q="python test_x.py"),
        gate(met=False, reason="No tool result shows the save path."),       # 아직: 같은 단계를 이어 간다
        act("fs.write_new", q="script again"),
        done_stage("Saved /e/test_x.py.", "shell.run", q="x"),
        gate(met=True),
        act("shell.run", q="python /e/test_x.py"),
        done_stage("p = 0.43"),                                             # 마지막 단계를 끝내며 답함
        gate(met=True),
    ])
    result, events = run_staged(llm, roles, staged_tools(calls))
    assert result.status == "done" and [c[0] for c in calls] == ["fs.write_new", "fs.write_new", "shell.run"]
    assert tools_in(llm.calls[1][1]) == {"fs.read", rk.HANDOFF}                    # 가설: 읽기만
    assert tools_in(llm.calls[3][1]) == {"fs.write_new", rk.HANDOFF}               # 구현
    assert tools_in(llm.calls[9][1]) == {"shell.run", rk.HANDOFF}                  # 실행
    assert llm.calls[1][1]["required"][:2] == ["progress", "stage_done"]
    assert "Now: stage 1/3 hypothesis. State it." in llm.calls[1][0][0]["content"]
    assert "Now: stage 2/3 implement." in llm.calls[3][0][0]["content"] and "1. hypothesis (done)" in llm.calls[3][0][0]["content"]
    gate_prompt = llm.calls[2][0][-1]["content"]
    assert gate_prompt.startswith("[check] You say stage 1/3 (hypothesis) is done: H: x ~ N(0,1)")
    assert "- approved:" in gate_prompt and llm.calls[2][1]["required"] == ["met", "approved"]
    assert llm.calls[5][1]["required"] == ["met"]                                  # 동의가 필요 없는 단계
    assert llm.calls[6][0][-1]["content"].startswith("[check] Stage 2/3 (implement) is not done yet: No tool result")
    statuses = [(e.data["index"], e.data["status"]) for e in events if e.kind == ev.STAGE]
    assert statuses == [(1, "now"), (1, "passed"), (2, "now"), (2, "not_done"), (2, "passed"), (3, "now"),
                        (3, "complete")]
    notes = [m["content"] for m in result.transcript if m["content"].startswith("[stage]")]
    assert notes[0] == "[stage] lab 1/3 hypothesis: now. State it."
    assert "[stage] lab 1/3 hypothesis: passed. H: x ~ N(0,1); reject if p < 0.05." in notes
    assert notes[-1] == "[stage] lab 3/3 run: passed. All stages are done. p = 0.43"


def test_a_stage_that_needs_agreement_waits_for_the_researcher_and_continues_next_turn():
    from glados.core.context import Conversation

    memory = Conversation()
    lab = staged_role()
    roles = [lab, rk.Role("web", "Web.", ["web."], "Search.")]
    tools = staged_tools()
    llm = FakeLLM([planned("lab", goal="Run a simple experiment."),
                   done_stage("H: the mean of x is 0.", "fs.read", q="x"), gate(met=True, approved=False),
                   answer("My hypothesis: the mean of x is 0. Shall I test it this way?")])
    first, events = run_staged(llm, roles, tools, request="let's do a simple experiment", memory=memory)
    assert first.reason == "ask" and first.answer.fact.endswith("Shall I test it this way?")
    assert llm.calls[3][0][-1]["content"].startswith("[check] The hypothesis stage is ready: H: the mean of x is 0.")
    assert [e.data["status"] for e in events if e.kind == ev.STAGE] == ["now", "waiting"]
    memory.add("let's do a simple experiment", first)
    llm = FakeLLM([planned("lab", goal="Go on with the experiment."),                 # stage 없이: 표시에서 이어 감
                   done_stage("H: the mean of x is 0.", "fs.read", q="x"), gate(met=True, approved=True),
                   act("fs.write_new", q="s"), answer("Saved.")])
    second, events = run_staged(llm, roles, tools, request="yes, go on", memory=memory)
    assert [(e.data["index"], e.data["status"]) for e in events if e.kind == ev.STAGE][:2] == [(1, "now"), (1, "passed")]
    assert tools_in(llm.calls[3][1]) == {"fs.write_new", rk.HANDOFF}


def test_planner_can_name_the_stage_and_pilot_waits_after_every_stage():
    lab = staged_role()
    roles = [lab, rk.Role("web", "Web.", ["web."], "Search.")]
    tools = staged_tools()
    plan = planned("lab", goal="Run the script again.")
    plan["stage"] = "run"
    llm = FakeLLM([plan, done_stage("Output shown.", "shell.run", q="x"), gate(met=True, approved=False),
                   answer("The run printed p = 0.4. Shall I go on?")])
    result, events = run_staged(llm, roles, tools, stages="pilot")
    assert "stage" in llm.calls[0][1]["properties"] and "stage" not in llm.calls[0][1]["required"]
    assert llm.calls[0][1]["properties"]["stage"]["enum"] == ["hypothesis", "implement", "run"]
    assert "- stage (optional, for a role with stages)" in llm.calls[0][0][0]["content"]
    assert "Stages: hypothesis, implement, run." in llm.calls[0][0][0]["content"]
    assert "Now: stage 3/3 run." in llm.calls[1][0][0]["content"]
    assert llm.calls[2][1]["required"] == ["met", "approved"]                     # pilot: 단계마다 동의
    assert result.reason == "ask"


def test_stages_off_and_resume_markers():
    lab = staged_role()
    roles = [lab, rk.Role("web", "Web.", ["web."], "Search.")]
    llm = FakeLLM([planned("lab"), act("shell.run", q="x"), answer("Ran it.")])
    result, events = run_staged(llm, roles, staged_tools(), stages="off")
    assert not any(e.kind == ev.STAGE for e in events) and "stage_done" not in llm.calls[1][1]["properties"]
    msgs = [{"role": "user", "content": "[stage] lab 2/3 implement: now. Write the script."}]
    assert rk.resume_at(lab, msgs) == 1
    assert rk.resume_at(lab, msgs + [{"role": "user", "content": "[stage] lab 2/3 implement: passed. Saved."}]) == 2
    assert rk.resume_at(lab, [{"role": "user", "content": "[stage] lab 3/3 run: passed. All stages are done."}]) == 0
    assert rk.resume_at(lab, [{"role": "user", "content": "[stage] other 2/3 x: now."}]) == 0


def test_experiment_role_file_has_the_design_stages():
    exp = rk.find(rk.load_roles(None), "experiment")
    assert [s.name for s in exp.stages] == ["hypothesis", "design", "implement", "test", "run", "analyze", "conclude"]
    assert exp.stages[0].approve and not any(s.approve for s in exp.stages[1:])
    assert exp.stages[0].tools == ["fs.read", "fs.search"] and "shell.run" in exp.stages[3].tools
    assert "known answer" in exp.stages[3].goal                                    # 방법을 먼저 확인 (설계서 04 테스트)


def test_a_stage_can_end_without_a_tool_call():
    """가설 단계처럼 도구 없이 끝나는 단계: action이 null이어도 stage_done이면 받는다."""
    from glados.core.schema import Step

    step = Step.model_validate({"progress": {"reason": "start", "answer": True},
                                "stage_done": {"reason": "H: mean is 0.", "answer": True},
                                "thought": "The hypothesis is clear.", "action": None, "done": False})
    assert step.stage_done.answer and step.action is None
    lab = staged_role()
    roles = [lab, rk.Role("web", "Web.", ["web."], "Search.")]
    llm = FakeLLM([planned("lab"), step.model_dump(), gate(met=True, approved=True), answer("Saved nothing yet.")])
    result, events = run_staged(llm, roles, staged_tools())
    assert [(e.data["index"], e.data["status"]) for e in events if e.kind == ev.STAGE][:3] == [
        (1, "now"), (1, "passed"), (2, "now")]

"""평가 세트 (M9.1): 사례 읽기, 점검, 한 사례 돌리기(가짜 모델), 보고서와 지난 결과 비교."""

from __future__ import annotations

import io
import json

from conftest import FakeLLM, act, answer
from glados import cli, evals
from glados.config import ensure_home, load_config
from glados.hands import Tool, ToolRegistry


def said(text, next_="answer"):
    return {"goal": "g", "next": next_, "plan": [], "check": "", "say": text, "sources": []}


def config(tmp_path):
    home = tmp_path / "gh"
    ensure_home(home)
    return load_config(home)


def test_cases_load_and_the_researchers_file_wins(tmp_path):
    cfg = config(tmp_path)
    ids = [c.id for c in evals.load_cases(cfg.home)]
    assert "one-word-coffee" in ids and "experiment-vague" in ids and len(ids) == len(set(ids))
    (cfg.home / "evals.toml").write_text('[[case]]\nid = "weather-no-place"\nturns = ["오늘 서울 날씨"]\n'
                                         'tools_any = ["web.search"]\n\n[[case]]\nid = "mine"\nturns = ["hi"]\n',
                                         encoding="utf-8")
    cases = {c.id: c for c in evals.load_cases(cfg.home)}
    assert cases["weather-no-place"].turns == ["오늘 서울 날씨"] and cases["weather-no-place"].checks == {"tools_any": ["web.search"]}
    assert "mine" in cases


def outcome(case, **kw):
    base = dict(passed=False, problems=[], seconds=1.0, tools=[], calls=[], answer="", internal="", reason="")
    base.update(kw)
    return evals.Outcome(case, **base)


def test_checks():
    case = evals.Case("c", ["x"], checks={"expect_ask": True, "max_tools": 1, "tools_any": ["fs."],
                                          "tools_none": ["remote."], "args_lacks": {"fs.search": ["roots"]},
                                          "answer_lacks": ["irreversible"], "max_seconds": 10, "no_sendback": True})
    good = outcome(case, tools=["fs.search"], calls=[{"tool": "fs.search", "args": {"query": "x"}}],
                   answer="어느 폴더를 볼까요?")
    assert evals.judge(case, good) == []
    bad = outcome(case, tools=["fs.search", "remote.log"], seconds=30, sendbacks=1,
                  calls=[{"tool": "fs.search", "args": {"roots": ["C:\\Users\\User"]}}],
                  answer="Done.", internal="as per my rules for irreversible actions")
    problems = evals.judge(case, bad)
    assert any("question" in p for p in problems) and any("used 2 tools" in p for p in problems)
    assert "used remote.log" in problems and any("roots" in p for p in problems)
    assert any("irreversible" in p for p in problems) and any("took 30s" in p for p in problems)
    assert any("sent back" in p for p in problems)
    assert evals.judge(evals.Case("d", ["x"], checks={"answer_has": ["4"]}), outcome(case, internal="It is 4.")) == []


def test_one_case_runs_in_a_fresh_conversation_with_approvals_denied(tmp_path):
    cfg = config(tmp_path)
    ran = []

    def make_app(cfg, screen, llm=None):
        app = cli.App(cfg, screen, llm=llm, os_name="linux")
        app.tools = ToolRegistry([Tool("shell.run", "run", run=lambda a: ran.append(a) or "ok")])
        return app

    case = evals.Case("run", ["run the experiment"], checks={"tools_none": ["remote."]})   # 영어: 번역 호출 없음
    plan = {"goal": "Run it.", "next": "act", "plan": ["Run."], "check": "", "say": "I will run it.", "sources": []}
    llm = FakeLLM([plan, act("shell.run", command="python x.py"), answer("The run was denied.")])
    out = evals.run_case(cfg, case, allow_run=False, make_app=make_app, llm=llm)
    assert out.passed and out.tools == ["shell.run"] and ran == []                # 승인 거절: 실제로 돌지 않음
    assert out.internal == "The run was denied."
    hello = evals.run_case(cfg, evals.Case("hi", ["hello"], checks={"max_tools": 0}),
                           allow_run=False, make_app=make_app, llm=FakeLLM([said("Hello, Researcher.")]))
    assert hello.passed and hello.answer == "Hello, Researcher."
    assert not (cfg.home / "sessions.db").exists() or "hello" not in (cfg.home / "sessions.db").read_bytes().decode("latin-1")


def test_report_compares_with_the_last_run(tmp_path):
    cfg = config(tmp_path)
    a, b = evals.Case("a", ["x"]), evals.Case("b", ["y"])
    path1, delta1 = evals.write_report(cfg.home, [outcome(a, passed=True), outcome(b, passed=False, problems=["p"])], "m")
    assert delta1 == {} and path1.exists() and "passed 1/2" in path1.read_text(encoding="utf-8")
    import time
    time.sleep(1.1)                                                              # 보고서 이름은 초 단위
    path2, delta2 = evals.write_report(cfg.home, [outcome(a, passed=False, problems=["q"]), outcome(b, passed=True)], "m")
    assert delta2 == {"a": "worse", "b": "better"}
    text = path2.read_text(encoding="utf-8")
    assert "| a | FAIL | WORSE |" in text and "| b | pass | better |" in text
    data = json.loads(path2.with_suffix(".json").read_text(encoding="utf-8"))
    assert [r["id"] for r in data["results"]] == ["a", "b"]


def test_main_skips_cases_that_need_running(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    seen = []
    monkeypatch.setattr(evals, "run_case", lambda cfg, case, allow_run: seen.append(case.id) or outcome(case, passed=True))
    out = io.StringIO()
    code = evals.main(cfg, cli.Screen(ansi=False, out=out), [], allow_run=False)
    assert code == 0 and "experiment-gaussian-run" not in seen and "one-word-coffee" in seen
    assert "skipped (need --allow-run): " in out.getvalue() and "passed " in out.getvalue()
    seen.clear()
    evals.main(cfg, cli.Screen(ansi=False, out=io.StringIO()), ["greeting"], allow_run=False)
    assert seen == ["greeting"]

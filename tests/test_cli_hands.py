"""터미널 화면과 손 목록 시험."""

from __future__ import annotations

import io
import time

import pytest

from conftest import FakeLLM, answer
from glados import cli, osinfo
from glados.config import ensure_home, load_config
from glados.hands import Tool, ToolError, ToolRegistry, load_hands
from glados.safety.policy import ToolCall


@pytest.fixture
def app(tmp_path):
    ensure_home(tmp_path)
    cfg = load_config(tmp_path)
    out = io.StringIO()
    a = cli.App(cfg, cli.Screen(ansi=False, out=out), llm=FakeLLM([]))
    yield a, out
    a.close()


def test_admin_start_is_refused(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GLADOS_HOME", str(tmp_path))
    monkeypatch.setattr(osinfo, "is_admin", lambda: True)
    assert cli.main([]) == 2
    assert "관리자 권한" in capsys.readouterr().out


def test_version(capsys):
    assert cli.main(["--version"]) == 0
    assert "GLaDOS 0.1.0" in capsys.readouterr().out


def test_ask_task_prints_answer_and_keeps_history(app):
    a, out = app
    a.llm.steps = [answer("모델은 qwen3.5:4b입니다.", "작지만 성실합니다.")]
    result = a.ask_task("지금 모델이 뭐야?")
    text = out.getvalue()
    assert result.status == "done"
    assert "모델은 qwen3.5:4b입니다." in text and "작지만 성실합니다." in text
    assert a.history[-2]["content"] == "지금 모델이 뭐야?"


def test_history_is_sent_on_next_task(app):
    a, _ = app
    a.llm.steps = [answer("첫 답입니다."), answer("둘째 답입니다.")]
    a.ask_task("첫 질문")
    a.ask_task("둘째 질문")
    messages = a.llm.calls[1][0]
    assert any(m["content"] == "첫 질문" for m in messages)


def test_task_is_logged(app):
    a, _ = app
    a.llm.steps = [answer("기록됩니다.")]
    a.ask_task("기록 시험")
    task_id, request, status = a.task_log.recent(1)[0]
    assert request == "기록 시험" and status == "done"
    lines = (a.cfg.home / "logs" / "tasks.jsonl").read_text(encoding="utf-8")
    assert "기록 시험" in lines and "기록됩니다." in lines


def test_bold_and_dim_with_ansi():
    s = cli.Screen(ansi=True, out=io.StringIO())
    assert s.bold("사실") == "\x1b[1m사실\x1b[0m" and s.dim("위트") == "\x1b[2m위트\x1b[0m"


def test_commands(app):
    a, out = app
    assert a.command("/help") is False and "/new" in out.getvalue()
    assert a.command("/undo") is False and "M2" in out.getvalue()
    assert a.command("/log") is False and "기록이 없습니다" in out.getvalue()
    assert a.command("/quit") is True


def test_missing_model_message(app):
    a, out = app
    a.llm.has_model = lambda: False
    assert a.check_model() is False
    assert "ollama pull qwen3.5:9b" in out.getvalue()


def test_header_shows_low_mode(tmp_path):
    ensure_home(tmp_path)
    text = (tmp_path / "glados.toml").read_text(encoding="utf-8").replace('mode = "normal"', 'mode = "low"')
    (tmp_path / "glados.toml").write_text(text, encoding="utf-8")
    a = cli.App(load_config(tmp_path), cli.Screen(ansi=False, out=io.StringIO()), llm=FakeLLM([]))
    assert "qwen3.5:4b (낮은 모드)" in a.header()
    a.close()


def test_source_dirs_cover_project_in_dev():
    dirs = cli.source_dirs()
    assert any(d.endswith("glados") for d in dirs)


# ---------------------------------------------------------------- 손
@pytest.mark.parametrize("os_name", ["windows", "mac", "linux"])
def test_hands_load_for_each_os(os_name):
    reg = load_hands(os_name)
    assert reg.names() == []                         # M1에는 도구 없음
    assert "도구가 없습니다" in reg.describe()


def test_duplicate_tool_names_rejected():
    with pytest.raises(ValueError):
        ToolRegistry([Tool("fs.read", "a"), Tool("fs.read", "b")])


def test_unknown_tool_message():
    with pytest.raises(ToolError) as e:
        ToolRegistry().run(ToolCall("app.open", {}))
    assert "이 컴퓨터에서는" in str(e.value)


def test_tool_timeout():
    reg = ToolRegistry([Tool("fs.read", "느림", run=lambda a: time.sleep(2) or "x", timeout_s=1)])
    with pytest.raises(ToolError) as e:
        reg.run(ToolCall("fs.read", {}))
    assert "1초" in str(e.value)

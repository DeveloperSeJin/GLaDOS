"""터미널 화면과 손 목록 시험."""

from __future__ import annotations

import io
import time
from datetime import datetime

import pytest

from conftest import FakeLLM, act, answer, make_places
from glados import cli, osinfo
from glados.config import ensure_home, load_config
from glados.core.context import FULL
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


def translation(fact: str, wit: str = "") -> dict:
    """가짜 모델이 번역 호출에 돌려줄 JSON (답은 영어로 쓰고, 화면에는 연구원님의 언어로)."""
    return {"fact": fact, "wit": wit}


def test_ask_task_prints_answer_and_keeps_history(app):
    a, out = app
    a.llm.steps = [answer("The model is qwen3.5:4b.", "Small, but diligent."),
                   translation("모델은 qwen3.5:4b입니다.", "작지만 성실합니다.")]
    result = a.ask_task("지금 모델이 뭐야?")
    text = out.getvalue()
    assert result.status == "done"
    assert "모델은 qwen3.5:4b입니다." in text and "작지만 성실합니다." in text and "Small, but" not in text
    turn = a.memory.turns[-1]
    assert turn.request == "지금 모델이 뭐야?" and turn.answer == "The model is qwen3.5:4b. Small, but diligent."


def test_history_is_sent_on_next_task(app):
    a, _ = app
    a.llm.steps = [answer("First answer."), answer("Second answer.")]
    a.ask_task("first question")
    a.ask_task("second question")
    messages = a.llm.calls[1][0]
    assert any(m["content"] == "first question" for m in messages)


PAPER = "/home/sejin/Downloads/학위논문.pdf"
FOUND = (f"2 found (scope: /home/sejin/Downloads)\n- {PAPER} · 2.1MB · 2026-10-02 · en: thesis, dissertation, paper\n"
         "- /home/sejin/Downloads/요약.pdf · 90KB · 2026-09-01 · en: summary")


def fake_file_hands(a, reads):
    a.places = make_places("linux")                       # 정책 판정을 컴퓨터와 상관없이 같게
    a.tools = ToolRegistry([
        Tool("fs.search", "find", run=lambda args: FOUND),
        Tool("fs.read", "read", run=lambda args: reads.append(args["path"]) or f"[{args['path']}] 52000 chars total, PDF with 30 pages\nEEG"),
    ])


def test_follow_up_remembers_previous_task(app):
    """M2에서 실제로 난 문제: 두 번째 질문에서 앞 작업을 기억하지 못하고 "0개입니다"라고 지어냄."""
    a, out = app
    reads: list[str] = []
    fake_file_hands(a, reads)
    a.llm.steps = [
        act("fs.search", query="paper", kind="pdf", roots=["~/Downloads"]),
        answer("Found 2 PDFs in Downloads; one is a thesis.", "", [PAPER]),
        translation("다운로드 폴더에서 PDF 2개를 찾았습니다. 하나는 학위논문입니다."),
        act("fs.read", path=PAPER),
        answer("The thesis is about turning EEG into sentences.", "", [PAPER]),
        translation("학위논문은 뇌파로 문장을 만드는 연구입니다."),
    ]
    a.ask_task("다운로드 폴더에서 논문 하나 찾아줘")
    result = a.ask_task("찾은 PDF 하나 읽고 요약해줘")
    assert result.status == "done" and reads == [PAPER]
    second = a.llm.calls[3][0]                     # 두 번째 작업의 첫 호출이 보는 대화 (0~2는 첫 작업과 번역)
    requests = [m["content"] for m in second if m["role"] == "user" and not m["content"].startswith("[")]
    assert requests == ["다운로드 폴더에서 논문 하나 찾아줘", "찾은 PDF 하나 읽고 요약해줘"]
    assert any(m["content"].startswith("[result: fs.search]") and PAPER in m["content"] for m in second)   # 찾은 결과 그대로
    assert any(m["role"] == "assistant" and '"fs.search"' in m["content"] for m in second)            # 앞 걸음도 그대로
    assert any(m["role"] == "assistant" and "Found 2 PDFs" in m["content"] for m in second)            # 기억은 영어 답
    assert "학위논문은 뇌파로 문장을 만드는 연구입니다." in out.getvalue()


def test_made_up_answer_is_sent_back(app):
    a, out = app
    reads: list[str] = []
    fake_file_hands(a, reads)
    a.llm.steps = [
        act("fs.search", query="paper", kind="pdf"),
        answer("Found 2 PDFs.", "", [PAPER]),
        translation("PDF 2개를 찾았습니다."),
        answer("There are 0 PDF files in Downloads."),                     # 출처 없이 단정 -> 되돌림
        act("fs.read", path=PAPER),
        answer("The thesis is about turning EEG into sentences.", "", [PAPER]),
        translation("학위논문은 뇌파로 문장을 만드는 연구입니다."),
    ]
    a.ask_task("다운로드 폴더에서 논문 하나 찾아줘")
    a.ask_task("찾은 PDF 하나 읽고 요약해줘")
    text = out.getvalue()
    assert "확인하지 않은 답이라 다시 확인하게 했습니다" in text and "0 PDF" not in text
    remembered = a.memory.turns[-1].messages
    assert not any("0 PDF" in m["content"] for m in remembered)          # 되돌린 답은 기억하지 않음
    assert reads == [PAPER]


def test_answer_from_memory_with_sources_is_accepted(app):
    """이미 읽은 파일을 다시 물으면 다시 읽지 않고 기억으로 답한다 (출처가 앞 결과에 있으면 통과)."""
    a, out = app
    reads: list[str] = []
    fake_file_hands(a, reads)
    a.llm.steps = [
        act("fs.read", path=PAPER),
        answer("It is an EEG study.", "", [PAPER]),
        translation("뇌파 연구입니다."),
        answer("Conclusion: EEG can be turned into sentences.", "", ["C:\\Users\\User\\Downloads\\학위논문.pdf"]),
        translation("결론: 뇌파로 문장을 만들 수 있습니다."),
    ]
    a.ask_task("학위논문.pdf 읽어줘")
    result = a.ask_task("그 논문 결론만 다시 말해줘")
    assert result.status == "done" and result.steps == 1 and reads == [PAPER]
    assert result.answer.fact == "결론: 뇌파로 문장을 만들 수 있습니다."
    assert "다시 확인하게" not in out.getvalue()


def test_stopped_task_is_remembered_with_report(app):
    a, out = app
    fake_file_hands(a, [])
    a.llm.steps = [act("fs.search", query="paper")] * 3 + [
        answer("I found 2 paper PDFs and searched no further."), translation("논문 PDF 2개를 찾았고, 더 찾지 않았습니다.")]
    result = a.ask_task("논문 찾아줘")
    assert result.status == "stopped" and result.answer is not None
    text = out.getvalue()
    assert "논문 PDF 2개를 찾았고" in text and "(같은 호출(fs.search)이 3번 이어져 멈췄습니다.)" in text
    turn = a.memory.turns[-1]
    assert turn.stop_note.startswith("(stopped:") and PAPER in "\n".join(m["content"] for m in turn.render(FULL))


def test_context_and_new_commands(app):
    a, out = app
    a.command("/context")
    assert "기억하고 있는 대화가 없습니다" in out.getvalue()
    a.llm.steps = [answer("First answer."), answer("Second answer.")]
    a.ask_task("first question")
    a.ask_task("second question")
    a.command("/context")
    text = out.getvalue()
    assert "지난 작업 2개를 기억합니다" in text and "1. [전체] first question" in text and "2. [전체] second question" in text
    a.command("/new")
    assert a.memory.turns == [] and "앞의 대화는 잊었습니다" in out.getvalue()
    a.llm.steps = [answer("Third answer.")]
    a.ask_task("third question")
    assert not any(m["content"] == "first question" for m in a.llm.calls[-1][0])


def test_task_is_logged(app):
    a, _ = app
    a.llm.steps = [answer("It is logged.")]
    a.ask_task("log this task")
    task_id, request, status = a.task_log.recent(1)[0]
    assert request == "log this task" and status == "done"
    lines = (a.cfg.home / "logs" / "tasks.jsonl").read_text(encoding="utf-8")
    assert "log this task" in lines and "It is logged." in lines


def test_bold_and_dim_with_ansi():
    s = cli.Screen(ansi=True, out=io.StringIO())
    assert s.bold("사실") == "\x1b[1m사실\x1b[0m" and s.dim("위트") == "\x1b[2m위트\x1b[0m"


def test_commands(app):
    a, out = app
    assert a.command("/help") is False and "/undo" in out.getvalue()
    assert a.command("/servers") is False and "M4" in out.getvalue()
    assert a.command("/log") is False and "기록이 없습니다" in out.getvalue()
    assert a.command("/undo") is False and "되돌릴 파일 수정이 없습니다" in out.getvalue()
    assert a.command("/quit") is True


def test_undo_command_restores_last_edit(app, tmp_path, monkeypatch):
    a, out = app
    note = tmp_path / "메모.md"
    note.write_text("원래 글", encoding="utf-8")
    a.hctx.checkpoints.save(note, "T1")
    note.write_text("고친 글", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt="": "1")
    a.command("/undo")
    assert note.read_text(encoding="utf-8") == "원래 글" and "되돌렸습니다" in out.getvalue()
    assert a.policy_log.recent(1)[0]["tool"] == "undo"


def test_system_prompt_has_current_time_and_file_rules(app):
    a, _ = app
    prompt = a.system_prompt()
    assert datetime.now().strftime("%Y-%m-%d") in prompt and "File work:" in prompt


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
def test_hands_load_for_each_os(os_name, app):
    a, _ = app
    assert load_hands(os_name).names() == []                      # 손 정보가 없으면 도구 없음
    reg = load_hands(os_name, a.hctx)
    assert reg.names() == ["fs.search", "fs.inspect", "fs.read", "fs.write_new", "fs.edit", "shell.run"]
    assert "fs.search(query, kind?, year?, roots?, content?, limit?)" in reg.describe()


def test_duplicate_tool_names_rejected():
    with pytest.raises(ValueError):
        ToolRegistry([Tool("fs.read", "a"), Tool("fs.read", "b")])


def test_unknown_tool_message():
    with pytest.raises(ToolError) as e:
        ToolRegistry().run(ToolCall("app.open", {}))
    assert "is not available on this computer" in str(e.value) and e.value.key == "tool.unavailable"


def test_tool_timeout():
    reg = ToolRegistry([Tool("fs.read", "느림", run=lambda a: time.sleep(2) or "x", timeout_s=1)])
    with pytest.raises(ToolError) as e:
        reg.run(ToolCall("fs.read", {}))
    assert "took longer than 1s" in str(e.value)

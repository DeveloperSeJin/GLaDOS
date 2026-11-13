"""터미널 화면과 손 목록 시험."""

from __future__ import annotations

import io
import time
from datetime import datetime

import pytest

from conftest import FakeLLM, act, answer, make_places, verdict
from glados.core.checker import Checker
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
                   translation("모델은 qwen3.5:4b입니다.")]
    result = a.ask_task("지금 모델이 뭐야?")
    text = out.getvalue()
    assert result.status == "done"
    assert "모델은 qwen3.5:4b입니다." in text and "Small, but diligent." in text      # 위트는 영어 그대로 (M6.3)
    assert "● GLaDOS\n모델은 qwen3.5:4b입니다.\nSmall, but diligent." in text      # 누가 말하는지 (M6.3)
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
        answer("There are 0 PDF files in Downloads."),                     # 근거 없이 단정 -> 답 점검이 되돌림
        verdict(claims_supported="The earlier search found 2 PDFs; nothing was read."),
        act("fs.read", path=PAPER),
        answer("The thesis is about turning EEG into sentences.", "", [PAPER]),
        verdict(),
        translation("학위논문은 뇌파로 문장을 만드는 연구입니다."),
    ]
    a.ask_task("다운로드 폴더에서 논문 하나 찾아줘")
    a.checker = Checker(a.llm)                                             # 답 점검 (M9.3)
    a.ask_task("찾은 PDF 하나 읽고 요약해줘")
    text = out.getvalue()
    assert "답을 점검해 고치게 했습니다 (근거)" in text and "0 PDF" not in text
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
    assert a.memory.turns == [] and "앞의 대화는 저장해 두었습니다" in out.getvalue()
    a.llm.steps = [answer("Third answer.")]
    a.ask_task("third question")
    assert not any(m["content"] == "first question" for m in a.llm.calls[-1][0])
    # 지난 대화 이어하기 (M6): 목록에는 지금 세션이 빠진다
    a.command("/resume")
    text = out.getvalue()
    assert "1. " in text and "first question (작업 2개)" in text and "third question (작업" not in text
    a.command("/resume 5")
    assert "/resume 5: 목록의 번호(1~1)를 쓰세요" in out.getvalue()
    a.command("/resume 1")
    assert "이어서 합니다: " in out.getvalue() and "마지막 작업: second question" in out.getvalue()
    assert [turn.request for turn in a.memory.turns] == ["first question", "second question"]
    a.llm.steps = [answer("Fourth answer.")]
    a.ask_task("fourth question")
    assert any(m["content"] == "first question" for m in a.llm.calls[-1][0])     # 불러온 대화가 맥락에 있다
    assert [t.request for t in a.sessions.turns(a.hctx.session_id)] == ["first question", "second question",
                                                                         "fourth question"]
    a.command("/resume")
    assert "third question (작업 1개)" in out.getvalue()


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
    assert a.command("/servers") is False and "설정된 서버가 없습니다" in out.getvalue()
    assert a.command("/runs") is False and out.getvalue().count("설정된 서버가 없습니다") == 2   # 서버가 없으면 실행도 없음
    assert a.command("/resume") is False and "이어 갈 지난 대화가 없습니다" in out.getvalue()
    assert a.command("/memory") is False and "기억한 것이 없습니다" in out.getvalue()
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
    assert reg.names() == ["fs.search", "fs.inspect", "fs.read", "fs.write_new", "fs.edit", "shell.run", "web.search", "web.fetch",
                           "papers.search", "session.resume", "memory.note"]
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


def test_egress_lines_sent_command_and_web_note(app, monkeypatch):
    from glados.core import events as ev
    from glados.core.events import Event

    a, out = app
    lines = cli.event_lines(Event(ev.EGRESS, {"tool": "web.search", "host": "api.tavily.com",
                                              "what": "LLM evaluation metrics (past year)"}))
    assert lines == [("dim", '↗ 외부 전송 · api.tavily.com · 검색어 "LLM evaluation metrics (past year)"')]
    lines = cli.event_lines(Event(ev.EGRESS, {"tool": "web.fetch", "host": "arxiv.org", "what": "https://arxiv.org/abs/1"}))
    assert lines == [("dim", "↗ 외부 전송 · arxiv.org · 페이지 읽기 https://arxiv.org/abs/1")]
    a.command("/sent")
    assert "밖으로 보낸 것이 없습니다" in out.getvalue()
    a.hctx.egress.record("web.search", "api.tavily.com", "LLM evaluation metrics", "Tavily", "T1")
    a.hctx.egress.record("web.fetch", "arxiv.org", "https://arxiv.org/abs/2610.00001", task="T1")
    a.command("/sent")
    text = out.getvalue()
    assert "이 세션의 외부 전송 2건 (웹 검색 1건)" in text and "검색  api.tavily.com  LLM evaluation metrics" in text
    assert "읽기  arxiv.org  https://arxiv.org/abs/2610.00001" in text and "egress.jsonl" in text
    assert a.web_note().startswith("웹 검색이 꺼져 있습니다")
    a.cfg.web.search = "tavily"
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert "TAVILY_API_KEY" in a.web_note()
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert a.web_note().startswith("웹 검색: Tavily (api.tavily.com)")


def test_task_events_from_hands_reach_the_screen_and_log(app):
    """웹 손이 보내기 직전에 알린 외부 전송이 화면과 작업 기록에 남고, 작업이 끝나면 손의 알림 통로를 닫는다."""
    from glados.core import events as ev
    from glados.core.events import Event

    a, out = app

    def fake_search(args):
        a.hctx.egress.record("web.search", "api.tavily.com", args["query"], "Tavily", a.hctx.task_id)
        a.hctx.emit(Event(ev.EGRESS, {"tool": "web.search", "host": "api.tavily.com", "what": args["query"]}))
        return "Web search (Tavily, sent to api.tavily.com): \"x\" · 0 results\nNo results."

    a.tools = ToolRegistry([Tool("web.search", "search", run=fake_search)])
    a.llm.steps = [act("web.search", query="LLM evaluation metrics"), answer("Nothing found.")]
    a.ask_task("search the web for LLM evaluation metrics")
    assert '↗ 외부 전송 · api.tavily.com · 검색어 "LLM evaluation metrics"' in out.getvalue()
    task_id = a.task_log.recent(1)[0][0]
    kinds = [r[0] for r in a.task_log.db.execute("SELECT kind FROM events WHERE task_id = ?", (task_id,))]
    assert "egress" in kinds and a.hctx.emit is None


def test_started_in_glados_folder_moves_to_the_workspace(app, tmp_path):
    """uv run glados는 늘 소스 폴더에서 열린다. 거기서는 파일·명령이 막히므로 작업 폴더를 작업 공간으로 옮긴다."""
    a, _ = app
    docs = tmp_path.parent / (tmp_path.name + "-docs")
    docs.mkdir()
    a.hctx.user_dirs = {"documents": (str(docs),)}
    assert a.workspace() == str(docs / "GLaDOS")                  # 기본: 문서 폴더 안의 GLaDOS
    a.cfg.workspace = str(tmp_path.parent / (tmp_path.name + "-ws"))
    assert a.workspace() == a.cfg.workspace                        # folders.toml workspace가 먼저
    a.set_cwd(str(tmp_path / "logs"))                              # GLADOS_HOME 안 (안전장치)
    assert a.cwd_is_safety()
    assert a.leave_safety_cwd(dev=False) == "" and a.cwd_is_safety()   # 설치한 glados: 연 폴더 그대로 (M6.2)
    moved = a.leave_safety_cwd(dev=True)                           # uv run glados (저장소의 .venv)
    assert moved == a.cfg.workspace and a.places.cwd == moved and a.hctx.places.cwd == moved
    assert (tmp_path.parent / (tmp_path.name + "-ws")).is_dir() and not a.cwd_is_safety()
    assert a.leave_safety_cwd(dev=True) == ""                      # 이미 밖이면 그대로
    assert "(relative paths and commands start here): " + moved in a.system_prompt()

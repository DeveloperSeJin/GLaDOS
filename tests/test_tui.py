"""TUI 화면 시험 (Textual을 화면 없이 돌린다: App.run_test + Pilot).

- Workbench 자리: 머리글(존재감·상태·칩), 작업 목록, 작업 카드, 맥락(과정·찾은 범위·출처·기억), 처음 화면
- 승인 상자 (한 줄 버튼, 숫자 키, 뜬 직후 숫자·Enter 무시, ←→, Esc 거절), 되돌리기 상자
- 보내기 버튼 (글이 있으면 켜짐, 작업 중에는 멈춤)
- 멈춤 (모델 호출 중, 승인 중), 끝내기
- !명령, /명령, 작업 중 입력, 여러 줄 붙여넣기, ↑↓
- 로고 (리사주 점자 심볼, 워드마크), 움직임 설정
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import threading
import time

import pytest

from conftest import FakeLLM, act, answer
from glados import cli, i18n, logo
from glados.config import ConfigError, ensure_home, load_config
from glados.core.llm import Cancelled
from glados.hands import Tool, ToolRegistry
from glados import tui
from glados.tui import ChatInput, ChoiceDialog, GladosTUI, NavItem, TaskCard

pytest.importorskip("textual")

SIZE = (120, 36)


def translation(fact: str, wit: str = "") -> dict:
    return {"fact": fact, "wit": wit}


class BlockingLLM(FakeLLM):
    """block_at번째 호출에서 멈춤 신호가 올 때까지 기다리다 Cancelled (스트리밍 클라이언트 흉내)."""

    def __init__(self, steps, block_at: int = 0):
        super().__init__(steps)
        self.block_at = block_at
        self.stop_event: threading.Event | None = None
        self.started = threading.Event()

    def chat_json(self, messages, schema, validate, retries=2, temperature=0.2):
        if len(self.calls) == self.block_at:
            self.calls.append((list(messages), schema))
            self.started.set()
            deadline = time.monotonic() + 10
            while not (self.stop_event and self.stop_event.is_set()):
                if time.monotonic() > deadline:
                    raise RuntimeError("never stopped")
                time.sleep(0.01)
            raise Cancelled()
        return super().chat_json(messages, schema, validate, retries, temperature)


class MeteredLLM(FakeLLM):
    """Ollama 스트리밍처럼 계량기에 적는 가짜 모델: 부를 때마다 받은 글을 조각으로, 끝나면 ↑100 ↓7."""

    meter = None

    def chat_json(self, messages, schema, validate, retries=2, temperature=0.2):
        meter = self.meter
        if meter is not None:
            meter.begin()
        try:
            item = self.steps[0] if self.steps else None
            if meter is not None and isinstance(item, dict):
                meter.piece(json.dumps(item, ensure_ascii=False))
            out = super().chat_json(messages, schema, validate, retries, temperature)
            if meter is not None:
                meter.end(100, 7)
            return out
        finally:
            if meter is not None:
                meter.end()


class BlockingMeteredLLM(BlockingLLM):
    """멈출 때까지 기다리는 호출 중에 토큰 두 조각을 받은 상태."""

    meter = None

    def chat_json(self, messages, schema, validate, retries=2, temperature=0.2):
        if len(self.calls) == self.block_at and self.meter is not None:
            self.meter.begin()
            self.meter.piece('{"plan": ["Search Downloads')
            self.meter.piece(' for PDFs."], "thought": "')
        return super().chat_json(messages, schema, validate, retries, temperature)


def make(tmp_path, llm, tools=None, language="ko", first_run=False) -> GladosTUI:
    """설정 폴더(안전장치)와 작업 폴더를 따로 둔다 (작업 폴더가 안전장치 안이면 명령이 막힌다)."""
    home, work = tmp_path / "glados_home", tmp_path / "work"
    work.mkdir(exist_ok=True)
    ensure_home(home)
    cfg = load_config(home)
    cfg.language = language
    app = GladosTUI(cfg, llm=llm, os_name="linux", check_model=False, first_run=first_run)
    app.engine.set_cwd(str(work))
    if tools is not None:
        app.engine.tools = tools
    return app


def chat_text(app: GladosTUI) -> str:
    """가운데에 보이는 글: 처음 화면의 안내 + 카드들 (윗줄·제목·본문·출력)."""
    parts = [app.home.notes.plain()] if app.home.display else []
    for card in app.query(TaskCard):
        parts.append(str(card.kicker.render()))
        parts.append(card.text())
    return "\n".join(parts)


def activity_text(app: GladosTUI) -> str:
    """과정 기록의 글 (좁은 칸에서 줄바꿈된 것을 다시 이어 붙임)."""
    log = app.query_one("#activity")
    return "".join(strip.text for strip in log.lines)


async def until(pilot, cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await pilot.pause(0.02)


async def send(pilot, text: str) -> None:
    pilot.app.query_one(ChatInput).value = text
    await pilot.press("enter")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 답
def test_answer_shows_in_chat_and_panel(tmp_path):
    tools = ToolRegistry([Tool("fs.search", "find", run=lambda a: "1 found (scope: ~/Downloads)\n- /d/학위논문.pdf · 1KB")])
    llm = FakeLLM([act("fs.search", query="paper", kind="pdf"),
                   answer("Found one paper: 학위논문.pdf.", "Small sample.", ["/d/학위논문.pdf"]),
                   translation("논문 1개를 찾았습니다: 학위논문.pdf.")])
    app = make(tmp_path, llm, tools)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press(*"다운로드에서 논문 찾아줘", "enter")
            await until(pilot, lambda: not app.job and len(app.engine.memory.turns) == 1)
            card = app.query_one(TaskCard)
            assert card.title_text == "다운로드에서 논문 찾아줘" and card.state == "done" and card.tools == 1
            assert str(card.kicker.render()).startswith("작업 · 완료 · ") and "도구 1번" in str(card.kicker.render())
            text = chat_text(app)
            assert "논문 1개를 찾았습니다: 학위논문.pdf." in text
            assert "출처: /d/학위논문.pdf" in text and "Found one paper" not in text
            # 누가 말하는지 (M6.3): 제목은 "연구원님" 이름표, 답 앞에는 GLaDOS의 코발트 점과 이름, 위트는 영어
            assert str(card.heading.render()) == "연구원님  다운로드에서 논문 찾아줘"
            assert "● GLaDOS\n논문 1개를 찾았습니다: 학위논문.pdf.\nSmall sample." in card.body.plain()
            assert "... fs.search" not in text                         # 과정은 맥락에
            panel = activity_text(app)
            assert "-> fs.search query=paper kind=pdf" in panel and "<- 1 found" in panel
            assert "1. Call fs.search." in panel and "답함 · 번역" in panel
            assert app.state_text.startswith("완료 · ") and app.presence_kind == "done" and app.mem_stats[0] == 1
            assert "지난 작업 1개" in str(app.query_one("#memory").render())
            assert not app.home.display                                # 처음 화면은 접힘
            assert str(app.query_one("#scope").render()) == "✓ ~/Downloads"          # 찾은 범위
            assert "[1] /d/학위논문.pdf" in str(app.query_one("#sources").render())  # 출처
            await until(pilot, lambda: len(app.query(NavItem)) == 1 and app.query_one(NavItem).parent.id == "nav-done")
            item = app.query_one(NavItem)                               # 작업 목록의 '이번 대화' (옮기기가 끝난 뒤)
            assert "다운로드에서" in str(item.render()) and item.has_class("selected")
    run(go())
    app.engine.close()


def test_panel_hidden_shows_steps_in_chat(tmp_path):
    tools = ToolRegistry([Tool("fs.search", "find", run=lambda a: "0 found")])
    llm = FakeLLM([act("fs.search", query="x"), answer("Nothing found.")])
    app = make(tmp_path, llm, tools, language="en")

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("f2")
            assert not app.aside_visible()
            await send(pilot, "Find x")
            await until(pilot, lambda: not app.job and app.engine.memory.turns)
            assert "... fs.search query=x" in chat_text(app) and "Nothing found." in chat_text(app)
            await pilot.press("f2")
            assert app.aside_visible()
    run(go())
    app.engine.close()


def test_panels_follow_window_width(tmp_path):
    app = make(tmp_path, FakeLLM([]))

    async def go():
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            assert not app.aside_visible() and not app.query_one("#nav").display
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            assert app.aside_visible() and not app.query_one("#nav").display
            await pilot.press("f3")                                     # 끄기
            await pilot.press("f3")                                     # 켜기: 좁다는 안내
            assert any("가로 110칸" in n.message for n in app._notifications)
            await pilot.resize_terminal(120, 30)
            await pilot.pause()
            assert app.query_one("#nav").display
    run(go())
    app.engine.close()


# ---------------------------------------------------------------- 승인
def shell_tools(ran: list) -> ToolRegistry:
    def run_shell(args):
        ran.append(args["command"])
        return f"$ {args['command']}\nexit 0 · 0.1s · bash · /tmp\nok"
    return ToolRegistry([Tool("shell.run", "run", run=run_shell)])


def test_approval_once_with_digit(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "KEY_GUARD_S", 0)
    ran: list = []
    llm = FakeLLM([act("shell.run", command="git push"), answer("Pushed.")])
    app = make(tmp_path, llm, shell_tools(ran), language="en")

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "push it")
            await until(pilot, lambda: isinstance(app.screen, ChoiceDialog))
            assert app.state_text == "Decision needed · shell.run" and app.presence_kind == "wait"
            assert app.query_one(TaskCard).state == "waiting"
            assert app.screen.focused.id == "pick-deny"                 # Enter는 거절 쪽
            await pilot.press("1")
            await until(pilot, lambda: not app.job and app.engine.memory.turns)
            assert ran == ["git push"]
            text = chat_text(app)
            assert "decision  shell.run command=git push" in text and "you: once" in text and "Pushed." in text
            assert "approval · you: once" in activity_text(app)
    run(go())
    app.engine.close()


def test_digit_right_after_dialog_is_ignored_then_escape_denies(tmp_path):
    ran: list = []
    llm = FakeLLM([act("shell.run", command="git push"), answer("Not pushed.")])
    app = make(tmp_path, llm, shell_tools(ran))

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "푸시해")
            await until(pilot, lambda: isinstance(app.screen, ChoiceDialog))
            app.screen.opened = time.monotonic() + 30                    # 느린 컴퓨터에서도 "뜬 직후"로 (시험 시간과 무관하게)
            await pilot.press("1")                                       # 치던 글의 숫자: 받지 않음
            await pilot.pause(0.1)
            assert isinstance(app.screen, ChoiceDialog)
            await pilot.press("escape")                                  # 거절
            await until(pilot, lambda: not app.job and app.engine.memory.turns)
            assert ran == [] and "연구원님:거절" in chat_text(app)
    run(go())
    app.engine.close()


def test_dialog_buttons_are_one_line_and_enter_waits_for_guard(tmp_path):
    ran: list = []
    llm = FakeLLM([act("shell.run", command="git push"), answer("Not pushed.")])
    app = make(tmp_path, llm, shell_tools(ran))

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "푸시해")
            await until(pilot, lambda: isinstance(app.screen, ChoiceDialog))
            dialog = app.screen
            await pilot.pause()
            pills = list(dialog.query(tui.Pill))
            assert pills and all(p.region.height == 1 for p in pills)          # 세 줄 상자가 아닌 한 줄 버튼
            assert pills[0].label_text == "이번만 허용" and pills[0].key == "1" and pills[0].has_class("decide")
            assert pills[-1].label_text == "거절" and pills[-1].key == "3"
            assert str(pills[-1].render()) == "거절  3"
            dialog.opened = time.monotonic() + 30                        # 느린 컴퓨터에서도 "뜬 직후"로
            await pilot.press("enter")                                   # 뜬 직후의 Enter: 받지 않음
            await pilot.pause(0.1)
            assert app.screen is dialog
            await pilot.press("left")                                    # ←→로 버튼 사이를 옮긴다
            assert dialog.focused.id != "pick-deny"
            await pilot.press("right")
            assert dialog.focused.id == "pick-deny"
            dialog.opened -= 60                                          # 잠시 뒤
            await pilot.press("enter")                                   # 포커스(거절)를 누른다
            await until(pilot, lambda: not app.job and app.engine.memory.turns)
            assert ran == [] and "연구원님:거절" in chat_text(app)
    run(go())
    app.engine.close()


def test_send_button_lights_up_and_turns_into_stop(tmp_path):
    llm = BlockingLLM([], block_at=0)
    app = make(tmp_path, llm)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            button = app.query_one("#send", tui.Pill)
            assert button.label_text == "보내기" and button.key == "↵" and not button.has_class("ready")
            assert button.region.height == 1 and app.query_one("#composer").region.height == 3
            await pilot.press(*"안녕")
            assert button.has_class("ready")                             # 글이 있으면 켜진다
            app.query_one(ChatInput).value = ""
            await pilot.pause()
            assert not button.has_class("ready")
            await send(pilot, "오래 걸리는 일")
            await until(pilot, llm.started.is_set)
            assert button.label_text == "멈춤" and button.key == "Esc" and button.has_class("stop")
            await pilot.click("#send")                                   # 작업 중에는 멈춤 버튼
            await until(pilot, lambda: not app.job)
            assert app.state_text == "멈춤" and app.query_one(TaskCard).state == "stopped"
            assert button.label_text == "보내기" and not button.has_class("stop")
            assert app.focused is app.query_one(ChatInput)
    run(go())
    app.engine.close()


def test_task_choice_only_when_grantable(tmp_path):
    from glados.safety.policy import Decision, ToolCall
    d = Decision("ask", "기본:shell.run", "허용 목록 밖", grantable=False, key="policy.ask_shell")
    box = tui.approval_dialog(ToolCall("shell.run", {"command": "x"}), d)
    assert [o[0] for o in box.options] == ["once", "deny"] and "2" not in box.shortcuts


def test_ctrl_c_during_approval_stops_task(tmp_path):
    ran: list = []
    llm = FakeLLM([act("shell.run", command="git push"), answer("never")])
    app = make(tmp_path, llm, shell_tools(ran))

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "푸시해")
            await until(pilot, lambda: isinstance(app.screen, ChoiceDialog))
            await pilot.press("ctrl+c")
            await until(pilot, lambda: not app.job)
            assert ran == [] and "연구원님이 멈췄습니다." in chat_text(app)
            assert len(llm.calls) == 1                                    # 멈춘 뒤 모델을 다시 부르지 않음
            turn = app.engine.memory.turns[-1]
            assert turn.stop_note == "(stopped: Stopped by the Researcher.)"
            assert "The Researcher denied this" in turn.messages[1]["content"]
    run(go())
    app.engine.close()


# ---------------------------------------------------------------- 멈춤·끝내기
def test_plan_shows_in_process_panel(tmp_path):
    tools = ToolRegistry([Tool("fs.search", "find", run=lambda a: "0 found (scope: ~/Downloads)")])
    plan = ["Search Downloads by name.", "Report what was found."]
    llm = FakeLLM([{**act("fs.search", query="paper"), "plan": plan}, answer("Nothing found.")])
    app = make(tmp_path, llm, tools)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "논문 찾아줘")
            await until(pilot, lambda: not app.job and app.engine.memory.turns)
            text = activity_text(app)
            assert "계획" in text and "1) Search Downloads by name." in text and "2) Report what was found." in text
            assert "Search Downloads by name." not in chat_text(app)          # 맥락이 보이면 카드에는 넣지 않음
    run(go())
    app.engine.close()


def test_elapsed_time_and_live_tokens_while_working(tmp_path):
    llm = BlockingMeteredLLM([], block_at=0)
    app = make(tmp_path, llm)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "오래 걸리는 일")
            await until(pilot, llm.started.is_set)
            app.refresh_live()
            await pilot.pause()
            card = app.query_one(TaskCard)
            live = str(card.live.render())
            assert card.live.display and live.startswith("↑ 0   ↓ 2 토큰")          # 건넨·받은 토큰
            assert 'Search Downloads for PDFs."], "thought": "' in live               # 받고 있는 글
            assert re.search(r"진행 중 · 1단계 · \d+초", str(card.kicker.render()))   # 흐른 시간
            assert re.search(r"작업 중 · 생각 · 1단계 · \d+초$", str(app.query_one("#status").render()))
            card.started -= 65
            app.refresh_live()
            assert re.search(r"1분 \d+초", str(card.kicker.render()))
            await pilot.press("escape")
            await until(pilot, lambda: not app.job)
            assert not card.live.display                                               # 멈춰도 지운다
            assert "진행 중" not in str(card.kicker.render())
    run(go())
    app.engine.close()


def test_live_tokens_are_replaced_by_the_answer(tmp_path):
    tools = ToolRegistry([Tool("fs.search", "find", run=lambda a: "1 found (scope: ~/Downloads)\n- /d/a.pdf · 1KB")])
    llm = MeteredLLM([act("fs.search", query="pdf"), answer("Found one PDF: a.pdf.", "", ["/d/a.pdf"]),
                      translation("PDF 1개를 찾았습니다: a.pdf.")])
    app = make(tmp_path, llm, tools)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "PDF 찾아줘")
            await until(pilot, lambda: not app.job and app.engine.memory.turns)
            card = app.query_one(TaskCard)
            assert not card.live.display                                               # 중간 토큰은 지우고
            text = chat_text(app)
            assert "PDF 1개를 찾았습니다: a.pdf." in text and '"thought"' not in text  # 답만 남는다
            assert "↑ 300  ↓ 21 토큰" in activity_text(app)                             # 과정에는 합계 (부른 3번)
    run(go())
    app.engine.close()


def test_escape_stops_model_call(tmp_path):
    llm = BlockingLLM([], block_at=0)
    app = make(tmp_path, llm)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "오래 걸리는 일")
            await until(pilot, llm.started.is_set)
            assert app.job == "task" and "Esc" in str(app.query_one("#keys").render())
            await pilot.press("escape")
            await until(pilot, lambda: not app.job)
            assert "연구원님이 멈췄습니다." in chat_text(app) and app.state_text == "멈춤"
            assert app.query_one(TaskCard).state == "stopped" and app.counts["stopped"] == 1
            turn = app.engine.memory.turns[-1]
            assert turn.request == "오래 걸리는 일" and turn.stop_note == "(stopped: Stopped by the Researcher.)"
    run(go())
    app.engine.close()


def test_busy_input_is_kept_and_commands_wait(tmp_path):
    llm = BlockingLLM([], block_at=0)
    app = make(tmp_path, llm)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "첫 번째")
            await until(pilot, llm.started.is_set)
            await send(pilot, "두 번째")
            assert app.query_one(ChatInput).value == "두 번째"            # 지우지 않고 둠
            assert any("작업 중입니다" in n.message for n in app._notifications)
            await pilot.press("escape")
            await until(pilot, lambda: not app.job)
            assert len(llm.calls) == 1
    run(go())
    app.engine.close()


def test_quit_while_working_ends_cleanly(tmp_path):
    llm = BlockingLLM([], block_at=0)
    app = make(tmp_path, llm)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "오래 걸리는 일")
            await until(pilot, llm.started.is_set)
            await pilot.press("ctrl+q")
            await pilot.pause(0.2)
    start = time.monotonic()
    run(go())                                                            # 작업 스레드가 남아 있으면 여기서 멈춘다
    assert time.monotonic() - start < 8 and app.closing and len(llm.calls) == 1
    app.engine.close()


# ---------------------------------------------------------------- !명령, /명령
@pytest.mark.skipif(sys.platform == "win32", reason="bash echo")
def test_direct_command_output_and_memory(tmp_path):
    app = make(tmp_path, FakeLLM([]))

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "!echo hello-tui")
            await until(pilot, lambda: not app.job and app.engine.memory.turns)
            card = app.query_one(TaskCard)
            assert card.kind == "command" and card.title_text == "!echo hello-tui"
            assert card.output.plain() == "hello-tui" and card.code == 0
            assert "종료 코드 0" in str(card.kicker.render()) and app.counts["commands"] == 1
            await send(pilot, "!cd " + str(tmp_path / "nope"))
            await pilot.pause(0.05)
            assert "폴더가 없습니다" in chat_text(app)
    run(go())
    app.engine.close()


def test_commands_help_lang_new(tmp_path):
    app = make(tmp_path, FakeLLM([answer("Hello, Researcher."), translation("안녕하세요, 연구원님.")]))
    app.engine.tools = ToolRegistry()

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("f1")
            assert "/context" in chat_text(app) and "TUI 키" in chat_text(app)
            await send(pilot, "안녕")
            await until(pilot, lambda: not app.job and app.engine.memory.turns)
            assert "안녕하세요, 연구원님." in chat_text(app)
            await send(pilot, "/lang en")
            assert app.query_one(ChatInput).placeholder.startswith("Go on")
            assert str(app.query_one("#activity-title").render()) == "PROCESS"
            assert str(app.query_one(TaskCard).kicker.render()).startswith("BUILT-IN")
            await send(pilot, "/new")
            assert app.mem_stats == (0, 0) and "No conversation in memory." in str(app.query_one("#memory").render())
            assert app.home.display and not app.query(TaskCard) and not app.query(NavItem)
            assert app.query_one(ChatInput).placeholder == "Ask anything"
            await send(pilot, "/nope")
            assert "Unknown command: /nope" in chat_text(app)
    run(go())
    i18n.set_language("ko")
    app.engine.close()


def test_undo_dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "KEY_GUARD_S", 0)
    app = make(tmp_path, FakeLLM([]))

    class CP:
        original, ts, task = str(tmp_path / "a.md"), time.time(), "T1"

    restored = []
    monkeypatch.setattr(app.engine, "last_checkpoint", lambda: CP())
    monkeypatch.setattr(app.engine, "restore", lambda cp: restored.append(cp) or cp.original)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "/undo")
            await until(pilot, lambda: isinstance(app.screen, ChoiceDialog))
            await pilot.press("1")
            await until(pilot, lambda: not isinstance(app.screen, ChoiceDialog))
            assert restored and "되돌렸습니다" in chat_text(app)
    run(go())
    app.engine.close()


# ---------------------------------------------------------------- 입력 칸
def test_multiline_paste_is_sent_in_full(tmp_path):
    from textual import events
    llm = FakeLLM([answer("It is a traceback."), translation("트레이스백입니다.")])
    app = make(tmp_path, llm, ToolRegistry())

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            box = app.query_one(ChatInput)
            await pilot.press(*"이 오류 설명해줘 ")
            box.post_message(events.Paste("Traceback:\n  File x\nValueError: bad"))
            await pilot.pause(0.05)
            assert box.value == "이 오류 설명해줘 [붙여넣은 글 1 · 3줄]"
            await pilot.press("enter")
            await until(pilot, lambda: not app.job and app.engine.memory.turns)
            sent = llm.calls[0][0][-1]["content"]
            assert "Traceback:\n  File x\nValueError: bad" in sent and "붙여넣은 글" not in sent
            assert app.query_one(TaskCard).title_text == "이 오류 설명해줘 [붙여넣은 글 1 · 3줄]"
    run(go())
    app.engine.close()


def test_history_keys(tmp_path):
    app = make(tmp_path, FakeLLM([]))

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "/help")
            await send(pilot, "/context")
            box = app.query_one(ChatInput)
            await pilot.press("up")
            assert box.value == "/context"
            await pilot.press("up")
            assert box.value == "/help"
            await pilot.press("down", "down")
            assert box.value == ""
    run(go())
    app.engine.close()


# ---------------------------------------------------------------- 시작 화면·설정
def test_wants_tui(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    assert cli.wants_tui("", "tui") and cli.wants_tui("tui", "plain")
    assert not cli.wants_tui("plain", "tui") and not cli.wants_tui("", "plain")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert not cli.wants_tui("tui", "tui")                              # 파이프면 줄 화면


def test_screen_setting(tmp_path):
    ensure_home(tmp_path)
    assert load_config(tmp_path).screen == "tui"
    text = (tmp_path / "glados.toml").read_text(encoding="utf-8")
    (tmp_path / "glados.toml").write_text(text.replace('screen = "tui"', 'screen = "plain"'), encoding="utf-8")
    assert load_config(tmp_path).screen == "plain"
    (tmp_path / "glados.toml").write_text(text.replace('screen = "tui"', 'screen = "fancy"'), encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(tmp_path)
    assert e.value.key == "config.screen"


def test_start_screen_logo_greeting_and_hint(tmp_path):
    app = make(tmp_path, FakeLLM([]), first_run=True)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await until(pilot, lambda: app.home.size_key == "mid")      # 배치가 끝날 때까지 (Windows는 느림)
            assert "처음 뵙겠습니다" in str(app.home.greet.render()) and "glados --plain" in chat_text(app)
            assert str(app.query_one("#wordmark").render()) == "G L a D O S"
            assert str(app.query_one("#home-wordmark").render()) == "\n".join(logo.WORDMARK_3)   # 120칸: 3줄 글자
            assert app.home.size_key == "mid" and app.presence_kind == "idle"
            assert str(app.query_one("#chip").render()) == "외부 전송 0 · 웹 검색 0"
            assert app.focused is app.query_one(ChatInput)
            assert app.query_one("#session").display and not app.query_one("#activity").display
            await pilot.resize_terminal(150, 40)
            await until(pilot, lambda: app.home.size_key == "big")   # 넓으면 큰 로고 (배치가 끝날 때까지 기다림: Windows는 느림)
            await pilot.resize_terminal(200, 50)
            await until(pilot, lambda: app.home.size_key == "xl")    # 아주 넓으면 더 큰 로고 (점이 많아 매끄럽다)
            assert str(app.query_one("#home-wordmark").render()) == "\n".join(logo.WORDMARK_5)
    run(go())
    app.engine.close()


def test_suggestion_fills_input_and_new_chat_button(tmp_path):
    app = make(tmp_path, FakeLLM([]))

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await until(pilot, lambda: app.home.size_key == "mid")      # 카드 자리가 정해진 뒤에 누른다
            await pilot.pause()
            await pilot.click("#suggest-file")
            await until(pilot, lambda: app.query_one(ChatInput).value == "다운로드에서 논문 찾아줘")
            await send(pilot, "/help")
            assert not app.home.display
            await pilot.click("#new-chat")
            assert app.home.display and not app.query(TaskCard)
    run(go())
    app.engine.close()


def test_remote_model_chip(tmp_path):
    home = tmp_path / "h"
    ensure_home(home)
    cfg = load_config(home)
    cfg.model.host = "http://192.168.0.7:11434"
    app = GladosTUI(cfg, llm=FakeLLM([]), os_name="linux", check_model=False)
    assert not app.local_model and app.model_host == "192.168.0.7"
    cfg.model.host = "http://localhost:11434"
    assert GladosTUI(cfg, llm=FakeLLM([]), os_name="linux", check_model=False).local_model


def test_presence_moves_only_while_working(tmp_path):
    llm = BlockingLLM([], block_at=0)
    app = make(tmp_path, llm)

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            assert app._timer._active.is_set() is False                  # 대기: 멈춤
            await send(pilot, "오래 걸리는 일")
            await until(pilot, llm.started.is_set)
            assert app.presence_kind == "work" and app._timer._active.is_set()
            first = str(app.query_one("#presence").render())
            await pilot.pause(0.4)
            assert str(app.query_one("#presence").render()) != first        # 점이 돌았다
            await pilot.press("escape")
            await until(pilot, lambda: not app.job)
            assert app.presence_kind == "done" and not app._timer._active.is_set()
    run(go())
    app.engine.close()


def test_motion_off_keeps_presence_still(tmp_path):
    llm = BlockingLLM([], block_at=0)
    app = make(tmp_path, llm)
    app.motion = False

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await send(pilot, "오래 걸리는 일")
            await until(pilot, llm.started.is_set)
            assert app.presence_kind == "work" and not app._timer._active.is_set()
            await pilot.press("escape")
            await until(pilot, lambda: not app.job)
    run(go())
    app.engine.close()


def test_motion_setting(tmp_path):
    ensure_home(tmp_path)
    assert load_config(tmp_path).motion is True
    text = (tmp_path / "glados.toml").read_text(encoding="utf-8")
    (tmp_path / "glados.toml").write_text(text.replace("motion = true", "motion = false"), encoding="utf-8")
    assert load_config(tmp_path).motion is False
    (tmp_path / "glados.toml").write_text(text.replace("motion = true", 'motion = "yes"'), encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(tmp_path)
    assert e.value.key == "config.motion"


# ---------------------------------------------------------------- 로고
def test_symbol_is_braille_grid_with_cobalt_dot():
    for kind in ("idle", "work", "wait", "done"):
        sym = logo.symbol(6, 3, logo.presence_at(kind, 0.5))
        rows = sym.plain.split("\n")
        assert len(rows) == 3 and all(len(r) == 6 for r in rows)
        assert all(ch == " " or 0x2800 <= ord(ch) <= 0x28FF for r in rows for ch in r)
        colors = {str(span.style) for span in sym.spans}
        assert logo.COBALT in colors                                    # 점은 늘 코발트
        assert (logo.CURVE_DIM in colors) == (kind in ("idle", "wait"))  # 대기·결정 대기는 흐린 곡선
    a = logo.symbol(16, 8, logo.presence_at("work", 0.0)).plain
    b = logo.symbol(16, 8, logo.presence_at("work", 1.0)).plain
    assert a != b                                                       # 작업 중이면 점이 움직인다
    assert logo.symbol(16, 8, logo.presence_at("done", 0)).plain == logo.symbol(16, 8, logo.presence_at("done", 5)).plain


def test_wordmark_rows_have_equal_width():
    for rows, lines in ((3, logo.WORDMARK_3), (4, logo.WORDMARK_4), (5, logo.WORDMARK_5)):
        assert len(lines) == rows and len({len(line) for line in lines}) == 1
        assert logo.wordmark_width(rows) == len(lines[0])


# ---------------------------------------------------------------- 웹 (M3)
def test_web_search_lights_the_chip_and_lists_what_was_sent(tmp_path, monkeypatch):
    """진짜 웹 손 + 이 컴퓨터에 띄운 가짜 검색 서비스: 머리글 칩, 과정, 오른쪽 "밖으로 보낸 것", /sent."""
    from test_web import Site

    from glados.hands import web

    site = Site()
    monkeypatch.setattr(web, "BASES", {"tavily": site.base + "/tavily"})
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    home, work = tmp_path / "glados_home", tmp_path / "work"
    work.mkdir()
    ensure_home(home)
    cfg = load_config(home)
    cfg.web.search = "tavily"
    llm = FakeLLM([{**act("web.search", query="LLM evaluation metrics", time_range="year"),
                    "plan": ["Search the web.", "Answer with URLs."]},
                   answer("Two recent papers.", "", ["https://arxiv.org/abs/2610.00001"]),
                   translation("최근 논문 2편입니다.")])
    app = GladosTUI(cfg, llm=llm, os_name="linux", check_model=False)
    app.engine.set_cwd(str(work))

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            assert str(app.query_one("#chip").render()) == "외부 전송 0 · 웹 검색 0"
            assert "웹 검색: Tavily (127.0.0.1:" in chat_text(app)                 # 처음 화면 안내
            await pilot.press(*"LLM 평가 지표 최신 논문 찾아줘", "enter")
            await until(pilot, lambda: not app.job and len(app.engine.memory.turns) == 1)
            assert str(app.query_one("#chip").render()) == "외부 전송 1 · 웹 검색 1"
            panel = activity_text(app)
            assert "↗ 127.0.0.1:" in panel and "LLM evaluation metrics (past year)" in panel
            card = app.query_one(TaskCard)
            assert card.egress == [("web.search", f"127.0.0.1:{site.server.server_address[1]}",
                                    "LLM evaluation metrics (past year)")]
            assert app.query_one("#egress").display and "LLM evaluation metrics" in str(app.query_one("#egress").render())
            assert "↗ 외부 전송" not in chat_text(app)                               # 맥락이 보이면 카드에는 답만
            assert "최근 논문 2편입니다." in chat_text(app)
            await send(pilot, "/sent")
            await until(pilot, lambda: "이 세션의 외부 전송 1건 (웹 검색 1건)" in chat_text(app))
    try:
        run(go())
    finally:
        app.engine.close()
        site.close()


def test_run_card_in_approval_box():
    """서버 실행 (M5): 승인 상자에 인자 대신 실행 카드 (GPU·명령·커밋·규칙), "이 작업 동안"은 없음."""
    from glados.safety.policy import Decision, ToolCall
    d = Decision("ask", "코드:서버실행", "서버에서 학습을 시작합니다", grantable=False, key="policy.remote_run")
    card = [("fact", "EXP-03 · server-a · GPU 1 (빔)"), ("plain", "명령  python train.py"),
            ("decision", "! 동시 사용 한도를 넘습니다")]
    box = tui.approval_dialog(ToolCall("remote.run", {"server": "server-a", "command": "python train.py"}, card=card), d)
    assert box.details.plain == "EXP-03 · server-a · GPU 1 (빔)\n명령  python train.py\n! 동시 사용 한도를 넘습니다"
    assert [o[0] for o in box.options] == ["once", "deny"]


def test_notice_card_does_not_take_over_the_task(tmp_path):
    """배경 감시 알림 (M5): 알림 카드가 생기고 토스트가 뜨지만, 지금 작업 카드(current)는 그대로."""
    from glados.hands.runner import Notice

    app = make(tmp_path, FakeLLM([]))
    sent = []
    app.engine.os_notify = sent.append

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            app.current = None
            app.show_notice(Notice("nan", "EXP-03", "notice.nan", {"run": "EXP-03", "line": "loss nan"}, decision=True))
            app.show_notice(Notice("done", "EXP-02", "notice.done", {"run": "EXP-02", "server": "server-a",
                                                                       "code": 0, "elapsed": "2시간 3분"},
                                   [("notice.last", {"line": "saved best.pt"})]))
            await pilot.pause(0.05)
            cards = [c for c in app.query(TaskCard) if c.kind == "notice"]
            assert len(cards) == 2 and cards[0].decision and not cards[1].decision
            text = chat_text(app)
            assert "결정 필요 · " in text and "EXP-03: 손실이 NaN입니다 (loss nan)" in text
            assert "EXP-02 끝남: server-a, 종료 코드 0, 2시간 3분" in text and "마지막 줄: saved best.pt" in text
            assert app.current is None and not app.home.display
            assert len(sent) == 2
    run(go())
    app.engine.close()


# ---------------------------------------------------------------- 이어하기·기억 (M6)
def test_resume_card_on_start_screen_and_resume_command(tmp_path):
    """지난 대화가 있으면 처음 화면에 이어하기 카드. 누르면 그 대화를 불러오고, /new 뒤에는 방금 대화가 카드로."""
    home = tmp_path / "glados_home"
    ensure_home(home)
    first = cli.App(load_config(home), cli.Screen(ansi=False, out=io.StringIO()), llm=FakeLLM([answer("Loss is 0.31.")]),
                    os_name="linux")
    first.ask_task("how is the cifar run")
    first.close()
    app = make(tmp_path, FakeLLM([answer("Epoch 7 was best.")]))

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            card = app.query_one("#suggest-resume")
            await until(pilot, lambda: app.home.size_key != "")
            await pilot.pause()
            assert card.display and "how is the cifar run" in str(card.render()) and "작업 1개" in str(card.render())
            await pilot.click("#suggest-resume")
            await until(pilot, lambda: len(app.engine.memory.turns) == 1)
            assert not app.home.display and "이어서 합니다: " in chat_text(app)
            assert app.mem_stats[0] == 1
            await send(pilot, "which epoch was best")
            await until(pilot, lambda: not app.job)
            assert len(app.engine.memory.turns) == 2
            await pilot.click("#new-chat")
            assert app.home.display and card.display and "how is the cifar run" in str(card.render())
            await send(pilot, "/resume")
            assert "1. " in chat_text(app) and "how is the cifar run (작업 2개)" in chat_text(app)
            await send(pilot, "/resume 1")
            await pilot.pause()
            assert len(app.engine.memory.turns) == 2 and len(app.query(TaskCard)) == 1
    run(go())
    app.engine.close()


def test_no_resume_card_without_earlier_conversation(tmp_path):
    app = make(tmp_path, FakeLLM([]))

    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            assert not app.query_one("#suggest-resume").display
    run(go())
    app.engine.close()


def test_memory_note_in_approval_box():
    """기억 (M6): 승인 상자에 저장할 글, 버튼은 기억하기·기억하지 않기."""
    from glados.safety.policy import Decision, ToolCall
    d = Decision("ask", "코드:기억", "이 내용을 기억할까요?", grantable=False, key="policy.memory_note")
    card = [("fact", "실험 결과는 표로"), ("dim", "~/.glados/MEMORY.md에 한 줄로 남깁니다")]
    box = tui.approval_dialog(ToolCall("memory.note", {"text": "실험 결과는 표로"}, card=card), d)
    assert box.details.plain.startswith("실험 결과는 표로\n")
    assert [(o[0], o[1]) for o in box.options] == [("once", "기억하기"), ("deny", "기억하지 않기")]

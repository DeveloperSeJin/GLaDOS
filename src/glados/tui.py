"""TUI 화면 (Textual) — Workbench 콘솔(디자인 캔버스 "Final · v1.0")을 터미널로 옮긴 것.

자리 (Workbench와 같음)
- 머리글: 존재감 심볼(리사주 + 코발트 점, logo.py) · GLaDOS · 지금 상태 한 줄 · 모델·OS·작업 폴더, 오른쪽에 칩 하나
  (외부 전송 · 웹 검색). 심볼은 대기면 1:2 흐린 곡선, 작업 중이면 3:2 곡선을 점이 돈다, 결정 대기면 점이 숨쉰다
- 왼쪽 작업 목록: + 새 대화, 진행 중, 이번 대화. 결정이 필요한 작업은 코발트 점. 누르면 그 작업으로 간다
- 가운데: 처음에는 로고·인사·제안 카드, 작업을 하면 작업 카드 (윗줄 · 제목(요청) · 사실 굵게 + 위트 흐리게 · 출처).
  아래에 입력 상자 하나 (입력과 보내기 버튼이 한 상자에). 처음에는 그 위에 할 수 있는 일 칩
- 버튼은 모두 한 줄 (Workbench의 버튼을 터미널 칸에 맞게): 결정은 꽉 찬 코발트, 보내기는 글이 있으면 꽉 찬 흰색,
  나머지는 옅은 바탕. 단축키는 글 뒤에 흐리게. 작업 중에는 보내기가 멈춤 버튼이 된다
- 창이 클수록 넓게: 가로 150·190칸에서 양쪽 패널이 넓어지고, 처음 화면 로고도 커진다 (점이 많아 곡선이 매끄럽다)
- 오른쪽 맥락: 과정(모델의 생각·도구·판정·결과 첫 줄), 찾은 범위, 출처, 기억. 처음에는 이번 세션과 이 컴퓨터
- 코발트는 두 곳에만: GLaDOS의 점, 연구원님의 결정 (승인 상자, 결정 기록, 결정 대기 표시)

동작
- 엔진 연결은 줄 화면과 같은 cli.App (같은 ask_task, /명령, !명령). 작업과 !명령은 스레드에서 돌고 이벤트는 화면 스레드로
- 승인·되돌리기는 상자. 숫자 1·2·3, ←→와 Enter, Esc 거절, 처음 자리는 거절, 뜬 직후 0.8초는 숫자·Enter를 받지 않는다
- Esc·Ctrl+C: 지금 작업만 멈춘다 (모델 호출은 스트리밍이라 곧 끊긴다, 명령은 프로세스 트리째 끝낸다)
- 대화처럼 (M8): 작업 카드에 "작업 · 완료 · 몇 초" 윗줄을 두지 않는다. 인사·답·되묻기는 말만 오가고, 도구를 쓰기
  시작하면 GLaDOS의 첫 말 아래에 진행 줄 하나("진행 중 · 2단계 · 0:12" -> "완료 · 39초 · 도구 4번")가 생긴다.
  계획 단계(영어)는 맥락의 과정에. 걸리는 시간은 머리글 상태 줄에도
- 마우스 (M7.9, 연구원님 요청): 끌어서 고른 뒤 오른쪽 클릭 = 복사, 고른 것이 없을 때 오른쪽 클릭 = 입력 칸에 붙여넣기
  (Windows 콘솔과 같은 방식), Ctrl+클릭 = 링크(http·https)를 브라우저로 연다. 클립보드는 운영 체제의 것
  (clipboard.py, 안 되면 터미널 복사 OSC 52). 글 속 주소는 링크 모양(밑줄)으로 그린다
- 경로 링크 (M8.1): 글 속의 파일·폴더 경로(C:\\..., /..., ~/...)도 실제로 있으면 밑줄 링크. Ctrl+클릭하면 폴더는 탐색기로,
  파일은 기본 프로그램으로 연다. 실행되는 파일(exe, bat, ps1, py, ...)은 열지 않고 폴더에서 골라 보여 준다 (opener.py).
  띄어쓰기가 든 경로는 있는 경로가 될 때까지 낱말을 이어 본다 (한글 조사가 붙어도 떼어 본다)
- 한글 입력(IME): 입력 칸에 있을 때 터미널 커서를 글자 자리에 보이게 둔다 (Windows Terminal은 커서가 숨으면
  조합 글자를 엉뚱한 곳에 그린다, Textual #5457). 움직이는 것은 머리글 심볼 하나뿐이고, 작업·결정 대기 중에만,
  입력 칸에 글이 있으면 멈춘다. [ui] motion = false면 늘 멈춤
- 줄 화면(glados --plain)은 그대로 있다
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any, Callable

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events, on, work
from textual.app import App as TextualApp
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

import os

from glados import clipboard, i18n, logo, opener, osinfo
from glados.cli import CD, DIRECT_LINES_SHOWN, STOPS, App, Direct, Screen, _brief, _Keyed, event_lines
from glados.core.llm import Meter
from glados.config import Config
from glados.core import events as ev
from glados.core.context import DROPPED
from glados.core.events import Event
from glados.hands import shell
from glados.i18n import localized, t
from glados.memory.log import summarize_args
from glados.safety.policy import Decision, ToolCall

# 브랜드 색 (brand-identity.md, Workbench)
VOID, PANEL, SURFACE = "#030303", "#070707", "#0B0B0B"
RULE, LINE, HOVER = "#1C1C1C", "#262626", "#161616"
DIM, FAINT, MUTED, WHITE, COBALT = "#5A5D62", "#8E9196", "#A9ADB3", "#F2F4F7", "#4D7CFF"
COBALT_HI, TRAIL_DARK = "#6A92FF", "#1D2D5B"     # 코발트 버튼의 누를 때 · 그 위 단축키 글자
STYLES = {
    "fact": f"bold {WHITE}",       # 사실은 굵게
    "wit": MUTED,                  # 위트는 흐리게
    "dim": FAINT,                  # 과정
    "plain": WHITE,
    "decision": f"bold {COBALT}",  # 연구원님의 결정
}
MAX_CARDS = 200           # 가운데에 남길 카드 수 (넘으면 오래된 것부터)
NAV_MIN = 110             # 이보다 좁으면 작업 목록을 숨긴다
ASIDE_MIN = 90            # 이보다 좁으면 맥락을 숨긴다
THOUGHT_SHOWN = 300       # 과정에 보여 줄 생각 길이
KEY_GUARD_S = 0.8         # 상자가 뜬 직후 이 시간 동안은 숫자 키를 받지 않는다 (치던 글이 승인이 되지 않게)
FRAME_S = 0.125           # 머리글 심볼이 움직일 때 한 장면 (초)
LIVE_EVERY = 2            # 몇 장면마다 흐른 시간·토큰을 다시 그릴지 (0.25초)
LIVE_LINES = 3            # 작업 중 토큰 칸의 글 줄 수
EGRESS_SHOWN = 6         # 오른쪽 맥락에 보여 줄 "밖으로 보낸 것" 수
HIDE_CURSOR, SHOW_CURSOR = "\x1b[?25l", "\x1b[?25h"
# 글 속 웹 주소 (Ctrl+클릭으로 연다). 괄호는 안에 있을 때만 (위키 주소), 끝의 문장 부호는 뺀다
URL_RE = re.compile(r"https?://[^\s<>\"'\[\]{}`가-힣ㄱ-ㅎㅏ-ㅣ]+")   # 한글 조사가 붙어도 주소는 거기까지
URL_TRAIL = ".,;:!?'\")]}>…"


def _urls(line: str) -> list[tuple[int, int, str]]:
    """줄 속 웹 주소 (시작, 끝, 주소)."""
    out = []
    for m in URL_RE.finditer(line):
        url = m.group(0)
        while url and url[-1] in URL_TRAIL and not (url[-1] == ")" and url.count("(") >= url.count(")")):
            url = url[:-1]
        if len(url) > len("https://"):
            out.append((m.start(), m.start() + len(url), url))
    return out


def url_at(line: str, col: int) -> str | None:
    """줄의 col번째 글자에 걸친 웹 주소."""
    for start, end, url in _urls(line):
        if start <= col < end:
            return url
    return None


# 글 속 파일·폴더 경로 (M8.1): Windows 드라이브 경로, 절대 경로, ~ 경로. 띄어쓰기를 넘어 이어질 수 있다
PATH_RE = re.compile(r"(?<![\w/\\:.~])(?:[A-Za-z]:[\\/]|~[\\/]|/(?=[^\s/]))[^\n\r\t\"'<>|*?,;`]*")
PATH_TRAIL = ".:!?)]}>…"
HANGUL_TAIL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]+$")
PATH_WORDS = 8               # 띄어쓰기가 든 경로: 이만큼 낱말까지 이어 본다
_exists: dict[str, bool] = {}


def path_exists(path: str) -> bool:
    """있는 경로인가 (화면을 그릴 때마다 디스크를 보지 않게 기억해 둔다)."""
    if path not in _exists:
        if len(_exists) > 4000:
            _exists.clear()
        try:
            _exists[path] = os.path.exists(os.path.expanduser(path))
        except (OSError, ValueError):
            _exists[path] = False
    return _exists[path]


def _paths(line: str) -> list[tuple[int, int, str]]:
    """줄 속의 실제로 있는 경로 (시작, 끝, 경로). 가장 길게 이어지는 것."""
    out = []
    for m in PATH_RE.finditer(line):
        span, best = m.group(0), ""
        ends = [w.end() for w in re.finditer(r"\S+", span)][:PATH_WORDS]
        for end in ends:
            head = span[:end].rstrip(PATH_TRAIL)
            for cand in (head, HANGUL_TAIL.sub("", head).rstrip(PATH_TRAIL)):
                if len(cand) > len(best) and len(cand) > 2 and path_exists(cand):
                    best = cand
        if best:
            out.append((m.start(), m.start() + len(best), best))
    return out


def path_at(line: str, col: int) -> str | None:
    for start, end, path in _paths(line):
        if start <= col < end:
            return path
    return None


def linkify(text: Text) -> Text:
    """글 속 웹 주소와 있는 경로에 링크 스타일 (밑줄 + 대상). 줄이 바뀌어도 조각마다 대상이 남아 Ctrl+클릭이 된다."""
    plain = text.plain
    for start, end, url in _urls(plain):
        text.stylize(Style(link=url, underline=True), start, end)
    for start, end, path in _paths(plain):
        text.stylize(Style(link=path, underline=True), start, end)
    return text
SCOPE = re.compile(r"\(scope: (.+)\)\s*$")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")

GLADOS_THEME = Theme(
    name="glados", primary=COBALT, secondary=MUTED, accent=COBALT, foreground=WHITE,
    background=VOID, surface=SURFACE, panel=LINE, dark=True,
)


def _secs(value: float) -> str:
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


def key_hints(spec: str) -> Text:
    """"Enter 보내기 · F1 도움말 · ..." → 키는 밝게, 설명은 흐리게 (맨 아래 줄)."""
    out = Text()
    for i, item in enumerate(spec.split(" · ")):
        key, _, label = item.partition(" ")
        if i:
            out.append("    ")
        out.append(key, style=WHITE)
        out.append(" " + label, style=FAINT)
    return out


def _clock(seconds: float) -> str:
    """흐른 시간: 47초 / 1분 5초."""
    s = max(0, int(seconds))
    return t("tui.time.s", s=s) if s < 60 else t("tui.time.ms", m=s // 60, s=s % 60)


def _tokens(n: int) -> str:
    return str(n) if n < 1000 else (f"{n / 1000:.1f}k" if n < 100_000 else f"{n // 1000}k")


def _tail(text: str, cells: int) -> str:
    """한 줄로 펴서 끝에서부터 cells칸 (한글은 두 칸). 잘렸으면 앞에 …"""
    flat = " ".join(text.split())
    out: list[str] = []
    used = 0
    for ch in reversed(flat):
        w = cell_len(ch)
        if used + w > cells:
            break
        out.append(ch)
        used += w
    tail = "".join(reversed(out))
    return tail if len(tail) == len(flat) else "…" + tail[1:]


def _clip(text: str, width: int) -> str:
    """화면 폭(한글은 두 칸)에 맞게 자른다."""
    out, used = "", 0
    for ch in text.replace("\n", " "):
        w = Text(ch).cell_len
        if used + w > width:
            return out.rstrip() + "…"
        out += ch
        used += w
    return out


# ---------------------------------------------------------------------- 엔진 쪽 자리
class TuiScreen(Screen):
    """엔진(cli.App)이 쓰는 화면 자리. 글을 TUI로 보낸다 (어느 스레드에서 불러도 된다)."""

    def __init__(self, ui: "GladosTUI"):
        super().__init__(ansi=False)
        self.ui = ui

    def bold(self, text: str) -> Text:          # type: ignore[override]
        return Text(text, style=STYLES["fact"])

    def dim(self, text: str) -> Text:           # type: ignore[override]
        return Text(text, style=STYLES["dim"])

    def cobalt(self, text: str) -> Text:        # type: ignore[override]
        return Text(text, style=STYLES["decision"])

    def styled(self, style: str, text: str) -> Text:   # type: ignore[override]
        return Text(text, style=STYLES.get(style, STYLES["plain"]))

    def say(self, text: Any = "") -> None:
        self.ui.post(self.ui.add_line, text)


class TuiEngine(App):
    """엔진 연결은 줄 화면 그대로. 이벤트 그리기와 승인만 TUI로."""

    def __init__(self, cfg: Config, ui: "GladosTUI", llm=None, os_name: str | None = None):
        self.ui = ui
        super().__init__(cfg, TuiScreen(ui), llm=llm, os_name=os_name)

    def render(self, e: Event) -> None:          # 작업 스레드에서 불린다
        self.ui.post(self.ui.show_event, e)

    def approve(self, call: ToolCall, d: Decision) -> str:   # 작업 스레드에서 불린다
        return self.ui.ask_approval(call, d)

    def notice(self, n) -> None:                  # 배경 감시 스레드에서 불린다 (M5)
        self.ui.post(self.ui.show_notice, n)


# ---------------------------------------------------------------------- 가운데: 카드
class Lines(Static):
    """줄을 더해 가는 글 묶음."""

    def __init__(self, *lines: Text, **kw: Any):
        super().__init__(**kw)
        self.lines: list[Text] = [linkify(line) for line in lines]
        if self.lines:
            self.update(Text("\n").join(self.lines))

    def add(self, line: Text) -> None:
        self.lines.append(linkify(line))
        self.update(Text("\n").join(self.lines))

    def plain(self) -> str:
        return "\n".join(line.plain for line in self.lines)

    def set(self, index: int, line: Text) -> None:
        """한 줄을 바꾼다 (작업 카드의 진행 줄)."""
        self.lines[index] = linkify(line)
        self.update(Text("\n").join(self.lines))


class LinkLog(RichLog):
    """과정 기록: 글 속 웹 주소를 링크로 (Ctrl+클릭)."""

    def write(self, content: Any, *args: Any, **kw: Any) -> "LinkLog":
        if isinstance(content, Text):
            linkify(content)
        super().write(content, *args, **kw)
        return self


class TaskCard(Vertical):
    """작업 하나 (Workbench 작업 화면): 윗줄 · 제목(요청) · 본문. !명령은 출력 상자를 함께."""

    def __init__(self, kind: str, title: str):
        super().__init__(classes=f"card {kind}")
        self.kind = kind                  # task | command | slash | info | notice (서버 실행 알림, M5)
        self.decision = False             # notice: 결정이 필요한 알림 (NaN, 시간 초과)
        self.when = ""                    # notice: 알린 시각
        self.title_text = title
        self.started = time.monotonic()
        self.state = "running"            # running | waiting | done | stopped | failed | timeout
        self.step = 1
        self.tools = 0
        self.seconds = 0.0
        self.code: int | None = None
        self.sources: list[str] = []
        self.scope: list[str] = []
        self.egress: list[tuple[str, str, str]] = []   # 이 작업이 밖으로 보낸 것 (도구, 받은 곳, 보낸 것)
        self.kicker = Static(classes="kicker")
        self.heading = Static(self.heading_text(), classes="title")
        self.body = Lines(classes="body")
        self.live = Static(classes="live")          # 작업 중에 주고받는 토큰 (답이 나오면 지움)
        self.output = Lines(classes="output")
        self.nav: NavItem | None = None
        self.progress_at: int | None = None         # 작업 카드: 본문 속 진행 줄의 자리 (도구를 쓰기 시작하면, M8)

    def heading_text(self) -> Text:
        """제목 = 연구원님이 쓴 것 (요청, !명령, /명령). 이름표를 붙여 GLaDOS의 답과 구별한다 (M6.3)."""
        if self.kind in ("task", "command", "slash"):
            return Text.assemble((t("speaker.you") + "  ", FAINT), (self.title_text, WHITE))
        return Text(self.title_text, style=f"bold {WHITE}")

    def compose(self) -> ComposeResult:
        yield self.kicker
        if self.title_text:
            yield self.heading
        yield self.body
        yield self.live
        yield self.output

    def on_mount(self) -> None:
        self.body.display = bool(self.body.lines)
        self.live.display = False
        self.output.display = bool(self.output.lines)
        self.kicker.display = self.kind != "task"   # 대화처럼 (M8): 작업 카드는 윗줄 없이, 일할 때만 본문의 진행 줄
        self.refresh_kicker()

    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.state in ("running", "waiting") else self.seconds

    def show_live(self, sent: int, received: int, text: str, active: bool) -> None:
        """건넨 토큰·받은 토큰과 지금 받고 있는 글의 끝부분 (세 줄)."""
        width = self.live.content_size.width or max(20, self.content_size.width - 2)
        body = _tail(text, max(20, width * LIVE_LINES - 1)) if text else (t("tui.live.reading") if active else "")
        head = Text.assemble(("↑ ", DIM), (_tokens(sent), FAINT), ("   ↓ ", DIM), (_tokens(received), FAINT),
                             (" " + t("tui.live.unit"), DIM))
        self.live.update(Text.assemble(head, "\n", (body, DIM)) if body else head)
        self.live.display = True

    def hide_live(self) -> None:
        self.live.display = False

    def add(self, line: Text) -> None:
        self.body.display = True
        self.body.add(line)

    def start_progress(self) -> None:
        """도구를 쓰기 시작했다: GLaDOS의 첫 말 아래에 진행 줄 (M8)."""
        if self.progress_at is None:
            self.add(self.progress_text())
            self.progress_at = len(self.body.lines) - 1

    def progress_text(self) -> Text:
        color = COBALT if self.state == "waiting" else (MUTED if self.state == "running" else FAINT)
        return Text.assemble(("  ⋯ " if self.state in ("running", "waiting") else "  ", DIM), (self.status_text(), color))

    def add_output(self, line: Text) -> None:
        self.output.display = True
        self.output.add(line)

    def text(self) -> str:
        return "\n".join(p for p in (self.title_text, self.body.plain(), self.output.plain()) if p)

    def status_text(self) -> str:
        s = self.state
        if self.kind == "command":
            if s == "running":
                return t("tui.k.cmd_running", time=_clock(self.elapsed()))
            if s == "stopped":
                return t("tui.k.cmd_stopped")
            if s == "failed":
                return t("tui.k.cmd_failed")
            if s == "timeout":
                return t("tui.k.cmd_timeout", seconds=_secs(self.seconds))
            return t("tui.k.cmd_done", code=self.code, seconds=_secs(self.seconds))
        if s == "running":
            return t("tui.k.running", step=self.step, time=_clock(self.elapsed()))
        if s == "waiting":
            return t("tui.k.waiting")
        if s in ("stopped", "failed"):
            return t("tui.k.stopped", seconds=_secs(self.seconds))
        if self.tools:
            return t("tui.k.done", seconds=_secs(self.seconds), tools=self.tools)
        return t("tui.k.done_plain", seconds=_secs(self.seconds))

    def short_status(self) -> str:
        """작업 목록에 쓰는 짧은 상태."""
        s = self.state
        if s == "running":
            when = _clock(self.elapsed())
            return t("tui.k.cmd_running", time=when) if self.kind == "command" else t("tui.k.running", step=self.step, time=when)
        if s == "waiting":
            return t("tui.k.waiting")
        if s in ("stopped", "failed", "timeout"):
            return t("tui.k.cmd_stopped")
        if self.kind == "command":
            return t("tui.k.cmd_short", code=self.code, seconds=_secs(self.seconds))
        return t("tui.k.done_plain", seconds=_secs(self.seconds))

    def refresh_kicker(self) -> None:
        if self.kind == "notice":
            label = t("tui.k.decide") if self.decision else t("tui.k.notice")
            self.kicker.update(Text.assemble((label, COBALT if self.decision else WHITE), (" · ", DIM),
                                             (self.when, FAINT)))
            return
        if self.kind == "task":
            if self.progress_at is not None and self.body.is_mounted:
                self.body.set(self.progress_at, self.progress_text())
            if self.nav is not None and self.nav.is_mounted:
                self.nav.refresh_item()
            return
        label = {"command": t("tui.k.command"), "slash": t("tui.k.slash"), "info": t("tui.k.info")}[self.kind]
        if self.kind in ("slash", "info"):
            self.kicker.update(Text(label, style=FAINT))
        else:
            color = COBALT if self.state == "waiting" else (WHITE if self.state == "running" else FAINT)
            self.kicker.update(Text.assemble((label, FAINT), (" · ", DIM), (self.status_text(), color)))
        if self.nav is not None and self.nav.is_mounted:
            self.nav.refresh_item()

    def finish(self, state: str) -> None:
        self.state = state
        self.seconds = time.monotonic() - self.started
        self.refresh_kicker()


# ---------------------------------------------------------------------- 왼쪽: 작업 목록
class NavItem(Static):
    """작업 목록 한 줄: 제목 + 점과 상태. 누르면 그 작업으로."""

    def __init__(self, card: TaskCard):
        super().__init__(classes="navitem")
        self.card = card
        card.nav = self

    def refresh_item(self) -> None:
        c = self.card
        width = max(8, (self.content_size.width or 22))
        dot = {"running": WHITE, "waiting": COBALT, "stopped": MUTED, "failed": MUTED, "timeout": MUTED}.get(c.state, DIM)
        meta_color = COBALT if c.state == "waiting" else FAINT
        self.update(Text.assemble(
            (_clip(c.title_text, width), WHITE), "\n",
            ("● ", dot), (_clip(c.short_status(), width - 2), meta_color),
        ))

    def on_mount(self) -> None:
        self.refresh_item()

    def on_resize(self) -> None:
        self.refresh_item()

    def on_click(self) -> None:
        app = self.app
        if isinstance(app, GladosTUI):
            app.select_card(self.card, scroll=True)


class Clickable(Static):
    """누르면 정해진 일을 하는 글 (제안 카드). 포커스는 입력 칸에 그대로."""

    def __init__(self, renderable: Any = "", action: Callable[[], None] | None = None, **kw: Any):
        super().__init__(renderable, **kw)
        self.action = action

    def on_click(self) -> None:
        if self.action is not None:
            self.action()


class Pill(Widget):
    """한 줄 버튼. Workbench의 버튼을 터미널 칸에 맞게 옮긴 것: 세 줄 상자 대신 한 줄,
    결정은 꽉 찬 코발트, 보내기는 꽉 찬 흰색, 나머지는 옅은 바탕. 단축키는 글 뒤에 흐리게.
    누르기·Enter. focusable이 아니면 눌러도 포커스는 입력 칸에 남는다."""

    COMPONENT_CLASSES = {"pill--key"}
    BINDINGS = [Binding("enter", "press", show=False)]     # Space는 두지 않는다 (치던 글의 빈칸이 버튼이 되지 않게)
    DEFAULT_CSS = """
    Pill { width: auto; height: 1; padding: 0 2; }
    """

    def __init__(self, label: str = "", key: str = "", action: Callable[[], None] | None = None,
                 focusable: bool = False, **kw: Any):
        super().__init__(**kw)
        self.label_text = label
        self.key = key
        self.action = action
        self.can_focus = focusable

    def set_label(self, label: str, key: str | None = None) -> None:
        if label == self.label_text and (key is None or key == self.key):
            return
        self.label_text = label
        if key is not None:
            self.key = key
        self.refresh(layout=True)

    def render(self) -> Text:
        focused = self.can_focus and self.has_focus          # 포커스는 글 밑줄 (Enter가 누를 버튼)
        text = Text(no_wrap=True, end="")
        text.append(self.label_text, style="underline" if focused else "")
        if self.key:
            text.append("  ")
            text.append(self.key, style=self.get_component_rich_style("pill--key", partial=True))
        return text

    def on_click(self, event) -> None:
        event.stop()
        self.press()

    def action_press(self) -> None:
        self.press()

    def press(self) -> None:
        if self.action is not None and not self.disabled:
            self.action()


# ---------------------------------------------------------------------- 처음 화면
class HomeView(Vertical):
    """로고·인사·제안 (Workbench 홈 + LogoFinal)."""

    def __init__(self, first_run: bool, home: str):
        super().__init__(id="home")
        self.first_run = first_run
        self.home_path = home
        self.symbol = Static(id="home-symbol")
        self.mark = Static(id="home-wordmark")
        self.fullname = Static(id="home-fullname")
        self.tagline = Static(id="home-tagline")
        self.greet = Static(id="home-greet")
        self.notes = Lines(id="home-notes")
        self.size_key = ""
        self.resume: Any = None                          # 이어 갈 지난 대화 (SessionInfo, M6). 없으면 카드를 숨김

    def compose(self) -> ComposeResult:
        with Horizontal(id="home-logo"):
            yield self.symbol
            with Vertical(id="home-brand"):
                yield self.mark
                yield self.fullname
                yield self.tagline
        yield self.greet
        yield Clickable(id="suggest-resume", classes="suggest")    # 이어하기 (M6): 지난 대화가 있을 때만, 한 줄 전체
        with Horizontal(id="suggestions"):
            yield Clickable(id="suggest-file", classes="suggest")
            yield Clickable(id="suggest-cmd", classes="suggest")
        yield self.notes

    def on_mount(self) -> None:
        self.notes.display = bool(self.notes.lines)
        self.relabel()

    def relabel(self) -> None:
        if self.first_run:
            title, sub = t("tui.greet.first"), t("tui.greet.first_sub", home=self.home_path)
        else:
            title, sub = t("tui.greet.again"), t("tui.greet.again_sub")
        self.greet.update(Text.assemble((title, f"bold {WHITE}"), "\n", (sub, MUTED)))
        self.fullname.update(Text(logo.FULL_NAME, style=FAINT))
        self.tagline.update(Text(logo.TAGLINE, style=MUTED))
        for wid, kind, fact, wit in (("suggest-file", "tui.suggest.kind_file", "tui.suggest.file", "tui.suggest.file_wit"),
                                     ("suggest-cmd", "tui.suggest.kind_cmd", "tui.suggest.cmd", "tui.suggest.cmd_wit")):
            card = self.query_one(f"#{wid}", Clickable)
            card.update(Text.assemble((t("tui.suggest.label", kind=t(kind)), FAINT), "\n",
                                      (t(fact), f"bold {WHITE}"), "\n", (t(wit), MUTED)))
            card.tooltip = t("tui.suggest.click")
            card.action = self._filler(t(fact))
        self.draw_resume()
        self.size_key = ""
        self.fit()

    def set_resume(self, info: Any) -> None:
        self.resume = info
        if self.is_mounted:
            self.draw_resume()
            self.size_key = ""
            self.fit()

    def draw_resume(self) -> None:
        """이어하기 카드 (M6): 가장 최근의 지난 대화. 누르면 그 대화를 불러온다."""
        card = self.query_one("#suggest-resume", Clickable)
        info = self.resume
        card.display = info is not None
        if info is None:
            return
        card.update(Text.assemble((t("tui.suggest.label", kind=t("tui.suggest.kind_resume")), FAINT), "\n",
                                  (_clip(info.title, 60), f"bold {WHITE}"), "\n",
                                  (t("tui.suggest.resume_wit", when=info.when(), count=info.count), MUTED)))
        card.tooltip = t("tui.suggest.resume_click")
        card.action = lambda: self.app.resume_session(info.id)

    def _filler(self, text: str) -> Callable[[], None]:
        return lambda: self.app.query_one(ChatInput).fill(text)

    def fit(self) -> None:
        """폭에 맞게 로고를 고른다: 심볼 20×10 + 5줄 글자 / 16×8 + 4줄 / 12×6 + 3줄 / 6×3 + 3줄 / 심볼만.
        넓은 화면일수록 점이 많아 곡선이 매끄럽다. 제안 카드는 넓으면 나란히, 좁으면 위아래로."""
        width = self.content_size.width
        if width <= 0:                                   # 아직 배치 전: 고르지 않는다 (가짜 폭으로 고르면 깜박임)
            return
        if width >= 98:
            key, cols, rows, mark_rows, full = "xl", 20, 10, 5, True
        elif width >= 80:
            key, cols, rows, mark_rows, full = "big", 16, 8, 4, True
        elif width >= 59:
            key, cols, rows, mark_rows, full = "mid", 12, 6, 3, True
        elif width >= 52:
            key, cols, rows, mark_rows, full = "lockup", 6, 3, 3, False
        else:
            key, cols, rows, mark_rows, full = "small", 12, 6, 0, False
        self.query_one("#suggestions").set_class(width < 56, "stack")
        if key == self.size_key:
            return
        self.size_key = key
        self.symbol.update(logo.symbol(cols, rows, logo.Presence("done")))
        self.symbol.styles.width = cols
        self.symbol.styles.height = rows
        self.mark.display = bool(mark_rows)
        self.fullname.display = full
        if mark_rows:
            self.mark.update(logo.wordmark(mark_rows))

    def on_resize(self) -> None:
        # 창 크기가 바뀌면 양옆 패널 폭도 따라 바뀌어 잠깐 좁은 폭이 먼저 올 수 있다 → 지금 폭으로 고르고,
        # 배치가 끝난 뒤 한 번 더 고른다 (같으면 아무것도 안 함)
        self.fit()
        self.call_after_refresh(self.fit)

    def add_note(self, line: Text) -> None:
        self.notes.display = True
        self.notes.add(line)


# ---------------------------------------------------------------------- 입력 칸
class ChatInput(Input):
    """입력 칸: ↑↓로 보낸 글 다시 쓰기, 여러 줄 붙여넣기는 표시로 넣고 보낼 때 펼친다,
    포커스가 있을 때 터미널 커서를 보이게 (한글 조합 위치)."""

    BINDINGS = [
        Binding("up", "history(-1)", show=False),
        Binding("down", "history(1)", show=False),
    ]

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.cursor_blink = False                 # 깜박임도 다시 그리기다 (한글 조합을 덜 건드리게)
        self.sent: list[tuple[str, dict[int, str]]] = []
        self.pos = 0
        self.pastes: dict[int, str] = {}

    @property
    def cursor_screen_offset(self) -> Offset:
        """글자 자리 그대로 (Input은 끝에 있을 때 한 칸 뒤를 돌려준다. 그러면 조합 글자가 한 칸 떨어져 뜬다)."""
        x, y, _w, _h = self.content_region
        scroll_x, _ = self.scroll_offset
        return Offset(x + self._position_to_cell(self.cursor_position) - scroll_x, y)

    def on_focus(self) -> None:
        if isinstance(self.app, GladosTUI):
            self.app.set_ime_cursor(True)

    def on_blur(self) -> None:
        if isinstance(self.app, GladosTUI):
            self.app.set_ime_cursor(False)

    def _on_paste(self, event) -> None:
        text = event.text or ""
        if "\n" not in text.strip("\r\n"):
            return                                # 한 줄이면 Input이 그대로 넣는다
        event.prevent_default()
        event.stop()
        n = max(self.pastes, default=0) + 1
        self.pastes[n] = text.strip("\r\n")
        self.insert_text_at_cursor(t("tui.pasted", n=n, lines=len(self.pastes[n].splitlines())))

    def fill(self, text: str) -> None:
        self.value = text
        self.cursor_position = len(text)
        self.focus()

    def take(self) -> tuple[str, str]:
        """(화면에 보일 글, 엔진에 보낼 글). 붙여넣은 글을 펼치고 칸을 비운다."""
        shown = self.value.strip()
        full = shown
        for n, text in self.pastes.items():
            full = full.replace(t("tui.pasted", n=n, lines=len(text.splitlines())), "\n" + text + "\n")
        if shown:
            self.sent.append((self.value, dict(self.pastes)))
        self.pos = len(self.sent)
        self.pastes = {}
        self.value = ""
        return shown, full.strip()

    def action_history(self, delta: int) -> None:
        if not self.sent:
            return
        self.pos = max(0, min(len(self.sent), self.pos + delta))
        if self.pos == len(self.sent):
            self.value, self.pastes = "", {}
        else:
            value, pastes = self.sent[self.pos]
            self.value, self.pastes = value, dict(pastes)
        self.cursor_position = len(self.value)


# ---------------------------------------------------------------------- 승인·되돌리기 상자
class ChoiceDialog(ModalScreen[str]):
    """승인 카드 (Workbench: 코발트 테두리, 굵은 한 줄, 내용 상자, 결정 버튼은 코발트).
    숫자 키나 버튼으로 고른다. Esc와 Enter의 처음 자리는 마지막 선택(거절·그만두기).

    입력 칸에 글을 치던 중에 상자가 뜰 수 있다. 그 글의 숫자가 승인이 되지 않게, 뜬 직후 잠깐은
    숫자 키를 받지 않는다. 글자 단축키(y 등)는 두지 않는다."""

    BINDINGS = [
        Binding("escape", "pick('cancel')", show=False),
        Binding("ctrl+c", "stop", show=False),          # 상자 밖의 키는 막히므로 여기서 멈춤을 받는다
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, kicker: str, headline: str, details: Text | None,
                 options: list[tuple[str, str, str, tuple[str, ...]]], keys_hint: str, wide: bool = False):
        """options: (값, 버튼 글, 버튼 종류 decide|plain, 단축키들). 마지막 옵션이 Esc.
        버튼 글 뒤에 첫 단축키를 흐리게 붙인다."""
        super().__init__()
        self.kicker_text = kicker
        self.headline = headline
        self.details = details
        self.options = options
        self.keys_hint = keys_hint
        self.shortcuts = {key: value for value, _, _, keys in options for key in keys}
        self.opened = time.monotonic()
        self.chosen: str | None = None
        self.wide = wide                  # 실행 카드처럼 줄이 긴 내용 (상자를 넓게)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide" if self.wide else ""):
            yield Static(Text(self.kicker_text, style=COBALT), id="dialog-kicker")
            yield Static(Text(self.headline, style=f"bold {WHITE}"), id="dialog-headline")
            if self.details is not None:
                yield Static(self.details, id="dialog-details")
            with Horizontal(id="dialog-buttons"):
                for value, label, kind, keys in self.options:
                    yield Pill(label, keys[0] if keys else "", action=self._picker(value), focusable=True,
                               id=f"pick-{value}", classes=f"choice {kind}")
            yield Static(Text(self.keys_hint, style=FAINT), id="dialog-keys")

    def _picker(self, value: str) -> Callable[[], None]:
        def pick() -> None:
            if time.monotonic() - self.opened >= KEY_GUARD_S:    # 뜬 직후의 Enter·누르기도 받지 않는다
                self.choose(value)
        return pick

    def on_mount(self) -> None:
        self.opened = time.monotonic()
        self.query_one(f"#pick-{self.options[-1][0]}", Pill).focus()     # Enter는 안전한 쪽

    def choose(self, value: str) -> None:
        """한 번만 닫는다 (키와 멈춤이 겹쳐도)."""
        if self.chosen is None:
            self.chosen = value
            self.dismiss(value)

    def on_key(self, event) -> None:
        value = self.shortcuts.get(event.key)
        if value is None:
            return
        event.stop()
        if time.monotonic() - self.opened >= KEY_GUARD_S:
            self.choose(value)

    def action_pick(self, value: str) -> None:
        self.choose(self.options[-1][0] if value == "cancel" else value)

    def action_stop(self) -> None:
        """Ctrl+C: 작업 중이면 작업을 멈춘다 (상자는 거절로 닫힘). 아니면 그만두기."""
        app = self.app
        if isinstance(app, GladosTUI) and app.job:
            app.action_stop_task()
        else:
            self.choose(self.options[-1][0])


CARD_STYLES = {"fact": f"bold {WHITE}", "plain": MUTED, "dim": FAINT, "decision": f"bold {COBALT}", "wit": MUTED}


def approval_dialog(call: ToolCall, d: Decision) -> ChoiceDialog:
    if call.card:                                  # 실행 카드 (remote.run: GPU·명령·커밋·규칙, M5)
        details = Text()
        for i, (style, text) in enumerate(call.card):
            if i:
                details.append("\n")
            details.append(text, style=CARD_STYLES.get(style, MUTED))
    else:
        details = Text(call.tool, style=f"bold {WHITE}")
        for key, value in summarize_args(call.args, 300).items():
            details.append(f"\n  {key}: ", style=FAINT)
            details.append(str(value), style=MUTED)
    if d.key == "policy.remote_run":               # 서버 실행: "이 명령어가 맞습니까?" (M5.2)
        options = [("once", t("tui.approve.run_yes"), "decide", ("1",)), ("deny", t("tui.approve.run_no"), "plain", ("3",))]
        hint = t("tui.approve.keys_run")
        return ChoiceDialog(t("tui.approve.kicker"), localized(d, fallback=d.reason), details, options, hint,
                            wide=bool(call.card))
    if d.key == "policy.memory_note":              # "기억해" (M6): 저장할 글을 보고 정한다
        options = [("once", t("tui.approve.note_yes"), "decide", ("1",)),
                   ("deny", t("tui.approve.note_no"), "plain", ("3",))]
        return ChoiceDialog(t("tui.approve.kicker"), localized(d, fallback=d.reason), details, options,
                            t("tui.approve.keys13"))
    options = [("once", t("tui.approve.once"), "decide", ("1",))]
    if d.grantable:
        options.append(("task", t("tui.approve.task"), "plain", ("2",)))
    options.append(("deny", t("tui.approve.deny"), "plain", ("3",)))
    hint = t("tui.approve.keys") if d.grantable else t("tui.approve.keys13")
    return ChoiceDialog(t("tui.approve.kicker"), localized(d, fallback=d.reason), details, options, hint,
                        wide=bool(call.card))


def undo_dialog(text: str) -> ChoiceDialog:
    options = [("restore", t("tui.undo.restore"), "decide", ("1",)),
               ("cancel", t("tui.undo.cancel"), "plain", ("3",))]
    return ChoiceDialog(t("tui.undo.kicker"), text, None, options, t("tui.undo.keys"))


# ---------------------------------------------------------------------- 화면
class GladosTUI(TextualApp):
    CSS = f"""
    Screen {{ background: {VOID}; color: {WHITE}; }}
    #masthead {{ height: 4; background: {PANEL}; border-bottom: solid {RULE}; padding: 0 1 0 1; }}
    #presence {{ width: 6; height: 3; margin-right: 2; }}
    #brand {{ width: 1fr; height: 3; }}
    #wordmark, #status, #machine {{ height: 1; }}
    #chip {{ width: auto; height: 3; border: round {LINE}; padding: 0 1; color: {MUTED}; }}
    #body {{ height: 1fr; }}
    #nav {{ width: 24; background: {PANEL}; border-right: solid {RULE}; padding: 1 1 0 1; }}
    #new-chat {{ width: 1fr; padding: 0 1; margin-bottom: 1; background: {HOVER}; color: {WHITE}; }}
    #new-chat:hover {{ background: {LINE}; }}
    .label {{ height: 1; color: {FAINT}; }}
    #nav-running, #nav-done {{ height: auto; margin-bottom: 1; }}
    .navitem {{ height: 2; padding: 0 1; margin-bottom: 1; }}
    .navitem:hover {{ background: {HOVER}; }}
    .navitem.selected {{ background: {HOVER}; }}
    #nav-empty {{ color: {DIM}; height: 1; padding: 0 1; }}
    #center {{ width: 1fr; }}
    #main {{ width: 100%; height: 1fr; padding: 1 2 0 2; scrollbar-size-vertical: 1; scrollbar-gutter: stable;
             scrollbar-color: {LINE}; scrollbar-background: {VOID}; }}
    #home {{ height: auto; }}
    #home-logo {{ height: auto; margin-bottom: 1; }}
    #home-symbol {{ margin-right: 2; }}
    #home-brand {{ height: auto; width: 1fr; }}
    #home-fullname {{ margin-top: 1; height: auto; }}
    #home-tagline {{ height: 1; }}
    #home-greet {{ height: auto; margin-top: 1; }}
    #home-notes {{ height: auto; margin-top: 1; color: {FAINT}; }}
    #suggestions {{ height: auto; margin-top: 1; }}
    #suggestions.stack {{ layout: vertical; }}
    .suggest {{ width: 1fr; height: auto; border: round {RULE}; padding: 0 1; margin-right: 1; }}
    #suggestions.stack .suggest {{ margin-right: 0; }}
    #suggest-resume {{ width: 100%; margin: 1 0 0 0; }}
    .suggest:hover {{ border: round {LINE}; background: {SURFACE}; }}
    .card {{ height: auto; margin-bottom: 1; padding-bottom: 1; border-bottom: solid {RULE}; }}
    .card .kicker {{ height: 1; }}
    .card .title {{ height: auto; }}
    .card .body {{ height: auto; margin-top: 1; }}
    .card .output {{ height: auto; margin-top: 1; background: {SURFACE}; border: round {RULE}; padding: 0 1; }}
    .card .live {{ height: auto; max-height: {LIVE_LINES + 1}; margin-top: 1; padding: 0 0 0 1;
                   border-left: solid {LINE}; color: {DIM}; }}
    #chips {{ height: 1; margin: 1 2 0 2; }}
    #composer {{ height: 3; margin: 0 2; padding: 0 0 0 1; border: round {LINE}; background: {SURFACE}; }}
    #prompt {{ width: 1fr; background: {SURFACE}; }}
    #prompt:focus {{ background-tint: {VOID} 0%; }}
    #prompt > .input--placeholder {{ color: {FAINT}; }}
    #send {{ margin: 0 1 0 1; background: {HOVER}; color: {FAINT}; }}
    #send:hover {{ color: {WHITE}; }}
    #send > .pill--key {{ color: {DIM}; }}
    #send.ready {{ background: {WHITE}; color: {VOID}; text-style: bold; }}
    #send.ready:hover {{ background: #FFFFFF; }}
    #send.ready > .pill--key {{ color: {DIM}; text-style: not bold; }}
    #send.stop {{ color: {MUTED}; }}
    #send.stop:hover {{ background: {LINE}; color: {WHITE}; }}
    #keys {{ height: 1; padding: 0 1; background: {PANEL}; }}
    #aside {{ width: 30; background: {PANEL}; border-left: solid {RULE}; padding: 1 1 0 1; }}
    .section {{ height: auto; margin-bottom: 1; color: {MUTED}; }}
    #activity {{ height: 1fr; min-height: 4; background: {PANEL}; scrollbar-size-vertical: 1; overflow-x: hidden;
                 scrollbar-color: {LINE}; scrollbar-background: {PANEL}; margin-bottom: 1; }}
    #memory {{ height: auto; max-height: 9; }}
    ChoiceDialog {{ align: center middle; background: {VOID} 60%; }}
    #dialog {{ width: 76; max-width: 95%; height: auto; padding: 1 3; background: {SURFACE};
               border: round {COBALT}; }}
    #dialog.wide {{ width: 104; }}
    #dialog-headline {{ margin-top: 1; }}
    #dialog-details {{ margin-top: 1; background: {PANEL}; padding: 0 1; }}
    #dialog-buttons {{ height: 1; margin-top: 2; }}
    .choice {{ margin-right: 2; background: {RULE}; color: {MUTED}; }}
    .choice > .pill--key {{ color: {DIM}; }}
    .choice:hover {{ background: {LINE}; color: {WHITE}; }}
    .choice:focus {{ background: {LINE}; color: {WHITE}; text-style: bold; }}
    .choice.decide {{ background: {COBALT}; color: {VOID}; text-style: bold; }}
    .choice.decide > .pill--key {{ color: {TRAIL_DARK}; text-style: not bold; }}
    .choice.decide:hover, .choice.decide:focus {{ background: {COBALT_HI}; color: {VOID}; }}
    #dialog-keys {{ margin-top: 2; }}
    """
    BINDINGS = [
        Binding("escape", "stop_task", show=False),
        Binding("f1", "help", show=False),
        Binding("f2", "toggle_aside", show=False),
        Binding("f3", "toggle_nav", show=False),
        Binding("pageup", "scroll_main(-1)", show=False),
        Binding("pagedown", "scroll_main(1)", show=False),
    ]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, cfg: Config, first_run: bool = False, notes: list[str] | None = None,
                 llm=None, os_name: str | None = None, check_model: bool = True):
        super().__init__()
        self.cfg = cfg
        self.first_run = first_run
        self.notes = list(notes or [])
        self.stop_event = threading.Event()
        self.job = ""                     # "" | "task" | "command"
        self.state_text = t("tui.state.idle")
        self.state_color = MUTED
        self.step = 0
        self.nav_on = True
        self.aside_on = True
        self.motion = cfg.motion
        self.presence_kind = "idle"       # idle | work | wait | done
        self.presence_since = time.monotonic()
        self.current: TaskCard | None = None     # 지금 글을 받는 카드
        self.selected: TaskCard | None = None    # 맥락이 보여 주는 카드
        self._dialog: ChoiceDialog | None = None
        self._cursor_on = False
        self._check = check_model
        self.mem_stats = (0, 0)           # (기억한 작업 수, 다음 작업에 쓰는 비율 %)
        self.counts = {"done": 0, "stopped": 0, "commands": 0}
        self.closing = False
        self._clip: Any = None                    # 클립보드 (처음 쓸 때 정한다: 화면 없이 돌면 이 프로그램 안에서만)
        self._eat_up = 0                          # 오른쪽 클릭·Ctrl+클릭으로 처리한 누름의 뗌은 넘기지 않는다
        self.machine_text = ""
        self.screen_width = 0
        self.engine = TuiEngine(cfg, self, llm=llm, os_name=os_name)
        self.engine.llm.stop_event = self.stop_event      # 모델 호출을 스트리밍으로 (멈출 수 있게)
        self.meter = Meter()                              # 작업 중에 주고받는 토큰 (화면이 따로 읽음)
        self.engine.llm.meter = self.meter
        self._ticks = 0
        self._shown_second = -1
        self.home = HomeView(first_run, str(cfg.home))
        host = cfg.model.host.split("://")[-1].split("/")[0]
        self.model_host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        self.local_model = self.model_host in LOCAL_HOSTS

    # ------------------------------------------------------------------ 만들기
    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Static(id="presence")
            with Vertical(id="brand"):
                yield Static(id="wordmark")
                yield Static(id="status")
                yield Static(id="machine")
            yield Static(id="chip")
        with Horizontal(id="body"):
            with Vertical(id="nav"):
                yield Pill(id="new-chat", action=self.new_conversation)
                yield Static(id="nav-running-label", classes="label")
                yield Vertical(id="nav-running")
                yield Static(id="nav-done-label", classes="label")
                yield Static(id="nav-empty")
                yield Vertical(id="nav-done")
            with Vertical(id="center"):
                with VerticalScroll(id="main", can_focus=False):
                    yield self.home
                yield Static(id="chips")
                with Horizontal(id="composer"):           # Workbench처럼 입력과 보내기를 한 상자에
                    yield ChatInput(id="prompt", compact=True, select_on_focus=False)
                    yield Pill(id="send", action=self.press_send)
            with Vertical(id="aside"):
                yield Static(id="session-title", classes="label")
                yield Static(id="session", classes="section")
                yield Static(id="machine-title", classes="label")
                yield Static(id="machine-info", classes="section")
                yield Static(id="activity-title", classes="label")
                log = LinkLog(id="activity", wrap=True, markup=False, highlight=False, max_lines=600, min_width=10)
                log.can_focus = False
                yield log
                yield Static(id="scope-title", classes="label")
                yield Static(id="scope", classes="section")
                yield Static(id="sources-title", classes="label")
                yield Static(id="sources", classes="section")
                yield Static(id="egress-title", classes="label")
                yield Static(id="egress", classes="section")
                yield Static(id="memory-title", classes="label")
                yield Static(id="memory", classes="section")
        yield Static(id="keys")

    def on_mount(self) -> None:
        self.register_theme(GLADOS_THEME)
        self.theme = "glados"
        e = self.engine
        for note in self.notes + [e.web_note(), t("tui.ime_hint")]:
            self.home.add_note(Text(note, style=FAINT))
        if e.cwd_is_safety():
            self.home.add_note(Text(t("cwd.safety_note"), style=FAINT))
        self.home.resume = e.resume_hint()          # 이어하기 카드 (M6)
        self.relabel()
        self.layout_panels()
        self.refresh_memory()
        self.refresh_aside()
        self.draw_presence()
        self._timer = self.set_interval(FRAME_S, self.tick_presence, pause=True)
        self.query_one(ChatInput).focus()
        self.call_after_refresh(self.sync_cursor)
        if self._check:
            self.set_state(t("tui.state.checking"))
            self.check_model_worker()
        self.engine.watch()                       # 서버 실행 배경 감시 (M5, 서버가 있을 때만)

    def relabel(self) -> None:
        """화면 언어가 바뀌면 (/lang) 고정 글을 다시 쓴다."""
        cfg = self.cfg
        mode = t("header.low") if cfg.model.is_low else ""
        self.query_one("#wordmark", Static).update(Text("G L a D O S", style=f"bold {WHITE}"))
        self.machine_text = t("tui.machine", model=cfg.model.name, mode=mode, os=osinfo.OS_LABELS[self.engine.os])
        self.draw_chip()
        self.query_one("#new-chat", Pill).set_label(t("tui.new_chat"))
        self.query_one("#nav-running-label", Static).update(t("tui.nav.running"))
        self.query_one("#nav-done-label", Static).update(t("tui.nav.session"))
        self.query_one("#nav-empty", Static).update(t("tui.nav.empty"))
        self.query_one("#session-title", Static).update(t("tui.aside.session"))
        self.query_one("#machine-title", Static).update(t("tui.aside.machine"))
        self.query_one("#activity-title", Static).update(t("tui.activity"))
        self.query_one("#scope-title", Static).update(t("tui.aside.scope"))
        self.query_one("#sources-title", Static).update(t("tui.aside.sources"))
        self.query_one("#egress-title", Static).update(t("tui.aside.egress"))
        self.query_one("#memory-title", Static).update(t("tui.memory"))
        self.refresh_send(relabel=True)
        self.draw_chips()
        self.home.relabel()
        for card in self.query(TaskCard):                 # 제목의 이름표 ("연구원님" / "You")
            card.heading.update(card.heading_text())
        for card in self.query(TaskCard):
            card.refresh_kicker()
        self.refresh_placeholder()
        self.update_status()
        self.refresh_aside()

    def egress_counts(self) -> tuple[int, int]:
        egress = self.engine.hctx.egress
        return egress.counts() if egress is not None else (0, 0)

    def draw_chip(self) -> None:
        """머리글 칩 하나: 이 세션의 외부 전송·웹 검색 수 (보내면 밝게). 모델이 다른 컴퓨터면 그것도."""
        sent, searches = self.egress_counts()
        chip = Text(t("tui.chip.local", sent=sent, searches=searches), style=WHITE if sent else MUTED)
        if not self.local_model:
            chip.append(" · " + t("tui.chip.remote", host=self.model_host), style=MUTED)
        self.query_one("#chip", Static).update(chip)

    def draw_chips(self) -> None:
        """입력창 위 할 수 있는 일 칩. 넓으면 앞에 "종류는 GLaDOS가 판단합니다"."""
        names = t("tui.chips").split("|")
        width = self.query_one("#chips").content_size.width or 60
        chips = Text()
        if width >= sum(Text(n).cell_len + 3 for n in names) + Text(t("tui.chips.label")).cell_len + 3:
            chips.append(t("tui.chips.label"), style=FAINT)
            chips.append("   ")
        for i, name in enumerate(names):
            if i:
                chips.append(" ")
            chips.append(f" {name} ", style=f"{FAINT} on {HOVER}")
        self.query_one("#chips", Static).update(chips)

    def refresh_send(self, relabel: bool = False) -> None:
        """보내기 버튼: 글이 있으면 흰색으로 켜지고, 작업 중에는 멈춤 버튼이 된다 (누르면 Esc와 같다)."""
        send = self.query_one("#send", Pill)
        if self.job:
            send.set_label(t("tui.stop"), "Esc")
            send.set_class(False, "ready")
            send.set_class(True, "stop")
            return
        send.set_label(t("tui.send"), "↵")
        send.set_class(False, "stop")
        send.set_class(bool(self.query_one(ChatInput).value.strip()), "ready")
        if relabel:
            send.refresh(layout=True)

    def press_send(self) -> None:
        if self.job:
            self.action_stop_task()
        else:
            self.submit()
        self.query_one(ChatInput).focus()

    @on(Input.Changed, "#prompt")
    def prompt_changed(self, event: Input.Changed) -> None:
        self.refresh_send()

    def refresh_placeholder(self) -> None:
        box = self.query_one(ChatInput)
        box.placeholder = t("tui.placeholder") if self.home.display else t("tui.placeholder.next")
        self.query_one("#chips").display = bool(self.home.display)

    # ------------------------------------------------------------------ 스레드 사이
    def post(self, fn: Callable[..., Any], *args: Any) -> None:
        """작업 스레드에서도, 화면 스레드에서도 부를 수 있게. 화면이 닫히는 중이면 버린다."""
        if threading.get_ident() == self._thread_id:
            fn(*args)
            return
        if self.closing or not self.is_running:
            return
        try:
            self.call_from_thread(fn, *args)
        except Exception:
            if not (self.closing or not self.is_running):
                raise

    # ------------------------------------------------------------------ 가운데
    @property
    def main(self) -> VerticalScroll:
        return self.query_one("#main", VerticalScroll)

    def new_card(self, kind: str, title: str) -> TaskCard:
        """작업·명령·안내 카드를 하나 연다. 처음 화면은 접는다."""
        if self.home.display:
            self.home.display = False
            self.refresh_placeholder()
            self.refresh_aside()
        card = TaskCard(kind, title)
        self.main.mount(card)
        cards = self.query(TaskCard)
        if len(cards) > MAX_CARDS:
            old = cards.first()
            if old.nav is not None:
                old.nav.remove()
            old.remove()
        if kind in ("task", "command"):
            self.query_one("#nav-running", Vertical).mount(NavItem(card))
            self.query_one("#nav-empty").display = False
            self.select_card(card)
        self.current = card
        self.main.call_after_refresh(self.main.scroll_end, animate=False)
        return card

    def show_notice(self, n) -> None:
        """서버 실행 알림 (M5): 가운데에 알림 카드, 오른쪽 아래 토스트, OS 알림. 지금 작업 카드는 그대로."""
        if self.closing:
            return
        if self.home.display:
            self.home.display = False
            self.refresh_placeholder()
            self.refresh_aside()
        card = TaskCard("notice", "")
        card.decision = bool(n.decision)
        card.when = datetime.fromtimestamp(n.ts).strftime("%H:%M")
        self.main.mount(card)
        lines = n.lines()
        card.add(Text(lines[0], style=STYLES["decision" if n.decision else "fact"]))
        for line in lines[1:]:
            card.add(Text(line, style=STYLES["dim"]))
        self.main.call_after_refresh(self.main.scroll_end, animate=False)
        self.notify(lines[0], title=n.title(), severity="warning" if n.decision else "information",
                    timeout=20 if n.decision else 8)
        self.engine.os_notify(n)

    def add_line(self, line: Any = "") -> None:
        """엔진이 쓴 글 한 줄. 지금 카드에, 없으면 처음 화면이나 안내 카드에."""
        if not isinstance(line, Text):
            line = Text(str(line), style=STYLES["plain"])
        if self.current is not None:
            card = self.current
        elif self.home.display:
            self.home.add_note(line)
            return
        else:
            card = self.new_card("info", "")
        main = self.main
        at_end = main.scroll_y >= main.max_scroll_y - 1
        card.add(line)
        if at_end:
            main.call_after_refresh(main.scroll_end, animate=False)

    def action_scroll_main(self, direction: int) -> None:
        if direction < 0:
            self.main.scroll_page_up(animate=False)
        else:
            self.main.scroll_page_down(animate=False)

    def select_card(self, card: TaskCard, scroll: bool = False) -> None:
        self.selected = card
        for item in self.query(NavItem):
            item.set_class(item.card is card, "selected")
        if scroll:
            self.main.scroll_to_widget(card, top=True, animate=False)
        self.refresh_aside()

    def move_to_done(self, card: TaskCard) -> None:
        """끝난 작업은 '이번 대화'로 (맨 위가 최근)."""
        if card.nav is None:
            return
        done = self.query_one("#nav-done", Vertical)
        if card.nav.parent is done:
            card.nav.refresh_item()
            return
        card.nav.remove()
        item = NavItem(card)
        first = done.children[0] if done.children else None
        done.mount(item, before=first)
        item.set_class(card is self.selected, "selected")

    # ------------------------------------------------------------------ 엔진 이벤트
    def aside_visible(self) -> bool:
        return bool(self.query_one("#aside").display)

    def show_event(self, e: Event) -> None:
        d = e.data
        card = self.current
        side = self.aside_visible()
        if card is not None:
            if e.kind in (ev.ANSWER, ev.STOP):
                card.hide_live()                          # 중간 토큰은 지우고 답만
            elif not self.motion:
                self.refresh_live()
            if e.kind == ev.OBSERVATION and d.get("tool") == "shell.run" and side:
                first = (str(d.get("text", "")).splitlines() or [""])[0]
                card.add(Text("  " + first, style=FAINT))      # "$ 명령" (제안 줄은 맥락의 과정에만)
            for style, text in event_lines(e):
                if e.kind == ev.START or (e.kind in (ev.PROPOSAL, ev.EGRESS) and side):
                    continue
                if e.kind == ev.PLAN and side and not d.get("say"):
                    continue                              # 계획 단계는 맥락에만. 카드에는 GLaDOS의 첫 말 (M8)
                if style == "speaker":                    # GLaDOS의 답 앞: 코발트 점 + 이름 (연구원님 말과 구별)
                    card.add(Text.assemble(("● ", COBALT), (text, FAINT)))
                    continue
                card.add(Text(text, style=STYLES.get(style, STYLES["plain"])))
            if e.kind in (ev.THOUGHT, ev.PROPOSAL):
                card.step = int(d.get("step") or card.step)
            if e.kind == ev.PROPOSAL:
                card.tools += 1
                card.start_progress()                     # 일을 시작했다: 진행 줄 (M8)
            if e.kind == ev.OBSERVATION and d.get("tool") in ("fs.search", "fs.inspect"):
                found = SCOPE.search((str(d.get("text", "")).splitlines() or [""])[0])
                if found:
                    card.scope = [r.strip() for r in found.group(1).split(",") if r.strip()]
            if e.kind == ev.ANSWER:
                card.sources = list(d.get("sources") or [])
            if e.kind == ev.EGRESS:
                card.egress.append((str(d.get("tool", "")), str(d.get("host", "")), str(d.get("what", ""))))
            if card.state == "running":
                card.refresh_kicker()
            if card is self.selected and e.kind in (ev.OBSERVATION, ev.ANSWER, ev.EGRESS):
                self.refresh_aside()
        self.add_activity(e)
        if e.kind == ev.EGRESS:
            self.draw_chip()
        if self.stop_event.is_set():
            return                                        # 상태 줄은 "멈추는 중"
        if e.kind == ev.START:
            self.step = 1
            self.set_state(t("tui.state.thinking", step=1), WHITE)
        elif e.kind in (ev.THOUGHT, ev.PROPOSAL):
            self.step = int(d.get("step") or self.step)
            if e.kind == ev.PROPOSAL:
                self.set_state(t("tui.state.tool", tool=d["tool"], step=self.step), WHITE)
        elif e.kind in (ev.OBSERVATION, ev.GUARD) or (e.kind == ev.DECISION and d["verdict"] == "deny"):
            self.set_state(t("tui.state.thinking", step=self.step + 1), WHITE)
            if card is not None and card.state == "running":
                card.step = self.step + 1                 # 다음 걸음을 생각하는 중 (머리글과 같은 단계)
                card.refresh_kicker()

    def add_activity(self, e: Event) -> None:
        log = self.query_one("#activity", RichLog)
        d = e.data
        if e.kind == ev.START:
            when = time.strftime("%H:%M", time.localtime(e.ts))
            req = str(d.get("request", "")).replace("\n", " ")
            log.write(Text.assemble((f"{when} ", FAINT), (req[:80] + ("..." if len(req) > 80 else ""), f"bold {WHITE}")))
        elif e.kind == ev.PLAN:
            goal = str(d.get("goal") or "")
            if d.get("say"):
                log.write(Text(f"   \u201c{d['say']}\u201d", style=MUTED))       # 연구원님께 한 첫 말
            log.write(Text(t("tui.plan") + (f" · {goal}" if goal else ""), style=FAINT))
            for i, step in enumerate(d.get("plan") or [], 1):
                log.write(Text(f"   {i}) {step}", style=WHITE))
            if d.get("check"):
                log.write(Text(f"   {t('tui.plan_check')}: {d['check']}", style=MUTED))
        elif e.kind == ev.THOUGHT:
            thought = str(d.get("thought", "")).replace("\n", " ")
            if len(thought) > THOUGHT_SHOWN:
                thought = thought[:THOUGHT_SHOWN] + "..."
            log.write(Text(f"{d.get('step')}. {thought}", style=MUTED))
        elif e.kind == ev.PROPOSAL:
            log.write(Text(f"   -> {d['tool']} {_brief(d.get('args', {}), 120)}", style=WHITE))
        elif e.kind == ev.DECISION:
            verdict = d["verdict"]
            if verdict == "ask":
                text = f"      {t('tui.verdict.ask')} · {i18n.who_text(str(d.get('decided_by', '')))}"
                log.write(Text(text, style=COBALT))
            elif verdict == "deny":
                reason = localized(_Keyed(d.get("key", ""), d.get("params", {})), fallback=d.get("reason", ""))
                log.write(Text(f"      {t('tui.verdict.deny')} · {reason}", style=MUTED))
            else:
                rule = i18n.rule_text(str(d.get("rule", "")))
                log.write(Text(f"      {t('tui.verdict.' + verdict)} · {rule}", style=FAINT))
        elif e.kind == ev.OBSERVATION:
            first = (str(d.get("text", "")).strip().splitlines() or [""])[0]
            log.write(Text(f"      <- {first[:160]}", style=MUTED if first.startswith("Error") else FAINT))
        elif e.kind == ev.GUARD:
            log.write(Text("   ! " + t(d.get("key") or "guard.evidence"), style=MUTED))
        elif e.kind == ev.EGRESS:
            what = str(d.get("what", "")).replace("\n", " ")
            log.write(Text.assemble(("      ↗ ", WHITE), (str(d.get("host", "")), WHITE),
                                    ("  " + what[:100] + ("..." if len(what) > 100 else ""), FAINT)))
        elif e.kind == ev.ANSWER:
            log.write(Text("   = " + t("tui.translated" if d.get("translated") else "tui.answered"), style=FAINT))
        elif e.kind == ev.STOP:
            reason = d.get("reason")
            text = t(f"stop.{reason}", **d.get("params", {})) if reason in STOPS else str(d.get("message", ""))
            log.write(Text("   x " + text, style=MUTED))

    # ------------------------------------------------------------------ 머리글·맥락
    def set_state(self, text: str, color: str = MUTED) -> None:
        self.state_text = text
        self.state_color = color
        self.update_status()

    def update_status(self) -> None:
        e = self.engine
        cwd = e.places.cwd or e.places.home
        if len(cwd) > 48:
            cwd = "..." + cwd[-45:]
        self.draw_status()
        self.query_one("#machine", Static).update(Text.assemble(
            (self.machine_text, FAINT), ("  ·  ", DIM), (t("tui.folder", path=cwd), FAINT)))
        self.query_one("#keys", Static).update(key_hints(t("tui.keys.busy") if self.job else t("tui.keys.idle")))
        self.refresh_send()
        self.refresh_aside(machine_only=True)

    def draw_status(self) -> None:
        status = Text(self.state_text, style=self.state_color)
        card = self.current
        if self.job and card is not None:
            status.append(" · " + _clock(card.elapsed()), style=FAINT)
        self.query_one("#status", Static).update(status)

    def refresh_live(self) -> None:
        """작업 중: 흐른 시간(카드 윗줄·머리글)과 주고받은 토큰(카드 안 칸). 화면 스레드에서."""
        card = self.current
        if not self.job or card is None or self.closing:
            return
        second = int(card.elapsed())
        if second != self._shown_second:
            self._shown_second = second
            card.refresh_kicker()
            self.draw_status()
        if self.job == "task":
            card.show_live(*self.meter.snapshot())

    def refresh_memory(self) -> None:
        e = self.engine
        pct = 0
        if e.memory.turns:
            budget = max(1, e.history_budget())
            used = sum(n for _, level, n in e.memory.preview(budget) if level != DROPPED)
            pct = min(100, round(100 * used / budget))
        self.mem_stats = (len(e.memory.turns), pct)
        lines = e.context_lines()
        head, rest = lines[0], lines[1:]
        if len(rest) > 5:
            rest = ["  ..."] + rest[-5:]
        memory = Text.assemble((t("tui.memory_line", count=self.mem_stats[0], pct=pct), f"bold {WHITE}"),
                               ("\n" + "\n".join([head, *rest]), MUTED))
        self.query_one("#memory", Static).update(memory)

    def refresh_aside(self, machine_only: bool = False) -> None:
        """오른쪽 맥락. 처음에는 이번 세션과 이 컴퓨터, 작업을 하면 과정과 고른 작업의 범위·출처."""
        if not self.is_mounted:
            return
        at_home = bool(self.home.display)
        e = self.engine
        cwd = e.places.cwd or e.places.home
        self.query_one("#machine-info", Static).update(Text.assemble((self.machine_text, WHITE), "\n", (cwd, FAINT)))
        if machine_only:
            return
        for wid in ("#session-title", "#session", "#machine-title", "#machine-info"):
            self.query_one(wid).display = at_home
        for wid in ("#activity-title", "#activity"):
            self.query_one(wid).display = not at_home
        c = self.counts
        sent_n, search_n = self.egress_counts()
        self.query_one("#session", Static).update(Text.assemble(
            (str(c["done"]), f"bold {WHITE}"), (f" {t('tui.stat.done')}   ", MUTED),
            (str(c["stopped"]), f"bold {WHITE}"), (f" {t('tui.stat.stopped')}   ", MUTED),
            (str(c["commands"]), f"bold {WHITE}"), (f" {t('tui.stat.commands')}", MUTED), "\n",
            (t("tui.local_note" if self.local_model else "tui.sent_note", sent=sent_n, searches=search_n), MUTED),
            *((("\n" + t("tui.remote_note", host=self.model_host), MUTED),) if not self.local_model else ()),
        ))
        card = self.selected
        scope = card.scope if card is not None and not at_home else []
        sources = card.sources if card is not None and not at_home else []
        for wid, on_ in (("#scope-title", scope), ("#scope", scope), ("#sources-title", sources), ("#sources", sources)):
            self.query_one(wid).display = bool(on_)
        self.query_one("#scope", Static).update(Text("\n".join(f"✓ {r}" for r in scope), style=WHITE))
        shown = sources[:8]
        text = Text()
        for i, src in enumerate(shown, 1):
            if i > 1:
                text.append("\n")
            text.append(f"[{i}] ", style=FAINT)
            text.append(src, style=WHITE)
        if len(sources) > len(shown):
            text.append(f"\n+{len(sources) - len(shown)}", style=FAINT)
        self.query_one("#sources", Static).update(linkify(text))
        sent = card.egress if card is not None and not at_home else []
        for wid in ("#egress-title", "#egress"):
            self.query_one(wid).display = bool(sent)
        out = Text()
        for i, (tool, host, what) in enumerate(sent[-EGRESS_SHOWN:]):
            if i:
                out.append("\n")
            out.append("↗ ", style=FAINT)
            out.append(host, style=WHITE)
            out.append("\n  " + (what if len(what) <= 90 else what[:89] + "…"), style=MUTED)
        if len(sent) > EGRESS_SHOWN:
            out.append(f"\n+{len(sent) - EGRESS_SHOWN}", style=FAINT)
        self.query_one("#egress", Static).update(out)

    def layout_panels(self, width: int | None = None) -> None:
        width = width or self.size.width
        self.screen_width = width
        nav, aside = self.query_one("#nav"), self.query_one("#aside")
        nav.display = self.nav_on and width >= NAV_MIN
        aside.display = self.aside_on and width >= ASIDE_MIN
        nav.styles.width, aside.styles.width = (30, 44) if width >= 190 else (28, 36) if width >= 150 else (24, 30)

    def on_resize(self, event) -> None:
        self.layout_panels(event.size.width)
        self.call_after_refresh(self.sync_cursor)
        self.call_after_refresh(self.draw_chips)

    def sync_cursor(self) -> None:
        """터미널 커서를 입력 칸 글자 자리로 (처음 그린 뒤, 창 크기가 바뀐 뒤)."""
        box = self.query_one(ChatInput)
        if box.has_focus:
            self.cursor_position = box.cursor_screen_offset

    def action_toggle_aside(self) -> None:
        self.aside_on = not self.aside_on
        if self.aside_on and self.screen_width < ASIDE_MIN:
            self.notify(t("tui.narrow", width=ASIDE_MIN), timeout=3)
        self.layout_panels(self.screen_width)

    def action_toggle_nav(self) -> None:
        self.nav_on = not self.nav_on
        if self.nav_on and self.screen_width < NAV_MIN:
            self.notify(t("tui.narrow", width=NAV_MIN), timeout=3)
        self.layout_panels(self.screen_width)

    def action_help(self) -> None:
        self.run_command("/help")

    # ------------------------------------------------------------------ 마우스: 복사·붙여넣기·링크 (M7.9)
    @property
    def clip(self) -> Any:
        if self._clip is None:
            self._clip = clipboard.MemoryClipboard() if self.is_headless else clipboard.SystemClipboard(self.engine.os)
        return self._clip

    def copy_to_clipboard(self, text: str) -> None:
        """운영 체제의 클립보드에 (Ctrl+C·오른쪽 클릭). 안 되면 터미널 복사(OSC 52)."""
        if self.clip.copy(text):
            self._clipboard = text
        else:
            super().copy_to_clipboard(text)

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, events.MouseDown) and not event.is_forwarded:
            if event.button == 3:
                self._eat_up = 3
                self.right_click()
                return
            if event.button == 1 and event.ctrl:
                url = self.link_at(*event.screen_offset)
                if url:
                    self._eat_up = 1
                    self.open_link(url)
                    return
        if isinstance(event, events.MouseUp) and not event.is_forwarded and self._eat_up:
            self._eat_up = 0
            return
        await super().on_event(event)

    def right_click(self) -> None:
        """고른 글이 있으면 복사, 없으면 입력 칸에 붙여넣기 (Windows 콘솔과 같은 방식)."""
        chosen = self.screen.get_selected_text()
        if chosen:
            self.copy_to_clipboard(chosen)
            self.screen.clear_selection()
            self.notify(t("tui.copied", n=len(chosen)), timeout=2)
            return
        if isinstance(self.screen, ModalScreen):
            return                                # 승인 상자 중에는 붙여넣지 않는다
        text = self.clip.paste()
        if not text:
            self.notify(t("tui.paste_empty"), timeout=2)
            return
        box = self.query_one(ChatInput)
        box.focus()
        box.post_message(events.Paste(text))      # 여러 줄은 입력 칸이 표시로 넣는다 (터미널 붙여넣기와 같은 길)

    def link_at(self, x: int, y: int) -> str | None:
        """화면 (x, y)의 웹 주소나 경로: 링크 스타일이 있으면 그것, 없으면 그 자리 글에서 찾는다."""
        try:
            link = self.screen.get_style_at(x, y).link
        except Exception:                         # 화면 밖, 그리는 중
            link = None
        if link and (URL_RE.fullmatch(link) or path_exists(link)):
            return link
        try:
            widget, offset = self.screen.get_widget_and_offset_at(x, y)
        except Exception:
            return None
        if widget is None or offset is None:
            return None
        if isinstance(widget, Lines):
            text = widget.plain()
        elif isinstance(widget, Input):
            text = widget.value
        else:
            got = widget.get_selection(Selection(None, None))
            text = got[0] if got else ""
        lines = text.split("\n")
        if not 0 <= offset.y < len(lines):
            return None
        return url_at(lines[offset.y], offset.x) or path_at(lines[offset.y], offset.x)

    def open_link(self, target: str) -> None:
        """웹 주소는 브라우저로, 경로는 탐색기·기본 프로그램으로 (실행 파일은 폴더에서 보여 주기만, M8.1)."""
        short = target if len(target) <= 80 else "…" + target[-79:]
        if URL_RE.fullmatch(target):
            self.notify(t("tui.link_open", url=short), timeout=2)
            self.open_url(target)
            return
        done = self.open_path(target)
        self.notify(t(f"tui.path_{done}", path=short), timeout=3)

    def open_path(self, path: str) -> str:
        return opener.open_path(path, self.engine.os)

    # ------------------------------------------------------------------ 존재감 (머리글 심볼)
    def set_presence(self, kind: str) -> None:
        if kind != self.presence_kind:
            self.presence_kind = kind
            self.presence_since = time.monotonic()
        timer = getattr(self, "_timer", None)
        if timer is not None:
            if self.motion and kind in ("work", "wait"):
                timer.resume()
            else:
                timer.pause()
        self.draw_presence()

    def draw_presence(self) -> None:
        elapsed = time.monotonic() - self.presence_since if self.motion else 0.0
        self.query_one("#presence", Static).update(logo.symbol(6, 3, logo.presence_at(self.presence_kind, elapsed)))

    def tick_presence(self) -> None:
        """작업·결정 대기 중에만 점이 움직인다. 입력 칸에 글이 있으면 멈춘다 (한글 조합을 건드리지 않게)."""
        if self.query_one(ChatInput).value:
            return
        self.draw_presence()
        self._ticks += 1
        if self._ticks % LIVE_EVERY == 0:
            self.refresh_live()

    # ------------------------------------------------------------------ 한글 입력 커서
    def set_ime_cursor(self, on: bool) -> None:
        """입력 칸에 있을 때만 터미널 커서를 보인다 (조합 중인 한글이 입력 칸에 뜨게)."""
        self._cursor_on = on
        driver = self._driver
        if driver is not None and not self.is_headless:
            driver.write(SHOW_CURSOR if on else HIDE_CURSOR)
            driver.flush()

    def _begin_update(self) -> None:
        if self._cursor_on and self._driver is not None and not self.is_headless:
            self._driver.write(HIDE_CURSOR)         # 그리는 동안 커서가 화면을 돌아다니지 않게
        super()._begin_update()

    def _end_update(self) -> None:
        super()._end_update()
        if self._cursor_on and self._driver is not None and not self.is_headless:
            self._driver.write(SHOW_CURSOR)         # 그린 뒤 커서는 입력 칸 글자 자리에 있다

    # ------------------------------------------------------------------ 입력
    @on(Input.Submitted, "#prompt")
    def submitted(self, event: Input.Submitted) -> None:
        self.submit()

    def submit_from_button(self) -> None:
        self.submit()
        self.query_one(ChatInput).focus()

    def submit(self) -> None:
        box = self.query_one(ChatInput)
        raw = box.value.strip()
        if not raw:
            return
        if self.job and raw.split()[0].lower() not in ("/quit", "/exit", "/q"):
            self.notify(t("tui.busy"), timeout=4)       # 글은 칸에 그대로 둔다
            return
        shown, full = box.take()
        if shown.startswith("/"):
            self.run_command(shown)
        elif shown.startswith("!"):
            self.start_direct(full[1:])
        else:
            self.start_task(shown, full)

    def run_command(self, text: str) -> None:
        cmd = text.split()[0].lower()
        if cmd in ("/quit", "/exit", "/q"):
            self.quit_now()
            return
        if self.job:
            self.notify(t("tui.busy"), timeout=4)
            return
        if cmd == "/new":
            self.new_conversation()
            return
        if cmd == "/resume":
            self.resume_command(text)
            return
        self.new_card("slash", text)
        if cmd == "/undo":
            self.start_undo()
            return
        self.engine.command(text)
        if cmd == "/help":
            self.add_line(Text(t("tui.help_keys"), style=STYLES["plain"]))
        if cmd == "/lang":
            self.relabel()
        self.current = None

    def clear_cards(self) -> None:
        for card in list(self.query(TaskCard)):
            card.remove()
        for item in list(self.query(NavItem)):
            item.remove()
        self.query_one("#nav-empty").display = True
        self.query_one("#activity", RichLog).clear()
        self.current = self.selected = None

    def new_conversation(self) -> None:
        """+ 새 대화 (/new): 기억을 비우고 처음 화면으로 (앞의 대화는 저장돼 있어 이어하기 카드로 보인다)."""
        if self.job:
            self.notify(t("tui.busy"), timeout=4)
            return
        self.engine.forget()
        self.clear_cards()
        self.home.set_resume(self.engine.resume_hint())
        self.home.display = True
        self.set_presence("idle")
        self.set_state(t("tui.state.idle"))
        self.refresh_placeholder()
        self.refresh_memory()
        self.refresh_aside()
        self.notify(t("new.done"), timeout=4)
        self.query_one(ChatInput).focus()

    def resume_session(self, sid: str) -> None:
        """처음 화면의 이어하기 카드."""
        self.resume_command("/resume", sid)

    def resume_command(self, text: str, sid: str = "") -> None:
        """/resume: 목록 카드. /resume N (또는 카드): 그 대화를 불러오고 화면을 비운 뒤 안내 카드."""
        if self.job:
            self.notify(t("tui.busy"), timeout=4)
            return
        e = self.engine
        if sid:
            info = e.load_session(sid)
            rows, loaded = (e.resumed_rows(info), True) if info is not None else ([("plain", t("resume.empty"))], False)
        else:
            rows, loaded = e.resume_lines(text.split()[1:])
        if loaded:
            self.clear_cards()
        card = self.new_card("slash", text)
        for style, line in rows:
            card.add(Text(line, style=STYLES.get(style, STYLES["plain"])))
        self.current = None
        if loaded:
            self.refresh_placeholder()
            self.refresh_memory()
            self.refresh_aside()

    # ------------------------------------------------------------------ 작업
    def start_task(self, shown: str, full: str) -> None:
        self.new_card("task", shown)
        self.begin_job("task", t("tui.state.thinking", step=1))
        self.task_worker(full)

    @work(thread=True, exclusive=True, group="job", exit_on_error=False)
    def task_worker(self, request: str) -> None:
        result = None
        try:
            result = self.engine.ask_task(request, cancelled=self.stop_event.is_set)
        except Exception as e:                  # 화면은 살아 있게
            self.post(self.add_line, Text(t("tui.error", detail=str(e)), style=STYLES["plain"]))
        finally:
            self.post(self.end_task, result)

    def start_direct(self, command: str) -> None:
        command = command.strip()
        card = self.new_card("command", "!" + command)
        if not command or CD.match(command):
            self.engine.run_direct(command)       # 빈 명령 안내, !cd는 바로 (작업 폴더만 바꿈)
            card.code = 0
            card.finish("done")
            self.move_to_done(card)
            self.current = None
            self.update_status()
            return
        self.begin_job("command", t("tui.state.command"))
        self.direct_worker(command)

    @work(thread=True, exclusive=True, group="job", exit_on_error=False)
    def direct_worker(self, command: str) -> None:
        result: Direct | None = None
        try:
            result = self.engine.execute_direct(command)
        except Exception as e:
            self.post(self.add_line, Text(t("tui.error", detail=str(e)), style=STYLES["plain"]))
        finally:
            self.post(self.end_command, result)

    def begin_job(self, kind: str, state: str) -> None:
        self.stop_event.clear()
        self.meter.reset()                            # 이 작업에서 주고받는 토큰을 처음부터 센다
        self._shown_second = -1
        self.job = kind
        self.step = 0
        self.set_state(state, WHITE)
        self.set_presence("work")

    def end_task(self, result: Any) -> None:
        card = self.current
        stopped = result is None or getattr(result, "status", "") != "done"
        if card is not None:
            card.finish("stopped" if stopped else "done")
            self.move_to_done(card)
            self.counts["stopped" if stopped else "done"] += 1
        self.end_job(card, stopped)

    def end_command(self, d: Direct | None) -> None:
        card = self.current
        stopped = self.stop_event.is_set()
        if card is not None:
            if d is None:
                card.finish("failed")
            elif d.interrupted or (stopped and d.result is not None):
                self.show_direct_output(card, d)
                card.add(Text(t("direct.interrupted"), style=FAINT))
                card.finish("stopped")
            elif d.error:
                card.add(Text(t("direct.failed", detail=d.error), style=STYLES["plain"]))
                card.finish("failed")
            else:
                self.show_direct_output(card, d)
                assert d.result is not None
                card.code = d.result.code
                card.finish("timeout" if d.result.timed_out else "done")
            self.move_to_done(card)
            self.counts["commands"] += 1
        self.end_job(card, stopped)

    def show_direct_output(self, card: TaskCard, d: Direct) -> None:
        """!명령 출력은 카드 안 상자에 (Workbench의 내용 상자). 아래에 실행한 폴더."""
        res = d.result
        if res is None:
            return
        out = res.out.rstrip().splitlines()
        for line in out[:DIRECT_LINES_SHOWN]:
            card.add_output(Text(line, style=WHITE))
        if len(out) > DIRECT_LINES_SHOWN:
            card.add_output(Text(t("direct.more", count=len(out) - DIRECT_LINES_SHOWN), style=FAINT))
        if res.err.strip():
            card.add_output(Text(t("direct.stderr"), style=FAINT))
            for line in res.err.rstrip().splitlines()[:DIRECT_LINES_SHOWN]:
                card.add_output(Text(line, style=MUTED))
        if not res.out.strip() and not res.err.strip():
            card.add_output(Text(t("direct.no_output"), style=FAINT))
        card.add(Text(d.cwd, style=FAINT))

    def end_job(self, card: TaskCard | None, stopped: bool) -> None:
        if self.closing:
            return
        if card is not None:
            card.hide_live()                          # 중간 토큰은 지운다 (답은 카드에 남는다)
            if card.kind == "task":
                sent, received, _, _ = self.meter.snapshot()
                self.query_one("#activity", RichLog).write(Text("   " + t(
                    "tui.activity.total", time=_clock(card.seconds), sent=_tokens(sent), received=_tokens(received)),
                    style=FAINT))
        self.job = ""
        self.stop_event.clear()
        self.current = None
        if card is not None:
            self.set_state(t("tui.state.stopped") if stopped else t("tui.state.done", seconds=_secs(card.seconds)))
        else:
            self.set_state(t("tui.state.idle"))
        self.set_presence("done")
        self.refresh_memory()
        self.refresh_aside()
        self.main.call_after_refresh(self.main.scroll_end, animate=False)

    def action_stop_task(self) -> None:
        """Esc: 작업 중이면 멈춘다 (쓰던 글은 건드리지 않는다)."""
        if not self.job or self.stop_event.is_set():
            return
        self.stop_event.set()
        shell.kill_running(self.engine.os)        # GLaDOS가 돌리던 명령도 끝낸다
        self.set_state(t("tui.state.stopping"), WHITE)
        self.close_dialog()

    def action_help_quit(self) -> None:
        """Ctrl+C: 작업 중이면 멈춤, 아니면 끝내는 법 안내 (Textual 기본은 안내만)."""
        if self.job:
            self.action_stop_task()
        else:
            self.notify(t("tui.quit_hint"), timeout=3)

    def quit_now(self) -> None:
        """끝내기: 돌던 작업과 명령을 멈추고 닫는다 (작업 스레드는 곧 끝난다)."""
        self.close_dialog()
        self.release()
        self.exit()

    def release(self) -> None:
        """작업 스레드가 기다리는 것을 모두 풀어 준다 (모델 호출, 승인, 명령)."""
        self.closing = True
        self.stop_event.set()
        shell.kill_running(self.engine.os)

    async def action_quit(self) -> None:
        self.quit_now()

    def on_unmount(self) -> None:
        self.release()                            # 어떤 길로 닫히든 작업 스레드가 남지 않게

    # ------------------------------------------------------------------ 승인·되돌리기
    def ask_approval(self, call: ToolCall, d: Decision) -> str:
        """작업 스레드에서 불린다. 상자를 띄우고 고를 때까지 기다린다. 멈추면 거절."""
        if self.stop_event.is_set():
            return "deny"
        done = threading.Event()
        box: dict[str, str] = {}

        def closed(choice: str | None) -> None:
            box["choice"] = choice or "deny"
            done.set()

        self.post(self.open_approval, call, d, closed)
        while not done.wait(0.1):
            if self.stop_event.is_set():
                self.post(self.close_dialog)
                done.wait(2.0)
                break
        choice = box.get("choice", "deny")
        if self.stop_event.is_set():
            choice = "deny"
        self.post(self.show_choice, call, choice)
        return choice

    def open_approval(self, call: ToolCall, d: Decision, closed: Callable[[str | None], None]) -> None:
        if self.current is not None:
            self.current.state = "waiting"
            self.current.refresh_kicker()
        self.set_state(t("tui.state.approval", tool=call.tool), COBALT)
        self.set_presence("wait")
        self.open_dialog(approval_dialog(call, d), closed)      # 상자는 화면 스레드에서 만든다

    def open_dialog(self, dialog: ChoiceDialog, closed: Callable[[str | None], None]) -> None:
        def done(choice: str | None) -> None:
            self._dialog = None
            self.set_ime_cursor(True)             # 입력 칸으로 돌아감
            closed(choice)

        self._dialog = dialog
        self.set_ime_cursor(False)                # 상자가 떠 있는 동안 입력 칸 커서는 숨김
        self.push_screen(dialog, done)

    def close_dialog(self) -> None:
        if self._dialog is not None and self._dialog.is_attached:
            self._dialog.choose(self._dialog.options[-1][0])

    def show_choice(self, call: ToolCall, choice: str) -> None:
        who = {"once": "연구원님:이번만", "task": "연구원님:이 작업 동안"}.get(choice, "연구원님:거절")
        line = Text.assemble((t("tui.decided"), COBALT), "  ", (f"{call.tool} {_brief(call.args, 100)}", FAINT),
                             ("  ·  ", DIM), (i18n.who_text(who), COBALT if choice != "deny" else MUTED))
        card = self.current
        if card is not None:
            card.add(line)
            card.state = "running"
            card.refresh_kicker()
        if self.job == "task" and not self.stop_event.is_set():
            self.set_presence("work")
            if choice == "deny":
                self.set_state(t("tui.state.thinking", step=self.step + 1), WHITE)
            else:
                self.set_state(t("tui.state.tool", tool=call.tool, step=max(1, self.step)), WHITE)

    def start_undo(self) -> None:
        e = self.engine
        card = self.current
        self.current = None
        cp = e.last_checkpoint()
        if cp is None:
            if card is not None:
                card.add(Text(t("undo.none"), style=STYLES["plain"]))
            return

        def closed(choice: str | None) -> None:
            text = t("undo.done", path=e.restore(cp)) if choice == "restore" else t("undo.kept")
            if card is not None:
                card.add(Text(text, style=STYLES["plain"]))
            self.set_presence("done")

        self.set_presence("wait")
        self.open_dialog(undo_dialog(e.undo_text(cp)), closed)

    # ------------------------------------------------------------------ 시작
    @work(thread=True, group="check", exit_on_error=False)
    def check_model_worker(self) -> None:
        try:
            ok = self.engine.check_model()       # 안 되면 그 까닭을 처음 화면에 쓴다
        except Exception as e:
            self.post(self.add_line, Text(t("tui.error", detail=str(e)), style=STYLES["plain"]))
            ok = False
        self.post(self.model_checked, ok)

    def model_checked(self, ok: bool) -> None:
        if not self.job:
            self.set_state(t("tui.state.idle") if ok else t("tui.state.no_model"))


def run_tui(cfg: Config, first_run: bool = False, notes: list[str] | None = None) -> int:
    shell.DETACH_CONSOLE = True                   # Windows: 명령이 TUI의 콘솔을 건드리지 못하게
    app = GladosTUI(cfg, first_run=first_run, notes=notes)
    moved = app.engine.leave_safety_cwd()        # 소스 폴더에서 열었으면 작업 공간으로 (파일·명령이 막히지 않게)
    if moved:
        app.notes.append(t("cwd.moved", path=moved))
    app.engine.start_background()                # 내용 색인 프로세스 (M7.1)
    try:
        app.run()
    finally:
        app.engine.close()
    print(t("bye"))
    return 0

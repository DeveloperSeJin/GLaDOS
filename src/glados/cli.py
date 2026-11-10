"""터미널 화면: 대화(REPL)와 한 줄 실행 (`glados "..."`).

- 입력은 input()으로 받는다. 한글 조합(IME)을 터미널에 맡기는 가장 단순한 방식이라
  Windows Terminal에서도 한글이 가장 덜 깨진다. M1 첫날 한글 입력 시험의 대상
- 화면 출력은 이 파일에만 둔다. 엔진은 이벤트만 낸다
- 사실(fact)은 굵게, 위트(wit)는 흐리게, 결정(승인)은 코발트
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

import glados
from glados import __version__, osinfo
from glados.config import Config, ConfigError, ensure_home, glados_home, load_config
from glados.core import events as ev
from glados.core.events import Event
from glados.core.llm import LLMError, OllamaClient, OllamaUnavailable
from glados.core.loop import TaskResult, run_task
from glados.core.prompts import build_system_prompt
from glados.hands import load_hands
from glados.memory.log import PolicyLog, TaskLog, summarize_args
from glados.safety.locations import Places
from glados.safety.policy import Decision, Policy, PolicyContext, ToolCall

HISTORY_TURNS = 6   # 맥락에 남길 지난 대화 수

USAGE = """사용법
  glados              대화 시작
  glados "할 일"      한 줄로 맡기고 답을 받은 뒤 끝냄
  glados --version    버전

설정·기록 폴더: GLADOS_HOME 환경 변수, 없으면 ~/.glados"""

HELP = """명령어
  /help     이 도움말
  /new      새 작업 (앞의 대화를 맥락에서 뺌)
  /log      최근 정책 판정 10개
  /quit     끝내기
  /undo /servers /runs /resume   다음 단계에서 만듭니다
작업 중 Ctrl+C를 누르면 지금 작업만 멈춥니다."""

LATER = {"/undo": "M2", "/servers": "M4", "/runs": "M5", "/resume": "M6"}


class Screen:
    def __init__(self, ansi: bool, out: TextIO | None = None):
        self.ansi = ansi
        self.out = out or sys.stdout

    def _style(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.ansi else text

    def bold(self, text: str) -> str:
        return self._style("1", text)

    def dim(self, text: str) -> str:
        return self._style("2", text)

    def cobalt(self, text: str) -> str:
        return self._style("38;2;77;124;255", text)   # #4D7CFF, 결정에만

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)


def source_dirs() -> tuple[str, ...]:
    """GLaDOS 설치·소스 폴더 (안전장치). 개발 중이면 프로젝트 폴더 전체."""
    pkg = Path(glados.__file__).resolve().parent
    dirs = [str(pkg)]
    if pkg.parent.name == "src" and (pkg.parent.parent / "pyproject.toml").exists():
        dirs.append(str(pkg.parent.parent))
    return tuple(dirs)


class App:
    def __init__(self, cfg: Config, screen: Screen, llm=None, os_name: str | None = None):
        self.cfg = cfg
        self.screen = screen
        self.os = os_name or osinfo.current_os()
        self.llm = llm if llm is not None else OllamaClient(cfg.model)
        self.tools = load_hands(self.os)
        self.policy = Policy(cfg.policy_layers)
        self.places = Places.for_this_computer(
            cfg.home, source_dirs=source_dirs(), extra_protected=tuple(cfg.extra_protected)
        )
        self.task_log = TaskLog(cfg.home)
        self.policy_log = PolicyLog(cfg.home)
        self.history: list[dict[str, str]] = []
        self.system_prompt = build_system_prompt(osinfo.OS_LABELS[self.os], cfg.model.name)

    # ------------------------------------------------------------------
    def header(self) -> str:
        mode = " (낮은 모드)" if self.cfg.model.is_low else ""
        return f"GLaDOS v0 · 모델 {self.cfg.model.name}{mode} · {osinfo.OS_LABELS[self.os]} · 기록 {self.cfg.home}"

    def check_model(self) -> bool:
        s = self.screen
        try:
            if not self.llm.has_model():
                s.say(f"모델 {self.cfg.model.name}이(가) 없습니다. 'ollama pull {self.cfg.model.name}'으로 받은 뒤 다시 실행하세요.")
                return False
        except OllamaUnavailable:
            s.say(f"Ollama에 연결할 수 없습니다 ({self.cfg.model.host}). Ollama가 켜져 있는지 확인하세요.")
            return False
        except LLMError as e:
            s.say(f"Ollama를 확인할 수 없습니다. {e}")
            return False
        return True

    # ------------------------------------------------------------------
    def ask_task(self, request: str) -> TaskResult | None:
        task_id = self.task_log.new_task(request)
        ctx = PolicyContext(places=self.places, registered_tools=frozenset(self.tools.names()))

        def emit(e: Event) -> None:
            self.task_log.event(task_id, e)
            self.render(e)

        def audit(call: ToolCall, d: Decision, who: str) -> None:
            self.policy_log.write(call.tool, call.args, d.verdict, d.rule, who, task_id)

        try:
            result = run_task(
                request,
                llm=self.llm,
                policy=self.policy,
                ctx=ctx,
                tools=self.tools,
                limits=self.cfg.limits,
                system_prompt=self.system_prompt,
                history=self.history[-HISTORY_TURNS * 2:],
                emit=emit,
                approve=self.approve,
                audit=audit,
                overflow_dir=self.cfg.home / "logs" / "outputs",
                task_id=task_id,
            )
        except KeyboardInterrupt:
            self._abort(task_id, "user", "멈췄습니다.")
            return None
        except LLMError as e:
            self._abort(task_id, "llm", f"모델을 부를 수 없습니다. {e}")
            return None

        self.task_log.finish(task_id, result.status)
        if result.status == "done" and result.answer is not None:
            reply = result.answer.fact + (f" {result.answer.wit}" if result.answer.wit else "")
            self.history += [{"role": "user", "content": request}, {"role": "assistant", "content": reply}]
        return result

    def _abort(self, task_id: str, reason: str, message: str) -> None:
        self.task_log.event(task_id, Event(ev.STOP, {"reason": reason, "message": message}))
        self.task_log.finish(task_id, "stopped")
        self.screen.say(message)

    # ------------------------------------------------------------------
    def render(self, e: Event) -> None:
        s = self.screen
        d = e.data
        if e.kind == ev.START:
            s.say(s.dim("작업 중..."))
        elif e.kind == ev.ANSWER:
            s.say(s.bold(d["fact"]))
            if d.get("wit"):
                s.say(s.dim(d["wit"]))
            if d.get("sources"):
                s.say(s.dim("출처: " + ", ".join(d["sources"])))
        elif e.kind == ev.PROPOSAL:
            s.say(s.dim(f"... {d['tool']}"))
        elif e.kind == ev.DECISION and d["verdict"] == "deny":
            s.say(s.dim(f"[차단] {d['tool']}: {d['reason']}"))
        elif e.kind == ev.STOP:
            s.say(d["message"])

    def approve(self, call: ToolCall, d: Decision) -> str:
        s = self.screen
        s.say(s.cobalt("[승인 필요]") + f" {d.reason}")
        s.say(f"  {call.tool} {summarize_args(call.args, 120)}")
        options = "  1) 이번만   2) 이 작업 동안   3) 거절" if d.grantable else "  1) 이번만   3) 거절"
        s.say(options)
        while True:
            try:
                choice = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                s.say()
                return "deny"
            if choice == "1":
                return "once"
            if choice == "2" and d.grantable:
                return "task"
            if choice == "3":
                return "deny"
            s.say(s.dim("1, 2, 3 중에서 골라 주세요." if d.grantable else "1 또는 3을 골라 주세요."))

    # ------------------------------------------------------------------
    def command(self, text: str) -> bool:
        """/명령을 처리한다. 끝내야 하면 True."""
        s = self.screen
        cmd = text.split()[0].lower()
        if cmd in ("/quit", "/exit", "/q"):
            return True
        if cmd == "/help":
            s.say(HELP)
        elif cmd == "/new":
            self.history.clear()
            s.say("새 작업을 시작합니다. 앞의 대화는 맥락에서 뺐습니다.")
        elif cmd == "/log":
            records = self.policy_log.recent(10)
            if not records:
                s.say("아직 정책 판정 기록이 없습니다. 도구는 M2부터 생깁니다.")
            for r in records:
                when = datetime.fromtimestamp(r.get("ts", 0)).strftime("%m-%d %H:%M")
                s.say(f"{when}  {r.get('tool')}  {r.get('verdict')}  {r.get('rule')}  ({r.get('decided_by')})")
        elif cmd in LATER:
            s.say(f"{cmd}는 {LATER[cmd]}에서 만듭니다.")
        else:
            s.say(f"모르는 명령입니다: {cmd}. /help를 보세요.")
        return False

    def repl(self, first_run: bool = False) -> int:
        s = self.screen
        s.say(s.dim(self.header()))
        if first_run:
            s.say(f"처음 뵙겠습니다. 설정 파일을 {self.cfg.home}에 만들었습니다. 무엇을 실험할까요?")
        else:
            s.say("다시 오셨군요. 무엇을 실험할까요?")
        while True:
            try:
                line = input("> ")
            except EOFError:
                s.say()
                break
            except KeyboardInterrupt:
                s.say()
                continue
            text = line.strip()
            if not text:
                continue
            if text.startswith("/"):
                if self.command(text):
                    break
                continue
            self.ask_task(text)
        s.say(s.dim("기록은 남겨 두었습니다."))
        return 0

    def close(self) -> None:
        self.task_log.close()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    if args[:1] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if args[:1] == ["--version"]:
        print(f"GLaDOS {__version__}")
        return 0

    screen = Screen(osinfo.enable_ansi())
    if osinfo.is_admin():
        screen.say("관리자 권한으로 실행되어 멈췄습니다. GLaDOS는 일반 권한으로 실행해야 합니다. 관리자가 아닌 창에서 다시 실행하세요.")
        return 2

    home = glados_home()
    try:
        created = ensure_home(home)
        cfg = load_config(home)
    except (ConfigError, OSError) as e:
        screen.say(f"설정을 읽을 수 없습니다. {e}")
        return 1

    if not osinfo.utf8_mode_on():
        screen.say(screen.dim("참고: PYTHONUTF8이 꺼져 있습니다. 'setx PYTHONUTF8 1' 후 새 창에서 실행하면 한글 처리가 더 안전합니다."))

    app = App(cfg, screen)
    try:
        if not app.check_model():
            return 1
        if args:
            result = app.ask_task(" ".join(args))
            return 0 if result is not None and result.status == "done" else 1
        return app.repl(first_run=bool(created))
    finally:
        app.close()

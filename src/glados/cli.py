"""줄 화면: 대화(REPL)와 한 줄 실행 (`glados "..."`). 엔진 연결(App)도 여기 있다 (TUI가 같이 쓴다).

- `glados`는 터미널이면 TUI 화면(tui.py)으로 연다. `glados --plain`이나 [ui] screen = "plain"이면 이 줄 화면
- 줄 화면의 입력은 input()으로 받는다. 한글 조합(IME)을 터미널에 맡기는 가장 단순한 방식이라
  Windows Terminal에서도 한글이 가장 덜 깨진다. TUI에서 한글 입력이 이상하면 이 화면을 쓴다
- 화면 출력은 화면 파일(cli.py, tui.py)에만 둔다. 엔진은 이벤트만 낸다. 이벤트 -> 화면 줄은 event_lines() 하나
- 화면 글은 모두 i18n 표에서 가져온다 (glados.toml [ui] language, /lang으로 이번 세션만 바꾸기)
- 엔진 안쪽은 영어 (M2.5). 답은 연구원님이 쓴 언어로 번역되어 온다 (ANSWER 이벤트). 번역을 못 하면 영어 그대로
- 사실(fact)은 굵게, 위트(wit)는 흐리게, 결정(승인)은 코발트
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TextIO

import glados
from glados import __version__, i18n, osinfo
from glados.config import Config, ConfigError, ensure_home, glados_home, load_config, peek_language
from glados.core import events as ev
from glados.core.context import (
    BRIEF, CLIPPED, DROPPED, FULL, MESSAGE_OVERHEAD, TASK_RESERVE, Conversation, Turn, limit_chars,
)
from glados.core.events import Event
from glados.core.guards import DEFAULT_GUARDS, needs_evidence
from glados.core.llm import Cancelled, LLMError, OllamaClient, OllamaUnavailable
from glados.core.loop import TaskResult, clip_output, run_task
from glados.core.prompts import build_system_prompt, reply_note
from glados.core.translate import Glossary, Translator, reply_language
from glados.hands import HandContext, ToolError, load_hands
from glados.hands import shell
from glados.hands.web import Egress, search_status
from glados.i18n import localized, t
from glados.memory.log import PolicyLog, TaskLog, summarize_args
from glados.memory.notes import Notes, budget as notes_budget
from glados.memory.sessions import SessionInfo, SessionStore
from glados.safety.privacy import secret_in_note
from glados.safety.checkpoint import Checkpoints
from glados.safety.locations import Places, classify, normalize
from glados.safety.policy import Decision, Policy, PolicyContext, ToolCall

LATER: dict[str, str] = {}   # 다음 단계에서 만들 명령 (/명령: 단계)
LEVELS = (FULL, CLIPPED, BRIEF, DROPPED)
STOPS = {"format", "same_call", "tool_errors", "max_steps", "user", "denied"}   # 엔진이 키로 알려 주는 멈춤 이유
SOURCES_SHOWN = 3        # 화면에 보여 줄 출처 수
SHELL_LINES_SHOWN = 12   # GLaDOS가 실행한 명령의 출력을 화면에 몇 줄까지
DIRECT_LINES_SHOWN = 200  # !명령 출력을 화면에 몇 줄까지
DIRECT_TIMEOUT_S = 600   # !명령 시간 제한 (Ctrl+C로 언제든 멈춤)
RESUME_LIST = 10         # /resume 목록에 보일 지난 대화 수
EARLIER_SHOWN = 3        # 기억이 비었을 때 모델에게 알려 줄 지난 대화 수
EARLIER_TITLE = 60       # 그 제목(첫 요청)의 최대 길이
CD = re.compile(r"^(?:cd|chdir|Set-Location|sl)(?:\s+(.*))?$", re.IGNORECASE)


@dataclass
class Direct:
    """!명령 하나의 결과 (화면이 보여 줄 것)."""
    command: str
    cwd: str
    result: shell.CommandResult | None = None
    error: str = ""
    interrupted: bool = False


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

    def styled(self, style: str, text: str) -> str:
        """event_lines()의 모양 이름 -> 이 화면의 글."""
        if style == "speaker":
            return self.cobalt("●") + " " + self.dim(text)
        if style == "fact":
            return self.bold(text)
        if style in ("wit", "dim"):
            return self.dim(text)
        if style == "decision":
            return self.cobalt(text)
        return text

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)


class _Keyed:
    """이벤트에 담긴 키와 값을 localized()에 넘기는 작은 그릇."""

    def __init__(self, key: str, params: dict):
        self.key = key
        self.params = tuple(params.items())


def _brief(args: dict, limit: int = 100) -> str:
    """도구 인자를 한 줄로 (긴 값은 자름)."""
    parts = []
    for k, v in args.items():
        text = str(v).replace("\n", " ")
        parts.append(f"{k}={text[:40] + '...' if len(text) > 40 else text}")
    line = " ".join(parts)
    return line if len(line) <= limit else line[:limit] + "..."


def event_lines(e: Event) -> list[tuple[str, str]]:
    """엔진 이벤트 하나 -> 화면 줄 [(모양, 글)]. 줄 화면과 TUI가 같이 쓴다.

    모양: speaker(GLaDOS의 코발트 점 + 이름) · fact(굵게, 사실) · wit(흐리게, 위트) · dim(흐리게, 과정) · plain."""
    d = e.data
    out: list[tuple[str, str]] = []
    if e.kind == ev.START:
        out.append(("dim", t("render.working")))
    elif e.kind == ev.ANSWER:
        out.append(("speaker", t("speaker.glados")))             # 누가 말하는지 (M6.3: 연구원님 말과 구별)
        out.append(("fact", d["fact"]))
        if d.get("wit"):
            out.append(("wit", d["wit"]))
        if d.get("untranslated"):
            out.append(("dim", t("render.untranslated")))
        sources = list(d.get("sources") or [])
        if len(sources) > SOURCES_SHOWN:
            out.append(("dim", t("render.sources_more", sources=", ".join(sources[:SOURCES_SHOWN]),
                                 count=len(sources) - SOURCES_SHOWN)))
        elif sources:
            out.append(("dim", t("render.sources", sources=", ".join(sources))))
    elif e.kind == ev.OBSERVATION and d.get("tool") == "shell.run":
        lines = str(d.get("text", "")).splitlines()[1:]          # 첫 줄($ 명령)은 위에 이미 나옴
        out.extend(("dim", "  " + line) for line in lines[:SHELL_LINES_SHOWN])
        if len(lines) > SHELL_LINES_SHOWN:
            out.append(("dim", "  ..."))
    elif e.kind == ev.PLAN:
        out.append(("dim", t("render.plan")))
        out.extend(("dim", f"  {i}. {step}") for i, step in enumerate(d.get("plan") or [], 1))
    elif e.kind == ev.PROPOSAL:
        out.append(("dim", f"... {d['tool']} {_brief(d.get('args', {}))}"))
    elif e.kind == ev.DECISION and d["verdict"] == "deny":
        reason = localized(_Keyed(d.get("key", ""), d.get("params", {})), fallback=d["reason"])
        out.append(("dim", t("render.blocked", tool=d["tool"], reason=reason)))
    elif e.kind == ev.GUARD:
        out.append(("dim", "... " + t(d.get("key") or "guard.evidence")))
    elif e.kind == ev.EGRESS:
        what = str(d.get("what", "")).replace("\n", " ")
        out.append(("dim", t("render.egress_search" if d.get("tool") == "web.search" else "render.egress_fetch",
                             host=d.get("host", ""), what=what[:120] + ("..." if len(what) > 120 else ""))))
    elif e.kind == ev.STOP:
        reason = d.get("reason")
        message = t(f"stop.{reason}", **d.get("params", {})) if reason in STOPS else d["message"]
        if d.get("answered"):
            out.append(("dim", f"({message})"))      # 답은 위에 나왔으니 멈춘 이유만 흐리게
        else:
            out.append(("plain", message))
            if reason == "tool_errors" and d.get("detail"):
                out.append(("dim", t("stop.error_detail", detail=d["detail"])))
    return out


def source_dirs() -> tuple[str, ...]:
    """GLaDOS 설치·소스 폴더 (안전장치). 개발 중이면 프로젝트 폴더 전체."""
    pkg = Path(glados.__file__).resolve().parent
    dirs = [str(pkg)]
    if pkg.parent.name == "src" and (pkg.parent.parent / "pyproject.toml").exists():
        dirs.append(str(pkg.parent.parent))
    return tuple(dirs)


def dev_run() -> bool:
    """저장소의 .venv로 돌고 있나 (`uv run glados`). 설치한 `glados` 명령(uv tool)은 자기 환경에서 돈다 (M6.2)."""
    try:
        prefix = Path(sys.prefix).resolve()
    except OSError:
        return False
    for src in source_dirs()[1:]:                     # [1:]: 저장소 폴더 (개발 중일 때만 있음)
        root = Path(src)
        if prefix == root or root in prefix.parents:
            return True
    return False


class App:
    def __init__(self, cfg: Config, screen: Screen, llm=None, os_name: str | None = None):
        self.cfg = cfg
        self.screen = screen
        i18n.set_language(cfg.language)
        self.os = os_name or osinfo.current_os()
        self.llm = llm if llm is not None else OllamaClient(cfg.model)
        self.policy = Policy(cfg.policy_layers)
        self.places = replace(
            Places.for_this_computer(cfg.home, source_dirs=source_dirs(), extra_protected=tuple(cfg.extra_protected)),
            cwd=os.getcwd(),                              # 작업 폴더: GLaDOS를 실행한 곳 (!cd로 바꿈)
        )
        self.hctx = HandContext(
            places=self.places,
            home=cfg.home,
            search_default=list(cfg.search_default),
            checkpoints=Checkpoints(cfg.home),
            user_dirs=osinfo.user_folders(self.places.home, self.os),
            command_timeout_s=max(5, cfg.limits.local_timeout_s - 5),
            glossary=Glossary(self.llm, cfg.home / "cache" / "names-en.json"),   # 이름 속 낱말 -> 영어 (찾기)
            llm=self.llm, model_name=cfg.model.name,                             # fs.inspect가 파일마다 묻는 모델
            web=cfg.web, egress=Egress(cfg.home / "logs" / "egress.jsonl"),     # 웹 손 (M3): 외부 전송 기록
            servers=list(cfg.servers),                                          # 원격 손 (M4): servers.toml
            sessions=SessionStore(cfg.home),                                    # 대화 세션 (M6): 이어하기
            notes=Notes(cfg.home, cfg.language),                                # MEMORY.md (M6): "기억해"
        )
        self.translator = Translator(self.llm, keep=[s.alias for s in cfg.servers])   # 영어 답 -> 연구원님의 언어 (서버 이름은 그대로)
        self.tools = load_hands(self.os, self.hctx)
        self.task_log = TaskLog(cfg.home)
        self.policy_log = PolicyLog(cfg.home)
        self.memory = Conversation()                      # 이 세션의 대화 (지난 작업을 통째로 기억)
        self.sessions: SessionStore = self.hctx.sessions  # 작업마다 저장 (M6). 지금 세션 번호는 hctx.session_id
        self.notes: Notes = self.hctx.notes
        self.context_chars = limit_chars(cfg.model.num_ctx)
        self.runner = self.hctx.runner                    # 서버 실행 (M5). 서버가 없으면 None
        if self.runner is not None:
            self.runner.on_notice = None                  # 화면이 준비되면 watch()가 붙인다 (그 전 알림은 모아 둠)
            # 코드를 읽어 메모리를 추정하고 지표 형식을 정하는 모델: 따로 연결 (화면의 멈춤·토큰 표시와 섞이지 않게)
            self.runner.llm = OllamaClient(cfg.model) if isinstance(self.llm, OllamaClient) else self.llm
            self.runner.exp_root = Path(self.workspace()) / "experiments"   # 실험 세션 폴더 (표·그래프·요약)
            self.runner.expire_waiting()                  # 지난 세션의 대기 줄은 취소 (켜져 있는 동안만 기다림)

    def system_prompt(self) -> str:
        """작업마다 새로 만든다 (지금 시각, 작업 폴더가 맞도록)."""
        names = self.tools.names()
        return build_system_prompt(
            osinfo.OS_LABELS[self.os], self.cfg.model.name, tool_names=names,
            shell=shell.shell_label(self.os) if "shell.run" in names else "",
            cwd=self.places.cwd or "",
            servers=[f"{s.alias} ({s.gpus or '?'} GPUs, work folder {s.workdir or '~'}, projects in "
                     f"{(s.workdir or '~').rstrip('/')}/{s.projects})" for s in self.cfg.servers],
            runs=self.runner.summary_line() if self.runner is not None else "",
            notes=self.notes.prompt(notes_budget(self.cfg.model.num_ctx)),
            earlier=self.earlier_sessions_line(),
        )

    def earlier_sessions_line(self) -> str:
        """기억이 비었을 때만: 최근 지난 대화 몇 개 (영어, 모델용). "아까 PDF 찾던 거 이어서"에 모델이 맞는 것을 골라
        session.resume을 부르게 (가장 최근 하나만 보여 주면 그게 엉뚱한 대화일 때 단서가 없다, M6.1)."""
        if self.memory.turns:
            return ""
        recent = self.sessions.recent(EARLIER_SHOWN, exclude=self.hctx.session_id)
        if not recent:
            return ""
        items = []
        for info in recent:
            when = datetime.fromtimestamp(info.last).strftime("%Y-%m-%d %H:%M")
            title = info.title if len(info.title) <= EARLIER_TITLE else info.title[:EARLIER_TITLE - 1] + "…"
            items.append(f'{when} "{title}" ({info.count} task{"" if info.count == 1 else "s"})')
        return "Earlier conversations (newest first): " + "; ".join(items) + "."

    def set_cwd(self, path: str) -> None:
        self.places = replace(self.places, cwd=path)
        self.hctx.places = self.places

    def cwd_is_safety(self) -> bool:
        return classify(self.places.cwd or self.places.home, self.places).safety

    def workspace(self) -> str:
        """GLaDOS가 새 파일을 만들고 명령을 돌려도 되는 곳: folders.toml workspace, 없으면 문서 폴더 안의 GLaDOS
        (실제 문서 폴더: OneDrive·다른 드라이브로 옮긴 위치까지), 그것도 안 되면 홈 안의 GLaDOS."""
        cands = []
        if self.cfg.workspace:
            cands.append(normalize(self.cfg.workspace, self.places))
        docs = self.hctx.user_dirs.get("documents") or ()
        base = next((d for d in docs if Path(d).is_dir()), docs[0] if docs else "")
        if base:
            cands.append(str(Path(base) / "GLaDOS"))
        cands.append(str(Path(self.places.home) / "GLaDOS"))
        for cand in cands:
            loc = classify(cand, self.places)
            if not loc.safety and not loc.protected:
                return cand
        return self.places.home

    def leave_safety_cwd(self, dev: bool | None = None) -> str:
        """`uv run glados`(개발용)를 자기 폴더(소스·GLADOS_HOME)에서 열었으면 작업 폴더를 작업 공간으로 옮긴다
        (uv run은 늘 소스 폴더에서 열리는데, 거기서는 파일을 만들거나 명령을 돌릴 수 없다).
        설치한 `glados` 명령은 연 폴더를 그대로 쓴다 (M6.2 연구원님 결정: 어디서 열지 연구원님이 고른 것).
        자기 폴더면 시작 화면이 "GLaDOS가 부르는 명령은 읽기만" 안내를 보여 준다.
        옮긴 곳을 돌려준다 (안 옮겼으면 ""). 화면을 열 때 한 번 부른다 (시험에서 만드는 App은 부르지 않음)."""
        if not (dev_run() if dev is None else dev) or not self.cwd_is_safety():
            return ""
        target = self.workspace()
        try:
            Path(target).mkdir(parents=True, exist_ok=True)
        except OSError:
            return ""
        self.set_cwd(target)
        return target

    def history_budget(self) -> int:
        """다음 작업에서 지난 대화에 쓸 글자 수 (루프가 작업을 시작할 때 쓰는 것과 같은 계산)."""
        note = max(len(reply_note("en")), len(reply_note("ko")))
        system = len(self.system_prompt()) + 2 + len(self.tools.describe()) + 2 + note + MESSAGE_OVERHEAD
        return max(0, self.context_chars - system - TASK_RESERVE)

    # ------------------------------------------------------------------
    def header(self) -> str:
        mode = t("header.low") if self.cfg.model.is_low else ""
        return t("header", model=self.cfg.model.name, mode=mode, os=osinfo.OS_LABELS[self.os], home=self.cfg.home)

    def check_model(self) -> bool:
        s = self.screen
        try:
            if not self.llm.has_model():
                s.say(t("model.missing", model=self.cfg.model.name))
                return False
        except OllamaUnavailable:
            s.say(t("ollama.unreachable", host=self.cfg.model.host))
            return False
        except LLMError as e:
            s.say(t("ollama.check_failed", detail=localized(e)))
            return False
        return True

    # ------------------------------------------------------------------
    def ask_task(self, request: str, cancelled=None) -> TaskResult | None:
        """작업 하나. cancelled: 연구원님이 멈췄는지 (TUI의 멈춤 키). 줄 화면은 Ctrl+C."""
        task_id = self.task_log.new_task(request)
        self.hctx.task_id = task_id
        lang = reply_language(request, default=i18n.get_language())   # 연구원님이 쓴 언어 (답을 이 언어로 번역)
        ctx = PolicyContext(places=self.places, registered_tools=frozenset(self.tools.names()),
                            private_names=self.cfg.private_words(), workspace=self.workspace(),
                            remote_user=self.cfg.user_name,
                            remote_dirs={s.alias: s.workdir for s in self.cfg.servers})

        def emit(e: Event) -> None:
            self.task_log.event(task_id, e)
            self.render(e)

        self.hctx.emit = emit                            # 손이 알리는 이벤트 (외부 전송)

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
                system_prompt=self.system_prompt(),
                final_note=reply_note(lang),
                memory=self.memory,
                context_chars=self.context_chars,
                emit=emit,
                approve=self.approve,
                audit=audit,
                overflow_dir=self.cfg.home / "logs" / "outputs",
                task_id=task_id,
                evidence_check=needs_evidence,
                answer_guards=DEFAULT_GUARDS,
                translator=self.translator,
                reply_lang=lang,
                cancelled=cancelled,
            )
        except Cancelled:                                # 보통은 루프가 받아 멈춤으로 돌려준다
            self._abort(task_id, "user", t("stop.user", "en"), t("stop.user"))
            return None
        except KeyboardInterrupt:
            shell.kill_running(self.os)                  # GLaDOS가 돌리던 명령도 끝낸다
            self._abort(task_id, "user", t("task.interrupted", "en"), t("task.interrupted"))
            return None
        except LLMError as e:
            self._abort(task_id, "llm", t("task.llm_failed", "en", detail=str(e)),
                        t("task.llm_failed", detail=localized(e)))
            return None
        finally:
            self.hctx.emit = None
            self.apply_resume()                          # session.resume을 불렀으면 그 대화를 기억에 (이번 턴은 그 뒤에)
        self.task_log.finish(task_id, result.status)
        self.remember(self.memory.add(request, result))
        return result

    # ------------------------------------------------------------------ 대화 세션 (M6)
    def remember(self, turn: Turn) -> None:
        """끝난 턴을 지금 세션에 저장한다 (첫 턴에서 세션을 만든다). 저장이 안 돼도 대화는 계속한다."""
        try:
            if not self.hctx.session_id:
                self.hctx.session_id = self.sessions.new(turn.request)
            self.sessions.append(self.hctx.session_id, turn)
        except sqlite3.Error:
            pass

    def forget(self) -> None:
        """/new: 기억을 비우고 새 세션 (앞의 세션은 저장돼 있다)."""
        self.memory.clear()
        self.hctx.session_id = ""
        self.hctx.resume_to = ""

    def load_session(self, sid: str) -> SessionInfo | None:
        """지난 세션을 기억에 넣고 그 세션에 이어서 저장한다."""
        info = self.sessions.get(sid)
        if info is None:
            return None
        turns = self.sessions.turns(sid)
        self.memory.clear()
        self.memory.turns.extend(turns)
        self.hctx.session_id = sid
        return info

    def apply_resume(self) -> None:
        sid, self.hctx.resume_to = self.hctx.resume_to, ""
        if not sid or sid == self.hctx.session_id:
            return
        info = self.load_session(sid)
        if info is not None:
            self.screen.say(self.screen.dim(t("resume.auto", when=info.when(), title=info.title, count=info.count)))

    def resume_lines(self, args: list[str]) -> tuple[list[tuple[str, str]], bool]:
        """/resume: 목록, /resume N: 불러오기. ([(모양, 글)], 불러왔는지)."""
        recent = self.sessions.recent(RESUME_LIST, exclude=self.hctx.session_id)
        if not recent:
            return [("plain", t("resume.empty"))], False
        if not args:
            rows = [("plain", t("resume.title"))]
            rows += [("plain", t("resume.row", n=i, when=x.when(), title=x.title, count=x.count))
                     for i, x in enumerate(recent, 1)]
            return rows, False
        arg = args[0]
        if not arg.isdigit() or not 1 <= int(arg) <= len(recent):
            return [("plain", t("resume.bad", arg=arg, max=len(recent)))], False
        info = self.load_session(recent[int(arg) - 1].id)
        if info is None:
            return [("plain", t("resume.empty"))], False
        return self.resumed_rows(info), True

    def resumed_rows(self, info: SessionInfo) -> list[tuple[str, str]]:
        rows = [("fact", t("resume.done", when=info.when(), title=info.title, count=info.count))]
        if self.memory.turns:
            last = " ".join(self.memory.turns[-1].request.split())
            rows.append(("dim", t("resume.last", request=last if len(last) <= 100 else last[:99] + "…")))
        return rows

    def memory_lines(self, args: list[str]) -> list[tuple[str, str]]:
        """/memory: 기억한 것 (MEMORY.md), /memory del N: 지우기."""
        items = self.notes.items()
        path = str(self.notes.path)
        if args and args[0].lower() in ("del", "rm", "delete", "remove", "지워", "삭제"):
            arg = args[1] if len(args) > 1 else ""
            if not arg.isdigit() or not 1 <= int(arg) <= len(items):
                return [("plain", t("memory.bad", max=max(len(items), 1)))]
            removed = self.notes.remove(int(arg))
            return [("plain", t("memory.removed", item=removed or ""))]
        if not items:
            return [("plain", t("memory.empty", path=path))]
        rows = [("plain", t("memory.title", path=path))]
        rows += [("plain", t("memory.row_secret" if secret_in_note(item) else "memory.row", n=i, item=item))
                 for i, item in enumerate(items, 1)]
        rows.append(("dim", t("memory.hint")))
        return rows

    def resume_hint(self) -> SessionInfo | None:
        """처음 화면에서 권할 지난 대화 (가장 최근 것)."""
        last = self.sessions.recent(1, exclude=self.hctx.session_id)
        return last[0] if last else None

    def _abort(self, task_id: str, reason: str, logged: str, shown: str) -> None:
        """기록은 영어(엔진 안쪽), 화면은 지금 언어."""
        self.task_log.event(task_id, Event(ev.STOP, {"reason": reason, "message": logged}))
        self.task_log.finish(task_id, "stopped")
        self.screen.say(shown)

    # ------------------------------------------------------------------
    def render(self, e: Event) -> None:
        s = self.screen
        for style, text in event_lines(e):
            s.say(s.styled(style, text))

    def approve(self, call: ToolCall, d: Decision) -> str:
        s = self.screen
        s.say(s.cobalt(t("approve.title")) + " " + localized(d, fallback=d.reason))
        if call.card:                                     # 실행 카드 (remote.run: GPU·명령·커밋·규칙)
            for style, text in call.card:
                s.say("  " + s.styled(style, text))
        else:
            s.say(f"  {call.tool} {summarize_args(call.args, 120)}")
        if d.key == "policy.remote_run":
            s.say(t("approve.options_run"))
        elif d.key == "policy.memory_note":
            s.say(t("approve.options_note"))
        else:
            s.say(t("approve.options_task") if d.grantable else t("approve.options"))
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
            s.say(s.dim(t("choose.123") if d.grantable else t("choose.13")))

    # ------------------------------------------------------------------
    def command(self, text: str) -> bool:
        """/명령을 처리한다. 끝내야 하면 True."""
        s = self.screen
        words = text.split()
        cmd = words[0].lower()
        if cmd in ("/quit", "/exit", "/q"):
            return True
        if cmd == "/help":
            s.say(t("help"))
        elif cmd == "/new":
            self.forget()
            s.say(t("new.done"))
        elif cmd == "/resume":
            for style, line in self.resume_lines(words[1:])[0]:
                s.say(s.styled(style, line))
        elif cmd == "/memory":
            for style, line in self.memory_lines(words[1:]):
                s.say(s.styled(style, line))
        elif cmd in ("/context", "/ctx"):
            for line in self.context_lines():
                s.say(line)
        elif cmd == "/lang":
            self.set_language(words[1].lower() if len(words) > 1 else "")
        elif cmd == "/log":
            records = self.policy_log.recent(10)
            if not records:
                s.say(t("log.empty"))
            for r in records:
                when = datetime.fromtimestamp(r.get("ts", 0)).strftime("%m-%d %H:%M")
                rule = i18n.rule_text(str(r.get("rule", "")))
                who = i18n.who_text(str(r.get("decided_by", "")))
                s.say(f"{when}  {r.get('tool')}  {r.get('verdict')}  {rule}  ({who})")
        elif cmd == "/undo":
            self.undo()
        elif cmd == "/sent":
            for line in self.sent_lines():
                s.say(line)
        elif cmd == "/servers":
            for line in self.server_lines():
                s.say(line)
        elif cmd == "/runs":
            for line in self.runs_lines():
                s.say(line)
        elif cmd in LATER:
            s.say(t("command.later", cmd=cmd, stage=LATER[cmd]))
        else:
            s.say(t("command.unknown", cmd=cmd))
        return False

    def sent_lines(self) -> list[str]:
        """/sent: 이 세션에서 이 컴퓨터 밖으로 보낸 것 (웹 검색어, 읽은 주소)."""
        egress = self.hctx.egress
        items = egress.recent(30) if egress is not None else []
        if not items:
            return [t("sent.empty")]
        total, searches = egress.counts()
        lines = [t("sent.head", sent=total, searches=searches)]
        for item in items:
            when = datetime.fromtimestamp(item.ts).strftime("%H:%M:%S")
            kind = t("sent.search" if item.tool == "web.search" else "sent.fetch")
            lines.append(f"  {when}  {kind}  {item.host}  {item.what[:100]}")
        lines.append(t("sent.log", path=self.cfg.home / "logs" / "egress.jsonl"))
        return lines

    def server_lines(self) -> list[str]:
        """/servers: servers.toml의 서버, 연결 상태, 이 세션에서 마지막으로 본 GPU (네트워크는 쓰지 않음)."""
        servers = [srv for srv in self.cfg.servers if srv.alias]
        if not servers:
            return [t("servers.empty")]
        hub, watch = self.hctx.hub, self.hctx.gpu_watch
        lines = [t("servers.head", count=len(servers))]
        for srv in servers:
            on = hub is not None and hub.connected(srv.alias)
            lines.append(t("servers.line", alias=srv.alias, state=t("servers.connected" if on else "servers.idle"),
                           gpus=srv.gpus or "?", workdir=srv.workdir or "~"))
            seen = watch.checked.get(srv.alias) if watch is not None else None
            if seen:
                c = watch.summary(srv.alias)
                lines.append(t("servers.seen", time=datetime.fromtimestamp(seen).strftime("%H:%M"),
                               free=c.get("free", 0), checking=c.get("checking", 0), mine=c.get("mine", 0),
                               busy=c.get("other", 0) + c.get("busy", 0)))
        lines.append(t("servers.hint", alias=servers[0].alias))
        return lines

    def runs_lines(self) -> list[str]:
        """/runs: 서버 실행 목록 (이 컴퓨터의 기록, 접속하지 않음)."""
        if self.runner is None:
            return [t("servers.empty")]
        return self.runner.runs_lines()

    # ------------------------------------------------------------------ 배경 감시 (M5)
    def watch(self) -> None:
        """화면이 열리면 부른다: 알림을 화면으로 받고 배경 감시를 돌린다 (한 줄 실행에서는 부르지 않음)."""
        if self.runner is None:
            return
        self.runner.on_notice = self.notice
        self.runner.start_watch()

    def notice(self, n) -> None:
        """배경 감시의 알림 (감시 스레드에서 불린다). 줄 화면은 그대로 찍는다."""
        s = self.screen
        lines = n.lines()
        s.say()
        s.say(s.cobalt(t("tui.k.decide") if n.decision else t("tui.k.notice")) + "  " + lines[0])
        for line in lines[1:]:
            s.say(s.dim(line))
        self.os_notify(n)

    def os_notify(self, n) -> None:
        if self.cfg.notify:
            from glados.notify import os_notify

            lines = n.lines()
            os_notify(n.title(), " ".join(line.strip() for line in lines), self.os)

    def web_note(self) -> str:
        """웹 검색을 쓸 수 있는지 한 줄 (처음 화면)."""
        state, name, where = search_status(self.cfg.web, os.environ)
        if state == "on":
            return t("web.on", name=name, host=where)
        if state == "nokey":
            return t("web.nokey", name=name, var=where)
        return t("web.off")

    def set_language(self, lang: str) -> None:
        """/lang: 이번 세션의 화면 언어만 바꾼다 (설정 파일은 연구원님이 직접 고침)."""
        if lang in i18n.LANGS:
            i18n.set_language(lang)
            self.screen.say(t("lang.now", name=i18n.LANG_NAMES[lang]))
        else:
            self.screen.say(t("lang.usage", name=i18n.LANG_NAMES[i18n.get_language()]))

    def context_lines(self) -> list[str]:
        """/context: 다음 작업에 무엇을 어느 단계로 넣을지."""
        if not self.memory.turns:
            return [t("context.empty")]
        budget = self.history_budget()
        rows = self.memory.preview(budget)
        counts = Counter(level for _, level, _ in rows)
        summary = ", ".join(
            t("context.count", level=t(f"level.{lv}"), count=counts[lv]) for lv in LEVELS if counts[lv]
        )
        used = sum(n for _, level, n in rows if level != DROPPED)
        lines = [t("context.head", count=len(rows), summary=summary) + t("context.size", used=used, budget=budget)]
        for i, (turn, level, _) in enumerate(rows, 1):
            req = turn.request.replace("\n", " ")
            lines.append(f"  {i}. [{t(f'level.{level}')}] {req[:50] + '...' if len(req) > 50 else req}")
        return lines

    def last_checkpoint(self):
        """/undo로 되돌릴 마지막 파일 수정 (없으면 None)."""
        cps = self.hctx.checkpoints
        return cps.last() if cps else None

    def undo_text(self, cp) -> str:
        when = datetime.fromtimestamp(cp.ts).strftime("%m-%d %H:%M")
        return t("undo.confirm", path=cp.original, when=when)

    def restore(self, cp) -> Path:
        """연구원님이 고른 되돌리기. 기록을 남긴다."""
        path = self.hctx.checkpoints.restore(cp)  # type: ignore[union-attr]
        self.policy_log.write("undo", {"path": str(path)}, "allow", "명령:/undo", "연구원님", cp.task)
        return path

    def undo(self) -> None:
        s = self.screen
        cp = self.last_checkpoint()
        if cp is None:
            s.say(t("undo.none"))
            return
        s.say(s.cobalt(t("undo.title")) + " " + self.undo_text(cp))
        s.say(t("undo.options"))
        while True:
            try:
                choice = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                s.say()
                choice = "3"
            if choice == "1":
                s.say(t("undo.done", path=self.restore(cp)))
                return
            if choice == "3":
                s.say(t("undo.kept"))
                return
            s.say(s.dim(t("choose.13")))

    # ------------------------------------------------------------------
    def run_direct(self, command: str) -> None:
        """'!명령': 연구원님이 직접 실행. 내 터미널에서 치는 것과 같아서 승인 없이 돌린다.

        결과는 화면에 보여 주고 대화 기억에 남긴다 ("이 결과 설명해줘"가 되도록). 기록은 남긴다.
        """
        command = command.strip()
        if not command:
            self.screen.say(t("direct.empty"))
            return
        cd = CD.match(command)
        if cd:
            self.change_dir(cd.group(1))
            return
        self.show_direct(self.execute_direct(command))

    def execute_direct(self, command: str) -> "Direct":
        """!명령을 실행하고 기록·기억에 남긴다. 화면에는 쓰지 않는다 (줄 화면과 TUI가 따로 보여 줌)."""
        cwd = self.places.cwd or self.places.home
        task_id = self.task_log.new_task("!" + command)
        try:
            res = shell.run_command(command, cwd, DIRECT_TIMEOUT_S, self.os)
        except KeyboardInterrupt:
            self.task_log.finish(task_id, "stopped")
            return Direct(command, cwd, interrupted=True)
        except ToolError as e:
            self.task_log.finish(task_id, "stopped")
            return Direct(command, cwd, error=localized(e))
        self.policy_log.write("shell.run", {"command": command, "cwd": cwd}, "allow", "명령:!", "연구원님:직접", task_id)
        text = shell.format_result(command, cwd, res, shell.shell_label(self.os), DIRECT_TIMEOUT_S)
        observation = clip_output(text, self.cfg.home / "logs" / "outputs", task_id)
        self.task_log.event(task_id, Event(ev.OBSERVATION, {"tool": "shell.run", "text": observation[:500]}))
        self.task_log.finish(task_id, "done")
        self.remember(self.memory.add_direct(command, observation))
        return Direct(command, cwd, res)

    def show_direct(self, d: "Direct") -> None:
        s = self.screen
        if d.interrupted:
            s.say()
            s.say(t("direct.interrupted"))
            return
        if d.error:
            s.say(t("direct.failed", detail=d.error))
            return
        res = d.result
        assert res is not None
        if res.timed_out:
            s.say(s.dim(t("direct.timeout", command=d.command, seconds=DIRECT_TIMEOUT_S, cwd=d.cwd)))
        else:
            s.say(s.dim(t("direct.header", command=d.command, code=res.code, seconds=res.seconds, cwd=d.cwd)))
        out = res.out.rstrip().splitlines()
        for line in out[:DIRECT_LINES_SHOWN]:
            s.say(line)
        if len(out) > DIRECT_LINES_SHOWN:
            s.say(s.dim(t("direct.more", count=len(out) - DIRECT_LINES_SHOWN)))
        if res.err.strip():
            s.say(s.dim(t("direct.stderr")))
            for line in res.err.rstrip().splitlines()[:DIRECT_LINES_SHOWN]:
                s.say(line)
        if not res.out.strip() and not res.err.strip():
            s.say(s.dim(t("direct.no_output")))

    def change_dir(self, target: str | None) -> None:
        """'!cd 폴더': 이 세션의 작업 폴더를 바꾼다 (!명령과 GLaDOS의 명령, 상대 경로가 모두 따름)."""
        s = self.screen
        if target and target.strip():
            path = normalize(target.strip().strip('"').strip("'"), self.places)
            if not os.path.isdir(path):
                s.say(t("cd.missing", path=path))
                return
            self.set_cwd(path)
        s.say(t("cd.now", path=self.places.cwd))
        if self.cwd_is_safety():
            s.say(s.dim(t("cwd.safety_note")))

    def repl(self, first_run: bool = False) -> int:
        s = self.screen
        s.say(s.dim(self.header()))
        s.say(s.dim(self.web_note()))
        s.say(t("greet.first", home=self.cfg.home) if first_run else t("greet.again"))
        last = self.resume_hint()
        if last is not None:
            s.say(s.dim(t("resume.hint", when=last.when(), title=last.title, count=last.count)))
        if self.cwd_is_safety():
            s.say(s.dim(t("cwd.safety_note")))
        self.watch()
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
            if text.startswith("!"):
                self.run_direct(text[1:])
                continue
            self.ask_task(text)
        s.say(s.dim(t("bye")))
        return 0

    def close(self) -> None:
        self.task_log.close()
        self.sessions.close()
        if self.runner is not None:
            self.runner.stop_watch()                      # 서버의 실행은 그대로 돈다 (tmux)
            self.runner.on_notice = None
            self.runner.store.close()
        if self.hctx.hub is not None:
            self.hctx.hub.close()                         # 서버 연결을 닫는다


def wants_tui(choice: str, cfg_screen: str) -> bool:
    """TUI로 열지. --plain/--tui가 설정보다 먼저. 터미널이 아니면(파이프, 시험) 줄 화면."""
    kind = choice or cfg_screen
    if kind != "tui":
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    home = glados_home()
    i18n.set_language(peek_language(home))     # 설정을 다 읽기 전 안내도 그 언어로

    if args[:1] in (["-h"], ["--help"]):
        print(t("usage"))
        return 0
    if args[:1] == ["--version"]:
        print(f"GLaDOS {__version__}")
        return 0
    choice = ""
    if args[:1] in (["--plain"], ["--tui"]):
        choice = args.pop(0)[2:]

    screen = Screen(osinfo.enable_ansi())
    if osinfo.is_admin():
        screen.say(t("admin.refused"))
        return 2

    try:
        created = ensure_home(home)
        cfg = load_config(home)
    except (ConfigError, OSError) as e:
        screen.say(t("config.error", detail=localized(e)))
        return 1

    notes = [] if osinfo.utf8_mode_on() else [t("utf8.hint")]
    if not args and wants_tui(choice, cfg.screen):
        from glados.tui import run_tui           # textual은 TUI를 열 때만 불러온다

        return run_tui(cfg, first_run=bool(created), notes=notes)

    for note in notes:
        screen.say(screen.dim(note))
    app = App(cfg, screen)
    moved = app.leave_safety_cwd()
    if moved:
        screen.say(screen.dim(t("cwd.moved", path=moved)))
    try:
        if not app.check_model():
            return 1
        if args:
            result = app.ask_task(" ".join(args))
            return 0 if result is not None and result.status == "done" else 1
        return app.repl(first_run=bool(created))
    finally:
        app.close()

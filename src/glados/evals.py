"""평가 세트 (M9.1): 실사용에서 실패한 요청을 실제 모델로 다시 돌려 재는 것.

연구원님 "땜질 말고 근본적인 해결책" -> 다른 하네스 조사 (Anthropic "Building effective agents": 가장 단순한 것부터,
결과가 실제로 나아질 때만 복잡하게, 예시 입력을 많이 돌려 보며 고친다). 구조를 바꿀 때마다 이것으로 잰다.

- `glados --eval [사례 id ...] [--allow-run]`
- 사례: 패키지의 defaults/evals.toml + GLADOS_HOME/evals.toml (연구원님이 더한 것, 같은 id면 이것이 이긴다)
- 사례마다 새 대화로 돈다 (기억 없음). turns의 앞 말들은 준비, 마지막 말의 결과를 점검한다
- 승인은 기본 거절 (평가가 연구원님 컴퓨터를 바꾸지 않게). --allow-run이면 shell.run만 "이번만" 허용
  (모델이 쓴 코드가 실제로 돈다: 연구원님이 고른 경우에만)
- 대화 세션(sessions.db)에는 남기지 않는다. 작업 기록(logs/tasks.jsonl)에는 남는다
- 보고서: GLADOS_HOME/evals/<시각>.md와 .json. 지난 결과와 비교해 좋아진 것·나빠진 것을 적는다

점검(모두 사례 파일에 적는 자료. 엔진의 규칙이 아니다)
  expect_ask        되묻고 끝났나 (reason "ask"이거나 답이 물음으로 끝남)
  max_tools         부른 도구 수 상한 (0이면 도구 없이)
  tools_any         이 중 하나는 불러야 함 (이름 또는 "web." 같은 앞부분)
  tools_none        이 중 아무것도 부르면 안 됨
  args_lacks        {도구 = [인자 이름]}: 그 도구를 부를 때 이 인자를 쓰면 안 됨 (말하지 않은 폴더로 좁히기 등)
  args_has          {도구 = [인자 이름]}: 그 도구를 부를 때 이 인자를 써야 함
  answer_has        답(화면 글이나 모델이 쓴 글)에 들어 있어야 할 글 (하나라도)
  answer_lacks      답에 있으면 안 될 글
  max_seconds       걸린 시간 상한
  no_sendback       안전망·검사기가 되돌리지 않아야 함
  role              대화 차례가 고른 역할 (M9.2)
  needs_run         --allow-run일 때만 돈다
"""

from __future__ import annotations

import io
import json
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from glados.core import events as ev

CHECKS = ("expect_ask", "max_tools", "tools_any", "tools_none", "args_lacks", "args_has", "answer_has",
          "answer_lacks", "max_seconds", "no_sendback", "role")


@dataclass
class Case:
    id: str
    turns: list[str]
    note: str = ""
    checks: dict[str, Any] = field(default_factory=dict)
    needs_run: bool = False


@dataclass
class Outcome:
    case: Case
    passed: bool
    problems: list[str]
    seconds: float
    tools: list[str]
    calls: list[dict[str, Any]]
    answer: str
    internal: str
    reason: str
    role: str = ""
    sendbacks: int = 0
    error: str = ""


def load_cases(home: Path) -> list[Case]:
    """패키지 사례 + 연구원님 사례 (같은 id면 연구원님 것)."""
    texts = [(resources.files("glados") / "defaults" / "evals.toml").read_text(encoding="utf-8")]
    mine = home / "evals.toml"
    if mine.exists():
        texts.append(mine.read_text(encoding="utf-8"))
    cases: dict[str, Case] = {}
    for text in texts:
        for raw in tomllib.loads(text).get("case", []):
            turns = raw.get("turns") or []
            if not raw.get("id") or not turns:
                continue
            checks = {k: raw[k] for k in CHECKS if k in raw}
            cases[raw["id"]] = Case(raw["id"], [str(x) for x in turns], str(raw.get("note", "")), checks,
                                    bool(raw.get("needs_run", False)))
    return list(cases.values())


def _matches(tool: str, names: list[str]) -> bool:
    return any(tool == n or (n.endswith(".") and tool.startswith(n)) for n in names)


def _asked(text: str) -> bool:
    return text.strip().rstrip("\"'”’)]").endswith(("?", "？"))


def judge(case: Case, out: Outcome) -> list[str]:
    """점검에 어긋난 것들 (비었으면 통과)."""
    c, bad = case.checks, []
    said = f"{out.answer}\n{out.internal}"
    if c.get("expect_ask") and not (out.reason == "ask" or _asked(out.answer) or _asked(out.internal)):
        bad.append("expected a question back")
    if "max_tools" in c and len(out.tools) > int(c["max_tools"]):
        bad.append(f"used {len(out.tools)} tools (max {c['max_tools']}): {', '.join(out.tools)}")
    if c.get("tools_any") and not any(_matches(t, list(c["tools_any"])) for t in out.tools):
        bad.append(f"did not use any of {c['tools_any']}")
    for tool in out.tools:
        if c.get("tools_none") and _matches(tool, list(c["tools_none"])):
            bad.append(f"used {tool}")
    for call in out.calls:
        for key in (c.get("args_lacks") or {}).get(call["tool"], []):
            if key in call["args"]:
                bad.append(f"{call['tool']} was given {key}={call['args'][key]!r}")
        for key in (c.get("args_has") or {}).get(call["tool"], []):
            if key not in call["args"]:
                bad.append(f"{call['tool']} was not given {key}")
    if c.get("answer_has") and not any(s.lower() in said.lower() for s in c["answer_has"]):
        bad.append(f"answer has none of {c['answer_has']}")
    for s in c.get("answer_lacks") or []:
        if s.lower() in said.lower():
            bad.append(f"answer contains {s!r}")
    if "max_seconds" in c and out.seconds > float(c["max_seconds"]):
        bad.append(f"took {out.seconds:.0f}s (max {c['max_seconds']})")
    if c.get("no_sendback") and out.sendbacks:
        bad.append(f"sent back {out.sendbacks} time(s)")
    if c.get("role") and out.role != c["role"]:
        bad.append(f"role {out.role or '-'} (expected {c['role']})")
    if out.error:
        bad.append(out.error)
    return bad


def run_case(cfg: Any, case: Case, allow_run: bool, make_app: Any = None, llm: Any = None) -> Outcome:
    """새 대화에서 한 사례. make_app: 시험에서 App 대신 (기본은 cli.App)."""
    from glados.cli import App, Screen

    app = (make_app or App)(cfg, Screen(ansi=False, out=io.StringIO()), **({"llm": llm} if llm is not None else {}))
    seen: list[Any] = []
    app.render = seen.append                                     # 화면 대신 이벤트를 모은다
    app.remember = lambda turn: None                             # 대화 세션에 남기지 않는다

    def approve(call: Any, decision: Any) -> str:
        return "once" if allow_run and call.tool == "shell.run" else "deny"

    app.approve = approve
    result, started, error = None, time.monotonic(), ""
    try:
        for turn in case.turns[:-1]:
            app.ask_task(turn)
        seen.clear()
        started = time.monotonic()
        result = app.ask_task(case.turns[-1])
    except Exception as e:                                       # 사례 하나가 깨져도 나머지는 돈다
        error = f"crashed: {type(e).__name__}: {e}"
    finally:
        seconds = time.monotonic() - started
        app.close()
    calls = [{"tool": e.data["tool"], "args": dict(e.data.get("args") or {})} for e in seen if e.kind == ev.PROPOSAL]
    roles = [str(e.data.get("role") or "") for e in seen if e.kind == ev.PLAN and e.data.get("step") == 0]
    answer = internal = reason = ""
    if result is not None:
        reason = result.reason
        if result.answer is not None:
            answer = result.answer.fact
        if result.internal is not None:
            internal = result.internal.fact
    elif not error:
        error = "no result"
    out = Outcome(case, False, [], seconds, [c["tool"] for c in calls], calls, answer, internal, reason,
                  role=(roles[-1] if roles else ""), sendbacks=sum(e.kind == ev.GUARD for e in seen), error=error)
    out.problems = judge(case, out)
    out.passed = not out.problems
    return out


def compare(now: list[Outcome], before: dict[str, bool]) -> dict[str, str]:
    """지난 결과와 비교: id -> "better" | "worse" | ""."""
    out = {}
    for o in now:
        if o.case.id in before and before[o.case.id] != o.passed:
            out[o.case.id] = "better" if o.passed else "worse"
    return out


def write_report(home: Path, outcomes: list[Outcome], model: str) -> tuple[Path, dict[str, str]]:
    folder = home / "evals"
    folder.mkdir(parents=True, exist_ok=True)
    previous = sorted(folder.glob("*.json"))
    before: dict[str, bool] = {}
    if previous:
        try:
            before = {r["id"]: r["passed"] for r in json.loads(previous[-1].read_text(encoding="utf-8"))["results"]}
        except (OSError, ValueError, KeyError, TypeError):
            before = {}
    delta = compare(outcomes, before)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    data = {"model": model, "when": stamp, "results": [{
        "id": o.case.id, "passed": o.passed, "problems": o.problems, "seconds": round(o.seconds, 1), "tools": o.tools,
        "role": o.role, "answer": o.answer, "internal": o.internal, "reason": o.reason} for o in outcomes]}
    (folder / f"{stamp}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    passed = sum(o.passed for o in outcomes)
    lines = [f"# GLaDOS eval {stamp}", "", f"Model: {model} · passed {passed}/{len(outcomes)}", "",
             "| case | result | change | seconds | tools | problems |", "|---|---|---|---|---|---|"]
    for o in outcomes:
        change = {"better": "better", "worse": "WORSE"}.get(delta.get(o.case.id, ""), "")
        lines.append(f"| {o.case.id} | {'pass' if o.passed else 'FAIL'} | {change} | {o.seconds:.0f} | "
                     f"{', '.join(o.tools) or '-'} | {'; '.join(o.problems) or ''} |")
    lines.append("")
    for o in outcomes:
        lines += [f"## {o.case.id}", "", f"- note: {o.case.note}" if o.case.note else "",
                  f"- turns: {' / '.join(o.case.turns)}", f"- role: {o.role or '-'} · reason: {o.reason or '-'}",
                  f"- answer: {o.answer}", f"- model wrote: {o.internal}" if o.internal != o.answer else "", ""]
    path = folder / f"{stamp}.md"
    path.write_text("\n".join(line for line in lines if line is not None), encoding="utf-8")
    return path, delta


def main(cfg: Any, screen: Any, ids: list[str], allow_run: bool) -> int:
    cases = [c for c in load_cases(cfg.home) if not ids or c.id in ids]
    skipped = [c.id for c in cases if c.needs_run and not allow_run]
    cases = [c for c in cases if c.id not in skipped]
    if not cases:
        screen.say("No eval cases to run.")
        return 1
    outcomes = []
    for i, case in enumerate(cases, 1):
        screen.say(f"[{i}/{len(cases)}] {case.id} ...")
        o = run_case(cfg, case, allow_run)
        outcomes.append(o)
        screen.say(f"  {'pass' if o.passed else 'FAIL'} · {o.seconds:.0f}s" +
                   (f" · {'; '.join(o.problems)}" if o.problems else ""))
    path, delta = write_report(cfg.home, outcomes, cfg.model.name)
    passed = sum(o.passed for o in outcomes)
    screen.say(f"passed {passed}/{len(outcomes)} · report: {path}")
    if skipped:
        screen.say(f"skipped (need --allow-run): {', '.join(skipped)}")
    worse = [k for k, v in delta.items() if v == "worse"]
    better = [k for k, v in delta.items() if v == "better"]
    if better:
        screen.say(f"better than last run: {', '.join(better)}")
    if worse:
        screen.say(f"WORSE than last run: {', '.join(worse)}")
    return 0 if passed == len(outcomes) else 1

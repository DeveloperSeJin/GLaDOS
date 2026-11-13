"""역할 (M9.2): 일의 종류마다 짧은 지시와 그 일에 쓰는 도구만.

연구원님 "땜질 말고 근본적인 해결책" -> 다른 하네스 조사. 원래 설계(라우터 + agents/<역할>.md + "지금 필요한 도구만")를
공식 소스를 보고 만든다:
- Qwen-Agent Router (QwenLM/Qwen-Agent qwen_agent/agents/router.py): 라우터 프롬프트에 "이름: 설명"으로 도우미를 늘어놓고
  모델이 하나를 고른다. 없는 이름이면 기본 것으로. 고른 도우미는 자기 지시·도구로 같은 대화를 이어 받는다
- Roo Code 모드 (RooCodeInc/Roo-Code packages/types/src/mode.ts, src/shared/tools.ts): 모드마다 slug·roleDefinition·
  whenToUse·groups(도구 묶음). 어느 모드에서나 쓰는 도구(ALWAYS_AVAILABLE_TOOLS)가 따로 있고, switch_mode로 바꾼다
- OpenAI Agents SDK handoffs (openai/openai-agents-python src/agents/handoffs): 넘기기는 도구 하나
  ("transfer_to_<이름>", 설명 = "Handoff to the <이름> agent to handle the request. " + 넘길 때 설명)
- Manus "mask, don't remove": 고를 수 있는 도구는 디코딩에서 가린다. 여기서는 Ollama format의 도구 이름 enum이 그 자리
- Claude Code 하위 에이전트: 마크다운 파일 = 앞머리(이름·설명·도구) + 본문(그 역할의 지시). 연구원님 파일이 이긴다

역할 파일: 패키지 agents/*.md, 그리고 GLADOS_HOME/agents/*.md (같은 이름이면 연구원님 것).
앞머리: name, description(언제 쓰는지: 대화 차례가 보고 고른다), tools(이름 또는 "fs." 같은 앞부분 목록),
stages(선택, M9.5): 단계마다 name, goal(할 일), gate(끝났다고 볼 조건), tools(그 단계에서 쓰는 도구), approve(연구원님 동의).
본문: 그 역할로 일할 때 시스템 메시지에 붙는 지시. {experiments}는 실험 폴더로 바뀐다.

단계 (M9.5, 설계서 3장 "실험 7단계" + Anthropic "prompt chaining with gates"): 한 번에 한 단계만 하고, 모델이 단계를
끝냈다고 하면 엔진이 그 단계의 조건(gate)을 같은 대화로 확인한 뒤에 넘어간다. 단계마다 쓸 수 있는 도구가 다르다
(Manus "mask, don't remove": 가설·설계 단계에서는 읽기만). 지금 단계는 대화에 "[stage]" 줄로 남아 다음 말에서 이어진다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from glados.hands import Tool, ToolRegistry

# 어느 역할에서나 쓰는 도구 (Roo Code ALWAYS_AVAILABLE_TOOLS처럼): 지난 대화 불러오기, 기억해 달라는 말 적기
ALWAYS = ("session.resume", "memory.note")
HANDOFF = "handoff"
HANDOFF_SUMMARY = ("hand this task to another role when the next step needs tools this role does not have "
                   "(role: one of {roles}; reason: what the next step needs)")


def _match(tool: str, names: list[str]) -> bool:
    return any(tool == p or (p.endswith(".") and tool.startswith(p)) for p in names)


@dataclass
class Stage:
    name: str
    goal: str
    gate: str
    tools: list[str] = field(default_factory=list)       # 비면 역할의 도구 모두
    approve: bool = False                                # 넘어가기 전에 연구원님이 동의했어야 함 (설계서: 01 가설)


@dataclass
class Role:
    name: str
    description: str
    tools: list[str] = field(default_factory=list)
    instructions: str = ""
    stages: list[Stage] = field(default_factory=list)

    def allows(self, tool: str, stage: Stage | None = None) -> bool:
        if tool in ALWAYS:
            return True
        return _match(tool, self.tools) and (stage is None or not stage.tools or _match(tool, stage.tools))

    def stage_index(self, name: str) -> int:
        return next((i for i, s in enumerate(self.stages) if s.name == name), -1)


def _parse(text: str) -> Role | None:
    if not text.startswith("---"):
        return None
    try:
        _, head, body = text.split("---", 2)
        meta = yaml.safe_load(head) or {}
    except (ValueError, yaml.YAMLError):
        return None
    name = str(meta.get("name") or "").strip()
    if not name or not meta.get("description"):
        return None
    tools = [str(t) for t in (meta.get("tools") or [])]
    stages = []
    for raw in meta.get("stages") or []:
        if isinstance(raw, dict) and raw.get("name") and raw.get("goal") and raw.get("gate"):
            stages.append(Stage(str(raw["name"]), " ".join(str(raw["goal"]).split()), " ".join(str(raw["gate"]).split()),
                                [str(t) for t in (raw.get("tools") or [])], bool(raw.get("approve", False))))
    return Role(name, " ".join(str(meta["description"]).split()), tools, body.strip(), stages)


def load_roles(home: Path | None = None) -> list[Role]:
    """패키지 역할 + 연구원님 역할 (같은 이름이면 연구원님 것). 이름 순서는 패키지 파일 이름 순."""
    found: dict[str, Role] = {}
    pkg = resources.files("glados") / "agents"
    for item in sorted(pkg.iterdir(), key=lambda p: p.name):
        if item.name.endswith(".md"):
            role = _parse(item.read_text(encoding="utf-8"))
            if role is not None:
                found[role.name] = role
    if home is not None and (home / "agents").is_dir():
        for path in sorted((home / "agents").glob("*.md")):
            try:
                role = _parse(path.read_text(encoding="utf-8"))
            except OSError:
                role = None
            if role is not None:
                found[role.name] = role
    return list(found.values())


def fill(role: Role, values: dict[str, str]) -> Role:
    """{experiments} 같은 자리를 이 컴퓨터의 값으로 (지시와 단계 모두)."""
    def put(text: str) -> str:
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        return text
    stages = [replace(s, goal=put(s.goal), gate=put(s.gate)) for s in role.stages]
    return replace(role, instructions=put(role.instructions), stages=stages)


def usable(roles: list[Role], registry: ToolRegistry) -> list[Role]:
    """이 컴퓨터에 그 도구가 하나라도 있는 역할만 (서버가 없으면 server 역할은 없다)."""
    names = registry.names()
    return [r for r in roles if any(r.allows(n) and n not in ALWAYS for n in names)]


def subset(role: Role, registry: ToolRegistry, roles: list[Role], stage: Stage | None = None) -> ToolRegistry:
    """그 역할이 쓰는 도구(단계가 있으면 그 단계의 것) + 다른 역할로 넘기는 도구 (다른 역할이 있을 때)."""
    tools: list[Tool] = [t for n in registry.names() if role.allows(n, stage) and (t := registry.get(n)) is not None]
    others = [r.name for r in roles if r.name != role.name]
    if others:
        params: dict[str, Any] = {
            "type": "object",
            "properties": {"role": {"type": "string", "enum": others}, "reason": {"type": "string"}},
            "required": ["role", "reason"],
        }
        tools.append(Tool(HANDOFF, HANDOFF_SUMMARY.format(roles=", ".join(others)), params))
    return ToolRegistry(tools)


def menu(roles: list[Role], registry: ToolRegistry) -> str:
    """대화 차례가 볼 역할 목록 (Qwen-Agent Router처럼 이름: 설명, 그리고 그 역할의 도구 이름)."""
    lines = ["Roles (pick one in role):"]
    for r in roles:
        names = [n for n in registry.names() if r.allows(n) and n not in ALWAYS]
        stages = f" Stages: {', '.join(s.name for s in r.stages)}." if r.stages else ""
        lines.append(f"- {r.name}: {r.description} Tools: {', '.join(names)}.{stages}")
    return "\n".join(lines)


def find(roles: list[Role], name: str) -> Role | None:
    return next((r for r in roles if r.name == name), None)


STAGE_MARK = "[stage]"


def stage_text(role: Role, at: int) -> str:
    """시스템 메시지에 붙는 단계 목록과 지금 단계 (할 일을 늘 눈앞에: Manus의 todo 되뇌기)."""
    n = len(role.stages)
    lines = [f"Stages of this {role.name} (one at a time):"]
    lines += [f"{i + 1}. {s.name}{' (done)' if i < at else ''}" for i, s in enumerate(role.stages)]
    now = role.stages[at]
    lines += [f"Now: stage {at + 1}/{n} {now.name}. {now.goal}", f"Done when: {now.gate}",
              "When this stage is done, set stage_done.answer to true and put what it produced in stage_done.reason "
              "(the hypothesis, the design, the script path, the key numbers). Work only on this stage."]
    return "\n".join(lines)


def marker(role: Role, at: int, status: str, text: str = "") -> str:
    """대화에 남는 단계 표시 (다음 말에서 이어 가려고). status: now | waiting | passed | complete."""
    s = role.stages[at]
    head = f"{STAGE_MARK} {role.name} {at + 1}/{len(role.stages)} {s.name}:"
    body = {"now": f"now. {s.goal}", "waiting": "done; waiting for the Researcher to agree.",
            "passed": "passed.", "complete": "passed. All stages are done."}[status]
    return f"{head} {body}" + (f" {text.strip()}" if text.strip() else "")


def resume_at(role: Role, messages: list[dict[str, str]]) -> int:
    """대화의 마지막 단계 표시로 이어 갈 단계 (없거나 다 끝났으면 0). 엔진이 남긴 표시를 읽는 것이라 판단이 아니다."""
    head = f"{STAGE_MARK} {role.name} "
    for m in reversed(messages):
        content = str(m.get("content", ""))
        if not content.startswith(head):
            continue
        try:
            k = int(content[len(head):].split("/", 1)[0])
        except ValueError:
            return 0
        rest = content.split(":", 1)[1] if ":" in content else ""
        if "All stages are done." in rest:
            return 0
        if rest.strip().startswith("passed."):
            return min(k, len(role.stages) - 1)
        return max(0, min(k - 1, len(role.stages) - 1))
    return 0

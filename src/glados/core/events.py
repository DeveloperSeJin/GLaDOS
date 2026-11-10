"""엔진이 내보내는 이벤트. 화면(cli, 나중의 glados serve)은 이것만 보고 그린다."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 이벤트 종류
START = "start"            # 작업 시작 {request}
THOUGHT = "thought"        # 모델의 판단 근거 {step, thought}
PROPOSAL = "proposal"      # 도구 호출 제안 {step, tool, args}
DECISION = "decision"      # 정책 판정 {tool, verdict, rule, reason, decided_by}
OBSERVATION = "observation"  # 도구 결과 요약 {tool, text}
ANSWER = "answer"          # 최종 답 {fact, wit, sources}
STOP = "stop"              # 끝나지 못하고 멈춤 {reason, detail}


@dataclass
class Event:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

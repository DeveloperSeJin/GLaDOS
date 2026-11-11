"""공유 서버 규칙 (팀 규칙이 없어서 GLaDOS 기본값, 설계서 6장). 순수 코드라 모델 없이 시험한다.

| 빈 GPU 판정 | 다른 사람 프로세스 없음 + 메모리 1GB 미만 + 사용률 5% 미만이 5분 이상 | 대기 |
| 동시 사용 GPU | 평일 09~19시 1장, 그 외 2장 | 승인 |

M4(서버 읽기)는 판정을 보고만 한다. M5(서버 실행)에서 remote.run이 이 판정으로 대기·승인을 정한다.
다른 사람 프로세스는 몇 개인지만 센다 (누구인지, 무엇인지는 남기지 않음).
5분은 GLaDOS가 직접 본 시간이다: 처음 보면 "지금은 비었지만 아직 확인 중"이고, 5분 넘게 이어서 비어 있어야 "빔".
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime

FREE_MEM_MB = 1024          # 이보다 적게 써야 빔
FREE_UTIL = 5               # 사용률(%)이 이보다 낮아야 빔
FREE_FOR_S = 300            # 이만큼 이어서 비어 있어야 "빔"
DAY_HOURS = (9, 19)         # 평일 낮
DAY_LIMIT, OFF_LIMIT = 1, 2  # 동시에 쓸 GPU 수
KEEP_S = 3600               # 기억할 관찰 (한 시간)


@dataclass(frozen=True)
class GpuSample:
    ts: float
    mem_mb: float
    util: float
    others: int             # 다른 사람 프로세스 수 (누구인지는 남기지 않음)
    mine: int               # 내 프로세스 수


def idle(s: GpuSample) -> bool:
    return s.others == 0 and s.mine == 0 and s.mem_mb < FREE_MEM_MB and s.util < FREE_UTIL


def free_for(samples: list[GpuSample], now: float) -> float:
    """지금까지 이어서 비어 있었던 시간(초): 마지막 관찰이 비어 있을 때, 거꾸로 이어진 빈 관찰의 첫 시각부터.
    마지막 관찰이 비어 있지 않으면 0."""
    if not samples or not idle(samples[-1]):
        return 0.0
    start = samples[-1].ts
    for s in reversed(samples):
        if not idle(s):
            break
        start = s.ts
    return max(0.0, now - start)


def state(samples: list[GpuSample], now: float) -> str:
    """mine(내가 씀) | other(다른 사람이 씀) | busy(프로세스는 안 보이는데 메모리·사용률이 높음)
    | free(5분 넘게 빔) | checking(지금은 비었지만 5분이 안 됨)."""
    last = samples[-1]
    if last.mine:
        return "mine"
    if last.others:
        return "other"
    if not idle(last):
        return "busy"
    return "free" if free_for(samples, now) >= FREE_FOR_S else "checking"


def gpu_limit(when: datetime) -> tuple[int, bool]:
    """(지금 동시에 쓸 수 있는 GPU 수, 평일 낮인지). 공휴일은 아직 모른다."""
    day = when.weekday() < 5 and DAY_HOURS[0] <= when.hour < DAY_HOURS[1]
    return (DAY_LIMIT if day else OFF_LIMIT), day


class GpuWatch:
    """서버·GPU마다 GLaDOS가 본 것 (이 세션, 한 시간). 여러 스레드에서 쓴다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: dict[tuple[str, int], list[GpuSample]] = {}
        self.checked: dict[str, float] = {}         # 서버마다 마지막으로 본 시각

    def add(self, server: str, gpu: int, sample: GpuSample) -> list[GpuSample]:
        with self._lock:
            items = self._samples.setdefault((server, gpu), [])
            items.append(sample)
            cutoff = sample.ts - KEEP_S
            while items and items[0].ts < cutoff:
                items.pop(0)
            self.checked[server] = max(self.checked.get(server, 0.0), sample.ts)
            return list(items)

    def summary(self, server: str, now: float | None = None) -> dict[str, int]:
        """마지막으로 본 상태별 GPU 수 (/servers 화면)."""
        now = time.time() if now is None else now
        out: dict[str, int] = {}
        with self._lock:
            for (srv, _), items in self._samples.items():
                if srv == server and items:
                    key = state(items, now)
                    out[key] = out.get(key, 0) + 1
        return out

"""공유 서버 규칙 (팀 규칙이 없어서 GLaDOS 기본값, 설계서 6장). 순수 코드라 모델 없이 시험한다.

| 빈 GPU 판정 | 다른 사람 프로세스 없음 + 메모리 1GB 미만 + 사용률 5% 미만이 5분 이상 | 대기 |
| 실행이 들어갈 GPU (M5.1) | 남은 메모리(전체 − 사용 중)가 필요량 + 여유(10%, 최소 2GB)를 5분 내내 넘음 | 대기 |
| 동시 사용 GPU | 평일 09~19시 1장, 그 외 2장 | 승인 |
| 실험 1개 최대 실행 시간 | 낮 4시간, 밤 12시간 | 알림 (결정 요청) |
| 하루 GPU 시간 예산 | 서버당 12 GPU시간 | 승인 카드에 경고, 넘으면 알림 |
| CPU 작업 | nice 10 | 실행 스크립트가 붙임 |

실행에 필요한 메모리를 알면 (연구원님이 말함, 코드로 추정, 지난 실행에서 잼) 빈 GPU 판정 대신 "들어갈 GPU" 판정을 쓴다:
다른 사람이 쓰는 GPU에도 같이 올라갈 수 있다 (연구원님 결정, 2026-11-11). 판단은 5분 동안 가장 적었던 남은 메모리로.
필요 메모리를 모르면 빈 GPU 판정 그대로.
M4(서버 읽기)는 판정을 보고만 한다. M5(서버 실행)의 remote.run은 이 판정으로 대기(빈 GPU가 확인될 때까지 줄)하고,
한도를 넘는 실행은 승인 카드에 경고를 띄운다 (실행은 어차피 매번 승인).
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
DAY_MAX_H, NIGHT_MAX_H = 4, 12   # 실험 1개 최대 실행 시간 (시작한 때가 평일 낮이면 4)
DAY_BUDGET_H = 12.0         # 서버당 하루 GPU 시간
NICE = 10                   # 실행 스크립트의 CPU 양보
MEM_MARGIN = 0.10           # 필요 메모리에 더하는 여유 (비율)
MEM_MARGIN_MIN_MB = 2048    # 여유의 최소값
BUSY_UTIL = 80              # 같이 쓸 GPU의 사용률이 이보다 높으면 승인 카드에 경고


@dataclass(frozen=True)
class GpuSample:
    ts: float
    mem_mb: float
    util: float
    others: int             # 다른 사람 프로세스 수 (누구인지는 남기지 않음)
    mine: int               # 내 프로세스 수
    total_mb: float = 0.0   # GPU 전체 메모리 (남은 메모리 = 전체 - 사용 중)

    @property
    def free_mb(self) -> float:
        return max(0.0, self.total_mb - self.mem_mb) if self.total_mb > 0 else 0.0


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


def need_with_margin(need_mb: float) -> float:
    """필요 메모리 + 여유 (10%, 최소 2GB)."""
    return need_mb + max(need_mb * MEM_MARGIN, MEM_MARGIN_MIN_MB)


def fits_for(samples: list[GpuSample], now: float, need_mb: float) -> tuple[float, float]:
    """(필요량 + 여유가 이어서 들어갈 수 있었던 시간(초), 그동안 가장 적었던 남은 메모리 MB).
    마지막으로 본 것이 들어가지 않으면 (0, 지금 남은 메모리)."""
    if not samples:
        return 0.0, 0.0
    want = need_with_margin(need_mb)
    last = samples[-1]
    if last.total_mb <= 0 or last.free_mb < want:
        return 0.0, last.free_mb
    start, low = last.ts, last.free_mb
    for s in reversed(samples):
        if s.total_mb <= 0 or s.free_mb < want:
            break
        start, low = s.ts, min(low, s.free_mb)
    return max(0.0, now - start), low


def max_hours(start: datetime) -> int:
    """이 시각에 시작한 실험의 최대 실행 시간 (넘으면 알림)."""
    return DAY_MAX_H if gpu_limit(start)[1] else NIGHT_MAX_H


def gpu_hours(spans: list[tuple[float, float, int]], since: float, now: float) -> float:
    """since 이후에 쓴 GPU 시간. spans: (시작, 끝 또는 0(아직 도는 중), GPU 수)."""
    total = 0.0
    for start, end, count in spans:
        if not start:
            continue
        a, b = max(start, since), min(end or now, now)
        if b > a:
            total += (b - a) / 3600 * max(1, count)
    return total


def day_start(now: float) -> float:
    """오늘 0시 (이 컴퓨터 시각)."""
    d = datetime.fromtimestamp(now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


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

    def history(self, server: str, gpu: int) -> list[GpuSample]:
        with self._lock:
            return list(self._samples.get((server, gpu), []))

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

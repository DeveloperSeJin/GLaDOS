"""GLaDOS 로고 (brand-identity: 06 LISSAJOUS) 를 터미널 글자로.

- 심볼: 오실로스코프 리사주 곡선(흰색) + 코발트 점 하나. 점자(Braille) 글자 한 칸이 점 2×4개라
  곡선을 점으로 그린다. 기본형 3:2, 위상 π/2, 점은 t=0.6
- 상태 (앱 안의 존재감): 대기 1:2 흐리게·점 멈춤 / 작업 중 3:2·점이 곡선을 따라 이동(꼬리 둘) /
  승인 대기 3:2 흐리게·점만 숨쉼 / 끝남·멈춤 3:2·점 멈춤
- 워드마크: Michroma(OFL)로 쓴 "GLaDOS"를 점자 글자로 옮겨 둔 것 (만들 때 한 번, 아래 상수)
- 화면 글자 폭: 점자는 한 칸짜리 글자다 (한글처럼 두 칸이 아님)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rich.text import Text

VOID, LINE, FAINT, MUTED, WHITE, COBALT = "#030303", "#262626", "#8E9196", "#A9ADB3", "#F2F4F7", "#4D7CFF"
CURVE_DIM = "#5A5D62"       # 흐린 곡선 (대기·승인 대기)
TRAIL = ("#2F4C9A", "#1D2D5B")   # 꼬리: 코발트 60%, 35%

_BIT = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (1, 0): 0x08,
        (1, 1): 0x10, (1, 2): 0x20, (0, 3): 0x40, (1, 3): 0x80}

# 곡선 모양: (a, b, 위상)
SHAPES = {"idle": (1, 2, math.pi / 4), "work": (3, 2, math.pi / 2)}
MARK_T = 0.6
SPEED = 0.875          # 한 바퀴 약 7초 (디자인: 40ms마다 0.035)
TRAIL_GAP = 0.05

# Michroma "GLaDOS"를 점자로 바꾼 결과 그대로 (손으로 고치지 않음). 5줄(아주 넓은 화면)·4줄(큰 것)·3줄(작은 것)
WORDMARK_4 = (
    "⣰⠞⠛⠛⠛⠛⠛⠛⢶⡀ ⣾               ⢸⠛⠛⠛⠛⠛⠛⠒⢶⡀ ⣴⠞⠛⠛⠛⠛⠛⠛⣦ ⢠⡾⠛⠛⠛⠛⠛⠛⠻⣆",
    "⣿       ⠈⠁ ⣿       ⠰⠟⠛⠛⠛⢳⡆ ⢸       ⢸⡇ ⣿       ⢸⡆⠸⣧⣀⣀⣀⣀⣀⣀⣀⠉",
    "⣿    ⠉⠉⠉⢹⡇ ⣿       ⣠⠶⠶⠶⠶⠾⡇ ⢸       ⢸⡇ ⣿       ⢸⠇⢀⡀ ⠉⠉⠉⠉⠉⠉⣿",
    "⠹⢦⣤⣤⣤⣤⣤⣤⠾⠁ ⢿⣤⣤⣤⣤⣤⣤ ⠻⣤⣤⣤⣤⠼⡇ ⢸⣤⣤⣤⣤⣤⣤⡤⠾⠁ ⠹⢦⣤⣤⣤⣤⣤⡤⠟ ⠈⠷⣤⣤⣤⣤⣤⣤⣤⠟",
)
WORDMARK_3 = (
    "⣴⠋⠉⠉⠉⠙⢷ ⢸⡆     ⣀⣀⣀⣀ ⢸⡟⠛⠛⠛⠛⠳⡆ ⡞⠋⠉⠉⠉⠙⣦ ⡞⠉⠉⠉⠉⠙⣷",
    "⣿  ⠠⠤⠤⢤ ⢸⡇    ⠈⣋⣀⣀⣹⡇⢸⡇     ⣿⢸⡇     ⣿ ⠛⠒⠲⠶⠶⠶⣦",
    "⠻⣄⣀⣀⣀⣠⡾ ⢸⣧⣤⣤⣤⣄⠸⣇⣀⣀⣸⡇⢸⣧⣤⣤⣤⣤⡴⠇ ⢷⣄⣀⣀⣀⣤⠟ ⢷⣄⣀⣀⣀⣠⡿",
)
WORDMARK_5 = (
    "⢠⡶⠟⠛⠛⠛⠛⠛⠛⠻⢶⡄  ⣿                   ⢸⡟⠛⠛⠛⠛⠛⠛⠻⠶⢶⡄  ⣰⡾⠛⠛⠛⠛⠛⠛⠛⠷⣦  ⣠⡾⠛⠛⠛⠛⠛⠛⠛⠛⢷⡄",
    "⣿⠇        ⠘⠃  ⣿         ⢠⣴⠶⠶⠶⠶⢶⣄  ⢸⡇        ⠘⣿ ⢀⣿         ⢻⡇ ⣿⡇        ⠘⠛",
    "⣿     ⠴⠶⠶⠶⢶⣦  ⣿         ⠈⢁⣀⣀⣀⣀⡀⣿⡀ ⢸⡇         ⣿ ⢸⣿         ⢸⡇ ⠈⠛⠻⠶⠶⠶⠶⠶⠶⠶⢶⣄",
    "⣿⡆        ⢸⡿  ⣿         ⣾⡟⠉⠉⠉⠉⠉⣿⡇ ⢸⡇        ⢠⣿ ⠈⣿         ⣼⡇ ⣶⡆        ⢈⣿",
    "⠘⠷⣦⣤⣤⣤⣤⣤⣤⣴⠾⠃  ⣿⣦⣤⣤⣤⣤⣤⣤⡤ ⠻⢧⣤⣤⣤⣤⠴⣿⠇ ⢸⣧⣤⣤⣤⣤⣤⣤⣴⠶⠟⠃  ⠙⠷⣤⣤⣤⣤⣤⣤⣤⡶⠟  ⠘⠷⣤⣤⣤⣤⣤⣤⣤⣤⡾⠏",
)
FULL_NAME = "GROUNDED LOGIC AND DESKTOP ORCHESTRATION SYSTEM"
TAGLINE = "Grounded in your machine."


@dataclass(frozen=True)
class Presence:
    """머리글 심볼의 상태. kind: idle | work | wait | done."""
    kind: str = "idle"
    t: float = MARK_T          # 점 위치 (work에서 움직임)
    pulse: float = 0.0         # 승인 대기의 숨쉬기 0..1


def _point(a: int, b: int, d: float, t: float) -> tuple[float, float]:
    return math.sin(a * t + d), math.sin(b * t)


def _to_dots(x: float, y: float, w: int, h: int, margin: float) -> tuple[int, int]:
    px = round((x * (1 - margin) + 1) / 2 * (w - 1))
    py = round((y * (1 - margin) + 1) / 2 * (h - 1))
    return px, py


def symbol(cols: int, rows: int, presence: Presence = Presence(), margin: float = 0.06) -> Text:
    """리사주 심볼 한 덩어리 (cols×rows 칸). 점은 코발트, 곡선은 흰색(흐린 상태는 회색)."""
    w, h = cols * 2, rows * 4
    a, b, d = SHAPES["idle"] if presence.kind == "idle" else SHAPES["work"]
    curve = [[False] * w for _ in range(h)]
    steps = max(400, (w + h) * 40)
    for i in range(steps + 1):
        x, y = _point(a, b, d, 2 * math.pi * i / steps)
        px, py = _to_dots(x, y, w, h, margin)
        curve[py][px] = True

    # 점(코발트)과 꼬리: 칸 단위로 색을 정한다 (한 칸은 한 색)
    color = [[None] * cols for _ in range(rows)]
    marks: list[tuple[float, float, str]] = []
    radius = 1.0 if cols <= 8 else 1.6
    if presence.kind == "work":
        marks += [(presence.t - 2 * TRAIL_GAP, radius * 0.6, TRAIL[1]), (presence.t - TRAIL_GAP, radius * 0.8, TRAIL[0])]
    if presence.kind == "wait":
        radius *= 0.8 + 0.5 * presence.pulse
    marks.append((presence.t if presence.kind == "work" else MARK_T, radius, COBALT))
    dots = [[False] * w for _ in range(h)]
    for t, r, c in marks:
        x, y = _point(a, b, d, t)
        cx, cy = _to_dots(x, y, w, h, margin)
        span = math.ceil(r)
        for yy in range(cy - span, cy + span + 1):
            for xx in range(cx - span, cx + span + 1):
                if 0 <= xx < w and 0 <= yy < h and (xx - cx) ** 2 + ((yy - cy) * 1.0) ** 2 <= r * r + 0.25:
                    dots[yy][xx] = True
                    color[yy // 4][xx // 2] = c

    line_color = WHITE if presence.kind in ("work", "done") else CURVE_DIM
    out = Text()
    for row in range(rows):
        if row:
            out.append("\n")
        for col in range(cols):
            v = 0
            for dy in range(4):
                for dx in range(2):
                    yy, xx = row * 4 + dy, col * 2 + dx
                    if curve[yy][xx] or dots[yy][xx]:
                        v |= _BIT[(dx, dy)]
            ch = chr(0x2800 + v) if v else " "
            out.append(ch, style=color[row][col] or line_color)
    return out


def _wordmark_lines(rows: int) -> tuple[str, ...]:
    return WORDMARK_5 if rows >= 5 else WORDMARK_4 if rows == 4 else WORDMARK_3


def wordmark(rows: int = 3) -> Text:
    return Text("\n".join(_wordmark_lines(rows)), style=f"bold {WHITE}")


def wordmark_width(rows: int = 3) -> int:
    return len(_wordmark_lines(rows)[0])


def presence_at(kind: str, elapsed: float) -> Presence:
    """상태와 흐른 시간(초)으로 그 순간의 모습. 작업 중이면 점이 돌고, 승인 대기면 점이 숨쉰다."""
    if kind == "work":
        return Presence("work", MARK_T + SPEED * elapsed)
    if kind == "wait":
        return Presence("wait", MARK_T, (1 + math.sin(elapsed * 2.4)) / 2)
    return Presence(kind)

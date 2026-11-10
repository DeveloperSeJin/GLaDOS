"""OS 구분, 실행 권한, 콘솔 설정.

OS마다 다른 코드는 이 파일과 hands/<os>/ 안에만 둔다.
"""

from __future__ import annotations

import os
import platform
import sys

OS_LABELS = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}


def current_os() -> str:
    """'windows' | 'mac' | 'linux'"""
    name = platform.system()
    if name == "Windows":
        return "windows"
    if name == "Darwin":
        return "mac"
    return "linux"


def is_admin() -> bool:
    """관리자 권한(Windows 관리자, root)으로 실행 중인지."""
    if current_os() == "windows":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def enable_ansi() -> bool:
    """굵게·흐리게 표시(ANSI)를 쓸 수 있으면 켜고 True를 돌려준다."""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if current_os() != "windows":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:
        return False


def utf8_mode_on() -> bool:
    """Python UTF-8 모드(PYTHONUTF8=1)가 켜져 있는지. Windows가 아니면 기본이 UTF-8이라 True."""
    return sys.flags.utf8_mode == 1 or current_os() != "windows"

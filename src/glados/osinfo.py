"""OS 구분, 실행 권한, 콘솔 설정.

OS마다 다른 코드는 이 파일과 hands/<os>/ 안에만 둔다.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Mapping

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


# ---------------------------------------------------------------------- 사용자 폴더
USER_FOLDERS = ("downloads", "documents", "desktop")
_EN = {"downloads": "Downloads", "documents": "Documents", "desktop": "Desktop"}
_KO = {"downloads": "다운로드", "documents": "문서", "desktop": "바탕화면"}         # 한국어 리눅스의 실제 폴더 이름
_WIN_VALUE = {   # HKCU\...\Explorer\User Shell Folders 값 이름
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "documents": "Personal",
    "desktop": "Desktop",
}
_XDG = {"downloads": "XDG_DOWNLOAD_DIR", "documents": "XDG_DOCUMENTS_DIR", "desktop": "XDG_DESKTOP_DIR"}


def user_folders(home: str | Path, os_name: str | None = None, env: Mapping[str, str] | None = None) -> dict[str, tuple[str, ...]]:
    """다운로드·문서·바탕화면의 실제 위치 후보 (앞이 우선, 없는 폴더도 들어 있음).

    - Windows: 레지스트리의 실제 위치 (OneDrive나 D 드라이브로 옮긴 경우)
    - 리눅스: ~/.config/user-dirs.dirs (한국어 설치면 ~/다운로드 같은 이름)
    - 맥: 이름이 늘 영어 (화면에만 한국어로 보임)
    """
    os_name = os_name or current_os()
    env = os.environ if env is None else env
    base = Path(home)
    xdg = _xdg_dirs(base, env) if os_name == "linux" else {}
    out: dict[str, tuple[str, ...]] = {}
    for kind in USER_FOLDERS:
        cands: list[str] = []
        if os_name == "windows":
            known = _windows_folder(kind)
            if known:
                cands.append(known)
        elif kind in xdg:
            cands.append(xdg[kind])
        cands.append(str(base / _EN[kind]))
        if os_name == "linux":
            cands.append(str(base / _KO[kind]))
        out[kind] = tuple(dict.fromkeys(cands))
    return out


def _windows_folder(kind: str) -> str | None:
    try:
        import winreg  # type: ignore[import-not-found]

        key = winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_CURRENT_USER,  # type: ignore[attr-defined]
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        with key:
            value, _ = winreg.QueryValueEx(key, _WIN_VALUE[kind])  # type: ignore[attr-defined]
        return os.path.expandvars(str(value)) or None
    except Exception:
        return None


def _xdg_dirs(home: Path, env: Mapping[str, str]) -> dict[str, str]:
    """user-dirs.dirs의 XDG_DOWNLOAD_DIR="$HOME/다운로드" 같은 줄을 읽는다."""
    config = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    wanted = {v: k for k, v in _XDG.items()}
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        kind = wanted.get(name.strip())
        if kind is None:
            continue
        value = value.strip().strip('"')
        if value.startswith("$HOME"):
            value = str(home) + value[len("$HOME"):]
        if not (value.startswith("/") or os.path.isabs(value)):
            continue
        value = os.path.normpath(value)
        if value != os.path.normpath(str(home)):          # 홈 자체면 "안 씀"이라는 뜻
            found[kind] = value
    return found

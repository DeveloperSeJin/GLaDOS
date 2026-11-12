"""화면에서 Ctrl+클릭한 파일·폴더 열기 (M8.1, 연구원님 "경로도 링크 되게").

- 폴더: 파일 탐색기(Finder)로 연다
- 파일: 그 파일의 기본 프로그램으로 연다 (json -> 편집기, pdf -> 뷰어)
- 실행되는 파일(exe, bat, ps1, py, lnk, ...)은 열지 않고 폴더에서 그 파일을 골라 보여 준다
  (클릭 한 번으로 프로그램이 돌지 않게. 실행은 연구원님이 직접)
- 없는 경로는 아무것도 하지 않는다
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# 열면 실행되는 형식: 폴더에서 보여 주기만 한다
RUNNABLE = {
    ".exe", ".com", ".bat", ".cmd", ".msi", ".msp", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
    ".wsh", ".hta", ".scr", ".pif", ".cpl", ".msc", ".lnk", ".url", ".reg", ".jar", ".py", ".pyw", ".pyz", ".sh",
    ".bash", ".zsh", ".command", ".app", ".appref-ms", ".application", ".gadget", ".inf", ".scf", ".sys", ".dll",
}


def target(path: str) -> Path:
    return Path(os.path.expanduser(path.strip()))


def open_path(path: str, os_name: str) -> str:
    """'opened' | 'revealed' | 'missing' | 'failed'."""
    p = target(path)
    try:
        if not p.exists():
            return "missing"
        if p.is_dir():
            _open(p, os_name)
            return "opened"
        if p.suffix.lower() in RUNNABLE:
            _reveal(p, os_name)
            return "revealed"
        _open(p, os_name)
        return "opened"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "failed"


def _open(p: Path, os_name: str) -> None:
    if os_name == "windows":
        os.startfile(str(p))                                     # type: ignore[attr-defined]
    elif os_name == "mac":
        subprocess.Popen(["open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _reveal(p: Path, os_name: str) -> None:
    """폴더를 열고 그 파일을 골라 둔다 (실행하지 않음)."""
    if os_name == "windows":
        subprocess.Popen(["explorer", f"/select,{p}"])
    elif os_name == "mac":
        subprocess.Popen(["open", "-R", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", str(p.parent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

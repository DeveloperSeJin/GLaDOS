"""운영 체제의 클립보드 (TUI의 복사·붙여넣기).

- Windows: user32의 클립보드 함수 (ctypes). 터미널 종류와 상관없이 된다
- 맥: pbcopy·pbpaste. 리눅스: wl-copy·wl-paste, xclip, xsel 중 있는 것
- 안 되면 False·None을 돌려준다. 그러면 TUI가 터미널 복사(OSC 52)로 넘긴다 (붙여넣기는 터미널의 Ctrl+V)
"""

from __future__ import annotations

import shutil
import subprocess
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
TIMEOUT_S = 2

LINUX_COPY = (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"])
LINUX_PASTE = (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"])


class SystemClipboard:
    def __init__(self, os_name: str):
        self.os = os_name

    def copy(self, text: str) -> bool:
        try:
            if self.os == "windows":
                return _win_copy(text)
            if self.os == "mac":
                return _run_copy(["pbcopy"], text)
            return any(_run_copy(cmd, text) for cmd in LINUX_COPY if shutil.which(cmd[0]))
        except (OSError, AttributeError, ValueError, subprocess.SubprocessError):
            return False

    def paste(self) -> str | None:
        try:
            if self.os == "windows":
                return _win_paste()
            if self.os == "mac":
                return _run_paste(["pbpaste"])
            for cmd in LINUX_PASTE:
                if shutil.which(cmd[0]):
                    got = _run_paste(cmd)
                    if got is not None:
                        return got
            return None
        except (OSError, AttributeError, ValueError, subprocess.SubprocessError):
            return None


class MemoryClipboard:
    """화면 없이 돌 때(시험)의 클립보드: 이 프로그램 안에서만."""

    def __init__(self) -> None:
        self.text: str | None = None

    def copy(self, text: str) -> bool:
        self.text = text
        return True

    def paste(self) -> str | None:
        return self.text


def _run_copy(cmd: list[str], text: str) -> bool:
    done = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=TIMEOUT_S)
    return done.returncode == 0


def _run_paste(cmd: list[str]) -> str | None:
    done = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S)
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------- Windows
def _win_api():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)          # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)      # type: ignore[attr-defined]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    return ctypes, user32, kernel32


def _win_open(user32) -> bool:
    for _ in range(10):                                # 다른 프로그램이 잠깐 열고 있을 수 있다
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def _win_copy(text: str) -> bool:
    ctypes, user32, kernel32 = _win_api()
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le") + b"\x00\x00"
    if not _win_open(user32):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)                # 넘기지 못했으면 우리가 푼다 (넘겼으면 클립보드 것)
            return False
        return True
    finally:
        user32.CloseClipboard()


def _win_paste() -> str | None:
    ctypes, user32, kernel32 = _win_api()
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _win_open(user32):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr).replace("\r\n", "\n")
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()

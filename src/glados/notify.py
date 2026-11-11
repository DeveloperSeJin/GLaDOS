"""OS 알림 (M5): 서버 실행이 끝나거나 손실이 NaN이면 다른 창을 보고 있어도 알 수 있게.

- Windows: Windows PowerShell 5.1의 알림 센터(토스트). 글은 -EncodedCommand(UTF-16)로 넘겨 따옴표·한글이 안전
- macOS: osascript display notification (글은 인자로)
- 리눅스: notify-send (있으면)
늘 배경에서, 실패해도 조용히 (GLaDOS 화면의 알림이 먼저다). [ui] notify = false면 쓰지 않는다.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import threading
from xml.sax.saxutils import escape

# Windows가 이미 아는 앱 ID (Windows PowerShell). 따로 설치한 앱 ID가 없어도 알림 센터에 뜬다
PS_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
MAX_BODY = 300


def windows_script(title: str, body: str) -> str:
    """토스트를 띄우는 PowerShell 글. 글은 XML로 이스케이프하고 PowerShell 작은따옴표 안에 넣는다."""
    xml = ("<toast><visual><binding template='ToastGeneric'>"
           f"<text>{escape(title)}</text><text>{escape(body[:MAX_BODY])}</text>"
           "</binding></visual></toast>")
    xml_ps = xml.replace("'", "''")
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$x.LoadXml('{xml_ps}')\n"
        "$n = New-Object Windows.UI.Notifications.ToastNotification $x\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{PS_APP_ID}').Show($n)\n"
    )


def command(title: str, body: str, os_name: str) -> list[str] | None:
    """이 OS에서 알림을 띄우는 명령 (없으면 None)."""
    if os_name == "windows":
        encoded = base64.b64encode(windows_script(title, body).encode("utf-16-le")).decode("ascii")
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                "-EncodedCommand", encoded]
    if os_name == "macos":
        return ["osascript", "-e", "on run argv", "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                title, body[:MAX_BODY]]
    if shutil.which("notify-send"):
        return ["notify-send", "--app-name=GLaDOS", title, body[:MAX_BODY]]
    return None


def os_notify(title: str, body: str, os_name: str) -> None:
    cmd = command(title, body, os_name)
    if cmd is None:
        return

    def run() -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            pass

    threading.Thread(target=run, name="glados-notify", daemon=True).start()

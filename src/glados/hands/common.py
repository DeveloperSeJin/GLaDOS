"""모든 OS에서 같은 코드로 도는 손.

M2: fs.search, fs.read, fs.write_new, fs.edit (hands/fs.py)
M2.4: shell.run (hands/shell.py, 셸만 OS마다 다름: Windows PowerShell, 맥·리눅스 bash/zsh)
M3: web.search, web.fetch (hands/web.py, 외부 전송으로 센다)
M4·M5: remote.gpu, remote.log, remote.exec, remote.run, git.snapshot (asyncssh)
"""

from __future__ import annotations


def tools(hctx) -> list:
    from glados.hands.fs import fs_tools
    from glados.hands.shell import shell_tools
    from glados.hands.web import web_tools

    return fs_tools(hctx) + shell_tools(hctx) + web_tools(hctx)

"""모든 OS에서 같은 코드로 도는 손.

M2: fs.search, fs.read, fs.write_new, fs.edit (hands/fs.py)
M2.4: shell.run (hands/shell.py, 셸만 OS마다 다름: Windows PowerShell, 맥·리눅스 bash/zsh)
M3: web.search, web.fetch (hands/web.py, 외부 전송으로 센다)
M4: remote.gpu, remote.log, remote.exec (hands/remote.py, asyncssh. servers.toml에 서버가 있을 때만)
M5: remote.run, git.snapshot
M6: session.resume, memory.note (hands/session.py)
"""

from __future__ import annotations


def tools(hctx) -> list:
    from glados.hands.fs import fs_tools
    from glados.hands.remote import remote_tools
    from glados.hands.session import session_tools
    from glados.hands.shell import shell_tools
    from glados.hands.web import web_tools

    return fs_tools(hctx) + shell_tools(hctx) + web_tools(hctx) + remote_tools(hctx) + session_tools(hctx)

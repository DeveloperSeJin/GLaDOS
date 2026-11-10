"""위치 세 갈래: 일반 / 보호 / GLaDOS 안전장치 (integration-decisions.md 3장).

- 일반 위치: 읽기·찾기·새 파일 허용
- 보호 위치: 키·비밀번호, 시스템·자동 실행 폴더, 셸 설정 파일 -> 읽기·쓰기 모두 승인
- GLaDOS 안전장치: 설정·기록 폴더(GLADOS_HOME) 전체, 저장소의 .glados 폴더, GLaDOS 소스 폴더
  -> GLaDOS 도구로는 쓰기·삭제 차단 (승인으로도 못 풂). 읽기는 허용

경로 판정은 그 OS의 표기법으로 한다. 시험할 때는 다른 OS를 흉내 낼 수 있다 (Places.os).
"""

from __future__ import annotations

import fnmatch
import ntpath
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from glados import osinfo

# 홈 아래의 키·비밀번호 폴더와 파일
SECRET_HOME = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
    ".config/gcloud", ".config/gh", ".netrc", ".git-credentials", ".pgpass", ".pypirc",
)
# 홈 아래의 셸 설정 파일 (자동 실행)
SHELL_HOME = (
    ".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zprofile",
    ".zshenv", ".zlogin", ".config/fish/config.fish", ".config/powershell",
)
# 어디에 있든 이름으로 보호하는 파일
SECRET_NAMES = (
    ".env", ".env.*", "*.pem", "*.p12", "*.pfx", "*.kdbx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
)

OS_PROTECTED: dict[str, dict[str, tuple[str, ...]]] = {
    "windows": {
        "abs": ("{SystemRoot}", "{ProgramFiles}", "{ProgramFiles(x86)}", "{ProgramData}"),
        "home": ("AppData", "Documents/PowerShell", "Documents/WindowsPowerShell"),
        "home_names": ("ntuser.dat*",),
    },
    "mac": {
        "abs": ("/System", "/Library", "/private/etc", "/private/var", "/etc", "/var", "/usr", "/bin", "/sbin"),
        "home": ("Library",),
        "home_names": (),
    },
    "linux": {
        "abs": (
            "/etc", "/usr", "/boot", "/var", "/root", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
            "/proc", "/sys", "/dev", "/snap",
        ),
        "home": (
            ".config/autostart", ".config/systemd", ".local/share/keyrings", ".pki", ".mozilla",
            ".config/google-chrome", ".config/chromium", ".config/BraveSoftware", ".config/microsoft-edge",
        ),
        "home_names": (),
    },
}

WINDOWS_ABS_DEFAULTS = {
    "SystemRoot": "C:\\Windows",
    "ProgramFiles": "C:\\Program Files",
    "ProgramFiles(x86)": "C:\\Program Files (x86)",
    "ProgramData": "C:\\ProgramData",
}


@dataclass(frozen=True)
class Places:
    """판정에 필요한 이 컴퓨터의 위치들 (그 OS의 표기법)."""

    os: str
    home: str
    glados_home: str
    source_dirs: tuple[str, ...] = ()
    extra_protected: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None
    resolve_links: bool = True   # 실제 컴퓨터에서는 바로가기(링크)를 따라가 판정. 시험에서는 끔

    @classmethod
    def for_this_computer(
        cls,
        glados_home: Path,
        source_dirs: tuple[str, ...] = (),
        extra_protected: tuple[str, ...] = (),
    ) -> "Places":
        keys = (
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "HOME", "XDG_CONFIG_HOME",
            "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "GLADOS_HOME",
        )
        env = tuple((k, os.environ[k]) for k in keys if k in os.environ)
        return cls(
            os=osinfo.current_os(),
            home=str(Path.home()),
            glados_home=str(glados_home),
            source_dirs=tuple(source_dirs),
            extra_protected=tuple(extra_protected),
            env=env,
        )

    def env_get(self, key: str) -> str | None:
        for k, v in self.env:
            if (k.upper() == key.upper()) if self.os == "windows" else (k == key):
                return v
        if key.upper() in ("USERPROFILE", "HOME"):
            return self.home
        return None


@dataclass(frozen=True)
class Location:
    path: str            # 정규화한 경로 (그 OS 표기)
    protected: bool      # 보호 위치
    safety: bool         # GLaDOS 안전장치
    why: str             # 걸린 규칙 설명 (없으면 "")


def _mod(os_name: str):
    return ntpath if os_name == "windows" else posixpath


def _sep(os_name: str) -> str:
    return "\\" if os_name == "windows" else "/"


def fold(os_name: str, s: str) -> str:
    """비교용. Windows·맥은 대소문자를 가리지 않는다."""
    return s.casefold() if os_name in ("windows", "mac") else s


def normalize(raw: str, places: Places) -> str:
    """~, 환경 변수, 상대 경로, '..'를 풀어 그 OS 표기의 절대 경로로."""
    m = _mod(places.os)
    s = str(raw).strip().strip('"').strip("'")
    if places.os == "windows":
        s = s.replace("/", "\\")
        if s.startswith("\\\\?\\"):
            s = s[4:]
        s = re.sub(r"%([^%]+)%", lambda mo: places.env_get(mo.group(1)) or mo.group(0), s)
        s = re.sub(r"\$env:([A-Za-z_][A-Za-z0-9_()]*)", lambda mo: places.env_get(mo.group(1)) or mo.group(0), s, flags=re.I)
    else:
        s = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda mo: places.env_get(mo.group(1)) or mo.group(0), s)
    if s == "~" or s.startswith("~/") or s.startswith("~\\"):
        s = places.home + s[1:]
    base = places.cwd or places.home
    if places.os == "windows":
        drive, rest = ntpath.splitdrive(s)
        if s.startswith("\\\\"):
            pass                                   # \\서버\공유 (네트워크)
        elif drive and not rest.startswith("\\"):
            s = drive + "\\" + rest                # C:x -> C:\x
        elif not drive and s.startswith("\\"):
            s = ntpath.splitdrive(base)[0] + s     # \x -> 지금 드라이브의 \x
        elif not drive:
            s = ntpath.join(base, s)               # 상대 경로
    elif not s.startswith("/"):
        s = posixpath.join(base, s)
    s = m.normpath(s)
    if places.os == "windows":
        drive, rest = ntpath.splitdrive(s)
        s = drive.upper() + rest
    return s


def _resolve_links(path: str, places: Places) -> str:
    """이 컴퓨터의 실제 경로라면 바로가기(심볼릭 링크·정션)를 따라간다."""
    if places.os != osinfo.current_os():
        return path
    try:
        if os.path.lexists(path):
            return normalize(os.path.realpath(path), places)
    except OSError:
        pass
    return path


def _under(path_f: str, root: str, places: Places) -> bool:
    root_f = fold(places.os, normalize(root, places))
    sep = _sep(places.os)
    return path_f == root_f or path_f.startswith(root_f.rstrip(sep) + sep)


def _os_abs_roots(places: Places) -> list[str]:
    roots = []
    for r in OS_PROTECTED[places.os]["abs"]:
        if places.os == "windows":
            key = r.strip("{}")
            roots.append(places.env_get(key) or WINDOWS_ABS_DEFAULTS[key])
        else:
            roots.append(r)
    return roots


def classify(raw: str, places: Places) -> Location:
    path = normalize(raw, places)
    if places.resolve_links:
        path = _resolve_links(path, places)
    m = _mod(places.os)
    pf = fold(places.os, path)
    parts = [p for p in pf.replace("/", "\\").split("\\")] if places.os == "windows" else pf.split("/")
    name = parts[-1] if parts else ""
    home = places.home

    safety_why = ""
    if _under(pf, places.glados_home, places):
        safety_why = "GLaDOS 설정·기록 폴더"
    elif any(_under(pf, s, places) for s in places.source_dirs):
        safety_why = "GLaDOS 소스 폴더"
    elif ".glados" in parts:
        safety_why = "저장소의 GLaDOS 정책 폴더(.glados)"

    protected_why = ""
    for rel in SECRET_HOME:
        if _under(pf, m.join(home, rel), places):
            protected_why = f"키·비밀번호 ({rel})"
            break
    if not protected_why:
        for pattern in SECRET_NAMES:
            if fnmatch.fnmatchcase(name, fold(places.os, pattern)):
                protected_why = f"키·비밀번호 파일 ({pattern})"
                break
    if not protected_why:
        for rel in SHELL_HOME:
            if _under(pf, m.join(home, rel), places):
                protected_why = f"셸 설정 ({rel})"
                break
    if not protected_why:
        for rel in OS_PROTECTED[places.os]["home"]:
            if _under(pf, m.join(home, rel), places):
                protected_why = f"시스템·자동 실행 ({rel})"
                break
    if not protected_why:
        home_f = fold(places.os, normalize(home, places))
        parent = fold(places.os, m.dirname(path))
        if parent == home_f:
            for pattern in OS_PROTECTED[places.os]["home_names"]:
                if fnmatch.fnmatchcase(name, pattern):
                    protected_why = f"시스템 파일 ({pattern})"
                    break
    if not protected_why:
        for root in _os_abs_roots(places):
            if _under(pf, root, places):
                protected_why = f"시스템 폴더 ({root})"
                break
    if not protected_why:
        for extra in places.extra_protected:
            if _under(pf, extra, places):
                protected_why = f"추가 보호 위치 ({extra})"
                break

    return Location(
        path=path,
        protected=bool(protected_why),
        safety=bool(safety_why),
        why=safety_why or protected_why,
    )


def mentions_safety(command: str, places: Places) -> str:
    """명령 글에 안전장치 위치가 나오면 그 이유를, 아니면 ""를 돌려준다.

    셸 명령의 모든 경로를 알아낼 수는 없어서 글자 비교로 걸러 내는 1차 방어다.
    완전한 차단은 OS 권한으로만 가능하다 (v1에서 검토).
    """
    variants = {command, command.replace("/", "\\"), command.replace("\\", "/")}
    folded = [fold(places.os, v) for v in variants]
    lower = [v.casefold() for v in variants]
    roots = [places.glados_home, *places.source_dirs]
    for root in roots:
        r = normalize(root, places)
        for cand in {r, r.replace("\\", "/")}:
            cf = fold(places.os, cand)
            if any(cf in v for v in folded):
                return "GLaDOS 안전장치 위치"
    if any(".glados" in v or "policy.yaml" in v or "glados_home" in v for v in lower):
        return "GLaDOS 안전장치 위치"
    return ""

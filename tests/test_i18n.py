"""화면 언어 시험: 두 벌이 빠짐없이 있고, 영어 화면에 한글이 새지 않는다."""

from __future__ import annotations

import io
import re
import string
from pathlib import Path

import pytest

from conftest import FakeLLM, act, answer, check, make_ctx, make_places
from glados import cli, i18n, osinfo
from glados.config import ConfigError, ensure_home, load_config, peek_language
from glados.core.context import BRIEF, CLIPPED, DROPPED, FULL
from glados.core.guards import about_files
from glados.hands import Tool, ToolRegistry
from glados.i18n import TEXT, localized, t

HANGUL = re.compile(r"[\uac00-\ud7a3\u3131-\u318e]")
SRC = Path(__file__).resolve().parent.parent / "src" / "glados"


def fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


# ---------------------------------------------------------------- 표
def test_every_key_has_both_languages_with_same_placeholders():
    for key, entry in TEXT.items():
        assert set(entry) == {"ko", "en"}, key
        assert entry["ko"].strip() and entry["en"].strip(), key
        assert fields(entry["ko"]) == fields(entry["en"]), key


def test_english_text_has_no_korean():
    leaks = {k: e["en"] for k, e in TEXT.items() if HANGUL.search(e["en"])}
    assert leaks == {}


def test_keys_used_in_code_exist():
    pattern = re.compile(
        r'"((?:usage|header|greet|bye|admin|config|utf8|model|ollama|llm|task|render|guard|stop|approve|choose|help|'
        r'command|new|lang|context|level|log|who|undo|policy|loc)(?:\.[a-z0-9_]+)+|usage|help|bye)"'
    )
    used = set()
    for path in SRC.rglob("*.py"):
        if path.name == "i18n.py":
            continue
        found = pattern.findall(path.read_text(encoding="utf-8"))
        used |= {k for k in found if k.rsplit(".", 1)[-1] not in {"yaml", "jsonl", "json", "toml", "md", "db"}}  # 파일 이름은 빼고
    dynamic = {f"stop.{r}" for r in cli.STOPS} | {f"level.{lv}" for lv in (FULL, CLIPPED, BRIEF, DROPPED)}
    dynamic |= {f"policy.{v}" for v in ("allow", "deny", "wait")}
    missing = sorted(k for k in used | dynamic if k not in TEXT)
    assert missing == []


def test_t_falls_back_and_formats():
    assert t("없는 키") == "없는 키"
    assert t("stop.same_call", "en", tool="fs.read", count=3) == "Stopped: the same call (fs.read) repeated 3 times."
    i18n.set_language("en")
    assert t("render.working") == "Working..." and t("render.working", "ko") == "작업 중..."
    with pytest.raises(ValueError):
        i18n.set_language("fr")


# ---------------------------------------------------------------- 정책 판정 이유
@pytest.mark.parametrize("os_name, tool, args", [
    ("linux", "fs.read", {"path": "~/.ssh/id_rsa"}),                   # 보호 위치 (키)
    ("linux", "fs.edit", {"path": "~/notes.md"}),                      # 원본 고치기
    ("linux", "fs.write_new", {"path": "~/.glados/policy.yaml"}),      # 안전장치
    ("linux", "shell.run", {"command": "sudo rm -rf /tmp/x"}),         # 명령 안 관리자
    ("windows", "shell.run", {"command": "winget install x", "admin": True}),
    ("linux", "remote.exec", {"command": "sudo reboot", "server": "a"}),
    ("linux", "fs.read", {"path": "/etc/passwd"}),                     # 시스템 폴더
    ("windows", "fs.edit", {"path": "C:\\Windows\\System32\\drivers\\etc\\hosts"}),
    ("mac", "fs.write_new", {"path": "~/Library/LaunchAgents/x.plist"}),
])
def test_policy_reasons_in_both_languages(policy, os_name, tool, args):
    d = check(policy, os_name, tool, **args)
    assert d.key.startswith("policy.")
    assert localized(d, lang="ko") == d.reason                          # 기록용 한국어와 같은 글
    english = localized(d, lang="en")
    assert english and not HANGUL.search(english.replace(str(args.get("path", "")), "")), english


def test_unregistered_and_no_rule_in_english(policy):
    d = check(policy, "linux", "app.open", ctx=make_ctx("linux", registered_tools=frozenset()), name="x")
    assert localized(d, lang="en") == "Unregistered tool (app.open)"


def test_log_names_in_english():
    assert i18n.rule_text("코드:안전장치", "en") == "code:safety"
    assert i18n.rule_text("기본:fs.search:*", "en") == "default:fs.search:*"
    assert i18n.rule_text("기본:fs.search:*", "ko") == "기본:fs.search:*"
    assert i18n.who_text("연구원님:이번만", "en") == "you: once" and i18n.who_text("정책", "en") == "policy"


# ---------------------------------------------------------------- 설정
def test_language_setting(tmp_path):
    ensure_home(tmp_path)
    assert load_config(tmp_path).language == "ko" and peek_language(tmp_path) == "ko"
    text = (tmp_path / "glados.toml").read_text(encoding="utf-8").replace('language = "ko"', 'language = "en"')
    (tmp_path / "glados.toml").write_text(text, encoding="utf-8")
    assert load_config(tmp_path).language == "en" and peek_language(tmp_path) == "en"
    (tmp_path / "glados.toml").write_text(text.replace('language = "en"', 'language = "fr"'), encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(tmp_path)
    assert e.value.key == "config.language" and "ui.language" in localized(e.value, lang="en")
    assert peek_language(tmp_path) == "ko"                              # 잘못된 값이면 기본값


def test_old_settings_without_ui_section_stay_korean(tmp_path):
    (tmp_path / "glados.toml").write_text('[model]\nmode = "low"\n', encoding="utf-8")
    assert load_config(tmp_path).language == "ko"


def test_main_messages_follow_setting(monkeypatch, tmp_path, capsys):
    ensure_home(tmp_path)
    text = (tmp_path / "glados.toml").read_text(encoding="utf-8")
    (tmp_path / "glados.toml").write_text(text.replace('language = "ko"', 'language = "en"'), encoding="utf-8")
    monkeypatch.setenv("GLADOS_HOME", str(tmp_path))
    monkeypatch.setattr(osinfo, "is_admin", lambda: True)
    assert cli.main([]) == 2
    assert "administrator rights" in capsys.readouterr().out
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Usage") and not HANGUL.search(out)


# ---------------------------------------------------------------- 영어 화면 전체 흐름
@pytest.fixture
def english_app(tmp_path):
    ensure_home(tmp_path)
    text = (tmp_path / "glados.toml").read_text(encoding="utf-8").replace('language = "ko"', 'language = "en"')
    (tmp_path / "glados.toml").write_text(text, encoding="utf-8")
    out = io.StringIO()
    a = cli.App(load_config(tmp_path), cli.Screen(ansi=False, out=out), llm=FakeLLM([]))
    a.places = make_places("linux", cwd=str(tmp_path))
    a.hctx.places = a.places
    a.tools = ToolRegistry([
        Tool("fs.search", "find", run=lambda args: "1개 찾음 (범위: /home/sejin/Downloads)\n- /home/sejin/Downloads/paper.pdf · 1KB · 2026-10-02"),
        Tool("fs.edit", "edit", run=lambda args: "edited"),
    ])
    yield a, out
    a.close()


def test_english_screen_has_no_korean(english_app, monkeypatch, tmp_path):
    a, out = english_app
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")       # 승인 카드에서 거절
    a.llm.steps = [
        act("fs.edit", path="~/notes.md", old="a", new="b"),           # 승인 카드
        act("shell.run", command="ls"),                                # 등록되지 않은 도구 -> 차단
        answer("I did not edit the file.", "Restraint is underrated."),
        act("fs.search", query="paper"), act("fs.search", query="paper"), act("fs.search", query="paper"),
        answer("Found one paper: paper.pdf.", "", ["/home/sejin/Downloads/paper.pdf"]),
    ]
    out.write(a.header() + "\n")
    a.ask_task("Edit my notes")
    a.ask_task("Find the paper in Downloads")
    a.run_direct('echo "hello"')
    a.run_direct("cd no-such-folder")
    a.run_direct("")
    a.set_cwd(str(tmp_path / "gone"))
    a.run_direct("ls")                                                  # 작업 폴더가 사라진 경우의 오류도 영어로
    a.set_cwd(str(tmp_path))
    for cmd in ("/help", "/context", "/log", "/undo", "/servers", "/nope", "/lang", "/new", "/context"):
        a.command(cmd)
    text = out.getvalue()
    assert "[Approval needed] Edits the original file (a checkpoint is saved first)" in text
    assert "1) Once   2) For this task   3) Deny" in text
    assert "[Blocked] shell.run: Unregistered tool (shell.run)" in text
    assert "(Stopped: the same call (fs.search) repeated 3 times.)" in text
    assert "Remembering 3 past tasks" in text and "[full] Edit my notes" in text and "[full] !echo \"hello\"" in text
    assert "you: denied" in text and "default:fs.edit:*" in text        # /log
    assert "model qwen3.5:9b" in text and "Sources: /home/sejin/Downloads/paper.pdf" in text
    assert '[Run] echo "hello" · exit 0' in text and "No such folder" in text and "Usage: !command" in text
    assert "Could not run: The working folder does not exist" in text
    leaks = [line for line in text.splitlines() if HANGUL.search(line)]
    assert leaks == []


def test_lang_command_switches_this_session(english_app):
    a, out = english_app
    a.command("/lang ko")
    assert "화면 언어: 한국어" in out.getvalue() and i18n.get_language() == "ko"
    a.command("/help")
    assert "명령어" in out.getvalue()
    a.command("/lang en")
    assert "Screen language: English" in out.getvalue()
    a.command("/lang xx")
    assert "Usage: /lang ko or /lang en (now: English)" in out.getvalue()


# ---------------------------------------------------------------- 영어 요청
def test_file_talk_is_detected_in_english():
    """근거 확인은 영어 글(요청 + 모델의 영어 생각과 답)로 판단한다."""
    for q in ("find the paper in my Downloads", "Summarize the PDF you found", "read it again", "save that as notes.md",
              "다운로드에서 찾아줘\nSearch the Downloads folder for the thesis."):
        assert about_files(q), q
    for q in ("hello", "how are you", "tell me a joke", "I am already here", "안녕\nGreet the Researcher."):
        assert not about_files(q), q


def test_english_folder_names_in_search(tmp_path):
    from glados.hands import HandContext
    from glados.hands import fs
    from glados.safety.locations import Places
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Downloads" / "paper.pdf").write_bytes(b"%PDF-1.4")
    hctx = HandContext(places=Places(os=osinfo.current_os(), home=str(home), glados_home=str(home / ".glados"),
                                     resolve_links=False), home=home / ".glados")
    for root in ("Downloads", "downloads folder", "~/Downloads"):
        assert "paper.pdf" in fs.search({"query": "paper", "roots": [root]}, hctx), root

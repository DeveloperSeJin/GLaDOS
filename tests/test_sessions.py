"""대화 세션과 기억 시험 (M6).

- 세션 저장소: 작업이 끝날 때마다 저장, 최근 목록, 낱말로 찾기, 요약
- /resume (목록, 번호), 말로 이어하기 (session.resume: 작업이 끝난 뒤 불러오기), 다시 켰을 때 안내
- MEMORY.md: memory.note (매번 승인, 승인 상자에 글), 중복, /memory, /memory del, 시스템 프롬프트, 길이 제한
"""

from __future__ import annotations

import io
from datetime import datetime

import pytest

from conftest import FakeLLM, act, answer, check, make_ctx
from glados import cli, osinfo
from glados.config import ensure_home, load_config
from glados.core.context import Turn
from glados.memory.notes import Notes, budget
from glados.memory.sessions import SessionStore, recap
from glados.safety.policy import Policy, ToolCall


def open_app(home, llm=None, approve=None, os_name: str = "linux") -> tuple[cli.App, io.StringIO]:
    ensure_home(home)
    out = io.StringIO()
    a = cli.App(load_config(home), cli.Screen(ansi=False, out=out), llm=llm or FakeLLM([]), os_name=os_name)
    if approve is not None:
        a.approve = approve
    return a, out


# ---------------------------------------------------------------- 저장소
def test_store_keeps_turns_in_order_and_lists_recent(tmp_path):
    store = SessionStore(tmp_path)
    a = store.new("GPU 실험 결과 정리해줘", now=1000.0)
    store.append(a, Turn("GPU 실험 결과 정리해줘", [{"role": "assistant", "content": "{}"}], "Loss fell to 0.31."), now=1001.0)
    store.append(a, Turn("그래프도", answer="Saved metrics.png."), now=1002.0)
    b = store.new("find the contract", now=2000.0)
    store.append(b, Turn("find the contract", answer="Found 2025_contract.pdf."), now=2001.0)
    store.new("empty", now=3000.0)                                              # 턴이 없는 세션은 목록에 없다
    assert [s.id for s in store.recent()] == [b, a]
    assert [s.id for s in store.recent(exclude=b)] == [a]
    info = store.get(a)
    assert (info.title, info.count, info.last) == ("GPU 실험 결과 정리해줘", 2, 1002.0)
    turns = store.turns(a)
    assert [t.request for t in turns] == ["GPU 실험 결과 정리해줘", "그래프도"]
    assert turns[0].messages == [{"role": "assistant", "content": "{}"}] and turns[1].answer == "Saved metrics.png."
    # 낱말로 찾기: 연구원님 말(한국어)과 답(영어) 모두에서. 비면 가장 최근, 안 맞으면 None
    assert store.find("GPU 실험").id == a
    assert store.find("loss metrics").id == a
    assert store.find("").id == b
    assert store.find("contract", exclude=b) is None
    assert store.find("완전히 다른 말") is None
    # 맞은 낱말 수가 같으면 작업이 많은 쪽 (M6.1: 방금 실패한 "이어서" 시도보다 실제로 하던 대화)
    c = store.new("아까 했던 PDF찾던거 이어서 하자", now=4000.0)
    store.append(c, Turn("아까 했던 PDF찾던거 이어서 하자", answer="No PDF files were found."), now=4001.0)
    d = store.new("다운로드에서 PDF 찾아줘", now=500.0)
    for i in range(3):
        store.append(d, Turn(f"PDF 질문 {i}", answer="Found 2 PDFs."), now=501.0 + i)
    assert store.find("PDF").id == d
    assert store.find("이어서 PDF").id == c                                        # 더 많이 맞으면 그쪽
    e = store.new("PDF 하나 더", now=5000.0)
    for i in range(3):
        store.append(e, Turn(f"PDF {i}", answer="ok"), now=5001.0 + i)
    assert store.find("PDF").id == e                                               # 낱말·작업 수가 같으면 최근 것
    store.close()


def test_command_only_sessions_are_not_listed(tmp_path):
    """M6.6: '!명령'만 있는 대화는 지난 대화 목록·찾기·이어하기 카드에 넣지 않는다 (저장은 한다).
    명령으로 시작했다가 말로 이어 간 대화는 제목을 첫 요청으로."""
    store = SessionStore(tmp_path)
    a = store.new("find the contract", now=1000.0)
    store.append(a, Turn("find the contract", answer="Found 2025_contract.pdf."), now=1001.0)
    b = store.new("!git status", now=2000.0)
    store.append(b, Turn("!git status", answer="(This is the output of a command the Researcher ran directly.)"), now=2001.0)
    c = store.new("!dir", now=3000.0)
    store.append(c, Turn("!dir", answer="(command output)"), now=3001.0)
    store.append(c, Turn("이 폴더에서 제일 큰 파일은?", answer="model.bin, 2.1 GB."), now=3002.0)
    assert [s.id for s in store.recent()] == [c, a]
    assert store.recent()[0].title == "이 폴더에서 제일 큰 파일은?"
    assert store.find("").id == c and store.find("git status") is None and store.find("contract").id == a
    assert store.get(b).title == "!git status" and len(store.turns(b)) == 1              # 저장은 그대로
    store.close()


def test_recap_lists_recent_tasks(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.new("first", now=0.0)
    for i in range(10):
        store.append(sid, Turn(f"request {i}\nsecond line", answer=f"Answer {i}.\nSources: a.pdf"))
    text = recap(store.get(sid), store.turns(sid))
    assert "(10 tasks), first request: first" in text
    assert "(the 2 earlier tasks are left out here)" in text
    assert "3. request 2 second line -> Answer 2." in text and "10. request 9 second line -> Answer 9." in text
    assert "Sources" not in text


# ---------------------------------------------------------------- /resume
def test_sessions_survive_a_restart_and_resume_by_number(tmp_path):
    home = tmp_path / "home"
    a, _ = open_app(home, FakeLLM([answer("Loss is 0.31."), answer("The best epoch is 7.")]))
    a.ask_task("how is the cifar run")
    a.ask_task("which epoch was best")
    sid = a.hctx.session_id
    a.close()

    b, out = open_app(home, FakeLLM([answer("Epoch 7 had dev acc 0.91.")]))
    try:
        assert b.memory.turns == [] and b.hctx.session_id == ""
        assert b.resume_hint().id == sid
        prompt = b.system_prompt()
        assert 'Earlier conversations (newest first): ' in prompt and '"how is the cifar run" (2 tasks).' in prompt
        b.command("/resume")
        assert "1. " in out.getvalue() and "how is the cifar run (작업 2개)" in out.getvalue()
        b.command("/resume 1")
        assert b.hctx.session_id == sid and len(b.memory.turns) == 2
        assert "Earlier conversations" not in b.system_prompt()                # 기억이 있으면 안내하지 않는다
        b.ask_task("and its dev acc?")
        sent = b.llm.calls[-1][0]
        assert any(m["content"] == "which epoch was best" for m in sent)
        assert [t.request for t in b.sessions.turns(sid)][-1] == "and its dev acc?"
        b.command("/resume")
        assert "이어 갈 지난 대화가 없습니다" in out.getvalue()                    # 지금 세션은 목록에서 빠진다
    finally:
        b.close()


def test_prompt_lists_three_recent_conversations_rule_and_marks_examples(tmp_path):
    """M6.1: 최근 대화 세 개를 보여 모델이 고르게. 지난 일은 먼저 session.resume, 프롬프트의 예시는 지난 일이 아니라고
    표시 (4b 모델이 파일 찾기 예시를 "아까 한 일"로 따라 했음). M6.6: '!명령'만 있는 대화는 목록에 넣지 않는다."""
    home = tmp_path / "home"
    a, _ = open_app(home, FakeLLM([answer("A plan."), answer("Four."), answer("Done."), answer("Hello.")]),
                    os_name=osinfo.current_os())
    for request in ("plan the ablation study", "what is two plus two", "x" * 80, "say hello"):
        a.forget()
        a.ask_task(request)
    a.forget()
    a.set_cwd(str(tmp_path))
    a.run_direct("echo hello")                                                     # !명령만 있는 대화가 가장 최근
    a.close()

    b, _ = open_app(home)
    try:
        prompt = b.system_prompt()
        line = next(ln for ln in prompt.splitlines() if "Earlier conversations" in ln)
        listed = line.split("Earlier conversations (newest first): ", 1)[1]
        assert listed.count('" (1 task)') == 3 and "echo" not in listed                    # 명령만 있는 대화는 빼고
        assert listed.index('"say hello"') < listed.index('"xxx')
        assert '"what is two plus two" (1 task)' in listed and "ablation" not in listed      # 넷째는 빠진다
        assert '"' + "x" * 59 + '…"' in listed                                      # 긴 제목은 줄임
        assert 'Earlier work not in this conversation ("아까", "어제", "이어서"): call session.resume first.' in prompt
        assert "- Example only, not a request in this conversation (the first step after the plan)" in prompt
        assert "- Example only, not a request in this conversation:\nResearcher: LLM" in prompt
        assert "{example}" not in prompt
    finally:
        b.close()


def test_resume_query_keeps_the_researchers_words(tmp_path):
    """M6.6: "얼룩말 프로젝트 하던거 이어서 하자"에 4b 모델이 query를 "Equestria"로 바꿔 찾음. 저장된 말은 연구원님이
    쓴 그대로라, 찾기 낱말도 그대로 (계약의 영어 규칙 예외, 도구 설명)."""
    a, _ = open_app(tmp_path / "home")
    try:
        assert "- session.resume(query?): continue an earlier conversation (query: the Researcher's own words for " \
               "it; empty = latest)" in a.tools.describe()
        assert "tool arguments (except memory.note text and session.resume query)" in a.system_prompt()
    finally:
        a.close()


def test_prompt_rules_for_known_facts_statements_and_note_language(tmp_path):
    """M6.2: 이미 아는 것(시각·서버·메모)은 도구 없이, 메모는 연구원님 말 그대로.
    "말만 한 것에는 짧게" 규칙은 넣지 않는다 (연구원님: 하드코딩)."""
    a, _ = open_app(tmp_path / "home")
    try:
        prompt = a.system_prompt()
        assert "- If these instructions already say it (time, servers, notes), answer without tools." in prompt
        assert "only shares something" not in prompt
        assert "tool arguments (except memory.note text and session.resume query)" in prompt
        assert "in their own words and language" in a.tools.describe()
    finally:
        a.close()


@pytest.mark.parametrize("text, blocked", [
    ("Card password: 1234", True), ("내 카드 비밀번호는 1234", True), ("비번은 qwerty", True),
    ("wifi password is hunter2", True), ("my PIN 0000", True), ("API key는 abc123xyz", True),
    ("token: ghxyz12345", True), ("sk-abcdefghijklmnopqrstuvwxyz123", True), ("주민번호 900101-1234567", True),
    ("card 4111 1111 1111 1111", True),
    ("비밀번호 관리자를 쓴다", False), ("Answer in three lines or fewer.", False), ("서버 별칭은 temp", False),
    ("token budget is 4096 per call", False), ("max tokens 2048", False), ("OTP는 휴대폰으로 받음", False),
    ("password policy: rotate every 90 days", False), ("암호화 방식은 AES-256", False), ("lr 3e-4 works best", False),
])
def test_secrets_are_never_remembered(text, blocked):
    d = check(Policy(__import__("conftest").base_layers()), "linux", "memory.note", text=text)
    assert (d.verdict, d.rule) == (("deny", "코드:비밀기억") if blocked else ("ask", "코드:기억"))
    assert not d.grantable


def test_direct_commands_are_saved_too(tmp_path):
    a, _ = open_app(tmp_path / "home", os_name=osinfo.current_os())             # 명령은 이 컴퓨터의 셸로 (Windows는 PowerShell)
    try:
        a.set_cwd(str(tmp_path))
        a.run_direct("echo hello")
        turns = a.sessions.turns(a.hctx.session_id)
        assert [t.request for t in turns] == ["!echo hello"] and "hello" in turns[0].messages[0]["content"]
    finally:
        a.close()


def test_resume_by_asking_loads_after_the_task(tmp_path):
    home = tmp_path / "home"
    a, _ = open_app(home, FakeLLM([answer("Started EXP-03 on server-a."), answer("Two other runs finished.")]))
    a.ask_task("start the resnet run")
    old = a.hctx.session_id
    a.ask_task("anything else?")
    a.close()
    c, _ = open_app(home, FakeLLM([answer("Four.")]))
    c.ask_task("what is two plus two")                                           # 다른 세션 하나 더
    c.close()

    b, out = open_app(home)
    try:
        b.llm.steps = [answer("Hello.")]
        b.ask_task("hi")                                                         # 지금 세션에 턴 하나
        now = b.hctx.session_id
        seen = {}

        def turns_while_running(args):                                           # 도구가 도는 동안 기억은 그대로
            seen["turns"] = [t.request for t in b.memory.turns]
            return real(args)

        tool = b.tools.get("session.resume")
        real = tool.run
        tool.run = turns_while_running
        b.llm.steps = [act("session.resume", query="resnet EXP-03"), answer("We started EXP-03 on server-a.")]
        result = b.ask_task("let's continue the resnet work")
        assert result.status == "done" and seen["turns"] == ["hi"]
        observed = b.llm.calls[-1][0][-1]["content"]
        assert "first request: start the resnet run" in observed and "1. start the resnet run -> Started EXP-03" in observed
        assert b.hctx.session_id == old and b.hctx.resume_to == ""
        assert [t.request for t in b.memory.turns] == ["start the resnet run", "anything else?",
                                                       "let's continue the resnet work"]
        assert [t.request for t in b.sessions.turns(old)][-1] == "let's continue the resnet work"
        assert [t.request for t in b.sessions.turns(now)] == ["hi"]             # 하던 세션은 그대로 남는다
        assert "지난 대화를 이어 갑니다: " in out.getvalue() and "start the resnet run (작업 2개)" in out.getvalue()
        # 맞는 대화가 없으면 최근 목록과 함께 오류 (모델이 다시 묻거나 고른다)
        b.llm.steps = [act("session.resume", query="zebra"), answer("I could not find it.")]
        b.ask_task("continue the zebra project")
        observed = b.llm.calls[-1][0][-1]["content"]
        assert 'No earlier conversation matches "zebra". Recent ones: ' in observed and "what is two plus two" in observed
    finally:
        b.close()


def test_resume_tool_is_allowed_and_note_always_asks():
    policy = Policy([("기본", __import__("conftest").base_layers()[0][1]),
                     ("연구원님", {"memory.note": {"*": "allow"}})])
    assert check(policy, "linux", "session.resume", query="x").verdict == "allow"
    d = check(policy, "linux", "memory.note", text="answer briefly")
    assert (d.verdict, d.rule, d.grantable, d.key) == ("ask", "코드:기억", False, "policy.memory_note")
    ctx = make_ctx("linux", grants={("memory.note", "코드:기억")})                # "이 작업 동안"으로도 풀리지 않는다
    assert policy.check(ToolCall("memory.note", {"text": "x"}), ctx).verdict == "ask"
    blocked = Policy([("기본", __import__("conftest").base_layers()[0][1]), ("연구원님", {"memory.note": {"*": "deny"}})])
    assert check(blocked, "linux", "memory.note", text="x").verdict == "deny"


# ---------------------------------------------------------------- MEMORY.md
def test_notes_file(tmp_path):
    notes = Notes(tmp_path)
    assert notes.items() == [] and notes.prompt(600) == ""
    day = datetime(2026, 11, 11)
    assert notes.add("  - 실험 결과는 표로\n보여줘 ", today=day) == (1, "2026-11-11 · 실험 결과는 표로 보여줘", True)
    assert notes.add("실험 결과는 표로 보여줘", today=day)[2] is False                  # 같은 글은 한 번만
    notes.add("server-a is down until Friday", today=day)
    text = notes.path.read_text(encoding="utf-8")
    assert text.startswith("# GLaDOS 기억") and text.endswith("- 2026-11-11 · server-a is down until Friday\n")
    # 연구원님이 직접 고친 줄도 메모, 머리말과 다른 줄은 지울 때 그대로
    notes.path.write_text(text + "\n## 내 메모\n- 답은 짧게\n", encoding="utf-8")
    assert notes.items()[-1] == "답은 짧게" and len(notes.items()) == 3
    assert notes.remove(2) == "2026-11-11 · server-a is down until Friday"
    assert "## 내 메모" in notes.path.read_text(encoding="utf-8") and notes.remove(9) is None
    assert notes.prompt(600) == "- 2026-11-11 · 실험 결과는 표로 보여줘\n- 답은 짧게"
    for i in range(40):
        notes.add(f"note number {i} about something long enough", today=day)
    short = notes.prompt(600)
    assert short.startswith("(") and "older notes left out" in short and short.endswith("long enough")
    assert len(short) <= 640 and "note number 39" in short and "note number 0 " not in short
    assert (budget(16384), budget(32768), budget(65536)) == (500, 1500, 3000)
    # 손으로 쓴 줄 (M6.2): "- " 없어도 메모, 제목·주석(여러 줄)·빈 줄은 아님. BOM·끝 줄바꿈 없음도
    hand = Notes(tmp_path / "hand")
    hand.path.parent.mkdir()
    hand.path.write_text("\ufeff# GLaDOS 기억\n\n<!-- 설명\n두 줄 -->\n\n- 2026-11-11 · Answer in three lines or fewer.\n"
                         "- 2026-11-11 · Card password: 1234\n* 별표 줄\n1. 번호 줄\n서버 별칭은 temp", encoding="utf-8")
    assert hand.items() == ["2026-11-11 · Answer in three lines or fewer.", "2026-11-11 · Card password: 1234",
                            "별표 줄", "번호 줄", "서버 별칭은 temp"]
    assert "Card password" not in hand.prompt(500) and "- 서버 별칭은 temp" in hand.prompt(500)   # 비밀은 모델에게 안 감
    assert hand.add("서버 별칭은 temp")[2] is False
    assert hand.add("새 메모", today=day) == (6, "2026-11-11 · 새 메모", True)
    assert hand.path.read_text(encoding="utf-8").endswith("서버 별칭은 temp\n- 2026-11-11 · 새 메모\n")
    assert hand.remove(5) == "서버 별칭은 temp" and hand.items()[-1] == "2026-11-11 · 새 메모"
    assert "<!-- 설명\n두 줄 -->" in hand.path.read_text(encoding="utf-8")
    assert len(Notes(tmp_path).add("x" * 500)[1]) <= 13 + 300


def test_remember_this_with_approval(tmp_path):
    asked = []

    def approve(call, d):
        asked.append((call.tool, d.key, d.grantable, list(call.card)))
        return "once" if len(asked) == 1 else "deny"

    a, out = open_app(tmp_path / "home", approve=approve)
    try:
        a.llm.steps = [act("memory.note", text="Show experiment results as a table"), answer("Noted.")]
        a.ask_task("remember: show experiment results as a table")
        tool, key, grantable, card = asked[0]
        assert (tool, key, grantable) == ("memory.note", "policy.memory_note", False)
        assert card[0] == ("fact", "Show experiment results as a table") and "MEMORY.md" in card[1][1]
        assert a.notes.items()[0].endswith("· Show experiment results as a table")
        assert "Saved as note 1 in MEMORY.md" in a.llm.calls[-1][0][-1]["content"]
        # 다음 작업의 시스템 프롬프트에 들어간다
        a.llm.steps = [answer("Sure.")]
        a.ask_task("hello")
        system = a.llm.calls[-1][0][0]["content"]
        assert "Notes the Researcher asked you to keep (already loaded; never search for MEMORY.md; memory.note " \
               "skips repeats):\n- " in system
        assert "Show experiment results as a table" in system
        # 거절하면 저장하지 않고, 모델에게 저장하지 말라고 알린다
        a.llm.steps = [act("memory.note", text="My birthday is in March"), answer("Not saved.")]
        a.ask_task("remember that my birthday is in March")
        assert len(a.notes.items()) == 1 and "did not want this saved" in a.llm.calls[-1][0][-1]["content"]
        # 비밀은 승인 상자도 없이 막는다 (M6.2: 카드 비밀번호를 승인해 저장했던 일)
        a.llm.steps = [act("memory.note", text="Card password: 1234"), answer("Not saved.")]
        a.ask_task("remember my card password is 1234")
        blocked = a.llm.calls[-1][0][-1]["content"]
        assert "Blocked: Passwords, PINs, card numbers, keys and tokens are never saved" in blocked
        assert "Do not save it anywhere else" in blocked and "password manager" in blocked
        assert len(asked) == 2 and len(a.notes.items()) == 1                    # 묻지 않았고 저장도 안 됨
        assert "비밀기억" in (a.cfg.home / "logs" / "policy.jsonl").read_text(encoding="utf-8")
        # /memory, /memory del
        a.command("/memory")
        assert "기억한 것 (" in out.getvalue() and "1. " in out.getvalue() and "/memory del 번호" in out.getvalue()
        a.command("/memory del 3")
        assert "목록의 번호(1~1)" in out.getvalue()
        a.command("/memory del 1")
        assert "지웠습니다: " in out.getvalue() and a.notes.items() == []
        assert "Notes the Researcher" not in a.system_prompt()
    finally:
        a.close()


def test_plain_start_mentions_the_last_conversation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    a, _ = open_app(home, FakeLLM([answer("Done.")]))
    a.ask_task("plan the ablation study")
    a.close()
    b, out = open_app(home)
    inputs = iter(["/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    try:
        b.repl()
        assert "지난 대화: " in out.getvalue() and "plan the ablation study (작업 1개). /resume 1 로 이어 갑니다." in out.getvalue()
    finally:
        b.close()


@pytest.mark.parametrize("num_ctx", [32768])
def test_prompt_budget_at_32k_with_notes(tmp_path, num_ctx):
    """32k(M7.4부터 집 컴퓨터 설정)에서 메모가 가득 차도 지난 대화 몫이 넉넉히 남는다."""
    home = tmp_path / "home"
    ensure_home(home)
    (home / "servers.toml").write_text('[[server]]\nalias = "server-a"\nworkdir = "/home/gildong/work"\n', encoding="utf-8")
    cfg = load_config(home)
    cfg.model.num_ctx = num_ctx
    a = cli.App(cfg, cli.Screen(ansi=False, out=io.StringIO()), llm=FakeLLM([]), os_name="windows")
    try:
        empty = a.history_budget()
        for i in range(30):
            a.notes.add(f"note {i}: keep the batch size at 32 for the resnet runs")
        full = a.history_budget()
        assert empty >= 20000 and 0 < empty - full <= 2500 and full > 18000     # M8.3 실험 규칙까지 넣은 크기 (16k는 이제 쓰지 않음)
    finally:
        a.close()

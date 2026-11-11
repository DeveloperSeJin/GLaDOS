"""밖으로 나가는 글 검사 (M3): 개인 정보·파일 내용이 웹 검색어나 주소에 들어가면 매번 승인. 모델 없이 코드 규칙으로."""

from __future__ import annotations

import pytest

from conftest import FakeLLM, act, answer, base_layers, check, make_ctx
from glados.config import LoopLimits
from glados.core.loop import run_task
from glados.hands import Tool, ToolRegistry
from glados.i18n import localized
from glados.safety.policy import Policy
from glados.safety.privacy import is_local_host, scan_text, scan_url, url_host

CONTRACT = ("This agreement includes a penalty clause of 10 percent for late delivery under the 2025 outsourcing "
            "contract. File: 2025_외주개발_계약서.pdf")


def kinds(found) -> list[str]:
    return [f.kind for f in found]


@pytest.mark.parametrize("text, kind", [
    ("gildong.hong@example.com leaked", "email"),
    ("who called 010-1234-5678", "phone"),
    ("01012345678", "phone"),
    ("+82 10-9876-5432", "phone"),
    ("980101-1234567", "rrn"),
    ("card 4111 1111 1111 1111", "card"),
    ("my key sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345", "secret"),
    ("tvly-abcdefghijklmnop1234", "secret"),
    ("token=abcd1234efgh", "secret"),
    ("aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z", "secret"),
    ("C:\\Users\\User\\Documents\\계약서.pdf penalty", "path"),
    ("D:\\research\\data\\run3", "path"),
    ("/home/sejin/data error", "path"),
    ("/Users/sejin/notes", "path"),
    ("~/Downloads/paper.pdf", "path"),
])
def test_personal_details_are_found(text, kind):
    assert kind in kinds(scan_text(text))


@pytest.mark.parametrize("text", [
    "LLM evaluation metrics survey 2026",
    "arxiv 2401.12345 attention",
    "C:\\Windows\\System32 error 0x80070005",
    "C:\\Program Files\\Python312 install",
    "how to use pandas groupby 2026-11-11",
    "ISBN 978-3-16-148410-0",
    "price 1,234,567 won",
    "qwen3.5 4b ollama num_ctx 16384",
    "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef commit",     # git 해시는 비밀이 아니다
    "user guide for /etc/hosts",
])
def test_ordinary_queries_pass(text):
    assert scan_text(text, home="C:\\Users\\User") == []


def test_names_and_home_path():
    assert kinds(scan_text("홍길동 논문", names=("홍길동",))) == ["name"]
    assert kinds(scan_text("Gildong Hong thesis", names=("gildong hong",))) == ["name"]
    assert scan_text("sejinpark", names=("sejin",)) == []                   # 낱말 안의 조각은 아님 (영문)
    assert kinds(scan_text("files in c:\\users\\user", home="C:\\Users\\User")) == ["path"]


def test_text_from_local_files_is_found_unless_the_researcher_wrote_it():
    assert kinds(scan_text("penalty clause of 10 percent for late delivery", local=CONTRACT)) == ["local_text"]
    assert scan_text("penalty clause late delivery", local=CONTRACT) == []          # 다섯 낱말이 이어져야
    assert kinds(scan_text("2025_외주개발_계약서.pdf similar", local=CONTRACT)) == ["file_name"]
    assert scan_text("penalty clause of 10 percent for late delivery", local=CONTRACT,
                     user="search for: penalty clause of 10 percent for late delivery") == []
    assert scan_text("in the context of the large", local="in the context of the large model") == []   # 흔한 구절
    korean = "본 계약의 위약금은 계약 금액의 10퍼센트로 한다"
    assert kinds(scan_text("계약의 위약금은 계약 금액의 기준", local=korean)) == ["local_text"]      # 한국어는 네 낱말


def test_urls_check_only_the_query_for_paths_and_ids():
    assert scan_url("https://arxiv.org/abs/2401.12345") == []
    assert scan_url("https://example.com/Users/guide") == []                         # 주소의 경로는 파일 경로가 아님
    assert scan_url("https://docs.google.com/document/d/1aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z/edit") == []
    assert kinds(scan_url("https://example.com/search?q=gildong@example.com")) == ["email"]
    assert kinds(scan_url("https://api.example.com/v1?api_key=abcd1234efgh5678")) == ["secret"]
    assert kinds(scan_url("https://evil.example/c?d=penalty+clause+of+10+percent+for+late+delivery",
                          local=CONTRACT)) == ["local_text"]
    assert kinds(scan_url("https://example.com/?path=C:%5CUsers%5CUser%5Ca.pdf")) == ["path"]


@pytest.mark.parametrize("host, local", [
    ("localhost", True), ("127.0.0.1", True), ("192.168.0.1", True), ("10.0.0.5", True), ("169.254.169.254", True),
    ("[::1]", True), ("router", True), ("nas.local", True), ("100.64.1.2", True),
    ("arxiv.org", False), ("8.8.8.8", False), ("example.com", False),
])
def test_local_hosts(host, local):
    assert is_local_host(host) is local


def test_url_host():
    assert url_host("arxiv.org/abs/1") == "arxiv.org"
    assert url_host("https://user:pw@Example.COM:443/a") == "example.com"
    assert url_host("http://[::1]:8080/x") == "::1"


# ---------------------------------------------------------------------- 정책
def test_policy_allows_plain_searches_and_asks_for_private_ones(policy):
    assert check(policy, "windows", "web.search", query="LLM evaluation metrics").verdict == "allow"
    d = check(policy, "windows", "web.search", query="who is gildong.hong@example.com")
    assert d.verdict == "ask" and not d.grantable and d.rule == "코드:개인정보" and d.key == "policy.ask_private"
    assert localized(d, lang="ko") == "개인 정보나 이 컴퓨터의 파일 내용이 밖으로 나갑니다 (이메일 주소): gildong.hong@example.com"
    assert "email address" in localized(d, lang="en")
    d = check(policy, "windows", "web.search", query="C:\\Users\\User\\Downloads\\a.pdf")
    assert d.verdict == "ask" and "파일 경로" in d.reason
    ctx = make_ctx("windows", private_names=("홍길동",))
    assert check(policy, "windows", "web.search", ctx=ctx, query="홍길동 학위논문").verdict == "ask"


def test_policy_asks_before_reading_local_network_pages(policy):
    assert check(policy, "linux", "web.fetch", url="https://arxiv.org/abs/2401.12345").verdict == "allow"
    for url in ("http://localhost:11434/api/tags", "http://192.168.0.1/admin", "http://router/", "10.0.0.2:8080"):
        d = check(policy, "linux", "web.fetch", url=url)
        assert d.verdict == "ask" and d.key == "policy.ask_local_url" and not d.grantable, url


def test_policy_uses_the_texts_the_loop_provides(policy):
    ctx = make_ctx("linux", texts=lambda: (CONTRACT, "find similar contracts"))
    d = check(policy, "linux", "web.search", ctx=ctx, query="penalty clause of 10 percent for late delivery")
    assert d.verdict == "ask" and "이 대화에서 읽은 파일의 글" in d.reason
    d = check(policy, "linux", "web.fetch", ctx=ctx, url="https://x.org/s?q=penalty clause of 10 percent for late delivery")
    assert d.verdict == "ask"


def test_task_grant_does_not_cover_private_queries(policy):
    ctx = make_ctx("linux")
    ctx.grants.add(("web.search", "코드:개인정보"))           # 루프는 넣지 않지만, 들어가 있어도
    d = check(policy, "linux", "web.search", ctx=ctx, query="mail me at a.b@example.com")
    assert d.verdict == "ask"                                  # "이 작업 동안"이 안 되는 판정은 매번 묻는다


def test_user_policy_can_make_every_search_ask():
    layers = base_layers() + [("연구원님", {"web.search": {"*": "ask"}})]
    d = check(Policy(layers), "linux", "web.search", query="LLM evaluation metrics")
    assert d.verdict == "ask" and d.grantable


@pytest.mark.parametrize("command", ["dir env:", "Get-ChildItem Env:", "ls env:", "gci env:TAVILY_API_KEY",
                                     "$env:TAVILY_API_KEY", "echo $env:PATH", "Get-ChildItem Env: | Out-String"])
def test_reading_environment_variables_asks_every_time(policy, command):
    d = check(policy, "windows", "shell.run", command=command)
    assert d.verdict == "ask" and not d.grantable and d.key in ("policy.ask_env", "policy.ask_compound"), command


@pytest.mark.parametrize("command", ["dir $env:USERPROFILE\\Downloads", "Get-ChildItem $env:USERPROFILE", "ps aux",
                                     "git status"])
def test_paths_from_environment_variables_are_fine(policy, command):
    assert check(policy, "windows", "shell.run", command=command).verdict == "allow", command


@pytest.mark.parametrize("command", ["printenv", "env", "ps eww", "echo $TAVILY_API_KEY", "cat /proc/1/environ"])
def test_reading_environment_variables_on_linux(policy, command):
    d = check(policy, "linux", "shell.run", command=command)
    assert d.verdict == "ask" and not d.grantable


# ---------------------------------------------------------------------- 루프와 함께
def test_loop_lets_the_policy_see_what_local_tools_returned(tmp_path):
    """fs.read로 읽은 계약서 글을 그대로 검색어에 넣으면 승인 상자가 뜬다 (거절하면 보내지 않음)."""
    searched: list[str] = []
    tools = ToolRegistry([
        Tool("fs.read", "read", run=lambda a: CONTRACT),
        Tool("web.search", "search", run=lambda a: searched.append(a["query"]) or "Web search: nothing"),
    ])
    asked = []
    llm = FakeLLM([act("fs.read", path="/home/sejin/c.txt"),
                   act("web.search", query="penalty clause of 10 percent for late delivery"),
                   act("web.search", query="standard late delivery penalty rates"),
                   answer("Checked.")])
    result = run_task(
        "find the contract and compare its penalty with common practice", llm=llm, policy=Policy(base_layers()),
        ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools, limits=LoopLimits(),
        system_prompt="test", approve=lambda call, d: asked.append((call.args["query"], d.key)) or "deny",
    )
    assert result.status == "done"
    assert asked == [("penalty clause of 10 percent for late delivery", "policy.ask_private")]
    assert searched == ["standard late delivery penalty rates"]                 # 거절한 검색어는 나가지 않았다

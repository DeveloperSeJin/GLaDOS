"""v0 시나리오 1~7 (v0-brief 2장): 처음부터 끝까지, 연구원님이 쓰는 앱(cli.App)으로.

모델만 가짜다 (정해 둔 걸음을 돌려주고, 필요하면 도구 결과를 보고 답을 만든다). 나머지는 진짜다:
정책, 승인, 손, 작업 기록, 번역 경로, 세션 저장. 파일은 가짜 홈, 웹은 가짜 응답(httpx MockTransport),
서버는 가짜 SSH 서버(test_remote.FakeServer)와 진짜 tmux. 인터넷·진짜 서버·진짜 홈은 쓰지 않는다.

완료 기준 1: 답에 출처·세션 이름·커밋이 들어간다. 완료 기준 4: 호출·판정·승인이 logs/에 남는다.
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import FakeLLM, StaticGlossary, act, answer
from test_fs import make_pdf
from glados import cli, notify
from glados.config import ensure_home, load_config
from glados.hands import web


# ---------------------------------------------------------------- 공통
class ScriptLLM(FakeLLM):
    """FakeLLM + 함수 걸음: 함수는 보낸 메시지를 받아 걸음을 돌려준다 (도구 결과의 세션 이름·커밋을 답에 넣을 때)."""

    def chat_json(self, messages, schema, validate, retries=2, temperature=0.2):
        if self.steps and callable(self.steps[0]):
            self.steps[0] = self.steps[0](messages)
        return super().chat_json(messages, schema, validate, retries, temperature)


def ko(fact: str, wit: str = "") -> dict[str, str]:
    """번역 호출에 돌려줄 한국어."""
    return {"fact": fact, "wit": wit}


def last_result(messages: list[dict[str, str]]) -> str:
    return next(m["content"] for m in reversed(messages) if m["content"].startswith("[result"))


class Lab:
    """가짜 홈 (다운로드·문서), GLADOS_HOME, 앱을 여는 법."""

    def __init__(self, root: Path):
        self.home = root / "home"
        self.gh = self.home / ".glados"
        self.work = self.home / "work"
        (self.home / "Downloads" / "계약").mkdir(parents=True)
        (self.home / "Documents").mkdir()
        self.work.mkdir()
        self.contract = self.home / "Downloads" / "계약" / "2025_외주개발_계약서.pdf"
        self.contract.write_bytes(make_pdf(["Outsourcing agreement", "Penalty clause: 10 percent of the fee per month of delay"]))
        (self.home / "Downloads" / "2024_임대_계약서.pdf").write_bytes(make_pdf(["Lease agreement", "No penalty."]))
        self.paper = self.home / "Documents" / "학위논문.pdf"
        self.paper.write_bytes(make_pdf(["Abstract: we compare three optimizers on CIFAR-10.",
                                         "Result: AdamW reaches 94.1 percent, SGD 93.2 percent."]))
        ensure_home(self.gh)
        self.asked: list[tuple[str, str, list]] = []
        self.answers: list[str] = []          # 승인 상자에서 고를 것 (차례대로, 없으면 거절)

    def approve(self, call, d) -> str:
        self.asked.append((call.tool, d.key, list(call.card)))
        return self.answers.pop(0) if self.answers else "deny"

    def open(self, steps: list[Any], servers: str = "", search: str = "") -> tuple[cli.App, io.StringIO]:
        if servers:
            (self.gh / "servers.toml").write_text(servers, encoding="utf-8")
        cfg = load_config(self.gh)
        if search:
            cfg.web.search = search
        out = io.StringIO()
        a = cli.App(cfg, cli.Screen(ansi=False, out=out), llm=ScriptLLM(steps), os_name="linux")
        a.hctx.glossary = StaticGlossary()                  # 파일 이름의 한국어 낱말 -> 영어 (모델 대신)
        a.set_cwd(str(self.work))
        a.approve = self.approve
        return a, out

    def logged(self, tool: str) -> list[dict]:
        lines = (self.gh / "logs" / "policy.jsonl").read_text(encoding="utf-8").splitlines()
        return [r for r in map(json.loads, lines) if r["tool"] == tool]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("the fake home uses HOME (Linux and macOS)")
    lab = Lab(tmp_path)
    monkeypatch.setenv("HOME", str(lab.home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GLADOS_HOME", raising=False)
    return lab


# ---------------------------------------------------------------- 1. 계약서 위약금 (fs.search, fs.read)
def test_scenario_1_contract_penalty_clause(lab):
    pdf = str(lab.contract)
    a, out = lab.open([
        act("fs.search", query="contract", kind="pdf", roots=["~/Downloads"]),
        act("fs.read", path=pdf),
        answer("Only 2025_외주개발_계약서.pdf has a penalty clause: 10 percent of the fee per month of delay.",
               "The 2024 lease is penalty-free. Rare restraint.", [pdf]),
        ko("위약금 조항은 2025_외주개발_계약서.pdf에만 있습니다: 지연 한 달마다 대금의 10%.",
           "2024년 임대 계약서에는 위약금이 없습니다. 드문 절제입니다."),
    ])
    try:
        result = a.ask_task("다운로드 폴더에서 계약서 PDF 찾아서 위약금 조항만 정리해줘")
        assert result.status == "done" and result.sources == [pdf]
        text = out.getvalue()
        assert "위약금 조항은 2025_외주개발_계약서.pdf에만 있습니다" in text and f"출처: {pdf}" in text
        found = a.llm.calls[1][0][-1]["content"]
        assert "2 found" in found and "2025_외주개발_계약서.pdf" in found and "2024_임대_계약서.pdf" in found
        assert "Penalty clause: 10 percent" in a.llm.calls[2][0][-1]["content"]
        assert lab.asked == []                                                  # 찾기·읽기는 묻지 않는다
        assert [r["verdict"] for r in lab.logged("fs.search") + lab.logged("fs.read")] == ["allow", "allow"]
        assert "다운로드 폴더에서 계약서" in (lab.gh / "logs" / "tasks.jsonl").read_text(encoding="utf-8")
    finally:
        a.close()


# ---------------------------------------------------------------- 2. 논문 요약 저장 (fs.read, fs.write_new)
def test_scenario_2_summarize_paper_into_markdown(lab):
    paper = str(lab.paper)
    before = lab.paper.read_bytes()
    summary = "# 학위논문 요약\n\n- 세 옵티마이저를 CIFAR-10에서 비교\n- AdamW 94.1%, SGD 93.2%\n"
    a, out = lab.open([
        act("fs.read", path=paper),
        act("fs.write_new", path="~/Documents/학위논문_요약.md", content=summary),
        answer("Saved the summary to ~/Documents/학위논문_요약.md. AdamW 94.1%, SGD 93.2% on CIFAR-10.", "",
               [paper]),
        ko("요약을 ~/Documents/학위논문_요약.md에 저장했습니다. CIFAR-10에서 AdamW 94.1%, SGD 93.2%입니다."),
    ])
    try:
        result = a.ask_task(f"{paper} 이 논문 PDF 요약해서 md로 저장해줘")
        assert result.status == "done"
        saved = lab.home / "Documents" / "학위논문_요약.md"
        assert saved.read_text(encoding="utf-8") == summary
        assert lab.paper.read_bytes() == before                                 # 원본은 그대로
        assert "학위논문_요약.md에 저장했습니다" in out.getvalue() and f"출처: {paper}" in out.getvalue()
        assert lab.asked == [] and lab.logged("fs.write_new")[0]["verdict"] == "allow"
    finally:
        a.close()


# ---------------------------------------------------------------- 3. 웹 조사 (web.search, web.fetch)
PAPERS = {
    "https://arxiv.org/abs/2610.00001": ("A Survey of LLM Evaluation Metrics", "We survey 42 metrics for LLMs."),
    "https://arxiv.org/abs/2609.00002": ("Judging the Judges", "LLM-as-a-judge agrees with humans 81 percent."),
    "https://arxiv.org/abs/2608.00003": ("FactScore Revisited", "Atomic fact scoring for long answers."),
}


@pytest.fixture
def fake_web(monkeypatch):
    """검색 서비스(Tavily)와 논문 페이지를 가짜로. 보낸 요청을 남긴다."""
    sent: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        sent.append((request.method, url))
        if url == "https://api.tavily.com/search":
            assert request.headers["authorization"] == "Bearer tvly-test"
            results = [{"title": t, "url": u, "content": s, "published_date": "2026-10-02"}
                       for u, (t, s) in PAPERS.items()]
            return httpx.Response(200, json={"query": json.loads(request.content)["query"], "results": results})
        if url in PAPERS:
            title, text = PAPERS[url]
            body = f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1><p>{text}</p></article></body></html>"
            return httpx.Response(200, html=body)
        return httpx.Response(404, text="no")

    real = httpx.Client

    def client(*args, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        kw["trust_env"] = False                          # 프록시 설정을 타지 않게
        return real(*args, **kw)

    monkeypatch.setattr(web.httpx, "Client", client)
    monkeypatch.setattr(web, "_resolves_local", lambda host: False)
    monkeypatch.setattr(web, "POLITE_S", 0.0)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    return sent


def test_scenario_3_recent_papers_from_the_web(lab, fake_web):
    urls = list(PAPERS)
    a, out = lab.open([
        act("web.search", query="LLM evaluation metrics papers", time_range="year"),
        *[act("web.fetch", url=u) for u in urls],
        answer("Three recent papers: a survey of 42 metrics; LLM-as-a-judge agrees with humans 81%; "
               "atomic fact scoring for long answers.", "", urls),
        ko("최신 논문 3개: 지표 42개 정리 서베이, LLM 심판이 사람과 81% 일치, 긴 답의 사실 단위 채점."),
    ], search="tavily")
    try:
        result = a.ask_task("LLM 평가 지표 최신 논문 3개 찾아서 요약해줘")
        assert result.status == "done" and result.sources == urls
        text = out.getvalue()
        assert "최신 논문 3개" in text and "출처: " + ", ".join(urls) in text
        assert '↗ 외부 전송 · api.tavily.com · 검색어 "LLM evaluation metrics papers (past year)"' in text   # 밖으로 나간 것을 표시
        assert f"↗ 외부 전송 · arxiv.org · 페이지 읽기 {urls[0]}" in text
        assert [u for _, u in fake_web] == ["https://api.tavily.com/search", *urls]
        mark = len(out.getvalue())
        a.command("/sent")
        sent = out.getvalue()[mark:]
        assert "LLM evaluation metrics papers" in sent and all(u in sent for u in urls)
        assert lab.asked == []
    finally:
        a.close()


# ---------------------------------------------------------------- 4~6. 공유 GPU 서버
asyncssh = pytest.importorskip("asyncssh")
from test_remote import FakeServer, ME                                     # noqa: E402
from glados.hands import remote                                            # noqa: E402


@pytest.fixture
def server(tmp_path):
    import subprocess

    s = FakeServer(tmp_path / "srv")
    proj = s.work / "proj"
    proj.mkdir()
    (proj / "train.py").write_text(
        "import sys\nlr = sys.argv[sys.argv.index('--lr') + 1]\n"
        "for e in range(1, 4):\n    print(f'epoch {e} lr {lr} loss {1.0 / e:.3f}')\n")
    git = ["git", "-c", "user.name=GLaDOS Test", "-c", "user.email=test@example.com"]
    subprocess.run(git + ["init", "-q", "-b", "main"], cwd=proj, check=True)
    subprocess.run(git + ["add", "."], cwd=proj, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "first"], cwd=proj, check=True)
    (proj / "notes.txt").write_text("try lr 3e-4\n")                                # 커밋 안 된 파일
    yield s
    s.close()


def server_app(lab: Lab, server: FakeServer, steps: list[Any]) -> tuple[cli.App, io.StringIO]:
    a, out = lab.open(steps, servers=f'[[server]]\nalias = "server-a"\ngpus = 4\nworkdir = "{server.work}"\n'
                                     f'tmux_prefix = "glados-test"\n')
    a.hctx.hub.options.update({"port": server.port, "username": ME, "known_hosts": server.known_hosts(),
                               "client_keys": [server.client_key], "agent_path": None, "config": []})
    a.hctx.hub.hosts["server-a"] = "127.0.0.1"
    a.runner.llm = None                     # 메모리를 말하지 않았으면 코드로 추정하는 모델 호출은 이 시험에서 끈다
    a.runner.on_notice = a.notice           # 배경 감시 스레드 대신 시험이 tick()을 부른다
    remote.sample_gpus(a.hctx.hub, a.hctx.gpu_watch, "server-a", time.time() - 360)   # 6분 전부터 봤다 (5분 규칙)
    return a, out


def test_scenario_4_is_the_gpu_free(lab, server):
    a, out = server_app(lab, server, [
        act("remote.gpu", server="server-a"),
        answer("server-a: GPU 1 is free (checked for 6 min). GPU 0 is used by someone else, GPU 2 is yours, "
               "GPU 3 is busy."),
        ko("server-a는 GPU 1이 비어 있습니다 (6분 동안 확인). GPU 0은 다른 분이, GPU 2는 연구원님이 쓰고 있고, GPU 3은 바쁩니다."),
    ])
    try:
        a.ask_task("server-a GPU 비었어?")
        seen = a.llm.calls[1][0][-1]["content"]
        assert "- GPU 1 (RTX A6000): free (free for 6 min)" in seen and "Free: 1 (GPU 1)" in seen
        assert "kim" not in seen and "secret_train" not in seen                  # 다른 사람 정보는 모델에게도 안 간다
        assert "server-a는 GPU 1이 비어 있습니다" in out.getvalue() and lab.asked == []
    finally:
        a.close()


def test_scenario_5_and_6_run_watch_and_report(lab, server, monkeypatch):
    monkeypatch.setattr(notify, "os_notify", lambda *args: None)

    def started(messages):
        obs = last_result(messages)
        session = re.search(r"in tmux session (\S+)", obs).group(1)
        commit = re.search(r"Commit (\w+) on main", obs).group(1)
        return answer(f"EXP-01 started on server-a GPU 1 in tmux session {session}, commit {commit} "
                      f"(1 uncommitted file saved in diff.patch).", "")

    def started_ko(messages):
        fact = json.loads(messages[-1]["content"])["fact"]
        session = re.search(r"tmux session (\S+),", fact).group(1)
        commit = re.search(r"commit (\w+) ", fact).group(1)
        return ko(f"EXP-01을 server-a GPU 1에서 시작했습니다. tmux 세션 {session}, 커밋 {commit} "
                  f"(커밋 안 된 파일 1개는 diff.patch로 저장).")

    command = "python train.py --lr 3e-4"
    a, out = server_app(lab, server, [
        act("remote.run", server="server-a", cwd="proj", command=command),
        started, started_ko,
    ])
    try:
        lab.answers = ["once"]
        result = a.ask_task(f"server-a에서 `{command}` 돌려줘")
        assert result.status == "done"
        tool, key, card = lab.asked[0]
        card_text = "\n".join(line for _, line in card)
        assert (tool, key) == ("remote.run", "policy.remote_run")                # "이 명령어가 맞습니까?" + 실행 카드
        assert card[0][1].startswith("EXP-01 · server-a · GPU 1") and f"명령  {command}" in card_text
        assert "tmux  glados-test-exp01" in card_text
        run = a.runner.store.get("EXP-01")
        text = out.getvalue()
        assert run.session == "glados-test-exp01" and run.commit and run.gpus == [1]
        assert f"tmux 세션 glados-test-exp01, 커밋 {run.commit}" in text           # 세션 이름·커밋이 답에
        folder = server.work / ".glados" / "runs" / "EXP-01"
        assert "notes.txt" in (folder / "untracked.txt").read_text()
        assert lab.logged("remote.run")[0]["decided_by"] == "연구원님:이번만"

        # 6. 끝나면 알림 (배경 감시 한 번), "EXP-01 어때?"
        end = time.monotonic() + 20
        while not (folder / "exit").exists() and time.monotonic() < end:
            time.sleep(0.1)
        a.runner.tick()
        text = out.getvalue()
        assert "알림  EXP-01 끝남: server-a, 종료 코드 0" in text
        a.llm.steps = [act("remote.log", server="server-a", run="EXP-01"),
                       answer("EXP-01 finished with exit code 0 after 3 epochs; loss fell from 1.000 to 0.333."),
                       ko("EXP-01은 종료 코드 0으로 끝났습니다. 3 epoch 동안 loss가 1.000에서 0.333으로 내려갔습니다.")]
        a.ask_task("EXP-01 어때?")
        status = a.llm.calls[-2][0][-1]["content"]
        assert status.startswith("[result: remote.log] EXP-01 on server-a: finished at ") and "exit code 0" in status
        assert "epoch 3 lr 3e-4 loss 0.333" in status
        assert "loss가 1.000에서 0.333으로" in out.getvalue()
        a.command("/runs")
        assert "EXP-01  끝남 · server-a GPU 1" in out.getvalue()
    finally:
        a.close()


# ---------------------------------------------------------------- 7. 어제 하던 거 이어서 (세션)
def test_scenario_7_continue_yesterday(lab):
    a, _ = lab.open([
        answer("Plan: sweep lr over 1e-4, 3e-4, 1e-3 with seed 0, then compare dev acc."),
        ko("계획: seed 0으로 lr 1e-4, 3e-4, 1e-3을 돌리고 dev acc를 비교합니다."),
    ])
    a.ask_task("lr 실험 계획 세워줘")
    yesterday = a.hctx.session_id
    a.close()

    b, out = lab.open([
        act("session.resume"),
        answer("Continuing the lr sweep plan: 1e-4, 3e-4, 1e-3 with seed 0. Next is starting the three runs."),
        ko("lr 실험 계획을 이어 갑니다: seed 0으로 1e-4, 3e-4, 1e-3. 다음은 세 실행을 시작하는 것입니다."),
    ])
    try:
        assert 'Earlier conversations (newest first): ' in b.system_prompt()
        assert '"lr 실험 계획 세워줘" (1 task).' in b.system_prompt()
        b.ask_task("어제 하던 거 이어서 하자")
        recap = b.llm.calls[1][0][-1]["content"]
        assert "first request: lr 실험 계획 세워줘" in recap and "Plan: sweep lr" in recap
        assert b.hctx.session_id == yesterday
        assert [t.request for t in b.memory.turns] == ["lr 실험 계획 세워줘", "어제 하던 거 이어서 하자"]
        assert "지난 대화를 이어 갑니다: " in out.getvalue() and "lr 실험 계획을 이어 갑니다" in out.getvalue()
        assert lab.logged("session.resume")[0]["verdict"] == "allow"
    finally:
        b.close()

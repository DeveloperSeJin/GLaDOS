"""대화 기억 시험: 지난 작업을 통째로 기억하고, 길이에 맞게 줄인다."""

from __future__ import annotations

from conftest import FakeLLM, act, answer, base_layers, make_ctx, verdict
from glados.config import LoopLimits
from glados.core import context as cx
from glados.core.context import BRIEF, CLIPPED, DROPPED, FULL, Conversation, Turn
from glados.core.checker import Checker
from glados.core.loop import TaskResult, run_task
from glados.core.schema import Answer
from glados.hands import Tool, ToolRegistry
from glados.safety.policy import Policy

PAPER = "/home/sejin/Downloads/학위논문.pdf"
FOUND = (f"2 found (scope: /home/sejin/Downloads)\n- {PAPER} · 2.1MB · 2026-10-02 · en: thesis, paper\n"
         "- /home/sejin/Downloads/요약.pdf · 90KB · 2026-09-01 · en: summary")
BODY = "This study turns EEG signals into sentences. " * 60          # 약 2,700자


def obs(tool: str, text: str) -> dict[str, str]:
    return cx.observation(tool, text)


def step(text: str) -> dict[str, str]:
    return {"role": "assistant", "content": text}


def done_result(transcript, trail=(), fact="Here is the answer.", sources=(), status="done", message=""):
    ans = Answer(fact=fact, wit="") if fact else None
    return TaskResult(status, ans, list(sources), len(transcript), "", message, list(trail), list(transcript))


def read_turn(request="학위논문 읽어줘") -> TaskResult:
    return done_result(
        [step('{"tool": "fs.read"}'), obs("fs.read", f"[{PAPER}] 2700 chars total\n{BODY}"), step('{"done": true}')],
        trail=[{"tool": "fs.read", "args": {"path": PAPER}, "head": f"[{PAPER}] 2700 chars total\n{BODY}"[:600]}],
        fact="It is an EEG study.", sources=[PAPER],
    )


# ---------------------------------------------------------------- 길이 어림
def test_limit_chars_by_machine():
    assert cx.limit_chars(16384) == int((16384 - 2048) * 1.3)      # 집 Windows (낮은 모드)
    assert cx.limit_chars(32768) == int((32768 - 2048) * 1.3)      # 연구실·맥
    assert cx.size([{"role": "user", "content": "가" * 100}]) == 100 + cx.MESSAGE_OVERHEAD


# ---------------------------------------------------------------- 턴
def test_turn_renders_three_levels():
    turn = Turn.from_result("학위논문 읽어줘", read_turn())
    full = turn.render(FULL)
    assert full[0] == {"role": "user", "content": "학위논문 읽어줘"} and BODY in full[2]["content"]
    clipped = turn.render(CLIPPED)
    assert len(clipped[2]["content"]) < 1000 and "(clipped to save context" in clipped[2]["content"]
    assert clipped[1] == full[1] and clipped[3] == full[3]           # 걸음은 그대로, 결과만 줄임
    brief = turn.render(BRIEF)
    assert len(brief) == 2 and "Sources: " + PAPER in brief[1]["content"] and "[work notes]" in brief[1]["content"]


def test_stopped_turn_ends_with_closing_message():
    result = done_result([step('{"tool": "fs.search"}'), obs("fs.search", FOUND)], fact="",
                         status="stopped", message="Stopped: the same call (fs.search) repeated 3 times.")
    turn = Turn.from_result("논문 찾아줘", result)
    full = turn.render(FULL)
    assert full[-1]["role"] == "assistant" and full[-1]["content"] == "(stopped: Stopped: the same call (fs.search) repeated 3 times.)"


def test_turn_without_steps_is_brief():
    turn = Turn.from_result("안녕", done_result([], fact="Hello, Researcher."))
    assert turn.render(FULL) == [{"role": "user", "content": "안녕"}, {"role": "assistant", "content": "Hello, Researcher."}]


# ---------------------------------------------------------------- 대화
def test_render_keeps_everything_when_it_fits():
    conv = Conversation()
    conv.add("학위논문 읽어줘", read_turn())
    conv.add("고마워", done_result([], fact="천만에요."))
    msgs = conv.render(100_000)
    assert [m["content"] for m in msgs if m["role"] == "user" and not m["content"].startswith("[")] == ["학위논문 읽어줘", "고마워"]
    assert any(BODY in m["content"] for m in msgs) and conv.last_levels == [FULL, FULL]


def test_render_shrinks_oldest_first():
    conv = Conversation()
    for i in range(3):
        conv.add(f"논문 {i} 읽어줘", read_turn())
    full_size = cx.size(Turn.from_result("x", read_turn()).render(FULL))
    conv.render(full_size + 1500)
    assert conv.last_levels[-1] == FULL and conv.last_levels[0] in (CLIPPED, BRIEF, DROPPED)
    conv.render(1200)
    assert conv.last_levels[-1] in (CLIPPED, BRIEF) and DROPPED in conv.last_levels
    assert conv.render(0) == [] and conv.last_levels == [DROPPED] * 3


def test_render_has_no_gaps():
    """중간 턴을 빼고 더 오래된 턴을 넣는 일은 없다 (대화가 이어져 보이게)."""
    conv = Conversation()
    conv.add("짧은 질문", done_result([], fact="짧은 답."))
    conv.add("긴 작업", read_turn())
    conv.add("짧은 질문 2", done_result([], fact="짧은 답 2."))
    conv.render(cx.size(conv.turns[2].render(FULL)) + 50)
    assert conv.last_levels == [DROPPED, DROPPED, FULL]


def test_preview_does_not_touch_last_render():
    conv = Conversation()
    assert conv.preview(None) == []
    conv.add("학위논문 읽어줘", read_turn())
    conv.add("고마워", done_result([], fact="천만에요."))
    rows = conv.preview(100_000)
    assert [(turn.request, level) for turn, level, _ in rows] == [("학위논문 읽어줘", FULL), ("고마워", FULL)]
    small = conv.preview(150)
    assert [level for _, level, _ in small] == [DROPPED, FULL]
    assert conv.last_levels == []                                           # 미리 보기만, 기록은 안 바꿈


# ---------------------------------------------------------------- 보내기 전에 맞추기
def test_fit_leaves_small_context_alone():
    system = {"role": "system", "content": "시스템"}
    hist = [{"role": "user", "content": "앞"}]
    task = [{"role": "user", "content": "지금"}]
    assert cx.fit(system, hist, task, 10_000) == (hist, task)


def test_fit_shrinks_history_then_old_observations():
    conv = Conversation()
    conv.add("학위논문 읽어줘", read_turn())
    system = {"role": "system", "content": "시"}
    hist = conv.render(None)
    task = [{"role": "user", "content": "세 파일 비교해줘"},
            step("a"), obs("fs.read", "가" * 3000), step("b"), obs("fs.read", "나" * 3000)]
    limit = cx.size([system]) + cx.size(task) + 500
    h2, t2 = cx.fit(system, hist, task, limit, conv.render)
    assert cx.size([system]) + cx.size(h2) + cx.size(t2) <= limit
    assert BODY not in "".join(m["content"] for m in h2)                 # 지난 대화를 먼저 줄임
    assert t2 == task                                                    # 이번 작업은 그대로
    h3, t3 = cx.fit(system, hist, task, cx.size([system]) + cx.size(task) - 1000, conv.render)
    assert h3 == [] and "(clipped to save context" in t3[2]["content"]       # 오래된 결과만 줄임
    assert t3[4] == task[4] and t3[0] == task[0]                          # 마지막 결과와 요청은 그대로


# ---------------------------------------------------------------- 근거
def test_grounded_by_file_name_in_tool_results():
    seen = [obs("fs.search", FOUND)]
    assert cx.grounded([PAPER], seen)
    assert cx.grounded(["C:\\Users\\User\\Downloads\\학위논문.pdf"], seen)    # 경로 표기가 달라도 이름으로
    assert not cx.grounded([], seen)                                         # 출처 없음
    assert not cx.grounded(["/x/없는파일.pdf"], seen)
    made_up = [step("Downloads has 비밀.pdf")]                               # 모델이 쓴 답은 근거가 아님
    assert not cx.grounded(["비밀.pdf"], made_up)
    memo = [step("Found it.\n[work notes]\n- fs.search → 1 found\n  - /a/비밀.pdf · 1KB")]
    assert cx.grounded(["비밀.pdf"], memo)                                   # 작업 메모는 도구 결과


def test_grounded_handles_decomposed_korean_names():
    import unicodedata
    nfd = unicodedata.normalize("NFD", PAPER)                               # 맥에서 만든 파일 이름
    assert cx.grounded([PAPER], [obs("fs.search", nfd)])


# ---------------------------------------------------------------- 루프와 함께
def reader(text=f"[{PAPER}] 전체 2700자\n{BODY}"):
    return Tool("fs.read", "read", run=lambda a: text)


def run(llm, memory, request, tools=None, context_chars=None, check=False):
    tools = tools or ToolRegistry([reader()])
    return run_task(
        request, llm=llm, policy=Policy(base_layers()),
        ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
        limits=LoopLimits(), system_prompt="test", memory=memory, context_chars=context_chars,
        checker=Checker(llm) if check else None,
    )


def test_loop_remembers_previous_task_in_full():
    memory = Conversation()
    llm = FakeLLM([act("fs.read", path=PAPER), answer("It is an EEG study.", "", [PAPER]),
                   answer("The conclusion: EEG can be turned into sentences.", "", [PAPER])])
    first = run(llm, memory, "학위논문 읽어줘")
    memory.add("학위논문 읽어줘", first)
    second = run(llm, memory, "그 논문 결론만 말해줘")
    assert second.status == "done" and second.steps == 1                 # 다시 읽지 않고 기억으로
    seen = llm.calls[2][0]
    assert any(BODY in m["content"] for m in seen)                        # 읽은 내용까지 기억
    assert [m["role"] for m in seen] == ["system", "user", "assistant", "user", "assistant", "user"]


def test_transcript_skips_sent_back_answer():
    memory = Conversation()
    llm = FakeLLM([answer("There are 0."), verdict(claims_supported="No tool result shows it."),
                   act("fs.read", path=PAPER), answer("I read it.", "", [PAPER]), verdict()])
    result = run(llm, memory, "찾은 PDF 읽어줘", check=True)
    contents = [m["content"] for m in result.transcript]
    assert not any("There are 0." in c or "[check]" in c for c in contents)
    assert len(result.transcript) == 3                                     # 읽기 걸음, 관찰, 답


def test_loop_fits_long_conversation_into_context():
    memory = Conversation()
    for i in range(6):
        memory.add(f"논문 {i} 읽어줘", read_turn())
    llm = FakeLLM([act("fs.read", path=PAPER), act("fs.read", path=PAPER + "2"), answer("I read them all.", "", [PAPER])])
    limit = cx.limit_chars(16384)
    result = run(llm, memory, "두 개 더 읽어줘", context_chars=limit)
    assert result.status == "done"
    for messages, _ in llm.calls:
        assert cx.size(messages) <= limit                                   # 어느 호출도 맥락 길이를 넘지 않음
    assert memory.last_levels[-1] == FULL and memory.last_levels[0] in (BRIEF, DROPPED)


def test_stopped_task_is_remembered_with_stop_note_and_report():
    memory = Conversation()
    llm = FakeLLM([act("fs.read", path=PAPER)] * 3 + [answer("I read it once and stopped.", "", [PAPER])])
    result = run(llm, memory, "학위논문 읽어줘")
    assert result.reason == "same_call"
    roles = [m["role"] for m in result.transcript]
    assert roles[-2:] == ["user", "assistant"] and result.transcript[-2]["content"].startswith("[stopped] Stopped: the same call")
    assert "Do not call more tools" not in result.transcript[-2]["content"]   # 지시문은 기억에 남기지 않음
    turn = memory.add("학위논문 읽어줘", result)
    assert turn.render(FULL)[-1]["content"] == result.transcript[-1]["content"]


def test_web_results_in_work_notes_and_local_texts():
    from glados.core.context import local_texts, observation, unseen_urls, work_memo

    memo = work_memo([{"tool": "web.search", "args": {"query": "llm metrics"},
                       "head": 'Web search (Tavily, sent to api.tavily.com): "llm metrics" · 2 results\n'
                               "- A | https://arxiv.org/abs/2610.00001\n  2026-10-02 · s\n- B | https://b.org/2"},
                      {"tool": "fs.read", "args": {"path": "/home/u/c.txt"}, "head": "[/home/u/c.txt] contract text"}])
    assert "  - A | https://arxiv.org/abs/2610.00001" in memo and "  - B | https://b.org/2" in memo
    msgs = [observation("web.fetch", "Page: https://b.org/2\nsecret? no, public text"),
            observation("fs.read", "[/home/u/c.txt] private contract text"),
            {"role": "user", "content": "compare my contract"},
            {"role": "user", "content": "[check] engine note"},
            {"role": "assistant", "content": memo}]
    local, user = local_texts(msgs)
    assert "private contract text" in local and "public text" not in local         # 웹에서 온 글은 원래 밖에 있던 것
    assert "arxiv.org" not in local and "/home/u/c.txt" in local                    # 작업 메모도 웹 줄은 뺀다
    assert user == "compare my contract"
    assert unseen_urls(["https://b.org/2/", "http://arxiv.org/abs/2610.00001", "https://c.org/3"], msgs) == [
        "https://c.org/3"]
    assert unseen_urls(["https://arxiv.org"], msgs) == ["https://arxiv.org"]          # 사이트 주소만으로는 안 됨

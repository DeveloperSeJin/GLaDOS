"""영어로 통일하기 시험: 낱말 나누기, 언어 판단, Glossary(모델 + 캐시), 찾기와 함께."""

from __future__ import annotations

import json
import time

import pytest

from conftest import FakeLLM
from glados import osinfo
from glados.core.translate import (
    Glossary, in_language, is_foreign, reply_language, split_name, stem,
)
from glados.hands import HandContext
from glados.hands import fs
from glados.safety.locations import Places


# ---------------------------------------------------------------- 낱말과 언어
def test_split_name_by_script():
    assert split_name("2025_외주개발_계약서 final.pdf") == (["final", "pdf"], ["외주개발", "계약서"])
    assert split_name("LDD학위논문v2") == (["ldd"], ["학위논문"])                 # 글자 종류가 바뀌는 곳에서
    assert split_name("請求書_2025.xlsx") == (["xlsx"], ["請求書"])                # 한국어만이 아님
    assert split_name("Rechnung_März") == (["rechnung"], ["märz"])               # 움라우트가 든 낱말은 통째로 옮길 낱말
    assert split_name("Ｒｅｐｏｒｔ_日本語のファイル") == (["report"], ["日本語", "の", "ファイル"])   # 전각 글자, 가나


def test_is_foreign_and_stem():
    assert is_foreign("학위논문") and is_foreign("märz") and not is_foreign("paper") and not is_foreign("2025")
    assert stem("papers") == stem("paper") == "paper" and stem("studies") == "study" and stem("class") == "class"


@pytest.mark.parametrize("text, lang", [
    ("Find the paper in my Downloads", "en"),
    ("Summarize C:\\Users\\User\\Downloads\\학위논문.pdf", "en"),        # 경로 속 한글은 세지 않음
    ("다운로드 폴더에서 논문 찾아줘", "ko"),
    ("BLEU-1 결과 알려줘", "ko"),
])
def test_reply_language(text, lang):
    assert reply_language(text) == lang


def test_reply_language_default_for_unclear_text():
    assert reply_language("ok", default="en") == "en" and reply_language("123", default="ko") == "ko"


def test_in_language():
    assert in_language("Found one paper: 학위논문.pdf.", "en")
    assert in_language('The thesis "뇌파 기반 문장 생성 연구" proposes LDD with BLEU-1 of 42.76.', "en")
    assert not in_language("파일이 없습니다.", "en")
    assert in_language("LDD의 BLEU-1은 42.76%로 baseline보다 높습니다.", "ko")          # 전문 용어는 영어 그대로
    assert not in_language("파일이 없습니다. 조건을放宽해 보겠습니다.", "ko")           # 한자 섞임
    assert not in_language("I found one paper.", "ko")
    assert not in_language("我找到了一篇论文。", "en") and not in_language("我找到了一篇论文。", "ko")   # 통째로 중국어
    assert in_language("42.76", "en")                                               # 글자가 없으면 어느 언어든


# ---------------------------------------------------------------- Glossary
def gloss_reply(*rows):
    return {"items": [{"i": i, "keywords": list(words)} for i, words in rows]}


def test_glossary_asks_model_once_and_caches(tmp_path):
    cache = tmp_path / "cache" / "names-en.json"
    llm = FakeLLM([gloss_reply((0, ["Thesis", "dissertation", "paper"]), (1, ["contract", "agreement!"]))])
    g = Glossary(llm, cache)
    got = g.english(["학위논문", "계약서", "paper", "학위논문"])
    assert got == {"학위논문": ("thesis", "dissertation", "paper"), "계약서": ("contract", "agreement")}
    system, user = llm.calls[0][0]
    assert "English search keywords" in system["content"] and user["content"] == "0: 학위논문\n1: 계약서"
    assert json.loads(cache.read_text(encoding="utf-8"))["words"]["계약서"] == ["contract", "agreement"]
    again = Glossary(FakeLLM([]), cache)                                         # 다시 켜도 캐시로 (모델 안 부름)
    assert again.english(["계약서"]) == {"계약서": ("contract", "agreement")}


def test_glossary_batches_and_limits(tmp_path):
    words = [f"낱말{i}" for i in range(7)]
    llm = FakeLLM([gloss_reply(*[(i, [f"w{i}"]) for i in range(3)]), gloss_reply(*[(i, [f"x{i}"]) for i in range(2)])])
    g = Glossary(llm, None, batch=3, max_new=5)
    got = g.english(words)
    assert len(llm.calls) == 2 and len(got) == 5                                 # 한 번에 5개까지, 3개씩 나눠 물음
    assert g.english(words[:5]) == got and len(llm.calls) == 2                   # 옮긴 것은 다시 묻지 않음


def test_glossary_survives_model_failure_and_deadline(tmp_path):
    g = Glossary(FakeLLM([RuntimeError("down")]), None)
    assert g.english(["학위논문"]) == {}                                          # 못 옮기면 없는 것으로
    g = Glossary(FakeLLM([]), None)
    assert g.english(["학위논문"], deadline=time.monotonic() - 1) == {}           # 시간이 지나면 묻지 않음
    assert Glossary(None, None).english(["학위논문"]) == {}


def test_glossary_ignores_bad_items(tmp_path):
    llm = FakeLLM([{"items": [{"i": 9, "keywords": ["x"]}, {"i": "a"}, {"i": 0, "keywords": "not a list"}]}])
    g = Glossary(llm, None)
    assert g.english(["학위논문"]) == {"학위논문": ()}


# ---------------------------------------------------------------- 찾기와 함께 (진짜 Glossary)
class DictGlossLLM:
    """Glossary 호출에 낱말 표로 답하는 가짜 모델 (낱말 순서와 상관없이)."""

    def __init__(self, words: dict[str, list[str]]):
        self.words = words
        self.calls: list[list[str]] = []

    def chat_json(self, messages, schema, validate, retries=2, temperature=0.2):
        lines = [ln.split(": ", 1) for ln in messages[-1]["content"].splitlines()]
        self.calls.append([tok for _, tok in lines])
        reply = {"items": [{"i": int(i), "keywords": self.words.get(tok, [])} for i, tok in lines]}
        return validate(reply), json.dumps(reply, ensure_ascii=False)


def test_search_with_real_glossary(tmp_path):
    """실제 기록 재현: 'Find the paper in my Downloads' -> query=paper, 파일은 학위논문.pdf."""
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Downloads" / "학위논문.pdf").write_bytes(b"%PDF-1.4")
    (home / "Downloads" / "영수증_11월.pdf").write_bytes(b"%PDF-1.4")
    llm = DictGlossLLM({"학위논문": ["thesis", "dissertation", "paper"], "영수증": ["receipt"], "월": ["month"],
                        "다운로드": ["downloads", "download"]})
    hctx = HandContext(
        places=Places(os=osinfo.current_os(), home=str(home), glados_home=str(home / ".glados"), resolve_links=False),
        home=home / ".glados", glossary=Glossary(llm, home / ".glados" / "cache" / "names-en.json"),
    )
    out = fs.search({"query": "paper", "kind": "pdf", "roots": ["~/Downloads"]}, hctx)
    assert out.startswith("1 found") and "학위논문.pdf" in out and "en: thesis, dissertation, paper" in out
    assert "영수증" not in out
    out = fs.search({"query": "receipt", "kind": "pdf", "roots": ["다운로드"]}, hctx)   # 폴더 말도 영어로 옮겨 알아봄
    assert out.startswith("1 found") and "영수증_11월.pdf" in out
    assert len(llm.calls) == 2 and llm.calls[1] == ["다운로드"]                     # 파일 이름 낱말은 한 번만 옮김
    cache = json.loads((home / ".glados" / "cache" / "names-en.json").read_text(encoding="utf-8"))["words"]
    assert cache["학위논문"] == ["thesis", "dissertation", "paper"]

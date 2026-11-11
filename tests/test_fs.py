"""파일 손 시험 (M2): 찾기·읽기·새 파일·수정+체크포인트."""

from __future__ import annotations

import os
import time
import unicodedata
from pathlib import Path

import pytest

from conftest import FakeLLM, StaticGlossary, act, answer, base_layers
from glados import osinfo
from glados.config import LoopLimits
from glados.core.loop import run_task
from glados.hands import HandContext, ToolError, load_hands
from glados.hands import fs
from glados.safety.checkpoint import Checkpoints
from glados.safety.locations import Places
from glados.safety.policy import Policy, PolicyContext


def make_pdf(pages: list[str]) -> bytes:
    """글이 든 아주 작은 PDF (영문 글꼴 Helvetica)."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objs.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


@pytest.fixture
def world(tmp_path):
    """가짜 홈: 다운로드·문서·.ssh, GLaDOS 설정 폴더."""
    home = tmp_path / "home"
    (home / "Downloads" / "계약").mkdir(parents=True)
    (home / "Documents").mkdir()
    (home / ".ssh").mkdir()
    gh = home / ".glados"
    gh.mkdir()
    (home / "Downloads" / "계약" / "2025_외주개발_계약서.pdf").write_bytes(make_pdf(["Penalty clause: 10 percent", "Term: 12 months"]))
    (home / "Downloads" / "2024_임대_계약서.pdf").write_bytes(make_pdf(["Lease"]))
    (home / "Documents" / "회의록.md").write_text("# 회의록\n- 할 일: 실험 3 재현\n", encoding="utf-8")
    (home / "Documents" / "예전메모.txt").write_bytes("한글 메모입니다".encode("cp949"))
    (home / ".ssh" / "id_계약_rsa").write_text("PRIVATE", encoding="utf-8")
    places = Places(
        os=osinfo.current_os(), home=str(home), glados_home=str(gh), resolve_links=False,
    )
    hctx = HandContext(
        places=places, home=gh, search_default=["~/Downloads", "~/Documents"], checkpoints=Checkpoints(gh),
        glossary=StaticGlossary(),
    )
    return home, hctx


# ---------------------------------------------------------------- 찾기
@pytest.mark.parametrize("use_rg", [True, False])
def test_search_by_name_and_kind(world, monkeypatch, use_rg):
    home, hctx = world
    if not use_rg:
        monkeypatch.setattr(fs.shutil, "which", lambda name: None)
    out = fs.search({"query": "contract", "kind": "pdf"}, hctx)                  # 영어 낱말로 한국어 이름을 찾음
    assert out.startswith("2 found")
    assert "2025_외주개발_계약서.pdf" in out and "2024_임대_계약서.pdf" in out
    assert "id_계약_rsa" not in out                    # 보호 위치는 결과에서 뺌


def test_search_matches_folder_names(world):
    _, hctx = world
    out = fs.search({"query": "contract outsourcing"}, hctx)
    assert "2025_외주개발_계약서.pdf" in out and "2024_임대" not in out


def test_search_year_from_name_or_mtime(world):
    home, hctx = world
    old = home / "Downloads" / "2024_임대_계약서.pdf"
    ts = time.mktime((2024, 5, 1, 0, 0, 0, 0, 0, -1))
    os.utime(old, (ts, ts))
    out = fs.search({"query": "계약", "year": 2025}, hctx)
    assert "2025_외주개발" in out and "2024_임대" not in out
    out = fs.search({"query": "계약", "year": 2024}, hctx)
    assert "2024_임대" in out


def test_search_nfd_names_found_with_nfc_query(world):
    home, hctx = world
    nfd_name = unicodedata.normalize("NFD", "논문초안.md")
    (home / "Documents" / nfd_name).write_text("x", encoding="utf-8")
    out = fs.search({"query": "논문"}, hctx)
    assert out.startswith("1 found")
    assert fs.search({"query": "paper"}, hctx).startswith("1 found")          # 영어로도 (낱말 뜻으로 맞춤)


def test_search_content_in_text_files(world):
    _, hctx = world
    out = fs.search({"query": "실험 3 재현", "content": True}, hctx)
    assert "회의록.md" in out and out.startswith("1 found")


def test_search_explicit_protected_root_lists_it(world):
    home, hctx = world
    out = fs.search({"query": "rsa", "roots": [str(home / ".ssh")]}, hctx)
    assert "id_계약_rsa" in out                       # 연구원님이 직접 고른 범위(승인 받음)


def test_search_skips_glados_home(world):
    home, hctx = world
    (home / ".glados" / "계약_메모.md").write_text("x", encoding="utf-8")
    out = fs.search({"query": "contract", "roots": [str(home)]}, hctx)
    assert "계약_메모.md" not in out


def test_search_excludes_visible_protected_files(world):
    home, hctx = world
    (home / "Documents" / "server.pem").write_text("KEY", encoding="utf-8")       # 숨김 파일이 아닌 키 파일
    (home / "Documents" / "server_설명.md").write_text("x", encoding="utf-8")
    out = fs.search({"query": "server"}, hctx)
    assert "server_설명.md" in out and "server.pem" not in out


def test_search_excludes_glados_source(world):
    home, hctx = world
    src = home / "code" / "glados"
    src.mkdir(parents=True)
    (src / "계약_시험.md").write_text("x", encoding="utf-8")
    (home / "code" / "계약_분석.md").write_text("x", encoding="utf-8")
    from dataclasses import replace

    hctx.places = replace(hctx.places, source_dirs=(str(src),))
    out = fs.search({"query": "계약", "roots": [str(home / "code")]}, hctx)
    assert "계약_분석.md" in out and "계약_시험.md" not in out


def test_search_missing_root(world):
    _, hctx = world
    with pytest.raises(ToolError):
        fs.search({"query": "x", "roots": ["~/없는폴더"]}, hctx)


def test_search_zero_result_lists_closest_instead(world):
    """다 맞는 이름이 없으면 같은 범위·종류에서 가장 가까운 파일을 대신 보여 준다 (일부 낱말이 맞은 것 먼저)."""
    _, hctx = world
    out = fs.search({"query": "lease invoice", "kind": "pdf"}, hctx)
    lines = out.splitlines()
    assert lines[0].startswith("0 found") and lines[1].startswith("No file name matches all the words. Closest 2 of 2 pdf files")
    assert "2024_임대_계약서.pdf" in lines[2] and "2025_외주개발_계약서.pdf" in lines[3]   # lease가 맞은 것이 먼저
    assert "회의록" not in out and lines[4].startswith("If the file you want is listed")
    assert lines[5].startswith("Only file names were searched.")                  # 내용을 볼지는 모델이 정한다
    empty = fs.search({"query": "nothing", "kind": "hwp"}, hctx)
    assert "there are no hwp/hwpx files in this scope" in empty
    plain = fs.search({"query": "", "kind": "hwp"}, hctx)
    assert plain.endswith("No file matches.")


def test_search_english_word_finds_korean_name(world):
    """실제 기록: "Find the paper in my Downloads" -> query=paper, 파일 이름은 학위논문.pdf."""
    home, hctx = world
    (home / "Downloads" / "학위논문.pdf").write_bytes(make_pdf(["LDD"]))
    for query in ("paper", "papers", "thesis", "dissertation", "논문"):
        out = fs.search({"query": query, "kind": "pdf", "roots": ["~/Downloads"]}, hctx)
        assert out.startswith("1 found") and "학위논문.pdf" in out, query
    assert out.splitlines()[1].endswith("· en: thesis, dissertation, paper")          # 모델이 영어 뜻을 봄
    out = fs.search({"query": "agreements", "kind": "pdf"}, hctx)
    assert out.startswith("2 found") and "en: contract, agreement, outsourcing, development" in out   # 폴더 이름(계약)도 낱말


def test_search_exact_names_come_first(world):
    home, hctx = world
    (home / "Documents" / "paper_draft.pdf").write_bytes(make_pdf(["x"]))
    (home / "Documents" / "학위논문_최종.pdf").write_bytes(make_pdf(["y"]))
    os.utime(home / "Documents" / "paper_draft.pdf", (1_600_000_000, 1_600_000_000))   # 더 오래된 파일이어도
    out = fs.search({"query": "paper", "kind": "pdf", "roots": ["~/Documents"]}, hctx)
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert "paper_draft.pdf" in lines[0] and "학위논문_최종.pdf" in lines[1]


@pytest.mark.parametrize("root", ["다운로드", "~/다운로드", "다운로드 폴더", "Downloads", "~\\Downloads", "~/Downloads/계약",
                                  "my downloads folder"])
def test_search_folder_words_go_to_real_folder(world, root):
    """폴더 말은 영어로 옮겨 알아본다 (다운로드 -> downloads)."""
    home, hctx = world
    out = fs.search({"query": "contract", "kind": "pdf", "roots": [root]}, hctx)
    assert "2025_외주개발_계약서.pdf" in out
    assert "회의록" not in out
    assert str(home / "Downloads") in out.splitlines()[0]


def test_search_korean_linux_folder_names(world):
    home, hctx = world
    (home / "바탕화면").mkdir()                                  # 한국어 리눅스: ~/Desktop 대신 ~/바탕화면
    (home / "바탕화면" / "발표_논문.pdf").write_bytes(make_pdf(["slides"]))
    hctx.user_dirs = osinfo.user_folders(home, "linux", env={})               # 실제 위치 후보에 ~/바탕화면
    out = fs.search({"query": "paper", "roots": ["~/Desktop"]}, hctx)
    assert "발표_논문.pdf" in out and "do not exist" not in out


def test_search_uses_known_folder_locations(world, tmp_path):
    home, hctx = world
    moved = tmp_path / "OneDrive" / "문서"                       # Windows: 문서를 OneDrive로 옮긴 경우
    moved.mkdir(parents=True)
    (moved / "실험_계획.md").write_text("x", encoding="utf-8")
    hctx.user_dirs = {"documents": (str(moved), str(home / "Documents"))}
    out = fs.search({"query": "", "roots": ["문서"]}, hctx)
    assert "실험_계획.md" in out and "회의록.md" in out          # 옮긴 곳과 원래 곳 둘 다


def test_search_home_alias_and_no_duplicates(world):
    home, hctx = world
    out = fs.search({"query": "lease", "roots": ["home", "~/Downloads"]}, hctx)
    assert out.startswith("1 found") and out.count("2024_임대_계약서.pdf") == 1


def test_search_missing_folder_word(world):
    _, hctx = world
    with pytest.raises(ToolError, match="None of the folders exist") as e:
        fs.search({"query": "x", "roots": ["바탕화면"]}, hctx)   # 이 가짜 홈에는 바탕화면이 없음
    assert e.value.key == "tool.no_roots"                     # 화면에서는 설정 언어로


def test_search_limit(world):
    home, hctx = world
    for i in range(5):
        (home / "Documents" / f"노트{i}.md").write_text("x", encoding="utf-8")
    out = fs.search({"query": "note", "limit": 2}, hctx)
    assert out.startswith("7 found, showing 2") and out.count("\n- ") == 2   # 노트 5개 + 뜻이 맞는 예전메모·회의록(notes)
    assert "예전메모.txt" not in out
    out = fs.search({"query": "노트", "limit": 2}, hctx)
    assert out.startswith("7 found, showing 2") and "예전메모.txt" not in out  # 글자 그대로 맞은 것이 먼저


# ---------------------------------------------------------------- 사용자 폴더 위치
def test_user_folders_linux_reads_xdg(tmp_path):
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "user-dirs.dirs").write_text(
        '# 주석\nXDG_DOWNLOAD_DIR="$HOME/다운로드"\nXDG_DOCUMENTS_DIR="$HOME/"\nXDG_DESKTOP_DIR="/data/바탕화면"\n',
        encoding="utf-8",
    )
    dirs = osinfo.user_folders(tmp_path, "linux", env={})
    assert dirs["downloads"][0] == os.path.normpath(str(tmp_path / "다운로드"))
    assert dirs["documents"] == (str(tmp_path / "Documents"), str(tmp_path / "문서"))   # 홈 자체면 안 씀
    assert dirs["desktop"][0] == os.path.normpath("/data/바탕화면")


def test_user_folders_mac_and_windows(tmp_path, monkeypatch):
    assert osinfo.user_folders(tmp_path, "mac", env={})["downloads"] == (str(tmp_path / "Downloads"),)
    monkeypatch.setattr(osinfo, "_windows_folder", lambda kind: "D:\\받은파일" if kind == "downloads" else None)
    dirs = osinfo.user_folders(tmp_path, "windows", env={})
    assert dirs["downloads"] == ("D:\\받은파일", str(tmp_path / "Downloads"))
    assert dirs["desktop"] == (str(tmp_path / "Desktop"),)


# ---------------------------------------------------------------- 읽기
def test_read_pdf_pages(world):
    home, hctx = world
    path = home / "Downloads" / "계약" / "2025_외주개발_계약서.pdf"
    out = fs.read({"path": str(path)}, hctx)
    assert "PDF with 2 pages" in out and "[p.1]" in out and "Penalty clause" in out and "[p.2]" in out
    only2 = fs.read({"path": str(path), "pages": "2"}, hctx)
    assert "Term: 12 months" in only2 and "Penalty" not in only2


def test_read_docx(world):
    from docx import Document

    home, hctx = world
    doc = Document()
    doc.add_paragraph("위약금 조항: 계약 금액의 10%")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "항목"
    table.rows[0].cells[1].text = "금액"
    path = home / "Documents" / "계약 요약.docx"
    doc.save(str(path))
    out = fs.read({"path": str(path)}, hctx)
    assert "위약금 조항" in out and "[table 1]" in out and "항목 | 금액" in out


def test_read_cp949_text(world):
    home, hctx = world
    out = fs.read({"path": "~/Documents/예전메모.txt"}, hctx)
    assert "한글 메모입니다" in out and "encoding cp949" in out


def test_read_paging_with_offset(world):
    home, hctx = world
    (home / "Documents" / "긴글.txt").write_text("가" * 5000, encoding="utf-8")
    first = fs.read({"path": "~/Documents/긴글.txt", "max_chars": 1000}, hctx)
    assert "5000 chars total" in first and "(continues: offset=1000)" in first
    last = fs.read({"path": "~/Documents/긴글.txt", "offset": 4500, "max_chars": 1000}, hctx)
    assert "continues" not in last and "from char 4500" in last


def test_read_rejects_binary_and_unsupported(world):
    home, hctx = world
    (home / "Documents" / "그림.bin").write_bytes(b"\x00\x01\x02" * 100)
    with pytest.raises(ToolError, match="binary"):
        fs.read({"path": "~/Documents/그림.bin"}, hctx)
    (home / "Documents" / "보고서.hwp").write_bytes(b"x")
    with pytest.raises(ToolError, match=r"Cannot read \.hwp files yet") as e:
        fs.read({"path": "~/Documents/보고서.hwp"}, hctx)
    assert e.value.key == "tool.unsupported" and "hwp" in str(e.value)
    with pytest.raises(ToolError, match="No such file"):
        fs.read({"path": "~/Documents/없는파일.md"}, hctx)


# ---------------------------------------------------------------- 새 파일
def test_write_new_never_overwrites(world):
    home, hctx = world
    out = fs.write_new({"path": "~/Documents/회의록.md", "content": "새 내용"}, hctx)
    assert "회의록 (2).md" in out
    assert (home / "Documents" / "회의록.md").read_text(encoding="utf-8").startswith("# 회의록")
    assert (home / "Documents" / "회의록 (2).md").read_text(encoding="utf-8") == "새 내용"


def test_write_new_makes_folders_and_keeps_newlines(world):
    home, hctx = world
    fs.write_new({"path": "~/Documents/요약/논문 요약.md", "content": "줄1\n줄2\n"}, hctx)
    assert (home / "Documents" / "요약" / "논문 요약.md").read_bytes() == "줄1\n줄2\n".encode("utf-8")


def test_write_new_refuses_document_formats(world):
    _, hctx = world
    with pytest.raises(ToolError, match=r"\.md"):
        fs.write_new({"path": "~/Documents/요약.docx", "content": "x"}, hctx)


# ---------------------------------------------------------------- 수정 + 체크포인트
def test_edit_replaces_once_and_undo_restores(world):
    home, hctx = world
    path = home / "Documents" / "회의록.md"
    before = path.read_text(encoding="utf-8")
    out = fs.edit({"path": str(path), "old": "실험 3 재현", "new": "실험 3 재현 (금요일까지)"}, hctx)
    assert out.startswith("Edited:") and "/undo" in out
    assert "금요일까지" in path.read_text(encoding="utf-8")
    cp = hctx.checkpoints.last()
    assert cp is not None and Path(cp.saved).read_text(encoding="utf-8") == before
    hctx.checkpoints.restore(cp)
    assert path.read_text(encoding="utf-8") == before
    assert hctx.checkpoints.last() is None


def test_edit_errors(world):
    home, hctx = world
    (home / "Documents" / "반복.txt").write_text("a\na\n", encoding="utf-8")
    with pytest.raises(ToolError, match="was not found"):
        fs.edit({"path": "~/Documents/반복.txt", "old": "zzz", "new": "y"}, hctx)
    with pytest.raises(ToolError, match="appears 2 times"):
        fs.edit({"path": "~/Documents/반복.txt", "old": "a", "new": "b"}, hctx)
    out = fs.edit({"path": "~/Documents/반복.txt", "old": "a", "new": "b", "replace_all": True}, hctx)
    assert "(2 places)" in out and (home / "Documents" / "반복.txt").read_text(encoding="utf-8") == "b\nb\n"


def test_edit_keeps_cp949_encoding(world):
    home, hctx = world
    fs.edit({"path": "~/Documents/예전메모.txt", "old": "메모", "new": "기록"}, hctx)
    assert (home / "Documents" / "예전메모.txt").read_bytes() == "한글 기록입니다".encode("cp949")


def test_edit_refuses_documents(world):
    home, hctx = world
    with pytest.raises(ToolError, match="Original documents are not edited"):
        fs.edit({"path": "~/Downloads/2024_임대_계약서.pdf", "old": "Lease", "new": "x"}, hctx)


# ---------------------------------------------------------------- 루프와 함께 (시나리오 1 축소판)
def test_scenario_find_read_summarize(world):
    home, hctx = world
    tools = load_hands(osinfo.current_os(), hctx)
    assert tools.names() == ["fs.search", "fs.inspect", "fs.read", "fs.write_new", "fs.edit", "shell.run", "web.search", "web.fetch"]
    pdf = str(home / "Downloads" / "계약" / "2025_외주개발_계약서.pdf")
    llm = FakeLLM([
        act("fs.search", query="contract", kind="pdf", year=2025),
        act("fs.read", path=pdf),
        act("fs.write_new", path="~/Documents/위약금_정리.md", content="| 계약 | 위약금 |\n| 2025 외주개발 | 10% |\n"),
        answer("One contract has a penalty clause.", "", sources=[pdf]),
    ])
    asked = []
    ctx = PolicyContext(places=hctx.places, registered_tools=frozenset(tools.names()))
    result = run_task(
        "작년 계약서 PDF에서 위약금 조항 정리해줘", llm=llm, policy=Policy(base_layers()), ctx=ctx,
        tools=tools, limits=LoopLimits(), system_prompt="test", approve=lambda c, d: asked.append(c) or "deny",
    )
    assert result.status == "done" and result.sources == [pdf]
    observations = [m["content"] for m in llm.calls[3][0] if m["content"].startswith("[result")]
    assert "2025_외주개발_계약서.pdf" in observations[0]
    assert "Penalty clause" in observations[1]
    assert "Created new file" in observations[2]
    assert (home / "Documents" / "위약금_정리.md").exists() and asked == []   # 일반 위치는 묻지 않음
    schema = llm.calls[0][1]
    search_args = schema["properties"]["action"]["anyOf"][0]["properties"]["args"]
    assert search_args["required"] == ["query"]                                # 도구별 인자 스키마


def test_edit_through_loop_needs_approval(world):
    home, hctx = world
    tools = load_hands(osinfo.current_os(), hctx)
    llm = FakeLLM([act("fs.edit", path="~/Documents/회의록.md", old="할 일", new="해야 할 일"), answer("Edited.")])
    asked = []
    ctx = PolicyContext(places=hctx.places, registered_tools=frozenset(tools.names()))
    run_task("회의록 고쳐줘", llm=llm, policy=Policy(base_layers()), ctx=ctx, tools=tools,
             limits=LoopLimits(), system_prompt="test", approve=lambda c, d: asked.append(d.reason) or "once")
    assert len(asked) == 1 and "해야 할 일" in (home / "Documents" / "회의록.md").read_text(encoding="utf-8")


def test_write_into_glados_home_denied_through_loop(world):
    home, hctx = world
    tools = load_hands(osinfo.current_os(), hctx)
    llm = FakeLLM([act("fs.write_new", path="~/.glados/policy.yaml", content="fs.edit: {'*': allow}"), answer("I could not.")])
    ctx = PolicyContext(places=hctx.places, registered_tools=frozenset(tools.names()))
    run_task("정책 풀어줘", llm=llm, policy=Policy(base_layers()), ctx=ctx, tools=tools,
             limits=LoopLimits(), system_prompt="test")
    assert not (home / ".glados" / "policy.yaml").exists()
    assert "GLaDOS safety files cannot be changed (GLaDOS settings and logs folder)" in llm.calls[1][0][-1]["content"]

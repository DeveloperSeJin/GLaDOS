"""모든 드라이브 찾기와 사진 검사 (M7.5): 파일 목록이 된 색인, 큰 범위 찾기, 그림·스캔을 뒤에서 보기,
그림을 직접 보는 살펴보기, 같은 찾기 반복 막기."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import FakeLLM, StaticGlossary, act, answer
from glados import osinfo
from glados.config import LoopLimits
from glados.core.llm import LLMError
from glados.core.loop import run_task
from glados.hands import HandContext, ToolRegistry, docs, fs, ocr
from glados.hands import index as ci
from glados.safety.checkpoint import Checkpoints
from glados.safety.locations import Places
from glados.safety.policy import Policy
from test_fs import make_pdf

pytestmark = pytest.mark.skipif(not ci.fts5_ok(), reason="this Python's SQLite has no FTS5 trigram")


def photo(path: Path, size=(1200, 800)) -> None:
    """20KB 넘는 그림 (색인이 볼 그림)."""
    import random

    rnd = random.Random(str(path))
    img = Image.new("RGB", size)
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size[0] * size[1])])
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "PNG")


class Seer(FakeLLM):
    """그림을 보면 정해 둔 답 (DESCRIBE 형식, 살펴보기 답)."""

    def __init__(self, describe: dict[str, str] | None = None, yes: tuple[str, ...] = (), fail: Exception | None = None):
        super().__init__([])
        self.describe = describe or {}
        self.yes = yes
        self.fail = fail
        self.calls_seen: list[str] = []

    def see(self, image, prompt, max_tokens=2048):
        if self.fail:
            raise self.fail
        assert image.startswith(b"\x89PNG")
        size = Image.open(__import__("io").BytesIO(image)).size
        self.calls_seen.append("ask" if "Question about this image" in prompt else "describe")
        if "Question about this image" in prompt:
            return "yes - a card titled 자동차운전면허증" if size in self.yes else "No - a photo of a cat"
        return self.describe.get(max(size), "DESCRIPTION: a photo of a cat\nTEXT:\nNO_TEXT")


@pytest.fixture
def world(tmp_path):
    """가짜 홈: 사진·문서·코드가 섞여 있고, 구글 드라이브처럼 홈 밖에 다른 드라이브(폴더)가 있다."""
    home = tmp_path / "home"
    drive = tmp_path / "gdrive" / "내 드라이브"
    (home / "Downloads").mkdir(parents=True)
    (home / "Documents" / "toolbox").mkdir(parents=True)
    (home / "Desktop").mkdir()
    drive.mkdir(parents=True)
    gh = home / ".glados"
    gh.mkdir()
    (home / "Documents" / "toolbox" / "LICENSE").write_text("Permission is granted ... license ... driver", encoding="utf-8")
    (home / "Documents" / "toolbox" / "LICENSE.txt").write_text("MIT license", encoding="utf-8")
    (home / "Documents" / "보고서.md").write_text("# 실험 보고서\n결과 요약", encoding="utf-8")
    (home / "Documents" / "setup.exe").write_bytes(b"MZ" + b"\0" * 100)
    photo(drive / "사진" / "IMG_0412.png", (1200, 800))                        # 면허증 사진
    photo(drive / "사진" / "IMG_0413.png", (1000, 700))                        # 고양이
    (drive / "icon.png").write_bytes(b"\x89PNG small")                          # 20KB 아래: 이름만
    (drive / "스캔.pdf").write_bytes(make_pdf(["", ""]))
    (tmp_path / "gdrive" / "$RECYCLE.BIN").mkdir()
    (tmp_path / "gdrive" / "$RECYCLE.BIN" / "old.txt").write_text("license", encoding="utf-8")
    places = Places(os=osinfo.current_os(), home=str(home), glados_home=str(gh), resolve_links=False)
    hctx = HandContext(places=places, home=gh, search_default=["~"], checkpoints=Checkpoints(gh),
                       glossary=StaticGlossary({**StaticGlossary.WORDS, "면허증": ("license", "permit")}),
                       model_name="qwen3.5:4b")
    roots = [str(home), str(tmp_path / "gdrive")]
    return home, drive, hctx, roots


def build(world, seer=None):
    _, _, hctx, roots = world
    idx = ci.FileIndex(hctx.home)
    try:
        ci.run_pass(idx, roots, hctx.places)
        idx.set_meta(roots=__import__("json").dumps(roots))
        if seer is not None:
            cache = docs.DocCache(hctx.home / "cache" / "doctext.sqlite")
            try:
                ci.ocr_pass(idx, seer, "qwen3.5:4b", lambda: False, cache=cache)
            finally:
                cache.close()
    finally:
        idx.close()


LICENSE_PHOTO = ("DESCRIPTION: a photo of a Korean driver's license card\nTEXT:\n자동차운전면허증\n홍길동\n"
                 "11-23-456789-01")


# ---------------------------------------------------------------- 파일 목록이 된 색인
def test_index_lists_every_file_and_queues_photos_and_scans(world):
    _, drive, hctx, roots = world
    build(world)
    idx = ci.FileIndex(hctx.home)
    try:
        st = idx.stats()
        by = st["by_status"]
        assert by["image"] == 2 and by["no_text"] == 1 and by["name"] >= 2         # 사진 2장, 스캔 1, 아이콘·exe
        assert st["ocr_left"] == 3 and float(idx.meta("catalog_finished")) > 0
        keys = idx.all_ids()
        assert not any("$recycle.bin" in k.casefold() for k in keys)               # $로 시작하는 폴더는 건너뜀
        assert ci.file_key(drive / "icon.png") in keys                              # 작은 그림도 이름은 있다
    finally:
        idx.close()


def test_background_ocr_describes_photos_and_reads_scans(world):
    _, drive, hctx, roots = world
    seer = Seer({1200: LICENSE_PHOTO})
    seer.texts = ["Scanned page one: lease", "page two"]
    seer_see = seer.see

    def see(image, prompt, max_tokens=2048):
        if "Transcribe all text" in prompt:                                          # 스캔 pdf 쪽
            return seer.texts.pop(0)
        return seer_see(image, prompt, max_tokens)

    seer.see = see
    build(world, seer)
    idx = ci.FileIndex(hctx.home)
    try:
        st = idx.stats()
        assert st["ocr_left"] == 0 and st["ocr_done"] == 3 and idx.meta("ocr_state") == "idle"
        lic = idx.entries([ci.file_key(drive / "사진" / "IMG_0412.png")])[ci.file_key(drive / "사진" / "IMG_0412.png")]
        assert lic.status == "ok" and "Korean driver's license" in idx.body(lic.id)
        assert idx.match([["면허증"]]) == [lic.id]
        scan = idx.entries([ci.file_key(drive / "스캔.pdf")])[ci.file_key(drive / "스캔.pdf")]
        assert scan.status == "ok" and idx.snippet(scan.id, ["page two"])[1] == 2
    finally:
        idx.close()
    cache = docs.DocCache(hctx.home / "cache" / "doctext.sqlite")                     # 다시 보지 않게 캐시
    try:
        path = drive / "사진" / "IMG_0412.png"
        assert cache.ocr(path, path.stat(), "qwen3.5:4b" + ocr.DESCRIBED)[0].startswith("Image: a photo of a Korean")
    finally:
        cache.close()


def test_background_ocr_waits_while_busy_and_stops_without_a_vision_model(world):
    _, _, hctx, roots = world
    build(world)
    idx = ci.FileIndex(hctx.home)
    try:
        stop = ci.threading.Event()
        waits: list[float] = []
        stop.wait = lambda s: waits.append(s) or False
        busy = iter([True, False] + [False] * 50)
        no_vision = LLMError("Model x cannot read images", "llm.no_vision", model="x")
        assert ci.ocr_pass(idx, Seer(fail=no_vision), "x", lambda: next(busy), stop) == 0
        assert waits == [ci.BUSY_WAIT_S] and idx.meta("ocr_state") == "missing"
        assert idx.stats()["ocr_left"] == 3                                         # 아무것도 버리지 않았다
    finally:
        idx.close()


def test_describe_parses_the_answer():
    class One:
        def __init__(self, text):
            self.text = text

        def see(self, image, prompt, max_tokens=2048):
            return self.text

    assert ocr.describe(One(LICENSE_PHOTO), b"") == ("Image: a photo of a Korean driver's license card\n"
                                                      "자동차운전면허증\n홍길동\n11-23-456789-01")
    assert ocr.describe(One("DESCRIPTION: a cat\nTEXT:\nNO_TEXT"), b"") == "Image: a cat"
    assert ocr.describe(One("just some words"), b"") == "Image\njust some words"
    assert ocr.ask(One("Yes - it shows a license"), b"", "q") == ("yes", "it shows a license")
    assert ocr.ask(One("maybe?"), b"", "q") == ("unsure", "maybe?")


# ---------------------------------------------------------------- 찾기
def test_big_scope_uses_the_file_list_and_lists_downloads_now(world, monkeypatch):
    home, drive, hctx, roots = world
    build(world)
    (home / "Documents" / "보고서.md").unlink()                                    # 목록에는 아직 있다
    (home / "Downloads" / "방금받은_보고서.md").write_text("new", encoding="utf-8") # 목록에는 아직 없다
    calls: list[str] = []
    real = fs._list_with_rg

    def rg(rg_path, root, exts):
        calls.append(str(root))
        return real(rg_path, root, exts)

    monkeypatch.setattr(fs, "_list_with_rg", rg)
    out = fs.search({"query": "보고서"}, hctx)                                       # 기본 범위 ~ (큰 범위)
    assert "방금받은_보고서.md" in out and "보고서.md" in out
    assert "File list from the index" in out
    assert all("Downloads" in c or "Desktop" in c for c in calls)                   # 홈 전체는 다시 훑지 않음
    out = fs.search({"query": "보고서", "roots": ["~/Documents"]}, hctx)             # 작은 범위는 지금 훑는다
    assert "File list from the index" not in out and "0 found" in out


def test_content_search_puts_literal_matches_before_translated_ones(world):
    home, drive, hctx, roots = world
    build(world, Seer({1200: LICENSE_PHOTO}))
    out = fs.search({"query": "면허증", "roots": ["~", str(drive.parent)], "content": True}, hctx)
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert "IMG_0412.png" in lines[0] and "자동차운전면허증" in lines[0]                # 글자 그대로 맞은 사진이 먼저
    assert any("LICENSE" in ln for ln in lines[1:])                                  # 영어 뜻(license)으로 맞은 것은 뒤로


def test_unseen_images_are_counted_and_inspect_looks_at_them(world):
    home, drive, hctx, roots = world
    build(world)
    out = fs.search({"query": "면허증", "roots": [str(drive.parent)], "content": True}, hctx)
    assert "2 images were not looked at yet" in out and "fs.inspect can look at images" in out
    hctx.llm = Seer(yes=((ocr.INSPECT_SIDE, ocr.INSPECT_SIDE // 2),))
    seen_sizes = []
    real_png = ocr.image_png

    def small_png(path, max_side=ocr.MAX_SIDE):
        seen_sizes.append(max_side)
        img = Image.new("L", (max_side, max_side // 2))                             # 1200 -> 896로 줄인 것처럼
        buf = __import__("io").BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue() if "IMG_0412" in str(path) else real_png(path, max_side)

    ocr_image_png, ocr.image_png = ocr.image_png, small_png
    try:
        out = fs.inspect({"question": "Is this a photo of a driver's license?", "roots": [str(drive)], "kind": "image"},
                         hctx)
    finally:
        ocr.image_png = ocr_image_png
    assert "1 of 3 inspected files answered yes" in out and "IMG_0412.png" in out and "자동차운전면허증" in out
    assert "images from the picture itself" in out and "Images: 3 of 3 looked at (2 new" in out      # 아이콘은 못 엶
    assert set(seen_sizes) == {ocr.INSPECT_SIDE}
    before = len(hctx.llm.calls_seen)
    out = fs.inspect({"question": "Is this a photo of a driver's license?", "roots": [str(drive)], "kind": "image"},
                     hctx)
    assert len(hctx.llm.calls_seen) == before and "(0 new, the rest remembered)" in out


def test_all_drives_root_and_default_scope_text(world, monkeypatch):
    home, drive, hctx, roots = world
    monkeypatch.setattr(ci, "all_roots", lambda os_name, h: [str(home), str(drive.parent)])
    hctx.search_default = ["*"]
    out = fs.search({"query": "보고서"}, hctx)
    assert f"scope: {home}, {drive.parent}" in out
    tools = {t.name: t for t in fs.fs_tools(hctx)}
    assert "leave it out to search every drive (C:, Google Drive, ...)" in tools["fs.search"].summary
    assert "set it only to a folder the Researcher named" in tools["fs.search"].summary
    assert "Images are looked at directly" in tools["fs.inspect"].summary
    assert ci.all_roots("linux", str(home))[0] == str(home)                         # 리눅스: 홈 + 붙인 볼륨
    monkeypatch.undo()


def test_walk_follows_folder_links_once(tmp_path):
    root = tmp_path / "r"
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.txt").write_text("x", encoding="utf-8")
    try:
        os.symlink(root / "a", root / "link", target_is_directory=True)
        os.symlink(root, root / "a" / "loop", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    places = Places(os=osinfo.current_os(), home=str(tmp_path), glados_home=str(tmp_path / "gh"), resolve_links=False)
    found = [p.name for p, _ in ci.walk([str(root)], places)]
    assert found == ["x.txt"]                                                        # 링크로 다시 와도 한 번만, 고리에 빠지지 않음


# ---------------------------------------------------------------- 같은 찾기 반복 막기
def test_same_search_twice_in_one_task_is_not_run_again():
    from conftest import base_layers, make_ctx
    from glados.hands import Tool

    runs: list[dict] = []

    def search(args):
        runs.append(args)
        return "0 found"

    tools = ToolRegistry([Tool("fs.search", "find", {"type": "object"}, search)])
    llm = FakeLLM([act("fs.search", query="면허증", roots=["~/Documents"]), act("fs.search", query="license"),
                   act("fs.search", query="면허증", roots=["~/Documents"]), answer("Not found.")])
    result = run_task("find my license", llm=llm, policy=Policy(base_layers()),
                      ctx=make_ctx("linux", registered_tools=frozenset(tools.names())), tools=tools,
                      limits=LoopLimits(), system_prompt="", approve=lambda c, d: "once")
    assert len(runs) == 2 and result.status == "done"
    notes = [str(m.get("content")) for m in result.transcript if "already made this exact call at step 1" in str(m.get("content"))]
    assert notes and "It was not run again" in notes[0]


# ---------------------------------------------------------------- M7.6: 사진을 먼저, 맞는 정도 순, 좁힌 범위 알림
def test_photos_in_your_folders_come_first_for_ocr(tmp_path):
    idx = ci.FileIndex(tmp_path / "gh")
    try:
        mine, app = tmp_path / "Pictures", tmp_path / "Unity" / "Assets"
        for i, (folder, name) in enumerate([(app, "texture.png"), (app, "photo.jpg"), (mine, "scan.png"),
                                            (mine, "IMG_1.jpg")]):
            photo(folder / name, (300, 300))
            os.utime(folder / name, (1000 + i, 1000 + i))
        ci.run_pass(idx, [str(tmp_path / "Pictures"), str(tmp_path / "Unity")],
                    Places(os=osinfo.current_os(), home=str(tmp_path), glados_home=str(tmp_path / "gh"), resolve_links=False))
        first = [ci.file_key(mine) + os.sep]
        order = [Path(p).name for _, p, _, _, _ in idx.needs_ocr(10, first)]
        assert order == ["IMG_1.jpg", "scan.png", "photo.jpg", "texture.png"]     # 내 폴더 > 사진 형식 > 최근
        assert [Path(p).name for _, p, _, _, _ in idx.needs_ocr(10)][:2] == ["IMG_1.jpg", "photo.jpg"]
    finally:
        idx.close()


def test_gpu_work_reads_photos_while_text_is_still_being_read(world):
    _, drive, hctx, roots = world
    build(world)                                                                   # 목록만 (사진은 아직)
    idx = ci.FileIndex(hctx.home)
    stop = ci.threading.Event()
    try:
        worker = ci.threading.Thread(target=ci.gpu_work, args=(idx, None, Seer({1200: LICENSE_PHOTO}), "qwen3.5:4b",
                                                               lambda: False, stop, None, [], 0.05), daemon=True)
        worker.start()
        deadline = time.time() + 20
        while time.time() < deadline and idx.stats()["ocr_left"]:
            time.sleep(0.05)
        assert idx.stats()["ocr_left"] == 0 and idx.match([["면허증"]])
    finally:
        stop.set()
        worker.join(5)
        idx.close()


def test_content_hits_follow_relevance_not_age(tmp_path):
    idx = ci.FileIndex(tmp_path / "gh")
    try:
        old = tmp_path / "old.txt"
        new = tmp_path / "new.txt"
        old.write_text("driver license " * 5 + "x " * 50, encoding="utf-8")
        new.write_text("driver license " + "y " * 58, encoding="utf-8")
        os.utime(old, (1000, 1000))
        a = idx.put(old, old.stat(), "ok", text=old.read_text(encoding="utf-8"))
        b = idx.put(new, new.stat(), "ok", text=new.read_text(encoding="utf-8"))
        assert idx.match([["driver"], ["license"]]) == [a, b]                       # 더 많이 맞는 오래된 것이 먼저
    finally:
        idx.close()


def test_narrowed_search_says_what_was_left_out(world, monkeypatch):
    home, drive, hctx, roots = world
    monkeypatch.setattr(ci, "all_roots", lambda os_name, h: [str(home), str(drive.parent)])
    hctx.search_default = ["*"]
    out = fs.search({"query": "면허증", "roots": ["~/Documents"]}, hctx)
    assert f"Only {home / 'Documents'} was searched. Leave roots out to search every drive ({home}, {drive.parent})" in out
    out = fs.search({"query": "면허증"}, hctx)                                       # 기본 범위면 말하지 않는다
    assert "Leave roots out" not in out

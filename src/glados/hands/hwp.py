"""한글 문서(HWP·HWPX)의 글 (M7.3). 읽기만 한다 (원본은 고치지 않음).

- hwpx: zip 안의 Contents/section0.xml, section1.xml, ... (OWPML). 문단(<hp:p>)마다 글(<hp:t>)을 모은다.
  표 안의 글도 문단이라 같이 나온다. 줄 바꿈(<hp:lineBreak/>)과 탭(<hp:tab/>)은 그대로
- hwp (한글 5.0): OLE 복합 파일(olefile). FileHeader의 속성 (압축, 암호, 배포용), BodyText/Section0, 1, ...
  스트림(압축이면 raw deflate)의 레코드 가운데 문단 글(HWPTAG_PARA_TEXT = 67)을 UTF-16으로 읽는다.
  문단 글 속 조종 문자: 1칸짜리(줄·문단 나눔, 하이픈, 묶음 빈칸)와 8칸짜리(표·그림·각주 같은 개체 자리)를 나눠 건너뛴다
- 암호가 걸린 문서는 locked. 배포용(복사 막음) 문서는 본문이 암호화돼 있어 미리 보기 글(PrvText, 앞부분)만 읽는다
- 한글 3.0 이전 파일은 읽지 않는다 (error)
"""

from __future__ import annotations

import re
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

PARA_TEXT = 67                                   # HWPTAG_BEGIN(16) + 51
ONE_CHAR = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}   # 1칸짜리 조종 문자 (나머지 0~31은 8칸짜리 개체 자리)
SIGNATURE = b"HWP Document File"
COMPRESSED, PASSWORD, DISTRIBUTION = 0x1, 0x2, 0x4
SECTION = re.compile(r"section(\d+)$", re.IGNORECASE)


@dataclass
class HwpText:
    status: str = "ok"            # ok | locked | error
    text: str = ""
    title: str = ""
    detail: str = ""              # 배포용이면 "preview only" (앞부분만)


# ---------------------------------------------------------------------- hwp 5.0
def para_text(payload: bytes) -> str:
    """문단 글 레코드 하나 -> 글. 개체 자리(표·그림 등)는 건너뛴다 (그 안의 글은 따로 문단 레코드로 나온다)."""
    out = bytearray()
    i, n = 0, len(payload) - 1
    while i < n:
        c = payload[i] | (payload[i + 1] << 8)
        if c >= 32:
            out += payload[i:i + 2]
            i += 2
        elif c in ONE_CHAR:
            if c in (10, 13):
                out += "\n".encode("utf-16-le")
            elif c == 24:
                out += "-".encode("utf-16-le")
            elif c in (30, 31):
                out += " ".encode("utf-16-le")
            i += 2
        else:
            if c == 9:
                out += "\t".encode("utf-16-le")
            i += 16                                   # 8칸 (조종 문자 + 개체 정보 + 조종 문자)
    return out.decode("utf-16-le", errors="replace")


def records(data: bytes):
    """(태그, 레벨, 내용) 레코드들. 크기가 0xFFF면 뒤의 4바이트가 실제 크기."""
    i, n = 0, len(data)
    while i + 4 <= n:
        head = int.from_bytes(data[i:i + 4], "little")
        i += 4
        tag, level, size = head & 0x3FF, (head >> 10) & 0x3FF, (head >> 20) & 0xFFF
        if size == 0xFFF:
            if i + 4 > n:
                return
            size = int.from_bytes(data[i:i + 4], "little")
            i += 4
        yield tag, level, data[i:i + size]
        i += size


def _inflate(data: bytes) -> bytes:
    d = zlib.decompressobj(-15)                      # raw deflate. 뒤에 붙은 것은 무시
    return d.decompress(data) + d.flush()


def read_hwp(path: Path, max_chars: int = 400_000) -> HwpText:
    import olefile

    try:
        if not olefile.isOleFile(str(path)):
            head = path.read_bytes()[:32]
            if head.startswith(b"HWP Document File V"):
                return HwpText("error", detail="HWP 3.0 or older is not supported")
            return HwpText("error", detail="not an HWP 5.0 file")
        ole = olefile.OleFileIO(str(path))
    except (OSError, ValueError) as e:
        return HwpText("error", detail=f"{type(e).__name__}: {e}"[:200])
    try:
        if not ole.exists("FileHeader"):
            return HwpText("error", detail="no FileHeader")
        header = ole.openstream("FileHeader").read()
        if not header.startswith(SIGNATURE):
            return HwpText("error", detail="bad signature")
        props = int.from_bytes(header[36:40], "little")
        title = ""
        try:                                         # 문서 정보의 제목 (HwpSummaryInformation, 2 = 제목)
            if ole.exists("\x05HwpSummaryInformation"):
                value = ole.getproperties("\x05HwpSummaryInformation").get(2)
                title = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
        except Exception:
            title = ""
        if props & PASSWORD:
            return HwpText("locked", title=title)
        if props & DISTRIBUTION:                     # 본문이 암호화됨: 미리 보기 글만
            prv = ole.openstream("PrvText").read().decode("utf-16-le", errors="replace") if ole.exists("PrvText") else ""
            prv = prv.replace("\x00", "")
            return HwpText("ok", _tidy(prv)[:max_chars], title, "preview only (distribution document)")
        sections = sorted((int(m.group(1)), "/".join(entry)) for entry in ole.listdir()
                          if len(entry) == 2 and entry[0] == "BodyText" and (m := SECTION.search(entry[1])))
        parts: list[str] = []
        size = 0
        for _, name in sections:
            raw = ole.openstream(name).read()
            data = _inflate(raw) if props & COMPRESSED else raw
            for tag, _, payload in records(data):
                if tag == PARA_TEXT:
                    piece = para_text(payload)
                    parts.append(piece)
                    size += len(piece)
                    if size >= max_chars:
                        break
            if size >= max_chars:
                break
        return HwpText("ok", _tidy("".join(parts))[:max_chars], title)
    except (OSError, ValueError, zlib.error) as e:
        return HwpText("error", detail=f"{type(e).__name__}: {e}"[:200])
    finally:
        ole.close()


# ---------------------------------------------------------------------- hwpx
def read_hwpx(path: Path, max_chars: int = 400_000) -> HwpText:
    from lxml import etree

    try:
        z = zipfile.ZipFile(str(path))
    except (OSError, zipfile.BadZipFile) as e:
        return HwpText("error", detail=f"{type(e).__name__}: {e}"[:200])
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=True)
    try:
        names = sorted(((int(m.group(1)), n) for n in z.namelist()
                        if n.lower().startswith("contents/") and (m := SECTION.search(n.rsplit(".", 1)[0]))
                        and n.lower().endswith(".xml")))
        if not names:
            return HwpText("error", detail="no Contents/section*.xml")
        parts: list[str] = []
        size = 0
        for _, name in names:
            root = etree.fromstring(z.read(name), parser)
            if root is None:
                continue
            for el in root.iter():
                local = etree.QName(el).localname if isinstance(el.tag, str) else ""
                if local == "p" and parts and not parts[-1].endswith("\n"):
                    parts.append("\n")
                elif local == "t":
                    pieces = [el.text or ""]
                    for child in el:
                        kind = etree.QName(child).localname if isinstance(child.tag, str) else ""
                        pieces.append("\n" if kind == "lineBreak" else "\t" if kind == "tab" else "")
                        pieces.append(child.tail or "")
                    piece = "".join(pieces)
                    parts.append(piece)
                    size += len(piece)
            if size >= max_chars:
                break
        return HwpText("ok", _tidy("".join(parts))[:max_chars])
    except (OSError, KeyError, ValueError, etree.LxmlError, zipfile.BadZipFile) as e:
        return HwpText("error", detail=f"{type(e).__name__}: {e}"[:200])
    finally:
        z.close()


def read(path: Path, max_chars: int = 400_000) -> HwpText:
    ext = path.suffix.lower().lstrip(".")
    return read_hwpx(path, max_chars) if ext == "hwpx" else read_hwp(path, max_chars)


def _tidy(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

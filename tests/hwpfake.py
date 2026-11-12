"""시험용 한글 문서 만들기 (M7.3): 아주 작은 OLE 복합 파일(CFB v3)과 그 안의 HWP 5.0, 그리고 HWPX(zip).

진짜 한글 파일을 저장소에 넣지 않으려고 직접 만든다. 스트림은 4096바이트 이상으로 채워 미니 스트림 없이
보통 섹터(512바이트)에 둔다 (읽는 쪽은 olefile이라 미니 스트림도 읽지만, 여기서는 만들기를 간단히).
"""

from __future__ import annotations

import io
import zipfile
import zlib

SECTOR = 512
END, FREE, FATSECT, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF


def _entry(name: str, kind: int, child: int = NOSTREAM, right: int = NOSTREAM, start: int = END, size: int = 0) -> bytes:
    raw = name.encode("utf-16-le") + b"\0\0" if name else b""
    out = raw.ljust(64, b"\0")
    out += (len(raw) if name else 0).to_bytes(2, "little") + bytes([kind, 1])          # 이름 길이, 종류, 검정
    out += NOSTREAM.to_bytes(4, "little") + right.to_bytes(4, "little") + child.to_bytes(4, "little")
    out += b"\0" * 16 + b"\0" * 4 + b"\0" * 16                                          # CLSID, 상태, 시각
    out += start.to_bytes(4, "little") + size.to_bytes(8, "little")
    assert len(out) == 128
    return out


def make_cfb(streams: dict[str, bytes]) -> bytes:
    """{"FileHeader": ..., "BodyText/Section0": ...} -> OLE 파일 바이트. 폴더는 한 단계만."""
    names = list(streams)
    storages = sorted({n.split("/")[0] for n in names if "/" in n})
    entries: list[dict] = [{"name": "Root Entry", "kind": 5, "kids": []}]
    where: dict[str, int] = {}
    for st in storages:
        entries.append({"name": st, "kind": 1, "kids": []})
        where[st] = len(entries) - 1
        entries[0]["kids"].append(len(entries) - 1)
    for n in names:
        data = streams[n].ljust(4096, b"\0")
        entries.append({"name": n.split("/")[-1], "kind": 2, "kids": [], "data": data})
        sid = len(entries) - 1
        entries[where[n.split("/")[0]] if "/" in n else 0]["kids"].append(sid)
    for e in entries:
        e["right"] = NOSTREAM
    for e in entries:
        kids = e["kids"]
        e["child"] = kids[0] if kids else NOSTREAM
        for a, b in zip(kids, kids[1:]):
            entries[a]["right"] = b

    dir_sectors = (len(entries) + 3) // 4
    next_free = 1 + dir_sectors                                                         # 0: FAT, 1..: 디렉터리
    fat = [FATSECT] + [END if i == dir_sectors - 1 else 2 + i for i in range(dir_sectors)]
    body = b""
    for e in entries:
        if e["kind"] != 2:
            continue
        n = len(e["data"]) // SECTOR + (1 if len(e["data"]) % SECTOR else 0)
        e["start"] = next_free
        fat += [next_free + i + 1 for i in range(n - 1)] + [END]
        body += e["data"].ljust(n * SECTOR, b"\0")
        next_free += n
    assert len(fat) <= SECTOR // 4
    fat_bytes = b"".join(x.to_bytes(4, "little") for x in fat).ljust(SECTOR, b"\xff")
    dir_bytes = b"".join(_entry(e["name"], e["kind"], e["child"], e["right"], e.get("start", END),
                                len(e.get("data", b""))) for e in entries)
    dir_bytes = dir_bytes.ljust(dir_sectors * SECTOR, b"\0")
    empty = _entry("", 0)
    dir_bytes = b"".join(dir_bytes[i:i + 128] if i < len(entries) * 128 else empty
                         for i in range(0, dir_sectors * SECTOR, 128))
    header = bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 16
    header += (0x3E).to_bytes(2, "little") + (3).to_bytes(2, "little") + (0xFFFE).to_bytes(2, "little")
    header += (9).to_bytes(2, "little") + (6).to_bytes(2, "little") + b"\0" * 6
    header += (0).to_bytes(4, "little") + (1).to_bytes(4, "little") + (1).to_bytes(4, "little")   # 디렉터리 섹터 수(v3=0), FAT 수, 첫 디렉터리
    header += (0).to_bytes(4, "little") + (4096).to_bytes(4, "little")
    header += END.to_bytes(4, "little") + (0).to_bytes(4, "little") + END.to_bytes(4, "little") + (0).to_bytes(4, "little")
    header += (0).to_bytes(4, "little") + FREE.to_bytes(4, "little") * 108                       # DIFAT: FAT은 0번 섹터
    assert len(header) == SECTOR
    return header + fat_bytes + dir_bytes + body


def record(tag: int, payload: bytes) -> bytes:
    size = len(payload)
    if size >= 0xFFF:
        return (tag | (0xFFF << 20)).to_bytes(4, "little") + size.to_bytes(4, "little") + payload
    return (tag | (size << 20)).to_bytes(4, "little") + payload


TABLE = b"\x0b\x00" + b" lbt" + b"\0" * 8 + b"\x0b\x00"           # 8칸짜리 개체 자리 (표)


def make_hwp(paragraphs: list[str], compressed: bool = True, props: int = 0, preview: str = "",
             table_after: int = -1) -> bytes:
    header = b"HWP Document File".ljust(32, b"\0") + (0x05000300).to_bytes(4, "little")
    header += (int(compressed) | props).to_bytes(4, "little")
    header = header.ljust(256, b"\0")
    body = b""
    for i, text in enumerate(paragraphs):
        body += record(66, b"\0" * 22)                                              # 문단 머리 (읽지 않음)
        payload = text.replace("\n", "\x0a").encode("utf-16-le")
        if i == table_after:
            payload += TABLE
        body += record(67, payload + b"\x0d\x00")
    if compressed:
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        body = c.compress(body) + c.flush()
    streams = {"FileHeader": header, "BodyText/Section0": body}
    if preview:
        streams["PrvText"] = preview.encode("utf-16-le")
    return make_cfb(streams)


HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"


def make_hwpx(sections: list[list[str]]) -> bytes:
    """문단 글 목록을 구역마다. 문단 글 안의 \\n은 <hp:lineBreak/>, | 은 표 칸으로."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        for si, paras in enumerate(sections):
            xml = [f'<hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">']
            for para in paras:
                if "|" in para:
                    cells = "".join(f"<hp:tc><hp:subList><hp:p><hp:run><hp:t>{c}</hp:t></hp:run></hp:p></hp:subList></hp:tc>"
                                    for c in para.split("|"))
                    xml.append(f"<hp:p><hp:run><hp:tbl><hp:tr>{cells}</hp:tr></hp:tbl></hp:run></hp:p>")
                else:
                    text = para.replace("\n", "<hp:lineBreak/>")
                    xml.append(f"<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>")
            xml.append("</hs:sec>")
            z.writestr(f"Contents/section{si}.xml", "".join(xml))
    return buf.getvalue()

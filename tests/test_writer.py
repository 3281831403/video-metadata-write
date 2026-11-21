"""video-metadata-write 的红灯测试。

前 10 条分别钉住 10 个缺陷，后 5 条是组合/边界场景。所有用例只使用标准库
与 pytest，不联网、无随机、不依赖真实媒体文件。
"""

import os
import struct

import pytest

from meta.writer import (
    MetaError,
    atomic_write,
    build_udta,
    find_box,
    read_boxes,
    replace_box,
    strip_metadata,
    validate,
    write_batch,
)


def _box(box_type: bytes, payload: bytes = b"") -> bytes:
    """构造普通 8 字节头的 box。"""
    assert len(box_type) == 4
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def _ext_box(box_type: bytes, total_size: int, payload: bytes) -> bytes:
    """构造 size==1 的 64 位扩展头 box。"""
    assert len(box_type) == 4
    assert total_size == 16 + len(payload)
    return struct.pack(">I", 1) + box_type + struct.pack(">Q", total_size) + payload


# ---------------------------------------------------------------------------
# 10 个缺陷，各 1 条
# ---------------------------------------------------------------------------


def test_box_size_includes_header() -> None:
    """缺陷 1：size 必须包含 8 字节头部。"""
    payload = b"isom\x00\x00\x02\x00"
    boxes = read_boxes(_box(b"ftyp", payload))

    assert len(boxes) == 1
    box = boxes[0]
    assert box["type"] == "ftyp"
    assert box["size"] == 8 + len(payload)
    assert box["header_size"] == 8
    assert box["offset"] == 0
    assert box["payload"] == payload
    assert box["truncated"] is False
    assert box["children"] == []


def test_64bit_extended_size() -> None:
    """缺陷 2：size==1 时用 8 字节 largesize，且头部占 16 字节。"""
    payload = b"\x00" * 16
    data = _ext_box(b"free", 32, payload) + _box(b"ftyp", b"isom")
    boxes = read_boxes(data)

    assert [box["type"] for box in boxes] == ["free", "ftyp"]
    assert boxes[0]["size"] == 32
    assert boxes[0]["header_size"] == 16
    assert boxes[0]["payload"] == payload
    assert boxes[1]["offset"] == 32
    assert boxes[1]["size"] == 12


def test_replace_fixes_parent_size() -> None:
    """缺陷 3：替换子 box 后父 box 的 size 必须同步修正。"""
    old_mvhd = _box(b"mvhd", b"\x00" * 92)
    data = _box(b"ftyp", b"isom") + _box(b"moov", old_mvhd)
    new_mvhd = _box(b"mvhd", b"\x11" * 40)

    out = replace_box(data, ["moov", "mvhd"], new_mvhd)
    boxes = read_boxes(out)

    assert len(out) == len(data) - len(old_mvhd) + len(new_mvhd)
    assert boxes[0]["type"] == "ftyp"
    assert boxes[1]["type"] == "moov"
    assert boxes[1]["size"] == 8 + len(new_mvhd)
    assert boxes[1]["payload"] == new_mvhd
    assert find_box(boxes, ["moov", "mvhd"])["size"] == len(new_mvhd)
    assert validate(out) == []


def test_atomic_write_failure_keeps_original(tmp_path, monkeypatch) -> None:
    """缺陷 4：写一半失败不能损坏原文件，也不能留下临时文件。"""
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"original-bytes")

    def boom(src, dst):
        raise OSError("simulated power loss")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, b"new-bytes")

    assert target.read_bytes() == b"original-bytes"
    assert [path.name for path in tmp_path.iterdir()] == ["clip.mp4"]


def test_utf8_length_prefix_counts_bytes() -> None:
    """缺陷 5：长度前缀是 UTF-8 字节数（中文 6 字节），不是字符数 2。"""
    out = build_udta({"©nam": "标题"})

    assert len(out) == 8 + 8 + 2 + 6
    assert struct.unpack(">I", out[0:4])[0] == len(out)
    assert out[4:8] == b"udta"
    assert struct.unpack(">I", out[8:12])[0] == 16
    assert out[12:16] == b"\xa9nam"
    assert struct.unpack(">H", out[16:18])[0] == 6
    assert out[18:24] == "标题".encode("utf-8")


def test_strip_keeps_moov() -> None:
    """缺陷 6：删元数据不能连带删除 moov / mdat。"""
    ftyp = _box(b"ftyp", b"isom")
    moov = _box(b"moov", _box(b"mvhd", b"\x00" * 20))
    udta = _box(b"udta", b"\xa9nam\x00\x06" + "标题".encode("utf-8"))
    mdat = _box(b"mdat", b"\x01\x02\x03")
    data = ftyp + moov + udta + mdat

    out = strip_metadata(data)

    assert out == ftyp + moov + mdat
    assert [box["type"] for box in read_boxes(out)] == ["ftyp", "moov", "mdat"]


def test_out_of_bounds_box_reported() -> None:
    """缺陷 7：声明长度越界必须报告，不能越界读。"""
    data = struct.pack(">I", 4096) + b"ftyp" + b"\x00" * 8
    problems = validate(data)

    assert any("out of bounds" in problem for problem in problems)


def test_non_ascii_type_reported() -> None:
    """缺陷 8：结构性 box 的 type 非 ASCII 必须报告。"""
    data = struct.pack(">I", 16) + b"\xff\xfe\xfd\xfc" + b"\x00" * 8
    problems = validate(data)

    assert any("non-ASCII" in problem for problem in problems)


def test_batch_rolls_back_all_files(tmp_path) -> None:
    """缺陷 9：批量中途失败必须回滚已写文件。"""
    good = _box(b"ftyp", b"isom") + _box(b"moov", _box(b"mvhd", b"\x00" * 20))
    bad = struct.pack(">I", 2048) + b"junk" + b"\x00" * 8
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(good)
    second.write_bytes(bad)

    with pytest.raises(MetaError):
        write_batch([first, second], {"©nam": "标题"})

    assert first.read_bytes() == good
    assert second.read_bytes() == bad


def test_zero_size_rejected_by_parser() -> None:
    """缺陷 10：size==0 必须抛 MetaError，不能死循环。"""
    data = struct.pack(">I", 0) + b"ftyp"

    with pytest.raises(MetaError, match="size"):
        read_boxes(data)


# ---------------------------------------------------------------------------
# 5 条组合 / 边界
# ---------------------------------------------------------------------------


def test_empty_file_has_no_boxes() -> None:
    """空文件解析为空列表，校验也无问题。"""
    assert read_boxes(b"") == []
    assert validate(b"") == []


def test_single_box_file_roundtrip() -> None:
    """单 box 解析字段正确，find_box 命中，1 字节尾部填充被容忍。"""
    data = _box(b"ftyp", b"isom\x00\x00\x02\x00")
    boxes = read_boxes(data)

    assert len(boxes) == 1
    assert boxes[0]["type"] == "ftyp"
    assert boxes[0]["size"] == len(data)
    assert find_box(boxes, ["ftyp"]) == boxes[0]
    assert find_box(boxes, []) is None
    assert find_box(boxes, ["moov"]) is None
    assert validate(data) == []
    assert validate(data + b"\x00") == []
    assert validate(data + b"\x00\x00") != []


def test_zero_size_validate_raises() -> None:
    """校验遇到 size==0 也必须抛 MetaError（致命结构错误）。"""
    data = struct.pack(">I", 0) + b"moov" + b"\x00" * 8

    with pytest.raises(MetaError, match="size"):
        validate(data)


def test_strip_then_read_only_required() -> None:
    """strip 后再 read，只剩播放必需的 ftyp / moov。"""
    ftyp = _box(b"ftyp", b"isom")
    moov = _box(b"moov", _box(b"trak", b"\x00" * 8))
    meta = _box(b"meta", b"\x00" * 8)
    out = strip_metadata(ftyp + moov + meta)

    assert out == ftyp + moov
    assert [box["type"] for box in read_boxes(out)] == ["ftyp", "moov"]
    assert validate(out) == []


def test_batch_success_preserves_order(tmp_path) -> None:
    """批量全成功时 written 顺序与输入一致，每个文件都带上标签。"""
    base = _box(b"ftyp", b"isom") + _box(b"moov", _box(b"mvhd", b"\x00" * 20))
    first = tmp_path / "one.mp4"
    second = tmp_path / "two.mp4"
    first.write_bytes(base)
    second.write_bytes(base)

    result = write_batch([first, second], {"©nam": "标题", "©cmt": "备注"})

    assert result["count"] == 2
    assert result["written"] == [str(first), str(second)]
    for path in (first, second):
        data = path.read_bytes()
        types = [box["type"] for box in read_boxes(data)]
        assert types[0] == "ftyp"
        assert types.count("moov") == 1
        assert types.count("udta") == 1
        assert "标题".encode("utf-8") in data
        assert validate(data) == []

"""MP4 元数据读写核心。

本模块只依赖标准库（``struct`` / ``json``）。所有函数当前均为桩，调用即抛
``NotImplementedError``；docstring 是必须实现的契约。

术语与约定
----------
* box 头为 ``size(4, 大端) + type(4)``。``size`` 是**含 8 字节头**的总长度。
* ``size == 1`` 表示 64 位扩展头：``size(4) + type(4) + largesize(8)``，
  ``largesize`` 同样含 16 字节头。
* 1 字节容差：文件末尾允许 1 字节填充；box 声明长度比实际可用字节多 1 字节
  仍视为完整。超过 1 字节才算越界。
* 元数据标签使用 UTF-8 文本，长度前缀是**字节数**，不是字符数。
"""

import os
import struct
import tempfile

_HEADER_SIZE = 8
_EXT_HEADER_SIZE = 16
_CONTAINER_TYPES = frozenset({"moov", "trak", "mdia", "minf", "stbl"})
_METADATA_TYPES = frozenset({"udta", "meta"})


class MetaError(Exception):
    """MP4 元数据操作的可预期错误：结构损坏、路径缺失、批量回滚等。"""


def _parse_region(data: bytes, start: int, end: int, strict: bool) -> list[dict]:
    """在 ``data[start:end]`` 区间内解析一串同级 box。

    ``strict`` 为真时用于顶层链：任何结构错误都抛 ``MetaError``；为假时用于
    容器子级：遇到无法继续解析的位置就停止，尽量保留已经解析出的子 box
    （例如占位符 ``moov/trak`` 中的全零 payload）。``offset`` 始终使用
    ``data`` 中的绝对偏移。
    """

    boxes: list[dict] = []
    offset = start

    while offset < end:
        remaining = end - offset

        # 文件（或容器 payload）末尾的 1 字节填充。
        if remaining == 1:
            break
        # 2..7 字节无法构成一个完整头部。
        if remaining < _HEADER_SIZE:
            if strict:
                raise MetaError(f"truncated box header at offset {offset}")
            break

        size = struct.unpack_from(">I", data, offset)[0]
        box_type = data[offset + 4:offset + 8].decode("latin-1")

        # size == 0 会让游标无法推进；绝不能继续循环。
        if size == 0:
            if strict:
                raise MetaError(f"invalid box size 0 at offset {offset}")
            break

        if size == 1:
            # 64 位扩展头：还需要 8 字节 largesize。
            if remaining < _EXT_HEADER_SIZE:
                if strict:
                    raise MetaError(
                        f"truncated 64-bit box header at offset {offset}"
                    )
                break
            header_size = _EXT_HEADER_SIZE
            declared = struct.unpack_from(">Q", data, offset + 8)[0]
            if declared < _EXT_HEADER_SIZE:
                if strict:
                    raise MetaError(
                        f"invalid box size {declared} at offset {offset}"
                    )
                break
        else:
            header_size = _HEADER_SIZE
            declared = size
            if declared < _HEADER_SIZE:
                if strict:
                    raise MetaError(
                        f"invalid box size {declared} at offset {offset}"
                    )
                break

        available = end - offset
        if declared > available:
            # 超过 1 字节才算越界；越界时 payload 截断到区间末尾。
            truncated = declared - available > 1
            box_end = end
        else:
            truncated = False
            box_end = offset + declared

        payload = data[offset + header_size:box_end]

        children: list[dict] = []
        if box_type in _CONTAINER_TYPES:
            # 子级按宽松模式解析，避免占位/损坏的子结构让整棵树不可读。
            children = _parse_region(
                data, offset + header_size, box_end, strict=False
            )

        boxes.append(
            {
                "type": box_type,
                "size": declared,
                "header_size": header_size,
                "offset": offset,
                "payload": payload,
                "truncated": truncated,
                "children": children,
            }
        )

        if truncated:
            break
        offset += declared

    return boxes


def read_boxes(data: bytes) -> list[dict]:
    """把 MP4 ``bytes`` 解析为 box 树。

    解析规则（必须严格遵守）：

    * 循环从偏移 0 前进到文件末尾；每个 box 记录声明总长度 ``size``。
    * ``size`` 含头部 8 字节；当 ``size == 1`` 时头部为 16 字节，总长度取随后
      8 字节大端 ``largesize``（同样含 16 字节头）。
    * ``size == 0`` 无法推进游标，抛 ``MetaError``（消息含 ``size``），不得死循环；
      ``size`` 小于最小合法头部（普通 8、扩展 16）同样抛 ``MetaError``。
    * 文件末尾 1 字节填充忽略；末尾剩 2..7 字节无法构成头部时抛 ``MetaError``。
    * 不越界读取：box 声明长度超出 ``len(data)`` 超过 1 字节时，``payload`` 截断
      到文件末尾、``truncated`` 置 ``True``，由 :func:`validate` 报告；超出 1
      字节以内按容差处理，``truncated`` 为 ``False``。

    返回顶层 box 的 ``list[dict]``，每个 dict 的键固定为：

    * ``type``: ``str``，type 字节用 latin-1 无损解码（非 ASCII 字节原样保留，
      交给 :func:`validate` 报告）。
    * ``size``: ``int``，声明总长度（含头部）。
    * ``header_size``: ``int``，8 或 16。
    * ``offset``: ``int``，在 ``data`` 中的绝对起始偏移。
    * ``payload``: ``bytes``，原始 payload（不含头部）。
    * ``truncated``: ``bool``，声明长度超出文件末尾 1 字节以上。
    * ``children``: ``list[dict]``，仅对容器类型 ``moov/trak/mdia/minf/stbl``
      递归解析，偏移为绝对偏移；其余类型（包括 ``udta``）为 ``[]``。
      ``udta`` 内部是标签数据，类型可含 0xA9，刻意不当作 box 递归。

    空输入返回 ``[]``。函数只读，不得修改 ``data``。
    """

    if not data:
        return []
    return _parse_region(data, 0, len(data), strict=True)


def find_box(boxes: list[dict], path: list[str]) -> dict | None:
    """按路径查找 box，例如 ``["moov", "trak", "mdia"]``。

    ``path`` 的每一项与当前层 box 的 ``type`` 精确匹配，逐层进入 ``children``。
    返回 ``read_boxes`` 结果中的同一个 dict（不复制、不改写）。路径为空、任一层
    缺失或 ``children`` 为空时返回 ``None``。
    """

    if not path:
        return None

    current = boxes
    found: dict | None = None
    for name in path:
        found = next((box for box in current if box["type"] == name), None)
        if found is None:
            return None
        current = found["children"]
    return found


def build_udta(tags: dict[str, str]) -> bytes:
    """由标签 dict 构造一个完整 ``udta`` box 的字节串。

    标签键为 4 字符 atom 名（``"©nam"``、``"©day"``、``"©cmt"`` 等），用
    latin-1 编码后必须恰好 4 字节；值为任意文本。每个标签生成一个子 box：

    ``size(4, 大端) + type(4) + length(2, 大端) + value``

    其中 ``length`` 是 ``value`` 的 **UTF-8 字节数**（中文 3 字节/字，绝不能
    用字符数，也不能截断），``value`` 是 UTF-8 字节。整个返回值是
    ``udta`` 头 + 按 ``tags`` 插入顺序拼接的所有子 box。

    ``tags`` 为空时返回只有 8 字节 ``udta`` 头的 box；键编码后不是 4 字节、
    值不是 ``str`` 时抛 ``MetaError``。
    """

    parts: list[bytes] = []

    for name, value in tags.items():
        if not isinstance(value, str):
            raise MetaError(f"tag value for {name!r} must be a str")

        try:
            name_bytes = name.encode("latin-1")
        except (AttributeError, UnicodeEncodeError) as exc:
            raise MetaError(f"invalid tag name: {name!r}") from exc

        if len(name_bytes) != 4:
            raise MetaError(f"tag name {name!r} must encode to exactly 4 bytes")

        value_bytes = value.encode("utf-8")
        if len(value_bytes) > 0xFFFF:
            raise MetaError(f"tag value for {name!r} is too long")

        child_size = _HEADER_SIZE + 2 + len(value_bytes)
        parts.append(
            struct.pack(">I", child_size)
            + name_bytes
            + struct.pack(">H", len(value_bytes))
            + value_bytes
        )

    payload = b"".join(parts)
    return struct.pack(">I", _HEADER_SIZE + len(payload)) + b"udta" + payload


def _declared_size_of_box(box: bytes) -> int:
    """返回一个完整 box 头部声明的总长度，并校验它与 ``len(box)`` 一致。"""

    if not isinstance(box, (bytes, bytearray, memoryview)):
        raise MetaError("new box must be bytes")
    box = bytes(box)

    if len(box) < _HEADER_SIZE:
        raise MetaError("new box is shorter than 8 bytes")

    size = struct.unpack_from(">I", box, 0)[0]
    if size == 1:
        if len(box) < _EXT_HEADER_SIZE:
            raise MetaError("new box has a truncated 64-bit header")
        declared = struct.unpack_from(">Q", box, 8)[0]
    else:
        declared = size

    if declared != len(box):
        raise MetaError(
            f"new box declares {declared} bytes but is {len(box)} bytes long"
        )
    return declared


def _find_chain(boxes: list[dict], path: list[str]) -> list[dict] | None:
    """沿 ``path`` 返回由外到内的 box 列表（最后一项目标 box）。"""

    if not path:
        return None

    chain: list[dict] = []
    current = boxes
    for name in path:
        match = next((box for box in current if box["type"] == name), None)
        if match is None:
            return None
        chain.append(match)
        current = match["children"]
    return chain


def replace_box(data: bytes, path: list[str], new_box: bytes) -> bytes:
    """把 ``path`` 指向的 box 整体替换为 ``new_box``，并修正所有祖先的 size。

    * ``new_box`` 必须是完整 box：长度至少 8，且其头部声明的总长度等于
      ``len(new_box)``，否则抛 ``MetaError``。
    * ``path`` 不存在（或中间层缺失）抛 ``MetaError``。
    * 用 ``delta = len(new_box) - 旧 box 总长度`` 逐级修正祖先的 size 字段；
      祖先使用 64 位扩展头时改写其 ``largesize``。只移动必要字节，未涉及区域
      原样保留。顶层 box 没有祖先，只替换自身。
    * 返回完整的新的 ``bytes``；替换后必须能被 :func:`read_boxes` 正常解析，
      且父 box 的 ``size`` 等于其头部加所有子 box 的总长度。
    """

    new_size = _declared_size_of_box(new_box)

    boxes = read_boxes(data)
    chain = _find_chain(boxes, path)
    if chain is None:
        raise MetaError(f"box path not found: {path}")

    target = chain[-1]
    old_size = target["size"]
    delta = new_size - old_size

    start = target["offset"]
    end = start + old_size

    out = bytearray(data[:start])
    out.extend(new_box)
    out.extend(data[end:])

    # 祖先都在目标之前，字节位置不受本次增删影响。
    for ancestor in chain[:-1]:
        new_ancestor_size = ancestor["size"] + delta
        if new_ancestor_size < 0:
            raise MetaError(f"replacement makes box {ancestor['type']} too small")

        if ancestor["header_size"] == _EXT_HEADER_SIZE:
            struct.pack_into(">Q", out, ancestor["offset"] + 8, new_ancestor_size)
        else:
            if new_ancestor_size > 0xFFFFFFFF:
                raise MetaError(
                    f"box {ancestor['type']} is too large for a 32-bit size"
                )
            struct.pack_into(">I", out, ancestor["offset"], new_ancestor_size)

    return bytes(out)


def atomic_write(path, data: bytes) -> None:
    """原子地写入文件：任意时刻目标文件要么是旧内容，要么是完整新内容。

    实现必须：在目标文件**同一目录**创建临时文件 → 写入 ``data`` → ``flush``
    并 ``os.fsync`` → 用 ``os.replace`` 覆盖目标。

    任何一步失败（写入、fsync、``os.replace``）都必须删除临时文件并原样抛出
    异常；不得先截断/覆盖目标文件，也不得留下半成品或临时文件。成功返回
    ``None``。``path`` 接受 ``str`` 或 ``os.PathLike``，父目录必须已存在。

    注意：测试会替换 ``os.replace`` 模拟断电，因此实现必须通过模块属性
    ``os.replace`` 调用，不能提前绑定为局部名。
    """

    fspath = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(fspath))

    fd, tmp_path = tempfile.mkstemp(prefix=".mp4-write-", suffix=".part", dir=directory)
    fileobj = None

    try:
        fileobj = os.fdopen(fd, "wb")
        fd = -1  # 现在由 fileobj 负责关闭。
        fileobj.write(data)
        fileobj.flush()
        os.fsync(fileobj.fileno())
        fileobj.close()
        fileobj = None

        # 必须通过 os.replace 属性调用（测试会 monkeypatch 它）。
        os.replace(tmp_path, fspath)
        tmp_path = None
    except BaseException:
        if fileobj is not None:
            try:
                fileobj.close()
            except OSError:
                pass
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
        raise


def strip_metadata(data: bytes) -> bytes:
    """删除元数据 box，其余字节原样、原顺序保留。

    只删除**顶层** ``udta`` 与 ``meta`` box；``ftyp``、``moov``、``mdat``、
    ``free`` 等一律保留，严禁连带删除 ``moov``（否则文件不可播放）。返回保留
    box 的拼接字节；没有元数据时返回与 ``data`` 相同的字节。

    结构非法（如 ``size == 0``）抛 ``MetaError``。替换后的父级长度不需要修正，
    因为删除的是顶层 box。
    """

    boxes = read_boxes(data)

    if not any(box["type"] in _METADATA_TYPES for box in boxes):
        return data

    parts: list[bytes] = []
    covered = 0

    for box in boxes:
        end = min(box["offset"] + box["size"], len(data))
        if end > covered:
            covered = end
        if box["type"] in _METADATA_TYPES:
            continue
        parts.append(data[box["offset"]:end])

    # 保留末尾的 1 字节填充，确保“其余字节原样保留”。
    if covered < len(data):
        parts.append(data[covered:])

    return b"".join(parts)


def validate(data: bytes) -> list[str]:
    """静态校验 MP4 顶层 box 链，返回问题描述列表；全部合法返回 ``[]``。

    检查项与消息要求（问题串形如 ``"box <type> at <offset>: <reason>"``）：

    * ``size == 0``：致命错误，无法推进解析，直接抛 ``MetaError``（消息含
      ``size``），不要返回问题列表。
    * 声明长度超出实际可用字节 **超过 1 字节**：问题串含 ``out of bounds``；
      超出不超过 1 字节属于容差，视为合法。
    * type 不是可打印 ASCII（0x20..0x7E）：问题串含 ``non-ASCII``。
    * 末尾剩 2..7 字节、无法构成 box 头：问题串含 ``truncated header``。
      末尾 1 字节填充忽略。
    * ``size`` 小于最小合法头部（普通 8、扩展 16）：问题串含 ``invalid size``。
    * ``size == 1`` 但剩余不足 16 字节：问题串含 ``truncated header``。

    只校验顶层 box 链，不递归 ``children``；``udta`` 内部标签不参与类型检查。
    函数只读，不修改 ``data``。
    """

    problems: list[str] = []
    total = len(data)
    offset = 0

    while offset < total:
        remaining = total - offset

        if remaining == 1:
            # 末尾 1 字节填充。
            break
        if remaining < _HEADER_SIZE:
            problems.append(f"box ? at {offset}: truncated header")
            break

        size = struct.unpack_from(">I", data, offset)[0]
        raw_type = data[offset + 4:offset + 8]
        box_type = raw_type.decode("latin-1")

        if size == 0:
            raise MetaError(f"invalid box size 0 at offset {offset}")

        if not all(0x20 <= byte <= 0x7E for byte in raw_type):
            problems.append(f"box {box_type} at {offset}: non-ASCII type")

        if size == 1:
            if remaining < _EXT_HEADER_SIZE:
                problems.append(f"box {box_type} at {offset}: truncated header")
                break
            declared = struct.unpack_from(">Q", data, offset + 8)[0]
            if declared < _EXT_HEADER_SIZE:
                problems.append(
                    f"box {box_type} at {offset}: invalid size {declared}"
                )
                break
        else:
            declared = size
            if declared < _HEADER_SIZE:
                problems.append(
                    f"box {box_type} at {offset}: invalid size {declared}"
                )
                break

        if declared > remaining:
            if declared - remaining > 1:
                problems.append(f"box {box_type} at {offset}: out of bounds")
                break
            # 1 字节容差：视为合法，且已无法继续解析。
            offset += declared
            continue

        offset += declared

    return problems


def write_batch(files: list, tags: dict[str, str]) -> dict:
    """批量写入 ``udta`` 标签：全部成功，或全部回滚。

    按 ``files`` 顺序逐个处理：

    1. 读取原字节并 :func:`validate`，有问题（或读取失败）抛 ``MetaError``；
    2. 用 :func:`build_udta` 生成 ``udta``；顶层已有 ``udta`` 则用
       :func:`replace_box` 替换，否则追加到文件末尾；
    3. 用 :func:`atomic_write` 写回。

    先把所有原字节保存在内存。任一文件在校验、写入阶段失败时，必须把**已经
    写入**的文件用 :func:`atomic_write` 恢复成原字节，然后抛 ``MetaError``
    （原始异常置于 ``__cause__``）；不得留下部分写入。

    返回 ``{"written": [str(path), ...], "count": n}``，``written`` 的顺序与
    ``files`` 完全一致，路径用 ``str(path)``。全部成功才返回。
    """

    # 第一遍：读出所有原字节并校验，此时尚未写入任何文件。
    originals: list[tuple[object, bytes]] = []

    for path in files:
        try:
            with open(path, "rb") as handle:
                original = handle.read()
        except OSError as exc:
            raise MetaError(f"failed to read {path}: {exc}") from exc

        problems = validate(original)
        if problems:
            raise MetaError(f"invalid MP4 file {path}: {problems[0]}")

        originals.append((path, original))

    # 标签构造失败时也还没有任何写入。
    udta = build_udta(tags)

    written: list[tuple[object, bytes]] = []

    try:
        for path, original in originals:
            boxes = read_boxes(original)
            if any(box["type"] == "udta" for box in boxes):
                new_data = replace_box(original, ["udta"], udta)
            else:
                new_data = original + udta

            atomic_write(path, new_data)
            written.append((path, original))
    except Exception as exc:
        # 回滚已经成功写入的文件；继续尝试其余文件，尽量恢复全部。
        for path, original in written:
            try:
                atomic_write(path, original)
            except Exception:
                pass
        raise MetaError(f"batch write failed: {exc}") from exc

    return {"written": [str(path) for path, _ in originals], "count": len(originals)}

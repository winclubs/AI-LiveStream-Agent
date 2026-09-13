"""
零依赖 Protocol Buffers 线格式 (wire format) 读写器
用于抖音弹幕 PushFrame → Response → Message 信封的逐级结构化解包，
替代"整帧按 UTF-8 解码再正则猜字段"的脆弱方案。
仅覆盖弹幕链路所需的 wire type: 0 (varint) / 1 (fixed64) / 2 (length-delimited) / 5 (fixed32)，
未知类型按协议规范安全跳过。
"""
from typing import Iterator, List, Optional, Tuple

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5


# ---------------------------------------------------------------------------
# 写入助手 (测试夹具构造与未来鉴权包复用)
# ---------------------------------------------------------------------------

def encode_varint(value: int) -> bytes:
    out = bytearray()
    v = value & 0xFFFFFFFFFFFFFFFF
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field_number: int, wire_type: int) -> bytes:
    return encode_varint((field_number << 3) | wire_type)


def varint(field_number: int, value: int) -> bytes:
    return _tag(field_number, WIRE_VARINT) + encode_varint(value)


def string(field_number: int, value: str) -> bytes:
    raw = (value or "").encode("utf-8")
    return _tag(field_number, WIRE_LEN) + encode_varint(len(raw)) + raw


import builtins

def bytes_field(field_number: int, value: bytes) -> bytes:
    return _tag(field_number, WIRE_LEN) + encode_varint(len(value)) + (value or b"")


def _bytes_compat(*args, **kwargs):
    """兼容包装器：支持 pb.bytes(field_number, value) 序列化，也兼顾内置 bytes(...) 转换"""
    if len(args) == 2 and isinstance(args[0], int):
        return bytes_field(args[0], args[1])
    return builtins.bytes(*args, **kwargs)


# 别名兼容 pb.bytes() 调用
bytes = _bytes_compat


# ---------------------------------------------------------------------------
# 读取助手
# ---------------------------------------------------------------------------

def read_varint(data: bytes, offset: int) -> Optional[Tuple[int, int]]:
    """读取一个 varint，返回 (value, new_offset)；越界返回 None"""
    result = 0
    shift = 0
    start = offset
    while offset < len(data):
        b = data[offset]
        result |= (b & 0x7F) << shift
        offset += 1
        if not (b & 0x80):
            return result, offset
        shift += 7
        if shift > 70 or offset - start > 10:
            return None
    return None


def iter_fields(data: bytes) -> Iterator[Tuple[int, int, object]]:
    """
    遍历 message 顶层字段，yield (field_number, wire_type, value)。
    wire_type 0 -> value=int；wire_type 2 -> value=bytes；其余类型仅跳过不产出。
    遇到损坏数据立即停止 (保证不越界)。
    """
    offset = 0
    n = len(data)
    while offset < n:
        tag = read_varint(data, offset)
        if tag is None:
            return
        tag_value, offset = tag
        field_number = tag_value >> 3
        wire_type = tag_value & 0x07
        if field_number == 0:
            return
        if wire_type == WIRE_VARINT:
            v = read_varint(data, offset)
            if v is None:
                return
            value, offset = v
            yield field_number, wire_type, value
        elif wire_type == WIRE_LEN:
            ln = read_varint(data, offset)
            if ln is None:
                return
            length, offset = ln
            if offset + length > n:
                return
            value = data[offset: offset + length]
            offset += length
            yield field_number, wire_type, value
        elif wire_type == WIRE_FIXED64:
            if offset + 8 > n:
                return
            offset += 8
        elif wire_type == WIRE_FIXED32:
            if offset + 4 > n:
                return
            offset += 4
        else:
            # group (3/4) 或未知类型：无法安全跳过，终止解析
            return


def iter_delimited(data: bytes, field_number: Optional[int] = None) -> Iterator[Tuple[int, bytes]]:
    """枚举指定 (或全部) length-delimited 字段，yield (field_number, bytes) (支持 repeated)"""
    for f, w, v in iter_fields(data):
        if w == WIRE_LEN and (field_number is None or f == field_number):
            yield f, v


def get_varint(data: bytes, field_number: int) -> Optional[int]:
    for f, w, v in iter_fields(data):
        if f == field_number and w == WIRE_VARINT:
            return int(v)
    return None


def get_bytes(data: bytes, field_number: int) -> Optional[bytes]:
    for f, w, v in iter_fields(data):
        if f == field_number and w == WIRE_LEN:
            return v
    return None


def get_string(data: bytes, field_number: int) -> Optional[str]:
    raw = get_bytes(data, field_number)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def iter_strings(data: bytes) -> List[str]:
    """枚举 message 内所有顶层字符串字段 (用于启发式兜底提取)"""
    out = []
    for f, w, v in iter_fields(data):
        if w == WIRE_LEN:
            try:
                s = v.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if s and all(ch.isprintable() for ch in s):
                out.append(s)
    return out

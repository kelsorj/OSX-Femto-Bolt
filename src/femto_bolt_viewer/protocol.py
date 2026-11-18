"""Bounded decoder for the native helper's FBLIVE01 stream."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO

import cv2
import numpy as np

from .depth_modes import get_mode

HEADER = struct.Struct('<8sIIIIIIQQQQ')
MAX_PAYLOAD = 16 * 1024 * 1024


@dataclass(frozen=True)
class Packet:
    kind: int
    payload: bytes
    width: int
    height: int
    sdk_format: int
    sequence: int
    host_monotonic_ns: int
    host_wall_ns: int
    sdk_timestamp_raw: int


def _read_exact(stream: BinaryIO, count: int, *, clean_eof: bool = False) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < count:
        piece = stream.read(count - len(chunks))
        if not piece:
            if not chunks and clean_eof:
                return None
            raise ValueError('Truncated camera packet')
        chunks.extend(piece)
    return bytes(chunks)


def read_packet(stream: BinaryIO, *, mode: str = 'nfov') -> Packet | None:
    geometry = get_mode(mode)
    header = _read_exact(stream, HEADER.size, clean_eof=True)
    if header is None:
        return None
    magic, kind, length, width, height, fmt, flags, seq, mono, wall, sdk = HEADER.unpack(header)
    if magic != b'FBLIVE01' or kind not in (0, 1, 2) or flags != 0:
        raise ValueError('Unsupported native camera packet')
    if not 0 < length <= (65536 if kind == 0 else MAX_PAYLOAD):
        raise ValueError('Camera packet exceeds its size bound')
    if kind == 1 and (width, height, fmt, length) != (
            geometry.transport_width, geometry.transport_height, 8, geometry.transport_bytes):
        raise ValueError('Unsupported raw depth transport')
    if kind == 2 and (not 0 < width <= 4096 or not 0 < height <= 3072 or width * height > 10_000_000):
        raise ValueError('Unsupported RGB dimensions')
    if kind != 0 and (mono == 0 or wall == 0 or seq == 0):
        raise ValueError('Camera frames require counters and host timestamps')
    payload = _read_exact(stream, length)
    assert payload is not None
    return Packet(kind, payload, width, height, fmt, seq, mono, wall, sdk)


def decode_color(packet: Packet) -> bytes:
    """Return RGB-sensor JPEG bytes; infrared is never a color fallback."""
    if packet.kind != 2:
        raise ValueError('RGB preview requires the color sensor')
    width, height = packet.width, packet.height
    if not 0 < width <= 4096 or not 0 < height <= 3072 or width * height > 10_000_000:
        raise ValueError('RGB dimensions exceed the preview bound')
    data = np.frombuffer(packet.payload, dtype=np.uint8)
    if packet.sdk_format == 5:
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None or image.shape != (height, width, 3):
            raise ValueError('RGB JPEG does not match its declared dimensions')
        return packet.payload
    if packet.sdk_format == 0:
        if width % 2 or data.size != width * height * 2:
            raise ValueError('Malformed YUYV color frame')
        image = cv2.cvtColor(data.reshape(height, width, 2), cv2.COLOR_YUV2BGR_YUYV)
    elif packet.sdk_format in (22, 23):
        if data.size != width * height * 3:
            raise ValueError('Malformed color frame')
        image = data.reshape(height, width, 3)
        if packet.sdk_format == 22:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    else:
        raise ValueError(f'Unsupported RGB transport format {packet.sdk_format}')
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError('RGB JPEG encoding failed')
    return encoded.tobytes()

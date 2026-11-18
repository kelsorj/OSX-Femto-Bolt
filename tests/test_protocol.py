"""Native stream parsing, truncation, bounds, and real RGB decoding."""
from __future__ import annotations

import io
from dataclasses import replace

import cv2
import numpy as np
import pytest

from femto_bolt_viewer import protocol
from femto_bolt_viewer.depth_modes import get_mode


def header(**changes):
    fields = dict(magic=b"FBLIVE01", kind=2, length=3, width=1, height=1, fmt=22,
                  flags=0, seq=1, mono=100, wall=100, sdk=0)
    fields.update(changes)
    return protocol.HEADER.pack(*fields.values())


def color_packet(payload, **changes):
    packet = protocol.Packet(2, payload, 2, 2, 23, 1, 100, 100, 0)
    return replace(packet, **changes)


def test_clean_eof_is_distinct_from_truncated_header():
    assert protocol.read_packet(io.BytesIO()) is None
    with pytest.raises(ValueError, match="Truncated"):
        protocol.read_packet(io.BytesIO(header()[:-1]))


def test_truncated_payload_is_not_published_as_a_frame():
    with pytest.raises(ValueError, match="Truncated"):
        protocol.read_packet(io.BytesIO(header() + b"xy"))


@pytest.mark.parametrize("changes", [
    {"magic": b"INVALID!"}, {"kind": 3}, {"flags": 1}, {"length": 0},
    {"length": protocol.MAX_PAYLOAD + 1}, {"kind": 0, "length": 65537},
    {"width": 0}, {"width": 4097}, {"height": 3073}, {"width": 4096, "height": 3072},
    {"seq": 0}, {"mono": 0}, {"wall": 0},
])
def test_invalid_header_is_rejected_before_payload_read(changes):
    stream = io.BytesIO(header(**changes))
    with pytest.raises(ValueError, match="Unsupported|size bound|counters"):
        protocol.read_packet(stream)


@pytest.mark.parametrize("mode", ["nfov", "wfov-binned"])
def test_raw_transport_must_match_selected_mode_exactly(mode):
    selected = get_mode(mode)
    fields = dict(kind=1, width=selected.transport_width, height=selected.transport_height,
                  length=selected.transport_bytes, fmt=8)
    bad = header(**{**fields, "length": selected.transport_bytes - 2})
    with pytest.raises(ValueError, match="raw depth transport"):
        protocol.read_packet(io.BytesIO(bad), mode=mode)


def test_partial_reads_reconstruct_a_complete_packet():
    class Fragmented(io.BytesIO):
        def read(self, count):
            return super().read(min(count, 2))

    packet = protocol.read_packet(Fragmented(header() + b"rgb"))

    assert packet.kind == 2
    assert packet.payload == b"rgb"
    assert packet.sequence == 1


def test_valid_jpeg_passes_through_without_reencoding():
    ok, encoded = cv2.imencode(".jpg", np.full((2, 2, 3), 120, np.uint8))
    assert ok
    data = encoded.tobytes()

    assert protocol.decode_color(color_packet(data, sdk_format=5)) == data


def test_jpeg_dimensions_are_checked_against_declared_sensor_frame():
    _, encoded = cv2.imencode(".jpg", np.full((3, 3, 3), 120, np.uint8))
    with pytest.raises(ValueError, match="declared dimensions"):
        protocol.decode_color(color_packet(encoded.tobytes(), sdk_format=5))


@pytest.mark.parametrize("packet", [
    color_packet(b"", kind=1), color_packet(b"123", sdk_format=22),
    color_packet(b"123", sdk_format=23), color_packet(b"123", sdk_format=0),
    color_packet(bytes(4), sdk_format=0, width=1), color_packet(b"junk", sdk_format=5),
    color_packet(bytes(12), sdk_format=99), color_packet(b"", width=0),
])
def test_malformed_or_non_rgb_payloads_are_refused(packet):
    with pytest.raises(ValueError):
        protocol.decode_color(packet)


@pytest.mark.parametrize("fmt,channels", [(22, (240, 30, 10)), (23, (10, 30, 240))])
def test_rgb_and_bgr_formats_produce_same_visible_red(fmt, channels):
    image = np.full((8, 8, 3), channels, np.uint8)
    result = protocol.decode_color(color_packet(image.tobytes(), width=8, height=8, sdk_format=fmt))
    decoded = cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR)

    assert decoded[4, 4, 2] > 230
    assert decoded[4, 4, 0] < 20

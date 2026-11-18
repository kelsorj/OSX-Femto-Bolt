"""Verified camera calibration and raw input shared by native/local converters.

This module contains no converter, subprocess, container or camera operations.
Its array adapter reproduces the published SDK header skip and copied tail.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
from functools import lru_cache
from pathlib import Path

import numpy as np

from femto_bolt_viewer.depth_modes import DepthMode, get_mode
from femto_bolt_viewer.inspect_capture import (
    MAX_FRAME_BYTES,
    MAX_MANIFEST_BYTES,
    _CaptureReader,
    inspect_capture,
    parse_mipi_header,
    parse_nvram_header,
)


def _verified_manifest(directory: Path, kind: str) -> tuple[dict, bytes]:
    reader = _CaptureReader(directory)
    try:
        original = reader.read('manifest.json', MAX_MANIFEST_BYTES)
        checked = inspect_capture(directory)
        if (not checked['file_integrity_verified'] or not checked['source_capture_complete']
                or checked['capture_kind'] != kind):
            raise ValueError(f'Expected a complete verified {kind} capture')
        if reader.read('manifest.json', MAX_MANIFEST_BYTES) != original:
            raise ValueError('Capture manifest changed during verification')
        return json.loads(original), original
    finally:
        reader.close()


def _read_frame(directory: Path, frame: dict) -> bytes:
    reader = _CaptureReader(directory)
    try:
        data = reader.read(frame['file'], MAX_FRAME_BYTES, frame['bytes'])
        if hashlib.sha256(data).hexdigest() != frame['sha256'].lower():
            raise ValueError('Frame changed after integrity verification')
        return data
    finally:
        reader.close()


def load_unit_calibration(nvram_dir: str | Path, serial: str) -> tuple[bytes, dict]:
    """Require complete verified, consistent calibration reads from this Bolt."""
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError('A nonempty live camera serial is required')
    directory = Path(nvram_dir).resolve(strict=True)
    manifest, original = _verified_manifest(directory, 'opaque_nvram_transport')
    device = manifest.get('device')
    if (not isinstance(device, dict) or device.get('serial') != serial
            or (device.get('vendor_id'), device.get('product_id')) != (0x2BC5, 0x066B)):
        raise ValueError('Saved factory calibration does not match the selected live Femto Bolt')
    blob = None
    for frame in manifest['frames']:
        data = _read_frame(directory, frame)
        envelope = parse_nvram_header(data)
        candidate = data[envelope['blob_offset']:envelope['blob_offset'] + envelope['blob_bytes']]
        if blob is not None and candidate != blob:
            raise ValueError('Repeated factory calibration blobs disagree')
        blob = candidate
    if not blob:
        raise ValueError('No usable factory calibration blob')
    return blob, {'serial': serial, 'calibration_sha256': hashlib.sha256(blob).hexdigest(),
                  'calibration_manifest_sha256': hashlib.sha256(original).hexdigest()}


@lru_cache(maxsize=2)
def _source_mask(mode: DepthMode) -> np.ndarray:
    mask = np.ones((mode.height, mode.width), dtype=bool)
    mask.reshape(-1)[-160:] = False
    mask.flags.writeable = False
    return mask


def _frame_parts(payload: bytes, mode: DepthMode) -> tuple[tuple[memoryview, ...], float, float, np.ndarray]:
    """Return immutable views implementing the SDK's header skip and tail copy."""
    if not isinstance(payload, bytes) or len(payload) != mode.transport_bytes:
        raise ValueError(f'Only an exact {mode.name} transport payload is supported')
    header = parse_mipi_header(payload)
    expected = {'encoding': 'stride2', 'header_revision': 2, 'width': mode.width,
                'height': mode.height, 'mode': mode.engine_mode, 'capture_count': mode.captures,
                'capture_id': 0}
    if any(header.get(key) != value for key, value in expected.items()):
        raise ValueError(f'Unsupported {mode.name} acquisition header')
    if mode.name == 'nfov' and header['output_format_raw'] != 11:
        raise ValueError('Unsupported nfov output format')
    if any(payload[1:320:2]):
        raise ValueError('Unsupported nonzero padding in the stride-2 header')
    copied_from = mode.raw_bytes - mode.height * 2
    view = memoryview(payload)
    parts = (view[320:mode.raw_bytes], view[copied_from:copied_from + 320])
    if mode.name == 'nfov' and np.frombuffer(parts[0], dtype='<u2').max() > 2047:
        raise ValueError('Observed mode-4 codewords exceed the supported eleven-bit field')
    header = payload[:320:2]
    adc = struct.unpack_from('<H', header, 56)[0]
    laser = struct.unpack_from('<i', header, 128)[0] / 1000.0
    slope = np.float32(5.45)
    sensor = float((np.float32(adc & 0xFFF) - (np.float32(1927) - np.float32(20739 >> 8) * slope)) / slope)
    if adc & 0x1000 or any(not math.isfinite(value) or not -50 <= value <= 150 for value in (sensor, laser)):
        raise ValueError('Live frame has unsupported temperature metadata')
    return parts, sensor, laser, _source_mask(mode)


def prepare_live_frame(payload: bytes, mode: str = 'nfov') -> tuple[bytes, float, float, np.ndarray]:
    """Return the exact SDK input, temperatures, and independent source mask."""
    parts, sensor, laser, mask = _frame_parts(payload, get_mode(mode))
    return b''.join(parts), sensor, laser, mask.copy()

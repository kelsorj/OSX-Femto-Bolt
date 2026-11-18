"""Unit-specific thermal correction and field-of-illumination masks.

The CCB record layouts follow the published CAL_TEMP_CORR_BLOCK,
CAL_SPATIAL_TEMP_CORR_BLOCK and CAL_FOI_MASK_BLOCK definitions. All coefficients
come from the supplied camera's blob; no scene measurements or another unit's
tables enter this calculation. The thermal model evaluates the factory cubic
polynomials, then solves the three phase observations by a normalized least
squares inverse. This module handles the prepared 16-bit, nine-capture modes.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass

import numpy as np

from femto_bolt_viewer.depth_modes import get_mode

_MAX_BLOB_BYTES = 2 * 1024 * 1024
_MAX_RECORDS = 1024
_TEMPERATURE_LIMITS = (-50.0, 150.0)


def _temperature(value: float) -> np.float32:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.floating)):
        raise ValueError("Temperature must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("Temperature is outside the supported numerical range") from exc
    if not math.isfinite(number) or not _TEMPERATURE_LIMITS[0] <= number <= _TEMPERATURE_LIMITS[1]:
        raise ValueError("Temperature is outside the supported -50 to 150 C envelope")
    return np.float32(number)


def _fma32(a, b, c):
    # A binary64 intermediate preserves the float32 product and avoids a
    # separate product rounding in these small factory polynomial evaluations.
    return np.float32(np.float64(a) * np.float64(b) + np.float64(c))


def _dot32(a, b):
    result = np.float32(0)
    for left, right in zip(a, b, strict=True):
        result = _fma32(left, right, result)
    return result


@dataclass(frozen=True)
class NativeCalibration:
    """Immutable calibration selected for one unit blob and acquisition mode."""

    mode: str
    calibration_sha256: str
    valid_mask: np.ndarray
    _temperature_coefficients: np.ndarray
    _base_sensor_temperature: np.float32

    def thermal_ubo(self, sensor: float, laser: float) -> bytes:
        """Return the 112-byte little-endian per-frame phase correction block.

        Temperature metadata describes the sensor and the one active laser.
        The second laser input is zero for the supported acquisition modes.
        Invalid temperatures or a singular factory model reject the frame.
        """
        sensor_value, laser_value = _temperature(sensor), _temperature(laser)
        sensor_scaled = np.float32(sensor_value / np.float32(100))
        laser_scaled = np.float32(laser_value / np.float32(100))
        laser_squared = np.float32(laser_scaled * laser_scaled)
        laser_cubed = np.float32(laser_scaled * laser_squared)
        basis = np.array([1, sensor_scaled, laser_scaled, 0, laser_squared, 0, laser_cubed, 0], dtype=np.float32)
        evaluated = self._temperature_coefficients[..., 0].copy()
        for index in range(1, 8):
            evaluated = _fma32(self._temperature_coefficients[..., index], basis[index], evaluated)
        amplitude, phase = evaluated[..., 0], evaluated[..., 1]
        if not np.isfinite(evaluated).all() or np.any(amplitude <= 0):
            raise ValueError("Factory temperature model gives invalid phase amplitudes")
        phase_steps = np.array([0, -2 * math.pi / 3, -4 * math.pi / 3], dtype=np.float32)
        angle = phase_steps[None, :] - phase
        forward = np.stack([amplitude * np.cos(angle), amplitude * np.sin(angle)], axis=-1)
        inverses = np.empty((3, 2, 3), dtype=np.float32)
        for frequency in range(3):
            matrix = forward[frequency]
            g00 = _dot32(matrix[:, 0], matrix[:, 0])
            g01 = _dot32(matrix[:, 0], matrix[:, 1])
            g11 = _dot32(matrix[:, 1], matrix[:, 1])
            determinant = _fma32(g00, g11, -np.float32(g01 * g01))
            if not np.isfinite(determinant) or determinant <= 0:
                raise ValueError("Factory temperature phase matrix is singular")
            gram_inverse = np.array([[g11, -g01], [-g01, g00]], dtype=np.float32) / determinant
            for component in range(2):
                for capture in range(3):
                    inverses[frequency, component, capture] = _dot32(gram_inverse[component], matrix[capture])
        normalization = np.sqrt(_dot32(inverses[0, :, 0], inverses[0, :, 0]))
        if not np.isfinite(normalization) or normalization <= 0:
            raise ValueError("Factory temperature phase normalization is invalid")
        inverses *= np.float32(1) / normalization
        if not np.isfinite(inverses).all():
            raise ValueError("Factory temperature phase inverse is nonfinite")
        result = np.zeros(28, dtype="<f4")
        result[0] = sensor_scaled - np.float32(self._base_sensor_temperature / np.float32(100))
        result[4:16].reshape(3, 4)[:, :3] = inverses[:, 0, :]
        result[16:28].reshape(3, 4)[:, :3] = inverses[:, 1, :]
        return result.tobytes()


def load_native_calibration(blob: bytes, mode: str) -> NativeCalibration:
    """Parse a bounded, complete CCB chain and select the requested mode.

    The complete nine temporal records, three spatial records and one FOI
    record are required. Duplicate identities, inconsistent grids/base
    temperatures and unsupported record revisions fail closed.
    """
    selected = get_mode(mode)
    if not isinstance(blob, bytes) or not 256 <= len(blob) <= _MAX_BLOB_BYTES:
        raise ValueError("Expected a bounded calibration blob")
    if (blob[:4] != bytes([3, 3, 1, 3]) or struct.unpack_from("<I", blob, 8)[0] != len(blob)
            or struct.unpack_from("<I", blob, 20)[0] != 240):
        raise ValueError("Unsupported calibration container header or length")
    temporal = {}
    spatial = {}
    valid_mask = None
    offset = 256
    records = 0
    while offset < len(blob):
        records += 1
        if records > _MAX_RECORDS or offset + 24 > len(blob):
            raise ValueError("Truncated or excessive calibration record chain")
        tag, length = struct.unpack_from("<II", blob, offset)
        if tag >> 16 != 0x4A50 or length < 24 or length % 8 or offset + length > len(blob):
            raise ValueError("Unsupported calibration record header or bounds")
        block, revision = tag & 255, (tag >> 8) & 255
        record = memoryview(blob)[offset:offset + length]
        offset += length
        if block not in (0x54, 0x53, 0x46):
            continue
        if length < 32:
            raise ValueError("Truncated thermal or illumination record")
        if record[24] != selected.engine_mode:
            continue
        if revision != 0:
            raise ValueError("Unsupported thermal or illumination record revision")
        if block == 0x54:
            frequency, capture, laser_id = record[25:28]
            if (length != 96 or not 1 <= capture <= 9 or frequency != (capture - 1) // 3
                    or laser_id != 16 or capture in temporal):
                raise ValueError("Invalid or duplicate temporal calibration identity")
            coefficients = np.frombuffer(record[32:96], dtype="<f4").reshape(2, 8).copy()
            if not np.isfinite(coefficients).all():
                raise ValueError("Nonfinite thermal calibration coefficients")
            temporal[capture] = coefficients
        elif block == 0x53:
            frequency = record[25]
            if length < 144 or frequency not in (0, 1, 2) or frequency in spatial:
                raise ValueError("Invalid or duplicate spatial calibration identity")
            rows, columns = struct.unpack_from("<2H", record, 32)
            if (rows, columns) != (selected.height, selected.width) or length != 144 + columns * 2:
                raise ValueError("Spatial calibration grid differs from the selected mode")
            base = np.frombuffer(record[128:140], dtype="<f4").copy()
            coefficients = np.frombuffer(record[72:128], dtype="<f4")
            if not np.isfinite(coefficients).all():
                raise ValueError("Nonfinite spatial calibration coefficients")
            for value in base:
                _temperature(value)
            spatial[frequency] = base
        else:
            if valid_mask is not None or length < 40:
                raise ValueError("Duplicate or truncated illumination mask")
            rows, columns, row_offset, column_offset = struct.unpack_from("<4H", record, 32)
            if ((rows, columns) != (selected.height, selected.width) or row_offset or column_offset
                    or length != 40 + rows * 4):
                raise ValueError("Illumination mask grid differs from the selected mode")
            extents = np.frombuffer(record[40:], dtype="<i2").reshape(rows, 2)
            empty = np.all(extents == -1, axis=1)
            starts, ends = extents[:, 0], extents[:, 1]
            invalid = ~empty & ((starts < 0) | (ends >= columns) | (starts > ends))
            if np.any(invalid):
                raise ValueError("Illumination row extent is invalid")
            xs = np.arange(columns)
            valid_mask = (~empty[:, None] & (xs >= starts[:, None]) & (xs <= ends[:, None]))
    if len(temporal) != 9 or len(spatial) != 3 or valid_mask is None:
        raise ValueError("Required mode-specific thermal or illumination calibration is missing")
    if not all(np.array_equal(base, spatial[0]) for base in spatial.values()):
        raise ValueError("Spatial calibration base temperatures disagree")
    coefficients = np.stack([temporal[i] for i in range(1, 10)]).reshape(3, 3, 2, 8)
    coefficients.setflags(write=False)
    valid_mask.setflags(write=False)
    return NativeCalibration(mode, hashlib.sha256(blob).hexdigest(), valid_mask, coefficients, spatial[0][0])

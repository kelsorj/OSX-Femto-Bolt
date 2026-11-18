"""Synthetic calibration proves native arithmetic without retained unit data."""

import hashlib
import math
import struct

import numpy as np
import pytest

from femto_bolt_viewer.depth_modes import get_mode
from femto_bolt_viewer.native_calibration import load_native_calibration


def record(kind, length):
    result = bytearray(length)
    struct.pack_into("<II", result, 0, (0x4A50 << 16) | kind, length)
    return result


def blob(records):
    result = bytearray(256)
    result[:4] = bytes([3, 3, 1, 3])
    struct.pack_into("<I", result, 20, 240)
    result.extend(b"".join(records))
    struct.pack_into("<I", result, 8, len(result))
    return bytes(result)


def records(mode="wfov-binned", *, base=20):
    selected = get_mode(mode)
    entries = []
    for capture in range(1, 10):
        entry = record(0x54, 96)
        entry[24:28] = bytes([selected.engine_mode, (capture - 1) // 3, capture, 16])
        struct.pack_into("<f", entry, 32, 1)
        entries.append(entry)
    for frequency in range(3):
        entry = record(0x53, 144 + selected.width * 2)
        entry[24:26] = bytes([selected.engine_mode, frequency])
        struct.pack_into("<H", entry, 26, [19800, 18900, 5400][frequency])
        struct.pack_into("<2H", entry, 32, selected.height, selected.width)
        struct.pack_into("<3f", entry, 128, base, 20, 0)
        entries.append(entry)
    entry = record(0x46, 40 + selected.height * 4)
    entry[24] = selected.engine_mode
    struct.pack_into("<2H", entry, 32, selected.height, selected.width)
    for row in range(selected.height):
        struct.pack_into("<2h", entry, 40 + row * 4, 0, selected.width - 1)
    entries.append(entry)
    return entries


@pytest.mark.parametrize("mode", ["nfov", "wfov-binned"])
def test_ideal_three_phase_model_matches_analytic_inverse(mode):
    data = blob(records(mode))
    calibration = load_native_calibration(data, mode)
    output = np.frombuffer(calibration.thermal_ubo(30, 35), dtype="<f4")
    assert output.size == 28
    assert output[0] == pytest.approx(0.1, abs=3e-8)
    assert np.all(output[1:4] == 0)
    real = output[4:16].reshape(3, 4)
    imag = output[16:28].reshape(3, 4)
    np.testing.assert_allclose(real, np.tile([1, -0.5, -0.5, 0], (3, 1)), atol=2e-7)
    np.testing.assert_allclose(imag, np.tile([0, -math.sqrt(3) / 2, math.sqrt(3) / 2, 0], (3, 1)), atol=2e-7)
    assert calibration.calibration_sha256 == hashlib.sha256(data).hexdigest()
    assert calibration.mode == mode
    assert calibration.valid_mask.shape == (get_mode(mode).height, get_mode(mode).width)
    assert calibration.valid_mask.all()
    assert not calibration.valid_mask.flags.writeable


def test_thermal_cubic_uses_sensor_and_laser_and_all_frequencies():
    entries = records()
    rng = np.random.default_rng(27061)
    coefficients = rng.uniform(-0.2, 0.2, size=(9, 2, 8))
    coefficients[:, 0, 0] += 2
    for entry, values in zip(entries[:9], coefficients, strict=True):
        entry[32:96] = values.astype("<f4").tobytes()
    calibration = load_native_calibration(blob(entries), "wfov-binned")
    for sensor, laser in [(17, 26), (37, 42), (63, 16)]:
        s, t = sensor / 100, laser / 100
        evaluated = coefficients @ np.array([1, s, t, 0, t * t, 0, t ** 3, 0])
        inverses = []
        for group in evaluated.reshape(3, 3, 2):
            a, p = group.T
            angles = -np.arange(3) * 2 * math.pi / 3 - p
            matrix = np.column_stack([a * np.cos(angles), a * np.sin(angles)])
            inverses.append(np.linalg.pinv(matrix))
        inverses = np.array(inverses)
        inverses /= np.linalg.norm(inverses[0, :, 0])
        output = np.frombuffer(calibration.thermal_ubo(sensor, laser), dtype="<f4")
        np.testing.assert_allclose(output[4:16].reshape(3, 4)[:, :3], inverses[:, 0], atol=4e-7)
        np.testing.assert_allclose(output[16:28].reshape(3, 4)[:, :3], inverses[:, 1], atol=4e-7)


def test_illumination_row_endpoints_are_inclusive_and_empty_rows_stay_empty():
    entries = records()
    struct.pack_into("<2h", entries[-1], 40, -1, -1)
    struct.pack_into("<2h", entries[-1], 44, 7, 10)
    struct.pack_into("<2h", entries[-1], 48, 511, 511)
    mask = load_native_calibration(blob(entries), "wfov-binned").valid_mask
    assert not mask[0].any()
    np.testing.assert_array_equal(np.flatnonzero(mask[1]), [7, 8, 9, 10])
    np.testing.assert_array_equal(np.flatnonzero(mask[2]), [511])


@pytest.mark.parametrize("temperature", [math.nan, math.inf, -math.inf, -50.01, 150.01, True, "30", 10 ** 400])
@pytest.mark.parametrize("argument", [0, 1])
def test_bad_frame_temperatures_reject(temperature, argument):
    calibration = load_native_calibration(blob(records()), "wfov-binned")
    temperatures = [30, 35]
    temperatures[argument] = temperature
    with pytest.raises(ValueError, match="Temperature"):
        calibration.thermal_ubo(*temperatures)


@pytest.mark.parametrize("remove", [0, 9, 12])
def test_missing_required_calibration_rejects(remove):
    entries = records()
    del entries[remove]
    with pytest.raises(ValueError, match="missing"):
        load_native_calibration(blob(entries), "wfov-binned")


@pytest.mark.parametrize("duplicate", [0, 9, 12])
def test_duplicate_record_rejects(duplicate):
    entries = records()
    entries.append(entries[duplicate])
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        load_native_calibration(blob(entries), "wfov-binned")


@pytest.mark.parametrize("entry,offset,fmt,value", [
    (0, 25, "B", 2), (0, 26, "B", 0), (0, 27, "B", 1),
    (0, 32, "f", math.nan), (0, 64, "f", math.inf),
    (9, 32, "H", 1), (9, 128, "f", math.nan), (9, 72, "f", math.inf),
    (9, 128, "f", 22), (12, 32, "H", 1), (12, 36, "H", 1),
    (0, 1, "B", 1), (9, 1, "B", 1), (12, 1, "B", 1),
])
def test_malformed_mode_record_rejects(entry, offset, fmt, value):
    entries = records()
    struct.pack_into("<" + fmt, entries[entry], offset, value)
    with pytest.raises(ValueError):
        load_native_calibration(blob(entries), "wfov-binned")


@pytest.mark.parametrize("extent", [(-1, 10), (0, -1), (5, 4), (0, 512), (-2, -2)])
def test_bad_illumination_extent_rejects(extent):
    entries = records()
    struct.pack_into("<2h", entries[-1], 40, *extent)
    with pytest.raises(ValueError, match="extent"):
        load_native_calibration(blob(entries), "wfov-binned")


@pytest.mark.parametrize("offset,fmt,value", [
    (0, "B", 0), (8, "I", 256), (20, "I", 0), (256, "I", 0),
    (260, "I", 0), (260, "I", 97), (260, "I", 2 ** 31),
])
def test_invalid_container_and_record_bounds_reject(offset, fmt, value):
    data = bytearray(blob(records()))
    struct.pack_into("<" + fmt, data, offset, value)
    with pytest.raises(ValueError):
        load_native_calibration(bytes(data), "wfov-binned")


def test_bad_blob_types_truncation_and_unselected_mode_reject():
    data = blob(records())
    for bad in [None, bytearray(data), data[:-1], bytes(255), bytes(2 * 1024 * 1024 + 1)]:
        with pytest.raises(ValueError):
            load_native_calibration(bad, "wfov-binned")
    with pytest.raises(ValueError, match="missing"):
        load_native_calibration(data, "nfov")


def test_zero_amplitude_model_rejects_frame():
    entries = records()
    for entry in entries[:9]:
        struct.pack_into("<f", entry, 32, 0)
    calibration = load_native_calibration(blob(entries), "wfov-binned")
    with pytest.raises(ValueError, match="amplitudes"):
        calibration.thermal_ubo(30, 30)

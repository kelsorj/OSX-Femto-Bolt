"""Freshness, lifecycle and capture/conversion concurrency without hardware."""
from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from femto_bolt_viewer import runtime
from femto_bolt_viewer.protocol import Packet


def packet(kind=1, sequence=1, *, mono=100, wall=2000, payload=b"raw"):
    return Packet(kind, payload, 2, 2, 5 if kind == 2 else 8, sequence,
                  int(mono * 1e9), int(wall * 1e9), 0)


@pytest.fixture
def viewer(tmp_path):
    return runtime.Viewer("SYNTHETIC-UNIT", tmp_path / "nvram", tmp_path / "profile",
                          tmp_path / "capture", tmp_path / "work")


@pytest.fixture
def clock(monkeypatch):
    values = SimpleNamespace(mono=100., wall=2000.)
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: values.mono, time=lambda: values.wall))
    return values


def ready_frames(viewer):
    viewer._connected = viewer._running = True
    viewer._generation = 1
    viewer._depth = (packet(), 100., np.full((512, 512), 420, np.uint16), 2., 1)
    viewer._colors.append((packet(2), 100., "synthetic-rgb"))
    viewer._conversion_times.extend([99.8, 99.9, 100.])


def test_fresh_frames_are_visible_but_do_not_claim_pixel_alignment(viewer, clock):
    ready_frames(viewer)

    result = viewer.snapshot()

    assert result["live"] is True
    assert result["paired"] is True
    assert result["depth_aligned_to_color"] is False
    assert result["depth_fps"] == 10
    assert result["valid_fraction"] == 1
    assert result["color_jpeg"] == "synthetic-rgb"
    depth = cv2.imdecode(np.frombuffer(base64.b64decode(result["depth_jpeg"]), np.uint8), cv2.IMREAD_COLOR)
    assert depth.shape == (512, 512, 3)


def test_status_does_not_encode_images(viewer, clock, monkeypatch):
    ready_frames(viewer)
    encoder = Mock(side_effect=AssertionError("status must not encode"))
    monkeypatch.setattr(runtime, "_jpeg", encoder)

    result = viewer.snapshot(include_images=False)

    assert "color_jpeg" not in result
    assert "depth_jpeg" not in result
    assert result["live"] is True
    encoder.assert_not_called()


@pytest.mark.parametrize("advance_clock", ["mono", "wall"])
def test_either_age_clock_can_expire_cached_frames(viewer, clock, advance_clock):
    ready_frames(viewer)
    setattr(clock, advance_clock, getattr(clock, advance_clock) + 2)

    result = viewer.snapshot()

    assert result["live"] is False
    assert result["color_jpeg"] is None
    assert result["depth_jpeg"] is None
    assert result["depth_fps"] == 0


def test_encoding_race_cannot_return_newly_stale_images(viewer, clock, monkeypatch):
    ready_frames(viewer)

    def slow_encoder(_):
        clock.mono += 2
        return "expired"

    monkeypatch.setattr(runtime, "_jpeg", slow_encoder)

    result = viewer.snapshot()

    assert result["live"] is False
    assert result["color_jpeg"] is None
    assert result["depth_jpeg"] is None


def test_reconnect_during_encoding_cannot_publish_previous_camera_generation(viewer, clock, monkeypatch):
    ready_frames(viewer)

    def reconnect(_):
        viewer._new_generation()
        return "old-generation"

    monkeypatch.setattr(runtime, "_jpeg", reconnect)

    result = viewer.snapshot()

    assert result["connected"] is False
    assert result["live"] is False
    assert result["color_jpeg"] is None
    assert result["depth_jpeg"] is None


def test_nearest_color_is_selected_by_arrival_and_large_offset_is_not_paired(viewer, clock):
    ready_frames(viewer)
    viewer._colors.clear()
    viewer._colors.append((packet(2, 1, mono=99.7), 100., "older"))
    viewer._colors.append((packet(2, 2, mono=99.8), 100., "nearer"))

    result = viewer.snapshot()

    assert result["color_jpeg"] == "nearer"
    assert result["pair_delta_ms"] == 200
    assert result["paired"] is False


def test_pipe_reader_cancels_without_waiting_for_camera_bytes():
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    entered = threading.Event()
    observed = []
    with os.fdopen(read_fd, "rb", buffering=0) as stream:
        reader = runtime._PipeReader(stream, stop)

        def read():
            entered.set()
            try:
                reader.read(10)
            except runtime._Stopped:
                observed.append("cancelled")

        thread = threading.Thread(target=read)
        thread.start()
        assert entered.wait(1)
        stop.set()
        thread.join(1)
        os.close(write_fd)

    assert not thread.is_alive()
    assert observed == ["cancelled"]


def test_capture_uses_latest_only_queue_and_scopes_sudo_to_native_executable(viewer, clock, monkeypatch):
    viewer.use_sudo = True
    process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
    launch = Mock(return_value=process)
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    monkeypatch.setattr(runtime, "_PipeReader", lambda *_: SimpleNamespace(deadline=0))
    monkeypatch.setattr(viewer, "_drain_stderr", lambda *_: None)
    monkeypatch.setattr(viewer, "_finish_capture", lambda *_: True)
    identity = packet(0, payload=json.dumps({"serial": viewer.serial, "depth_mode": viewer.mode.name,
                                           "kind": "live_rgb_raw_depth"}).encode())
    observed = []

    def finish():
        observed.append(viewer._pending[1].sequence)
        raise runtime._Stopped()

    packets = iter([identity, packet(sequence=1), packet(sequence=2)])

    def read(*_, **__):
        return next(packets, None) or finish()

    monkeypatch.setattr(runtime, "read_packet", read)

    viewer._capture_loop()

    assert observed == [2]
    assert viewer.snapshot(include_images=False)["dropped_depth"] == 1
    assert launch.call_args.args[0] == ["/usr/bin/sudo", "-n", str(viewer.capture_bin),
        "--serial", "SYNTHETIC-UNIT", "--fps", "15", "--depth-mode", "wfov-binned"]
    assert launch.call_args.kwargs["stdin"] == runtime.subprocess.DEVNULL
    assert "shell" not in launch.call_args.kwargs


def test_capture_retries_are_bounded_without_hardware(viewer, monkeypatch):
    launch = Mock(side_effect=PermissionError("USB permission denied"))
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    monkeypatch.setattr(runtime, "MAX_CAPTURE_RESTARTS", 1)
    monkeypatch.setattr(viewer._stop, "wait", lambda _: False)

    viewer._capture_loop()

    assert launch.call_count == 2
    result = viewer.snapshot(include_images=False)
    assert result["restarts"] == 1
    assert "2 failed attempts" in result["error"]
    assert result["connected"] is False


def test_live_helper_that_cannot_exit_is_never_reopened(viewer, monkeypatch):
    process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
    launch = Mock(return_value=process)
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    monkeypatch.setattr(runtime, "_PipeReader", lambda *_: SimpleNamespace(deadline=0))
    monkeypatch.setattr(runtime, "read_packet", lambda *_, **__: None)
    monkeypatch.setattr(viewer, "_drain_stderr", lambda *_: None)
    monkeypatch.setattr(viewer, "_finish_capture", lambda *_: False)

    viewer._capture_loop()

    launch.assert_called_once()
    assert "avoid opening the camera twice" in viewer.snapshot(include_images=False)["error"]


@pytest.mark.parametrize("identity", [
    {}, {"backend": "docker"},
    {"backend": "native", "native_macos_depth_engine": True, "depth_mode": "nfov", "native_profile_sha256": "a" * 64},
    {"backend": "native", "native_macos_depth_engine": True, "depth_mode": "wfov-binned", "native_profile_sha256": "invalid"},
])
def test_converter_requires_native_execution_and_exact_mode_identity(viewer, monkeypatch, identity):
    converter = Mock(provenance=identity)
    monkeypatch.setattr(runtime, "NativeDepthConverter", Mock(return_value=converter))
    monkeypatch.setattr(runtime, "MAX_CONVERTER_RESTARTS", 0)

    viewer._convert_loop()

    converter.convert.assert_not_called()
    converter.close.assert_called_once_with()
    result = viewer.snapshot(include_images=False)
    assert result["converter_ready"] is False
    assert "validated mode-specific profile" in result["error"]


@pytest.mark.parametrize("invalid_mask", [np.ones((512, 512), np.uint8), np.ones((2, 2), bool)])
def test_converter_cannot_publish_an_invalid_validity_mask(viewer, monkeypatch, invalid_mask):
    converter = Mock()
    converter.provenance = {"backend": "native", "native_macos_depth_engine": True,
                            "depth_mode": "wfov-binned", "native_profile_sha256": "a" * 64}
    converter.convert.return_value = {"depth_mm": np.full((512, 512), 420, np.uint16),
                                      "valid": invalid_mask, "process_ms": 1.}
    monkeypatch.setattr(runtime, "NativeDepthConverter", Mock(return_value=converter))
    monkeypatch.setattr(runtime, "MAX_CONVERTER_RESTARTS", 0)
    viewer._connected = True
    viewer._generation = 1
    viewer._pending = (1, packet(), time.monotonic())

    viewer._convert_loop()

    assert viewer._depth is None
    result = viewer.snapshot(include_images=False)
    assert result["rejected_depth"] == 1
    assert "validity mask" in result["error"]


def test_converter_sequence_survives_reconnect_and_old_results_are_discarded(viewer, monkeypatch):
    entered_first, entered_second = threading.Event(), threading.Event()
    release_first, release_second = threading.Event(), threading.Event()
    calls = []
    result = {"depth_mm": np.full((512, 512), 420, np.uint16),
              "valid": np.ones((512, 512), bool), "process_ms": 1.}

    def convert(payload, sequence):
        calls.append((payload, sequence))
        if len(calls) == 1:
            entered_first.set()
            assert release_first.wait(2)
        else:
            entered_second.set()
            assert release_second.wait(2)
        return result

    converter = Mock()
    converter.provenance = {"backend": "native", "native_macos_depth_engine": True,
                            "depth_mode": "wfov-binned", "native_profile_sha256": "a" * 64}
    converter.convert.side_effect = convert
    monkeypatch.setattr(runtime, "NativeDepthConverter", Mock(return_value=converter))
    viewer._connected = True
    viewer._generation = 1
    viewer._pending = (1, packet(sequence=50, payload=b"old"), time.monotonic())
    thread = threading.Thread(target=viewer._convert_loop)
    thread.start()
    try:
        assert entered_first.wait(2)
        generation = viewer._new_generation()
        with viewer._condition:
            viewer._connected = True
            viewer._pending = (generation, packet(sequence=1, payload=b"new"), time.monotonic())
            viewer._condition.notify()
        release_first.set()
        assert entered_second.wait(2)
        assert viewer._depth is None
        assert calls == [(b"old", 1), (b"new", 2)]
    finally:
        viewer.close()
        release_first.set()
        release_second.set()
        thread.join(2)

    assert not thread.is_alive()
    converter.close.assert_called_once_with()


@pytest.mark.parametrize("mode,shape", [("wfov-binned", (512, 512, 3)), ("nfov", (576, 640, 3))])
def test_demo_renders_real_jpegs_but_never_claims_live_camera(mode, shape):
    demo = runtime.DemoViewer(mode=mode)
    demo.start()
    result = demo.snapshot()

    assert result["demo"] is True
    assert result["connected"] is False
    assert result["live"] is False
    assert result["converter_ready"] is False
    assert result["depth_fps"] == 0
    color = cv2.imdecode(np.frombuffer(base64.b64decode(result["color_jpeg"]), np.uint8), cv2.IMREAD_COLOR)
    depth = cv2.imdecode(np.frombuffer(base64.b64decode(result["depth_jpeg"]), np.uint8), cv2.IMREAD_COLOR)
    assert color.shape == (720, 1280, 3)
    assert depth.shape == shape
    demo.close()
    assert demo.snapshot()["depth_jpeg"] is None

"""Native macOS/Metal depth conversion with explicit, unit-bound resources.

The private profile contains static calibration and compute programs, never
scene frames. Linux/Docker are not imported or invoked by this runtime.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import platform
import re
import select
import struct
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from femto_bolt_viewer.conversion_backend import FrameRejectedError
from femto_bolt_viewer.conversion_input import load_unit_calibration, prepare_live_frame
from femto_bolt_viewer.depth_modes import get_mode
from femto_bolt_viewer.native_calibration import load_native_calibration

logger = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
READY = struct.Struct('<4sIIII')
REQUEST = struct.Struct('<4sIIQ')
RESPONSE = struct.Struct('<4sIQIIf')
_BUILD_LOCK = threading.Lock()
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_RESOURCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')


def _same_json_value(value, expected):
    """JSON numeric equality must not accept booleans/floats as resource IDs."""
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(_same_json_value(value[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(value) == len(expected) and all(_same_json_value(a, b) for a, b in zip(value, expected, strict=True))
    return value == expected


def _resource_name(value):
    if not isinstance(value, str) or not _RESOURCE.fullmatch(value):
        raise ValueError('Invalid native profile resource path')
    return value


class _CancellationSignal:
    """Observe either caller cancellation or close(), including during build."""

    def __init__(self, closed, external):
        self.closed, self.external = closed, external

    def is_set(self):
        return self.closed.is_set() or self.external is not None and self.external.is_set()


def _bounded_file(path: Path, maximum: int) -> bytes:
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if not data or len(data) > maximum:
        raise ValueError('Native profile resource is empty or exceeds its size limit')
    return data


def validate_native_profile(directory: Path, serial: str, mode: str, calibration_sha256: str) -> tuple[Path, dict, str]:
    """Validate resource identity, sizes, paths, hashes and supported dispatch."""
    selected = get_mode(mode)
    directory = Path(directory).resolve(strict=True)
    if not (directory / 'profile.json').is_file():
        directory = (directory / selected.name).resolve(strict=True)
    original = _bounded_file(directory / 'profile.json', 128 * 1024)
    try:
        profile = json.loads(original)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Invalid native profile JSON') from exc
    required = {'schema_version': 1, 'serial': serial, 'mode': selected.name,
                'width': selected.width, 'height': selected.height,
                'calibration_sha256': calibration_sha256, 'raw_buffer_id': 5,
                'thermal_buffer_id': 2, 'depth_texture_id': 6, 'amplitude_texture_id': 7}
    if not isinstance(profile, dict) or any(not _same_json_value(profile.get(key), value) for key, value in required.items()):
        raise ValueError('Native profile does not match the camera, calibration, mode or supported layout')
    buffers, textures = profile.get('buffers'), profile.get('textures')
    if (not isinstance(buffers, dict) or set(buffers) != {'1', '2', '3', '4', '5'}
            or not isinstance(textures, dict) or set(textures) != {str(i) for i in range(1, 8)}):
        raise ValueError('Native profile resource set is incomplete')
    files = {}
    for key, length in {'1': 16448, '2': 112, '3': 2352, '4': 16, '5': selected.raw_bytes}.items():
        spec = buffers[key]
        if not isinstance(spec, dict) or type(spec.get('length')) is not int or spec['length'] != length:
            raise ValueError('Native buffer size does not match the supported pipeline')
        if key in {'2', '5'}:
            if set(spec) != {'length'}:
                raise ValueError('Native dynamic buffers cannot contain captured frame or temperature data')
        else:
            files[_resource_name(spec.get('file'))] = length
    for key, spec in textures.items():
        intermediate = key == '4'
        expected = {'width': selected.width, 'height': selected.height * (3 if intermediate else 1),
                    'format': 33328 if intermediate else 33322}
        if not isinstance(spec, dict) or any(type(spec.get(k)) is not int or spec[k] != v for k, v in expected.items()):
            raise ValueError('Native texture dimensions or format do not match the mode')
        if key in {'4', '6', '7'}:
            if set(spec) != set(expected):
                raise ValueError('Native intermediate/output textures cannot contain scene images')
        else:
            files[_resource_name(spec.get('file'))] = selected.width * selected.height * 2
    stages = profile.get('stages')
    if not isinstance(stages, list) or len(stages) != 2:
        raise ValueError('Native profile requires two compute stages')
    expected_stages = [
        {'groups': [selected.width * selected.height // 256, 1, 1], 'threads': [256, 1, 1],
         'buffers': [{'id': 5, 'index': 0, 'offset': 0}, {'id': 1, 'index': 1, 'offset': 0}, {'id': 2, 'index': 2, 'offset': 0}],
         'textures': [{'id': 1, 'index': 0}, {'id': 2, 'index': 1}, {'id': 3, 'index': 2}, {'id': 4, 'index': 3}]},
        {'groups': [selected.width // 32, selected.height // 8, 1], 'threads': [32, 8, 1],
         'buffers': [{'id': 3, 'index': 0, 'offset': 0}, {'id': 4, 'index': 1, 'offset': 0}],
         'textures': [{'id': 4, 'index': 0}, {'id': 5, 'index': 1}, {'id': 6, 'index': 2}, {'id': 7, 'index': 3}]},
    ]
    for stage, expected in zip(stages, expected_stages, strict=True):
        if not isinstance(stage, dict) or any(not _same_json_value(stage.get(key), value) for key, value in expected.items()):
            raise ValueError('Unsupported native resource bindings or dispatch dimensions')
        files[_resource_name(stage.get('source'))] = None
    hashes = profile.get('file_sha256')
    if len(files) != 9 or not isinstance(hashes, dict) or set(hashes) != set(files):
        raise ValueError('Native profile must hash each distinct static resource')
    for name, size in files.items():
        path = (directory / name).resolve(strict=True)
        if path.parent != directory:
            raise ValueError('Native profile resource escapes its directory')
        expected_hash = hashes[name]
        if not isinstance(expected_hash, str) or not _SHA.fullmatch(expected_hash):
            raise ValueError('Invalid native profile resource hash')
        data = _bounded_file(path, 8 * 1024 * 1024)
        if size is not None and len(data) != size:
            raise ValueError('Native profile resource length mismatch')
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError('Native profile resource checksum mismatch')
    return directory, profile, hashlib.sha256(original).hexdigest()


def _build_worker(work_dir: Path, cancel_event: threading.Event | _CancellationSignal | None,
                  *, cache_dir: Path | None = None) -> Path:
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('The native depth backend currently requires Apple Silicon macOS')
    source = HERE / 'native_depth_engine.mm'
    fingerprint = hashlib.sha256(source.read_bytes() + platform.mac_ver()[0].encode()).hexdigest()[:24]
    cache = Path(cache_dir) if cache_dir is not None else work_dir.parent / 'native-worker-cache'
    cache.mkdir(parents=True, exist_ok=True)
    output = cache / ('native-depth-' + fingerprint)
    with _BUILD_LOCK:
        if output.is_file() and os.access(output, os.X_OK):
            return output
        temporary = output.with_name(output.name + '.building-%s' % os.getpid())
        log_path = cache / (output.name + '.build.log')
        with log_path.open('wb') as log:
            process = subprocess.Popen(['/usr/bin/xcrun', 'clang++', '-std=c++17', '-fobjc-arc', '-O2',
                                        str(source), '-framework', 'Foundation', '-framework', 'Metal',
                                        '-o', str(temporary)], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            deadline = time.monotonic() + 60
            try:
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError('Native worker build cancelled')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Native worker build timed out')
                    time.sleep(.05)
                if process.returncode:
                    raise RuntimeError('Native worker compilation failed; see %s' % log_path)
                os.replace(temporary, output)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                temporary.unlink(missing_ok=True)
    return output


class NativeDepthConverter:
    """One persistent native worker, bounded frame I/O, no alternate backend."""

    def __init__(self, nvram_dir, serial, sdk_dir, work_dir, *, native_profile_dir,
                 mode='nfov', cancel_event=None, frame_timeout_s=5.0, startup_timeout_s=60.0,
                 native_worker_cache_dir=None):
        del sdk_dir  # The SDK's Linux library is not used by the native runtime.
        if (not math.isfinite(frame_timeout_s) or not 0 < frame_timeout_s <= 60
                or not math.isfinite(startup_timeout_s) or not 0 < startup_timeout_s <= 120):
            raise ValueError('Native conversion timeouts must be finite and bounded')
        self.nvram_dir, self.serial = Path(nvram_dir), serial
        self.work_dir, self.native_profile_dir = Path(work_dir), Path(native_profile_dir)
        self.native_worker_cache_dir = Path(native_worker_cache_dir) if native_worker_cache_dir is not None else None
        self.mode = get_mode(mode)
        self.cancel_event = cancel_event
        self.frame_timeout_s, self.startup_timeout_s = frame_timeout_s, startup_timeout_s
        self.provenance = {}
        self._process = None
        self._stderr = None
        self._calibration = None
        self._sequence = 0
        self._lock, self._state_lock = threading.Lock(), threading.Lock()
        self._closed = threading.Event()

    def _check(self, deadline):
        if self._closed.is_set() or self.cancel_event is not None and self.cancel_event.is_set():
            raise RuntimeError('Native depth operation cancelled')
        if time.monotonic() >= deadline:
            raise TimeoutError('Native depth operation timed out')

    def _write(self, process, data, deadline):
        self._check(deadline)
        view = memoryview(data)
        offset = 0
        descriptor = process.stdin.fileno()
        while offset < len(view):
            self._check(deadline)
            if not select.select([], [descriptor], [], min(.1, max(0, deadline - time.monotonic())))[1]:
                continue
            try:
                size = os.write(descriptor, view[offset:offset + 1024 * 1024])
            except BlockingIOError:
                continue
            if not size:
                raise RuntimeError('Native worker input closed')
            offset += size

    def _read(self, process, count, deadline):
        self._check(deadline)
        chunks = bytearray()
        descriptor = process.stdout.fileno()
        while len(chunks) < count:
            self._check(deadline)
            if not select.select([descriptor], [], [], min(.1, max(0, deadline - time.monotonic())))[0]:
                continue
            try:
                data = os.read(descriptor, min(1024 * 1024, count - len(chunks)))
            except BlockingIOError:
                continue
            if not data:
                raise RuntimeError('Native worker returned truncated output or exited')
            chunks.extend(data)
        return bytes(chunks)

    def start(self):
        with self._lock:
            if self._process is not None:
                raise RuntimeError('Native converter is already started')
            self._closed.clear()
            deadline = time.monotonic() + self.startup_timeout_s
            self._check(deadline)
            blob, calibration_provenance = load_unit_calibration(self.nvram_dir, self.serial)
            directory, profile, profile_hash = validate_native_profile(
                self.native_profile_dir, self.serial, self.mode.name, calibration_provenance['calibration_sha256'])
            self._calibration = load_native_calibration(blob, self.mode.name)
            self.work_dir.mkdir(parents=True, exist_ok=True)
            build_options = {}
            if self.native_worker_cache_dir is not None:
                build_options['cache_dir'] = self.native_worker_cache_dir
            executable = _build_worker(self.work_dir, _CancellationSignal(self._closed, self.cancel_event), **build_options)
            self._check(deadline)
            self._stderr = (self.work_dir / 'native-engine.stderr.log').open('ab')
            try:
                with self._state_lock:
                    self._check(deadline)
                    process = subprocess.Popen([str(executable), str(directory)], stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=self._stderr, bufsize=0)
                    self._process = process
                os.set_blocking(process.stdin.fileno(), False)
                os.set_blocking(process.stdout.fileno(), False)
                ready = READY.unpack(self._read(process, READY.size, deadline))
                if ready != (b'NDR1', self.mode.width, self.mode.height, self.mode.raw_bytes, 112):
                    raise RuntimeError('Native worker readiness dimensions do not match the selected camera mode')
                self._sequence = 0
                self.provenance = {**calibration_provenance, 'backend': 'native',
                    'native_macos_depth_engine': True, 'native_profile_sha256': profile_hash,
                    'native_worker_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
                    'native_calculation': 'Metal compute with factory thermal correction and illumination mask',
                    'depth_mode': self.mode.name, 'architecture': platform.machine(),
                    'profile_provenance': profile.get('provenance', {})}
                logger.info('Native Metal depth engine ready for camera %s, mode %s', self.serial, self.mode.name)
            except Exception as exc:
                self._terminate()
                if self._closed.is_set() or self.cancel_event is not None and self.cancel_event.is_set():
                    raise RuntimeError('Native depth operation cancelled') from exc
                raise

    def convert(self, payload: bytes, sequence: int) -> dict:
        with self._lock:
            process = self._process
            if process is None or self._calibration is None:
                raise RuntimeError('Native depth converter is not started')
            if type(sequence) is not int or not self._sequence < sequence < 2 ** 64:
                raise FrameRejectedError('Native input sequence must be positive and advance')
            started = time.monotonic()
            try:
                raw, sensor, laser, source_mask = prepare_live_frame(payload, self.mode.name)
                thermal = self._calibration.thermal_ubo(sensor, laser)
            except ValueError as exc:
                raise FrameRejectedError(str(exc)) from exc
            prepared = time.monotonic()
            deadline = started + self.frame_timeout_s
            try:
                self._write(process, REQUEST.pack(b'NDP1', len(raw), len(thermal), sequence), deadline)
                self._write(process, raw, deadline)
                self._write(process, thermal, deadline)
                uploaded = time.monotonic()
                magic, status, received, width, height, ms = RESPONSE.unpack(self._read(process, RESPONSE.size, deadline))
                if (magic != b'NDO1' or status != 0 or received != sequence
                        or (width, height) != (self.mode.width, self.mode.height)
                        or not math.isfinite(ms) or ms < 0):
                    raise RuntimeError('Native worker frame identity or dimensions are invalid')
                size = width * height * 2
                depth = np.frombuffer(self._read(process, size, deadline), dtype='<u2').reshape(height, width).copy()
                amplitude = np.frombuffer(self._read(process, size, deadline), dtype='<u2').reshape(height, width).copy()
            except Exception as exc:
                self._terminate()
                if self._closed.is_set() or self.cancel_event is not None and self.cancel_event.is_set():
                    raise RuntimeError('Native depth operation cancelled') from exc
                raise
            downloaded = time.monotonic()
            mask = source_mask & self._calibration.valid_mask
            depth[~mask] = 0
            amplitude[~mask] = 0
            self._sequence = sequence
            return {'depth_mm': depth, 'amplitude': amplitude, 'valid': (depth > 0) & mask,
                    'process_ms': ms, 'encoding': 'native-metal-local',
                    'timings_ms': {'prepare': (prepared - started) * 1000,
                                   'upload': (uploaded - prepared) * 1000,
                                   'engine': ms, 'download_and_wait': (downloaded - uploaded) * 1000,
                                   'total': (time.monotonic() - started) * 1000}}

    def _terminate(self):
        with self._state_lock:
            process, self._process = self._process, None
            stderr, self._stderr = self._stderr, None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
        if stderr is not None:
            stderr.close()

    def close(self):
        self._closed.set()
        self._terminate()

"""Native acquisition and conversion for the standalone browser viewer.

Only the USB helper optionally runs with sudo. Frames remain in bounded memory;
the Python service never records camera images automatically.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import select
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from .depth_display import colorize_depth_mm
from .depth_modes import get_mode
from .native_converter import NativeDepthConverter
from .protocol import decode_color, read_packet

logger = logging.getLogger(__name__)
STALE_SECONDS = 2.0
MAX_CAPTURE_RESTARTS = 5
MAX_CONVERTER_RESTARTS = 3


def _jpeg(image: np.ndarray) -> str:
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError('Preview JPEG encoding failed')
    return base64.b64encode(encoded).decode('ascii')


def _display_limit(max_mm: float) -> float:
    if isinstance(max_mm, bool) or not isinstance(max_mm, (int, float)):
        raise ValueError('Maximum depth display distance must be a number')
    value = float(max_mm)
    if not math.isfinite(value) or not 150 < value <= 100_000:
        raise ValueError('Maximum depth display distance must be greater than 150 and at most 100000 mm')
    return value


class _Stopped(Exception):
    pass


class _PipeReader:
    """Cancelable unbuffered reads; its owning thread alone closes the pipe."""

    def __init__(self, stream, stop: threading.Event):
        self.fd = stream.fileno()
        self.stop = stop
        self.deadline = time.monotonic() + 12.0
        os.set_blocking(self.fd, False)

    def read(self, count: int) -> bytes:
        while not self.stop.is_set():
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Camera helper stopped delivering complete packets')
            if not select.select([self.fd], [], [], min(.1, remaining))[0]:
                continue
            try:
                return os.read(self.fd, min(count, 1024 * 1024))
            except BlockingIOError:
                continue
        raise _Stopped()


class Viewer:
    """One camera, a latest-only raw queue, and a persistent native converter."""

    def __init__(self, serial, nvram, native_profile_dir, capture_bin, work,
                 mode='wfov-binned', use_sudo=False):
        if not isinstance(serial, str) or not serial.strip() or '\x00' in serial:
            raise ValueError('A camera serial is required')
        self.serial = serial
        self.mode = get_mode(mode)
        self.nvram, self.native_profile_dir = Path(nvram), Path(native_profile_dir)
        self.capture_bin, self.work = Path(capture_bin).resolve(), Path(work)
        self.use_sudo = bool(use_sudo)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._encode_lock = threading.Lock()
        self._threads = []
        self._started = False
        self._running = False
        self._connected = False
        self._converter_ready = False
        self._generation = 0
        self._pending = None
        self._colors = deque(maxlen=24)
        self._depth = None
        self._encoded_depth = None
        self._conversion_times = deque(maxlen=45)
        self._converter = None
        self._capture_error = None
        self._converter_error = None
        self._dropped_depth = self._rejected_depth = self._restarts = 0

    def start(self):
        with self._lock:
            if self._started:
                if self._running and not self._stop.is_set():
                    return
                raise RuntimeError('Create a new Viewer after closing it')
            self._started = self._running = True
        self.work.mkdir(parents=True, exist_ok=True)
        self._threads = [threading.Thread(target=self._convert_loop, name='bolt-depth', daemon=True),
                         threading.Thread(target=self._capture_loop, name='bolt-capture', daemon=True)]
        for thread in self._threads:
            thread.start()

    def close(self):
        self._stop.set()
        with self._condition:
            self._running = self._connected = self._converter_ready = False
            self._generation += 1
            self._pending = self._depth = None
            self._colors.clear()
            self._condition.notify_all()
        # The native converter observes stop during startup and I/O. Its owner
        # closes it in finally; closing a reader's buffered pipe here can hang.
        deadline = time.monotonic() + 4.0
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))

    def _new_generation(self):
        with self._condition:
            self._generation += 1
            self._connected = False
            self._pending = self._depth = None
            self._colors.clear()
            self._conversion_times.clear()
            return self._generation

    @staticmethod
    def _drain_stderr(stream, stop, tail):
        try:
            fd = stream.fileno()
            os.set_blocking(fd, False)
            while not stop.is_set():
                if not select.select([fd], [], [], .1)[0]:
                    continue
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not data:
                    break
                tail.append(data.decode('utf-8', errors='replace'))
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    @staticmethod
    def _finish_capture(process):
        # Closing stdout is sufficient even for a root-owned helper: its
        # bounded writes detect the broken pipe and terminate acquisition.
        if process.stdout is not None:
            process.stdout.close()
        try:
            process.wait(timeout=.8)
            return True
        except subprocess.TimeoutExpired:
            pass
        for operation in (process.terminate, process.kill):
            try:
                operation()
                process.wait(timeout=.5)
                return True
            except (PermissionError, ProcessLookupError, subprocess.TimeoutExpired):
                continue
        return process.poll() is not None

    def _capture_loop(self):
        command = [str(self.capture_bin), '--serial', self.serial, '--fps', '15',
                   '--depth-mode', self.mode.name]
        if self.use_sudo:
            command = ['/usr/bin/sudo', '-n', *command]
        for attempt in range(MAX_CAPTURE_RESTARTS + 1):
            if self._stop.is_set():
                return
            generation = self._new_generation()
            process = None
            stderr_stop = threading.Event()
            stderr_thread = None
            stderr_tail = deque(maxlen=4)
            capture_exited = True
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, bufsize=0)
                stderr_thread = threading.Thread(target=self._drain_stderr,
                    args=(process.stderr, stderr_stop, stderr_tail), daemon=True, name='bolt-errors')
                stderr_thread.start()
                stream = _PipeReader(process.stdout, self._stop)
                hello = read_packet(stream, mode=self.mode.name)
                if hello is None or hello.kind != 0:
                    raise ValueError('Camera helper did not identify the selected camera')
                identity = json.loads(hello.payload)
                if (not isinstance(identity, dict) or identity.get('serial') != self.serial
                        or identity.get('depth_mode') != self.mode.name
                        or identity.get('kind') != 'live_rgb_raw_depth'):
                    raise ValueError('Camera helper identity does not match the selected serial and mode')
                with self._lock:
                    if generation != self._generation or self._stop.is_set():
                        return
                    self._connected, self._capture_error = True, None
                counters = {1: 0, 2: 0}
                while not self._stop.is_set():
                    stream.deadline = time.monotonic() + 12.0
                    packet = read_packet(stream, mode=self.mode.name)
                    if packet is None:
                        raise RuntimeError('Camera helper disconnected')
                    if packet.kind == 0 or packet.sequence <= counters[packet.kind]:
                        raise ValueError('Camera packet sequence did not advance')
                    counters[packet.kind] = packet.sequence
                    received = time.monotonic()
                    age = time.time() - packet.host_wall_ns / 1e9
                    if not -1.0 <= age < STALE_SECONDS:
                        if packet.kind == 1:
                            with self._lock:
                                self._rejected_depth += 1
                        continue
                    if packet.kind == 2:
                        color = base64.b64encode(decode_color(packet)).decode('ascii')
                        with self._lock:
                            if generation == self._generation and not self._stop.is_set():
                                self._colors.append((packet, received, color))
                    else:
                        with self._condition:
                            if generation != self._generation or self._stop.is_set():
                                return
                            if self._pending is not None:
                                self._dropped_depth += 1
                            self._pending = (generation, packet, received)
                            self._condition.notify()
            except _Stopped:
                return
            except Exception as exc:
                if not self._stop.is_set():
                    message = str(exc)
                    # The helper's diagnostic includes USB/permission failures;
                    # retain only a bounded tail, never dump binary payloads.
                    if stderr_thread is not None:
                        stderr_thread.join(timeout=.15)
                    detail = ''.join(stderr_tail).strip()[-1200:]
                    if detail:
                        message += ': ' + detail
                    with self._lock:
                        self._capture_error = message
                    logger.warning('Camera capture stopped: %s', message)
            finally:
                with self._condition:
                    if generation == self._generation:
                        self._generation += 1
                        self._connected = False
                        self._pending = self._depth = None
                        self._colors.clear()
                        self._condition.notify_all()
                if process is not None:
                    capture_exited = self._finish_capture(process)
                stderr_stop.set()
                if stderr_thread is not None:
                    stderr_thread.join(timeout=.3)
            if not capture_exited:
                with self._lock:
                    self._capture_error = ('Camera helper did not exit after its output pipe closed. '
                        'Automatic reconnect stopped to avoid opening the camera twice. '
                        'Wait for the helper to exit before restarting the viewer.')
                return
            if attempt == MAX_CAPTURE_RESTARTS:
                with self._lock:
                    self._capture_error = (f'Camera acquisition stopped after {attempt + 1} failed attempts. '
                                           f'Last error: {self._capture_error}')
                return
            if self._stop.wait(min(2 ** attempt, 5)):
                return
            with self._lock:
                self._restarts += 1

    def _convert_loop(self):
        converter = None
        sequence = 0
        failures = 0
        try:
            while not self._stop.is_set():
                if converter is None:
                    try:
                        converter = NativeDepthConverter(self.nvram, self.serial, None, self.work / 'converter',
                            native_profile_dir=self.native_profile_dir, mode=self.mode.name, cancel_event=self._stop)
                        with self._lock:
                            self._converter = converter
                        converter.start()
                        provenance = converter.provenance
                        profile_hash = provenance.get('native_profile_sha256', '')
                        if (provenance.get('backend') != 'native'
                                or provenance.get('native_macos_depth_engine') is not True
                                or provenance.get('depth_mode') != self.mode.name
                                or not isinstance(profile_hash, str) or len(profile_hash) != 64
                                or any(c not in '0123456789abcdef' for c in profile_hash)):
                            raise ValueError('Converter did not establish native execution and a validated mode-specific profile')
                        with self._lock:
                            if not self._stop.is_set():
                                self._converter_ready, self._converter_error = True, None
                    except Exception as exc:
                        if converter is not None:
                            converter.close()
                        converter = None
                        with self._lock:
                            self._converter = None
                            self._converter_ready = False
                            self._converter_error = str(exc)
                        failures += 1
                        if failures > MAX_CONVERTER_RESTARTS:
                            with self._lock:
                                self._converter_error = (f'Depth conversion stopped after {failures} failures. '
                                                         f'Last error: {self._converter_error}')
                            return
                        if self._stop.wait(min(failures, 3)):
                            return
                        continue
                with self._condition:
                    self._condition.wait_for(lambda: self._stop.is_set() or self._pending is not None, timeout=.25)
                    if self._stop.is_set():
                        return
                    pending, self._pending = self._pending, None
                if pending is None:
                    continue
                generation, packet, received = pending
                with self._lock:
                    if generation != self._generation or not self._connected:
                        continue
                if time.monotonic() - received >= STALE_SECONDS:
                    with self._lock:
                        self._dropped_depth += 1
                    continue
                sequence += 1  # Camera counters restart after reconnect; this one never does.
                try:
                    result = converter.convert(packet.payload, sequence)
                    depth = np.asarray(result['depth_mm'])
                    if depth.dtype != np.uint16 or depth.shape != (self.mode.height, self.mode.width):
                        raise ValueError('Native converter returned invalid depth dimensions or dtype')
                    depth = depth.copy()
                    valid = np.asarray(result['valid'])
                    if valid.dtype != np.bool_ or valid.shape != depth.shape:
                        raise ValueError('Native converter returned an invalid validity mask')
                    depth[~valid] = 0
                    depth.setflags(write=False)
                    conversion_ms = float(result.get('timings_ms', {}).get('total', result['process_ms']))
                    if not math.isfinite(conversion_ms) or conversion_ms < 0:
                        raise ValueError('Native converter returned an invalid duration')
                    with self._lock:
                        if (generation == self._generation and self._connected and not self._stop.is_set()
                                and time.monotonic() - received < STALE_SECONDS):
                            self._depth = (packet, received, depth, conversion_ms, generation)
                            self._conversion_times.append(time.monotonic())
                            self._converter_error = None
                except Exception as exc:
                    with self._lock:
                        self._rejected_depth += 1
                        self._converter_error = str(exc)
                        self._depth = None
                    # Frame rejection leaves a healthy worker running. Import
                    # lazily so protocol/demo checks don't construct the engine.
                    from .conversion_backend import FrameRejectedError
                    if isinstance(exc, FrameRejectedError):
                        continue
                    converter.close()
                    converter = None
                    with self._lock:
                        self._converter_ready = False
                        self._converter = None
                    failures += 1
                    if failures > MAX_CONVERTER_RESTARTS:
                        with self._lock:
                            self._converter_error = (f'Depth conversion stopped after {failures} failures. '
                                                     f'Last error: {self._converter_error}')
                        return
                    if self._stop.wait(min(failures, 3)):
                        return
        finally:
            if converter is not None:
                converter.close()
            with self._lock:
                self._converter = None
                self._converter_ready = False

    @staticmethod
    def _age(frame, now, wall):
        if frame is None:
            return None
        return max(0.0, now - frame[1], wall - frame[0].host_wall_ns / 1e9) * 1000

    def snapshot(self, max_mm=800, include_images=True):
        max_mm = _display_limit(max_mm)
        now, wall = time.monotonic(), time.time()
        with self._lock:
            depth = self._depth
            colors = list(self._colors)
            generation = self._generation
            connected = self._connected and not self._stop.is_set()
            times = tuple(self._conversion_times)
            result = {'serial': self.serial, 'mode': self.mode.name, 'demo': False,
                'running': self._running and not self._stop.is_set(), 'connected': connected,
                'error': self._capture_error or self._converter_error,
                'converter_ready': self._converter_ready, 'width': self.mode.width, 'height': self.mode.height,
                'dropped_depth': self._dropped_depth, 'rejected_depth': self._rejected_depth,
                'restarts': self._restarts, 'depth_aligned_to_color': False,
                'minimum_mm': 150, 'maximum_mm': max_mm,
                'hfov_deg': self.mode.hfov_deg, 'vfov_deg': self.mode.vfov_deg}
        depth_age = self._age(depth, now, wall)
        fresh_depth = connected and depth is not None and depth_age < STALE_SECONDS * 1000
        fresh_colors = [c for c in colors if connected and self._age(c, now, wall) < STALE_SECONDS * 1000]
        if fresh_colors and fresh_depth:
            color = min(fresh_colors, key=lambda c: abs(c[0].host_monotonic_ns - depth[0].host_monotonic_ns))
        else:
            color = fresh_colors[-1] if fresh_colors else None
        color_age = self._age(color or (colors[-1] if colors else None), now, wall)
        pair_delta = (abs(color[0].host_monotonic_ns - depth[0].host_monotonic_ns) / 1e6
                      if fresh_depth and color is not None else None)
        recent = [t for t in times if now - t < STALE_SECONDS]
        fps = ((len(recent) - 1) / (recent[-1] - recent[0])
               if fresh_depth and len(recent) > 1 and recent[-1] > recent[0] else 0.0)
        result.update({'depth_fps': round(fps, 2), 'conversion_ms': depth[3] if fresh_depth else None,
            'depth_age_ms': depth_age, 'color_age_ms': color_age, 'pair_delta_ms': pair_delta,
            'color_width': color[0].width if color else None, 'color_height': color[0].height if color else None,
            'valid_fraction': float(np.count_nonzero(depth[2]) / depth[2].size) if fresh_depth else None,
            'depth_sequence': depth[0].sequence if fresh_depth else None,
            'color_sequence': color[0].sequence if color else None,
            'live': bool(fresh_depth and color is not None), 'depth_stale': not fresh_depth,
            'color_stale': color is None, 'paired': pair_delta is not None and pair_delta <= 100})
        if include_images:
            result['color_jpeg'] = color[2] if color else None
            result['depth_jpeg'] = None
            if fresh_depth:
                key = (generation, depth[0].sequence, max_mm)
                with self._encode_lock:
                    if self._encoded_depth is None or self._encoded_depth[0] != key:
                        encoded = _jpeg(colorize_depth_mm(depth[2], 150, max_mm))
                        self._encoded_depth = (key, encoded)
                    result['depth_jpeg'] = self._encoded_depth[1]
        # Encoding runs outside the frame lock. Recheck freshness and connection
        # identity afterwards so a slow HTTP client never receives stale frames.
        checked_now, checked_wall = time.monotonic(), time.time()
        with self._lock:
            changed = generation != self._generation or not self._connected or self._stop.is_set()
        expired_depth = fresh_depth and self._age(depth, checked_now, checked_wall) >= STALE_SECONDS * 1000
        expired_color = color is not None and self._age(color, checked_now, checked_wall) >= STALE_SECONDS * 1000
        if changed:
            result.update(connected=False, live=False, paired=False, depth_stale=True, color_stale=True, depth_fps=0.0)
            if include_images:
                result.update(color_jpeg=None, depth_jpeg=None)
        else:
            if expired_depth:
                result.update(live=False, paired=False, depth_stale=True, depth_fps=0.0)
                if include_images:
                    result['depth_jpeg'] = None
            if expired_color:
                result.update(live=False, paired=False, color_stale=True)
                if include_images:
                    result['color_jpeg'] = None
        return result


class DemoViewer:
    """Clearly labelled deterministic synthetic preview; never opens a camera."""

    def __init__(self, mode='wfov-binned'):
        self.mode = get_mode(mode)
        self.serial = 'DEMO — synthetic, no camera'
        self._running = False
        h, w = self.mode.height, self.mode.width
        y, x = np.mgrid[:h, :w]
        circle = ((x - w / 2) / (w * .48)) ** 2 + ((y - h / 2) / (h * .48)) ** 2 < 1
        self._depth = np.where(circle, 250 + y * 550 / h, 0).astype(np.uint16)
        self._depth[h // 3:2 * h // 3, w // 3:2 * w // 3] = 420
        color = np.full((720, 1280, 3), (32, 25, 20), np.uint8)
        cv2.rectangle(color, (150, 250), (550, 600), (210, 140, 35), -1)
        cv2.circle(color, (900, 400), 170, (70, 170, 220), -1)
        cv2.putText(color, 'DEMO - SYNTHETIC IMAGES', (160, 110), cv2.FONT_HERSHEY_SIMPLEX,
                    1.4, (245, 245, 245), 3, cv2.LINE_AA)
        self._color = _jpeg(color)
        self._cache = None
        self._lock = threading.Lock()

    def start(self):
        self._running = True

    def close(self):
        self._running = False

    def snapshot(self, max_mm=800, include_images=True):
        max_mm = _display_limit(max_mm)
        result = {'serial': self.serial, 'mode': self.mode.name, 'demo': True, 'running': self._running,
            'connected': False, 'live': False, 'error': None, 'converter_ready': False,
            'depth_fps': 0.0, 'conversion_ms': None, 'depth_age_ms': None, 'color_age_ms': None,
            'pair_delta_ms': None, 'width': self.mode.width, 'height': self.mode.height,
            'color_width': 1280, 'color_height': 720,
            'valid_fraction': float(np.count_nonzero(self._depth) / self._depth.size),
            'dropped_depth': 0, 'rejected_depth': 0, 'restarts': 0,
            'depth_aligned_to_color': False, 'depth_stale': not self._running, 'color_stale': not self._running,
            'paired': False, 'minimum_mm': 150, 'maximum_mm': max_mm,
            'hfov_deg': self.mode.hfov_deg, 'vfov_deg': self.mode.vfov_deg}
        if include_images:
            with self._lock:
                if self._cache is None or self._cache[0] != max_mm:
                    self._cache = (max_mm, _jpeg(colorize_depth_mm(self._depth, 150, max_mm)))
                result['color_jpeg'] = self._color if self._running else None
                result['depth_jpeg'] = self._cache[1] if self._running else None
        return result

"""Identity, resource and real-pipe lifecycle checks without camera/vendor data."""
from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from femto_bolt_viewer import native_converter as native
from femto_bolt_viewer.conversion_backend import FrameRejectedError

SERIAL = 'SYNTHETIC-UNIT'
CALIBRATION_SHA = 'a' * 64
WIDTH = HEIGHT = 512
RAW_BYTES = WIDTH * HEIGHT * 18


@pytest.fixture
def profile_directory(tmp_path):
    """Use zero-filled synthetic constants and inert shader text, never a camera profile."""
    directory = tmp_path / 'profiles' / 'wfov-binned'
    directory.mkdir(parents=True)
    files = {'b1.bin': bytes(16448), 'b3.bin': bytes(2352), 'b4.bin': bytes(16),
             't1.bin': bytes(WIDTH * HEIGHT * 2), 't2.bin': bytes(WIDTH * HEIGHT * 2),
             't3.bin': bytes(WIDTH * HEIGHT * 2), 't5.bin': bytes(WIDTH * HEIGHT * 2),
             'stage1.metal': b'// synthetic stage one', 'stage2.metal': b'// synthetic stage two'}
    for name, data in files.items():
        (directory / name).write_bytes(data)
    profile = {
        'schema_version': 1, 'serial': SERIAL, 'mode': 'wfov-binned',
        'width': WIDTH, 'height': HEIGHT, 'calibration_sha256': CALIBRATION_SHA,
        'raw_buffer_id': 5, 'thermal_buffer_id': 2, 'depth_texture_id': 6, 'amplitude_texture_id': 7,
        'buffers': {'1': {'length': 16448, 'file': 'b1.bin'}, '2': {'length': 112},
                    '3': {'length': 2352, 'file': 'b3.bin'}, '4': {'length': 16, 'file': 'b4.bin'},
                    '5': {'length': RAW_BYTES}},
        'textures': {str(i): {'width': WIDTH, 'height': HEIGHT, 'format': 33322,
                             **({'file': f't{i}.bin'} if i in (1, 2, 3, 5) else {})}
                     for i in range(1, 8)},
        'stages': [
            {'source': 'stage1.metal', 'groups': [1024, 1, 1], 'threads': [256, 1, 1],
             'buffers': [{'id': 5, 'index': 0, 'offset': 0}, {'id': 1, 'index': 1, 'offset': 0},
                         {'id': 2, 'index': 2, 'offset': 0}],
             'textures': [{'id': 1, 'index': 0}, {'id': 2, 'index': 1},
                          {'id': 3, 'index': 2}, {'id': 4, 'index': 3}]},
            {'source': 'stage2.metal', 'groups': [16, 64, 1], 'threads': [32, 8, 1],
             'buffers': [{'id': 3, 'index': 0, 'offset': 0}, {'id': 4, 'index': 1, 'offset': 0}],
             'textures': [{'id': 4, 'index': 0}, {'id': 5, 'index': 1},
                          {'id': 6, 'index': 2}, {'id': 7, 'index': 3}]},
        ],
        'file_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    profile['textures']['4'] = {'width': WIDTH, 'height': HEIGHT * 3, 'format': 33328}
    write_profile(directory, profile)
    return directory, profile


def write_profile(directory, profile):
    (directory / 'profile.json').write_text(json.dumps(profile))


def validate(directory):
    return native.validate_native_profile(directory, SERIAL, 'wfov-binned', CALIBRATION_SHA)


def test_valid_profile_and_mode_parent_resolve_same_unit_bound_identity(profile_directory):
    directory, expected = profile_directory
    digest = hashlib.sha256((directory / 'profile.json').read_bytes()).hexdigest()
    assert validate(directory) == (directory, expected, digest)
    assert validate(directory.parent) == (directory, expected, digest)


@pytest.mark.parametrize(('key', 'value'), [
    ('serial', 'DIFFERENT-UNIT'), ('mode', 'nfov'), ('calibration_sha256', 'b' * 64),
    ('schema_version', 2), ('schema_version', True), ('width', 640), ('width', 512.0),
    ('height', 576), ('raw_buffer_id', 2), ('thermal_buffer_id', 2.0),
    ('depth_texture_id', 7), ('amplitude_texture_id', 6),
])
def test_profile_rejects_wrong_identity_or_noninteger_layout(profile_directory, key, value):
    directory, profile = profile_directory
    profile[key] = value
    write_profile(directory, profile)
    with pytest.raises(ValueError, match='match'):
        validate(directory)


@pytest.mark.parametrize('document', ['{', '[]', 'null', 'true', '123', '"profile"'])
def test_profile_rejects_invalid_json_or_root_type(profile_directory, document):
    directory, _ = profile_directory
    (directory / 'profile.json').write_text(document)
    with pytest.raises(ValueError):
        validate(directory)


@pytest.mark.parametrize('filename', [None, [], {}, 42, '../outside.bin', '/tmp/outside.bin', '', '.hidden'])
@pytest.mark.parametrize('location', ['buffer', 'texture', 'stage'])
def test_profile_rejects_resource_filename_types_and_paths(profile_directory, filename, location):
    directory, profile = profile_directory
    resources = {'buffer': (profile['buffers']['1'], 'file'),
                 'texture': (profile['textures']['1'], 'file'),
                 'stage': (profile['stages'][0], 'source')}
    spec, key = resources[location]
    spec[key] = filename
    write_profile(directory, profile)
    with pytest.raises(ValueError):
        validate(directory)


def test_profile_rejects_symlink_outside_profile_directory(profile_directory, tmp_path):
    directory, _ = profile_directory
    path = directory / 'b4.bin'
    external = tmp_path / 'outside.bin'
    external.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(external)
    with pytest.raises(ValueError, match='escapes'):
        validate(directory)


@pytest.mark.parametrize('digest', [None, [], 17, 'A' * 64, 'a' * 63, 'z' * 64])
def test_profile_rejects_invalid_resource_hash(profile_directory, digest):
    directory, profile = profile_directory
    profile['file_sha256']['b4.bin'] = digest
    write_profile(directory, profile)
    with pytest.raises(ValueError, match='hash'):
        validate(directory)


def test_profile_rejects_tampered_static_resource(profile_directory):
    directory, _ = profile_directory
    (directory / 'b4.bin').write_bytes(b'x' * 16)
    with pytest.raises(ValueError, match='checksum'):
        validate(directory)


@pytest.mark.parametrize('size', [0, 15, 17, 8 * 1024 * 1024 + 1])
def test_profile_rejects_empty_wrong_size_or_oversize_resource(profile_directory, size):
    directory, _ = profile_directory
    (directory / 'b4.bin').write_bytes(bytes(size))
    with pytest.raises(ValueError, match='size|length'):
        validate(directory)


@pytest.mark.parametrize('location', ['raw', 'thermal', 'intermediate', 'depth', 'amplitude'])
def test_profile_cannot_smuggle_captured_data_into_dynamic_resources(profile_directory, location):
    directory, profile = profile_directory
    spec = {'raw': profile['buffers']['5'], 'thermal': profile['buffers']['2'],
            'intermediate': profile['textures']['4'], 'depth': profile['textures']['6'],
            'amplitude': profile['textures']['7']}[location]
    spec['file'] = 'b4.bin'
    write_profile(directory, profile)
    with pytest.raises(ValueError, match='cannot contain'):
        validate(directory)


@pytest.mark.parametrize(('key', 'value'), [('groups', [1024, True, 1]), ('threads', [256.0, 1, 1]),
                                         ('groups', [1023, 1, 1]), ('threads', [512, 1, 1])])
def test_profile_rejects_wrong_or_noninteger_dispatch(profile_directory, key, value):
    directory, profile = profile_directory
    profile['stages'][0][key] = value
    write_profile(directory, profile)
    with pytest.raises(ValueError, match='bindings|dispatch'):
        validate(directory)


@pytest.mark.parametrize(('key', 'value'), [('id', 5.0), ('index', False), ('offset', False),
                                         ('id', 4), ('index', 2), ('offset', 4)])
def test_profile_rejects_wrong_or_noninteger_buffer_bindings(profile_directory, key, value):
    directory, profile = profile_directory
    profile['stages'][0]['buffers'][0][key] = value
    write_profile(directory, profile)
    with pytest.raises(ValueError, match='bindings|dispatch'):
        validate(directory)


# A separate process exercises actual nonblocking pipes, framing and process
# cleanup. It intentionally performs no physical depth conversion or calibration.
FAKE_WORKER = '''import pathlib, struct, sys, time
mode = MODE
width = height = 512
raw_bytes = width * height * 18
ready = struct.pack('<4sIIII', b'NDR1', width, height, raw_bytes, 112)
if mode == 'hang-start':
    time.sleep(60)
elif mode == 'short-ready':
    sys.stdout.buffer.write(ready[:7]); sys.stdout.buffer.flush(); sys.exit(0)
elif mode == 'wrong-ready':
    ready = struct.pack('<4sIIII', b'NDR1', 640, 576, raw_bytes, 112)
sys.stdout.buffer.write(ready); sys.stdout.buffer.flush()
while True:
    header = sys.stdin.buffer.read(20)
    if not header: break
    magic, size, thermal_size, sequence = struct.unpack('<4sIIQ', header)
    raw = sys.stdin.buffer.read(size)
    thermal = sys.stdin.buffer.read(thermal_size)
    pathlib.Path(sys.argv[1], 'request-seen').write_text(str(sequence))
    if mode == 'hang-frame': time.sleep(60)
    identity = (b'NDO1', 0, sequence, width, height, 1.25)
    changes = {'wrong-magic': (b'BAD!', 0, sequence, width, height, 1.25),
               'wrong-status': (b'NDO1', 1, sequence, width, height, 1.25),
               'wrong-sequence': (b'NDO1', 0, sequence + 1, width, height, 1.25),
               'wrong-width': (b'NDO1', 0, sequence, 640, height, 1.25),
               'wrong-height': (b'NDO1', 0, sequence, width, 576, 1.25),
               'nan-time': (b'NDO1', 0, sequence, width, height, float('nan')),
               'negative-time': (b'NDO1', 0, sequence, width, height, -1.0)}
    response = struct.pack('<4sIQIIf', *changes.get(mode, identity))
    if mode == 'short-header':
        sys.stdout.buffer.write(response[:8]); sys.stdout.buffer.flush(); break
    sys.stdout.buffer.write(response)
    depth = struct.pack('<H', struct.unpack_from('<H', raw)[0]) * (width * height)
    amplitude = struct.pack('<H', 77) * (width * height)
    if mode == 'short-depth':
        sys.stdout.buffer.write(depth[:12]); sys.stdout.buffer.flush(); break
    sys.stdout.buffer.write(depth)
    if mode == 'short-amplitude':
        sys.stdout.buffer.write(amplitude[:12]); sys.stdout.buffer.flush(); break
    sys.stdout.buffer.write(amplitude); sys.stdout.buffer.flush()
'''


@pytest.fixture
def converter_factory(tmp_path, profile_directory, monkeypatch):
    directory, _ = profile_directory
    source_mask = np.ones((HEIGHT, WIDTH), dtype=bool)
    source_mask[0, 1] = False
    calibration_mask = np.ones((HEIGHT, WIDTH), dtype=bool)
    calibration_mask[0, 2] = False
    thermal_calls = []

    def thermal(sensor, laser):
        thermal_calls.append((sensor, laser))
        return bytes(112)

    calibration = SimpleNamespace(valid_mask=calibration_mask, thermal_ubo=thermal)
    monkeypatch.setattr(native, 'load_unit_calibration',
                        lambda *_: (b'synthetic calibration', {'calibration_sha256': CALIBRATION_SHA}))
    monkeypatch.setattr(native, 'load_native_calibration', lambda *_: calibration)
    monkeypatch.setattr(native, 'prepare_live_frame',
                        lambda payload, _: (payload[:2] + bytes(RAW_BYTES - 2), 24.5, 31.25, source_mask))
    instances = []

    def make(mode='ok', **kwargs):
        worker = tmp_path / f'fake-worker-{len(instances)}'
        worker.write_text(f'#!{sys.executable}\n' + FAKE_WORKER.replace('MODE', repr(mode)))
        worker.chmod(0o700)
        monkeypatch.setattr(native, '_build_worker', lambda *_: worker)
        converter = native.NativeDepthConverter(tmp_path / 'nvram', SERIAL, tmp_path / 'absent-sdk',
                                                tmp_path / 'work', native_profile_dir=directory,
                                                mode='wfov-binned', **kwargs)
        instances.append(converter)
        return converter, thermal_calls

    yield make
    for converter in instances:
        converter.close()


def test_native_conversion_uses_frame_temperatures_masks_both_planes_and_keeps_provenance(converter_factory):
    converter, thermal_calls = converter_factory()
    converter.start()
    first = converter.convert(struct.pack('<H', 123), 7)
    second = converter.convert(struct.pack('<H', 456), 2**64 - 1)
    assert first['depth_mm'].shape == (HEIGHT, WIDTH)
    np.testing.assert_array_equal(first['depth_mm'][0, :4], [123, 0, 0, 123])
    np.testing.assert_array_equal(first['amplitude'][0, :4], [77, 0, 0, 77])
    np.testing.assert_array_equal(first['valid'][0, :4], [True, False, False, True])
    assert second['depth_mm'][10, 10] == 456
    assert first['depth_mm'][10, 10] == 123  # Output owns its storage across frames.
    assert thermal_calls == [(24.5, 31.25), (24.5, 31.25)]
    assert first['encoding'] == 'native-metal-local'
    assert first['process_ms'] == 1.25
    assert converter.provenance['backend'] == 'native'
    assert converter.provenance['native_macos_depth_engine'] is True
    assert len(converter.provenance['native_profile_sha256']) == 64
    assert len(converter.provenance['native_worker_sha256']) == 64
    assert converter.provenance['calibration_sha256'] == CALIBRATION_SHA


@pytest.mark.parametrize('sequence', [0, -1, True, 1.0, '1', None, 2**64])
def test_invalid_input_sequences_do_not_break_healthy_worker(converter_factory, sequence):
    converter, _ = converter_factory()
    converter.start()
    with pytest.raises(FrameRejectedError, match='sequence'):
        converter.convert(b'\x01\x00', sequence)
    assert converter.convert(b'\x02\x00', 1)['depth_mm'][1, 1] == 2


def test_repeated_sequence_is_rejected_without_consuming_next_frame(converter_factory):
    converter, _ = converter_factory()
    converter.start()
    converter.convert(b'\x01\x00', 5)
    with pytest.raises(FrameRejectedError, match='sequence'):
        converter.convert(b'\x02\x00', 5)
    assert converter.convert(b'\x03\x00', 6)['depth_mm'][1, 1] == 3


def test_bad_input_frame_is_rejected_without_terminating_worker(converter_factory, monkeypatch):
    converter, _ = converter_factory()
    converter.start()
    process = converter._process

    def reject(*_):
        raise ValueError('bad source frame')

    monkeypatch.setattr(native, 'prepare_live_frame', reject)
    with pytest.raises(FrameRejectedError, match='bad source frame'):
        converter.convert(b'bad', 1)
    assert process.poll() is None


@pytest.mark.parametrize('mode', ['wrong-magic', 'wrong-status', 'wrong-sequence', 'wrong-width',
                                 'wrong-height', 'nan-time', 'negative-time',
                                 'short-header', 'short-depth', 'short-amplitude'])
def test_bad_worker_response_closes_process_and_never_falls_back(converter_factory, mode):
    converter, _ = converter_factory(mode)
    converter.start()
    process, stderr = converter._process, converter._stderr
    with pytest.raises(RuntimeError, match='invalid|truncated'):
        converter.convert(b'\x01\x00', 1)
    assert process.poll() is not None
    assert process.stdin.closed and process.stdout.closed and stderr.closed
    with pytest.raises(RuntimeError, match='not started'):
        converter.convert(b'\x01\x00', 2)


@pytest.mark.parametrize('mode', ['wrong-ready', 'short-ready', 'hang-start'])
def test_failed_start_cleans_child_and_log(converter_factory, monkeypatch, mode):
    converter, _ = converter_factory(mode, startup_timeout_s=.25)
    launched = []
    popen = subprocess.Popen

    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        launched.append((process, kwargs['stderr']))
        return process

    monkeypatch.setattr(native.subprocess, 'Popen', record)
    with pytest.raises((RuntimeError, TimeoutError), match='dimensions|truncated|timed out'):
        converter.start()
    assert len(launched) == 1
    process, stderr = launched[0]
    assert process.poll() is not None
    assert process.stdin.closed and process.stdout.closed and stderr.closed


def test_frame_timeout_terminates_unresponsive_worker(converter_factory):
    converter, _ = converter_factory('hang-frame', frame_timeout_s=.2)
    converter.start()
    process = converter._process
    with pytest.raises(TimeoutError, match='timed out'):
        converter.convert(b'\x01\x00', 1)
    assert process.poll() is not None


def test_precancelled_start_never_launches_child(converter_factory, monkeypatch):
    cancel = threading.Event()
    cancel.set()
    converter, _ = converter_factory(cancel_event=cancel)

    def forbidden(*args, **kwargs):
        pytest.fail('A pre-cancelled start must not create a process')

    monkeypatch.setattr(native.subprocess, 'Popen', forbidden)
    with pytest.raises(RuntimeError, match='cancelled'):
        converter.start()


def test_cancelled_frame_terminates_worker(converter_factory):
    cancel = threading.Event()
    converter, _ = converter_factory(cancel_event=cancel)
    converter.start()
    process = converter._process
    cancel.set()
    with pytest.raises(RuntimeError, match='cancelled'):
        converter.convert(b'\x01\x00', 1)
    assert process.poll() is not None


def test_close_is_idempotent_and_reaps_worker(converter_factory):
    converter, _ = converter_factory()
    converter.start()
    process, stderr = converter._process, converter._stderr
    converter.close()
    converter.close()
    assert process.poll() is not None
    assert process.stdin.closed and process.stdout.closed and stderr.closed
    with pytest.raises(RuntimeError, match='not started'):
        converter.convert(b'\x01\x00', 1)


def test_double_start_cannot_leak_first_worker(converter_factory):
    converter, _ = converter_factory()
    converter.start()
    process = converter._process
    with pytest.raises(RuntimeError, match='already started'):
        converter.start()
    assert converter._process is process
    assert converter.convert(b'\x01\x00', 1)['depth_mm'][1, 1] == 1


def test_close_during_build_reaches_builder_cancellation_and_prevents_start(converter_factory, monkeypatch):
    converter, _ = converter_factory()
    building = threading.Event()
    continue_build = threading.Event()
    cancellation_seen = threading.Event()
    failures = []

    def build(_directory, cancel_event):
        building.set()
        assert cancel_event is not None
        assert continue_build.wait(2)
        assert cancel_event.is_set(), 'close() never reached the compiler cancellation token'
        cancellation_seen.set()
        raise RuntimeError('Native worker build cancelled')

    def start():
        try:
            converter.start()
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(native, '_build_worker', build)
    thread = threading.Thread(target=start, daemon=True)
    thread.start()
    assert building.wait(2)
    converter.close()
    continue_build.set()
    thread.join(3)
    assert not thread.is_alive()
    assert cancellation_seen.is_set()
    assert len(failures) == 1 and isinstance(failures[0], RuntimeError)
    assert 'cancelled' in str(failures[0])


def test_close_interrupts_waiting_conversion_and_reaps_child(converter_factory, monkeypatch):
    converter, _ = converter_factory('hang-frame')
    converter.start()
    process = converter._process
    reading = threading.Event()
    resume = threading.Event()
    failures = []
    read = converter._read

    def synchronized_read(*args):
        reading.set()
        assert resume.wait(2)
        return read(*args)

    def convert():
        try:
            converter.convert(b'\x01\x00', 1)
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(converter, '_read', synchronized_read)
    thread = threading.Thread(target=convert, daemon=True)
    thread.start()
    assert reading.wait(2)
    converter.close()
    resume.set()
    thread.join(3)
    assert not thread.is_alive()
    assert process.poll() is not None
    assert process.stdin.closed and process.stdout.closed
    assert len(failures) == 1 and isinstance(failures[0], RuntimeError)
    assert 'cancelled' in str(failures[0])


def test_actual_native_module_imports_without_docker_implementation():
    script = '''import builtins
original = builtins.__import__
def checked(name, *args, **kwargs):
    if name in {'femto_bolt_viewer.live_converter',
                'femto_bolt_viewer.docker_transport',
                'femto_bolt_viewer.runtime_cache',
                'femto_bolt_viewer.offline_depth'}:
        raise AssertionError('Native runtime imported a Docker implementation')
    return original(name, *args, **kwargs)
builtins.__import__ = checked
from femto_bolt_viewer.native_converter import NativeDepthConverter
assert callable(NativeDepthConverter)
'''
    result = subprocess.run([sys.executable, '-B', '-c', script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def build_environment(tmp_path, monkeypatch):
    (tmp_path / 'native_depth_engine.mm').write_text('// synthetic compiler input')
    monkeypatch.setattr(native, 'HERE', tmp_path)
    monkeypatch.setattr(native.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(native.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(native.platform, 'mac_ver', lambda: ('26.0', (), ''))
    return tmp_path


def test_successful_worker_build_is_cached_and_uses_only_native_compiler(build_environment, monkeypatch):
    launches = []

    def compile_worker(argv, **kwargs):
        launches.append((argv, kwargs))
        output = Path(argv[-1])
        output.write_bytes(b'synthetic native executable')
        output.chmod(0o700)
        return SimpleNamespace(poll=lambda: 0, wait=lambda: 0, returncode=0)

    monkeypatch.setattr(native.subprocess, 'Popen', compile_worker)
    first = native._build_worker(build_environment / 'work', None)
    second = native._build_worker(build_environment / 'work', None)
    assert first == second
    assert first.read_bytes() == b'synthetic native executable'
    assert len(launches) == 1
    argv, kwargs = launches[0]
    assert argv[:2] == ['/usr/bin/xcrun', 'clang++']
    assert kwargs['stdout'].closed and kwargs['stderr'].closed
    assert not list(first.parent.glob('*.building-*'))


def test_stable_worker_cache_reuses_binary_across_sessions_and_invalidates_changed_source(build_environment, monkeypatch):
    launches = []

    def compile_worker(argv, **kwargs):
        output = Path(argv[-1])
        launches.append(output)
        output.write_bytes(b'synthetic native executable')
        output.chmod(0o700)
        return SimpleNamespace(poll=lambda: 0, wait=lambda: 0, returncode=0)

    monkeypatch.setattr(native.subprocess, 'Popen', compile_worker)
    cache = build_environment / 'managed' / 'native-worker-cache'
    first_session = build_environment / 'managed' / 'session-one' / 'converter'
    second_session = build_environment / 'managed' / 'session-two' / 'converter'
    first = native._build_worker(first_session, None, cache_dir=cache)
    second = native._build_worker(second_session, None, cache_dir=cache)
    assert first == second
    assert len(launches) == 1
    assert first.parent == cache
    assert not (first_session.parent / 'native-worker-cache').exists()
    assert not (second_session.parent / 'native-worker-cache').exists()

    (build_environment / 'native_depth_engine.mm').write_text('// changed synthetic compiler input')
    updated = native._build_worker(second_session, None, cache_dir=cache)
    assert updated != first
    assert len(launches) == 2
    assert updated.is_file() and first.is_file()


def test_converter_start_forwards_shared_cache_to_worker_build(converter_factory, monkeypatch, tmp_path):
    cache = tmp_path / 'shared-worker-cache'
    converter, _ = converter_factory(native_worker_cache_dir=cache)
    fake_worker = native._build_worker(converter.work_dir, None)
    builds = []

    def build(work, cancel, **kwargs):
        builds.append((work, cancel, kwargs))
        return fake_worker

    monkeypatch.setattr(native, '_build_worker', build)
    converter.start()
    assert len(builds) == 1
    assert builds[0][0] == converter.work_dir
    assert builds[0][2] == {'cache_dir': cache}
    assert converter.convert(b'\x01\x00', 1)['depth_mm'][1, 1] == 1


@pytest.mark.parametrize('reason', ['cancel', 'timeout', 'compiler-error'])
def test_failed_worker_build_reaps_compiler_and_removes_partial_binary(build_environment, monkeypatch, reason):
    cancel = threading.Event()
    process = SimpleNamespace(returncode=1 if reason == 'compiler-error' else None, killed=False, waited=False)
    logs = []

    def kill():
        process.killed = True
        process.returncode = -9

    def wait():
        process.waited = True
        return process.returncode

    def compile_worker(argv, **kwargs):
        Path(argv[-1]).write_bytes(b'incomplete binary')
        logs.append(kwargs['stderr'])
        return process

    process.poll = lambda: process.returncode
    process.kill, process.wait = kill, wait
    if reason == 'cancel':
        cancel.set()
    if reason == 'timeout':
        times = iter([0, 61])
        monkeypatch.setattr(native.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(native.subprocess, 'Popen', compile_worker)
    with pytest.raises((RuntimeError, TimeoutError), match='cancelled|timed out|compilation failed'):
        native._build_worker(build_environment / 'work', cancel)
    assert process.waited
    assert process.killed is (reason != 'compiler-error')
    assert logs[0].closed
    assert not list((build_environment / 'native-worker-cache').glob('*.building-*'))

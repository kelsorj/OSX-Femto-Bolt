"""The source build refuses drift and never fetches SDKs in portable-test mode."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location("build_native", Path(__file__).parents[1] / "scripts/build_native.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


@pytest.fixture
def checkout(tmp_path):
    header = tmp_path / "src/platform/Platform.hpp"
    header.parent.mkdir(parents=True)
    header.write_text("// synthetic checkout marker")
    return tmp_path


def test_wrong_sdk_revision_is_refused_without_reset(checkout, monkeypatch):
    run = Mock(return_value="0" * 40)
    monkeypatch.setattr(build, "run", run)

    with pytest.raises(ValueError, match="never reset"):
        build.verify_checkout("git", checkout)

    run.assert_called_once_with(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture=True)


def test_dirty_sdk_is_refused(checkout, monkeypatch):
    run = Mock(side_effect=[build.SDK_REVISION, " M src/platform/Platform.hpp"])
    monkeypatch.setattr(build, "run", run)

    with pytest.raises(ValueError, match="modified"):
        build.verify_checkout("git", checkout)

    assert run.call_count == 2


@pytest.mark.parametrize("status", ["-0123456789 dep", "+0123456789 dep", "U0123456789 dep"])
def test_missing_or_diverged_submodule_is_refused(checkout, monkeypatch, status):
    monkeypatch.setattr(build, "run", Mock(side_effect=[build.SDK_REVISION, "", status]))

    with pytest.raises(ValueError, match="submodules"):
        build.verify_checkout("git", checkout)


def test_explicit_missing_sdk_does_not_fetch_or_create_directory(tmp_path, monkeypatch):
    run = Mock(side_effect=AssertionError("must not run git"))
    monkeypatch.setattr(build, "run", run)
    path = tmp_path / "missing"

    with pytest.raises(ValueError, match="does not exist"):
        build.prepare_sdk("git", path, fetch=False)

    assert not path.exists()
    run.assert_not_called()


def test_new_sdk_fetches_exact_commit_before_checkout_and_submodules(tmp_path, monkeypatch):
    run = Mock(return_value="")
    verify = Mock()
    monkeypatch.setattr(build, "run", run)
    monkeypatch.setattr(build, "verify_checkout", verify)
    source = tmp_path / "new-checkout"

    assert build.prepare_sdk("git", source, fetch=True) == source

    commands = [call.args[0] for call in run.call_args_list]
    assert commands == [
        ["git", "init", str(source)],
        ["git", "-C", str(source), "remote", "add", "origin", build.SDK_URL],
        ["git", "-C", str(source), "fetch", "--depth", "1", "origin", build.SDK_REVISION],
        ["git", "-C", str(source), "checkout", "--detach", build.SDK_REVISION],
        ["git", "-C", str(source), "submodule", "update", "--init", "--recursive"],
    ]
    verify.assert_called_once_with("git", source)


def test_portable_core_build_never_fetches_sdk_or_runs_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(build.os, "geteuid", lambda: 501)
    monkeypatch.setattr(build.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build, "executable", lambda name: "/tools/" + name)
    fetch = Mock(side_effect=AssertionError("portable tests must not fetch SDK"))
    run = Mock(return_value="")
    monkeypatch.setattr(build, "prepare_sdk", fetch)
    monkeypatch.setattr(build, "run", run)

    assert build.main(["--core-only", "--build-dir", str(tmp_path), "--jobs", "2"]) == 0

    fetch.assert_not_called()
    commands = [call.args[0] for call in run.call_args_list]
    assert len(commands) == 3
    assert "-DFEMTO_CORE_TESTS_ONLY=ON" in commands[0]
    targets = commands[1][commands[1].index("--target") + 1:]
    assert set(targets) == {"femto_probe_core_tests", "femto_live_core_tests", "femto_capture_ownership_tests"}
    assert commands[2] == ["/tools/ctest", "--test-dir", str(tmp_path), "--output-on-failure", "-R", "^femto_"]


@pytest.mark.parametrize("jobs", ["0", "129"])
def test_invalid_parallelism_is_refused_before_build(monkeypatch, jobs):
    monkeypatch.setattr(build.os, "geteuid", lambda: 501)
    run = Mock(side_effect=AssertionError("must not start build"))
    monkeypatch.setattr(build, "run", run)

    assert build.main(["--core-only", "--jobs", jobs]) == 1
    run.assert_not_called()


@pytest.mark.parametrize("serial", ["", "../unit", "unit;echo"])
def test_calibration_wrapper_rejects_unsafe_serial_before_sudo(tmp_path, serial):
    result = subprocess.run(["/bin/sh", str(build.ROOT / "scripts/capture_calibration.sh"),
                             serial, str(tmp_path / "new")], capture_output=True, text=True, timeout=2)

    assert result.returncode == 2
    assert "serial must contain" in result.stderr or "Build the native probe" in result.stderr
    assert not (tmp_path / "new").exists()


def test_calibration_wrapper_refuses_existing_output_before_sudo(tmp_path):
    binary = tmp_path / "probe"
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o700)
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep"
    sentinel.write_text("private calibration")

    result = subprocess.run(["/bin/sh", str(build.ROOT / "scripts/capture_calibration.sh"),
                             "SYNTHETIC-UNIT", str(destination), str(binary)],
                            capture_output=True, text=True, timeout=2)

    assert result.returncode == 2
    assert "already exists" in result.stderr
    assert sentinel.read_text() == "private calibration"

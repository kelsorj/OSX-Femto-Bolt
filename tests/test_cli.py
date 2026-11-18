"""CLI checks fail closed without touching a camera or elevating Python."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from femto_bolt_viewer import cli, conversion_input, native_calibration, native_converter


@pytest.fixture
def supported_host(monkeypatch, tmp_path):
    binary = tmp_path / "capture"
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o700)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 501)
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/xcrun")
    return binary


def doctor_args(binary, *extra):
    return cli.parser().parse_args(["doctor", "--capture-bin", str(binary), *extra])


def test_doctor_without_profile_is_not_ready_even_when_all_tools_are_present(supported_host):
    result = cli.diagnose(doctor_args(supported_host))

    assert result["ready"] is False
    assert result["usb_tested"] is False
    assert result["automatic_profile_provisioning"] is False
    assert result["checks"]["capture_executable"] is True
    assert result["checks"]["unit_profile_valid"] is False
    assert "does not create" in result["profile_error"]


def test_doctor_refuses_mismatched_unit_and_does_not_start_a_worker(supported_host, monkeypatch):
    def mismatch(*_):
        raise ValueError("Saved factory calibration does not match selected camera")

    monkeypatch.setattr(conversion_input, "load_unit_calibration", mismatch)
    start = Mock(side_effect=AssertionError("doctor must not start a worker"))
    monkeypatch.setattr(native_converter.NativeDepthConverter, "start", start)
    args = doctor_args(supported_host, "--serial", "TEST-UNIT", "--nvram", "missing",
                       "--native-profile-dir", "missing")

    result = cli.diagnose(args)

    assert result["ready"] is False
    assert "does not match" in result["profile_error"]
    start.assert_not_called()


def test_doctor_binds_profile_to_verified_calibration_and_selected_mode(supported_host, monkeypatch):
    loader = Mock(return_value=(b"synthetic", {"calibration_sha256": "a" * 64}))
    thermal = Mock()
    profile = Mock(return_value=(None, {}, "b" * 64))
    monkeypatch.setattr(conversion_input, "load_unit_calibration", loader)
    monkeypatch.setattr(native_calibration, "load_native_calibration", thermal)
    monkeypatch.setattr(native_converter, "validate_native_profile", profile)
    args = doctor_args(supported_host, "--serial", "TEST-UNIT", "--nvram", "nvram",
                       "--native-profile-dir", "profile", "--mode", "nfov")

    result = cli.diagnose(args)

    loader.assert_called_once_with(args.nvram, "TEST-UNIT")
    thermal.assert_called_once_with(b"synthetic", "nfov")
    profile.assert_called_once_with(args.native_profile_dir, "TEST-UNIT", "nfov", "a" * 64)
    assert result["ready"] is True
    assert result["usb_tested"] is False
    assert result["profile_sha256"] == "b" * 64


@pytest.mark.parametrize(("system", "machine"), [("Linux", "aarch64"), ("Darwin", "x86_64")])
def test_doctor_reports_unsupported_host(supported_host, monkeypatch, system, machine):
    monkeypatch.setattr(cli.platform, "system", lambda: system)
    monkeypatch.setattr(cli.platform, "machine", lambda: machine)

    result = cli.diagnose(doctor_args(supported_host))

    assert result["ready"] is False
    assert result["checks"]["apple_silicon_macos"] is False


def test_doctor_exits_nonzero_with_machine_readable_reason(supported_host, capsys):
    result = cli.main(["doctor", "--capture-bin", str(supported_host)])

    assert result == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False


def test_viewer_refuses_root_before_importing_runtime(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)

    with pytest.raises(SystemExit) as exc:
        cli.main(["demo"])

    assert exc.value.code == 2
    assert "normal user" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_viewer_refuses_out_of_range_port_before_startup(monkeypatch, capsys, port):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 501)

    with pytest.raises(SystemExit) as exc:
        cli.main(["demo", "--port", port])

    assert exc.value.code == 2
    assert "between 1 and 65535" in capsys.readouterr().err


def test_run_requires_explicit_unit_inputs():
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(["run"])

    assert exc.value.code == 2


def test_run_preflight_failure_never_constructs_viewer_or_opens_http(monkeypatch, capsys):
    from femto_bolt_viewer import runtime
    import uvicorn

    monkeypatch.setattr(cli.os, "geteuid", lambda: 501)
    monkeypatch.setattr(cli, "diagnose", lambda _: {"ready": False, "profile_error": "unit mismatch"})
    viewer = Mock(side_effect=AssertionError("must not start camera"))
    serve = Mock(side_effect=AssertionError("must not open HTTP"))
    monkeypatch.setattr(runtime, "Viewer", viewer)
    monkeypatch.setattr(uvicorn, "run", serve)

    result = cli.main(["run", "--serial", "TEST-UNIT", "--nvram", "nvram", "--native-profile-dir", "profile"])

    assert result == 1
    assert json.loads(capsys.readouterr().err)["profile_error"] == "unit mismatch"
    viewer.assert_not_called()
    serve.assert_not_called()


def test_run_binds_only_loopback_and_passes_sudo_to_capture_runtime(supported_host, tmp_path, monkeypatch):
    from femto_bolt_viewer import runtime, server
    import uvicorn

    monkeypatch.setattr(cli, "diagnose", lambda _: {"ready": True})
    viewer = Mock()
    app = object()
    make_app = Mock(return_value=app)
    serve = Mock()
    monkeypatch.setattr(runtime, "Viewer", viewer)
    monkeypatch.setattr(server, "create_app", make_app)
    monkeypatch.setattr(uvicorn, "run", serve)

    result = cli.main(["run", "--serial", "TEST-UNIT", "--nvram", str(tmp_path / "nvram"),
                       "--native-profile-dir", str(tmp_path / "profile"), "--work", str(tmp_path / "cache"),
                       "--capture-bin", str(supported_host), "--sudo", "--port", "8766"])

    assert result == 0
    assert viewer.call_args.args[:4] == ("TEST-UNIT", tmp_path / "nvram", tmp_path / "profile", supported_host)
    assert viewer.call_args.kwargs == {"mode": "wfov-binned", "use_sudo": True}
    make_app.assert_called_once_with(viewer.return_value)
    serve.assert_called_once_with(app, host="127.0.0.1", port=8766, access_log=False)

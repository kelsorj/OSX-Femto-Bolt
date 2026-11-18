"""Local viewer entry point; administrator privileges belong only to USB capture."""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import sys
import threading
import uuid
import webbrowser
from pathlib import Path

from . import __version__
from .depth_modes import SUPPORTED_MODES


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Experimental native macOS Femto Bolt RGB/depth viewer")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("run", "doctor", "demo"):
        p = sub.add_parser(command, help={"run": "View a camera using its compatible private profile",
                                         "doctor": "Check local prerequisites without opening USB",
                                         "demo": "Preview the interface with synthetic images"}[command])
        p.add_argument("--mode", choices=SUPPORTED_MODES, default="wfov-binned")
        if command != "demo":
            required = command == "run"
            p.add_argument("--serial", required=required)
            p.add_argument("--nvram", type=Path, required=required,
                           help="Verified factory-calibration capture directory for this camera")
            p.add_argument("--native-profile-dir", type=Path, required=required,
                           help="Compatible private conversion profile; automatic creation is not available")
            p.add_argument("--capture-bin", type=Path, default=Path("build/native/bin/femto_live_capture"))
        if command != "doctor":
            p.add_argument("--port", type=int, default=8765)
            p.add_argument("--open", action="store_true", help="Open the local viewer in your browser")
        if command == "run":
            p.add_argument("--sudo", action="store_true", help="Use sudo -n for the native USB helper only")
            p.add_argument("--work", type=Path, help="Private runtime/cache folder (defaults to user Library/Caches)")
    return root


def diagnose(args) -> dict:
    """Check exact unit identity and resources; never claim a connected camera."""
    from .conversion_input import load_unit_calibration
    from .native_calibration import load_native_calibration
    from .native_converter import validate_native_profile

    checks = {
        "apple_silicon_macos": platform.system() == "Darwin" and platform.machine() == "arm64",
        "ordinary_user": not hasattr(os, "geteuid") or os.geteuid() != 0,
        "xcrun_available": shutil.which("xcrun") is not None,
        "capture_executable": args.capture_bin.is_file() and os.access(args.capture_bin, os.X_OK),
        "unit_profile_valid": False,
    }
    report = {"version": __version__, "checks": checks, "usb_tested": False,
              "automatic_profile_provisioning": False}
    if args.serial and args.nvram and args.native_profile_dir:
        try:
            blob, identity = load_unit_calibration(args.nvram, args.serial)
            load_native_calibration(blob, args.mode)
            _, _, digest = validate_native_profile(args.native_profile_dir, args.serial, args.mode,
                                                  identity["calibration_sha256"])
            report["profile_sha256"] = digest
            checks["unit_profile_valid"] = True
        except (ValueError, OSError, KeyError) as exc:
            report["profile_error"] = str(exc)
    else:
        report["profile_error"] = (
            "Supply --serial, --nvram and --native-profile-dir. "
            "Reading factory calibration alone does not create a native conversion profile."
        )
    report["ready"] = all(checks.values())
    return report


def main(argv=None) -> int:
    p = parser()
    args = p.parse_args(argv)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("femto_bolt_viewer")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if args.command == "doctor":
        report = diagnose(args)
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 1
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        p.error("Run the viewer as your normal user. Use --sudo to elevate only the native USB helper.")
    if not 1 <= args.port <= 65535:
        p.error("Port must be between 1 and 65535")
    from .runtime import DemoViewer, Viewer
    from .server import create_app

    if args.command == "demo":
        preview = DemoViewer(mode=args.mode)
    else:
        report = diagnose(args)
        if not report["ready"]:
            print(json.dumps(report, indent=2), file=sys.stderr)
            return 1
        work = args.work or Path.home() / "Library/Caches/OSX-Femto-Bolt"
        work = work.resolve()
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        preview = Viewer(args.serial, args.nvram.resolve(), args.native_profile_dir.resolve(),
                         args.capture_bin.resolve(), work / ("session-" + uuid.uuid4().hex),
                         mode=args.mode, use_sudo=args.sudo)
    url = f"http://127.0.0.1:{args.port}"
    logger.info("Opening %s viewer at %s", "synthetic demo" if args.command == "demo" else "camera", url)
    timer = None
    if args.open:
        timer = threading.Timer(1.0, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    import uvicorn

    try:
        uvicorn.run(create_app(preview), host="127.0.0.1", port=args.port, access_log=False)
    finally:
        if timer:
            timer.cancel()
    return 0

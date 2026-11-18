#!/usr/bin/env python3
"""Build the macOS USB transport from a pinned public source checkout.

Run as your ordinary user. This does not access cameras or install privileged
helpers. Only the two capture targets and hardware-free tests are built; SDK
binary extensions are not linked, installed, or copied into the output.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

SDK_URL = "https://github.com/orbbec/OrbbecSDK_v2.git"
SDK_REVISION = "575347ca742d219ef61b1e214e97043f17f48d23"
ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, capture: bool = False) -> str:
    print("+ " + shlex.join(command), flush=True)
    result = subprocess.run(command, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    # A pip-installed CMake may sit beside the chosen Python even when its
    # environment has not been activated.
    sibling = Path(sys.executable).parent / name
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    raise ValueError(f"{name} was not found. Install CMake and Xcode Command Line Tools first.")


def verify_checkout(git: str, source: Path) -> None:
    if not (source / "src/platform/Platform.hpp").is_file():
        raise ValueError(f"Not an Orbbec SDK source checkout: {source}")
    revision = run([git, "-C", str(source), "rev-parse", "HEAD"], capture=True)
    if revision != SDK_REVISION:
        raise ValueError(f"SDK must be exactly {SDK_REVISION}; found {revision}. Existing checkouts are never reset.")
    status = run([git, "-C", str(source), "status", "--porcelain", "--untracked-files=no"], capture=True)
    if status:
        raise ValueError("SDK tracked sources are modified. Use a clean checkout of the pinned revision.")
    submodules = run([git, "-C", str(source), "submodule", "status", "--recursive"], capture=True)
    if any(line and line[0] in "-+U" for line in submodules.splitlines()):
        raise ValueError("SDK submodules are missing or modified. Run git submodule update --init --recursive in that checkout.")


def prepare_sdk(git: str, source: Path, *, fetch: bool) -> Path:
    source = source.expanduser().resolve()
    if not source.exists():
        if not fetch:
            raise ValueError(f"The --sdk checkout does not exist: {source}")
        source.parent.mkdir(parents=True, exist_ok=True)
        # Fetch only the supported commit. Its full SHA is checked again below;
        # there is no mutable branch or tag in the reproducible build input.
        run([git, "init", str(source)])
        run([git, "-C", str(source), "remote", "add", "origin", SDK_URL])
        run([git, "-C", str(source), "fetch", "--depth", "1", "origin", SDK_REVISION])
        run([git, "-C", str(source), "checkout", "--detach", SDK_REVISION])
        run([git, "-C", str(source), "submodule", "update", "--init", "--recursive"])
    verify_checkout(git, source)
    return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, help="Reuse a clean pinned SDK checkout without changing it")
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build/native")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 2))
    parser.add_argument("--core-only", action="store_true", help="Test framing, bounds and capture logic without SDK or hardware (macOS/Linux)")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise ValueError("Run the build as your ordinary user, without sudo.")
        if args.jobs < 1 or args.jobs > 128:
            raise ValueError("--jobs must be between 1 and 128")
        if not args.core_only and (platform.system() != "Darwin" or platform.machine() != "arm64"):
            raise ValueError("The viewer currently supports Apple Silicon macOS. Portable tests use --core-only.")
        cmake = executable("cmake")
        ctest = executable("ctest")
        build = args.build_dir.expanduser().resolve()
        if args.core_only and args.build_dir == ROOT / "build/native":
            build = ROOT / "build/core-tests"
        configuration = [cmake, "-S", str(ROOT / "native"), "-B", str(build),
                         "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_TESTING=ON",
                         f"-DFEMTO_CORE_TESTS_ONLY={'ON' if args.core_only else 'OFF'}"]
        if args.core_only:
            targets = ["femto_probe_core_tests", "femto_live_core_tests", "femto_capture_ownership_tests"]
        else:
            run(["/usr/bin/xcrun", "--find", "clang++"], capture=True)
            source = args.sdk or ROOT / "build/deps/orbbec-sdk-v2"
            source = prepare_sdk(executable("git"), source, fetch=args.sdk is None)
            configuration += [f"-DFEMTO_SDK_SOURCE={source}", "-DCMAKE_OSX_ARCHITECTURES=arm64"]
            targets = ["femto_raw_probe", "femto_live_capture", "femto_probe_core_tests", "femto_live_core_tests", "femto_capture_ownership_tests"]
        run(configuration)
        run([cmake, "--build", str(build), "--parallel", str(args.jobs), "--target", *targets])
        # SDK third parties register unrelated tests even with OB_BUILD_TESTS
        # disabled. We deliberately do not build those targets or run them.
        run([ctest, "--test-dir", str(build), "--output-on-failure", "-R", "^femto_"])
        if not args.core_only:
            for target in ("femto_raw_probe", "femto_live_capture"):
                binary = build / "bin" / target
                run(["/usr/bin/codesign", "--force", "--sign", "-", str(binary)])
                run([str(binary), "--check-build"])
            print(f"Native capture tools: {build / 'bin'}")
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

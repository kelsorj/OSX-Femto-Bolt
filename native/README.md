# Native USB transport

Build from the repository root as your ordinary user:

```sh
python3 scripts/build_native.py
```

This requires Apple Silicon macOS, Xcode Command Line Tools, Git and CMake.
Install the command-line tools with `xcode-select --install`; CMake can be
installed with `python3 -m pip install cmake` in the viewer's virtual environment.
The script fetches the public Orbbec SDK source at commit
`575347ca742d219ef61b1e214e97043f17f48d23`, initializes its submodules, and builds
only the source USB transport plus our native executables. Proprietary SDK
binary extensions are not linked, installed or copied into the build output.

To use an existing clean checkout without modifying it:

```sh
python3 scripts/build_native.py --sdk /path/to/OrbbecSDK_v2
```

The build verifies the exact revision and rejects modified tracked files or
uninitialized submodules. The generated macOS Bolt enumeration change lives
inside the build directory; the SDK source is unchanged. All downloaded source
and binaries remain under the ignored `build/` directory.

The native tools are `build/native/bin/femto_raw_probe` and
`build/native/bin/femto_live_capture`. Both provide `--check-build`, which does
not enumerate or open a camera. The live tool writes binary framed RGB/raw
transport data to stdout; connect it to the viewer's relay, never directly to
a terminal.

For camera discovery and factory-data capture, stop other camera applications
first, then use these explicit native commands:

```sh
sudo build/native/bin/femto_raw_probe --list
mkdir -p "$HOME/Library/Application Support/OSX-Femto-Bolt/calibration"
scripts/capture_calibration.sh YOUR_CAMERA_SERIAL \
  "$HOME/Library/Application Support/OSX-Femto-Bolt/calibration/YOUR_CAMERA_SERIAL"
```

The wrapper must run without sudo. It invokes only the native executable with
sudo; Python and the web server stay unprivileged. The native probe saves private
owner-only files and returns ownership to the account identified by sudo.
It refuses existing output directories. Captured factory data alone does **not**
generate a native compute profile; see the project README for the current
profile-import requirement.

Portable tests require only CMake and a C++17 compiler; they do not fetch SDK
source, access USB, or require administrator privileges:

```sh
python3 scripts/build_native.py --core-only
```

This runs the 34-case probe suite, 14 live framing, queue, profile and error
scenarios, and eight ownership-transfer cases with privileged calls replaced
by test doubles. The same tests run automatically after the full native build.

These sources were extracted and adapted from the GPL-3.0-or-later pyBravo
project. See the repository NOTICE and LICENSE files for attribution. SDK and
third-party source remain in their own separately licensed checkout; binaries
are built locally rather than redistributed here.

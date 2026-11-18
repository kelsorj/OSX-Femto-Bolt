# OSX-Femto-Bolt

An experimental **native macOS RGB and depth viewer** for the Orbbec Femto Bolt.
The viewer runs locally and displays the camera in a web browser. USB acquisition
and depth processing run on macOS; no Docker or Linux runtime is used.

**Current limitation:** live depth requires a compatible native profile and
factory calibration data for your particular camera. An
[owner-shared reference calibration](examples/calibration/femto-bolt-CL8TC53000Z/README.md)
is included for experimentation, with an independent-validation disclaimer.
It is not calibrated for other cameras and does not include depth-conversion
programs. This repository does not yet generate a profile for an arbitrary new Bolt.
You can build the public code and try the synthetic demo without those files.
This is a developer release, not a turnkey macOS driver.

The shared sample is never used automatically. Anyone experimenting with it must
verify accuracy on their own hardware before relying on measurements.

## Requirements

- An Apple Silicon Mac with Metal support. Intel Macs are not supported.
- Python 3.11 or newer, Git, CMake, and Apple's Command Line Tools.
- A powered Femto Bolt with a USB 3 data connection for live capture.
- A compatible per-camera native profile bundle and that camera's factory
  calibration/NVRAM directory for live depth.

The application is independent of pyBravo and has no robot control functions.
It is a community project, not an official Orbbec SDK release.

## Install and build

Check `python3 --version` first: it must be 3.11 or newer. Some macOS installations
provide Python 3.9 as `python3`; use the executable for your installed Python 3.11+
in the virtual-environment command below.

```sh
git clone https://github.com/kelsorj/OSX-Femto-Bolt.git
cd OSX-Femto-Bolt
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python scripts/build_native.py
```

The build downloads the pinned public Orbbec SDK source and compiles the native
USB capture tools locally. The Metal host is compiled separately on first live
use. To build the capture tools from an existing SDK checkout:

```sh
python scripts/build_native.py --sdk /path/to/OrbbecSDK_v2
```

The checkout must match the pinned revision. The build does not provide a depth
profile or make proprietary resources redistributable. See
[third-party notices](THIRD_PARTY_NOTICES.md).

## Try the browser interface

```sh
femto-bolt-viewer demo --port 8765
```

Open <http://127.0.0.1:8765>. Demo images are synthetic and are not measurements
from a camera.

## Run with a camera

First check the toolchain and a prepared camera profile:

```sh
femto-bolt-viewer doctor \
  --serial YOUR_CAMERA_SERIAL \
  --nvram /path/to/your-camera/nvram \
  --native-profile-dir /path/to/your-camera/native-profiles
```

If you need to read the camera's factory calibration, use the
[calibration capture helper](docs/profiles.md#read-factory-calibration). This
creates the NVRAM directory, **not** the missing native compute profile.

With a compatible profile already available:

```sh
femto-bolt-viewer run \
  --serial YOUR_CAMERA_SERIAL \
  --nvram /path/to/your-camera/nvram \
  --native-profile-dir /path/to/your-camera/native-profiles \
  --capture-bin build/native/bin/femto_live_capture \
  --mode wfov-binned \
  --port 8765
```

Open <http://127.0.0.1:8765>. Keep camera-specific data outside the repository.
The serial, factory calibration hash, and selected mode must match the profile;
a profile for another camera is not a substitute.

USB access on macOS may require administrator authorization. In your terminal,
run `sudo -v`, then add `--sudo` to the viewer command. This elevates only the
native capture executable; the web server and Python viewer remain under your
ordinary account. No passwordless permission rule is installed. See
[USB access](docs/usb-access.md).

## Understanding the images

RGB and depth have different lenses and fields of view. A native depth image
therefore does not have the same outline or pixel positions as the RGB image.
The wide binned depth mode produces a 512 × 512 image; narrow mode produces
640 × 576. Black depth pixels indicate no valid measurement.

The displayed depth scale maps measured millimetres to colors. Changing that
scale changes the picture, not the underlying measurements. Treat the
experimental output as a visualization until independently validated for your
measurement task.

## What remains to make installation universal

The main missing piece is a reproducible, distributable way to provision each
camera's native depth profile. The current engine accepts separately supplied
compute resources and factory tables; this repository supplies neither vendor
compute programs nor a tool to extract or translate them. See
[profile requirements and contribution boundaries](docs/profiles.md).

Useful contributions include a permitted native depth implementation, documented
calibration formats, deterministic profile generation, and comparisons against
known distances across modes and cameras. Please include reproduction steps and
hardware/software versions in bug reports. Do not attach private calibration,
profiles, recordings, or proprietary SDK resources to public issues.

## Development and licensing

See the [validation record](docs/validation.md) for tested hardware, performance,
packaging checks, and the limits of those checks.

```sh
python -m pytest
```

Original project code is GPL-3.0-or-later; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
The native transport build uses separately licensed public SDK source and
third-party libraries. Their complete notices are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). No vendor extension binary or
camera-specific profile is distributed here.

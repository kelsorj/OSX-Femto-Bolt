# Validation of the developer preview

Checked on 18 November 2026 on an Apple M4 Mac, using Python 3.12 in a fresh
virtual environment. These checks establish a working viewer for one prepared
camera, not universal camera provisioning or metrology accuracy.

## Automated and packaging checks

- 271 Python tests pass: calibration identity and bounds, native worker I/O,
  malformed packets, depth colors, reconnection, stale-frame suppression,
  shutdown, HTTP/WebSocket behavior, CLI checks, and native build planning.
- 16 native CTest entries pass, covering 56 cases/scenarios for acquisition,
  bounded queues, packet framing, supported modes, and calibration-file ownership.
- Python lint and JavaScript syntax checks pass.
- The default build fetched the pinned SDK from public GitHub and built both
  capture executables. Their `--check-build` checks pass; those checks do not
  access USB. Linked dynamic dependencies are Apple/system libraries only.
- The wheel was installed into a separate environment without the source
  project's editable installation or pyBravo. Imports, packaged HTML/JavaScript,
  Metal host source, and the synthetic demo work there.
- Wheel and source archive contents were checked for private profiles,
  calibration files, recordings, shader resources, and unit identifiers.

## Live camera check

One Femto Bolt connected over USB 3 was tested in `wfov-binned` mode using its
existing, privately prepared profile and factory calibration. This exercise used
an already installed, authorized native USB helper speaking the same capture
protocol. It did not test administrator installation of the newly built helper.
The Python viewer and Metal conversion host came from this standalone project.

All 24 one-second samples showed fresh RGB and depth:

| Observation | Result |
|---|---|
| RGB image | 1280 × 720 |
| Native depth image | 512 × 512 |
| Depth frame rate | 14.98–15.04 FPS |
| Conversion duration, median | 4.75 ms |
| Depth frame age, median | 40.95 ms |
| Host-arrival pairing offset, median | 18.49 ms |
| Dropped/rejected depth acquisitions during sample | 0 / 0 |

The capture helper retried twice while USB interfaces reappeared after releasing
the previous camera session, then recovered automatically. The browser showed
both live images and metrics. The synthetic demo and depth-color range control
were also checked in the browser. The previous camera service was restored
after testing.

## Unverified or incomplete

- Creating the required native profile for a new camera is not implemented.
- The live check does not establish absolute depth accuracy, hardware timestamp
  synchronization, or performance on other Macs/cameras.
- Narrow-field transport and profile handling have automated coverage; this
  standalone release has only been live-tested in wide binned mode.
- RGB and depth are separate native views. Registration is not implemented here.
- Intel Macs are not supported.

The original developer-preview validation used private resources. A subsequent
owner-approved [factory-calibration sample](../examples/calibration/femto-bolt-CL8TC53000Z/README.md)
is now published for reference. Its checksum, complete 99-record container, and
NFOV/WFOV thermal and illumination parsing were checked. This adds no accuracy
validation for other units. No complete native profiles, recordings or scene
images accompany this report.

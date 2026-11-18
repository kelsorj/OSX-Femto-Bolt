# Native depth profiles

The native Metal host is a compute-program runner. Its source alone is not a
complete Femto Bolt raw-to-depth algorithm: live depth also requires compute
resources and factory calibration tables matched to the camera and mode.

The current profile format binds a bundle to a camera serial number, the hash of
its factory calibration, a depth mode, resource hashes, dimensions, and dispatch
parameters. The runtime rejects incompatible or incomplete bundles. Renaming a
profile or copying one from another camera does not calibrate a new unit.

## What this repository provides

- The public-source USB capture helper and native Metal host.
- Profile validation and the browser viewer.
- Synthetic demo data for trying the interface without hardware.
- An [owner-shared factory-calibration reference](../examples/calibration/femto-bolt-CL8TC53000Z/README.md)
  from one Bolt. It includes an independent-validation disclaimer and does not
  supply depth-conversion programs or calibrate other units.

## What you must already have for live depth

- A supported Femto Bolt and its factory calibration/NVRAM directory.
- A compatible profile bundle for that same camera and selected mode.
- Permission to use the bundle's included compute resources.

The wide binned and narrow modes use different profiles. Keep the profile root,
its mode directories, and resource names as supplied; do not alter the manifest
to bypass compatibility checks. Use the CLI's `--native-profile-dir` argument to
select the bundle root.

**Automatic profile creation for arbitrary new cameras is unresolved.** This
release does not include a provisioner or instructions for extracting proprietary
programs. Prior local experiments relied on separately provisioned compute
resources; their existence is not a distributable provisioning solution.

## Read factory calibration

After building the native capture tools, close other applications using the
camera and run this script as your ordinary account:

```sh
scripts/capture_calibration.sh YOUR_CAMERA_SERIAL /path/to/new-nvram-directory
```

The destination must not already exist, and its parent directory must exist.
An optional third argument selects a different `femto_raw_probe` executable;
the default is `build/native/bin/femto_raw_probe`.

The script uses `sudo` only for the native USB probe and may ask for your
administrator password in Terminal. The probe captures two copies of the
factory calibration and gives the output files back to your account. Keep the
resulting directory private unless its owner explicitly chooses to share it,
and pass it to the viewer's `--nvram` option. The published reference `.ccb` is
not a capture directory for this option.

This reads factory data from the device; it does not generate, download, or grant
permission to redistribute the separate native compute profile. A matching
profile is still required for live depth.

## How to help close the gap

A contribution should provide a reproducible process with documented inputs,
resource provenance and distribution terms. Possible routes include an approved
native engine or a separately developed implementation with a documented
calibration format. The public SDK's MIT source license must not be assumed to
cover its proprietary extensions or programs derived from them.

Validate a proposed implementation on more than one camera, and record depth
errors against known distances and surfaces across supported modes. Check warmup,
temperature changes, invalid pixels, and RGB/depth timing. Report the method and
aggregate errors. Publish a camera's factory data only with its owner's explicit
permission, a provenance record and an independent-validation disclaimer.

Until that work is complete, the project should be described as an experimental
viewer for separately provisioned cameras, not a universal macOS Bolt driver.

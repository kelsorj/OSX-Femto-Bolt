# Owner-shared Femto Bolt factory calibration

This factory-calibration sample comes from the maintainer's Femto Bolt,
serial **CL8TC53000Z**, and is published at the owner's request for reference
and experimentation.

> **Verify it yourself before relying on it.** This is the factory calibration
> of one physical camera. It is not calibrated for other cameras. Anyone
> experimenting with it must independently verify depth accuracy across their
> distances, surfaces, operating temperatures, and acquisition modes before
> relying on measurements. It is provided as-is, without any accuracy guarantee
> or warranty. No cross-camera accuracy validation has been performed.

## Contents and provenance

| File | Contents |
| --- | --- |
| `calibration.ccb` | Exact 485,616-byte calibration blob extracted from four identical factory-data reads |
| `manifest.json` | Source unit, file identity, structural checks and limitations |
| `SHA256SUMS.txt` | SHA-256 checksums of the blob and manifest |

Verify the download from this directory with:

```sh
shasum -a 256 -c SHA256SUMS.txt
```

The 26-byte transport envelope, zero padding, USB metadata and original capture
manifest have been removed. The calibration blob itself is unchanged. Its SHA-256
is `a566624a8fe99e4e8c3362ff58025e8c53affa5badc632a49b7a0bc5932f0267`.

The blob contains a complete bounded chain of 99 calibration records. The
project's thermal-correction and illumination-mask parser accepts both `nfov`
and `wfov-binned` records. These checks establish file integrity and compatibility
with those parsers; they do not establish measurement accuracy or fully decode
every record. Factory header fields include firmware version `6109.11`, internal
unit identifier `2525-0041746`, and sensor identifier `P8N800.00_5_9_19`.

There are no scene images, local filesystem paths, USB recordings, depth-engine
binaries or compute shaders in this sample. Sharing this calibration does not
grant rights to separate vendor software.

## What the sample enables

Use it to inspect a real calibration container, reproduce parser behavior, and
develop or compare calibration processing. The original application's source
license and third-party notices remain in the repository.

This directory is **not** a `--nvram` capture directory or a
`--native-profile-dir` bundle. It does not contain the compute programs needed
to turn raw camera data into depth. The viewer still requires a complete profile
matching the connected camera; automatic profile generation remains unfinished.
Its camera-identity checks have not been relaxed, and this sample is never
selected automatically.

For experiments involving another unit, retain both the source and target camera
identities, label results as unverified, and compare against independent known
distances at several ranges and locations in the image. Check different surfaces,
warmup conditions and each mode you intend to use. Record the errors rather than
assuming a plausible-looking depth image proves accuracy. No universal acceptable
error tolerance is claimed here.

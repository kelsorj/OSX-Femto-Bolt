#!/usr/bin/env python3
"""Verify a native raw capture and inspect published header fields, offline.

Exit 0 means the recorded files match their manifest, not that sensor samples,
calibration or depth have been decoded. No camera or SDK is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_CAPTURE_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FRAMES = 120
NVRAM_HEADER = struct.Struct("<QHQHHI")
MIPI_HEADER_BYTES = 160
MIPI_MAGIC = 0x5931


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_nvram_header(data: bytes) -> dict[str, Any]:
    """Inspect the SDK's 26-byte envelope; the enclosed calibration is opaque."""
    if len(data) < NVRAM_HEADER.size:
        raise ValueError("NVRAM transport header is shorter than 26 bytes")
    counter, extension, timestamp, width, height, size = NVRAM_HEADER.unpack_from(data)
    if size == 0:
        raise ValueError("NVRAM transport declares an empty blob")
    if size > len(data) - NVRAM_HEADER.size:
        raise ValueError("NVRAM blob size exceeds the saved payload")
    blob = memoryview(data)[NVRAM_HEADER.size:NVRAM_HEADER.size + size]
    return {
        "kind": "nvram",
        "frame_counter": counter,
        "timestamp_us": timestamp,
        "width": width,
        "height": height,
        "extension_length_raw": extension,
        "blob_offset": NVRAM_HEADER.size,
        "blob_bytes": size,
        "blob_sha256": hashlib.sha256(blob).hexdigest(),
        "trailing_bytes": len(data) - NVRAM_HEADER.size - size,
        "framed_blob_bounds_valid": True,
        "calibration_blob_decoded": False,
        "note": "The published SDK extracts bytes immediately after this fixed envelope; extension semantics are unverified.",
    }


def parse_mipi_header(data: bytes) -> dict[str, Any]:
    """Normalize only the first published header; never unpack sample planes."""
    if len(data) >= 2 and struct.unpack_from("<H", data)[0] == MIPI_MAGIC:
        if len(data) < MIPI_HEADER_BYTES:
            raise ValueError("Contiguous MIPI header is shorter than 160 bytes")
        header = data[:MIPI_HEADER_BYTES]
        encoding = "contiguous"
    else:
        if len(data) < MIPI_HEADER_BYTES * 2:
            raise ValueError("Payload has no complete contiguous or stride-2 MIPI header")
        header = data[:MIPI_HEADER_BYTES * 2:2]
        encoding = "stride2"
        if struct.unpack_from("<H", header)[0] != MIPI_MAGIC:
            raise ValueError("MIPI magic is absent after stride-2 normalization")

    def u16(offset: int) -> int:
        return struct.unpack_from("<H", header, offset)[0]

    capture = u16(98)
    phase = u16(100)
    shift = u16(104)
    frequency = u16(106)
    return {
        "kind": "mipi",
        "encoding": encoding,
        "normalized_header_bytes": len(header),
        "normalized_header_sha256": _sha256(header),
        "chip_id": u16(0),
        "header_revision": u16(2),
        "header_revision_validated": False,
        "firmware_revision_raw": struct.unpack_from("<I", header, 4)[0],
        "frame_counter": u16(8),
        "roi_number": u16(10),
        "width": u16(12),
        "height": u16(14),
        "output_format_raw": u16(22),
        "mode_raw": u16(96),
        "mode": u16(96) & 15,
        "capture_info_raw": capture,
        "capture_id": capture & 63,
        "capture_count_encoded": (capture >> 6) & 63,
        "capture_count": ((capture >> 6) & 63) + 1,
        "frequency_count_encoded": (capture >> 12) & 15,
        "phase_info_raw": phase,
        "phase_step": phase & 15,
        "phase_count_encoded": (phase >> 4) & 15,
        "frequency_id": (phase >> 8) & 15,
        "phase_shift_raw": shift,
        "phase_shift_numerator": shift & 511,
        "phase_shift_denominator": (shift >> 9) & 127,
        "modulation_frequency_encoded": frequency,
        "modulation_frequency_hz": frequency * 10000,
        "integration_time_raw": u16(110),
        "intercapture_delay_raw": u16(112),
        "payload_structure_validated": False,
        "phase_samples_decoded": False,
        "note": "Fields are read at published 160-byte-header offsets. Revision semantics, subsequent headers, sample packing and sample planes are unverified.",
    }


class _CaptureReader:
    """Read flat native capture files relative to one held directory descriptor."""

    def __init__(self, directory: Path):
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)

    def close(self) -> None:
        os.close(self.fd)

    def read(self, name: str, limit: int, expected_size: int | None = None) -> bytes:
        # Native capture files are flat. Reject directory traversal rather than
        # following arbitrary manifest paths or any intermediate symlink.
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or "/" in name or "\\" in name or "\x00" in name):
            raise ValueError("Manifest file paths must be single relative filenames")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{name}: only regular files are accepted")
            if info.st_size > limit:
                raise ValueError(f"{name}: file exceeds the allowed byte limit")
            if expected_size is not None and info.st_size != expected_size:
                raise ValueError(f"{name}: size mismatch; expected {expected_size}, found {info.st_size}")
            data = stream.read((expected_size if expected_size is not None else limit) + 1)
            if len(data) != info.st_size or len(data) > limit:
                raise ValueError(f"{name}: size changed while reading")
            return data


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _verify_file(reader: _CaptureReader, name: Any, size: int, digest: Any, limit: int) -> tuple[dict, bytes]:
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        raise ValueError(f"{name}: SHA-256 must contain exactly 64 hexadecimal characters")
    data = reader.read(name, limit, size)
    actual = _sha256(data)
    if actual != digest.lower():
        raise ValueError(f"{name}: SHA-256 mismatch")
    return {"verified": True, "bytes": len(data), "sha256": actual}, data


def _summarize_frames(records: list[dict], manifest_frames: list[dict]) -> dict[str, Any]:
    hashes: dict[str, list[int]] = defaultdict(list)
    blobs = []
    counters = []
    kinds = set()
    for i, record in enumerate(records):
        if record.get("integrity", {}).get("verified"):
            hashes[record["integrity"]["sha256"]].append(i)
        header = record.get("header", {})
        counters.append(header.get("frame_counter"))
        if header:
            kinds.add(header["kind"])
        if header.get("kind") == "nvram":
            blobs.append(header["blob_sha256"])
    modulus = 65536 if kinds == {"mipi"} else 2 ** 64 if kinds == {"nvram"} else None
    deltas = [
        (b - a) % modulus if modulus is not None and a is not None and b is not None else None
        for a, b in zip(counters, counters[1:])
    ]
    host_times = [frame.get("host_steady_ns") for frame in manifest_frames]
    valid_times = all(type(t) is int and 0 <= t <= 2 ** 63 - 1 for t in host_times)
    host_deltas = [b - a for a, b in zip(host_times, host_times[1:])] if valid_times else []
    rate = None
    if len(host_times) >= 2 and valid_times and all(delta > 0 for delta in host_deltas):
        rate = (len(host_times) - 1) * 1e9 / (host_times[-1] - host_times[0])
    return {
        "raw_payload_repetition": {
            "checked_frames": sum(len(indices) for indices in hashes.values()),
            "distinct_hashes": len(hashes),
            "repeated_payload_count": sum(len(indices) - 1 for indices in hashes.values()),
            "repeated_groups": [{"sha256": digest, "frame_indices": indices}
                                for digest, indices in hashes.items() if len(indices) > 1],
        },
        "nvram_blob_consistency": {
            "checked_frames": len(blobs),
            "distinct_blob_hashes": sorted(set(blobs)),
            "consistent": len(set(blobs)) == 1 if len(blobs) >= 2 else None,
            "note": "Identical opaque bytes do not establish correct or decoded calibration.",
        },
        "sensor_counters": {
            "values": counters, "modulus": modulus, "deltas": deltas,
            "note": "Only observed first-header counters are reported; this does not prove frame completeness or losslessness.",
        },
        "callback_timing": {
            "host_steady_ns": host_times, "deltas_ns": host_deltas,
            "observed_callback_rate_fps": rate,
            "note": "Short-burst host callback rate only. This is not sustained throughput or a validated sensor clock.",
        },
    }


def inspect_capture(capture_dir: str | Path) -> dict[str, Any]:
    """Return integrity evidence and limited header observations for one capture."""
    report: dict[str, Any] = {
        "schema_version": 1,
        "data_kind": "RAW_NOT_DEPTH",
        "ok": False,
        "file_integrity_verified": False,
        "payload_structure_validated": False,
        "calibration_blob_decoded": False,
        "phase_samples_decoded": False,
        "calibrated_depth_available": False,
        "errors": [],
        "warnings": [],
        "frames": [],
    }
    reader = None
    try:
        directory = Path(capture_dir).resolve(strict=True)
        report["capture_directory"] = str(directory)
        reader = _CaptureReader(directory)
        manifest = json.loads(reader.read("manifest.json", MAX_MANIFEST_BYTES),
                              object_pairs_hook=_unique_keys, parse_constant=_reject_nonfinite)
        if not isinstance(manifest, dict):
            raise ValueError("Manifest must be a JSON object")
        if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
            raise ValueError("Only native capture manifest schema_version 1 is supported")
        if manifest.get("data_kind") != "RAW_NOT_DEPTH":
            raise ValueError("Expected native RAW_NOT_DEPTH capture data")
        kind = manifest.get("capture_kind")
        if kind not in {"raw_transport", "opaque_nvram_transport"}:
            raise ValueError("Unsupported capture_kind")
        requested = _integer(manifest.get("requested_frames"), "requested_frames", 1, MAX_FRAMES)
        received = _integer(manifest.get("received_frames"), "received_frames", 0, requested)
        frames = manifest.get("frames")
        if not isinstance(frames, list) or len(frames) != received:
            raise ValueError("Manifest frame list and received_frames disagree")
        if type(manifest.get("complete")) is not bool:
            raise ValueError("Manifest complete must be a boolean")
        if manifest["complete"] and received != requested:
            raise ValueError("A complete capture must contain the requested frame count")
        report["source_capture_complete"] = manifest["complete"]
        report["source_capture_reason"] = manifest.get("reason")
        report["capture_kind"] = kind
        report["device"] = manifest.get("device")
        report["profile"] = manifest.get("profile")
        report["sdk_revision"] = manifest.get("sdk_revision")
        report["transport_patch"] = manifest.get("transport_patch")
        if not frames:
            raise ValueError("Manifest contains no saved frame evidence")
        if not manifest["complete"]:
            report["warnings"].append("Source capture is incomplete; verification covers only its saved files")

        sizes = []
        total = 0
        used_paths = set()
        for i, frame in enumerate(frames):
            if not isinstance(frame, dict):
                raise ValueError(f"Frame {i} must be a JSON object")
            size = _integer(frame.get("bytes"), f"frame {i} bytes", 1, MAX_FRAME_BYTES)
            metadata_size = _integer(frame.get("metadata_bytes"), f"frame {i} metadata_bytes", 0, MAX_METADATA_BYTES)
            total += size + metadata_size
            if total > MAX_CAPTURE_BYTES:
                raise ValueError("Manifest exceeds the 256 MiB capture byte budget")
            for name in (frame.get("file"), frame.get("metadata_file")):
                if name is None:
                    continue
                if not isinstance(name, str) or name in used_paths:
                    raise ValueError("Manifest file references must be unique filenames")
                used_paths.add(name)
            sizes.append((size, metadata_size))

        for i, (frame, (size, metadata_size)) in enumerate(zip(frames, sizes)):
            entry: dict[str, Any] = {"index": i, "file": frame.get("file")}
            report["frames"].append(entry)
            try:
                integrity, payload = _verify_file(reader, frame.get("file"), size, frame.get("sha256"), MAX_FRAME_BYTES)
                entry["integrity"] = integrity
                try:
                    entry["header"] = (parse_nvram_header(payload) if kind == "opaque_nvram_transport"
                                       else parse_mipi_header(payload))
                except ValueError as exc:
                    entry["header_error"] = str(exc)
            except (OSError, ValueError) as exc:
                entry["integrity"] = {"verified": False, "error": str(exc)}
                report["errors"].append(f"Frame {i}: {exc}")
            try:
                if metadata_size == 0:
                    if frame.get("metadata_file") is not None or frame.get("metadata_sha256") is not None:
                        raise ValueError("Absent metadata must have null file and SHA-256")
                    entry["metadata_integrity"] = {"verified": True, "bytes": 0, "present": False}
                else:
                    metadata, _ = _verify_file(reader, frame.get("metadata_file"), metadata_size,
                                               frame.get("metadata_sha256"), MAX_METADATA_BYTES)
                    entry["metadata_integrity"] = metadata
            except (OSError, ValueError) as exc:
                entry["metadata_integrity"] = {"verified": False, "error": str(exc)}
                report["errors"].append(f"Frame {i} metadata: {exc}")

        recognized = sum("header" in frame for frame in report["frames"])
        report["header_evidence"] = {
            "recognized_frames": recognized,
            "frame_count": len(frames),
            "all_recognized": recognized == len(frames),
            "note": "Only fixed-envelope bounds or first-header magic/fields were inspected; full sensor payloads are unvalidated.",
        }
        report.update(_summarize_frames(report["frames"], frames))
        report["file_integrity_verified"] = not report["errors"]
        report["ok"] = report["file_integrity_verified"]
        if recognized != len(frames):
            report["warnings"].append("Some saved payloads lack a recognized first header; file hashes alone cannot validate sensor data")
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        report["errors"].append(str(exc))
    finally:
        if reader is not None:
            reader.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path, help="One directory written by femto_raw_probe")
    args = parser.parse_args(argv)
    report = inspect_capture(args.capture_dir)
    json.dump(report, sys.stdout, indent=2, ensure_ascii=True, allow_nan=False)
    sys.stdout.write("\n")
    return 0 if report["file_integrity_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

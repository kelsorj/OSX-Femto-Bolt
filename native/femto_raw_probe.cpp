// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (c) 2026 pyBravo contributors.
// Extracted and adapted for OSX-Femto-Bolt; see ../NOTICE.
// Experimental native raw acquisition using the SDK's open USB transport.
// This program does not load a depth engine or produce distance measurements.
#include "probe_core.hpp"
#include "capture_ownership.hpp"
#include "Platform.hpp"
#include "common/DeviceSeriesInfo.hpp"
#include "frame/Frame.hpp"
#include "logger/Logger.hpp"
#include "stream/StreamProfile.hpp"

#include <CommonCrypto/CommonDigest.h>
#include <algorithm>
#include <csignal>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>

#ifndef FEMTO_SDK_REVISION
#define FEMTO_SDK_REVISION "unrecorded"
#endif
#ifndef FEMTO_TRANSPORT_PATCH_ID
#define FEMTO_TRANSPORT_PATCH_ID "unrecorded"
#endif

namespace fs = std::filesystem;
namespace ob = libobsensor;
using namespace femto_bolt;

namespace {
volatile std::sig_atomic_t interrupted = 0;
void on_signal(int) { interrupted = 1; }

const char *usage =
    "femto_raw_probe --check-build  (initialization check without USB access)\n"
    "femto_raw_probe --list [--serial SERIAL]\n"
    "femto_raw_probe --profiles [--serial SERIAL]\n"
    "femto_raw_probe --capture NEW_DIRECTORY --profile-index N [--serial SERIAL] [--frames 4] [--timeout 10]\n"
    "femto_raw_probe --nvram NEW_DIRECTORY [--serial SERIAL] [--frames 4] [--timeout 10]\n"
    "Limits: 1-120 frames, 1-30 seconds frame wait, 256 MiB payload/metadata total. Output is RAW_NOT_DEPTH.\n"
    "The frame-wait timeout does not guarantee a bound on SDK initialization or USB cleanup.\n"
    "--nvram records the opaque firmware-data stream; it does not decode calibration.\n";

std::string port_json(const ob::USBSourcePortInfo &port) {
    std::ostringstream out;
    out << "{\"vendor_id\":11205,\"product_id\":1643,\"interface\":2,\"serial\":" << json_string(port.serial)
        << ",\"usb_connection\":" << json_string(port.connSpec) << ",\"usb_url\":" << json_string(port.url)
        << ",\"interface_url\":" << json_string(port.infUrl) << "}";
    return out.str();
}

std::string profile_json(const ob::VideoStreamProfile &profile, std::size_t index) {
    std::ostringstream out;
    out << "{\"index\":" << index << ",\"width\":" << profile.getWidth() << ",\"height\":" << profile.getHeight()
        << ",\"fps\":" << profile.getFps() << ",\"sdk_format\":" << int(profile.getFormat())
        << ",\"sdk_stream_type\":" << int(profile.getType()) << ",\"meaning\":\"raw UVC transport profile, not depth-image dimensions\"}";
    return out.str();
}

void write_bytes(const fs::path &path, const std::vector<std::uint8_t> &bytes) {
    std::ofstream out(path, std::ios::binary | std::ios::out);
    out.exceptions(std::ios::badbit | std::ios::failbit);
    out.write(reinterpret_cast<const char *>(bytes.data()), std::streamsize(bytes.size()));
    out.close();
}

std::string sha256(const std::vector<std::uint8_t> &bytes) {
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(bytes.data(), static_cast<CC_LONG>(bytes.size()), digest);
    std::ostringstream out;
    for(const auto byte : digest) out << std::hex << std::setw(2) << std::setfill('0') << unsigned(byte);
    return out.str();
}

std::string manifest_prefix(const Options &options, const ob::USBSourcePortInfo &device,
                            const ob::VideoStreamProfile &profile, std::size_t profile_index,
                            std::size_t received_frames, bool complete, const std::string &reason) {
    std::ostringstream manifest;
    manifest << "{\n\"schema_version\":1,\"data_kind\":\"RAW_NOT_DEPTH\",\"depth_engine_used\":false,"
             << "\"calibrated_depth_available\":false,\"payload_structure_validated\":false,\"calibration_blob_decoded\":false,"
             << "\"sdk_revision\":" << json_string(FEMTO_SDK_REVISION) << ",\n"
             << "\"transport_patch\":" << json_string(FEMTO_TRANSPORT_PATCH_ID) << ",\n"
             << "\"capture_kind\":" << json_string(options.mode == Mode::Nvram ? "opaque_nvram_transport" : "raw_transport")
             << ",\"complete\":" << (complete ? "true" : "false") << ",\"reason\":" << json_string(reason)
             << ",\"requested_frames\":" << options.frames << ",\"received_frames\":" << received_frames
             << ",\"timeout_seconds\":" << options.timeout_seconds << ",\"device\":" << port_json(device)
             << ",\"profile\":" << profile_json(profile, profile_index)
             << ",\"completion_note\":\"complete means the requested callback payload count was recorded; sensor payload structure and validity are unverified.\""
             << ",\"payload_note\":\"SDK-delivered assembled UVC payload; not a USB packet recording. Frames rejected before callback are not counted.\""
             << ",\"metadata_note\":\"The SDK retains at most 12 bytes of UVC payload header and 255 bytes of additional metadata before this callback. Saved metadata is not a complete USB header recording.\""
             << ",\"counter_note\":\"sdk_sequence is the backend callback count, not a validated sensor frame counter.\""
             << ",\"timestamp_note\":\"sdk_timestamp_raw is the low-level UVC backend value; its units and device-clock meaning are unverified. Host timestamps are nanoseconds.\",\n\"frames\":[\n";
    return manifest.str();
}

int capture(const Options &options, const std::shared_ptr<ob::IVideoStreamPort> &stream,
            const ob::USBSourcePortInfo &device, const std::shared_ptr<const ob::VideoStreamProfile> &profile,
            std::size_t profile_index) {
    const auto directory = fs::absolute(options.directory);
    if(!fs::create_directory(directory)) throw std::runtime_error("Output directory already exists; refusing to overwrite it");
    fs::permissions(directory, fs::perms::owner_all);
    CaptureOwnership ownership(directory);
    // Captures and per-device calibration must not be committed if an operator
    // chooses a directory inside a repository. Only this ignore rule is visible.
    {
        std::ofstream ignore(directory / ".gitignore");
        ignore.exceptions(std::ios::badbit | std::ios::failbit);
        ignore << "*\n!.gitignore\n";
        ignore.close();
    }
    // Establish source/provenance before any stream or payload file exists. A
    // failed payload write must leave this explicitly incomplete record intact.
    write_atomic_text(directory / "manifest.json",
                      manifest_prefix(options, device, *profile, profile_index, 0, false, "Capture/persistence pending") + "\n]}\n");
    auto buffer = std::make_shared<CaptureBuffer>(options.frames);
    const auto started = std::chrono::steady_clock::now();
    bool start_attempted = false;
    std::string reason;
    try {
        start_attempted = true;
        stream->startStream(profile, [buffer](std::shared_ptr<ob::Frame> frame) {
            try {
                if(!frame) { buffer->fail("SDK supplied a null frame"); return; }
                FrameRecord record;
                record.host_wall_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
                record.host_steady_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
                record.sdk_sequence = frame->getNumber();
                record.sdk_timestamp_raw = frame->getTimeStampUsec();
                record.sdk_system_timestamp_us = frame->getSystemTimeStampUsec();
                buffer->add(frame->getData(), frame->getDataSize(), frame->getMetadata(), frame->getMetadataSize(), std::move(record));
            }
            catch(const std::exception &exc) { buffer->fail(std::string("SDK callback failed: ") + exc.what()); }
            catch(...) { buffer->fail("SDK callback failed with an unknown exception"); }
        });
        const auto deadline = started + std::chrono::seconds(options.timeout_seconds);
        while(!interrupted && std::chrono::steady_clock::now() < deadline) {
            if(buffer->wait_until(std::min(deadline, std::chrono::steady_clock::now() + std::chrono::milliseconds(100)))) break;
        }
        buffer->finish(interrupted ? "Interrupted by operator" : "Capture timed out before the requested frame count");
    }
    catch(const std::exception &exc) { buffer->fail(std::string("Stream start/capture failed: ") + exc.what()); }
    catch(...) { buffer->fail("Stream start/capture failed with an unknown exception"); }
    // Also attempt cleanup when startStream threw after partially opening USB.
    if(start_attempted) {
        try { stream->stopAllStream(); }
        catch(const std::exception &exc) { buffer->fail(std::string("Stream cleanup failed: ") + exc.what()); }
        catch(...) { buffer->fail("Stream cleanup failed with an unknown exception"); }
    }
    auto frames = buffer->take_frames();
    reason = buffer->error();
    const bool complete = frames.size() == options.frames && reason.empty();
    std::ostringstream manifest;
    manifest << manifest_prefix(options, device, *profile, profile_index, frames.size(), complete, reason);
    for(std::size_t i = 0; i < frames.size(); ++i) {
        const auto &frame = frames[i];
        std::ostringstream base;
        base << "frame_" << std::setw(6) << std::setfill('0') << i;
        const auto file = base.str() + ".bin";
        write_bytes(directory / file, frame.bytes);
        const auto metadata_file = base.str() + ".metadata.bin";
        if(!frame.metadata.empty()) write_bytes(directory / metadata_file, frame.metadata);
        if(i) manifest << ",\n";
        manifest << "{\"file\":" << json_string(file) << ",\"bytes\":" << frame.bytes.size()
                 << ",\"sha256\":" << json_string(sha256(frame.bytes))
                 << ",\"metadata_file\":" << (frame.metadata.empty() ? "null" : json_string(metadata_file))
                 << ",\"metadata_sha256\":" << (frame.metadata.empty() ? "null" : json_string(sha256(frame.metadata)))
                 << ",\"metadata_bytes\":" << frame.metadata.size() << ",\"sdk_sequence\":" << frame.sdk_sequence
                 << ",\"sdk_timestamp_raw\":" << frame.sdk_timestamp_raw << ",\"sdk_system_timestamp_us\":" << frame.sdk_system_timestamp_us
                 << ",\"host_wall_ns\":" << frame.host_wall_ns << ",\"host_steady_ns\":" << frame.host_steady_ns << "}";
    }
    manifest << "\n]}\n";
    write_atomic_text(directory / "manifest.json", manifest.str());
    ownership.finish();
    std::cout << "{\"data_kind\":\"RAW_NOT_DEPTH\",\"complete\":" << (complete ? "true" : "false")
              << ",\"frames\":" << frames.size() << ",\"reason\":" << json_string(reason)
              << ",\"manifest\":" << json_string((directory / "manifest.json").string()) << "}\n";
    return complete ? 0 : 4;
}
} // namespace

int main(int argc, char **argv) {
    try {
        Options options = parse_options(std::vector<std::string>(argv + 1, argv + argc));
        if(options.mode == Mode::Help) { std::cout << usage; return 0; }
        if(!options.directory.empty()) options.directory = fs::absolute(options.directory).string();
        // SDK configuration lookup must not inherit a writable working directory
        // when this native USB executable is invoked with sudo.
        if(::chdir("/") != 0) throw std::runtime_error("Cannot select the trusted SDK working directory");
        // Unit calibration is private data; files are owner-readable only.
        ::umask(0077);
        if(!options.directory.empty() && fs::exists(options.directory))
            throw std::invalid_argument("Output directory already exists; refusing to overwrite it");
        std::signal(SIGINT, on_signal);
        std::signal(SIGTERM, on_signal);
        ob::Logger::setLogSeverity(OB_LOG_SEVERITY_OFF);
        ob::Logger::setLogCallback(OB_LOG_SEVERITY_WARN, [](OBLogSeverity severity, const std::string &message) {
            static std::mutex output_mutex;
            std::lock_guard<std::mutex> lock(output_mutex);
            std::cerr << "{\"sdk_log_severity\":" << int(severity) << ",\"message\":" << json_string(message) << "}\n";
        });
        const auto logger = ob::Logger::getInstance();
        // The public Context normally initializes this whitelist. This probe
        // intentionally bypasses Context/Pipeline, so it must retain the same
        // manager before querying USB through the lower-level Platform layer.
        const auto device_series = ob::DeviceSeriesInfoManager::getInstance();
        if(!ob::isSupportedDevice(0x2bc5, 0x066b))
            throw std::runtime_error("SDK device initialization did not enable the Femto Bolt USB identifier");
        if(options.mode == Mode::CheckBuild) {
            std::cout << "{\"status\":\"build_checked\",\"sdk_revision\":" << json_string(FEMTO_SDK_REVISION)
                      << ",\"transport_patch\":" << json_string(FEMTO_TRANSPORT_PATCH_ID)
                      << ",\"bolt_usb_id_supported\":true,\"usb_access_attempted\":false,\"depth_engine_used\":false}\n";
            return 0;
        }
        auto platform = ob::Platform::getInstance();
        std::vector<std::shared_ptr<const ob::USBSourcePortInfo>> devices;
        std::vector<DeviceDescriptor> descriptors;
        for(const auto &port : platform->queryUsbSourcePortInfos(false)) {
            auto usb = std::dynamic_pointer_cast<const ob::USBSourcePortInfo>(port);
            if(!usb) continue;
            devices.push_back(usb);
            descriptors.push_back({usb->vid, usb->pid, usb->infIndex, usb->portType == ob::SOURCE_PORT_USB_UVC, usb->serial});
        }
        if(options.mode == Mode::List) {
            const auto matches = matching_device_indices(descriptors, options.serial);
            std::cout << "{\"status\":" << json_string(matches.empty() ? "no_matching_accessible_device" : "enumerated") << ",\"devices\":[";
            for(std::size_t i = 0; i < matches.size(); ++i) { if(i) std::cout << ','; std::cout << port_json(*devices[matches[i]]); }
            std::cout << "]}\n";
            return matches.empty() ? 2 : 0;
        }
        const auto device = devices[select_unique_device(descriptors, options.serial)];
        auto stream = std::dynamic_pointer_cast<ob::IVideoStreamPort>(platform->getSourcePort(device));
        if(!stream) throw std::runtime_error("The selected Bolt interface does not expose a video stream");
        const auto profiles = stream->getStreamProfileList();
        if(options.mode == Mode::Profiles) {
            std::cout << "{\"device\":" << port_json(*device) << ",\"profiles\":[";
            bool comma = false;
            for(std::size_t i = 0; i < profiles.size(); ++i) {
                auto profile = std::dynamic_pointer_cast<const ob::VideoStreamProfile>(profiles[i]);
                if(!profile) continue;
                if(comma) std::cout << ',';
                comma = true;
                std::cout << profile_json(*profile, i);
            }
            std::cout << "]}\n";
            return 0;
        }
        std::vector<ProfileDescriptor> profile_descriptors;
        for(const auto &sp : profiles) {
            const auto p = std::dynamic_pointer_cast<const ob::VideoStreamProfile>(sp);
            profile_descriptors.push_back(p ? ProfileDescriptor{true, p->getWidth(), p->getHeight(), p->getFps()} : ProfileDescriptor{});
        }
        const auto index = select_profile_index(profile_descriptors, options.profile_index, options.mode == Mode::Nvram);
        auto profile = std::dynamic_pointer_cast<const ob::VideoStreamProfile>(profiles[index]);
        return capture(options, stream, *device, profile, index);
    }
    catch(const std::exception &exc) {
        std::cerr << "{\"status\":\"error\",\"message\":" << json_string(exc.what()) << "}\n";
        return 1;
    }
}

// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (c) 2026 pyBravo contributors.
// Extracted and adapted for OSX-Femto-Bolt; see ../NOTICE.
// Standalone capture bookkeeping. No SDK or hardware dependency.
#pragma once

#include <chrono>
#include <cerrno>
#include <condition_variable>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fcntl.h>
#include <limits>
#include <mutex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <system_error>
#include <unistd.h>
#include <vector>

namespace femto_bolt {

constexpr std::size_t kMaxFrameBytes = 32U * 1024U * 1024U;
constexpr std::size_t kMaxMetadataBytes = 1024U * 1024U;
constexpr std::size_t kMaxCaptureBytes = 256U * 1024U * 1024U;

enum class Mode { List, Profiles, Capture, Nvram, CheckBuild, Help };

struct Options {
    Mode mode = Mode::List;
    std::string serial;
    std::string directory;
    std::size_t profile_index = 0;
    unsigned frames = 4;
    unsigned timeout_seconds = 10;
};

inline unsigned parse_unsigned(const std::string &text, unsigned maximum, const std::string &name) {
    if(text.empty()) throw std::invalid_argument(name + " needs an unsigned integer");
    unsigned value = 0;
    for(const unsigned char c : text) {
        if(c < '0' || c > '9' || value > maximum / 10 || (value == maximum / 10 && unsigned(c - '0') > maximum % 10))
            throw std::invalid_argument(name + " is outside its allowed range");
        value = value * 10 + unsigned(c - '0');
    }
    return value;
}

inline Options parse_options(const std::vector<std::string> &args) {
    Options options;
    std::set<std::string> seen;
    bool mode_set = false;
    for(std::size_t i = 0; i < args.size(); ++i) {
        const auto &arg = args[i];
        if(!seen.insert(arg).second) throw std::invalid_argument("Repeated option: " + arg);
        auto value = [&]() -> const std::string & {
            if(++i >= args.size() || args[i].empty() || args[i].rfind("--", 0) == 0)
                throw std::invalid_argument("Missing value for " + arg);
            return args[i];
        };
        if(arg == "--list" || arg == "--profiles" || arg == "--capture" || arg == "--nvram" || arg == "--check-build" || arg == "--help") {
            if(mode_set) throw std::invalid_argument("Choose exactly one action");
            mode_set = true;
            if(arg == "--list") options.mode = Mode::List;
            if(arg == "--profiles") options.mode = Mode::Profiles;
            if(arg == "--check-build") options.mode = Mode::CheckBuild;
            if(arg == "--help") options.mode = Mode::Help;
            if(arg == "--capture" || arg == "--nvram") {
                options.mode = arg == "--capture" ? Mode::Capture : Mode::Nvram;
                options.directory = value();
            }
        }
        else if(arg == "--serial") options.serial = value();
        else if(arg == "--profile-index") options.profile_index = parse_unsigned(value(), 10000, arg);
        else if(arg == "--frames") options.frames = parse_unsigned(value(), 120, arg);
        else if(arg == "--timeout") options.timeout_seconds = parse_unsigned(value(), 30, arg);
        else throw std::invalid_argument("Unknown option: " + arg);
    }
    const bool capture = options.mode == Mode::Capture || options.mode == Mode::Nvram;
    if(options.frames == 0 || options.timeout_seconds == 0) throw std::invalid_argument("Frames and timeout must be positive");
    if(options.mode == Mode::Capture && !seen.count("--profile-index"))
        throw std::invalid_argument("--capture requires an enumerated --profile-index");
    if(options.mode != Mode::Capture && seen.count("--profile-index"))
        throw std::invalid_argument("--profile-index is only valid with --capture");
    if(!capture && (seen.count("--frames") || seen.count("--timeout")))
        throw std::invalid_argument("Frame and timeout options require capture");
    if(options.mode == Mode::CheckBuild && seen.count("--serial"))
        throw std::invalid_argument("--check-build does not access or select devices");
    return options;
}

inline bool is_bolt_depth_port(unsigned vendor, unsigned product, unsigned interface_index, bool is_uvc) {
    return vendor == 0x2bc5 && product == 0x066b && interface_index == 2 && is_uvc;
}

struct DeviceDescriptor {
    unsigned vendor = 0, product = 0, interface_index = 0;
    bool is_uvc = false;
    std::string serial;
};

inline std::vector<std::size_t> matching_device_indices(const std::vector<DeviceDescriptor> &devices, const std::string &requested_serial) {
    std::vector<std::size_t> matches;
    for(std::size_t i = 0; i < devices.size(); ++i) {
        const auto &d = devices[i];
        if(is_bolt_depth_port(d.vendor, d.product, d.interface_index, d.is_uvc)
           && (requested_serial.empty() || d.serial == requested_serial)) matches.push_back(i);
    }
    return matches;
}

inline std::size_t select_unique_device(const std::vector<DeviceDescriptor> &devices, const std::string &requested_serial) {
    const auto matches = matching_device_indices(devices, requested_serial);
    if(matches.empty()) throw std::runtime_error("No matching accessible Femto Bolt depth interface. Check power, USB 3 data connection, and macOS USB access.");
    if(matches.size() != 1) throw std::runtime_error("Multiple matching Bolt interfaces; select one unique --serial");
    return matches.front();
}

struct ProfileDescriptor {
    bool is_video = false;
    unsigned width = 0, height = 0, fps = 0;
};

inline std::size_t select_profile_index(const std::vector<ProfileDescriptor> &profiles, std::size_t requested_index, bool nvram) {
    if(!nvram) {
        if(requested_index >= profiles.size()) throw std::invalid_argument("Profile index is outside the enumerated profile list");
        if(!profiles[requested_index].is_video) throw std::invalid_argument("Selected profile is not a video transport profile");
        return requested_index;
    }
    std::vector<std::size_t> matches;
    for(std::size_t i = 0; i < profiles.size(); ++i) {
        const auto &p = profiles[i];
        if(p.is_video && p.width == 1024 && p.height == 512 && p.fps == 5) matches.push_back(i);
    }
    if(matches.size() != 1) throw std::runtime_error("A unique 1024x512@5 firmware-data profile is required; inspect --profiles");
    return matches.front();
}

inline std::string json_string(const std::string &text) {
    const char *digits = "0123456789abcdef";
    std::string out = "\"";
    for(std::size_t i = 0; i < text.size(); ++i) {
        const auto c = static_cast<unsigned char>(text[i]);
        if(c == '\\' || c == '"') { out += '\\'; out += char(c); }
        else if(c < 0x20 || c >= 0x7f) {
            // Preserve Unicode paths/descriptors. Invalid descriptor bytes are
            // represented individually, without emitting malformed JSON text.
            std::size_t length = c >= 0xc2 && c <= 0xdf ? 2
                               : c >= 0xe0 && c <= 0xef ? 3
                               : c >= 0xf0 && c <= 0xf4 ? 4 : 0;
            bool valid = length != 0 && length <= text.size() - i;
            for(std::size_t j = 1; valid && j < length; ++j) {
                const auto next = static_cast<unsigned char>(text[i + j]);
                valid = next >= 0x80 && next <= 0xbf;
            }
            if(valid) {
                const auto second = static_cast<unsigned char>(text[i + 1]);
                valid = !(c == 0xe0 && second < 0xa0)    // overlong encoding
                     && !(c == 0xed && second > 0x9f)    // surrogate code point
                     && !(c == 0xf0 && second < 0x90)    // overlong encoding
                     && !(c == 0xf4 && second > 0x8f);   // above U+10FFFF
            }
            if(valid) {
                out.append(text, i, length);
                i += length - 1;
                continue;
            }
            out += "\\u00"; out += digits[c >> 4]; out += digits[c & 15];
        }
        else out += char(c);
    }
    return out + '"';
}

// One writer owns a capture directory. Exclusive staging prevents clobbering a
// leftover or concurrent stage, and the old manifest survives any failed write.
inline void write_atomic_text(const std::filesystem::path &target, const std::string &text) {
    const std::filesystem::path staged = target.string() + ".tmp";
    int fd = ::open(staged.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
    if(fd < 0) throw std::system_error(errno, std::generic_category(), "Create staged manifest");
    try {
        std::size_t offset = 0;
        while(offset < text.size()) {
            const auto written = ::write(fd, text.data() + offset, text.size() - offset);
            if(written < 0 && errno == EINTR) continue;
            if(written <= 0) throw std::system_error(written == 0 ? EIO : errno, std::generic_category(), "Write staged manifest");
            offset += static_cast<std::size_t>(written);
        }
        if(::fsync(fd) != 0) throw std::system_error(errno, std::generic_category(), "Sync staged manifest");
        const int closed = ::close(fd);
        fd = -1;
        if(closed != 0) throw std::system_error(errno, std::generic_category(), "Close staged manifest");
        if(::rename(staged.c_str(), target.c_str()) != 0)
            throw std::system_error(errno, std::generic_category(), "Replace capture manifest");
    }
    catch(...) {
        if(fd >= 0) ::close(fd);
        ::unlink(staged.c_str());
        throw;
    }
}

struct FrameRecord {
    std::vector<std::uint8_t> bytes;
    std::vector<std::uint8_t> metadata;
    std::uint64_t sdk_sequence = 0;
    std::uint64_t sdk_timestamp_raw = 0;
    std::uint64_t sdk_system_timestamp_us = 0;
    std::int64_t host_wall_ns = 0;
    std::int64_t host_steady_ns = 0;
};

// SDK callbacks only copy bounded data into this buffer. File I/O happens after
// the stream stops. A rejected frame terminates capture; it is never padded.
class CaptureBuffer {
public:
    explicit CaptureBuffer(unsigned target, std::size_t budget = kMaxCaptureBytes) : target_(target), budget_(budget) {
        if(target == 0 || target > 120 || budget == 0 || budget > kMaxCaptureBytes)
            throw std::invalid_argument("Invalid capture limits");
    }

    bool add(const std::uint8_t *data, std::size_t size, const std::uint8_t *metadata, std::size_t metadata_size, FrameRecord record) {
        std::lock_guard<std::mutex> lock(mutex_);
        if(!accepting_) return false;
        if(!data || size == 0 || size > kMaxFrameBytes || metadata_size > kMaxMetadataBytes || (metadata_size && !metadata)) {
            fail_locked("Invalid or oversized SDK frame"); return false;
        }
        if(size > budget_ - total_bytes_ || metadata_size > budget_ - total_bytes_ - size) {
            fail_locked("Capture byte budget exhausted"); return false;
        }
        try {
            record.bytes.assign(data, data + size);
            record.metadata.clear();
            if(metadata_size) record.metadata.assign(metadata, metadata + metadata_size);
            frames_.push_back(std::move(record));
            total_bytes_ += size + metadata_size;
        }
        catch(const std::exception &exc) { fail_locked(std::string("Frame copy failed: ") + exc.what()); return false; }
        if(frames_.size() == target_) accepting_ = false;
        cv_.notify_all();
        return true;
    }

    void fail(const std::string &reason) {
        std::lock_guard<std::mutex> lock(mutex_);
        fail_locked(reason);
    }

    bool wait_until(std::chrono::steady_clock::time_point deadline) {
        std::unique_lock<std::mutex> lock(mutex_);
        return cv_.wait_until(lock, deadline, [&] { return !accepting_; });
    }

    void finish(const std::string &incomplete_reason) {
        std::lock_guard<std::mutex> lock(mutex_);
        if(accepting_) fail_locked(incomplete_reason);
    }

    std::vector<FrameRecord> take_frames() {
        std::lock_guard<std::mutex> lock(mutex_);
        if(accepting_) throw std::logic_error("Cannot read an active capture");
        return std::move(frames_);
    }

    std::string error() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return error_;
    }

private:
    void fail_locked(const std::string &reason) {
        if(error_.empty()) error_ = reason;
        accepting_ = false;
        cv_.notify_all();
    }
    unsigned target_;
    std::size_t budget_, total_bytes_ = 0;
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    bool accepting_ = true;
    std::string error_;
    std::vector<FrameRecord> frames_;
};

} // namespace femto_bolt

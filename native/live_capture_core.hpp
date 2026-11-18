// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (c) 2026 pyBravo contributors.
// Extracted and adapted for OSX-Femto-Bolt; see ../NOTICE.
// Bounded wire framing and latest-frame buffering; no SDK or hardware needed.
#pragma once

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace femto_bolt_live {

constexpr std::size_t max_payload_bytes = 16U * 1024U * 1024U;

enum class DepthMode { nfov, wfov_binned };

struct DepthTransportProfile {
    const char *name;
    std::uint32_t width, height, engine_mode;
};

inline DepthMode parse_depth_mode(const std::string &name) {
    if (name == "nfov") return DepthMode::nfov;
    if (name == "wfov-binned") return DepthMode::wfov_binned;
    throw std::invalid_argument("--depth-mode must be nfov or wfov-binned");
}

inline DepthTransportProfile depth_transport_profile(DepthMode mode) {
    // Orbbec SDK v2.10.6 RawPhaseStreamer::backendProfileMap_ and
    // getDepthEngineMode(): these are raw transport sizes, not depth pixels.
    switch (mode) {
        case DepthMode::nfov: return {"nfov", 7680, 434, 4};
        case DepthMode::wfov_binned: return {"wfov-binned", 8192, 290, 7};
    }
    throw std::invalid_argument("Unknown depth mode");
}

inline bool matches_depth_profile(DepthMode mode, std::uint32_t width, std::uint32_t height,
                                  unsigned fps, unsigned requested_fps, std::uint32_t format) {
    const auto expected = depth_transport_profile(mode);
    return width == expected.width && height == expected.height && fps == requested_fps && format == 8;
}

struct Packet {
    std::uint32_t kind = 0, width = 0, height = 0, format = 0;
    std::uint64_t sequence = 0, steady_ns = 0, wall_ns = 0, sdk_timestamp_raw = 0;
    std::vector<std::uint8_t> payload;
};

inline std::array<std::uint8_t, 64> wire_header(const Packet &packet) {
    if (packet.kind > 2 || packet.payload.empty() || packet.payload.size() > max_payload_bytes)
        throw std::invalid_argument("Invalid live packet kind or payload size");
    std::array<std::uint8_t, 64> header{};
    const std::string magic = "FBLIVE01";
    std::copy(magic.begin(), magic.end(), header.begin());
    auto put = [&](std::size_t offset, std::uint64_t value, std::size_t count) {
        for (std::size_t i = 0; i < count; ++i) header[offset+i] = static_cast<std::uint8_t>(value >> (8*i));
    };
    put(8, packet.kind, 4);
    put(12, packet.payload.size(), 4);
    put(16, packet.width, 4);
    put(20, packet.height, 4);
    put(24, packet.format, 4);
    put(32, packet.sequence, 8);
    put(40, packet.steady_ns, 8);
    put(48, packet.wall_ns, 8);
    put(56, packet.sdk_timestamp_raw, 8);
    return header;
}

class LatestFrames {
    std::mutex mutex_;
    std::condition_variable changed_;
    std::array<std::optional<Packet>, 2> slots_;
    std::array<std::uint64_t, 2> received_{};
    std::array<std::uint64_t, 2> replaced_{};
    std::array<std::chrono::steady_clock::time_point, 2> arrivals_{};
    std::string error_;
    bool stopped_ = false;
public:
    void publish(Packet packet) {
        if (packet.kind < 1 || packet.kind > 2 || packet.payload.empty() || packet.payload.size() > max_payload_bytes)
            throw std::invalid_argument("Invalid camera payload");
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopped_) return;
        auto index = packet.kind - 1;
        packet.sequence = ++received_[index];
        if (slots_[index]) ++replaced_[index];
        arrivals_[index] = std::chrono::steady_clock::now();
        slots_[index] = std::move(packet);
        changed_.notify_one();
    }
    std::optional<Packet> take(std::chrono::milliseconds wait) {
        std::unique_lock<std::mutex> lock(mutex_);
        changed_.wait_for(lock, wait, [&] { return stopped_ || !error_.empty() || slots_[0] || slots_[1]; });
        if (!error_.empty()) throw std::runtime_error(error_);
        if (!slots_[0] && !slots_[1]) return std::nullopt;
        std::size_t index = !slots_[0] ? 1 : !slots_[1] ? 0 : slots_[0]->steady_ns <= slots_[1]->steady_ns ? 0 : 1;
        auto packet = std::move(slots_[index]);
        slots_[index].reset();
        return packet;
    }
    void fail(const std::string &error) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (error_.empty()) error_ = error;
        changed_.notify_one();
    }
    void stop() {
        std::lock_guard<std::mutex> lock(mutex_);
        stopped_ = true;
        changed_.notify_all();
    }
    bool stale(std::chrono::steady_clock::time_point start, std::chrono::seconds timeout) {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto now = std::chrono::steady_clock::now();
        for (auto arrival : arrivals_) {
            if (arrival == std::chrono::steady_clock::time_point{}) arrival = start;
            if (now - arrival > timeout) return true;
        }
        return false;
    }
    std::array<std::uint64_t, 4> counts() {
        std::lock_guard<std::mutex> lock(mutex_);
        return {received_[0], received_[1], replaced_[0], replaced_[1]};
    }
};
} // namespace femto_bolt_live

// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (c) 2026 pyBravo contributors. See ../NOTICE.
// Synthetic capture-core checks. No SDK, USB access, sleeps, or camera data.
#include "live_capture_core.hpp"
#include <cassert>
#include <iostream>

using namespace femto_bolt_live;
using namespace std::chrono_literals;

Packet frame(std::uint32_t kind, std::uint64_t timestamp) {
    Packet p;
    p.kind = kind;
    p.steady_ns = timestamp;
    p.payload = {1, 2, 3};
    return p;
}

int main(int argc, char **argv) {
    assert(argc == 2);
    std::string scenario = argv[1];
    if (scenario == "wire") {
        auto p = frame(2, 0x1020304050607080ULL);
        p.width = 640; p.height = 480; p.format = 5;
        p.sequence = 0x1122334455667788ULL;
        p.wall_ns = 0x2233445566778899ULL;
        p.sdk_timestamp_raw = 0x33445566778899aaULL;
        const auto header = wire_header(p);
        std::cout.write(reinterpret_cast<const char *>(header.data()), header.size());
    } else if (scenario == "bounded") {
        LatestFrames frames;
        for (int i = 0; i < 1000; ++i) {
            frames.publish(frame(1, i * 2));
            frames.publish(frame(2, i * 2 + 1));
        }
        assert((frames.counts() == std::array<std::uint64_t, 4>{1000, 1000, 999, 999}));
        auto depth = frames.take(0ms), rgb = frames.take(0ms);
        assert(depth && depth->kind == 1 && depth->sequence == 1000 && depth->steady_ns == 1998);
        assert(rgb && rgb->kind == 2 && rgb->sequence == 1000 && rgb->steady_ns == 1999);
        assert(!frames.take(0ms));
    } else if (scenario == "oldest") {
        LatestFrames frames;
        frames.publish(frame(1, 30));
        frames.publish(frame(2, 20));
        assert(frames.take(0ms)->kind == 2);
        assert(frames.take(0ms)->kind == 1);
    } else if (scenario == "stop") {
        LatestFrames frames;
        frames.publish(frame(1, 1));
        frames.stop();
        frames.publish(frame(2, 2));
        assert(frames.take(0ms)->kind == 1);
        assert(!frames.take(0ms));
        assert((frames.counts() == std::array<std::uint64_t, 4>{1, 0, 0, 0}));
    } else if (scenario == "failure") {
        LatestFrames frames;
        frames.publish(frame(1, 1));
        frames.fail("first failure");
        frames.fail("second failure");
        try { frames.take(0ms); assert(false); }
        catch (const std::runtime_error &error) { assert(std::string(error.what()) == "first failure"); }
    } else if (scenario == "missing_stream_stale") {
        LatestFrames frames;
        frames.publish(frame(1, 1));
        assert(frames.stale(std::chrono::steady_clock::now() - 10s, 5s));
        frames.publish(frame(2, 2));
        assert(!frames.stale(std::chrono::steady_clock::now() - 10s, 5s));
    } else if (scenario == "invalid_kind" || scenario == "empty" || scenario == "oversized") {
        auto p = frame(3, 0);
        if (scenario == "empty") { p.kind = 1; p.payload.clear(); }
        if (scenario == "oversized") { p.kind = 1; p.payload.resize(max_payload_bytes + 1); }
        try { wire_header(p); assert(false); } catch (const std::invalid_argument &) {}
        LatestFrames frames;
        try { frames.publish(p); assert(false); } catch (const std::invalid_argument &) {}
        assert(!frames.take(0ms));
    } else if (scenario == "hello") {
        auto p = frame(0, 0);
        assert(wire_header(p)[8] == 0);
        LatestFrames frames;
        try { frames.publish(p); assert(false); } catch (const std::invalid_argument &) {}
    } else if (scenario == "payload_limit") {
        auto p = frame(1, 0);
        p.payload.resize(max_payload_bytes);
        assert(wire_header(p)[15] == 1);
        LatestFrames frames;
        frames.publish(std::move(p));
        assert(frames.take(0ms)->payload.size() == max_payload_bytes);
    } else if (scenario == "mode_nfov") {
        assert(parse_depth_mode("nfov") == DepthMode::nfov);
        const auto p = depth_transport_profile(DepthMode::nfov);
        assert(std::string(p.name) == "nfov" && p.width == 7680 && p.height == 434 && p.engine_mode == 4);
    } else if (scenario == "mode_wide") {
        assert(parse_depth_mode("wfov-binned") == DepthMode::wfov_binned);
        const auto p = depth_transport_profile(DepthMode::wfov_binned);
        assert(std::string(p.name) == "wfov-binned" && p.width == 8192 && p.height == 290 && p.engine_mode == 7);
    } else if (scenario == "mode_invalid") {
        try { parse_depth_mode("wfov"); assert(false); } catch (const std::invalid_argument &) {}
        try { parse_depth_mode("7"); assert(false); } catch (const std::invalid_argument &) {}
        try { parse_depth_mode(""); assert(false); } catch (const std::invalid_argument &) {}
        try { depth_transport_profile(static_cast<DepthMode>(99)); assert(false); }
        catch (const std::invalid_argument &) {}
    } else if (scenario == "mode_profiles") {
        assert(matches_depth_profile(DepthMode::nfov, 7680, 434, 15, 15, 8));
        assert(matches_depth_profile(DepthMode::wfov_binned, 8192, 290, 15, 15, 8));
        assert(!matches_depth_profile(DepthMode::nfov, 8192, 290, 15, 15, 8));
        assert(!matches_depth_profile(DepthMode::wfov_binned, 7680, 434, 15, 15, 8));
        assert(!matches_depth_profile(DepthMode::wfov_binned, 8192, 290, 15, 30, 8));
        assert(!matches_depth_profile(DepthMode::wfov_binned, 8192, 290, 15, 15, 5));
        assert(!matches_depth_profile(DepthMode::wfov_binned, 8192, 291, 15, 15, 8));
        assert(!matches_depth_profile(DepthMode::wfov_binned, 512, 512, 15, 15, 8));
    } else {
        return 2;
    }
}

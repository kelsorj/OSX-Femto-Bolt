// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (c) 2026 pyBravo contributors.
// Extracted and adapted for OSX-Femto-Bolt; see ../NOTICE.
// Native RGB + raw-phase transport for a local depth-conversion bridge.
#include "live_capture_core.hpp"
#include "probe_core.hpp"
#include "Platform.hpp"
#include "common/DeviceSeriesInfo.hpp"
#include "frame/Frame.hpp"
#include "logger/Logger.hpp"
#include "stream/StreamProfile.hpp"

#include <cerrno>
#include <csignal>
#include <fcntl.h>
#include <iostream>
#include <poll.h>
#include <set>
#include <unistd.h>

namespace ob = libobsensor;
namespace live = femto_bolt_live;
using femto_bolt::json_string;
static_assert(OB_FORMAT_Y16 == 8, "Update live transport profile matching for this SDK");

namespace {
volatile std::sig_atomic_t interrupted = 0;
void stop_signal(int) { interrupted = 1; }

struct Options {
    std::string serial;
    unsigned fps = 15, duration = 0;
    live::DepthMode depth_mode = live::DepthMode::nfov;
    bool profiles = false, check = false;
};
Options parse(int argc, char **argv) {
    Options result;
    std::set<std::string> seen;
    for (int i = 1; i < argc; ++i) {
        const std::string name(argv[i]);
        if (!seen.insert(name).second) throw std::invalid_argument("Repeated option");
        if (name == "--profiles") result.profiles = true;
        else if (name == "--check-build") result.check = true;
        else if (name == "--serial" || name == "--fps" || name == "--duration" || name == "--depth-mode") {
            if (++i >= argc) throw std::invalid_argument("Missing option value");
            std::string value(argv[i]);
            if (name == "--serial") result.serial = value;
            else if (name == "--depth-mode") result.depth_mode = live::parse_depth_mode(value);
            else if (name == "--fps") result.fps = femto_bolt::parse_unsigned(value, 30, name);
            else result.duration = femto_bolt::parse_unsigned(value, 86400, name);
        } else throw std::invalid_argument("Unknown option: " + name);
    }
    if (result.check) {
        if (seen.size() != 1) throw std::invalid_argument("--check-build takes no other options");
        return result;
    }
    if (result.serial.empty() || result.serial.size() > 128 || result.serial.rfind("--", 0) == 0)
        throw std::invalid_argument("An explicit nonempty --serial is required");
    if (result.fps != 5 && result.fps != 15 && result.fps != 30)
        throw std::invalid_argument("--fps must be 5, 15 or 30");
    return result;
}

const char *encoding(OBFormat format) {
    switch (format) {
        case OB_FORMAT_MJPG: return "MJPG";
        case OB_FORMAT_YUYV: return "YUYV";
        case OB_FORMAT_RGB: return "RGB";
        case OB_FORMAT_BGR: return "BGR";
        default: return "RAW";
    }
}
std::string profile_json(const ob::VideoStreamProfile &profile) {
    std::ostringstream out;
    out << "{\"width\":" << profile.getWidth() << ",\"height\":" << profile.getHeight()
        << ",\"fps\":" << profile.getFps() << ",\"sdk_format\":" << int(profile.getFormat())
        << ",\"encoding\":" << json_string(encoding(profile.getFormat())) << '}';
    return out.str();
}

void write_bytes(const std::uint8_t *data, std::size_t count) {
    while (count) {
        if (interrupted) throw std::runtime_error("Stream interrupted");
        const auto written = ::write(STDOUT_FILENO, data, count);
        if (written > 0) { data += written; count -= static_cast<std::size_t>(written); continue; }
        if (written < 0 && errno == EINTR) continue;
        if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            struct pollfd fd { STDOUT_FILENO, POLLOUT, 0 };
            const int status = poll(&fd, 1, 100);
            if (status < 0 && errno == EINTR) continue;
            if (status >= 0 && !(fd.revents & (POLLHUP | POLLERR | POLLNVAL))) continue;
        }
        throw std::runtime_error("Live output pipe closed or write failed");
    }
}
void send(const live::Packet &packet) {
    const auto header = live::wire_header(packet);
    write_bytes(header.data(), header.size());
    write_bytes(packet.payload.data(), packet.payload.size());
}

struct Endpoint {
    std::shared_ptr<const ob::USBSourcePortInfo> info;
    std::shared_ptr<ob::IVideoStreamPort> stream;
    ob::StreamProfileList profiles;
    std::shared_ptr<const ob::VideoStreamProfile> selected;
};

std::shared_ptr<const ob::VideoStreamProfile> choose_profile(const Endpoint &endpoint, unsigned fps, bool depth,
                                                          live::DepthMode depth_mode) {
    std::shared_ptr<const ob::VideoStreamProfile> selected;
    long long best = std::numeric_limits<long long>::max();
    for (const auto &entry : endpoint.profiles) {
        auto profile = std::dynamic_pointer_cast<const ob::VideoStreamProfile>(entry);
        if (!profile) continue;
        if (depth) {
            if (live::matches_depth_profile(depth_mode, profile->getWidth(), profile->getHeight(),
                                            profile->getFps(), fps, static_cast<std::uint32_t>(profile->getFormat()))) {
                if (selected) throw std::runtime_error("Ambiguous raw depth profile");
                selected = profile;
            }
            continue;
        }
        if (profile->getWidth() > 1920 || profile->getHeight() > 1080 || profile->getWidth() < 640
            || profile->getHeight() < 480 || profile->getFps() > 30 || profile->getFps() < fps) continue;
        const auto format = profile->getFormat();
        if (format != OB_FORMAT_MJPG && format != OB_FORMAT_YUYV && format != OB_FORMAT_RGB && format != OB_FORMAT_BGR) continue;
        const long long penalty = (format == OB_FORMAT_MJPG ? 0 : 100000000)
            + std::abs(static_cast<int>(profile->getFps()) - static_cast<int>(fps)) * 1000000LL
            + std::abs(static_cast<long long>(profile->getWidth()) * profile->getHeight() - 1280LL * 720);
        if (penalty < best) { best = penalty; selected = profile; }
    }
    if (!selected) {
        if (depth) throw std::runtime_error(std::string("Requested raw Y16 profile unavailable for ")
                                           + live::depth_transport_profile(depth_mode).name);
        throw std::runtime_error("No supported RGB profile at requested rate");
    }
    return selected;
}
} // namespace

int main(int argc, char **argv) {
    // The platform owns the libusb context and event thread; ports hold raw
    // pointers into it. Keep it alive until every stream and port is destroyed.
    std::shared_ptr<ob::Platform> platform;
    std::array<Endpoint, 2> endpoints;
    std::array<bool, 2> attempted{};
    auto frames = std::make_shared<live::LatestFrames>();
    int result = 0;
    try {
        // SDK configuration discovery falls back to the current directory. This
        // capture helper must never load caller-owned XML when run through sudo.
        // Use the same trusted directory for non-root runs so behavior is identical.
        if (::chdir("/") != 0) throw std::runtime_error("Cannot select trusted working directory");
        const auto options = parse(argc, argv);
        std::signal(SIGINT, stop_signal);
        std::signal(SIGTERM, stop_signal);
        std::signal(SIGPIPE, SIG_IGN);
        ob::Logger::setLogSeverity(OB_LOG_SEVERITY_OFF);
        ob::Logger::setLogCallback(OB_LOG_SEVERITY_WARN, [](OBLogSeverity severity, const std::string &message) {
            static std::mutex mutex;
            std::lock_guard<std::mutex> lock(mutex);
            std::cerr << "{\"sdk_severity\":" << int(severity) << ",\"message\":" << json_string(message) << "}\n";
        });
        const auto logger = ob::Logger::getInstance();
        const auto manager = ob::DeviceSeriesInfoManager::getInstance();
        if (!ob::isSupportedDevice(0x2bc5, 0x066b)) throw std::runtime_error("Bolt transport not initialized");
        if (options.check) {
            std::cout << "{\"status\":\"build_checked\",\"usb_access_attempted\":false,\"wire_protocol\":\"FBLIVE01\"}\n";
            return 0;
        }
        platform = ob::Platform::getInstance();
        for (const auto &port : platform->queryUsbSourcePortInfos(false)) {
            auto info = std::dynamic_pointer_cast<const ob::USBSourcePortInfo>(port);
            if (!info || info->portType != ob::SOURCE_PORT_USB_UVC || info->vid != 0x2bc5 || info->pid != 0x066b
                || info->serial != options.serial || (info->infIndex != 0 && info->infIndex != 2)) continue;
            auto &endpoint = endpoints[info->infIndex == 2 ? 0 : 1];
            if (endpoint.info) throw std::runtime_error("Multiple matching interfaces for requested serial");
            endpoint.info = info;
        }
        if (!endpoints[0].info || !endpoints[1].info) throw std::runtime_error("Both RGB and raw depth interfaces are required for the selected Bolt");
        if (endpoints[0].info->url != endpoints[1].info->url) throw std::runtime_error("RGB/depth interfaces are on different USB devices");
        for (auto &endpoint : endpoints) {
            endpoint.stream = std::dynamic_pointer_cast<ob::IVideoStreamPort>(platform->getSourcePort(endpoint.info));
            if (!endpoint.stream) throw std::runtime_error("Interface is not a video stream");
            endpoint.profiles = endpoint.stream->getStreamProfileList();
        }
        if (options.profiles) {
            std::cout << "{\"serial\":" << json_string(options.serial) << ",\"interfaces\":[";
            for (std::size_t i = 0; i < endpoints.size(); ++i) {
                if (i) std::cout << ',';
                std::cout << "{\"interface\":" << endpoints[i].info->infIndex << ",\"profiles\":[";
                bool comma = false;
                for (const auto &entry : endpoints[i].profiles) {
                    auto profile = std::dynamic_pointer_cast<const ob::VideoStreamProfile>(entry);
                    if (!profile) continue;
                    if (comma) std::cout << ',';
                    std::cout << profile_json(*profile);
                    comma = true;
                }
                std::cout << "]}";
            }
            std::cout << "]}\n";
            return 0;
        }
        for (std::size_t i = 0; i < endpoints.size(); ++i)
            endpoints[i].selected = choose_profile(endpoints[i], options.fps, i == 0, options.depth_mode);
        const int flags = fcntl(STDOUT_FILENO, F_GETFL);
        if (flags < 0 || fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK) < 0) throw std::runtime_error("Cannot configure bounded output writes");
        live::Packet hello;
        const auto text = "{\"kind\":\"live_rgb_raw_depth\",\"serial\":" + json_string(options.serial)
            + ",\"usb_connection\":" + json_string(endpoints[0].info->connSpec)
            + ",\"depth_mode\":" + json_string(live::depth_transport_profile(options.depth_mode).name)
            + ",\"depth_profile\":" + profile_json(*endpoints[0].selected)
            + ",\"color_profile\":" + profile_json(*endpoints[1].selected)
            + ",\"timestamp_kind\":\"host callback arrival; device clocks not synchronized\",\"depth_aligned_to_color\":false}";
        hello.payload.assign(text.begin(), text.end());
        send(hello);
        const auto started = std::chrono::steady_clock::now();
        for (std::size_t i = 0; i < endpoints.size(); ++i) {
            const auto profile = endpoints[i].selected;
            attempted[i] = true;
            endpoints[i].stream->startStream(profile, [frames, profile, i](std::shared_ptr<ob::Frame> frame) {
                try {
                    if (!frame || !frame->getData() || !frame->getDataSize() || frame->getDataSize() > live::max_payload_bytes)
                        throw std::runtime_error("Invalid camera payload size");
                    live::Packet packet;
                    packet.kind = static_cast<std::uint32_t>(i + 1);
                    packet.width = profile->getWidth(); packet.height = profile->getHeight();
                    packet.format = static_cast<std::uint32_t>(profile->getFormat());
                    packet.steady_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
                    packet.wall_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
                    packet.sdk_timestamp_raw = frame->getTimeStampUsec();
                    packet.payload.assign(frame->getData(), frame->getData() + frame->getDataSize());
                    frames->publish(std::move(packet));
                } catch (const std::exception &exc) { frames->fail(exc.what()); }
                catch (...) { frames->fail("Unknown camera callback failure"); }
            });
        }
        while (!interrupted) {
            if (options.duration && std::chrono::steady_clock::now() - started >= std::chrono::seconds(options.duration)) break;
            if (frames->stale(started, std::chrono::seconds(10))) throw std::runtime_error("Camera stopped delivering one or both streams");
            auto packet = frames->take(std::chrono::milliseconds(100));
            if (packet) send(*packet);
        }
    } catch (const std::exception &exc) {
        if (!interrupted) {
            std::cerr << "{\"status\":\"error\",\"message\":" << json_string(exc.what()) << "}\n";
            result = 1;
        }
    }
    frames->stop();
    for (std::size_t i = 0; i < endpoints.size(); ++i) if (attempted[i]) {
        try { endpoints[i].stream->stopAllStream(); }
        catch (const std::exception &exc) { std::cerr << "{\"cleanup_error\":" << json_string(exc.what()) << "}\n"; result = 1; }
        catch (...) { std::cerr << "{\"cleanup_error\":\"unknown\"}\n"; result = 1; }
    }
    const auto counts = frames->counts();
    std::cerr << "{\"status\":\"stopped\",\"depth_received\":" << counts[0] << ",\"rgb_received\":" << counts[1]
              << ",\"depth_replaced\":" << counts[2] << ",\"rgb_replaced\":" << counts[3] << "}\n";
    return result;
}

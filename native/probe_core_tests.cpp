// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (c) 2026 pyBravo contributors.
// Extracted and adapted for OSX-Femto-Bolt; see ../NOTICE.
// Hardware-free tests: compile this file alone with a C++17 compiler.
// Checks deliberately remain enabled with -DNDEBUG in a Release build.
#include "probe_core.hpp"

#include <array>
#include <atomic>
#include <filesystem>
#include <fstream>
#include <functional>
#include <future>
#include <iostream>
#include <iterator>
#include <limits>
#include <stdexcept>
#include <thread>
#include <utility>

using namespace femto_bolt;

namespace {

class TemporaryDirectory {
public:
    TemporaryDirectory() {
        static std::atomic<unsigned> sequence{0};
        const auto tick = std::chrono::steady_clock::now().time_since_epoch().count();
        path = std::filesystem::temp_directory_path()
             / ("femto-bolt-core-" + std::to_string(tick) + "-" + std::to_string(sequence++));
        if(!std::filesystem::create_directory(path)) throw std::runtime_error("Cannot create isolated test directory");
    }

    ~TemporaryDirectory() {
        std::error_code ignored;
        std::filesystem::remove_all(path, ignored);
    }

    std::filesystem::path path;
};

void save_fixture(const std::filesystem::path &path, const std::string &text) {
    std::ofstream output(path, std::ios::binary);
    output << text;
    output.close();
    if(!output) throw std::runtime_error("Cannot write test fixture");
}

std::string read_fixture(const std::filesystem::path &path) {
    std::ifstream input(path, std::ios::binary);
    if(!input) throw std::runtime_error("Cannot read test fixture");
    return std::string(std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>());
}

void require(bool condition, const char *description) {
    if(!condition) throw std::runtime_error(description);
}

template <typename Exception = std::invalid_argument, typename Function>
void require_throws(Function action, const char *description) {
    try {
        action();
    }
    catch(const Exception &) {
        return;
    }
    throw std::runtime_error(description);
}

void default_action_only_lists_devices() {
    const auto options = parse_options({});
    require(options.mode == Mode::List, "default action must not start a stream");
    require(options.directory.empty(), "listing must not choose an output directory");
    require(options.serial.empty(), "default must not invent a camera identity");
}

void build_check_is_a_distinct_non_capture_action() {
    const auto options = parse_options({"--check-build"});
    require(options.mode == Mode::CheckBuild, "build check must be distinguishable from USB enumeration");
    require(options.directory.empty() && options.serial.empty(), "build check must not choose capture output or a camera");
}

void build_check_rejects_hardware_and_capture_arguments() {
    require_throws([] { parse_options({"--check-build", "--list"}); }, "build check must not also enumerate USB");
    require_throws([] { parse_options({"--check-build", "--serial", "camera"}); }, "build check must not select a camera");
    require_throws([] { parse_options({"--check-build", "--frames", "1"}); }, "build check must not accept a capture length");
    require_throws([] { parse_options({"--check-build", "--timeout", "1"}); }, "build check must not accept capture controls");
    require_throws([] { parse_options({"--check-build", "--profile-index", "0"}); }, "build check must not choose a stream profile");
}

void explicit_capture_preserves_profile_and_identity() {
    const auto options = parse_options({"--serial", "bolt serial \"A\"", "--capture", "capture with spaces",
                                       "--profile-index", "0", "--frames", "120", "--timeout", "30"});
    require(options.mode == Mode::Capture, "capture mode was not selected");
    require(options.serial == "bolt serial \"A\"", "serial must be preserved exactly");
    require(options.directory == "capture with spaces", "output directory must be preserved exactly");
    require(options.profile_index == 0 && options.frames == 120 && options.timeout_seconds == 30,
            "valid inclusive boundaries must be accepted");
}

void nvram_capture_does_not_accept_an_arbitrary_profile() {
    const auto options = parse_options({"--nvram", "private-capture", "--frames", "1", "--timeout", "1"});
    require(options.mode == Mode::Nvram && options.frames == 1 && options.timeout_seconds == 1,
            "minimum bounded NVRAM capture must be accepted");
    require_throws([] { parse_options({"--nvram", "private-capture", "--profile-index", "2"}); },
                   "NVRAM mode must not accept an arbitrary profile index");
}

void capture_requires_an_explicit_enumerated_profile_index() {
    require_throws([] { parse_options({"--capture", "destination"}); },
                   "capture must not choose a profile implicitly");
    require_throws([] { parse_options({"--profiles", "--profile-index", "0"}); },
                   "profile selection must not be silently ignored during listing");
}

void contradictory_actions_and_repeated_options_are_rejected() {
    require_throws([] { parse_options({"--list", "--profiles"}); }, "two actions must not select a winner");
    require_throws([] { parse_options({"--profiles", "--serial", "A", "--serial", "B"}); },
                   "duplicate camera identity must not select a winner");
    require_throws([] { parse_options({"--capture", "one", "--capture", "two", "--profile-index", "0"}); },
                   "duplicate capture destinations must be refused");
}

void missing_empty_and_unknown_arguments_are_rejected() {
    require_throws([] { parse_options({"--serial"}); }, "missing serial value must be refused");
    require_throws([] { parse_options({"--serial", ""}); }, "empty serial must be refused");
    require_throws([] { parse_options({"--capture", "--profile-index", "0"}); },
                   "another option must not become the capture path");
    require_throws([] { parse_options({"--firmware-update"}); }, "unknown actions must not be accepted");
    require_throws([] { parse_options({"unexpected-positional-argument"}); }, "unknown positional argument must fail");
}

void numeric_limits_reject_signs_fractional_values_and_overflow() {
    require_throws([] { parse_unsigned("-1", 120, "frames"); }, "negative frame count must fail");
    require_throws([] { parse_unsigned("+1", 120, "frames"); }, "signed frame count must fail");
    require_throws([] { parse_unsigned("1.5", 120, "frames"); }, "fractional frame count must fail");
    require_throws([] { parse_unsigned(" 1", 120, "frames"); }, "whitespace must not hide malformed numbers");
    require_throws([] { parse_unsigned("1x", 120, "frames"); }, "trailing junk must fail");
    require_throws([] { parse_unsigned("42949672960000000000", 10000, "profile"); },
                   "integer overflow must be refused before multiplication");
    require_throws([] { parse_unsigned("", 120, "frames"); }, "empty numbers must fail");
}

void capture_limits_are_positive_bounded_and_not_ignored() {
    require_throws([] { parse_options({"--nvram", "output", "--frames", "0"}); }, "zero frames must fail");
    require_throws([] { parse_options({"--nvram", "output", "--frames", "121"}); }, "overlong capture must fail");
    require_throws([] { parse_options({"--nvram", "output", "--timeout", "0"}); }, "zero timeout must fail");
    require_throws([] { parse_options({"--nvram", "output", "--timeout", "31"}); }, "overlong timeout must fail");
    require_throws([] { parse_options({"--capture", "output", "--profile-index", "10001"}); },
                   "unbounded profile index must fail");
    require_throws([] { parse_options({"--list", "--frames", "1"}); }, "listing must not ignore capture controls");
    require_throws([] { parse_options({"--profiles", "--timeout", "1"}); }, "profile listing must not ignore timeout");
}

void usb_filter_only_accepts_the_bolt_depth_video_interface() {
    require(is_bolt_depth_port(0x2bc5, 0x066b, 2, true), "the published Bolt depth interface must match");
    require(!is_bolt_depth_port(0x8086, 0x066b, 2, true), "another vendor must never match");
    require(!is_bolt_depth_port(0x2bc5, 0x0660, 2, true), "another Orbbec model must never match");
    require(!is_bolt_depth_port(0x2bc5, 0x066b, 0, true), "the color interface must never match");
    require(!is_bolt_depth_port(0x2bc5, 0x066b, 2, false), "a non-video interface must never match");
}

void device_selection_preserves_index_after_filtering_other_cameras() {
    const std::vector<DeviceDescriptor> devices{
        {0x8086, 0x0b07, 2, true, "A"},
        {0x2bc5, 0x066b, 0, true, "A"},
        {0x2bc5, 0x066b, 2, true, "A"},
        {0x2bc5, 0x066b, 2, false, "A"},
        {0x2bc5, 0x066b, 2, true, "B"},
    };
    require(matching_device_indices(devices, "A") == std::vector<std::size_t>({2}),
            "serial matching must still reject the wrong camera and interfaces");
    require(select_unique_device(devices, "A") == 2, "selected index must refer to the original device list");
    require(select_unique_device(devices, "B") == 4, "explicit serial must select its own physical camera");
    require_throws<std::runtime_error>([&] { select_unique_device(devices, ""); },
                                      "two Bolts must not silently choose the first");
    require_throws<std::runtime_error>([&] { select_unique_device(devices, "missing"); },
                                      "unknown serial must not fall back to another camera");
}

void duplicate_or_missing_serials_cannot_resolve_device_ambiguity() {
    const std::vector<DeviceDescriptor> duplicates{
        {0x2bc5, 0x066b, 2, true, "same"}, {0x2bc5, 0x066b, 2, true, "same"},
    };
    const std::vector<DeviceDescriptor> unidentified{
        {0x2bc5, 0x066b, 2, true, ""}, {0x2bc5, 0x066b, 2, true, ""},
    };
    require_throws<std::runtime_error>([&] { select_unique_device(duplicates, "same"); },
                                      "a duplicate serial must not bypass ambiguity protection");
    require_throws<std::runtime_error>([&] { select_unique_device(unidentified, ""); },
                                      "two unidentified cameras must not be interchangeable");
    require_throws<std::runtime_error>([] { select_unique_device({}, ""); },
                                      "no camera must remain an explicit error");
}

void profile_selection_requires_an_existing_video_profile() {
    const std::vector<ProfileDescriptor> profiles{{true, 7680, 434, 30}, {false, 0, 0, 0}, {true, 8192, 130, 15}};
    require(select_profile_index(profiles, 2, false) == 2, "an explicit raw profile must not be replaced with a default");
    require_throws([&] { select_profile_index(profiles, 1, false); }, "a non-video transport profile must be refused");
    require_throws([&] { select_profile_index(profiles, profiles.size(), false); }, "one-past-end index must fail");
    require_throws([&] { select_profile_index(profiles, std::numeric_limits<std::size_t>::max(), false); },
                   "large profile index must fail without arithmetic overflow");
    require_throws([] { select_profile_index({}, 0, false); }, "empty profile list must not invent a mode");
}

void nvram_profile_selection_requires_the_exact_unique_firmware_mode() {
    const std::vector<ProfileDescriptor> profiles{
        {true, 1024, 512, 15}, {true, 512, 1024, 5}, {true, 1024, 512, 5}, {false, 1024, 512, 5},
    };
    require(select_profile_index(profiles, 0, true) == 2, "only the documented firmware mode may be selected automatically");
    require_throws<std::runtime_error>([] { select_profile_index({{true, 1024, 512, 15}}, 0, true); },
                                      "nearby frame rate is not a firmware-data substitute");
    require_throws<std::runtime_error>([] {
        select_profile_index({{true, 1024, 512, 5}, {true, 1024, 512, 5}}, 0, true);
    }, "duplicate firmware profiles must not silently select one");
    require_throws<std::runtime_error>([] { select_profile_index({}, 0, true); },
                                      "no firmware profile must fail explicitly");
}

void descriptor_strings_cannot_inject_json_fields() {
    const std::string hostile = "A\",\"complete\":true,\"x\":\"\\\n\r\t";
    require(json_string(hostile) == "\"A\\\",\\\"complete\\\":true,\\\"x\\\":\\\"\\\\\\u000a\\u000d\\u0009\"",
            "quotes, slashes and control bytes must be escaped");
    require(json_string(std::string("\0\x7f\x80\xff", 4)) == "\"\\u0000\\u007f\\u0080\\u00ff\"",
            "arbitrary descriptor bytes must produce valid JSON");
}

void valid_utf8_paths_and_descriptors_keep_their_unicode_characters() {
    const std::string text = u8"/captures/café/液体/🧪/e\u0301";
    require(json_string(text) == '"' + text + '"', "valid UTF-8 must not be reinterpreted as Latin-1 bytes");
    const std::string boundaries = "\xc2\x80\xdf\xbf\xe0\xa0\x80\xed\x9f\xbf\xee\x80\x80\xef\xbf\xbf"
                                   "\xf0\x90\x80\x80\xf4\x8f\xbf\xbf";
    require(json_string(boundaries) == '"' + boundaries + '"', "valid UTF-8 boundary code points must remain unchanged");
}

void malformed_utf8_is_escaped_instead_of_becoming_invalid_json() {
    require(json_string("\xc0\x80") == "\"\\u00c0\\u0080\"", "overlong two-byte NUL must be refused");
    require(json_string("\xe0\x80\x80") == "\"\\u00e0\\u0080\\u0080\"", "overlong three-byte form must be refused");
    require(json_string("\xf0\x80\x80\x80") == "\"\\u00f0\\u0080\\u0080\\u0080\"", "overlong four-byte form must be refused");
    require(json_string("\xed\xa0\x80") == "\"\\u00ed\\u00a0\\u0080\"", "UTF-8 encoded high surrogate must be refused");
    require(json_string("\xed\xbf\xbf") == "\"\\u00ed\\u00bf\\u00bf\"", "UTF-8 encoded low surrogate must be refused");
    require(json_string("\xf4\x90\x80\x80") == "\"\\u00f4\\u0090\\u0080\\u0080\"", "values above U+10FFFF must be refused");
    require(json_string("\xf5\x80\x80\x80") == "\"\\u00f5\\u0080\\u0080\\u0080\"", "invalid four-byte lead must be refused");
    require(json_string("\xc3") == "\"\\u00c3\"", "truncated two-byte sequence must be escaped");
    require(json_string("\xe2\x82") == "\"\\u00e2\\u0082\"", "truncated three-byte sequence must be escaped");
    require(json_string("\xf0\x9f\xa7") == "\"\\u00f0\\u009f\\u00a7\"", "truncated four-byte sequence must be escaped");
}

void invalid_descriptor_bytes_do_not_corrupt_adjacent_valid_utf8() {
    const std::string text = std::string(u8"café") + "\x80" + u8"/液体" + "\xed\xa0\x80" + u8"/🔬";
    const std::string expected = std::string(u8"\"café") + "\\u0080" + u8"/液体" + "\\u00ed\\u00a0\\u0080" + u8"/🔬\"";
    require(json_string(text) == expected, "recovery after invalid bytes must preserve neighboring UTF-8 and separators");
}

void atomic_manifest_replacement_preserves_complete_json_and_cleans_staging() {
    TemporaryDirectory directory;
    const auto target = directory.path / "manifest.json";
    const std::string initial = "{\"data_kind\":\"RAW_NOT_DEPTH\",\"complete\":false,\"frames\":[]}\n";
    const std::string final = "{\"data_kind\":\"RAW_NOT_DEPTH\",\"complete\":true,\"frames\":[{\"bytes\":3}]}\n";
    save_fixture(target, initial);

    write_atomic_text(target, final);

    require(read_fixture(target) == final, "atomic replacement must preserve the full final JSON text exactly");
    require(!std::filesystem::exists(target.string() + ".tmp"), "successful replacement must consume its staging file");
}

void a_staging_directory_cannot_destroy_the_existing_incomplete_manifest() {
    TemporaryDirectory directory;
    const auto target = directory.path / "manifest.json";
    const auto staging = std::filesystem::path(target.string() + ".tmp");
    const std::string initial = "{\"data_kind\":\"RAW_NOT_DEPTH\",\"complete\":false,\"reason\":\"capture in progress\"}\n";
    save_fixture(target, initial);
    require(std::filesystem::create_directory(staging), "staging blocker must be created");
    save_fixture(staging / "owned-by-another-writer", "preserve me");

    require_throws<std::runtime_error>([&] { write_atomic_text(target, "{\"complete\":true}\n"); },
                                      "an unwritable staging destination must refuse replacement");

    require(read_fixture(target) == initial, "failed staging write must retain the original incomplete manifest");
    require(read_fixture(staging / "owned-by-another-writer") == "preserve me",
            "failure cleanup must not remove a preexisting staging directory");
}

void an_existing_staging_file_is_not_overwritten_or_deleted() {
    TemporaryDirectory directory;
    const auto target = directory.path / "manifest.json";
    const auto staging = std::filesystem::path(target.string() + ".tmp");
    save_fixture(target, "{\"complete\":false}\n");
    save_fixture(staging, "another writer's unfinished content");

    require_throws<std::runtime_error>([&] { write_atomic_text(target, "{\"complete\":true}\n"); },
                                      "preexisting staging file must not be claimed by a new writer");

    require(read_fixture(target) == "{\"complete\":false}\n", "staging collision must not alter the saved manifest");
    require(read_fixture(staging) == "another writer's unfinished content", "staging collision must not erase another writer's data");
}

void rename_failure_preserves_destination_and_removes_only_owned_staging() {
    TemporaryDirectory directory;
    const auto target = directory.path / "manifest.json";
    require(std::filesystem::create_directory(target), "rename blocker must be created");
    save_fixture(target / "previous-data", "preserve existing destination");

    require_throws<std::runtime_error>([&] { write_atomic_text(target, "{\"complete\":true}\n"); },
                                      "rename onto an existing directory must fail");

    require(std::filesystem::is_directory(target), "failed rename must not remove an existing destination");
    require(read_fixture(target / "previous-data") == "preserve existing destination", "failed rename must preserve prior data");
    require(!std::filesystem::exists(target.string() + ".tmp"), "failed rename must clean up only the writer's own staging file");
}

void invalid_buffer_configuration_is_rejected_before_allocation() {
    require_throws([] { CaptureBuffer buffer(0); }, "zero target must fail");
    require_throws([] { CaptureBuffer buffer(121); }, "excessive target must fail");
    require_throws([] { CaptureBuffer buffer(1, 0); }, "zero byte budget must fail");
    require_throws([] { CaptureBuffer buffer(1, kMaxCaptureBytes + 1); }, "excessive budget must fail");
}

void capture_copies_original_bytes_metadata_and_timestamps() {
    std::array<std::uint8_t, 4> payload{{0, 255, 17, 0}};
    std::array<std::uint8_t, 2> metadata{{128, 1}};
    FrameRecord record;
    record.sdk_sequence = 42;
    record.sdk_timestamp_raw = 987654321;
    record.sdk_system_timestamp_us = 123456789;
    record.host_wall_ns = 9123456789;
    record.host_steady_ns = 456789;
    CaptureBuffer buffer(1);

    require(buffer.add(payload.data(), payload.size(), metadata.data(), metadata.size(), record), "valid frame was rejected");
    payload.fill(0);
    metadata.fill(0);
    const auto frames = buffer.take_frames();

    require(frames.size() == 1, "exactly the accepted frame must be retained");
    require(frames[0].bytes == std::vector<std::uint8_t>({0, 255, 17, 0}), "raw payload must be copied without reinterpretation");
    require(frames[0].metadata == std::vector<std::uint8_t>({128, 1}), "metadata must be copied separately");
    require(frames[0].sdk_sequence == 42 && frames[0].sdk_timestamp_raw == 987654321,
            "source counter and raw timestamp must be preserved");
    require(frames[0].sdk_system_timestamp_us == 123456789 && frames[0].host_wall_ns == 9123456789
                && frames[0].host_steady_ns == 456789,
            "host and source clocks must not be substituted for one another");
    require(buffer.error().empty(), "successful capture must not acquire an error");
}

void absent_metadata_does_not_reuse_a_previous_frames_metadata() {
    const std::uint8_t payload = 12;
    FrameRecord record;
    record.metadata = {23, 34, 45};
    CaptureBuffer buffer(1, 1);
    require(buffer.add(&payload, 1, nullptr, 0, record), "single-byte capture must fit its exact budget");
    const auto frames = buffer.take_frames();
    require(frames[0].metadata.empty(), "absent metadata must not retain stale bytes outside the capture budget");
}

void reaching_target_stops_acceptance_without_overwriting_saved_frames() {
    const std::uint8_t first = 1;
    const std::uint8_t late = 2;
    CaptureBuffer buffer(1);
    require(buffer.add(&first, 1, nullptr, 0, {}), "first valid frame must be accepted");
    require(!buffer.add(&late, 1, nullptr, 0, {}), "late callbacks must not exceed the target");
    buffer.finish("deadline expired");
    const auto frames = buffer.take_frames();
    require(frames.size() == 1 && frames[0].bytes == std::vector<std::uint8_t>({1}), "late callback changed the capture");
    require(buffer.error().empty(), "finishing a complete capture must not invent a timeout");
}

void byte_budget_counts_metadata_and_retains_only_complete_frames() {
    const std::array<std::uint8_t, 3> payload{{1, 2, 3}};
    const std::array<std::uint8_t, 2> metadata{{4, 5}};
    CaptureBuffer buffer(2, 5);
    require(buffer.add(payload.data(), 3, metadata.data(), 2, {}), "a frame exactly fitting the budget must be accepted");
    require(!buffer.add(payload.data(), 1, nullptr, 0, {}), "next frame must not exceed the total byte budget");
    const auto frames = buffer.take_frames();
    require(frames.size() == 1, "budget exhaustion must not retain a partial or padded frame");
    require(buffer.error() == "Capture byte budget exhausted", "budget failure must remain distinguishable");
}

void metadata_cannot_cross_the_remaining_capture_budget() {
    const std::array<std::uint8_t, 3> payload{{1, 2, 3}};
    const std::array<std::uint8_t, 2> metadata{{4, 5}};
    CaptureBuffer buffer(1, 4);
    require(!buffer.add(payload.data(), 3, metadata.data(), 2, {}), "metadata must count against the budget");
    require(buffer.take_frames().empty(), "an oversized combined frame must not be partially copied");
    require(!buffer.error().empty(), "budget refusal must report an error");
}

void invalid_payload_pointers_and_lengths_fail_before_copying() {
    const std::uint8_t byte = 7;
    CaptureBuffer null_payload(1);
    require(!null_payload.add(nullptr, 1, nullptr, 0, {}), "null payload must fail");
    require(null_payload.take_frames().empty(), "null payload must not create a frame");
    CaptureBuffer empty_payload(1);
    require(!empty_payload.add(&byte, 0, nullptr, 0, {}), "empty payload must fail");
    CaptureBuffer oversized_payload(1);
    require(!oversized_payload.add(&byte, kMaxFrameBytes + 1, nullptr, 0, {}), "oversized length must fail before reading data");
    CaptureBuffer overflow_payload(1);
    require(!overflow_payload.add(&byte, std::numeric_limits<std::size_t>::max(), nullptr, 0, {}),
            "overflow-sized payload must fail before reading data");
    CaptureBuffer missing_metadata(1);
    require(!missing_metadata.add(&byte, 1, nullptr, 1, {}), "missing metadata pointer must fail");
    CaptureBuffer oversized_metadata(1);
    require(!oversized_metadata.add(&byte, 1, &byte, kMaxMetadataBytes + 1, {}), "oversized metadata must fail before reading data");
}

void failure_stops_capture_and_preserves_the_first_reason() {
    const std::uint8_t byte = 7;
    CaptureBuffer buffer(2);
    buffer.fail("USB disconnected");
    buffer.fail("secondary shutdown error");
    require(!buffer.add(&byte, 1, nullptr, 0, {}), "failure must stop late callbacks");
    require(buffer.error() == "USB disconnected", "shutdown must not obscure the original failure");
    require(buffer.take_frames().empty(), "failed empty capture must remain empty");
}

void timeout_preserves_partial_capture_without_claiming_completion() {
    const std::uint8_t byte = 7;
    CaptureBuffer buffer(2);
    require(buffer.add(&byte, 1, nullptr, 0, {}), "first frame must be accepted");
    require(!buffer.wait_until(std::chrono::steady_clock::time_point::min()), "an incomplete capture must not report completion");
    buffer.finish("Capture timed out before requested frame count");
    const auto frames = buffer.take_frames();
    require(frames.size() == 1 && frames[0].bytes == std::vector<std::uint8_t>({7}), "timeout must retain original partial evidence");
    require(buffer.error() == "Capture timed out before requested frame count", "partial capture must retain its reason");
}

void active_capture_cannot_be_drained_and_complete_capture_does_not_wait() {
    const std::uint8_t byte = 7;
    CaptureBuffer buffer(1);
    require_throws<std::logic_error>([&] { buffer.take_frames(); }, "active capture must not be drained concurrently");
    require(buffer.add(&byte, 1, nullptr, 0, {}), "frame must remain acceptable after a refused drain");
    require(buffer.wait_until(std::chrono::steady_clock::time_point::min()), "complete capture must be observable without waiting");
    require(buffer.take_frames().size() == 1, "completed capture must remain available");
}

void simultaneous_callbacks_cannot_overrun_the_frame_target() {
    CaptureBuffer buffer(1, 1);
    std::promise<void> start;
    auto ready = start.get_future().share();
    std::atomic<unsigned> accepted{0};
    auto callback = [&](std::uint8_t byte) {
        ready.wait();
        if(buffer.add(&byte, 1, nullptr, 0, {})) ++accepted;
    };
    std::thread first(callback, 1);
    std::thread second(callback, 2);
    std::thread third(callback, 3);
    std::thread fourth(callback, 4);
    start.set_value();
    first.join();
    second.join();
    third.join();
    fourth.join();

    const auto frames = buffer.take_frames();
    require(accepted == 1 && frames.size() == 1, "concurrent callbacks must accept exactly one complete frame");
    require(frames[0].bytes.size() == 1 && frames[0].bytes[0] >= 1 && frames[0].bytes[0] <= 4,
            "the winning callback must remain a complete original payload");
    require(buffer.error().empty(), "late concurrent callbacks must not turn completion into an error");
}

} // namespace

int main(int argc, char **argv) {
    if(argc == 2 && std::string(argv[1]) == "--json-fixtures") {
        std::cout << "{\"path\":" << json_string(u8"/captures/café/液体/🧪/e\u0301")
                  << ",\"descriptor\":" << json_string(u8"Bolt \"é\" — 温度 🔬")
                  << ",\"overlong\":" << json_string("\xc0\x80")
                  << ",\"surrogate\":" << json_string("\xed\xa0\x80")
                  << ",\"out_of_range\":" << json_string("\xf4\x90\x80\x80")
                  << ",\"truncated\":" << json_string("\xe2\x82") << "}\n";
        return 0;
    }
    const std::vector<std::pair<const char *, std::function<void()>>> tests{
        {"default_action_only_lists_devices", default_action_only_lists_devices},
        {"build_check_is_a_distinct_non_capture_action", build_check_is_a_distinct_non_capture_action},
        {"build_check_rejects_hardware_and_capture_arguments", build_check_rejects_hardware_and_capture_arguments},
        {"explicit_capture_preserves_profile_and_identity", explicit_capture_preserves_profile_and_identity},
        {"nvram_capture_does_not_accept_an_arbitrary_profile", nvram_capture_does_not_accept_an_arbitrary_profile},
        {"capture_requires_an_explicit_enumerated_profile_index", capture_requires_an_explicit_enumerated_profile_index},
        {"contradictory_actions_and_repeated_options_are_rejected", contradictory_actions_and_repeated_options_are_rejected},
        {"missing_empty_and_unknown_arguments_are_rejected", missing_empty_and_unknown_arguments_are_rejected},
        {"numeric_limits_reject_signs_fractional_values_and_overflow", numeric_limits_reject_signs_fractional_values_and_overflow},
        {"capture_limits_are_positive_bounded_and_not_ignored", capture_limits_are_positive_bounded_and_not_ignored},
        {"usb_filter_only_accepts_the_bolt_depth_video_interface", usb_filter_only_accepts_the_bolt_depth_video_interface},
        {"device_selection_preserves_index_after_filtering_other_cameras", device_selection_preserves_index_after_filtering_other_cameras},
        {"duplicate_or_missing_serials_cannot_resolve_device_ambiguity", duplicate_or_missing_serials_cannot_resolve_device_ambiguity},
        {"profile_selection_requires_an_existing_video_profile", profile_selection_requires_an_existing_video_profile},
        {"nvram_profile_selection_requires_the_exact_unique_firmware_mode", nvram_profile_selection_requires_the_exact_unique_firmware_mode},
        {"descriptor_strings_cannot_inject_json_fields", descriptor_strings_cannot_inject_json_fields},
        {"valid_utf8_paths_and_descriptors_keep_their_unicode_characters", valid_utf8_paths_and_descriptors_keep_their_unicode_characters},
        {"malformed_utf8_is_escaped_instead_of_becoming_invalid_json", malformed_utf8_is_escaped_instead_of_becoming_invalid_json},
        {"invalid_descriptor_bytes_do_not_corrupt_adjacent_valid_utf8", invalid_descriptor_bytes_do_not_corrupt_adjacent_valid_utf8},
        {"atomic_manifest_replacement_preserves_complete_json_and_cleans_staging", atomic_manifest_replacement_preserves_complete_json_and_cleans_staging},
        {"a_staging_directory_cannot_destroy_the_existing_incomplete_manifest", a_staging_directory_cannot_destroy_the_existing_incomplete_manifest},
        {"an_existing_staging_file_is_not_overwritten_or_deleted", an_existing_staging_file_is_not_overwritten_or_deleted},
        {"rename_failure_preserves_destination_and_removes_only_owned_staging", rename_failure_preserves_destination_and_removes_only_owned_staging},
        {"invalid_buffer_configuration_is_rejected_before_allocation", invalid_buffer_configuration_is_rejected_before_allocation},
        {"capture_copies_original_bytes_metadata_and_timestamps", capture_copies_original_bytes_metadata_and_timestamps},
        {"absent_metadata_does_not_reuse_a_previous_frames_metadata", absent_metadata_does_not_reuse_a_previous_frames_metadata},
        {"reaching_target_stops_acceptance_without_overwriting_saved_frames", reaching_target_stops_acceptance_without_overwriting_saved_frames},
        {"byte_budget_counts_metadata_and_retains_only_complete_frames", byte_budget_counts_metadata_and_retains_only_complete_frames},
        {"metadata_cannot_cross_the_remaining_capture_budget", metadata_cannot_cross_the_remaining_capture_budget},
        {"invalid_payload_pointers_and_lengths_fail_before_copying", invalid_payload_pointers_and_lengths_fail_before_copying},
        {"failure_stops_capture_and_preserves_the_first_reason", failure_stops_capture_and_preserves_the_first_reason},
        {"timeout_preserves_partial_capture_without_claiming_completion", timeout_preserves_partial_capture_without_claiming_completion},
        {"active_capture_cannot_be_drained_and_complete_capture_does_not_wait", active_capture_cannot_be_drained_and_complete_capture_does_not_wait},
        {"simultaneous_callbacks_cannot_overrun_the_frame_target", simultaneous_callbacks_cannot_overrun_the_frame_target},
    };
    for(const auto &test : tests) {
        try {
            test.second();
            std::cout << "PASS " << test.first << '\n';
        }
        catch(const std::exception &error) {
            std::cerr << "FAIL " << test.first << ": " << error.what() << '\n';
            return 1;
        }
    }
    std::cout << "All raw-recorder core tests passed (" << tests.size() << " cases)\n";
    return 0;
}

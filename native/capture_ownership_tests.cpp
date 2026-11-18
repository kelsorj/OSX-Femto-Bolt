// SPDX-License-Identifier: GPL-3.0-or-later
// Exercise privileged-operation decisions without root or changing ownership.
#include "probe_core.hpp"
#include <cstdlib>
#include <dirent.h>
#include <fstream>
#include <iostream>
#include <sys/stat.h>

namespace {
uid_t simulated_euid = 0;
bool reject_file_transfer = false;
std::vector<std::string> transfers;
ino_t transferred_directory = 0;
uid_t simulated_geteuid() { return simulated_euid; }
int simulated_fchownat(int, const char *name, uid_t uid, gid_t gid, int flags) {
    if(uid != 501 || gid != 20 || flags != AT_SYMLINK_NOFOLLOW)
        throw std::runtime_error("Incorrect owner or symlink policy");
    if(reject_file_transfer) { errno = EPERM; return -1; }
    transfers.emplace_back(name);
    return 0;
}
int simulated_fchown(int fd, uid_t uid, gid_t gid) {
    if(uid != 501 || gid != 20) throw std::runtime_error("Incorrect directory owner");
    struct stat status{};
    if(::fstat(fd, &status) != 0) throw std::runtime_error("Lost original directory descriptor");
    transferred_directory = status.st_ino;
    transfers.emplace_back("directory-last");
    return 0;
}
} // namespace

// Headers declaring these POSIX APIs were included above. Only the ownership
// class's calls are substituted; real filesystem behavior stays under test.
#define geteuid simulated_geteuid
#define fchownat simulated_fchownat
#define fchown simulated_fchown
#include "capture_ownership.hpp"
#undef geteuid
#undef fchownat
#undef fchown

namespace fs = std::filesystem;
using femto_bolt::CaptureOwnership;

namespace {
void require(bool condition, const char *message) {
    if(!condition) throw std::runtime_error(message);
}
template <typename Function> void must_reject(Function action) {
    bool rejected = false;
    try { action(); }
    catch(const std::exception &) { rejected = true; }
    require(rejected, "Unsafe ownership operation was accepted");
}
struct TemporaryDirectory {
    fs::path path;
    TemporaryDirectory() {
        static unsigned sequence = 0;
        path = fs::temp_directory_path() / ("femto-owner-" + std::to_string(::getpid()) + "-" + std::to_string(sequence++));
        require(fs::create_directory(path), "Cannot create test directory");
    }
    ~TemporaryDirectory() {
        std::error_code ignored;
        fs::remove_all(path, ignored);
    }
};
void reset() {
    simulated_euid = 0;
    reject_file_transfer = false;
    transfers.clear();
    transferred_directory = 0;
    ::setenv("SUDO_UID", "501", 1);
    ::setenv("SUDO_GID", "20", 1);
}
void save(const fs::path &path) {
    std::ofstream file(path);
    file << "private test data\n";
    require(bool(file), "Cannot save fixture");
}
void transfer_is_ordered_and_idempotent() {
    reset(); TemporaryDirectory directory;
    save(directory.path / "manifest.json");
    save(directory.path / "frame.bin");
    CaptureOwnership owner(directory.path);
    owner.finish();
    require(transfers.size() == 3 && transfers.back() == "directory-last", "Directory became accessible before files were transferred");
    owner.finish();
    require(transfers.size() == 3, "Repeated finish transferred ownership again");
}
void ordinary_user_and_direct_root_do_not_chown() {
    reset(); TemporaryDirectory directory;
    simulated_euid = 501;
    ::setenv("SUDO_UID", "invalid", 1);
    { CaptureOwnership owner(directory.path); owner.finish(); }
    simulated_euid = 0;
    ::unsetenv("SUDO_UID");
    { CaptureOwnership owner(directory.path); owner.finish(); }
    ::setenv("SUDO_UID", "0", 1);
    { CaptureOwnership owner(directory.path); owner.finish(); }
    require(transfers.empty(), "No sudo owner must mean no ownership changes");
}
void malformed_owner_is_rejected() {
    reset(); TemporaryDirectory directory;
    for(const char *value : {"-1", "1x", "999999999999999", ""}) {
        ::setenv("SUDO_UID", value, 1);
        must_reject([&] { CaptureOwnership owner(directory.path); });
    }
    require(transfers.empty(), "Malformed identity changed file ownership");
}
void ownership_uses_original_directory_after_rename() {
    reset(); TemporaryDirectory container;
    const auto original = container.path / "capture";
    const auto moved = container.path / "moved";
    fs::create_directory(original);
    save(original / "original.bin");
    struct stat before{};
    ::stat(original.c_str(), &before);
    CaptureOwnership owner(original);
    fs::rename(original, moved);
    fs::create_directory(original);
    save(original / "unrelated.bin");
    owner.finish();
    require(transfers == std::vector<std::string>({"original.bin", "directory-last"}), "Ownership followed a replacement path");
    require(transferred_directory == before.st_ino, "Ownership transferred the wrong directory");
}
void links_and_subdirectories_are_rejected() {
    for(unsigned kind = 0; kind < 3; ++kind) {
        reset(); TemporaryDirectory directory; TemporaryDirectory external;
        save(external.path / "file");
        const auto entry = directory.path / "unsafe";
        if(kind == 0) fs::create_symlink(external.path / "file", entry);
        else if(kind == 1) fs::create_hard_link(external.path / "file", entry);
        else fs::create_directory(entry);
        CaptureOwnership owner(directory.path);
        must_reject([&] { owner.finish(); });
        require(transfers.empty(), "Unsafe entry was handed to chown");
        fs::remove(entry); // Allow normal RAII cleanup after the rejection.
    }
}
void directory_symlink_is_rejected() {
    reset(); TemporaryDirectory directory;
    fs::create_directory(directory.path / "real");
    fs::create_directory_symlink(directory.path / "real", directory.path / "link");
    must_reject([&] { CaptureOwnership owner(directory.path / "link"); });
    require(transfers.empty(), "Directory symlink was followed");
}
void transfer_failure_is_reported_and_retryable() {
    reset(); TemporaryDirectory directory;
    save(directory.path / "frame.bin");
    CaptureOwnership owner(directory.path);
    reject_file_transfer = true;
    must_reject([&] { owner.finish(); });
    require(transfers.empty(), "Directory changed owner despite a failed file transfer");
    reject_file_transfer = false;
    owner.finish();
    require(transfers.size() == 2, "Failed transfer could not be retried");
}
void exception_path_still_restores_ownership() {
    reset(); TemporaryDirectory directory;
    save(directory.path / "incomplete-manifest.json");
    try {
        CaptureOwnership owner(directory.path);
        throw std::runtime_error("simulated capture failure");
    } catch(const std::runtime_error &) {}
    require(transfers.size() == 2 && transfers.back() == "directory-last", "RAII failed to return partial capture files");
}
} // namespace

int main() {
    try {
        transfer_is_ordered_and_idempotent();
        ordinary_user_and_direct_root_do_not_chown();
        malformed_owner_is_rejected();
        ownership_uses_original_directory_after_rename();
        links_and_subdirectories_are_rejected();
        directory_symlink_is_rejected();
        transfer_failure_is_reported_and_retryable();
        exception_path_still_restores_ownership();
        std::cout << "All capture ownership tests passed (8 cases; no privileged operations)\n";
        return 0;
    } catch(const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}

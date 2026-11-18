// SPDX-License-Identifier: GPL-3.0-or-later
// Return newly captured private files to the ordinary user who invoked sudo.
#pragma once

#include "probe_core.hpp"
#include <cstdlib>
#include <dirent.h>
#include <sys/stat.h>

namespace femto_bolt {

class CaptureOwnership {
    int directory_fd_ = -1;
    uid_t uid_ = 0;
    gid_t gid_ = 0;
public:
    explicit CaptureOwnership(const std::filesystem::path &directory) {
        if(::geteuid() != 0) return;
        const char *uid = std::getenv("SUDO_UID"), *gid = std::getenv("SUDO_GID");
        if(!uid || !gid) return; // Direct root execution has no ordinary owner.
        uid_ = parse_unsigned(uid, std::numeric_limits<unsigned>::max(), "SUDO_UID");
        gid_ = parse_unsigned(gid, std::numeric_limits<unsigned>::max(), "SUDO_GID");
        if(uid_ == 0) return;
        directory_fd_ = ::open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
        if(directory_fd_ < 0)
            throw std::system_error(errno, std::generic_category(), "Open new capture directory for ownership transfer");
    }
    CaptureOwnership(const CaptureOwnership &) = delete;
    CaptureOwnership &operator=(const CaptureOwnership &) = delete;

    void finish() {
        if(directory_fd_ < 0) return;
        // Anchor all operations to the originally created directory. It stays
        // root-only until every regular file has been transferred, and no
        // recursive walk or symlink traversal is used.
        const int iterator_fd = ::dup(directory_fd_);
        if(iterator_fd < 0) throw std::system_error(errno, std::generic_category(), "Duplicate capture directory descriptor");
        DIR *entries = ::fdopendir(iterator_fd);
        if(!entries) {
            const int error = errno;
            ::close(iterator_fd);
            throw std::system_error(error, std::generic_category(), "Read capture directory entries");
        }
        try {
            ::rewinddir(entries);
            while(true) {
                errno = 0;
                const auto *entry = ::readdir(entries);
                if(!entry) {
                    if(errno) throw std::system_error(errno, std::generic_category(), "Read capture directory");
                    break;
                }
                const std::string name(entry->d_name);
                if(name == "." || name == "..") continue;
                struct stat status{};
                if(::fstatat(directory_fd_, name.c_str(), &status, AT_SYMLINK_NOFOLLOW) != 0)
                    throw std::system_error(errno, std::generic_category(), "Inspect captured file");
                if(!S_ISREG(status.st_mode) || status.st_nlink != 1)
                    throw std::runtime_error("Refusing ownership transfer for a nonregular or linked capture file");
                if(::fchownat(directory_fd_, name.c_str(), uid_, gid_, AT_SYMLINK_NOFOLLOW) != 0)
                    throw std::system_error(errno, std::generic_category(), "Return captured file to the invoking user");
            }
        } catch(...) {
            ::closedir(entries);
            throw;
        }
        ::closedir(entries);
        if(::fchown(directory_fd_, uid_, gid_) != 0)
            throw std::system_error(errno, std::generic_category(), "Return capture directory to the invoking user");
        ::close(directory_fd_);
        directory_fd_ = -1;
    }

    ~CaptureOwnership() {
        try { finish(); }
        catch(const std::exception &exc) {
            std::fprintf(stderr, "Capture ownership cleanup failed: %s\n", exc.what());
            if(directory_fd_ >= 0) ::close(directory_fd_);
        }
    }
};
} // namespace femto_bolt

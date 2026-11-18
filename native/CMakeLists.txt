# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 pyBravo contributors.
# Extracted and adapted for OSX-Femto-Bolt; see ../NOTICE.
cmake_minimum_required(VERSION 3.16)
project(osx_femto_bolt LANGUAGES C CXX)

include(CTest)
option(FEMTO_CORE_TESTS_ONLY "Build portable transport tests without SDK or camera access" OFF)
if(BUILD_TESTING OR FEMTO_CORE_TESTS_ONLY)
    enable_testing()
    find_package(Threads REQUIRED)
    add_executable(femto_probe_core_tests probe_core_tests.cpp)
    target_compile_features(femto_probe_core_tests PRIVATE cxx_std_17)
    target_link_libraries(femto_probe_core_tests PRIVATE Threads::Threads)
    add_test(NAME femto_probe_core COMMAND femto_probe_core_tests)

    add_executable(femto_capture_ownership_tests capture_ownership_tests.cpp)
    target_compile_features(femto_capture_ownership_tests PRIVATE cxx_std_17)
    add_test(NAME femto_capture_ownership COMMAND femto_capture_ownership_tests)

    add_executable(femto_live_core_tests live_capture_core_tests.cpp)
    target_compile_features(femto_live_core_tests PRIVATE cxx_std_17)
    target_link_libraries(femto_live_core_tests PRIVATE Threads::Threads)
    foreach(scenario bounded oldest stop failure missing_stream_stale invalid_kind
            empty oversized hello payload_limit mode_nfov mode_wide mode_invalid mode_profiles)
        add_test(NAME femto_live_${scenario} COMMAND femto_live_core_tests ${scenario})
    endforeach()
endif()
if(FEMTO_CORE_TESTS_ONLY)
    return()
endif()

if(NOT APPLE)
    message(FATAL_ERROR "The camera transport supports macOS only; use FEMTO_CORE_TESTS_ONLY for portable tests")
endif()

set(FEMTO_SDK_SOURCE "" CACHE PATH "Pinned Orbbec SDK source checkout")
set(FEMTO_SDK_REVISION "575347ca742d219ef61b1e214e97043f17f48d23")
if(NOT EXISTS "${FEMTO_SDK_SOURCE}/src/platform/Platform.hpp")
    message(FATAL_ERROR "Set FEMTO_SDK_SOURCE to the SDK v2.10.6 source checkout")
endif()
find_package(Git REQUIRED)
execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${FEMTO_SDK_SOURCE}" rev-parse HEAD
    OUTPUT_VARIABLE sdk_revision OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE revision_result)
if(NOT revision_result EQUAL 0 OR NOT sdk_revision STREQUAL FEMTO_SDK_REVISION)
    message(FATAL_ERROR "The internal transport interface requires SDK revision ${FEMTO_SDK_REVISION}")
endif()

# Only the source-built USB transport and its common libraries are linked.
# No depth engine, binary extensions, SDK installation, network discovery,
# or camera firmware update is involved.
set(OB_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(OB_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(OB_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(OB_BUILD_DOCS OFF CACHE BOOL "" FORCE)
set(OB_BUILD_USB_PAL ON CACHE BOOL "" FORCE)
set(OB_BUILD_NET_PAL OFF CACHE BOOL "" FORCE)
set(OB_BUILD_GMSL_PAL OFF CACHE BOOL "" FORCE)
set(OB_INSTALL_EXAMPLES_SOURCE OFF CACHE BOOL "" FORCE)
set(OB_BUILD_WITH_EXTENSIONS_COMMIT_HASH OFF CACHE BOOL "" FORCE)
set(OB_ENABLE_BOLT_OPENGL_COMPAT OFF CACHE BOOL "" FORCE)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib")
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib")
add_subdirectory("${FEMTO_SDK_SOURCE}" sdk EXCLUDE_FROM_ALL)

# The SDK's macOS enumeration guard belongs to its processed-depth API. Replace
# only this raw prototype's compiled enumerator; keep the upstream source intact.
set(femto_enumerator "${FEMTO_SDK_SOURCE}/src/platform/usb/enumerator/UsbEnumeratorLibusb.cpp")
set(femto_raw_enumerator "${CMAKE_BINARY_DIR}/generated/UsbEnumeratorLibusb.cpp")
include("${CMAKE_CURRENT_SOURCE_DIR}/prepare_transport.cmake")
femto_prepare_transport("${femto_enumerator}" "${femto_raw_enumerator}")
get_target_property(femto_platform_sources platform SOURCES)
list(FIND femto_platform_sources "${femto_enumerator}" femto_enumerator_index)
if(femto_enumerator_index EQUAL -1)
    message(FATAL_ERROR "SDK platform target no longer contains the expected USB enumerator")
endif()
list(REMOVE_ITEM femto_platform_sources "${femto_enumerator}")
list(APPEND femto_platform_sources "${femto_raw_enumerator}")
set_property(TARGET platform PROPERTY SOURCES "${femto_platform_sources}")
target_include_directories(platform PRIVATE "${FEMTO_SDK_SOURCE}/src/platform/usb/enumerator")

add_executable(femto_raw_probe femto_raw_probe.cpp)
target_compile_features(femto_raw_probe PRIVATE cxx_std_17)
target_compile_definitions(femto_raw_probe PRIVATE FEMTO_SDK_REVISION="${FEMTO_SDK_REVISION}")
target_compile_definitions(femto_raw_probe PRIVATE FEMTO_TRANSPORT_PATCH_ID="bolt_raw_enumeration_v1")
target_link_libraries(femto_raw_probe PRIVATE ob::platform)

add_executable(femto_live_capture femto_live_capture.cpp)
target_compile_features(femto_live_capture PRIVATE cxx_std_17)
target_link_libraries(femto_live_capture PRIVATE ob::platform)

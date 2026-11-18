# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 pyBravo contributors.
# Extracted and adapted for OSX-Femto-Bolt; see ../NOTICE.
# The pinned SDK excludes Bolt on macOS because normal depth needs a missing
# engine. The experimental raw-only transport must still enumerate its USB port.
# Generate a local translation unit without changing the checked-out SDK.
function(femto_prepare_transport source destination)
    get_filename_component(source_absolute "${source}" ABSOLUTE)
    get_filename_component(destination_absolute "${destination}" ABSOLUTE)
    if(source_absolute STREQUAL destination_absolute)
        message(FATAL_ERROR "Raw transport generation must not overwrite SDK source")
    endif()
    file(READ "${source}" contents)
    set(bolt_exclusion [=[        // filter out Femto Bolt device
        if((desc.idProduct == 0x066B) && (desc.idVendor = ORBBEC_DEVICE_VID)) {
            LOG_WARN("Femto Bolt is unavailable on macOS duo to Depth Engine");
            continue;
        }
]=])
    string(FIND "${contents}" "${bolt_exclusion}" first_match)
    if(first_match EQUAL -1)
        message(FATAL_ERROR "Pinned macOS Bolt exclusion was not found; inspect SDK source before building")
    endif()
    string(LENGTH "${bolt_exclusion}" exclusion_length)
    math(EXPR after_match "${first_match} + ${exclusion_length}")
    string(SUBSTRING "${contents}" ${after_match} -1 remainder)
    string(FIND "${remainder}" "${bolt_exclusion}" second_match)
    if(NOT second_match EQUAL -1)
        message(FATAL_ERROR "Multiple Bolt exclusions found; refusing ambiguous source transformation")
    endif()
    string(REPLACE "${bolt_exclusion}"
        "        // OSX-Femto-Bolt raw transport: enumerate Bolt without a depth engine.\n"
        patched "${contents}")
    get_filename_component(destination_directory "${destination}" DIRECTORY)
    file(MAKE_DIRECTORY "${destination_directory}")
    file(WRITE "${destination}" "${patched}")
endfunction()

# Standalone invocation also supports hardware-free regression tests.
if(DEFINED FEMTO_TRANSPORT_INPUT AND DEFINED FEMTO_TRANSPORT_OUTPUT)
    femto_prepare_transport("${FEMTO_TRANSPORT_INPUT}" "${FEMTO_TRANSPORT_OUTPUT}")
endif()

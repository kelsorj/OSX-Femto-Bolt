#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Run this wrapper without sudo. Only the native USB probe needs elevation.
set -eu

usage() {
    printf '%s\n' 'Usage: scripts/capture_calibration.sh SERIAL NEW_DIRECTORY [NATIVE_PROBE]' \
        'Captures two copies of the camera factory calibration; output must not exist.' \
        'This does not create the separate native depth-compute profile.'
}

if [ "${1-}" = '--help' ]; then
    usage
    exit 0
fi
if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    usage >&2
    exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
    printf '%s\n' 'Run this script without sudo. It elevates only the native probe.' >&2
    exit 2
fi
case "$1" in
    ''|*[!A-Za-z0-9_-]*)
        printf '%s\n' 'The serial must contain only letters, digits, underscores or hyphens.' >&2
        exit 2 ;;
esac
script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_directory=$(dirname -- "$script_directory")
probe=${3:-"$project_directory/build/native/bin/femto_raw_probe"}
case "$probe" in
    /*) ;;
    *) probe="$PWD/$probe" ;;
esac
case "$2" in
    /*) destination=$2 ;;
    *) destination="$PWD/$2" ;;
esac
if [ ! -x "$probe" ] || [ -L "$probe" ]; then
    printf '%s\n' "Build the native probe first: python3 $project_directory/scripts/build_native.py" >&2
    exit 2
fi
if [ -e "$destination" ] || [ -L "$destination" ]; then
    printf '%s\n' 'The output directory already exists; choose a new directory.' >&2
    exit 2
fi
if [ ! -d "$(dirname -- "$destination")" ]; then
    printf '%s\n' 'Create the parent directory as your ordinary user first.' >&2
    exit 2
fi
printf '%s\n' 'Close other applications using the camera. The native probe may request your administrator password.'
/usr/bin/sudo "$probe" --nvram "$destination" --serial "$1" --frames 2 --timeout 10
printf '%s\n' "Saved private factory data in: $destination" \
    'These files belong to your account. Do not publish them.' \
    'A matching native compute profile is also required before depth conversion can run.'

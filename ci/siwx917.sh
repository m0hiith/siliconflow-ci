#!/usr/bin/env bash
# The stages of the siwx917-soc profile, run through ci/stage.sh.
#
#   ci/siwx917.sh generate   slc generate firmware/siwx917 for BRD4338A into $OUT/gen,
#                            plus autogen/version.h (the boot line's version and sha)
#                            and autogen/sfci_build.h (SFCI_HIL_HOOKS)
#   ci/siwx917.sh compile    build the generated makefile project (release)
#   ci/siwx917.sh package    package the ELF, .bin, .map and the SDK's flash images
#
# $OUT is $SFCI_OUT_DIR, default "out". Only firmware/siwx917/*.slcp and its
# sources are committed; everything under $OUT/gen is generated.
#
# Environment:
#   SFCI_SDK_PACKAGE_PATH  comma-separated SDK paths for slc --sdk-package-path
#                          (default: the SDKs slc is already configured with)
#   ARM_GCC_DIR            GNU Arm toolchain root (default: the one slc recorded)
#   SFCI_HIL_HOOKS         1 builds in the hardware-in-the-loop test hooks (a UART
#                          command that drives the button, for tests/hil); default 0.
#                          Never set it for an image that may be released.
set -euo pipefail

PROJECT=siwx917_soc
SLCP=firmware/siwx917/$PROJECT.slcp
BOARD=brd4338a

out_dir=${SFCI_OUT_DIR:-out}
gen_dir="$out_dir/gen"
build_dir="$gen_dir/build/release"
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# What firmware/cmake/gen_version.cmake writes for CMake builds, for the slc
# project: FW_VERSION from `git describe --always --dirty`, FW_COMMIT_SHORT
# from `git rev-parse --short HEAD`, "unknown" when git cannot say.
write_version_h() {
    local version sha template
    version=$(git describe --always --dirty 2>/dev/null) || version=
    sha=$(git rev-parse --short HEAD 2>/dev/null) || sha=
    template=$(<firmware/include/version.h.in)
    # Quoted replacements are literal, even with bash 5.2's patsub_replacement.
    template=${template//@FW_VERSION@/"${version:-unknown}"}
    template=${template//@FW_COMMIT_SHORT@/"${sha:-unknown}"}
    printf '%s\n' "$template" >"$1"
    echo "siwx917.sh: FW_VERSION=${version:-unknown} FW_COMMIT_SHORT=${sha:-unknown}"
}

write_build_h() {
    local hooks=${SFCI_HIL_HOOKS:-0}
    if [[ $hooks != 0 && $hooks != 1 ]]; then
        echo "siwx917.sh: SFCI_HIL_HOOKS must be 0 or 1, not '$hooks'" >&2
        exit 2
    fi
    printf '%s\n' "/* Written by ci/siwx917.sh generate. */" "#ifndef SFCI_BUILD_H" "#define SFCI_BUILD_H" \
        "#define SFCI_HIL_HOOKS $hooks" "#endif" >"$1"
    echo "siwx917.sh: SFCI_HIL_HOOKS=$hooks"
}

generate() {
    # Start clean so a re-run never builds stale generated files.
    rm -rf "$gen_dir"
    local args=(generate -p "$SLCP" -d "$gen_dir" --with "$BOARD" -o makefile)
    if [[ -n ${SFCI_SDK_PACKAGE_PATH:-} ]]; then
        args+=("--sdk-package-path=$SFCI_SDK_PACKAGE_PATH")
    fi
    slc "${args[@]}"
    # slc can report success without writing a project; the makefile is what compile needs.
    if [[ ! -f $gen_dir/$PROJECT.Makefile ]]; then
        echo "siwx917.sh: slc generate did not write $gen_dir/$PROJECT.Makefile" >&2
        exit 1
    fi
    # autogen/ is on the generated project's include path.
    write_version_h "$gen_dir/autogen/version.h"
    write_build_h "$gen_dir/autogen/sfci_build.h"
}

compile() {
    make -C "$gen_dir" -f "$PROJECT.Makefile" release -j"$(nproc 2>/dev/null || echo 2)"
}

package() {
    local files=() name
    # The ELF (the SDK names it .out), its raw image and the link map must exist.
    for name in "$PROJECT.out" "$PROJECT.bin" "$PROJECT.map"; do
        files+=("$build_dir/$name")
    done
    # Flash images from the SDK's post-build (Simplicity Commander), when it ran.
    for name in "$PROJECT.rps" "${PROJECT}_isp.bin" "$PROJECT.hex" "$PROJECT.s37"; do
        if [[ -f $build_dir/$name ]]; then
            files+=("$build_dir/$name")
        else
            echo "siwx917.sh: no $name (post-build did not produce it)"
        fi
    done
    "$script_dir/package.sh" "${files[@]}"
}

case ${1:-} in
generate | compile | package) "$1" ;;
*)
    echo "usage: ci/siwx917.sh generate|compile|package" >&2
    exit 2
    ;;
esac

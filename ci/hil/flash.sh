#!/usr/bin/env bash
# Flash the SiWx917 board with Simplicity Commander, or reset it.
#
#   ci/hil/flash.sh flash <image>   check the serial port and probe, flash,
#                                   verify, reset
#   ci/hil/flash.sh reset           reset the board (ci/hil/boot_check.py)
#
# Environment (set on the self-hosted runner, not in the workflow):
#   SFCI_HIL_DEVICE        the target part, for Commander's --device
#   SFCI_HIL_SERIAL_PORT   the board's UART, e.g. /dev/ttyACM0
#   SFCI_HIL_PROBE_SERIAL  optional: the debug probe's serial number, when
#                          the runner has more than one probe
#   SFCI_HIL_COMMANDER     optional: the commander binary (default: commander)
#
# Exit codes:
#   0   done
#   2   usage or configuration error, including the TODO below not filled in
#   75  infrastructure: serial port missing or busy, probe not found, flash
#       or verify or reset failed. The image was SHA-256-verified by
#       ci/hil/fetch.py, so a failure to write it is the hardware's.
#       ci/hil/retry_infra.sh retries on 75, and the backend classifies the
#       stage as infra_hw.
set -euo pipefail

INFRA=75
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

die() {
    echo "flash.sh: $1" >&2
    exit "$2"
}

usage() { die "usage: ci/hil/flash.sh flash <image> | reset" 2; }

[[ $# -ge 1 ]] || usage
mode=$1
image=
case $mode in
flash)
    [[ $# -eq 2 ]] || usage
    image=$2
    [[ -f $image ]] || die "no such image: $image" 2
    ;;
reset) [[ $# -eq 1 ]] || usage ;;
*) usage ;;
esac

device=${SFCI_HIL_DEVICE:-}
port=${SFCI_HIL_SERIAL_PORT:-}
probe_serial=${SFCI_HIL_PROBE_SERIAL:-}
commander=${SFCI_HIL_COMMANDER:-commander}
[[ -n $device ]] || die "SFCI_HIL_DEVICE is not set on this runner" 2
[[ -n $port ]] || die "SFCI_HIL_SERIAL_PORT is not set on this runner" 2
command -v "$commander" >/dev/null || die "Simplicity Commander not found: $commander" 2

# >>> TODO(hil): Simplicity Commander arguments ---------------------------------
# Fill each array from your working local command; leave none empty. They are
# passed to "$commander" as they are. Available: "$device", "$image" (flash,
# verify), "$probe_serial" (may be empty). If one of your commands already
# verifies or resets, point the next array at a harmless check instead of
# deleting it (each array is one step that must succeed).
probe_args=()  # TODO: confirm the probe and target answer (no flash change)
flash_args=()  # TODO: write "$image" to the target
verify_args=() # TODO: verify the target's flash against "$image"
reset_args=()  # TODO: reset the target so the firmware boots
# <<< TODO(hil) -----------------------------------------------------------------

# Run one Commander step; any failure is the hardware's (exit 75).
step() {
    local name=$1 what=$2
    shift 2
    if [[ $# -eq 0 ]]; then
        die "TODO: fill ${name}_args in ci/hil/flash.sh with your Commander arguments" 2
    fi
    echo "flash.sh: $name: $commander $*"
    if ! "$commander" "$@"; then
        die "$what" "$INFRA"
    fi
}

if [[ $mode == reset ]]; then
    step reset "reset failed" "${reset_args[@]+"${reset_args[@]}"}"
    exit 0
fi

# Fail before flashing if the test could not read the UART afterwards.
python3 "$script_dir/serial_port.py" check "$port" || exit $?
step probe "probe not found, or the target does not answer" "${probe_args[@]+"${probe_args[@]}"}"
step flash "flash failed" "${flash_args[@]+"${flash_args[@]}"}"
step verify "flash verify failed" "${verify_args[@]+"${verify_args[@]}"}"
step reset "reset failed" "${reset_args[@]+"${reset_args[@]}"}"
echo "flash.sh: flashed $(basename "$image") to $device"

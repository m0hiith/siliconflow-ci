#!/usr/bin/env bash
# Run a command, and run it once more if it fails with exit code 75
# (infrastructure, e.g. the probe was not found). Any other result is final.
#
#   ci/hil/retry_infra.sh <command...>
#
# Used for flashing only: tests are never retried. Waits
# $SFCI_HIL_RETRY_DELAY_S seconds (default 5) before the retry, so the probe
# can re-enumerate. Exits with the last attempt's exit code.
set -uo pipefail

INFRA=75

if [[ $# -lt 1 ]]; then
    echo "usage: ci/hil/retry_infra.sh <command...>" >&2
    exit 2
fi

"$@"
rc=$?
if [[ $rc -ne $INFRA ]]; then
    exit "$rc"
fi
delay=${SFCI_HIL_RETRY_DELAY_S:-5}
echo "retry_infra.sh: infrastructure failure (exit $INFRA); retrying once in ${delay}s" >&2
sleep "$delay"
"$@"
exit $?

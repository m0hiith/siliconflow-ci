#!/usr/bin/env bash
# Static analysis with cppcheck. Fails only on findings of severity "error";
# warnings and style findings are reported and counted but do not fail.
#
#   ci/analyze.sh <build-dir>
#
# Writes $OUT/analysis/cppcheck.xml and $OUT/analysis/cppcheck-summary.json.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: ci/analyze.sh <build-dir>" >&2
    exit 2
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
build_dir=$1
out_dir=${SFCI_OUT_DIR:-out}
mkdir -p "$out_dir/analysis"

cppcheck --version
cppcheck \
    --std=c11 \
    --enable=warning,style,performance,portability \
    --inline-suppr \
    --error-exitcode=0 \
    -I firmware/include \
    -I "$build_dir/generated/include" \
    --xml --xml-version=2 \
    --output-file="$out_dir/analysis/cppcheck.xml" \
    firmware/src

python3 "$script_dir/cppcheck_gate.py" \
    "$out_dir/analysis/cppcheck.xml" \
    "$out_dir/analysis/cppcheck-summary.json"

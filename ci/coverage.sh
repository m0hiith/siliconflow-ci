#!/usr/bin/env bash
# Line coverage of the unit tests, as Cobertura XML for ci/report.py.
#
#   ci/coverage.sh <build-dir>
#
# Configures firmware/ again in <build-dir> with -DSFCI_COVERAGE=ON, so the
# packaged build stays uninstrumented, then runs the firmware's `coverage`
# target (ctest + gcovr) and copies its report to $OUT/coverage/coverage.xml.
# $OUT is $SFCI_OUT_DIR, default "out".
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: ci/coverage.sh <build-dir>" >&2
    exit 2
fi

build_dir=$1
out_dir=${SFCI_OUT_DIR:-out}

cmake -S firmware -B "$build_dir" -G Ninja -DSFCI_COVERAGE=ON
# The coverage target runs the tests but does not build them.
cmake --build "$build_dir"
cmake --build "$build_dir" --target coverage

mkdir -p "$out_dir/coverage"
cp "$build_dir/coverage.xml" "$out_dir/coverage/coverage.xml"

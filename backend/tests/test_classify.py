"""services.classify against real gcc, ld, ctest, cppcheck, CMake and OpenOCD
output (tests/fixtures/logs, see its README for how each was produced)."""
from functools import cache
from pathlib import Path

import pytest

from app.services import classify
from app.services.classify import RULES, FirstError

LOGS = Path(__file__).parent / "fixtures" / "logs"


@cache
def _lines(name: str) -> tuple[str, ...]:
    return tuple((LOGS / name).read_text().split("\n")[:-1])


def lines(name: str) -> list[str]:
    return list(_lines(name))


# Every rule, with the real lines (file, 1-based line number) it must match.
RULE_SAMPLES: dict[str, list[tuple[str, int]]] = {
    "probe-not-found": [("openocd-no-cmsis-dap.flash.log", 8), ("openocd-no-jlink.flash.log", 8)],
    "openocd-init-failed": [("openocd-no-cmsis-dap.flash.log", 9), ("openocd-no-jlink.flash.log", 9)],
    "no-space-left": [("gcc-no-space.compile.log", 5)],
    "compiler-killed": [("gcc-oom-killed.compile.log", 1)],
    "host-unresolved": [("cmake-no-network.configure.log", 10)],
    "ld-region-overflowed": [("ld-region-overflowed.link.log", 2)],
    "ld-section-will-not-fit": [("ld-section-will-not-fit.link.log", 1), ("ld-region-overflowed.link.log", 1)],
    "ld-undefined-reference": [("ld-undefined-reference.compile.log", 14)],
    "ld-multiple-definition": [("ld-multiple-definition.compile.log", 14)],
    "ld-returned": [("ld-undefined-reference.compile.log", 15), ("ld-multiple-definition.compile.log", 15)],
    "unity-fail": [("ctest-unity-fail.test.log", 15)],
    "sanitizer-runtime-error": [("ctest-ubsan.test.log", 9)],
    "ctest-failed": [("ctest-unity-fail.test.log", 27), ("ctest-ubsan.test.log", 16)],
    "cppcheck-gate": [("cppcheck-null-pointer.analyze.log", 9)],
    "cppcheck-error": [("cppcheck-null-pointer.analyze.log", 10)],
    "gcc-error": [
        ("gcc-undeclared.compile.log", 5),
        ("gcc-werror.compile.log", 9),
        ("gcc-undeclared-color.log", 2),
    ],
    "gcc-werror": [("gcc-werror.compile.log", 12)],
    "cmake-error": [("cmake-error.configure.log", 7)],
}
RULES_BY_ID = {rule.id: rule for rule in RULES}


def test_every_rule_has_real_samples():
    assert set(RULE_SAMPLES) == set(RULES_BY_ID)
    assert len(RULES_BY_ID) == len(RULES)  # ids are unique
    assert {rule.failure_class for rule in RULES} == set(classify.FAILURE_CLASSES)


@pytest.mark.parametrize(
    ("rule_id", "sample"),
    [(rule_id, sample) for rule_id, samples in RULE_SAMPLES.items() for sample in samples],
)
def test_rule_matches_its_sample(rule_id, sample):
    name, number = sample
    line = classify.strip_ansi(lines(name)[number - 1])
    assert RULES_BY_ID[rule_id].pattern.search(line), line


@pytest.mark.parametrize("name", sorted(p.name for p in LOGS.glob("clean.*.log")))
def test_clean_logs_match_nothing(name):
    # Includes "cppcheck: error=0, warning=0, ..." and CMake/ninja progress.
    idx = classify.index(lines(name))
    assert (idx.failure_class, idx.errors, idx.warnings, idx.first_error) == (None, [], [], None)
    assert idx.line_count == len(lines(name))
    assert all(not rule.pattern.search(line) for rule in RULES for line in lines(name))


# fixture -> class, deciding rule, error lines, warning lines, first error
CASES = {
    "gcc-undeclared.compile.log": (
        "compile_error", "gcc-error", [2, 5], [],
        FirstError(5, "'SL_MAVG_LIMIT' undeclared (first use in this function)",
                   "/w/firmware/src/sensor_logic.c", 74, 20),
    ),
    "gcc-undeclared-color.log": (
        "compile_error", "gcc-error", [2], [],
        FirstError(2, "'SL_MAVG_LIMIT' undeclared (first use in this function)",
                   "firmware/src/sensor_logic.c", 74, 20),
    ),
    "gcc-werror.compile.log": (
        "compile_error", "gcc-error", [6, 9, 12], [],
        FirstError(9, "unused variable 'scratch' [-Werror=unused-variable]",
                   "/w/firmware/src/sensor_logic.c", 74, 9),
    ),
    "cmake-error.configure.log": (
        "compile_error", "cmake-error", [7], [],
        FirstError(7, 'Cannot specify link libraries for target "firmware_app" which is not built '
                      "by this project.", "CMakeLists.txt", 59, None),
    ),
    "ld-undefined-reference.compile.log": (
        "link_error", "ld-undefined-reference", [11, 14, 15], [],
        FirstError(14, "undefined reference to `sl_calibrate'", "main.c", None, None),
    ),
    "ld-multiple-definition.compile.log": (
        "link_error", "ld-multiple-definition", [11, 14, 15], [],
        FirstError(14, "multiple definition of `hal_now_ms'; CMakeFiles/firmware.dir/src/main.c.o:"
                       "main.c:(.text+0x2d0): first defined here", "hal_fake.c", None, None),
    ),
    "ld-region-overflowed.link.log": (
        "size_overflow", "ld-region-overflowed", [1, 2, 3], [],
        FirstError(2, "region `RAM' overflowed by 3072 bytes"),
    ),
    "ld-section-will-not-fit.link.log": (
        "size_overflow", "ld-section-will-not-fit", [1, 2], [],
        FirstError(1, "big_flash.elf section `.text' will not fit in region `FLASH'"),
    ),
    "ctest-unity-fail.test.log": (
        "test_failure", "unity-fail", [8, 15, 27], [],
        FirstError(15, "test_noisy_signal_around_one_threshold: Expected 2 Was 1",
                   "/w/firmware/tests/test_threshold.c", 75, None),
    ),
    "ctest-ubsan.test.log": (
        "test_failure", "sanitizer-runtime-error", [8, 9, 16], [],
        FirstError(9, "index 4 out of bounds for type 'int [4]'",
                   "/w/firmware/tests/test_threshold.c", 27, 5),
    ),
    "cppcheck-null-pointer.analyze.log": (
        "analysis", "cppcheck-gate", [9, 10], [11],
        FirstError(10, "Null pointer dereference: p [nullPointer]",
                   "firmware/src/sensor_logic.c", 111, 13),
    ),
    "gcc-no-space.compile.log": (
        "infra_ci", "no-space-left", [3, 5, 10, 12, 17, 19, 24, 26, 31, 33, 38, 40], [],
        FirstError(5, "error closing /tmp/ccsSVAMp.s: No space left on device",
                   "/w/firmware/src/main.c", 58, 1),
    ),
    "gcc-oom-killed.compile.log": (
        "infra_ci", "compiler-killed", [1], [],
        FirstError(1, "gcc: fatal error: Killed signal terminated program cc1"),
    ),
    "cmake-no-network.configure.log": (
        "infra_ci", "host-unresolved", [10, 12, 14, 16, 20, 24], [],
        FirstError(10, "fatal: unable to access 'https://github.com/ThrowTheSwitch/Unity.git/': "
                       "Could not resolve host: github.com"),
    ),
    "openocd-no-cmsis-dap.flash.log": (
        "infra_hw", "probe-not-found", [8, 9], [],
        FirstError(8, "Error: unable to find a matching CMSIS-DAP device"),
    ),
    "openocd-no-jlink.flash.log": (
        "infra_hw", "probe-not-found", [8, 9], [],
        FirstError(8, "Error: No J-Link device found"),
    ),
}


def test_every_failure_sample_has_a_case():
    samples = {p.name for p in LOGS.glob("*.log") if not p.name.startswith("clean.")}
    assert samples == set(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_index_of_real_log(name):
    failure_class, rule, errors, warnings, first = CASES[name]
    idx = classify.index(lines(name))
    assert (idx.failure_class, idx.rule) == (failure_class, rule)
    assert (idx.errors, idx.warnings) == (errors, warnings)
    assert (idx.error_count, idx.warning_count) == (len(errors), len(warnings))
    assert idx.first_error == first
    assert idx.line_count == len(lines(name))


# --------------------------------------------------------------- ordering
# Each of these logs also matches a rule of a lower-priority class; the
# table order is what makes the classification right.


@pytest.mark.parametrize(
    ("name", "also_matches", "expected"),
    [
        # gcc prints the full disk as "main.c:58:1: fatal error: ..."
        ("gcc-no-space.compile.log", "gcc-error", "infra_ci"),
        # cppcheck findings look like gcc errors
        ("cppcheck-null-pointer.analyze.log", "gcc-error", "analysis"),
        # the linker ends every failure with "ld returned 1 exit status"
        ("ld-region-overflowed.link.log", "ld-returned", "size_overflow"),
        ("ld-section-will-not-fit.link.log", "ld-returned", "size_overflow"),
        # the failed clone surfaces as a CMake error
        ("cmake-no-network.configure.log", "cmake-error", "infra_ci"),
        # a probe that is not found ends OpenOCD's init
        ("openocd-no-jlink.flash.log", "openocd-init-failed", "infra_hw"),
    ],
)
def test_rule_order_decides(name, also_matches, expected):
    log = [classify.strip_ansi(line) for line in lines(name)]
    assert any(RULES_BY_ID[also_matches].pattern.search(line) for line in log)
    assert classify.index(lines(name)).failure_class == expected


def test_first_rule_wins_regardless_of_line_order():
    # The undefined reference comes after a size overflow in the log, but
    # size_overflow ranks higher.
    log = lines("ld-undefined-reference.compile.log") + lines("ld-region-overflowed.link.log")
    rule, number = classify.classify(log)
    assert (rule.id, number) == ("ld-region-overflowed", len(lines("ld-undefined-reference.compile.log")) + 2)


def test_classify_returns_none_for_unknown_failures():
    assert classify.classify(["make: *** [all] Error 2"]) is None
    idx = classify.index(["something odd", "FAILED: firmware"])
    assert (idx.failure_class, idx.errors) == (None, [2])
    assert idx.first_error == FirstError(2, "FAILED: firmware")


# ------------------------------------------------------------------ limits


def test_index_lists_are_capped_but_counts_are_exact():
    error = lines("gcc-undeclared.compile.log")[4]
    log = [error] * (classify.MAX_INDEXED_LINES + 5)
    idx = classify.index(log)
    assert len(idx.errors) == classify.MAX_INDEXED_LINES
    assert idx.error_count == classify.MAX_INDEXED_LINES + 5
    assert idx.errors[:3] == [1, 2, 3]


def test_long_messages_are_clipped():
    idx = classify.index(["src/a.c:1:1: error: " + "x" * 2000])
    assert len(idx.first_error.message) == classify.MAX_MESSAGE_LEN
    assert idx.first_error.message.endswith("…")


def test_index_is_json_ready():
    data = classify.index(lines("ctest-ubsan.test.log")).to_json()
    assert data["first_error"] == {
        "line_number": 9,
        "message": "index 4 out of bounds for type 'int [4]'",
        "file": "/w/firmware/tests/test_threshold.c",
        "line": 27,
        "column": 5,
    }
    assert data["failure_class"] == "test_failure"

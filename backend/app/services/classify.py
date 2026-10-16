"""Failure classification of stage logs. Pure functions, no I/O.

RULES is an ordered table of regular expressions, each matched against single
log lines. A log's class is the class of the first rule in the table that
matches any of its lines, so the order of the table is the priority:

  infra_hw, infra_ci  first: a missing debug probe, a full disk or an OOM-killed
                      compiler also print compiler- or test-shaped errors, and
                      none of them is the firmware's fault;
  size_overflow       before link_error: the linker reports both;
  analysis            before compile_error: cppcheck prints gcc-style
                      "file:line:col: error:" lines, told apart by the
                      trailing "[checkId]" (gcc's suffix starts with "-");
  test_failure        test output never contains compiler errors, so its
                      place is free.

A stage can also say it failed for infrastructure reasons with its exit code:
INFRA_HW_EXIT_CODE (75, sysexits' EX_TEMPFAIL) is what the hardware-in-the-loop
scripts (ci/hil) exit with when the probe, serial port or flash verification
failed. exit_code_index() classifies such a stage when there is no log.

index() also lists the line numbers (1-based) of error and warning lines and
extracts the first error: the first error line that names a source location
(skipping echoes such as ninja's "FAILED: <target>"), or, for infrastructure
failures, the line that decided the class, since that is the root cause.
"""
import re
from dataclasses import asdict, dataclass, field
from typing import Any

FAILURE_CLASSES = (
    "compile_error",
    "link_error",
    "size_overflow",
    "test_failure",
    "analysis",
    "infra_hw",
    "infra_ci",
)
INFRA_CLASSES = frozenset({"infra_hw", "infra_ci"})

# ci/hil/*: the debug probe, the serial port or flash verification failed.
INFRA_HW_EXIT_CODE = 75

# Keep the stored index small; counts are exact even when lists are cut.
MAX_INDEXED_LINES = 1000
MAX_MESSAGE_LEN = 500

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# "/usr/bin/ld: ", "arm-none-eabi-ld: ": dropped from unlocated messages.
LINKER_PREFIX = re.compile(r"^(?:\S*/)?[\w.-]*\bld: ")
# A path in a diagnostic: anything up to the next colon, not starting with a
# space (paths may contain spaces, e.g. "/home/runner/work/silicon labs/...").
_PATH = r"(?P<file>[^\s:][^:]*?)"


@dataclass(frozen=True)
class Rule:
    id: str
    failure_class: str
    pattern: re.Pattern[str]


def _rule(rule_id: str, failure_class: str, pattern: str) -> Rule:
    assert failure_class in FAILURE_CLASSES, failure_class
    return Rule(rule_id, failure_class, re.compile(pattern))


RULES: tuple[Rule, ...] = (
    # --- infra_hw: the debug probe or target, not the firmware
    _rule(
        "probe-not-found",
        "infra_hw",
        r"^Error: (?:unable to find a matching CMSIS-DAP device|No J-Link device found)",
    ),
    _rule("openocd-init-failed", "infra_hw", r"^\*\* OpenOCD init failed \*\*"),
    # --- infra_ci: the runner or the network
    _rule("no-space-left", "infra_ci", r"No space left on device"),
    _rule("compiler-killed", "infra_ci", r"fatal error: Killed signal terminated program \S+"),
    _rule("host-unresolved", "infra_ci", r"Could not resolve host: \S+"),
    # --- size_overflow: the image does not fit the target's memory
    _rule("ld-region-overflowed", "size_overflow", r"region `[^']+' overflowed by \d+ bytes?"),
    _rule("ld-section-will-not-fit", "size_overflow", r"section `[^']+' will not fit in region `[^']+'"),
    # --- link_error
    _rule("ld-undefined-reference", "link_error", r"undefined reference to `[^']+'"),
    _rule("ld-multiple-definition", "link_error", r"multiple definition of `[^']+'"),
    _rule("ld-returned", "link_error", r"\bld returned \d+ exit status"),
    # --- test_failure
    _rule("unity-fail", "test_failure", rf"^{_PATH}:\d+:\w+:FAIL\b"),
    _rule("sanitizer-runtime-error", "test_failure", rf"^{_PATH}:\d+:\d+: runtime error: "),
    _rule("ctest-failed", "test_failure", r"^The following tests FAILED:"),
    # --- analysis (cppcheck, as ci/cppcheck_gate.py prints it)
    _rule("cppcheck-gate", "analysis", r"^cppcheck: \d+ error\(s\); failing$"),
    _rule("cppcheck-error", "analysis", rf"^{_PATH}:\d+:\d+: error: .* \[[A-Za-z]\w*\]$"),
    # --- compile_error (CMake configuration errors included)
    _rule("gcc-error", "compile_error", rf"^{_PATH}:\d+:(?:\d+:)? (?:fatal )?error: "),
    _rule("gcc-werror", "compile_error", r"^cc1: all warnings being treated as errors$"),
    _rule("cmake-error", "compile_error", r"^CMake Error\b"),
)

# Lines that count as errors or warnings in the index, besides rule matches.
ERROR_LINE = re.compile(
    r"\b(?:fatal |runtime )?error: "  # gcc, ld/collect2, UBSan
    r"|^FAILED: "  # ninja
    r"|\*\*\*(?:Failed|Timeout|Exception)\b"  # ctest
    r"|^Error: "  # OpenOCD
    r"|^ERROR: "  # AddressSanitizer
    r"|^CMake Error\b"
)
WARNING_LINE = re.compile(
    r"\bwarning: "  # gcc, ld
    r"|^[^\s:][^:]*:\d+:\d+: (?:warning|style|performance|portability): .* \[[A-Za-z]\w*\]$"  # cppcheck
    r"|^CMake Warning\b"
    r"|^Warn : "  # OpenOCD
)

# Where a located error came from; tried in order on each error line.
LOCATIONS: tuple[re.Pattern[str], ...] = (
    # gcc, cppcheck, UBSan: file:line[:col]: [fatal |runtime ]error: message
    re.compile(rf"^{_PATH}:(?P<line>\d+):(?:(?P<column>\d+):)? (?:fatal |runtime )?error: (?P<message>.+)$"),
    # Unity: file:line:test:FAIL[: message]
    re.compile(rf"^{_PATH}:(?P<line>\d+):(?P<test>\w+):FAIL(?:: (?P<message>.*))?$"),
    # ld: file:(section+offset): message
    re.compile(rf"^{_PATH}:\([^)]*\): (?P<message>.+)$"),
    # CMake: the message is on the next, indented line
    re.compile(r"^CMake Error at (?P<file>.+?):(?P<line>\d+) \(\w+\):$"),
)


@dataclass(frozen=True)
class FirstError:
    line_number: int  # in the log, 1-based
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None


@dataclass
class LogIndex:
    line_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    errors: list[int] = field(default_factory=list)  # 1-based line numbers
    warnings: list[int] = field(default_factory=list)
    failure_class: str | None = None
    rule: str | None = None
    first_error: FirstError | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def exit_code_index(exit_code: int | None) -> LogIndex | None:
    """The index of a stage known only by its exit code: infra_hw for
    INFRA_HW_EXIT_CODE, otherwise None (nothing to say)."""
    if exit_code != INFRA_HW_EXIT_CODE:
        return None
    return LogIndex(failure_class="infra_hw", rule=f"exit-code-{INFRA_HW_EXIT_CODE}")


def classify(lines: list[str]) -> tuple[Rule, int] | None:
    """The first rule in RULES that matches any line, and the (1-based)
    number of the first line it matches."""
    clean = [strip_ansi(line) for line in lines]
    for rule in RULES:
        for number, line in enumerate(clean, start=1):
            if rule.pattern.search(line):
                return rule, number
    return None


def index(lines: list[str]) -> LogIndex:
    result = LogIndex(line_count=len(lines))
    errors: list[int] = []
    clean = [strip_ansi(line) for line in lines]
    for number, line in enumerate(clean, start=1):
        if ERROR_LINE.search(line) or any(rule.pattern.search(line) for rule in RULES):
            errors.append(number)
        elif WARNING_LINE.search(line):
            result.warning_count += 1
            if len(result.warnings) < MAX_INDEXED_LINES:
                result.warnings.append(number)
    result.error_count = len(errors)
    result.errors = errors[:MAX_INDEXED_LINES]

    rule_line = None
    if decided := classify(clean):
        rule, rule_line = decided
        result.failure_class, result.rule = rule.failure_class, rule.id
        if rule.failure_class in INFRA_CLASSES:
            errors = [rule_line]
    # Every line a rule matches is in `errors`, so rule_line is too.
    result.first_error = first_error(clean, errors, rule_line)
    return result


def first_error(lines: list[str], errors: list[int], rule_line: int | None) -> FirstError | None:
    """The first error line with a source location; failing that, the line
    that decided the class; failing that, the first error line."""
    for number in errors:
        if located := _locate(lines, number):
            return located
    number = rule_line if rule_line is not None else (errors[0] if errors else None)
    if number is None:
        return None
    message = LINKER_PREFIX.sub("", lines[number - 1].strip())
    return FirstError(line_number=number, message=_clip(message))


def _locate(lines: list[str], number: int) -> FirstError | None:
    line = lines[number - 1]
    for pattern in LOCATIONS:
        m = pattern.match(line)
        if m is None:
            continue
        groups = m.groupdict()
        message = (groups.get("message") or "").strip()
        if groups.get("test"):
            message = f"{groups['test']}: {message}" if message else groups["test"]
        if not message:
            message = _continuation(lines, number)
        if not message:
            continue
        return FirstError(
            line_number=number,
            message=_clip(message),
            file=groups["file"],
            line=int(groups["line"]) if groups.get("line") else None,
            column=int(groups["column"]) if groups.get("column") else None,
        )
    return None


def _continuation(lines: list[str], number: int) -> str:
    """The indented lines after `number` (CMake's message), joined."""
    parts = []
    for line in lines[number:]:
        if not line.startswith(" ") or not line.strip():
            break
        parts.append(line.strip())
    return " ".join(parts)


def _clip(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE_LEN else message[: MAX_MESSAGE_LEN - 1] + "…"


def strip_ansi(line: str) -> str:
    return ANSI_ESCAPE.sub("", line)

import pytest

from app.models import TestOutcome
from app.services.junit import JUnitError, parse_junit

O = TestOutcome

# Shaped like `ctest --output-junit` output from CMake 3.28.
CTEST = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Linux-c" tests="4" failures="1" disabled="1" skipped="0" hostname="" time="0" timestamp="2026-10-14T09:01:20">
  <testcase name="test_debounce" classname="test_debounce" time="0.00412" status="run">
    <system-out>test_debounce.c:88:test_debounce_rejects_bounce:PASS</system-out>
  </testcase>
  <testcase name="test_threshold" classname="test_threshold" time="0.0101" status="fail">
    <failure message="Failed">test_threshold.c:74:test_hysteresis:FAIL: Expected 1 Was 0</failure>
    <system-out>test_threshold.c:74:test_hysteresis:FAIL: Expected 1 Was 0</system-out>
  </testcase>
  <testcase name="test_moving_average" classname="test_moving_average" time="0" status="notrun">
    <skipped message="Disabled"/>
  </testcase>
  <testcase name="test_flaky" classname="test_flaky" time="0" status="disabled"/>
</testsuite>
"""


def test_ctest_output():
    cases = {c.name: c for c in parse_junit(CTEST)}
    assert {n: c.outcome for n, c in cases.items()} == {
        "test_debounce": O.passed,
        "test_threshold": O.failed,
        "test_moving_average": O.skipped,
        "test_flaky": O.skipped,
    }
    ok = cases["test_debounce"]
    assert (ok.suite, ok.classname, ok.duration_ms, ok.message) == ("Linux-c", "test_debounce", 4, None)
    failed = cases["test_threshold"]
    assert failed.duration_ms == 10
    assert failed.message == "Failed\ntest_threshold.c:74:test_hysteresis:FAIL: Expected 1 Was 0"
    assert cases["test_moving_average"].message == "Disabled"


def test_testsuites_root_with_error_and_nested_suites():
    data = b"""<testsuites>
      <testsuite name="a">
        <testcase name="t1" time="1.5"><error message="segfault"/></testcase>
        <testsuite name="a.inner"><testcase name="t2"/></testsuite>
      </testsuite>
      <testsuite name="b"><testcase name="t1" time="oops"/></testsuite>
    </testsuites>"""
    cases = [(c.suite, c.name, c.outcome, c.duration_ms) for c in parse_junit(data)]
    assert cases == [
        ("a", "t1", O.error, 1500),
        ("a.inner", "t2", O.passed, None),
        ("b", "t1", O.passed, None),
    ]


def test_long_messages_are_truncated_and_nameless_cases_dropped():
    data = f'<testsuite name="s"><testcase name="t"><failure>{"x" * 10000}</failure></testcase><testcase/></testsuite>'
    (case,) = parse_junit(data.encode())
    assert len(case.message) == 4000


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"<testsuite",
        b"<html><body/></html>",
        # Entity expansion is refused outright, not just bounded.
        b'<!DOCTYPE t [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]><testsuite name="&b;"/>',
        b'<!DOCTYPE t [<!ENTITY x SYSTEM "file:///etc/passwd">]><testsuite name="&x;"/>',
    ],
)
def test_unusable_or_hostile_xml_is_rejected(data):
    with pytest.raises(JUnitError):
        parse_junit(data)

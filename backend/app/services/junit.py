"""Parse JUnit XML into test cases.

Handles ctest's --output-junit and the common Ant/Surefire shape: a
<testsuite> root or a <testsuites> root holding several. The XML comes from a
runner, so it is parsed with defusedxml (no DTDs, no entity expansion).
"""
from dataclasses import dataclass
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as SafeET
from defusedxml import DefusedXmlException

from app.models import TestOutcome

MAX_MESSAGE_LEN = 4000
# ctest marks tests it did not run with these statuses (and a <skipped> child).
SKIPPED_STATUSES = frozenset({"notrun", "disabled"})


class JUnitError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCase:
    suite: str
    name: str
    classname: str | None
    outcome: TestOutcome
    duration_ms: int | None
    message: str | None


def _message(element: Element) -> str | None:
    parts = [element.get("message") or "", (element.text or "").strip()]
    text = "\n".join(p for p in parts if p).strip()
    return text[:MAX_MESSAGE_LEN] or None


def _duration_ms(value: str | None) -> int | None:
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN
        return None
    return round(seconds * 1000)


def _case(suite: str, case: Element) -> ParsedCase:
    outcome, detail = TestOutcome.passed, None
    for tag, tag_outcome in (
        ("error", TestOutcome.error),
        ("failure", TestOutcome.failed),
        ("skipped", TestOutcome.skipped),
    ):
        found = case.find(tag)
        if found is not None:
            outcome, detail = tag_outcome, found
            break
    else:
        if case.get("status") in SKIPPED_STATUSES:
            outcome = TestOutcome.skipped
    return ParsedCase(
        suite=suite,
        name=case.get("name", ""),
        classname=case.get("classname"),
        outcome=outcome,
        duration_ms=_duration_ms(case.get("time")),
        message=_message(detail) if detail is not None else None,
    )


def parse_junit(data: bytes) -> list[ParsedCase]:
    try:
        root = SafeET.fromstring(data)
    except (SafeET.ParseError, DefusedXmlException) as exc:
        raise JUnitError(f"not usable JUnit XML: {exc}") from None
    if root.tag not in ("testsuite", "testsuites"):
        raise JUnitError(f"unexpected root element <{root.tag}>")
    cases = []
    for suite in root.iter("testsuite"):
        name = suite.get("name", "")
        cases.extend(_case(name, case) for case in suite.findall("testcase") if case.get("name"))
    return cases

"""services.outcome.decide: the final-status rule, without a database."""
import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.models import BuildStatus, StageStatus
from app.services.outcome import StageFact, decide

P, F, SK = StageStatus.passed, StageStatus.failed, StageStatus.skipped


def stages(*specs):
    return [StageFact(name, required, status) for name, required, status in specs]


ALL_PASSED = stages(("configure", True, P), ("compile", True, P), ("test", True, P))


def test_success_with_every_required_stage_passed_is_passed():
    assert decide("success", ALL_PASSED) == (
        BuildStatus.passed,
        "workflow run succeeded and every required stage passed",
    )


def test_a_failed_required_stage_fails_even_a_successful_run():
    # e.g. a step with continue-on-error: GitHub says success, the stage says otherwise.
    status, reason = decide("success", stages(("compile", True, P), ("test", True, F)))
    assert status is BuildStatus.failed
    assert reason == "required stages did not pass: test (failed)"


def test_a_skipped_required_stage_is_not_a_pass():
    status, reason = decide(
        "failure", stages(("configure", True, P), ("compile", True, F), ("test", True, SK))
    )
    assert status is BuildStatus.failed
    assert reason == "required stages did not pass: compile (failed), test (skipped)"


def test_optional_stages_do_not_decide():
    status, _ = decide("success", [*ALL_PASSED, StageFact("lint", False, F), StageFact("x", False, SK)])
    assert status is BuildStatus.passed


def test_failure_outside_the_stages_is_failed():
    assert decide("failure", ALL_PASSED) == (
        BuildStatus.failed,
        "workflow run failed outside the required stages",
    )


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "startup_failure", "neutral", ""])
def test_other_conclusions_are_not_decided_here(conclusion):
    with pytest.raises(ValueError):
        decide(conclusion, ALL_PASSED)


@given(
    conclusion=st.sampled_from(["success", "failure"]),
    facts=st.lists(
        st.builds(StageFact, st.text(min_size=1, max_size=8), st.booleans(), st.sampled_from(list(StageStatus))),
        max_size=8,
    ),
)
def test_passed_exactly_when_success_and_all_required_passed(conclusion, facts):
    status, _ = decide(conclusion, facts)
    expected = conclusion == "success" and all(f.status is P for f in facts if f.required)
    assert (status is BuildStatus.passed) == expected
    assert status in (BuildStatus.passed, BuildStatus.failed)

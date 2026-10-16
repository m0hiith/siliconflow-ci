"""Database constraints reject invalid and duplicate rows."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models import Artifact, Build, BuildStage, BuildTrigger, WebhookEvent
from tests.factories import make_build, make_project

SHA_A = "a" * 64
T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


async def _expect_violation(session, constraint: str, exc_type=IntegrityError):
    with pytest.raises(exc_type) as info:
        await session.flush()
    assert constraint in str(info.value)
    await session.rollback()


async def test_duplicate_build_number_in_project_rejected(session):
    project = await make_project(session)
    build = await make_build(session, project)
    await session.commit()

    session.add(
        Build(
            project_id=project.id,
            repository_id=build.repository_id,
            number=build.number,
            trigger=BuildTrigger.push,
            profile="host-cmake",
            ref="refs/heads/main",
        )
    )
    await _expect_violation(session, "uq_builds_project_id_number")


async def test_same_number_allowed_in_different_projects(session):
    a = await make_project(session)
    b = await make_project(session)
    ba = await make_build(session, a)
    bb = await make_build(session, b)
    await session.commit()
    assert ba.number == bb.number == 1


async def test_duplicate_run_id_and_attempt_rejected(session):
    project = await make_project(session)
    await make_build(session, project, gh_run_id=111, gh_run_attempt=1)
    await session.commit()

    await make_build(session, project, gh_run_id=111, gh_run_attempt=2)  # re-run: allowed
    await session.commit()

    with pytest.raises(IntegrityError) as info:
        await make_build(session, project, gh_run_id=111, gh_run_attempt=1)
    assert "uq_builds_gh_run_id_gh_run_attempt" in str(info.value)
    await session.rollback()


async def test_builds_without_run_id_do_not_collide(session):
    project = await make_project(session)
    await make_build(session, project)
    await make_build(session, project)
    await session.commit()  # NULL run ids are distinct


async def test_run_id_and_attempt_must_be_set_together(session):
    project = await make_project(session)
    with pytest.raises(IntegrityError) as info:
        await make_build(session, project, gh_run_id=5)
    assert "ck_builds_gh_run_pair" in str(info.value)


async def test_build_finished_before_started_rejected(session):
    project = await make_project(session)
    with pytest.raises(IntegrityError) as info:
        await make_build(session, project, started_at=T0, finished_at=T0 - timedelta(seconds=1))
    assert "ck_builds_finished_after_started" in str(info.value)


async def test_build_finished_equal_to_started_allowed(session):
    project = await make_project(session)
    await make_build(session, project, started_at=T0, finished_at=T0)
    await session.commit()


async def test_stage_finished_before_started_rejected(session):
    project = await make_project(session)
    build = await make_build(session, project)
    session.add(
        BuildStage(
            build_id=build.id,
            name="compile",
            position=1,
            started_at=T0,
            finished_at=T0 - timedelta(milliseconds=1),
        )
    )
    await _expect_violation(session, "ck_build_stages_finished_after_started")


async def test_duplicate_artifact_kind_and_sha_rejected(session):
    project = await make_project(session)
    build = await make_build(session, project)
    session.add(Artifact(build_id=build.id, kind="elf", file_name="fw.elf", size_bytes=10, sha256=SHA_A))
    await session.commit()

    # Same content as a different kind is fine.
    session.add(Artifact(build_id=build.id, kind="bin", file_name="fw.bin", size_bytes=10, sha256=SHA_A))
    await session.commit()

    session.add(Artifact(build_id=build.id, kind="elf", file_name="copy.elf", size_bytes=10, sha256=SHA_A))
    await _expect_violation(session, "uq_artifacts_build_id_kind_sha256")


async def test_artifact_sha256_must_be_lowercase_hex(session):
    project = await make_project(session)
    build = await make_build(session, project)
    session.add(Artifact(build_id=build.id, kind="elf", file_name="fw.elf", size_bytes=1, sha256="A" * 64))
    await _expect_violation(session, "ck_artifacts_sha256_format")


async def test_commit_sha_must_be_40_lowercase_hex(session):
    project = await make_project(session)
    with pytest.raises(IntegrityError) as info:
        await make_build(session, project, commit_sha="abc123")
    assert "ck_builds_commit_sha_format" in str(info.value)


async def test_duplicate_webhook_delivery_rejected(session):
    event = {"delivery_id": "d-1", "event": "workflow_run", "payload": {}}
    await session.execute(insert(WebhookEvent).values(**event))
    await session.commit()
    with pytest.raises(IntegrityError) as info:
        await session.execute(insert(WebhookEvent).values(**event))
    assert "pk_webhook_events" in str(info.value)
    await session.rollback()


async def test_webhook_delivery_on_conflict_do_nothing(session):
    """The dedupe pattern webhook ingestion will use."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(WebhookEvent)
        .values(delivery_id="d-2", event="workflow_run", payload={})
        .on_conflict_do_nothing(index_elements=["delivery_id"])
        .returning(WebhookEvent.delivery_id)
    )
    assert (await session.execute(stmt)).scalar_one_or_none() == "d-2"
    assert (await session.execute(stmt)).scalar_one_or_none() is None


async def test_unknown_build_status_rejected_by_database(session):
    project = await make_project(session)
    build = await make_build(session, project)
    await session.commit()
    from sqlalchemy import text

    with pytest.raises(DBAPIError) as info:
        await session.execute(
            text("UPDATE builds SET status = 'exploded' WHERE id = :id"), {"id": build.id}
        )
    assert "build_status" in str(info.value)

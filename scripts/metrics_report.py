#!/usr/bin/env python3
"""Print SiliconFlow's measured results, read from its database and nothing else.

Every figure is a SQL aggregate over recorded rows, read in one read-only,
repeatable-read transaction so the sections agree with each other. Nothing is
estimated or filled in: a section with no rows says so.

The database URL comes from SFCI_DATABASE_URL (the backend's own variable, e.g.
postgresql+asyncpg://user:pass@host:5432/sfci). It is never printed. Against
the Compose stack:

    docker compose exec -T backend python - < scripts/metrics_report.py

Needs only asyncpg, which the backend image already has.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg

# Tables the report reads; all exist at Alembic head.
TABLES = (
    "builds",
    "build_stages",
    "test_cases",
    "artifacts",
    "run_reports",
    "linked_reports",
    "build_sizes",
    "gate_results",
    "webhook_events",
)

VERDICT = "('passed', 'failed')"
NOT_PASSED = "('failed', 'timed_out', 'errored')"
DURATION = "extract(epoch from finished_at - started_at)::float8"

QUERIES: dict[str, str] = {
    "window": "SELECT count(*) AS builds, min(created_at) AS first, max(created_at) AS last FROM builds",
    "by_status": "SELECT status::text AS status, count(*) AS n FROM builds GROUP BY 1 ORDER BY 2 DESC, 1",
    "by_profile_status": """
        SELECT profile, status::text AS status, count(*) AS n FROM builds
        GROUP BY 1, 2 ORDER BY 1, 3 DESC, 2""",
    "by_trigger": "SELECT trigger::text AS trigger, count(*) AS n FROM builds GROUP BY 1 ORDER BY 2 DESC, 1",
    "pass_rate": f"""
        SELECT profile,
               count(*) FILTER (WHERE status = 'passed') AS passed,
               count(*) AS verdicts
        FROM builds WHERE status IN {VERDICT}
        GROUP BY ROLLUP (profile) ORDER BY profile NULLS LAST""",
    "durations": f"""
        SELECT profile, count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY {DURATION}) AS p50,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY {DURATION}) AS p90,
               max({DURATION}) AS max
        FROM builds
        WHERE status IN {VERDICT} AND started_at IS NOT NULL AND finished_at IS NOT NULL
        GROUP BY 1 ORDER BY 1""",
    "failure_classes": f"""
        SELECT status::text AS status, coalesce(failure_class, '(unclassified)') AS class, count(*) AS n
        FROM builds WHERE status IN {NOT_PASSED}
        GROUP BY 1, 2 ORDER BY 1, 3 DESC, 2""",
    "webhooks": """
        SELECT event, count(*) AS n,
               count(*) FILTER (WHERE processed_at IS NOT NULL) AS processed,
               count(*) FILTER (WHERE processed_at IS NULL AND last_error IS NOT NULL) AS erroring
        FROM webhook_events GROUP BY 1 ORDER BY 2 DESC, 1""",
    "run_reports": "SELECT artifacts_state AS state, count(*) AS n FROM run_reports GROUP BY 1 ORDER BY 2 DESC, 1",
    "linked_reports": "SELECT profile, count(*) AS n FROM linked_reports GROUP BY 1 ORDER BY 1",
    "artifacts": """
        SELECT count(*) AS rows,
               count(DISTINCT sha256) AS distinct_sha256,
               count(*) FILTER (WHERE storage_key IS NOT NULL) AS stored,
               coalesce(sum(size_bytes), 0) AS bytes,
               count(*) FILTER (WHERE release_eligible) AS release_eligible,
               count(DISTINCT build_id) FILTER (WHERE release_eligible) AS eligible_builds
        FROM artifacts""",
    "gates": """
        SELECT rule_id,
               count(*) FILTER (WHERE passed) AS passed,
               count(*) FILTER (WHERE NOT passed) AS failed
        FROM gate_results GROUP BY 1 ORDER BY 1""",
    "tests": """
        SELECT outcome::text AS outcome, count(*) AS n,
               count(DISTINCT (suite, name)) AS distinct_tests
        FROM test_cases GROUP BY 1 ORDER BY 2 DESC, 1""",
    "sizes": """
        SELECT b.profile, count(*) AS n,
               min(s.text_bytes) AS text_min, max(s.text_bytes) AS text_max,
               (array_agg(s.text_bytes ORDER BY b.created_at DESC, b.id DESC))[1] AS text_latest,
               (array_agg(s.data_bytes ORDER BY b.created_at DESC, b.id DESC))[1] AS data_latest,
               (array_agg(s.bss_bytes ORDER BY b.created_at DESC, b.id DESC))[1] AS bss_latest
        FROM build_sizes s JOIN builds b ON b.id = s.build_id
        GROUP BY 1 ORDER BY 1""",
}


def asyncpg_dsn(url: str) -> str:
    """postgresql+asyncpg://... (SQLAlchemy) -> postgresql://... (asyncpg)."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme=parts.scheme.split("+", 1)[0]))


async def collect(conn: asyncpg.Connection) -> dict[str, Any]:
    """Run every query in one read-only snapshot."""
    data: dict[str, Any] = {"generated_at": datetime.now(timezone.utc)}
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        data["database"] = await conn.fetchval("SELECT current_database()")
        present = {
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }
        data["revision"] = (
            await conn.fetchval("SELECT version_num FROM alembic_version")
            if "alembic_version" in present
            else None
        )
        data["missing_tables"] = [t for t in TABLES if t not in present]
        if data["missing_tables"]:
            return data
        for key, sql in QUERIES.items():
            data[key] = [dict(r) for r in await conn.fetch(sql)]
    return data


# ------------------------------------------------------------------ rendering


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> list[str]:
    """Fixed-width text table; columns are (key, header)."""
    if not rows:
        return ["  no rows"]
    cells = [[fmt(r[k]) for k, _ in columns] for r in rows]
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, (_, h) in enumerate(columns)]
    line = lambda vals: "  " + "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
    return [line([h for _, h in columns]), line(["-" * w for w in widths])] + [line(c) for c in cells]


def fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.1f}"
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return str(v)


def pct(num: int, den: int) -> str:
    return f"{100 * num / den:.1f}% ({num}/{den})" if den else "-"


def render(data: dict[str, Any]) -> str:
    out = [
        "SiliconFlow CI - measured results",
        f"database: {data['database']}  alembic revision: {data['revision'] or '-'}",
        f"generated: {fmt(data['generated_at'])}",
        "",
    ]
    if data["missing_tables"]:
        out.append(
            "Schema is not at head; missing tables: " + ", ".join(data["missing_tables"])
        )
        out.append("Apply migrations first: docker compose run --rm migrate")
        return "\n".join(out)

    w = data["window"][0]
    if not w["builds"]:
        out.append("No builds recorded. Every figure below needs at least one build.")
        return "\n".join(out)
    out.append(f"Builds recorded: {w['builds']}  ({fmt(w['first'])} to {fmt(w['last'])})")

    def section(title: str, lines: list[str]) -> None:
        out.extend(["", title, *lines])

    section("Builds by status", table(data["by_status"], [("status", "status"), ("n", "builds")]))
    section("Builds by trigger", table(data["by_trigger"], [("trigger", "trigger"), ("n", "builds")]))
    section(
        "Builds by profile and status",
        table(data["by_profile_status"], [("profile", "profile"), ("status", "status"), ("n", "builds")]),
    )
    section(
        "Pass rate (passed / (passed + failed); cancelled, timed_out, errored excluded)",
        table(
            [
                {"profile": r["profile"] or "all profiles", "rate": pct(r["passed"], r["verdicts"])}
                for r in data["pass_rate"]
            ],
            [("profile", "profile"), ("rate", "pass rate")],
        ),
    )
    section(
        "Build duration, started to finished, seconds (passed and failed builds)",
        table(
            data["durations"],
            [("profile", "profile"), ("n", "builds"), ("p50", "median"), ("p90", "p90"), ("max", "max")],
        ),
    )
    section(
        "Failure classes of builds that did not pass (from server-side log classification)",
        table(data["failure_classes"], [("status", "status"), ("class", "class"), ("n", "builds")]),
    )
    section(
        "GitHub webhook deliveries (one row per X-GitHub-Delivery)",
        table(
            data["webhooks"],
            [("event", "event"), ("n", "deliveries"), ("processed", "processed"), ("erroring", "erroring")],
        ),
    )
    section(
        "Run reports accepted (OIDC-authenticated), by artifact ingestion state",
        table(data["run_reports"], [("state", "artifacts_state"), ("n", "reports")]),
    )
    section(
        "Linked (hardware-in-the-loop) reports",
        table(data["linked_reports"], [("profile", "profile"), ("n", "reports")]),
    )
    a = data["artifacts"][0]
    section(
        "Artifacts (re-hashed on the server, stored by SHA-256)",
        [
            f"  artifact rows: {a['rows']}",
            f"  distinct SHA-256: {a['distinct_sha256']}",
            f"  stored in content-addressed store: {a['stored']}",
            f"  total bytes: {a['bytes']}",
            f"  release-eligible artifacts: {a['release_eligible']} "
            f"(from {a['eligible_builds']} builds)",
        ],
    )
    section(
        "Release gate results (one row per build and rule)",
        table(data["gates"], [("rule_id", "rule"), ("passed", "passed"), ("failed", "failed")]),
    )
    section(
        "Test cases (from server-verified JUnit)",
        table(
            data["tests"],
            [("outcome", "outcome"), ("n", "results"), ("distinct_tests", "distinct tests")],
        ),
    )
    section(
        "Firmware size, bytes (as reported by the size stage; latest = newest build)",
        table(
            data["sizes"],
            [
                ("profile", "profile"),
                ("n", "builds"),
                ("text_min", "text min"),
                ("text_max", "text max"),
                ("text_latest", "text latest"),
                ("data_latest", "data latest"),
                ("bss_latest", "bss latest"),
            ],
        ),
    )
    return "\n".join(out)


async def main() -> int:
    url = os.environ.get("SFCI_DATABASE_URL")
    if not url:
        print("SFCI_DATABASE_URL is not set", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(asyncpg_dsn(url))
    try:
        data = await collect(conn)
    finally:
        await conn.close()
    print(render(data))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

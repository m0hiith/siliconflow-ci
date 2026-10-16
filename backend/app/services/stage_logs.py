"""Stage logs: cap, redact, index, gzip.

ci/stage.sh writes each stage's combined output to out/logs/<stage>.log, so
logs reach the backend inside the run's artifact zip and are picked up by the
ingest job (services.artifacts). The run report does not hash them: like
stage statuses they are the runner's account of what happened, kept as
evidence and classified, never trusted for gating.

Each log is

  1. capped at settings.stage_log_max_bytes: the head and the tail are kept,
     cut at line ends, with a marker line between them;
  2. redacted: anything that looks like a GitHub token or a JWT is replaced
     before the log is stored or indexed;
  3. indexed and classified (services.classify);
  4. gzipped and stored content-addressed, like artifacts.

The build's failure_class is the class of its first failed required stage
(by position) whose log was classified. It describes why the build failed;
it never changes the build's status.
"""
import gzip
import re
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Build, BuildStage, StageStatus
from app.services import classify

LOGS_PREFIX = "logs/"
LOG_SUFFIX = ".log"
STAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")  # as in schemas.ReportStage
CHUNK = 1 << 16

# GitHub token formats (ghp_ personal, gho_ OAuth, ghu_ user-to-server,
# ghs_ installation/Actions, ghr_ refresh, github_pat_ fine-grained) and
# JWTs, whose header and payload are base64url JSON and so start with "eyJ".
# The lookbehind treats "_" as a boundary too ("GITHUB_TOKEN_ghs_...").
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
        "[REDACTED:github-token]",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"),
        "[REDACTED:jwt]",
    ),
)


@dataclass(frozen=True)
class PreparedLog:
    gz: bytes  # what is stored
    index: classify.LogIndex


def stage_of(entry: str) -> str | None:
    """The stage a zip entry is the log of, or None: logs/<stage>.log only."""
    if not (entry.startswith(LOGS_PREFIX) and entry.endswith(LOG_SUFFIX)):
        return None
    name = entry[len(LOGS_PREFIX) : -len(LOG_SUFFIX)]
    return name if STAGE_NAME_RE.match(name) else None


def read_capped(src: IO[bytes], limit: int) -> tuple[bytes, int]:
    """Read a stream keeping at most about `limit` bytes: the first and last
    halves, cut at line ends. Returns (data, bytes omitted)."""
    buf = bytearray()
    while len(buf) <= limit and (chunk := src.read(CHUNK)):
        buf += chunk
    if len(buf) <= limit:
        return bytes(buf), 0

    # Too long: keep the head, and a rolling tail of the rest.
    half = max(limit // 2, 1)
    head, rest = buf[:half], buf[half:]
    total = len(buf)
    while chunk := src.read(CHUNK):
        total += len(chunk)
        rest += chunk
        if len(rest) > 2 * half:
            del rest[: len(rest) - half]
    rest = rest[-half:]

    # Cut at line ends, so no line (or token) is kept only in part.
    cut = head.rfind(b"\n")
    head = head[: cut + 1] if cut >= 0 else bytearray()
    start = rest.find(b"\n")
    rest = rest[start + 1 :] if start >= 0 else bytearray()
    omitted = total - len(head) - len(rest)
    marker = f"[siliconflow: {omitted} bytes omitted; log over {limit} bytes]\n".encode()
    return bytes(head + marker + rest), omitted


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def split_lines(text: str) -> list[str]:
    """Lines as the index numbers them: split on \\n (a \\r\\n ending counts as
    one), no empty last line for a trailing newline."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def prepare(src: IO[bytes], limit: int) -> PreparedLog:
    """Cap, redact, index and gzip a log. Blocking."""
    data, _ = read_capped(src, limit)
    text = redact(data.decode("utf-8", errors="replace"))
    index = classify.index(split_lines(text))
    # mtime=0 makes identical logs compress to identical bytes, so they are stored once.
    gz = gzip.compress(text.encode("utf-8"), compresslevel=6, mtime=0)
    return PreparedLog(gz=gz, index=index)


def read_lines(path: Path) -> list[str]:
    """A stored log's lines. Blocking."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return split_lines(f.read())


async def record(
    session: AsyncSession, build_id: str, logs: dict[str, tuple[str, classify.LogIndex]]
) -> str | None:
    """Point the build's stages at their stored logs and set the build's
    failure_class. Returns the class (None when nothing was classified)."""
    for name, (key, idx) in logs.items():
        await session.execute(
            update(BuildStage)
            .where(BuildStage.build_id == build_id, BuildStage.name == name)
            .values(log_key=key, log_index=idx.to_json())
        )
    failed = await session.scalars(
        select(BuildStage.name)
        .where(
            BuildStage.build_id == build_id,
            BuildStage.required,
            BuildStage.status == StageStatus.failed,
        )
        .order_by(BuildStage.position)
    )
    failure_class = next(
        (logs[n][1].failure_class for n in failed if n in logs and logs[n][1].failure_class), None
    )
    if failure_class is not None:
        await session.execute(
            update(Build).where(Build.id == build_id).values(failure_class=failure_class)
        )
    return failure_class

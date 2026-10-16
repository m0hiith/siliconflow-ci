#!/usr/bin/env python3
"""Fetch a build's firmware image from SiliconFlow and verify its SHA-256.

    python3 ci/hil/fetch.py --run-id <id> --run-attempt <n> --dest <dir>
    python3 ci/hil/fetch.py --build-id bld_... --dest <dir>

Finds the build of the given workflow run attempt (or takes --build-id), waits
until SiliconFlow has settled it and stored its artifacts, and checks that it
passed, is a --profile build (default siwx917-soc) and ran on a release ref
(refs/heads/main or a refs/tags/v* tag). It then downloads --file (default
siwx917_soc.rps) and hashes it. The image is moved to <dest>/<file> only if
its SHA-256 and size match the build's artifact listing and the
X-Checksum-SHA256 header. A mismatch fails before anything else happens.

<out>/hil/build.json records the build (id, number, commit, ref) as soon as it
is known, so the run report can name the build even if a later check fails,
and gets the verified image added at the end. <out> is $SFCI_OUT_DIR (default
"out"). The API is $SFCI_API_URL.

Exit codes: 0 fetched and verified; 1 failed (build not usable, artifact
missing, SHA-256 mismatch, SiliconFlow not answering in time); 2 usage.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_PROFILE = "siwx917-soc"
DEFAULT_FILE = "siwx917_soc.rps"
RELEASE_REFS = ("refs/heads/main", "refs/tags/v*")
TERMINAL = {"passed", "failed", "cancelled", "timed_out", "errored"}
BUILD_ID_RE = re.compile(r"^bld_[0-9A-Za-z]{1,28}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
CHUNK = 1 << 16


class FetchError(Exception):
    pass


class Api:
    def __init__(self, base_url: str, timeout_s: float = 30.0) -> None:
        self.base = base_url.rstrip("/") + "/api/v1"
        self.timeout = timeout_s

    def _open(self, path: str, params: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": "sfci-hil"})
        return urllib.request.urlopen(request, timeout=self.timeout)

    def get_json(self, path: str, params: dict | None = None):
        """The decoded body, or None for 404. Raises FetchError on anything
        else, including connection failures, so callers can poll."""
        try:
            with self._open(path, params) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise FetchError(f"GET {path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise FetchError(f"GET {path}: {exc}") from None

    def download(self, path: str, dst: Path, limit: int) -> tuple[int, str, str | None]:
        """Stream at most limit+1 bytes to dst. Returns (size, sha256, X-Checksum-SHA256)."""
        h = hashlib.sha256()
        size = 0
        try:
            with self._open(path) as response, dst.open("wb") as out:
                header = response.headers.get("X-Checksum-SHA256")
                while size <= limit and (chunk := response.read(min(CHUNK, limit + 1 - size))):
                    h.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
        except urllib.error.HTTPError as exc:
            raise FetchError(f"GET {path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise FetchError(f"GET {path}: {exc}") from None
        return size, h.hexdigest(), header


def poll(what: str, attempt, wait_s: float, interval_s: float):
    """Call attempt() until it returns something other than None, retrying
    FetchError, for at most wait_s."""
    deadline = time.monotonic() + wait_s
    last_error = None
    while True:
        try:
            if (result := attempt()) is not None:
                return result
        except FetchError as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            detail = f" (last error: {last_error})" if last_error else ""
            raise FetchError(f"gave up after {wait_s:g}s waiting for {what}{detail}")
        time.sleep(interval_s)


def resolve_build(api: Api, run_id: int, run_attempt: int, wait_s: float, interval_s: float) -> str:
    def attempt():
        page = api.get_json("/builds", {"gh_run_id": run_id, "gh_run_attempt": run_attempt, "limit": 2})
        items = (page or {}).get("items") or []
        if len(items) > 1:
            raise FetchError(f"more than one build for run {run_id} attempt {run_attempt}")
        return items[0]["id"] if items else None

    return poll(f"the build of run {run_id} attempt {run_attempt}", attempt, wait_s, interval_s)


def settled(build: dict) -> bool:
    """Finished, and if it passed, done with its artifacts."""
    if build.get("status") not in TERMINAL:
        return False
    return build["status"] != "passed" or build.get("artifacts_state") not in (None, "pending")


def check_build(build: dict, profile: str) -> None:
    problems = []
    if build.get("status") != "passed":
        problems.append(f"status is {build.get('status')}, not passed")
    if build.get("profile") != profile:
        problems.append(f"profile is {build.get('profile')}, not {profile}")
    ref = build.get("ref") or ""
    if not any(fnmatch.fnmatchcase(ref, pattern) for pattern in RELEASE_REFS):
        problems.append(f"ref {ref} is not one of {', '.join(RELEASE_REFS)}")
    if build.get("artifacts_state") != "stored":
        problems.append(f"artifacts are {build.get('artifacts_state')}, not stored")
    if not SHA1_RE.fullmatch(build.get("commit_sha") or ""):
        problems.append("commit is unknown")
    if problems:
        raise FetchError(f"build {build.get('id')} cannot be tested: " + "; ".join(problems))


def fetch_image(api: Api, build_id: str, file_name: str, dest: Path) -> dict:
    artifacts = api.get_json(f"/builds/{build_id}/artifacts")
    matches = [a for a in artifacts or [] if a.get("file_name") == file_name]
    if len(matches) != 1:
        names = ", ".join(sorted(a.get("file_name", "?") for a in artifacts or [])) or "none"
        raise FetchError(f"build {build_id} has no single artifact {file_name} (artifacts: {names})")
    artifact = matches[0]
    expected_sha, expected_size = artifact.get("sha256"), artifact.get("size_bytes")
    if not SHA256_RE.fullmatch(expected_sha or "") or not isinstance(expected_size, int):
        raise FetchError(f"artifact {artifact.get('id')} has no usable sha256 and size")

    part = dest / f".{file_name}.part"
    try:
        size, sha, header = api.download(f"/artifacts/{artifact['id']}/download", part, expected_size)
        if (size, sha) != (expected_size, expected_sha) or header != expected_sha:
            raise FetchError(
                f"SHA-256 mismatch for {file_name}: listed {expected_size} bytes {expected_sha}, "
                f"header {header}, downloaded {size}{'+' if size > expected_size else ''} bytes {sha}"
            )
        final = dest / file_name
        os.replace(part, final)
    finally:
        part.unlink(missing_ok=True)
    print(f"fetch.py: {file_name}: {size} bytes, sha256 {sha} verified")
    return {"file": file_name, "path": str(final), "sha256": sha, "size": size, "artifact_id": artifact["id"]}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--build-id")
    which.add_argument("--run-id", type=int, help="the firmware workflow run (with --run-attempt)")
    p.add_argument("--run-attempt", type=int, default=1)
    p.add_argument("--dest", type=Path, required=True, help="directory for the verified image")
    p.add_argument("--file", default=DEFAULT_FILE, help="artifact file name to fetch")
    p.add_argument("--profile", default=DEFAULT_PROFILE, help="the build's expected profile")
    p.add_argument("--out", type=Path, default=Path(os.environ.get("SFCI_OUT_DIR", "out")))
    p.add_argument("--api-url", default=os.environ.get("SFCI_API_URL"))
    p.add_argument("--wait", type=float, default=600.0, help="seconds to wait for SiliconFlow")
    p.add_argument("--interval", type=float, default=10.0, help="seconds between polls")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.api_url:
        print("fetch.py: set SFCI_API_URL (or pass --api-url)", file=sys.stderr)
        return 2
    if args.build_id is not None and not BUILD_ID_RE.fullmatch(args.build_id):
        print(f"fetch.py: not a build id: {args.build_id!r}", file=sys.stderr)
        return 2
    if not FILE_RE.fullmatch(args.file):
        print(f"fetch.py: not a plain file name: {args.file!r}", file=sys.stderr)
        return 2

    # Never leave an older image where the flash step will look for it.
    args.dest.mkdir(parents=True, exist_ok=True)
    (args.dest / args.file).unlink(missing_ok=True)
    build_json = args.out / "hil" / "build.json"
    build_json.unlink(missing_ok=True)

    api = Api(args.api_url)
    try:
        build_id = args.build_id or resolve_build(
            api, args.run_id, args.run_attempt, args.wait, args.interval
        )
        print(f"fetch.py: build {build_id}")
        write_json(build_json, {"build_id": build_id})

        def attempt():
            build = api.get_json(f"/builds/{build_id}")
            if build is None:
                raise FetchError(f"build {build_id} not found")
            return build if settled(build) else None

        build = poll(f"build {build_id} to settle", attempt, args.wait, args.interval)
        record = {
            "build_id": build_id,
            "number": build.get("number"),
            "profile": build.get("profile"),
            "ref": build.get("ref"),
            "commit_sha": build.get("commit_sha"),
        }
        write_json(build_json, record)
        check_build(build, args.profile)
        image = fetch_image(api, build_id, args.file, args.dest)
        write_json(build_json, {**record, "image": image})
    except FetchError as exc:
        print(f"fetch.py: {exc}", file=sys.stderr)
        return 1
    print(f"fetch.py: build #{build.get('number')} commit {build['commit_sha']} ready to flash")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

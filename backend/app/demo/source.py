"""The worker's RepoSource in demo mode: recorded fixtures instead of GitHub.

Ingestion and gate evaluation call it exactly as they call GitHubApp. It
answers from the demo directory: a run's Actions artifact is the scenario
build's zip (demo/fixtures/artifacts), and gates.yml at a commit is the file
the scenario names for that commit (demo/fixtures/gates). Everything after
that (re-hashing, the manifest check, content-addressed storage, JUnit
parsing, log classification, gate evaluation) is the production code.
"""
import asyncio
import shutil
from pathlib import Path
from typing import Any

from app.config import Settings
from app.demo.scenarios import DEMO_REPOSITORY, DemoFixtures
from app.services.gates import GATES_PATH
from app.services.github import GitHubError

DEMO_INSTALLATION_ID = 1


class DemoRepoSource:
    def __init__(self, settings: Settings, fixtures: DemoFixtures | None = None) -> None:
        if not settings.demo_mode:
            raise RuntimeError("the demo source only runs in demo mode")
        self._fixtures = fixtures or DemoFixtures(settings.demo_dir)
        builds = self._fixtures.builds()
        self._zips = {(b.run_id, b.artifact_name): self._fixtures.zip_path(b.zip_name) for b in builds}
        # Artifact ids, as GitHub would give each run attempt's artifact its own.
        self._ids = {key: i for i, key in enumerate(sorted(self._zips), start=1)}
        self._gates = {b.sha: self._fixtures.gates_path(b.gates) for b in builds}

    @property
    def configured(self) -> bool:
        # There is no GitHub to reconcile against, so the reconciler stays off.
        return False

    async def aclose(self) -> None:
        pass

    def _check_repo(self, full_name: str) -> None:
        if full_name != DEMO_REPOSITORY:
            raise GitHubError(f"demo source: unknown repository {full_name!r}", status=404)

    async def repository_installation_id(self, full_name: str) -> int:
        self._check_repo(full_name)
        return DEMO_INSTALLATION_ID

    async def find_run_artifact(
        self, installation_id: int, full_name: str, run_id: int, name: str
    ) -> dict[str, Any] | None:
        self._check_repo(full_name)
        path = self._zips.get((run_id, name))
        if path is None or not path.is_file():
            return None
        return {
            "id": self._ids[(run_id, name)],
            "name": name,
            "expired": False,
            "size_in_bytes": path.stat().st_size,
            "workflow_run": {"id": run_id},
        }

    async def download_artifact(
        self, installation_id: int, full_name: str, artifact_id: int, dest: Path, max_bytes: int
    ) -> int:
        self._check_repo(full_name)
        key = next((k for k, i in self._ids.items() if i == artifact_id), None)
        if key is None:
            raise GitHubError(f"demo source: no artifact {artifact_id}", status=404)
        path = self._zips[key]
        size = path.stat().st_size
        if size > max_bytes:
            raise GitHubError(f"artifact download: larger than {max_bytes} bytes")
        await asyncio.to_thread(shutil.copyfile, path, dest)
        return size

    async def get_file(
        self, installation_id: int, full_name: str, path: str, ref: str
    ) -> bytes | None:
        self._check_repo(full_name)
        if path != GATES_PATH:
            return None
        gates = self._gates.get(ref)
        if gates is None or not gates.is_file():
            return None
        return gates.read_bytes()

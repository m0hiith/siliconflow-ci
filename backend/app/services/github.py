"""GitHub App client: the backend's only way to call the GitHub API.

Authentication follows GitHub's App flow, with no personal access tokens:
  1. sign a short-lived App JWT (RS256) with the App's private key;
  2. exchange it for an installation access token for the repository's
     installation (valid for one hour);
  3. call the API with the installation token.

Installation tokens are cached per installation and refreshed shortly before
they expire. Tokens are never logged; error messages carry GitHub's status and
message only.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from app.config import Settings

log = logging.getLogger(__name__)

API_VERSION = "2022-11-28"
# GitHub rejects App JWTs that live longer than 10 minutes. iat is backdated
# to tolerate clock drift between us and GitHub.
JWT_LIFETIME = timedelta(minutes=9)
JWT_BACKDATE = timedelta(seconds=60)
# Refresh an installation token when less than this much of its life is left,
# so a token never expires mid-request.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
AHEAD_CACHE_SIZE = 4096


class GitHubError(Exception):
    """A GitHub API call failed. `status` is None when no response arrived."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubNotConfigured(GitHubError):
    pass


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: datetime

    def __repr__(self) -> str:  # keep the token out of reprs and tracebacks
        return f"InstallationToken(expires_at={self.expires_at.isoformat()})"


def _error_from(response: httpx.Response, what: str) -> GitHubError:
    try:
        detail = response.json().get("message") or ""
    except ValueError:
        detail = ""
    message = f"{what}: GitHub answered {response.status_code}"
    if detail:
        message += f" ({detail})"
    return GitHubError(message, status=response.status_code)


class GitHubApp:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._app_id = settings.github_app_id
        self._private_key = settings.github_app_private_key
        self._timeout = settings.github_api_timeout_s
        self._transport = transport
        self._http = httpx.AsyncClient(
            base_url=settings.github_api_url,
            timeout=settings.github_api_timeout_s,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "siliconflow-ci",
            },
        )
        self._tokens: dict[int, InstallationToken] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        # (repo, base, head) -> ahead_by. Two commit SHAs always compare the same.
        self._ahead: dict[tuple[str, str, str], int] = {}

    @property
    def configured(self) -> bool:
        return self._app_id is not None and self._private_key is not None

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ auth

    def app_jwt(self, now: float | None = None) -> str:
        """A fresh App JWT, used only to look up and exchange installation tokens."""
        if not self.configured:
            raise GitHubNotConfigured("GitHub App is not configured")
        now = time.time() if now is None else now
        claims = {
            "iat": int(now - JWT_BACKDATE.total_seconds()),
            "exp": int(now + JWT_LIFETIME.total_seconds()),
            "iss": self._app_id,
        }
        return jwt.encode(claims, self._private_key.get_secret_value(), algorithm="RS256")

    async def installation_token(self, installation_id: int) -> str:
        """A valid installation token, from the cache unless it is near expiry."""
        cached = self._tokens.get(installation_id)
        if cached is not None and not self._near_expiry(cached):
            return cached.token
        # One exchange per installation at a time; concurrent callers wait
        # for it and then reuse its token.
        lock = self._locks.setdefault(installation_id, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(installation_id)
            if cached is not None and not self._near_expiry(cached):
                return cached.token
            fresh = await self._exchange(installation_id)
            self._tokens[installation_id] = fresh
            log.info(
                "github installation token refreshed",
                extra={"installation_id": installation_id, "expires_at": fresh.expires_at.isoformat()},
            )
            return fresh.token

    def invalidate(self, installation_id: int) -> None:
        self._tokens.pop(installation_id, None)

    @staticmethod
    def _near_expiry(token: InstallationToken) -> bool:
        return token.expires_at - datetime.now(UTC) < TOKEN_REFRESH_MARGIN

    async def _exchange(self, installation_id: int) -> InstallationToken:
        response = await self._send(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            bearer=self.app_jwt(),
            what="installation token exchange",
        )
        if response.status_code != 201:
            raise _error_from(response, "installation token exchange")
        body = response.json()
        return InstallationToken(
            token=body["token"], expires_at=datetime.fromisoformat(body["expires_at"])
        )

    async def repository_installation_id(self, full_name: str) -> int:
        """The App's installation id for a repository (App JWT auth)."""
        response = await self._send(
            "GET", f"/repos/{full_name}/installation", bearer=self.app_jwt(), what="installation lookup"
        )
        if response.status_code != 200:
            raise _error_from(response, "installation lookup")
        return int(response.json()["id"])

    # ------------------------------------------------------- transport

    async def _send(
        self,
        method: str,
        url: str,
        *,
        bearer: str,
        what: str,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return await self._http.request(
                method, url, json=json, headers={**(headers or {}), "Authorization": f"Bearer {bearer}"}
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"{what}: {type(exc).__name__}") from exc

    async def _installation_request(
        self,
        installation_id: int,
        method: str,
        url: str,
        *,
        what: str,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        token = await self.installation_token(installation_id)
        response = await self._send(method, url, bearer=token, what=what, json=json, headers=headers)
        if response.status_code == 401:
            # Revoked or rotated early: drop it and retry once with a new token.
            self.invalidate(installation_id)
            token = await self.installation_token(installation_id)
            response = await self._send(
                method, url, bearer=token, what=what, json=json, headers=headers
            )
        return response

    # ------------------------------------------------------------- API

    async def dispatch_workflow(
        self,
        installation_id: int,
        full_name: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
    ) -> None:
        """Start a workflow_dispatch run. GitHub answers 204 with no run id;
        the run is correlated later (see services.github_events)."""
        response = await self._installation_request(
            installation_id,
            "POST",
            f"/repos/{full_name}/actions/workflows/{workflow}/dispatches",
            what="workflow dispatch",
            json={"ref": ref, "inputs": inputs},
        )
        if response.status_code not in (200, 204):
            raise _error_from(response, "workflow dispatch")

    async def get_run_attempt(
        self, installation_id: int, full_name: str, run_id: int, attempt: int
    ) -> dict[str, Any]:
        """One attempt of a workflow run, shaped like a webhook's workflow_run."""
        response = await self._installation_request(
            installation_id,
            "GET",
            f"/repos/{full_name}/actions/runs/{run_id}/attempts/{attempt}",
            what="run lookup",
        )
        if response.status_code != 200:
            raise _error_from(response, "run lookup")
        return response.json()

    async def get_file(
        self, installation_id: int, full_name: str, path: str, ref: str
    ) -> bytes | None:
        """A file's raw content at `ref` (a commit SHA), or None if it does not exist there."""
        response = await self._installation_request(
            installation_id,
            "GET",
            f"/repos/{full_name}/contents/{quote(path)}?ref={quote(ref, safe='')}",
            what="file fetch",
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _error_from(response, "file fetch")
        return response.content

    async def commits_ahead(self, installation_id: int, full_name: str, base: str, head: str) -> int:
        """How many commits `head` has that `base` does not (GitHub's ahead_by)."""
        key = (full_name, base, head)
        if (cached := self._ahead.get(key)) is not None:
            return cached
        response = await self._installation_request(
            installation_id,
            "GET",
            # per_page=1: only the counts are wanted, not the commit list.
            f"/repos/{full_name}/compare/{quote(base, safe='')}...{quote(head, safe='')}?per_page=1",
            what="compare",
        )
        if response.status_code != 200:
            raise _error_from(response, "compare")
        ahead = int(response.json()["ahead_by"])
        if len(self._ahead) >= AHEAD_CACHE_SIZE:
            self._ahead.clear()
        self._ahead[key] = ahead
        return ahead

    async def find_run_artifact(
        self, installation_id: int, full_name: str, run_id: int, name: str
    ) -> dict[str, Any] | None:
        """The run's Actions artifact called `name`, or None if it is not listed (yet)."""
        response = await self._installation_request(
            installation_id,
            "GET",
            f"/repos/{full_name}/actions/runs/{run_id}/artifacts?name={quote(name)}&per_page=100",
            what="artifact lookup",
        )
        if response.status_code != 200:
            raise _error_from(response, "artifact lookup")
        for artifact in response.json().get("artifacts", []):
            if artifact.get("name") == name and (artifact.get("workflow_run") or {}).get("id") == run_id:
                return artifact
        return None

    async def download_artifact(
        self, installation_id: int, full_name: str, artifact_id: int, dest: Path, max_bytes: int
    ) -> int:
        """Download an artifact's zip to `dest`; returns its size in bytes.

        GitHub answers with a redirect to short-lived blob storage. The
        redirect is followed by a separate client that sends no Authorization
        header, so the installation token never leaves api.github.com.
        """
        response = await self._installation_request(
            installation_id,
            "GET",
            f"/repos/{full_name}/actions/artifacts/{artifact_id}/zip",
            what="artifact download",
        )
        if response.status_code != 302 or "location" not in response.headers:
            raise _error_from(response, "artifact download")

        size = 0
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, follow_redirects=True
            ) as blob, blob.stream("GET", response.headers["location"]) as stream:
                if stream.status_code != 200:
                    raise GitHubError(
                        f"artifact download: storage answered {stream.status_code}", stream.status_code
                    )
                with dest.open("wb") as f:
                    async for chunk in stream.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise GitHubError(f"artifact download: larger than {max_bytes} bytes")
                        f.write(chunk)
        except httpx.HTTPError as exc:
            raise GitHubError(f"artifact download: {type(exc).__name__}") from exc
        return size

    async def cancel_run(self, installation_id: int, full_name: str, run_id: int) -> None:
        """Ask GitHub to cancel a run (202). The build's status changes only
        when the resulting workflow_run webhook arrives."""
        response = await self._installation_request(
            installation_id,
            "POST",
            f"/repos/{full_name}/actions/runs/{run_id}/cancel",
            what="run cancel",
        )
        if response.status_code != 202:
            raise _error_from(response, "run cancel")

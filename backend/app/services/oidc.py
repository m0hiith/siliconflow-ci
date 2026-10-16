"""Verify GitHub Actions OIDC tokens, which authenticate run reports.

A job with `permissions: id-token: write` can ask GitHub for a JWT that names
the repository, run and commit it runs for, signed by GitHub and addressed to
an audience the job picks ("siliconflow"). Checking the signature against
GitHub's published keys (JWKS) proves a report came from that run, with no
shared secret on the runner.

Keys are cached for settings.oidc_jwks_cache_s. A token signed with an unknown
key id triggers a refetch, at most once a minute, so key rotation is picked up
without letting made-up key ids make us hammer GitHub.
"""
import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx
import jwt

from app.config import Settings

log = logging.getLogger(__name__)

ALGORITHMS = ["RS256"]
LEEWAY_S = 60  # clock drift between GitHub and us
MIN_REFETCH_INTERVAL_S = 60.0
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "repository", "run_id", "run_attempt", "sha", "ref", "workflow_ref"]


class InvalidToken(Exception):
    """The token is missing, malformed, forged, expired or for someone else."""


class OidcUnavailable(Exception):
    """GitHub's keys could not be fetched; the caller should retry later."""


@dataclass(frozen=True)
class RunClaims:
    """The claims of a verified token that identify a workflow run."""

    sub: str
    repository: str
    repository_id: str | None
    run_id: int
    run_attempt: int
    sha: str
    ref: str
    workflow_ref: str
    event_name: str | None
    actor: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RunClaims":
        try:
            return cls(
                sub=str(payload.get("sub", "")),
                repository=str(payload["repository"]),
                repository_id=str(payload["repository_id"]) if "repository_id" in payload else None,
                run_id=int(payload["run_id"]),
                run_attempt=int(payload["run_attempt"]),
                sha=str(payload["sha"]),
                ref=str(payload["ref"]),
                workflow_ref=str(payload["workflow_ref"]),
                event_name=payload.get("event_name"),
                actor=payload.get("actor"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidToken(f"malformed claims: {exc}") from None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class GitHubOidcVerifier:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._issuer = settings.oidc_issuer.rstrip("/")
        self._audience = settings.oidc_audience
        self._jwks_url = f"{self._issuer}/.well-known/jwks"
        self._cache_s = settings.oidc_jwks_cache_s
        self._http = httpx.AsyncClient(
            timeout=settings.github_api_timeout_s,
            transport=transport,
            headers={"User-Agent": "siliconflow-ci"},
        )
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def verify(self, token: str) -> RunClaims:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise InvalidToken(f"malformed token: {exc}") from None
        if header.get("alg") not in ALGORITHMS:
            raise InvalidToken(f"unexpected algorithm {header.get('alg')!r}")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidToken("token has no key id")

        key = await self._key(kid)
        try:
            payload = jwt.decode(
                token,
                key.key,
                algorithms=ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=LEEWAY_S,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.InvalidTokenError as exc:
            raise InvalidToken(str(exc)) from None
        return RunClaims.from_payload(payload)

    async def _key(self, kid: str) -> jwt.PyJWK:
        if not self._stale() and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            # Another caller may have refreshed the keys while we waited.
            if kid in self._keys and not self._stale():
                return self._keys[kid]
            if self._stale() or self._may_refetch():
                await self._refresh()
        if kid not in self._keys:
            raise InvalidToken("token signed with an unknown key")
        return self._keys[kid]

    def _stale(self) -> bool:
        return self._fetched_at is None or time.monotonic() - self._fetched_at > self._cache_s

    def _may_refetch(self) -> bool:
        return self._fetched_at is None or time.monotonic() - self._fetched_at >= MIN_REFETCH_INTERVAL_S

    async def _refresh(self) -> None:
        try:
            response = await self._http.get(self._jwks_url)
            response.raise_for_status()
            key_set = jwt.PyJWKSet.from_dict(response.json())
        except (httpx.HTTPError, ValueError, jwt.PyJWKSetError) as exc:
            if self._keys:
                # Keep serving the keys we have; GitHub rotates them slowly.
                log.warning("oidc jwks refresh failed; keeping cached keys", extra={"error": type(exc).__name__})
                self._fetched_at = time.monotonic()
                return
            raise OidcUnavailable(f"could not fetch GitHub OIDC keys: {type(exc).__name__}") from exc
        self._keys = {k.key_id: k for k in key_set.keys if k.key_id}
        self._fetched_at = time.monotonic()
        log.info("oidc jwks refreshed", extra={"keys": len(self._keys)})

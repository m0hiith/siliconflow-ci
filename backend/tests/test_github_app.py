"""services.github: App JWT, installation-token caching, and API calls,
against a respx-mocked GitHub."""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import respx

from app.config import Settings
from app.services.github import GitHubApp, GitHubError, GitHubNotConfigured, InstallationToken
from tests.conftest import GITHUB_API, GITHUB_APP_ID

INSTALLATION = 42
REPO = "acme/fw"


def token_response(token: str, expires_in: timedelta = timedelta(hours=1)) -> httpx.Response:
    expires_at = (datetime.now(UTC) + expires_in).isoformat().replace("+00:00", "Z")
    return httpx.Response(201, json={"token": token, "expires_at": expires_at})


@pytest.fixture
def gh_settings(github_app_key_pem) -> Settings:
    # These tests need no database; any URL satisfies the setting.
    return Settings(
        database_url="postgresql+asyncpg://unused/unused",
        github_app_id=GITHUB_APP_ID,
        github_app_private_key=github_app_key_pem,
        github_api_url=GITHUB_API,
    )


@pytest.fixture
async def github(gh_settings):
    client = GitHubApp(gh_settings)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def mock():
    with respx.mock(base_url=GITHUB_API, assert_all_called=False) as router:
        yield router


def bearer(request: httpx.Request) -> str:
    return request.headers["authorization"].removeprefix("Bearer ")


# --------------------------------------------------------------- App JWT


def test_app_jwt_is_rs256_signed_by_the_app_key(github, github_app_key):
    token = github.app_jwt(now=1_800_000_000)
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    # The fixed `now` is not the real clock, so skip the time checks.
    claims = jwt.decode(
        token,
        github_app_key.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == GITHUB_APP_ID
    # Backdated for clock drift, and within GitHub's 10-minute limit.
    assert claims["iat"] < 1_800_000_000 < claims["exp"]
    assert claims["exp"] - claims["iat"] <= 600


def test_private_key_with_escaped_newlines_is_accepted(github_app_key_pem):
    escaped = github_app_key_pem.replace("\n", "\\n")
    s = Settings(database_url="postgresql+asyncpg://unused/unused", github_app_private_key=escaped)
    assert s.github_app_private_key.get_secret_value() == github_app_key_pem


def test_empty_app_settings_count_as_unset():
    s = Settings(
        database_url="postgresql+asyncpg://unused/unused", github_app_id="", github_app_private_key=""
    )
    assert s.github_app_id is None and s.github_app_private_key is None


async def test_unconfigured_app_refuses_to_sign():
    client = GitHubApp(Settings(database_url="postgresql+asyncpg://unused/unused"))
    try:
        assert not client.configured
        with pytest.raises(GitHubNotConfigured):
            await client.installation_token(INSTALLATION)
    finally:
        await client.aclose()


# ---------------------------------------------------- installation tokens


async def test_token_exchange_uses_app_jwt_and_is_cached(github, mock, github_app_key):
    route = mock.post(f"/app/installations/{INSTALLATION}/access_tokens").mock(
        return_value=token_response("ghs_one")
    )
    assert await github.installation_token(INSTALLATION) == "ghs_one"
    assert await github.installation_token(INSTALLATION) == "ghs_one"

    assert route.call_count == 1
    request = route.calls.last.request
    claims = jwt.decode(bearer(request), github_app_key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == GITHUB_APP_ID
    assert request.headers["x-github-api-version"] == "2022-11-28"


async def test_token_near_expiry_is_refreshed(github, mock):
    route = mock.post(f"/app/installations/{INSTALLATION}/access_tokens").mock(
        side_effect=[
            token_response("ghs_old", expires_in=timedelta(minutes=2)),
            token_response("ghs_new"),
        ]
    )
    assert await github.installation_token(INSTALLATION) == "ghs_old"
    assert await github.installation_token(INSTALLATION) == "ghs_new"
    assert route.call_count == 2


async def test_tokens_are_cached_per_installation(github, mock):
    mock.post("/app/installations/1/access_tokens").mock(return_value=token_response("ghs_a"))
    mock.post("/app/installations/2/access_tokens").mock(return_value=token_response("ghs_b"))
    assert await github.installation_token(1) == "ghs_a"
    assert await github.installation_token(2) == "ghs_b"


async def test_concurrent_callers_share_one_exchange(github, mock):
    async def slow(request):
        await asyncio.sleep(0.05)
        return token_response("ghs_shared")

    route = mock.post(f"/app/installations/{INSTALLATION}/access_tokens").mock(side_effect=slow)
    tokens = await asyncio.gather(*(github.installation_token(INSTALLATION) for _ in range(5)))
    assert tokens == ["ghs_shared"] * 5
    assert route.call_count == 1


async def test_failed_exchange_raises_with_status_and_no_token(github, mock):
    mock.post(f"/app/installations/{INSTALLATION}/access_tokens").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(GitHubError) as info:
        await github.installation_token(INSTALLATION)
    assert info.value.status == 404
    assert "Not Found" in str(info.value)


def test_token_repr_hides_the_token():
    t = InstallationToken(token="ghs_secret", expires_at=datetime.now(UTC))
    assert "ghs_secret" not in repr(t)


# ------------------------------------------------------------- API calls


async def test_dispatch_workflow(github, mock):
    mock.post(f"/app/installations/{INSTALLATION}/access_tokens").mock(
        return_value=token_response("ghs_tok")
    )
    route = mock.post(f"/repos/{REPO}/actions/workflows/firmware-ci.yml/dispatches").mock(
        return_value=httpx.Response(204)
    )
    await github.dispatch_workflow(
        INSTALLATION, REPO, "firmware-ci.yml", ref="main", inputs={"build_id": "bld_X"}
    )
    request = route.calls.last.request
    assert bearer(request) == "ghs_tok"
    assert json.loads(request.content) == {"ref": "main", "inputs": {"build_id": "bld_X"}}


async def test_rejected_token_is_replaced_and_retried_once(github, mock):
    exchange = mock.post(f"/app/installations/{INSTALLATION}/access_tokens").mock(
        side_effect=[token_response("ghs_revoked"), token_response("ghs_fresh")]
    )
    cancel = mock.post(f"/repos/{REPO}/actions/runs/7/cancel").mock(
        side_effect=[httpx.Response(401, json={"message": "Bad credentials"}), httpx.Response(202)]
    )
    await github.cancel_run(INSTALLATION, REPO, 7)
    assert exchange.call_count == 2
    assert [bearer(c.request) for c in cancel.calls] == ["ghs_revoked", "ghs_fresh"]


async def test_network_error_becomes_github_error(github, mock):
    mock.post(f"/app/installations/{INSTALLATION}/access_tokens").mock(
        return_value=token_response("ghs_tok")
    )
    mock.post(f"/repos/{REPO}/actions/runs/7/cancel").mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(GitHubError) as info:
        await github.cancel_run(INSTALLATION, REPO, 7)
    assert info.value.status is None


async def test_repository_installation_lookup_uses_app_jwt(github, mock, github_app_key):
    route = mock.get(f"/repos/{REPO}/installation").mock(
        return_value=httpx.Response(200, json={"id": 99})
    )
    assert await github.repository_installation_id(REPO) == 99
    jwt.decode(bearer(route.calls.last.request), github_app_key.public_key(), algorithms=["RS256"])

"""services.oidc: verifying GitHub Actions OIDC tokens against a mocked JWKS."""
import time

import httpx
import jwt
import pytest
import respx

from app.config import Settings
from app.services import oidc as oidc_module
from app.services.oidc import GitHubOidcVerifier, InvalidToken, OidcUnavailable
from tests import oidc
from tests.github import SHA

RUN_ID = 30433642


@pytest.fixture(scope="module")
def key():
    return oidc.new_key()


@pytest.fixture(scope="module")
def other_key():
    return oidc.new_key()


@pytest.fixture
def mock(key):
    with respx.mock(assert_all_called=False) as router:
        router.get(oidc.JWKS_URL).respond(json=oidc.jwks((key, oidc.KID)))
        yield router


@pytest.fixture
async def verifier():
    v = GitHubOidcVerifier(Settings(database_url="postgresql+asyncpg://unused/unused"))
    try:
        yield v
    finally:
        await v.aclose()


def token(key, kid=oidc.KID, **overrides):
    """A token for RUN_ID; an override of None removes that claim."""
    payload = {**oidc.claims(run_id=RUN_ID, sha=SHA), **overrides}
    return oidc.sign({k: v for k, v in payload.items() if v is not None}, key, kid)


async def test_valid_token_yields_run_claims(mock, verifier, key):
    claims = await verifier.verify(token(key, run_attempt=2))
    assert claims.repository == "acme/fw"
    assert claims.repository_id == "300029405"
    assert (claims.run_id, claims.run_attempt) == (RUN_ID, 2)  # strings in the token
    assert claims.sha == SHA
    assert claims.ref == "refs/heads/main"
    assert claims.workflow_ref == "acme/fw/.github/workflows/firmware-ci.yml@refs/heads/main"
    assert (claims.event_name, claims.actor) == ("push", "octocat")


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else"},
        {"aud": ["someone-else", "another"]},
        {"iss": "https://token.actions.githubusercontent.com.evil.example"},
        {"iss": "https://gitlab.example/oidc"},
        {"exp": int(time.time()) - 120},  # beyond the 60 s leeway
        {"nbf": int(time.time()) + 600},
        {"exp": None},
        {"iat": None},
        {"aud": None},
        {"repository": None},
        {"run_id": None},
        {"run_attempt": None},
        {"sha": None},
        {"workflow_ref": None},
        {"run_id": "not-a-number"},
    ],
    ids=lambda o: ",".join(f"{k}={v}" for k, v in o.items()),
)
async def test_bad_claims_are_rejected(mock, verifier, key, overrides):
    with pytest.raises(InvalidToken):
        await verifier.verify(token(key, **overrides))


async def test_slightly_expired_token_is_within_leeway(mock, verifier, key):
    await verifier.verify(token(key, exp=int(time.time()) - 30))


async def test_signature_from_another_key_is_rejected(mock, verifier, other_key):
    # Right key id, wrong key: a forged token.
    with pytest.raises(InvalidToken):
        await verifier.verify(token(other_key))


async def test_tampered_payload_is_rejected(mock, verifier, key):
    header, payload, signature = token(key).split(".")
    forged = oidc.sign(oidc.claims(run_id=RUN_ID + 1, sha=SHA), key).split(".")[1]
    with pytest.raises(InvalidToken):
        await verifier.verify(f"{header}.{forged[:-2]}{payload[-2:]}.{signature}")
    with pytest.raises(InvalidToken):
        await verifier.verify(f"{header}.{forged}.{signature}")


@pytest.mark.parametrize("value", ["", "not-a-jwt", "a.b.c", "eyJhbGciOiJub25lIn0.e30."])
async def test_garbage_is_rejected(mock, verifier, value):
    with pytest.raises(InvalidToken):
        await verifier.verify(value)


async def test_unsigned_and_symmetric_tokens_are_rejected(mock, verifier, key):
    payload = oidc.claims(run_id=RUN_ID, sha=SHA)
    unsigned = jwt.encode(payload, None, algorithm="none", headers={"kid": oidc.KID})
    # HS256 "signed" with the public key, the classic algorithm-confusion attack.
    hs = jwt.encode(payload, "x" * 32, algorithm="HS256", headers={"kid": oidc.KID})
    for value in (unsigned, hs):
        with pytest.raises(InvalidToken):
            await verifier.verify(value)


async def test_token_without_key_id_is_rejected(mock, verifier, key):
    no_kid = jwt.encode(oidc.claims(run_id=RUN_ID, sha=SHA), key, algorithm="RS256")
    with pytest.raises(InvalidToken, match="no key id"):
        await verifier.verify(no_kid)


async def test_keys_are_cached(mock, verifier, key):
    for attempt in range(1, 6):
        await verifier.verify(token(key, run_attempt=attempt))
    assert mock.routes[0].call_count == 1


async def test_rotated_key_is_fetched_on_first_sight(mock, verifier, key, other_key):
    await verifier.verify(token(key))
    # GitHub rotates: the JWKS now also carries a new key.
    mock.routes[0].respond(json=oidc.jwks((key, oidc.KID), (other_key, "rotated")))
    verifier._fetched_at -= oidc_module.MIN_REFETCH_INTERVAL_S  # a minute has passed

    claims = await verifier.verify(token(other_key, kid="rotated"))
    assert claims.run_id == RUN_ID
    assert mock.routes[0].call_count == 2


async def test_unknown_key_ids_cannot_force_refetches(mock, verifier, key, other_key):
    await verifier.verify(token(key))
    for i in range(5):
        with pytest.raises(InvalidToken, match="unknown key"):
            await verifier.verify(token(other_key, kid=f"made-up-{i}"))
    assert mock.routes[0].call_count == 1  # rate-limited: no refetch within a minute


async def test_stale_cache_is_refreshed(mock, verifier, key):
    await verifier.verify(token(key))
    verifier._fetched_at -= verifier._cache_s + 1
    await verifier.verify(token(key))
    assert mock.routes[0].call_count == 2


async def test_unreachable_jwks_is_unavailable_not_invalid(verifier, key):
    with respx.mock() as router:
        route = router.get(oidc.JWKS_URL).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(OidcUnavailable):
            await verifier.verify(token(key))
        route.mock(side_effect=None, return_value=httpx.Response(503))
        with pytest.raises(OidcUnavailable):
            await verifier.verify(token(key))
        assert route.call_count == 2  # nothing cached, so every attempt refetches


async def test_failed_refresh_keeps_serving_cached_keys(mock, verifier, key):
    await verifier.verify(token(key))
    verifier._fetched_at -= verifier._cache_s + 1
    mock.routes[0].respond(500)
    claims = await verifier.verify(token(key))
    assert claims.run_id == RUN_ID

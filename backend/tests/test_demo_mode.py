"""Demo mode (docs/demo.md): what it refuses, and that outside it nothing of it exists."""
import pytest
import respx
from pydantic import ValidationError

from app.config import Settings
from app.demo.trust import DEMO_ISSUER, DemoTrust
from tests import oidc, reports

DEMO_URL = "postgresql+asyncpg://sfci:pw@postgres:5432/sfci_demo"
REAL_URL = "postgresql+asyncpg://sfci:pw@postgres:5432/sfci"


def demo_settings(**overrides) -> Settings:
    return Settings(**{"database_url": DEMO_URL, "demo_mode": True, **overrides})


# ------------------------------------------------------------ startup guards


def test_demo_mode_starts_on_the_demo_database():
    assert demo_settings().demo_mode


def test_demo_mode_refuses_any_other_database():
    with pytest.raises(ValidationError, match="only runs against the database 'sfci_demo'"):
        demo_settings(database_url=REAL_URL)


def test_real_mode_refuses_the_demo_database():
    with pytest.raises(ValidationError, match="holds demo data"):
        Settings(database_url=DEMO_URL)


@pytest.mark.parametrize(
    ("field", "value", "env"),
    [
        ("github_webhook_secret", "s3cret", "SFCI_GITHUB_WEBHOOK_SECRET"),
        ("github_app_id", "123", "SFCI_GITHUB_APP_ID"),
        ("github_app_private_key", "-----BEGIN PRIVATE KEY-----", "SFCI_GITHUB_APP_PRIVATE_KEY"),
    ],
)
def test_demo_mode_refuses_production_credentials(field, value, env):
    with pytest.raises(ValidationError, match=env) as info:
        demo_settings(**{field: value})
    assert value not in str(info.value)


def test_demo_mode_refuses_prod_env():
    with pytest.raises(ValidationError, match="SFCI_ENV=prod"):
        demo_settings(env="prod")


def test_empty_credentials_count_as_unset():
    assert demo_settings(github_webhook_secret="", github_app_id="").demo_mode


# ------------------------------------------------- outside demo mode (default)


async def test_meta_says_not_demo(client):
    r = await client.get("/api/v1/meta")
    assert r.status_code == 200
    assert r.json() == {"demo_mode": False, "version": "0.1.0"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/demo/scenarios"),
        ("POST", "/api/v1/demo/scenarios/clean_main"),
        ("POST", "/api/v1/demo/scenarios/no_such_scenario"),
    ],
)
async def test_demo_endpoints_are_404_outside_demo_mode(client, method, path):
    r = await client.request(method, path)
    unknown = await client.request(method, "/api/v1/no-such-endpoint")
    assert (r.status_code, r.json()) == (404, {"detail": "Not Found"}) == (unknown.status_code, unknown.json())


@pytest.mark.parametrize("published", [False, True], ids=["demo key unknown", "demo key published"])
async def test_demo_issuer_is_rejected_like_any_untrusted_issuer(client, published):
    """A demo token gets the same 401 as a token from an issuer nobody trusts,
    even if GitHub's key set somehow held the demo key."""
    demo, stranger = DemoTrust(), oidc.new_key()
    body = reports.report()
    claims = oidc.claims(run_id=reports.RUN_ID, sha=body["github"]["sha"])
    demo_token = demo.token(oidc.AUDIENCE, {k: v for k, v in claims.items() if k not in ("iss", "aud")})
    stranger_token = oidc.sign({**claims, "iss": "https://issuer.invalid"}, stranger)
    github_keys = oidc.jwks((stranger, oidc.KID))
    if published:
        github_keys["keys"] += demo.jwks()["keys"]
    with respx.mock(assert_all_called=False) as router:
        router.get(oidc.JWKS_URL).respond(json=github_keys)
        r_demo = await reports.post(client, body, demo_token)
        r_stranger = await reports.post(client, body, stranger_token)
    assert r_demo.status_code == r_stranger.status_code == 401
    assert r_demo.json() == r_stranger.json() == {"detail": "invalid token"}


async def test_webhooks_signed_with_a_demo_secret_are_rejected(client):
    from tests.github import deliver

    r = await deliver(client, "ping", {"zen": "hi"}, secret=DemoTrust().webhook_secret)
    assert r.status_code == 401


def test_demo_trust_never_shows_its_secrets():
    trust = DemoTrust()
    assert trust.webhook_secret not in repr(trust)
    assert "PRIVATE" not in repr(trust)
    assert DEMO_ISSUER == "sfci-demo"

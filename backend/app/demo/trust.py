"""Demo-only signers, made when the API starts in demo mode.

In demo mode no GitHub run exists to issue an OIDC token or sign a webhook,
so the API makes its own: an RSA key whose public half is the only key the
OIDC verifier trusts (issuer "sfci-demo", served as a JWKS from memory), and
a random webhook secret. Both live in this process only and die with it; the
replay (app.demo.replay) signs with them in the same process.

The verification code is unchanged: GitHubOidcVerifier checks the demo
tokens exactly as it checks GitHub's, against a different issuer and key set,
and the webhook endpoint checks HMAC-SHA256 with the demo secret. Outside demo
mode none of this exists, and a token from issuer "sfci-demo" is an unknown
issuer's token like any other.
"""
import json
import secrets
import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr

from app.config import Settings

DEMO_ISSUER = "sfci-demo"
# Never resolved: requests to it are answered by jwks_transport() in memory.
DEMO_JWKS_URL = "http://sfci-demo.invalid/.well-known/jwks"
TOKEN_LIFETIME_S = 300


class DemoTrust:
    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = f"sfci-demo-{secrets.token_hex(4)}"
        self.webhook_secret = secrets.token_hex(32)

    def __repr__(self) -> str:  # keep the key and secret out of reprs
        return f"DemoTrust(kid={self.kid!r})"

    def settings_for(self, settings: Settings) -> Settings:
        """The API's settings in demo mode: the demo webhook secret and issuer.
        Settings validation has already refused production secrets."""
        return settings.model_copy(
            update={
                "github_webhook_secret": SecretStr(self.webhook_secret),
                "oidc_issuer": DEMO_ISSUER,
            }
        )

    def jwks(self) -> dict[str, Any]:
        key = json.loads(RSAAlgorithm.to_jwk(self._key.public_key()))
        return {"keys": [{**key, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def jwks_transport(self) -> httpx.MockTransport:
        """Serves the demo JWKS to the OIDC verifier without a network."""

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == DEMO_JWKS_URL:
                return httpx.Response(200, json=self.jwks())
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    def token(self, audience: str, claims: dict[str, Any]) -> str:
        """An OIDC token shaped like GitHub's for a workflow job."""
        now = int(time.time())
        payload = {
            "iss": DEMO_ISSUER,
            "aud": audience,
            "iat": now,
            "nbf": now - 5,
            "exp": now + TOKEN_LIFETIME_S,
            **claims,
        }
        return jwt.encode(payload, self._key, algorithm="RS256", headers={"kid": self.kid})

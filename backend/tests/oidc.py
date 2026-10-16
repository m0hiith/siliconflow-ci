"""A fake GitHub Actions OIDC issuer: a throwaway RSA key, its JWKS, and
tokens shaped like the ones GitHub issues to a workflow job."""
import json
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = f"{ISSUER}/.well-known/jwks"
AUDIENCE = "siliconflow"
KID = "sfci-test-key"
WORKFLOW_PATH = ".github/workflows/firmware-ci.yml"


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key: rsa.RSAPrivateKey, kid: str = KID) -> dict[str, Any]:
    data = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return {**data, "kid": kid, "use": "sig", "alg": "RS256"}


def jwks(*keys: tuple[rsa.RSAPrivateKey, str]) -> dict[str, Any]:
    return {"keys": [jwk(key, kid) for key, kid in keys]}


def claims(
    *,
    repository: str = "acme/fw",
    repository_id: int | None = 300029405,
    run_id: int,
    run_attempt: int = 1,
    sha: str,
    ref: str = "refs/heads/main",
    event_name: str = "push",
    workflow_path: str = WORKFLOW_PATH,
    now: float | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Claims as GitHub issues them; note run_id and run_attempt are strings."""
    now = time.time() if now is None else now
    workflow_ref = f"{repository}/{workflow_path}@{ref}"
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": f"repo:{repository}:ref:{ref}",
        "iat": int(now),
        "nbf": int(now) - 5,
        "exp": int(now) + 300,
        "jti": "c3f1c8a0-0000-4000-8000-000000000000",
        "repository": repository,
        "repository_owner": repository.split("/")[0],
        "run_id": str(run_id),
        "run_number": "7",
        "run_attempt": str(run_attempt),
        "sha": sha,
        "ref": ref,
        "ref_type": "branch",
        "event_name": event_name,
        "actor": "octocat",
        "workflow": "Firmware CI",
        "workflow_ref": workflow_ref,
        "job_workflow_ref": workflow_ref,
        "runner_environment": "github-hosted",
    }
    if repository_id is not None:
        payload["repository_id"] = str(repository_id)
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not None}


def sign(payload: dict[str, Any], key: rsa.RSAPrivateKey, kid: str = KID) -> str:
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})

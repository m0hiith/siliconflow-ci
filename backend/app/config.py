import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

WORKFLOW_PATH_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$")
STAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class Settings(BaseSettings):
    """Runtime configuration, read from SFCI_* environment variables only."""

    model_config = SettingsConfigDict(env_prefix="SFCI_", extra="ignore")

    # SecretStr keeps the password out of reprs and logs.
    database_url: SecretStr = Field(
        description="SQLAlchemy URL, e.g. postgresql+asyncpg://user:pass@host:5432/db"
    )
    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    db_pool_size: int = 5

    worker_poll_interval_s: float = 2.0
    worker_heartbeat_path: str = "/tmp/sfci-worker-heartbeat"

    # Shared secret configured on the GitHub webhook. Unset means every
    # delivery is rejected; unsigned webhooks are never accepted.
    github_webhook_secret: SecretStr | None = None
    # The firmware workflows. Each is tracked as its own builds (one per run
    # attempt) with its own required stages; workflow_run events from other
    # workflows (e.g. the toolchain image) are ignored. The primary workflow's
    # required stages are required_stages, and manual builds dispatch it.
    github_workflow_path: str = ".github/workflows/firmware-ci.yml"
    # Further firmware workflows and their required stages, as JSON in
    # SFCI_GITHUB_EXTRA_WORKFLOWS: {".github/workflows/x.yml": ["configure", ...]}.
    github_extra_workflows: dict[str, tuple[str, ...]] = {
        ".github/workflows/firmware-ci-cortex-m4.yml": ("configure", "compile", "package", "size"),
        ".github/workflows/firmware-ci-siwx917-soc.yml": ("generate", "compile", "package"),
    }
    # Hardware-in-the-loop workflows (docs/hil.md), as JSON in
    # SFCI_GITHUB_HIL_WORKFLOWS: {path: {report profile: profile of the builds
    # it tests}}. They create no builds: each run report names the build it
    # tested and adds a "hil" stage to it (services.linked_reports).
    github_hil_workflows: dict[str, dict[str, str]] = {
        ".github/workflows/hil.yml": {"siwx917-hil": "siwx917-soc"},
    }

    # GitHub App credentials (docs/github-app.md). Unset: manual builds and
    # cancellation answer 503; webhook ingestion does not need them.
    github_app_id: str | None = None
    # PEM private key. Literal "\n" sequences are accepted for single-line env files.
    github_app_private_key: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    github_api_timeout_s: float = 10.0

    # Run reports are authenticated with GitHub Actions OIDC tokens issued for
    # this audience (ci/post_report.sh requests the same one).
    oidc_issuer: str = "https://token.actions.githubusercontent.com"
    oidc_audience: str = "siliconflow"
    oidc_jwks_cache_s: float = 3600.0

    # Stages every build must pass, comma-separated. A run report can add
    # required stages but never drop one of these.
    required_stages: Annotated[tuple[str, ...], NoDecode] = (
        "configure",
        "compile",
        "test",
        "analyze",
        "package",
    )
    # A run that concluded success/failure with no report after this long is errored.
    report_deadline_s: int = 600

    # Reconciler (services.reconciler): how often the worker looks for builds
    # whose webhooks went missing, and when a build counts as overdue. The
    # timeout matches the longest job's timeout-minutes in firmware-ci.yml.
    reconcile_interval_s: float = 300.0
    build_timeout_s: int = 900
    reconcile_grace_s: int = 300

    # Content-addressed artifact store: <artifact_root>/sha256/ab/cd/<sha256>.
    artifact_root: str = "/var/lib/sfci/artifacts"
    # Largest Actions artifact zip the worker will download.
    artifact_max_download_bytes: int = 1 << 30
    # Longer stage logs keep their first and last halves (services.stage_logs).
    stage_log_max_bytes: int = 16 << 20
    # For compare links between builds.
    github_web_url: str = "https://github.com"

    @field_validator("github_webhook_secret", "github_app_private_key", "github_app_id", mode="before")
    @classmethod
    def _empty_secret_is_unset(cls, value: object) -> object:
        # An empty HMAC key would let anyone sign a payload.
        return None if value == "" else value

    @field_validator("required_stages", mode="before")
    @classmethod
    def _split_stages(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(name.strip() for name in value.split(",") if name.strip())
        return value

    @field_validator("github_extra_workflows")
    @classmethod
    def _check_workflows(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for path, stages in value.items():
            if not WORKFLOW_PATH_RE.fullmatch(path):
                raise ValueError(f"{path!r} is not a .github/workflows/*.yml path")
            if not stages or not all(STAGE_NAME_RE.fullmatch(name) for name in stages):
                raise ValueError(f"{path!r} needs one or more valid required stage names")
        return value

    @model_validator(mode="after")
    def _check_hil_workflows(self) -> "Settings":
        for path, profiles in self.github_hil_workflows.items():
            if not WORKFLOW_PATH_RE.fullmatch(path):
                raise ValueError(f"{path!r} is not a .github/workflows/*.yml path")
            if self.required_stages_for(path) is not None:
                raise ValueError(f"{path!r} cannot be both a firmware and a hardware-in-the-loop workflow")
            if not profiles or not all(
                STAGE_NAME_RE.fullmatch(k) and STAGE_NAME_RE.fullmatch(v) for k, v in profiles.items()
            ):
                raise ValueError(f"{path!r} needs one or more valid report and build profiles")
        return self

    def hil_profiles_for(self, workflow_path: str) -> dict[str, str] | None:
        """{report profile: tested build profile} of a hardware-in-the-loop
        workflow; None for any other workflow."""
        wanted = workflow_path.lower()
        for path, profiles in self.github_hil_workflows.items():
            if wanted == path.lower():
                return profiles
        return None

    def required_stages_for(self, workflow_path: str) -> tuple[str, ...] | None:
        """The required stages of a firmware workflow; None for any other workflow."""
        wanted = workflow_path.lower()
        if wanted == self.github_workflow_path.lower():
            return self.required_stages
        for path, stages in self.github_extra_workflows.items():
            if wanted == path.lower():
                return stages
        return None

    @field_validator("github_app_private_key", mode="before")
    @classmethod
    def _unescape_newlines(cls, value: object) -> object:
        if isinstance(value, str) and "\\n" in value:
            return value.replace("\\n", "\n")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # populated from the environment

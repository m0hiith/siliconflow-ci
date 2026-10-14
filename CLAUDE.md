# SiliconFlow CI

Automated embedded firmware build, test and release platform. Portfolio project
targeting embedded/release engineering roles (Silicon Labs, SiWx917).

## Architecture (do not change without asking)
- GitHub Actions EXECUTES builds. SiliconFlow RECORDS, CORRELATES and GATES.
- The backend never runs user code.
- Runners report facts in a versioned "run report" (schema sfci.run-report/1).
  Only the backend decides build status and release eligibility.
- Run reports are authenticated with GitHub OIDC JWTs; webhooks with HMAC-SHA256.
- Every webhook is deduplicated on X-GitHub-Delivery; all handlers are idempotent.
- Build status changes ONLY through backend/app/services/transitions.py.
- Artifacts are re-hashed (SHA-256) on the server and stored content-addressed.

## Stack
- firmware/: C11, CMake + Ninja, Unity tests, gcovr coverage
- backend/: Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic v2, pytest
- frontend/: React + TypeScript + Vite, TanStack Query, generated OpenAPI client
- infra: Docker Compose (postgres, backend, worker, frontend, minio later)
- ci/: bash + Python helper scripts used by workflows

## Build statuses
queued -> running -> passed | failed | cancelled | timed_out | errored
Terminal states never change. 'errored' = CI infrastructure failed, not firmware.

## Conventions
- IDs: 'bld_' / 'art_' + ULID. Timestamps: timestamptz, UTC.
- No fake or hard-coded data in UI or API. Seed data must come from real builds.
- Every backend change ships with pytest tests. Every C change ships with Unity tests.
- Pin GitHub Actions to full commit SHAs. Never use pull_request_target.
- Secrets only from env vars; never log tokens.
- Keep diffs focused on the current task; list follow-ups instead of doing them.

## Commands
- Firmware: cmake -S firmware -B build -G Ninja && cmake --build build && ctest --test-dir build
- Backend tests: cd backend && pytest -q
- Full stack: docker compose up --build

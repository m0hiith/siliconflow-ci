# Playwright fixtures

API responses for two real builds, written by the backend test
`tests/test_build_details.py`, which replays two recorded CI runs
(`backend/tests/fixtures/ci_runs`) through the real pipeline: webhooks, an
OIDC-signed run report, artifact ingestion and gate evaluation. Do not edit
them by hand; regenerate with:

    docker compose run --rm -v "$PWD/frontend/e2e/fixtures:/out" \
      -e SFCI_UI_FIXTURES_DIR=/out --user root tests \
      pytest -q tests/test_build_details.py -k recorded

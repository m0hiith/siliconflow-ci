"""ci/post_report.sh against a local server that plays both GitHub's OIDC
token endpoint and the SiliconFlow API."""
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

POST = Path(__file__).resolve().parents[1] / "post_report.sh"
REQUEST_TOKEN = "actions-request-token-sfci"
ID_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJyZXBvOmFjbWUvZncifQ.c2lnbmF0dXJl"


class FakeServer:
    def __init__(self) -> None:
        self.api_codes: list[int] = []  # one per POST; the last one repeats
        self.token_requests: list[dict] = []
        self.posts: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _send(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                outer.token_requests.append(
                    {"query": parse_qs(url.query), "auth": self.headers.get("Authorization")}
                )
                if self.headers.get("Authorization") != f"bearer {REQUEST_TOKEN}":
                    return self._send(401, {"message": "bad request token"})
                self._send(200, {"value": ID_TOKEN})

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.posts.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "body": body,
                    }
                )
                codes = outer.api_codes
                code = codes.pop(0) if len(codes) > 1 else (codes[0] if codes else 202)
                self._send(code, {"status": f"code {code}"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    with FakeServer() as s:
        yield s


@pytest.fixture
def report_file(tmp_path) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"schema": "sfci.run-report/1", "stages": []}) + "\n")
    return path


def run_post(report: Path, server: FakeServer | None, **env_overrides) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ACTIONS_", "SFCI_", "GITHUB_"))}
    if server is not None:
        env.update(
            SFCI_API_URL=server.url + "/",
            ACTIONS_ID_TOKEN_REQUEST_URL=f"{server.url}/token?api-version=2.0",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN=REQUEST_TOKEN,
        )
    env.update({"SFCI_POST_RETRY_DELAY_S": "0", **env_overrides})
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run(["bash", str(POST), str(report)], env=env, capture_output=True, text=True)


def test_report_is_posted_with_an_oidc_token(server, report_file):
    r = run_post(report_file, server)
    assert r.returncode == 0, r.stderr
    assert "run report accepted (202)" in r.stdout

    (token_request,) = server.token_requests
    assert token_request["query"]["audience"] == ["siliconflow"]
    assert token_request["query"]["api-version"] == ["2.0"]
    assert token_request["auth"] == f"bearer {REQUEST_TOKEN}"

    (post,) = server.posts
    assert post["path"] == "/api/v1/runs/report"
    assert post["auth"] == f"Bearer {ID_TOKEN}"
    assert post["content_type"] == "application/json"
    assert post["body"] == report_file.read_bytes()  # sent byte for byte


def test_audience_can_be_overridden(server, report_file):
    assert run_post(report_file, server, SFCI_OIDC_AUDIENCE="sfci-staging").returncode == 0
    assert server.token_requests[0]["query"]["audience"] == ["sfci-staging"]


def test_tokens_never_appear_in_output(server, report_file):
    server.api_codes = [503, 401]
    r = run_post(report_file, server)
    assert r.returncode == 1
    for secret in (ID_TOKEN, REQUEST_TOKEN):
        assert secret not in r.stdout and secret not in r.stderr


def test_token_is_masked_inside_actions(server, report_file):
    r = run_post(report_file, server, GITHUB_ACTIONS="true")
    assert r.returncode == 0
    # The only place the token is printed is the mask command, which the runner swallows.
    assert r.stdout.count(ID_TOKEN) == 1 and f"::add-mask::{ID_TOKEN}" in r.stdout


@pytest.mark.parametrize("transient", [404, 429, 500, 503])
def test_transient_answers_are_retried(server, report_file, transient):
    server.api_codes = [transient, transient, 201]
    r = run_post(report_file, server)
    assert r.returncode == 0, r.stderr
    assert len(server.posts) == 3
    assert f"attempt 1/8: HTTP {transient}" in r.stderr


def test_backoff_doubles_up_to_the_cap(server, report_file):
    server.api_codes = [404]
    r = run_post(
        report_file, server,
        SFCI_POST_ATTEMPTS="5", SFCI_POST_RETRY_DELAY_S="1", SFCI_POST_RETRY_MAX_DELAY_S="2",
    )
    assert r.returncode == 1
    waits = [line.rsplit("retrying in ", 1)[1] for line in r.stderr.splitlines() if "retrying in" in line]
    assert waits == ["1s", "2s", "2s", "2s"]


def test_gives_up_after_the_last_attempt(server, report_file):
    server.api_codes = [503]
    r = run_post(report_file, server, SFCI_POST_ATTEMPTS="3")
    assert r.returncode == 1
    assert len(server.posts) == 3
    assert "not delivered after 3 attempts (last HTTP 503)" in r.stderr


@pytest.mark.parametrize("code", [400, 401, 403, 422])
def test_rejection_fails_without_retry(server, report_file, code):
    server.api_codes = [code]
    r = run_post(report_file, server)
    assert r.returncode == 1
    assert len(server.posts) == 1
    assert f"rejected (HTTP {code})" in r.stderr


def test_finished_build_is_a_warning_not_a_failure(server, report_file):
    server.api_codes = [409]
    r = run_post(report_file, server, GITHUB_ACTIONS="true")
    assert r.returncode == 0
    assert len(server.posts) == 1
    assert "::warning title=run report::run report not recorded (409)" in r.stdout


def test_unreachable_backend_is_retried_then_fails(report_file, server):
    r = run_post(report_file, server, SFCI_API_URL="http://127.0.0.1:9", SFCI_POST_ATTEMPTS="2")
    assert r.returncode == 1
    assert "attempt 1/2: HTTP 000" in r.stderr


def test_no_api_url_skips_quietly(report_file, server):
    r = run_post(report_file, server, SFCI_API_URL="")
    assert r.returncode == 0
    assert "SFCI_API_URL is not set" in r.stderr
    assert server.token_requests == [] and server.posts == []


def test_no_oidc_token_skips_with_a_warning(report_file):
    r = run_post(report_file, None, SFCI_API_URL="http://127.0.0.1:9")
    assert r.returncode == 0
    assert "no OIDC token available" in r.stderr


def test_token_endpoint_refusal_fails(server, report_file):
    r = run_post(report_file, server, ACTIONS_ID_TOKEN_REQUEST_TOKEN="wrong")
    assert r.returncode == 1
    assert "could not obtain an OIDC token" in r.stderr
    assert server.posts == []


def test_usage_errors(tmp_path):
    for args in ([], [str(tmp_path / "missing.json")]):
        r = subprocess.run(["bash", str(POST), *args], capture_output=True, text=True)
        assert r.returncode == 2

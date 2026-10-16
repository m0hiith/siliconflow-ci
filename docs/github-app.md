# GitHub App

SiliconFlow calls GitHub through a GitHub App, never a personal access token.
It makes four kinds of calls:

- **workflow_dispatch**: starts a manual build (on behalf of a maintainer).
- **cancel**: asks GitHub to cancel a build's workflow run (on behalf of a maintainer).
- **artifact download**: fetches a run's `out/` artifact after its run report
  arrives, so the worker can re-hash and store the firmware (see
  [run-reports.md](run-reports.md)).
- **run lookup**: every 5 minutes the worker's reconciler fetches the run
  attempt of each build still queued or running past `SFCI_BUILD_TIMEOUT_S`
  + `SFCI_RECONCILE_GRACE_S` (15 + 5 minutes), and applies its real state,
  so a lost webhook cannot leave a build stuck. Each correction is logged at
  WARNING as `build corrected from the GitHub API`.

GitHub Actions runs the builds. The backend records each build, correlates it
with its workflow run, and gates releases. It never runs user code.

## Creating the App

Under *Settings → Developer settings → GitHub Apps → New GitHub App*, on the
account or organisation that owns the firmware repository:

| Setting | Value |
| --- | --- |
| Webhook URL | `https://<backend host>/api/v1/webhooks/github` |
| Webhook secret | Random, e.g. `openssl rand -hex 32`. The same value goes in `SFCI_GITHUB_WEBHOOK_SECRET`. |
| Where can this App be installed? | Only on this account |

### Repository permissions

| Permission | Access | Why |
| --- | --- | --- |
| Actions | **Read and write** | Dispatch `firmware-ci.yml`, cancel its runs, and download their artifacts. |
| Contents | **Read-only** | Read the branches and workflow files that a dispatch targets. |
| Metadata | **Read-only** | Required by GitHub for every App. |

The App needs no other permissions. In particular it has no write access to
code, pull requests or checks.

### Subscribed events

| Event | Used for |
| --- | --- |
| `workflow_run` | Creating builds for push and pull_request runs, binding manual builds to their run, and recording each run's conclusion. |
| `workflow_job` | Detecting when a build starts running. |

### Install and configure

1. Generate a private key on the App's page. GitHub downloads a `.pem` file.
2. Install the App on the firmware repository only, not on all repositories.
3. Set these environment variables on the backend (see `.env.example`):

   | Variable | Value |
   | --- | --- |
   | `SFCI_GITHUB_APP_ID` | The App ID shown on the App's page. |
   | `SFCI_GITHUB_APP_PRIVATE_KEY` | The PEM contents, either quoted across several lines or on one line with literal `\n` between lines. |
   | `SFCI_GITHUB_WEBHOOK_SECRET` | The webhook secret set above. |

   You don't need to configure the installation id. The backend looks it up
   the first time it is needed (`GET /repos/{owner}/{repo}/installation`) and
   stores it on the repository row.

If the App variables are unset, webhooks are still accepted, but triggering
and cancelling builds answers `503`.

## How the backend authenticates

`backend/app/services/github.py` does the following:

1. It signs an App JWT (RS256, issuer = App ID). The JWT lives for 9 minutes,
   inside GitHub's 10-minute limit, and its `iat` is backdated 60 seconds to
   allow for clock drift.
2. It exchanges the JWT for an installation token with
   `POST /app/installations/{id}/access_tokens`. Tokens are valid for one hour.
3. It caches tokens per installation and refreshes a token when it has less
   than 5 minutes left. When several callers need a token at once, they share
   a single exchange. If GitHub answers `401`, the token is dropped and the
   call is retried once with a new token.

Tokens and the private key are never logged. Errors report only GitHub's
status code and message.

## Manual builds

`POST /api/v1/projects/{project_id}/builds` is restricted to maintainers. The
body is optional: `{"repository_id": "...", "branch": "main"}`.

1. The backend records a `queued` build with `trigger=manual` and writes a
   `build.trigger` row to `audit_log`. Both are committed before GitHub is
   called.
2. It dispatches `firmware-ci.yml` on the branch with inputs
   `{build_id}`. The workflow names its run `sfci <build_id>`.
3. If the dispatch fails (GitHub refuses, or the network fails), the build
   moves to `errored`, with the reason in `status_reason`. The response is
   still `201`, and its body shows the errored build.

GitHub's dispatch response contains no run id. Correlation works like this:

- Today, the first `workflow_run` webhook whose `display_title` is
  `sfci <build_id>` binds the run to the build. See
  `services/github_events.find_or_create_build`.
- Planned: the workflow's first step will POST its run id back to the backend.
  Run-name matching stays as a fallback.

A dispatch that timed out may still have started a run. That run is bound to
the build by name, but the build stays `errored`, because terminal states
never change.

## Cancelling

`POST /api/v1/builds/{build_id}/cancel` is restricted to maintainers. It calls
`POST /repos/{owner}/{repo}/actions/runs/{run_id}/cancel` and writes a
`build.cancel_request` row to `audit_log`. The build's status does not change
at this point. It becomes `cancelled` only when GitHub sends the
`workflow_run` `completed` event with conclusion `cancelled`.

| Response | Meaning |
| --- | --- |
| `202` | GitHub accepted the request. |
| `409` | The build is already finished, it has no workflow run yet, or GitHub says the run can no longer be cancelled. |
| `502` | GitHub returned an error or did not answer. |

## Who counts as a maintainer

Sign-in is not implemented yet. Until it is, the backend cannot identify any
caller, so both endpoints answer `401` to everyone (`app/auth.py`). Tests
supply a user through FastAPI dependency overrides.

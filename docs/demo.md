# Demo mode

Demo mode lets SiliconFlow be shown live, without a deployed GitHub App and
without a firmware repository sending real builds. It replays recorded inputs
through the real backend, so the screens show what the production code makes of
those inputs. It is opt-in, stored separately, and always labelled.

```sh
make demo          # http://localhost:8080, then click "Start tour"
make demo-reset    # delete every demo build and artifact, start from empty
make demo-down     # stop it, keeping its data
make demo-replay   # replay every scenario without the UI (SPEED=4 for faster)
```

## What is real and what is replayed

| Part | In demo mode |
| --- | --- |
| Webhook endpoint, HMAC-SHA256 check, X-GitHub-Delivery dedupe | real code, demo secret |
| Run-report endpoint, OIDC JWT verification, claim matching | real code, demo issuer |
| Worker, `transitions.py`, `outcome.py` | real code |
| Artifact ingestion: re-hash, manifest check, content-addressed store, JUnit | real code; the zip comes from `demo/fixtures/artifacts` |
| Stage-log redaction and `classify.py` | real code |
| Gate evaluation, ELF size measurement | real code; `gates.yml` comes from `demo/fixtures/gates` |
| Analytics, metrics | real code |
| Webhooks and run reports | recorded, then replayed (see [demo/README.md](../demo/README.md)) |

The replay never writes a build, stage, artifact, test case or gate result.
It sends HTTP requests to the API's own endpoints, as GitHub and a runner
would, and the worker does the rest. The one row it writes directly is the
demo project and repository. In a real deployment an operator registers those.

## Trust boundaries

Demo mode changes **which inputs are trusted**, never **how they are checked**.

- **Run reports.** When the API starts in demo mode it generates an RSA key in
  memory. The OIDC verifier is given only that key's public half, as a JWKS
  served from memory (no network), and the issuer `sfci-demo`. Tokens are
  checked by the same `GitHubOidcVerifier` code, with the same algorithm,
  audience, expiry and claim rules. GitHub's real tokens are rejected in demo
  mode, since their issuer is not `sfci-demo`.
- **Webhooks.** The API generates a random webhook secret at startup. The
  same `verify_signature` checks it, and the same `record_delivery` dedupes
  on X-GitHub-Delivery.
- **Artifacts and gates.yml.** The worker's `RepoSource` is `DemoRepoSource`
  instead of `GitHubApp`. It serves the scenario's zip and `gates.yml` through
  the same four calls that ingestion and gate evaluation make. Everything after
  the download is the production code. The GitHub reconciler is off.
- **Where the signers live.** The key and secret exist only in the API process.
  The replay runs in the same process ([backend/app/demo/replay.py](../backend/app/demo/replay.py))
  and posts to the app through its ASGI interface. They are never logged,
  written to disk or returned by any endpoint, and they die with the process.

## Why demo mode cannot weaken production

- **It is off by default.** `SFCI_DEMO_MODE` defaults to `false`. Without it,
  no demo code path is reachable:
  - the demo endpoints answer `404 {"detail": "Not Found"}`, exactly like a path that does not exist;
  - a token from issuer `sfci-demo` is rejected with the same `401 invalid token` as any untrusted issuer;
  - a webhook signed with a demo secret is rejected with `401`.

  Tests cover each of these ([test_demo_mode.py](../backend/tests/test_demo_mode.py)).
- **It refuses to start next to real data or real credentials.** In demo mode
  the backend and worker exit at startup in any of these cases:
  - the database is not named `sfci_demo`;
  - `SFCI_ENV` is `prod`;
  - `SFCI_GITHUB_WEBHOOK_SECRET`, `SFCI_GITHUB_APP_ID` or `SFCI_GITHUB_APP_PRIVATE_KEY` is set.

  The reverse also holds: outside demo mode, the backend refuses to start on a
  database named `sfci_demo`. The startup error never echoes the settings' values.
- **It has separate storage.** `make demo` runs Compose project
  `siliconflow-demo`. It has its own Postgres volume (database `sfci_demo`),
  its own artifact volume and its own network, and Postgres is not published.
  It reads `.env.demo`, which holds only a generated database password; the
  real stack's `.env` is never read.
- **It is always labelled.**
  - Every page shows a banner that cannot be dismissed and stays in view while scrolling: "Demo mode — replayed sample inputs, processed by the real SiliconFlow backend. Not production results."
  - Every screenshot taken in demo mode therefore carries it.
  - The API and worker log a `DEMO MODE` warning at startup.
- **It never feeds the measured results.** `scripts/metrics_report.py`
  refuses to run against `sfci_demo` and prints
  `DEMO DATA — NOT MEASURED RESULTS`. The README's measured results only
  ever come from real builds.

## The guided tour

The banner's **Start tour** button opens a ten-step tour that follows the
README's demo script:

1. the problem
2. a clean build
3. Build Details
4. artifacts and gates
5. a compile error
6. passed but not releasable
7. webhook redelivery
8. a tampered artifact
9. who fixes which failure
10. Analytics

Each step does three things: replays its scenario through the API
(`POST /api/v1/demo/scenarios/{name}`), opens the page it is about, and
highlights the part to look at. Each step explains itself in plain English,
with a **Technical detail** section for engineers.

**Keyboard and screen readers.** Use ← and → to move between steps and Esc to
close; Step X of N is announced. **Plain language** in the banner swaps jargon
for plain words, for example "SHA-256" becomes "file fingerprint" and "gate"
becomes "release rule".

**Replaying again is harmless.** A replayed build keeps the times of its first
replay, so its report is byte-identical and the API answers "duplicate". Use
`make demo-reset` to start from empty.

## Known differences from a live deployment

- **The weekly pass-rate chart shows one week.** Analytics groups builds by
  when the server recorded them, and the five weeks of history are recorded
  when they are replayed. Durations, sizes and flaky tests use the replayed
  runs' own timestamps and results.
- **Commit links lead nowhere.** Commit and Actions run links point at
  `github.com/siliconflow-demo/firmware`, which does not exist.
- **Some fixtures are synthetic.** The cortex-m4, siwx917-soc and HIL runs are
  assembled rather than captured, and their provenance is listed in
  [demo/README.md](../demo/README.md).

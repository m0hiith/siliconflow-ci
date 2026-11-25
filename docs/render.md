# Deploying to Render (demo mode, free tier)

[render.yaml](../render.yaml) is a Render Blueprint that runs SiliconFlow in
**demo mode** ([demo.md](demo.md)) on Render's free tier. Visitors see the
same UI as `make demo`: replayed CI inputs processed by the real backend,
under a permanent "Demo mode" banner, with a guided tour.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/m0hiith/siliconflow-ci)

## Set it up

1. Click the button above, or in the Render dashboard choose
   **New → Blueprint** and pick this repository.
2. Render reads `render.yaml` and proposes two resources:
   `siliconflow-ci` (web service) and `siliconflow-demo-db` (Postgres).
   Click **Apply**. No secrets are asked for: demo mode refuses to start
   with GitHub credentials.
3. Wait for the first deploy (a few minutes: it builds the UI, runs the
   frontend tests and installs the backend). Open
   `https://<service>.onrender.com` and click **Start tour**.

Every push to `main` redeploys.

## What runs where

| Compose (local) | Render |
| --- | --- |
| `frontend` (nginx) | nginx inside the `siliconflow-ci` container |
| `backend` (API) | uvicorn inside the same container, on `127.0.0.1:8000` |
| `worker` | `python -m app.worker` inside the same container |
| `migrate` | `alembic upgrade head` at container start |
| `postgres` | Render Postgres `siliconflow-demo-db`, database `sfci_demo`, internal connections only |
| `artifacts` volume | the container's own disk |

The free tier has no persistent disks and no background workers, and the API
and worker both read the artifact store, so the three processes share one
container ([deploy/render/Dockerfile](../deploy/render/Dockerfile)).
[start.sh](../deploy/render/start.sh) starts them and exits if any one stops,
so Render restarts the whole service.

## Free-tier behaviour

- **It sleeps.** After about 15 idle minutes the service stops. The next
  visitor waits about a minute while it starts.
- **Each start is empty.** The container's disk is wiped on every restart,
  deploy and wake-up, so [reset_demo_db.py](../deploy/render/reset_demo_db.py)
  empties the demo database at the same moment, and the database never
  refers to files that are gone. Visitors click **Start tour** to replay the
  scenarios. The reset refuses to run outside demo mode or against any
  database other than `sfci_demo`.
- **Free Postgres expires** after a limited period (see Render's pricing
  page). Recreate it from the Blueprint when it does; the instance holds only
  replayable demo data.
- **512 MB of memory.** The three processes fit with room to spare when
  replaying the demo scenarios.

## Moving to real builds later

Real mode needs a different setup, not a toggle: a database not named
`sfci_demo`, `SFCI_DEMO_MODE` unset, `SFCI_ENV=prod`, the GitHub App secrets
([github-app.md](github-app.md)) and a persistent artifact store. That means
a paid instance with a disk, or object storage (S3/MinIO, not implemented
yet). An always-on instance is also needed: GitHub gives up on a webhook
after about 10 seconds, faster than a free service wakes up.

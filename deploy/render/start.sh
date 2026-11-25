#!/usr/bin/env bash
# Entrypoint of the Render container (deploy/render/Dockerfile): migrate,
# then run the API, the worker and nginx, and exit when any of them exits so
# Render restarts the whole service.
set -euo pipefail
cd /app

# Render supplies postgresql://...; SQLAlchemy needs the asyncpg driver named.
# The URL holds the password: never echo it.
: "${DATABASE_URL:?DATABASE_URL is not set (render.yaml links it from the database)}"
export SFCI_DATABASE_URL="postgresql+asyncpg://${DATABASE_URL#*://}"

# The free tier keeps artifacts on the container's own disk, which is empty
# after every restart; start the demo database empty too, so the two agree.
if [[ "${SFCI_DEMO_MODE:-false}" == "true" && "${SFCI_DEMO_RESET_ON_BOOT:-false}" == "true" ]]; then
  python deploy/reset_demo_db.py
fi
alembic upgrade head

sed "s/__PORT__/${PORT:-10000}/" /etc/sfci/nginx.conf.template > /tmp/nginx.conf

pids=()
stop() {
  kill -TERM "${pids[@]}" 2>/dev/null || true
  wait || true
}
trap 'stop; exit 0' TERM INT

uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log &
pids+=($!)
python -m app.worker &
pids+=($!)
nginx -e stderr -c /tmp/nginx.conf -g "daemon off;" &
pids+=($!)

set +e
wait -n
status=$?
echo "start.sh: a process exited (status ${status}); stopping the rest" >&2
stop
exit 1

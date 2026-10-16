"""Container healthchecks (no curl in the image).

    python -m app.healthcheck api      # GET /api/v1/health/ready on localhost
    python -m app.healthcheck worker   # heartbeat file updated recently
"""
import os
import sys
import time
import urllib.request

from app.config import get_settings


def check_api(port: int = 8000) -> bool:
    url = f"http://127.0.0.1:{port}/api/v1/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


def check_worker() -> bool:
    settings = get_settings()
    max_age = max(30.0, settings.worker_poll_interval_s * 5)
    try:
        age = time.time() - os.path.getmtime(settings.worker_heartbeat_path)
    except OSError:
        return False
    return age < max_age


def main(argv: list[str]) -> int:
    checks = {"api": check_api, "worker": check_worker}
    if len(argv) != 2 or argv[1] not in checks:
        print("usage: python -m app.healthcheck api|worker", file=sys.stderr)
        return 2
    return 0 if checks[argv[1]]() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

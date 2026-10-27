#!/usr/bin/env python3
"""Replay demo scenarios against a SiliconFlow instance in demo mode.

    python3 scripts/demo_replay.py                      # every scenario, in demo-script order
    python3 scripts/demo_replay.py clean_main tamper    # just these
    python3 scripts/demo_replay.py --speed 4            # four times faster

The signing happens inside the API (app.demo.replay): it holds the demo-only
OIDC key and webhook secret, made at startup and never written anywhere, and
posts each scenario's webhooks and run reports to its own real endpoints. This
script asks it to, through POST /api/v1/demo/scenarios/{name}, and prints what
each request was answered. An instance not in demo mode answers 404.

Standard library only.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

ORDER = [
    "history",
    "clean_main",
    "compile_error",
    "passed_not_releasable",
    "redelivery",
    "infra_hw",
    "tamper",
]


def call(api: str, method: str, path: str) -> object:
    request = urllib.request.Request(api.rstrip("/") + path, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("scenarios", nargs="*", default=ORDER, help=f"default: {' '.join(ORDER)}")
    p.add_argument("--api", default="http://localhost:8000", help="the API's base URL")
    p.add_argument("--speed", type=float, default=1.0, help="pacing multiplier (0.1 to 100)")
    args = p.parse_args(argv)

    try:
        meta = call(args.api, "GET", "/api/v1/meta")
    except (urllib.error.URLError, OSError) as exc:
        print(f"cannot reach {args.api}: {exc}", file=sys.stderr)
        return 2
    if not meta.get("demo_mode"):
        print(f"{args.api} is not in demo mode; refusing to replay sample inputs into it", file=sys.stderr)
        return 2

    for name in args.scenarios:
        try:
            call(args.api, "POST", f"/api/v1/demo/scenarios/{name}?speed={args.speed}")
        except urllib.error.HTTPError as exc:
            print(f"{name}: HTTP {exc.code} {exc.read().decode(errors='replace')}", file=sys.stderr)
            return 1
        print(f"{name}: replaying")
        shown = 0
        while True:
            time.sleep(0.5)
            scenario = next(s for s in call(args.api, "GET", "/api/v1/demo/scenarios") if s["name"] == name)
            for event in scenario["events"][shown:]:
                print(f"  {event['build']:<14} {event['step']:<24} HTTP {event['status_code']} {event['result']}")
            shown = len(scenario["events"])
            if scenario["state"] != "running":
                break
        if scenario["state"] != "done":
            print(f"{name}: {scenario['state']}: {scenario['error']}", file=sys.stderr)
            return 1
        builds = ", ".join(f"{k}={v}" for k, v in scenario["build_ids"].items())
        print(f"{name}: done ({builds})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

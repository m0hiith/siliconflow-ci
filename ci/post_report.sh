#!/usr/bin/env bash
# POST a run report to SiliconFlow, authenticated with a GitHub OIDC token.
#
#   ci/post_report.sh <report.json>
#
# The token is requested with audience $SFCI_OIDC_AUDIENCE (default
# "siliconflow"); the job needs `permissions: id-token: write`. The report goes
# to $SFCI_API_URL/api/v1/runs/report.
#
# Exit codes:
#   0  accepted, or nothing to do: no SFCI_API_URL, no OIDC token (fork pull
#      requests get none), or the build is already finished (409)
#   1  the backend rejected the report or stayed unreachable after retries
#   2  usage error
#
# Unknown builds (404: the run's webhook may not have arrived yet), 429 and
# 5xx are retried with backoff: $SFCI_POST_ATTEMPTS tries (default 8), waiting
# $SFCI_POST_RETRY_DELAY_S (default 2) doubling up to
# $SFCI_POST_RETRY_MAX_DELAY_S (default 30) between them, about two minutes
# in all. A report that never gets in errors the build, so the wait covers a
# backlog of webhooks, not just one in flight. The token is never printed.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: ci/post_report.sh <report.json>" >&2
    exit 2
fi
report=$1
if [[ ! -f $report ]]; then
    echo "post_report.sh: no such file: $report" >&2
    exit 2
fi

api_url=${SFCI_API_URL:-}
audience=${SFCI_OIDC_AUDIENCE:-siliconflow}
attempts=${SFCI_POST_ATTEMPTS:-8}
delay=${SFCI_POST_RETRY_DELAY_S:-2}
max_delay=${SFCI_POST_RETRY_MAX_DELAY_S:-30}

in_actions() { [[ ${GITHUB_ACTIONS:-} == true ]]; }
warn() {
    if in_actions; then echo "::warning title=run report::$*"; else echo "post_report.sh: $*" >&2; fi
}
fail() {
    if in_actions; then echo "::error title=run report::$*"; else echo "post_report.sh: $*" >&2; fi
    exit 1
}

if [[ -z $api_url ]]; then
    warn "SFCI_API_URL is not set; run report not sent"
    exit 0
fi
if [[ -z ${ACTIONS_ID_TOKEN_REQUEST_URL:-} || -z ${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-} ]]; then
    warn "no OIDC token available (missing 'id-token: write', or a fork pull request); run report not sent"
    exit 0
fi

# Headers carrying secrets go through private files, never the command line.
tmp=$(umask 077 && mktemp -d)
trap 'rm -rf "$tmp"' EXIT
(umask 077 && printf 'Authorization: bearer %s\n' "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" >"$tmp/request-auth")

if ! curl -sS --fail --max-time 30 --retry 3 -H @"$tmp/request-auth" \
    -o "$tmp/token.json" "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${audience}"; then
    fail "could not obtain an OIDC token"
fi
token=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["value"])' "$tmp/token.json") ||
    fail "OIDC token response had no 'value'"
rm -f "$tmp/token.json"
if in_actions; then echo "::add-mask::$token"; fi
(umask 077 && printf 'Authorization: Bearer %s\n' "$token" >"$tmp/api-auth")

url="${api_url%/}/api/v1/runs/report"
for ((attempt = 1; attempt <= attempts; attempt++)); do
    # Short timeouts: eight hung attempts must still fit in the job's timeout.
    code=$(curl -sS --connect-timeout 10 --max-time 30 -o "$tmp/response" -w '%{http_code}' \
        -X POST -H 'Content-Type: application/json' -H @"$tmp/api-auth" \
        --data-binary @"$report" "$url") || code=000
    body=$(head -c 2000 "$tmp/response" 2>/dev/null || true)
    case $code in
        200 | 201 | 202)
            echo "run report accepted ($code): $body"
            exit 0
            ;;
        409)
            warn "run report not recorded (409): $body"
            exit 0
            ;;
        000 | 404 | 429 | 5??)
            if ((attempt < attempts)); then
                wait_s=$((delay * 2 ** (attempt - 1)))
                ((wait_s > max_delay)) && wait_s=$max_delay
                echo "run report attempt $attempt/$attempts: HTTP $code $body; retrying in ${wait_s}s" >&2
                sleep "$wait_s"
            else
                echo "run report attempt $attempt/$attempts: HTTP $code $body" >&2
            fi
            ;;
        *)
            fail "run report rejected (HTTP $code): $body"
            ;;
    esac
done
fail "run report not delivered after $attempts attempts (last HTTP $code)"

#!/usr/bin/env bash
# Run one CI stage and record its outcome.
#
#   ci/stage.sh <name> -- <command...>
#
# - combined stdout/stderr is shown live and written to $OUT/logs/<name>.log
# - one JSON line is appended to $OUT/stages.jsonl
# - exits with the command's exit code
#
# $OUT is $SFCI_OUT_DIR, default "out". Skipped stages are not recorded here:
# a stage with no line is marked "skipped" by ci/report.py.
set -uo pipefail

usage() {
    echo "usage: ci/stage.sh <name> -- <command...>" >&2
    exit 2
}

[[ $# -ge 3 ]] || usage
name=$1
[[ $2 == "--" ]] || usage
shift 2

# Restricting the name keeps it safe as a file name and as a JSON string.
if [[ ! $name =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "stage.sh: invalid stage name '$name' (allowed: A-Z a-z 0-9 . _ -)" >&2
    exit 2
fi

out_dir=${SFCI_OUT_DIR:-out}
mkdir -p "$out_dir/logs" || exit 2
log="$out_dir/logs/$name.log"

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Workflow commands (log folding, annotations) only when running in Actions.
gh_cmd() { if [[ ${GITHUB_ACTIONS:-} == true ]]; then echo "::$*"; fi; }

started_at=$(utc_now)
gh_cmd "group::stage $name"
"$@" 2>&1 | tee "$log"
exit_code=${PIPESTATUS[0]}
gh_cmd "endgroup::"
finished_at=$(utc_now)

if [[ $exit_code -eq 0 ]]; then
    status=passed
else
    status=failed
fi

printf '{"name":"%s","status":"%s","exit_code":%d,"started_at":"%s","finished_at":"%s"}\n' \
    "$name" "$status" "$exit_code" "$started_at" "$finished_at" >>"$out_dir/stages.jsonl"

if [[ $exit_code -ne 0 ]]; then
    gh_cmd "error title=stage $name failed::exit code $exit_code (log: $log)"
fi
exit "$exit_code"

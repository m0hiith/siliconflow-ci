#!/usr/bin/env bash
# Collect build outputs into $OUT/artifacts and write manifest.json.
#
#   ci/package.sh <binary> [<binary>...]
#
# Each binary is copied as-is. If it is an ELF file and objcopy is available
# (override with $OBJCOPY), a raw <name>.bin image is written next to it
# (<name> without a .elf or .out suffix), unless a file with that name is
# itself one of the inputs, as when the SDK's build has already made it.
# $OUT is $SFCI_OUT_DIR, default "out".
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: ci/package.sh <binary> [<binary>...]" >&2
    exit 2
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
out_dir=${SFCI_OUT_DIR:-out}
art_dir="$out_dir/artifacts"
objcopy=${OBJCOPY:-objcopy}

# Start clean so a re-run never ships stale files.
rm -rf "$art_dir"
mkdir -p "$art_dir"

is_elf() { [[ $(head -c 4 "$1" | od -An -tx1 | tr -d ' \n') == 7f454c46 ]]; }

inputs=("$@")
is_input() {
    local src
    for src in "${inputs[@]}"; do [[ $(basename "$src") == "$1" ]] && return 0; done
    return 1
}

for src in "$@"; do
    if [[ ! -f $src ]]; then
        echo "package.sh: no such file: $src" >&2
        exit 1
    fi
    base=$(basename "$src")
    cp "$src" "$art_dir/$base"
    echo "packaged $base"

    if is_elf "$src"; then
        stem=${base%.elf}
        stem=${stem%.out}
        if is_input "$stem.bin"; then
            echo "package.sh: $stem.bin given as an input; not regenerating it"
        elif command -v "$objcopy" >/dev/null 2>&1; then
            "$objcopy" -O binary "$src" "$art_dir/$stem.bin"
            echo "packaged $stem.bin"
        else
            echo "package.sh: $objcopy not found; skipping .bin for $base"
        fi
    fi
done

python3 "$script_dir/manifest.py" "$art_dir"

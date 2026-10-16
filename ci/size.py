#!/usr/bin/env python3
"""Measure an ELF's memory footprint for the run report.

    python3 ci/size.py [--size-tool arm-none-eabi-size] build-m4/firmware.elf

Runs `<size-tool> -A -d` for the per-section breakdown and writes
$SFCI_OUT_DIR/size/size.json (default out/size/size.json), which ci/report.py
puts in the run report under "size".

`size -A` prints no section flags, so it cannot say which sections are text,
data or bss. The totals come from `<size-tool> -B -d` instead, which classifies
sections by their flags: text is allocated read-only sections with contents,
data is writable ones with contents, bss is allocated sections without (the
same "text" the backend computes from the stored ELF for the text_bytes_max
gate). The size tool defaults to $SIZE, then arm-none-eabi-size.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_TOOL = "arm-none-eabi-size"


class SizeError(Exception):
    pass


def run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SizeError(f"{cmd[0]}: {exc}") from exc
    if r.returncode != 0:
        raise SizeError(f"{' '.join(cmd)} exited {r.returncode}: {r.stderr.strip()}")
    return r.stdout


def parse_sysv(output: str) -> list[dict]:
    """Sections from `size -A -d`: a "<file>  :" line, a header, one line per
    section, then "Total"."""
    sections = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[0] in ("section", "Total"):
            continue
        name, size, addr = parts
        if not (size.isdigit() and addr.isdigit()):
            continue
        sections.append({"name": name, "size": int(size), "addr": int(addr)})
    return sections


def parse_berkeley(output: str) -> dict:
    """text/data/bss from `size -B -d` for a single file."""
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != 2 or lines[0].split()[:3] != ["text", "data", "bss"]:
        raise SizeError(f"unexpected Berkeley output: {output!r}")
    values = lines[1].split()
    try:
        text, data, bss = (int(v) for v in values[:3])
    except ValueError:
        raise SizeError(f"unexpected Berkeley output: {output!r}") from None
    return {"text_bytes": text, "data_bytes": data, "bss_bytes": bss}


def measure(elf: Path, tool: str) -> dict:
    totals = parse_berkeley(run([tool, "-B", "-d", str(elf)]))
    sections = parse_sysv(run([tool, "-A", "-d", str(elf)]))
    return {"file": elf.name, **totals, "sections": sections}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("elf", type=Path)
    p.add_argument("--size-tool", default=os.environ.get("SIZE") or DEFAULT_TOOL)
    p.add_argument("--out", default=os.environ.get("SFCI_OUT_DIR", "out"), type=Path)
    args = p.parse_args(argv)

    if not args.elf.is_file():
        print(f"size.py: no such file: {args.elf}", file=sys.stderr)
        return 1
    try:
        result = measure(args.elf, args.size_tool)
    except SizeError as exc:
        print(f"size.py: {exc}", file=sys.stderr)
        return 1

    output = args.out / "size" / "size.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    for s in result["sections"]:
        print(f"{s['name']:<24} {s['size']:>10}  0x{s['addr']:08x}")
    print(
        f"{result['file']}: text {result['text_bytes']}  data {result['data_bytes']}  "
        f"bss {result['bss_bytes']} bytes"
    )
    print(f"size written to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

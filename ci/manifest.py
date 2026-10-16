#!/usr/bin/env python3
"""Write <dir>/manifest.json listing every file in <dir> with size and SHA-256.

    python3 ci/manifest.py out/artifacts

The backend re-hashes every artifact and rejects any that disagree with this
manifest, so the manifest is a claim to verify, not a source of truth.
"""
import hashlib
import json
import sys
from pathlib import Path

SCHEMA = "sfci.artifact-manifest/1"
MANIFEST_NAME = "manifest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(art_dir: Path) -> dict:
    files = []
    for path in sorted(art_dir.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        files.append(
            {
                "file": path.relative_to(art_dir).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {"schema": SCHEMA, "files": files}


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: manifest.py <artifact-dir>", file=sys.stderr)
        return 2
    art_dir = Path(argv[1])
    if not art_dir.is_dir():
        print(f"manifest.py: not a directory: {art_dir}", file=sys.stderr)
        return 1
    manifest = build_manifest(art_dir)
    out = art_dir / MANIFEST_NAME
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    for entry in manifest["files"]:
        print(f"{entry['sha256']}  {entry['file']}  ({entry['size']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

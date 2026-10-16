import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "package.sh"

FAKE_ELF = b"\x7fELF" + b"\x02\x01\x01" + b"\x00" * 57 + b"payload"


def fake_objcopy(tmp_path):
    """An objcopy stand-in: `objcopy -O binary IN OUT` writes a marker to OUT."""
    path = tmp_path / "fake-objcopy"
    path.write_text('#!/bin/sh\nprintf "BIN:%s" "$(basename "$3")" > "$4"\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def run_package(tmp_path, *files, objcopy):
    env = dict(os.environ, SFCI_OUT_DIR=str(tmp_path / "out"), OBJCOPY=str(objcopy))
    return subprocess.run(
        ["bash", str(PACKAGE), *files], cwd=tmp_path, env=env, capture_output=True, text=True
    )


def manifest(tmp_path):
    return json.loads((tmp_path / "out" / "artifacts" / "manifest.json").read_text())


def test_elf_is_copied_with_bin_and_manifest(tmp_path):
    (tmp_path / "firmware.elf").write_bytes(FAKE_ELF)
    r = run_package(tmp_path, "firmware.elf", objcopy=fake_objcopy(tmp_path))
    assert r.returncode == 0, r.stderr

    art = tmp_path / "out" / "artifacts"
    assert (art / "firmware.elf").read_bytes() == FAKE_ELF
    assert (art / "firmware.bin").read_text() == "BIN:firmware.elf"

    m = manifest(tmp_path)
    assert m["schema"] == "sfci.artifact-manifest/1"
    by_name = {f["file"]: f for f in m["files"]}
    assert set(by_name) == {"firmware.elf", "firmware.bin"}
    for name, entry in by_name.items():
        data = (art / name).read_bytes()
        assert entry["size"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()


def test_extensionless_elf_gets_bin_suffix(tmp_path):
    (tmp_path / "firmware").write_bytes(FAKE_ELF)
    r = run_package(tmp_path, "firmware", objcopy=fake_objcopy(tmp_path))
    assert r.returncode == 0, r.stderr
    assert {f["file"] for f in manifest(tmp_path)["files"]} == {"firmware", "firmware.bin"}


def test_non_elf_gets_no_bin(tmp_path):
    (tmp_path / "firmware").write_bytes(b"\xcf\xfa\xed\xfe macho")
    r = run_package(tmp_path, "firmware", objcopy=fake_objcopy(tmp_path))
    assert r.returncode == 0, r.stderr
    assert [f["file"] for f in manifest(tmp_path)["files"]] == ["firmware"]


def test_missing_objcopy_is_not_fatal(tmp_path):
    (tmp_path / "firmware.elf").write_bytes(FAKE_ELF)
    r = run_package(tmp_path, "firmware.elf", objcopy=tmp_path / "no-such-objcopy")
    assert r.returncode == 0, r.stderr
    assert "skipping .bin" in r.stdout
    assert [f["file"] for f in manifest(tmp_path)["files"]] == ["firmware.elf"]


def test_missing_input_fails(tmp_path):
    r = run_package(tmp_path, "nope.elf", objcopy=fake_objcopy(tmp_path))
    assert r.returncode == 1
    assert "no such file" in r.stderr


def test_rerun_removes_stale_artifacts(tmp_path):
    (tmp_path / "old.elf").write_bytes(FAKE_ELF)
    (tmp_path / "new.elf").write_bytes(FAKE_ELF + b"2")
    objcopy = fake_objcopy(tmp_path)
    run_package(tmp_path, "old.elf", objcopy=objcopy)
    r = run_package(tmp_path, "new.elf", objcopy=objcopy)
    assert r.returncode == 0, r.stderr
    assert {f["file"] for f in manifest(tmp_path)["files"]} == {"new.elf", "new.bin"}


def test_out_suffix_elf_gets_stem_bin(tmp_path):
    (tmp_path / "app.out").write_bytes(FAKE_ELF)
    r = run_package(tmp_path, "app.out", objcopy=fake_objcopy(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "out" / "artifacts" / "app.bin").read_text() == "BIN:app.out"


def test_bin_given_as_input_is_not_regenerated(tmp_path):
    (tmp_path / "app.out").write_bytes(FAKE_ELF)
    (tmp_path / "app.bin").write_bytes(b"sdk image")
    (tmp_path / "app.rps").write_bytes(b"rps image")
    r = run_package(tmp_path, "app.out", "app.bin", "app.rps", objcopy=fake_objcopy(tmp_path))
    assert r.returncode == 0, r.stderr

    art = tmp_path / "out" / "artifacts"
    assert (art / "app.bin").read_bytes() == b"sdk image"
    assert {f["file"] for f in manifest(tmp_path)["files"]} == {"app.out", "app.bin", "app.rps"}

#!/usr/bin/env python3
"""Build the public countersyncd Cargo workspace as one cached Bazel action."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def absolute(path: str) -> Path:
    # Keep Bazel's input symlinks in their lexical package namespace.
    return Path(os.path.abspath(path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-date-epoch", required=True)
    args = parser.parse_args()

    source_root = absolute(args.source_root)
    output = absolute(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="swss-cargo-", dir=output.parent) as temporary:
        work = Path(temporary)
        source = work / "source"
        for value in sorted(args.source):
            input_file = absolute(value)
            relative = input_file.relative_to(source_root)
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(input_file, destination)
            destination.chmod(input_file.stat().st_mode & 0o777)
        for required in ("Cargo.toml", "Cargo.lock", "crates/countersyncd/Cargo.toml"):
            if not (source / required).is_file():
                raise ValueError(f"missing Cargo input: {required}")
        vendor_archive = source / ".sonic-bazel-cargo-vendor.tar"
        if not vendor_archive.is_file() or not (source / ".cargo/config.toml").is_file():
            raise ValueError("missing declared offline Cargo vendor inputs")
        (source / "vendor").mkdir()
        with tarfile.open(vendor_archive, mode="r:") as archive:
            for member in archive:
                relative = Path(member.name)
                if not member.isfile() or relative.is_absolute() or not relative.parts or ".." in relative.parts or relative.parts[0] != "vendor":
                    raise ValueError(f"invalid Cargo vendor archive entry: {member.name}")
                extracted = archive.extractfile(member)
                assert extracted is not None
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as stream:
                    shutil.copyfileobj(extracted, stream)
                destination.chmod(member.mode & 0o777)
        vendor_archive.unlink()

        environment = os.environ.copy()
        environment.update(
            CARGO_HOME=str(work / "cargo-home"),
            CARGO_TARGET_DIR=str(work / "target"),
            CARGO_INCREMENTAL="0",
            LC_ALL="C.UTF-8",
            SOURCE_DATE_EPOCH=args.source_date_epoch,
            TZ="UTC",
        )
        (work / "cargo-home").mkdir()
        rustflags = environment.get("RUSTFLAGS", "")
        environment["RUSTFLAGS"] = (
            f"{rustflags} --remap-path-prefix={source}=.".strip()
        )
        subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "--locked",
                "--offline",
                "--bin",
                "countersyncd",
                "--manifest-path",
                str(source / "Cargo.toml"),
            ],
            cwd=source,
            env=environment,
            check=True,
        )
        binary = work / "target/release/countersyncd"
        if not binary.is_file() or binary.read_bytes()[:4] != b"\x7fELF":
            raise ValueError("Cargo did not produce an ELF countersyncd binary")
        shutil.copyfile(binary, output)
        output.chmod(0o755)


if __name__ == "__main__":
    main()

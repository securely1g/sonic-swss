#!/usr/bin/env python3
"""Stage Bazel outputs and run the existing SWSS Debian binary package flow."""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any


STAGE_MANIFEST = ".sonic-bazel-stage.json"


def absolute(path: str) -> Path:
    # Do not resolve Bazel input symlinks out of their lexical package namespace.
    return Path(os.path.abspath(path))


def relative_path(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"invalid relative package path: {value}")
    return Path(*path.parts)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get("schema_version") != 1:
        raise ValueError(f"unsupported manifest schema: {path}")
    return value


def copy_file(source: Path, destination: Path, mode: int) -> None:
    if not source.is_file():
        raise ValueError(f"missing package input: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def require_elf(path: Path, require_debug: bool = False) -> None:
    with path.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise ValueError(f"expected a Bazel-built ELF executable: {path}")
    if require_debug:
        sections = subprocess.check_output(["readelf", "--sections", "--wide", str(path)], text=True)
        if ".debug_info" not in sections:
            raise ValueError(f"native binary has no DWARF debug information; build Bazel with --strip=never: {path}")


def check_stage(stage: Path) -> list[dict[str, Any]]:
    manifest = read_json(stage / STAGE_MANIFEST)
    entries = manifest.get("files", [])
    expected: dict[str, dict[str, Any]] = {}
    for entry in entries:
        relative = relative_path(entry["path"]).as_posix()
        if relative in expected:
            raise ValueError(f"duplicate staged path: {relative}")
        expected[relative] = entry
    found = set()
    for path in stage.rglob("*"):
        relative = path.relative_to(stage).as_posix()
        if path.is_symlink():
            raise ValueError(f"unexpected staged symlink: {relative}")
        if path.is_file() and relative != STAGE_MANIFEST:
            found.add(relative)
    if found != set(expected):
        raise ValueError(
            f"staged file inventory differs: missing={sorted(set(expected) - found)}, "
            f"extra={sorted(found - set(expected))}"
        )
    for relative, entry in expected.items():
        path = stage / relative
        if digest(path) != entry["sha256"]:
            raise ValueError(f"staged input changed: {relative}")
        if stat.S_IMODE(path.stat().st_mode) != entry["mode"]:
            raise ValueError(f"staged mode changed: {relative}")
    return entries


def install_stage(stage: Path, destination: Path) -> None:
    for entry in check_stage(stage):
        relative = relative_path(entry["path"])
        copy_file(stage / relative, destination / relative, entry["mode"])


def package_contents(path: Path, inspect_debug: bool = False) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    control_text = subprocess.check_output(["dpkg-deb", "--field", str(path)], text=True)
    control = dict(Parser().parsestr(control_text).items())
    files: dict[str, Any] = {}
    payloads: dict[str, dict[str, str]] = {}
    has_debug_info = False
    process = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", str(path)], stdout=subprocess.PIPE)
    assert process.stdout is not None
    with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
        for member in archive:
            name = member.name.removeprefix("./").rstrip("/")
            if not name or name == ".":
                continue
            relative_path(name)
            if member.uid != 0 or member.gid != 0:
                raise ValueError(f"package entry is not owned by root: {name}")
            files[name] = {
                "mode": member.mode,
                "type": "file" if member.isfile() else "other",
            }
            if member.isfile():
                extracted = archive.extractfile(member)
                assert extracted is not None
                checksum = hashlib.sha256()
                prefix = extracted.read(4)
                checksum.update(prefix)
                inspect_member = inspect_debug and not has_debug_info and prefix == b"\x7fELF"
                with tempfile.TemporaryFile(dir=path.parent) if inspect_member else open(os.devnull, "wb") as debug_file:
                    if inspect_member:
                        debug_file.write(prefix)
                    for block in iter(lambda: extracted.read(1024 * 1024), b""):
                        checksum.update(block)
                        if inspect_member:
                            debug_file.write(block)
                    if inspect_member:
                        debug_file.flush()
                        sections = subprocess.check_output(
                            ["readelf", "--sections", "--wide", f"/proc/self/fd/{debug_file.fileno()}"],
                            pass_fds=(debug_file.fileno(),),
                            text=True,
                        )
                        has_debug_info = ".debug_info" in sections
                payloads[name] = {"sha256": checksum.hexdigest(), "prefix": prefix.hex()}
    process.stdout.close()
    if process.wait() != 0:
        raise ValueError(f"could not read package payload: {path}")
    return {"control": control, "files": files, "has_debug_info": has_debug_info}, payloads


def build(args: argparse.Namespace) -> None:
    inventory = read_json(absolute(args.inventory))
    source_root = absolute(args.source_root)
    outputs = {
        "swss": absolute(args.swss_output),
        "swss-dbg": absolute(args.debug_output),
    }
    manifest_output = absolute(args.manifest_output)
    for output in [*outputs.values(), manifest_output]:
        output.parent.mkdir(parents=True, exist_ok=True)

    binaries: dict[str, Path] = {}
    for value in args.binary:
        install_path, separator, file_path = value.partition("=")
        if not separator or install_path in binaries:
            raise ValueError(f"invalid or duplicate binary mapping: {value}")
        binaries[relative_path(install_path).as_posix()] = absolute(file_path)
    expected_binaries = {entry["install_path"] for entry in inventory["programs"]}
    if set(binaries) != expected_binaries:
        raise ValueError("Bazel program outputs differ from the configured Automake inventory")

    with tempfile.TemporaryDirectory(prefix="swss-debian-", dir=manifest_output.parent) as temporary:
        work = Path(temporary)
        source = work / "source"
        copied_sources = set()
        for value in sorted(args.source):
            input_file = absolute(value)
            relative = input_file.relative_to(source_root)
            copy_file(input_file, source / relative, stat.S_IMODE(input_file.stat().st_mode))
            copied_sources.add(relative.as_posix())
        if copied_sources != set(inventory["package_source_files"]):
            raise ValueError("Debian action inputs differ from the generated package inventory")

        stage = work / "stage"
        stage_files = []
        for install_path, input_file in sorted(binaries.items()):
            require_elf(input_file, require_debug=True)
            destination = stage / relative_path(install_path)
            copy_file(input_file, destination, 0o755)
            stage_files.append({"path": install_path, "mode": 0o755, "sha256": digest(destination)})
        for entry in inventory["automake_install"]:
            destination = stage / relative_path(entry["install_path"])
            if destination.exists():
                raise ValueError(f"duplicate package install path: {entry['install_path']}")
            copy_file(source / relative_path(entry["source"]), destination, entry["mode"])
            stage_files.append(
                {"path": entry["install_path"], "mode": entry["mode"], "sha256": digest(destination)}
            )
        stage.mkdir(parents=True, exist_ok=True)
        (stage / STAGE_MANIFEST).write_text(
            json.dumps({"schema_version": 1, "files": sorted(stage_files, key=lambda item: item["path"])}, indent=2)
            + "\n"
        )
        check_stage(stage)

        cargo = absolute(args.cargo)
        require_elf(cargo)
        copy_file(cargo, source / relative_path(inventory["cargo"]["package_source"]), 0o755)
        environment = os.environ.copy()
        environment.update(
            DEB_BUILD_OPTIONS=inventory["deb_build_options"],
            LC_ALL="C.UTF-8",
            SONIC_BAZEL_STAGEDIR=str(stage),
            SOURCE_DATE_EPOCH=str(inventory["source_date_epoch"]),
            TZ="UTC",
        )
        subprocess.run(
            ["dpkg-buildpackage", "-b", "-uc", "-us", "-nc"],
            cwd=source,
            env=environment,
            check=True,
        )

        required_paths = {
            entry["install_path"]: {"mode": 0o755, "binary": True}
            for entry in inventory["programs"]
        }
        for entry in inventory["automake_install"] + inventory["debhelper_install"]:
            required_paths[entry["install_path"]] = {
                "mode": entry["mode"],
                "binary": entry.get("kind") == "cargo",
                "source": entry["source"],
            }

        result = {"schema_version": 1, "packages": []}
        for package in inventory["packages"]:
            package_file = work / package["filename"]
            if not package_file.is_file():
                raise ValueError(f"debhelper did not produce {package['filename']}")
            contents, payloads = package_contents(package_file, inspect_debug=package["name"] == "swss-dbg")
            control = contents["control"]
            for field, expected in (
                ("Package", package["name"]),
                ("Version", package["version"]),
                ("Architecture", package["architecture"]),
            ):
                if control.get(field) != expected:
                    raise ValueError(f"unexpected {field} in {package['filename']}: {control.get(field)}")
            if package["name"] == "swss":
                if not control.get("Depends"):
                    raise ValueError("SWSS package has no computed shared-library dependencies")
                for install_path, expected in required_paths.items():
                    member = contents["files"].get(install_path)
                    if member != {"mode": expected["mode"], "type": "file"}:
                        raise ValueError(f"missing or incorrectly installed package file: {install_path}")
                    if expected["binary"]:
                        if payloads[install_path]["prefix"] != b"\x7fELF".hex():
                            raise ValueError(f"package binary is not ELF: {install_path}")
                    elif payloads[install_path]["sha256"] != digest(source / expected["source"]):
                        raise ValueError(f"package data differs from the declared input: {install_path}")
            else:
                if f"swss (= {package['version']})" not in control.get("Depends", ""):
                    raise ValueError("SWSS debug package does not depend on the matching SWSS version")
                if not any(name.startswith("usr/lib/debug/") for name in payloads) or not contents["has_debug_info"]:
                    raise ValueError("SWSS debug package contains no DWARF debug information")
            copy_file(package_file, outputs[package["name"]], 0o644)
            result["packages"].append(
                {
                    **package,
                    "sha256": digest(package_file),
                    "depends": control.get("Depends", ""),
                    "has_debug_info": contents["has_debug_info"],
                    "files": contents["files"],
                }
            )
        manifest_output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--inventory", required=True)
    build_parser.add_argument("--source-root", required=True)
    build_parser.add_argument("--source", action="append", default=[])
    build_parser.add_argument("--binary", action="append", default=[])
    build_parser.add_argument("--cargo", required=True)
    build_parser.add_argument("--swss-output", required=True)
    build_parser.add_argument("--debug-output", required=True)
    build_parser.add_argument("--manifest-output", required=True)
    for name in ("check-stage", "install-stage"):
        command = commands.add_parser(name)
        command.add_argument("--stage", required=True)
        if name == "install-stage":
            command.add_argument("--destination", required=True)
    args = parser.parse_args()
    if args.command == "build":
        build(args)
    elif args.command == "check-stage":
        check_stage(absolute(args.stage))
    else:
        install_stage(absolute(args.stage), absolute(args.destination))


if __name__ == "__main__":
    main()

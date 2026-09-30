#!/usr/bin/env python3
"""Validate SWSS runtime tar contents and matching detached ELF debug files."""

import argparse
import ast
import json
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import tarfile
import tempfile
import zlib


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_contract(source):
    values = {}
    for node in ast.parse((source / "bazel/production_sources.bzl").read_text()).body:
        if isinstance(node, ast.Assign):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    expected = {p["install_path"]: (0o755, None) for p in values["SWSS_PROGRAMS"].values()}
    for item in values["SWSS_AUTOMAKE_INSTALL"]:
        expected[item["install_path"]] = (item["mode"], source / item["source"])
    for line in (source / "debian/swss.install").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        origin, directory = line.split()
        path = str(PurePosixPath(directory) / PurePosixPath(origin).name)
        require(path not in expected, "duplicate install path: " + path)
        expected[path] = (0o755 if directory == "usr/bin" else 0o644,
                          None if origin == "target/release/countersyncd" else source / origin)
    return expected


def read_archive(path):
    files = {}
    with tarfile.open(path) as archive:
        for member in archive:
            name = str(PurePosixPath(member.name))
            require(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts,
                    "unsafe archive path: " + name)
            require(member.uid == 0 and member.gid == 0, "non-root owner: " + name)
            if member.isdir():
                require(member.mode == 0o755, "directory permissions: " + name)
                continue
            require(member.isfile(), "unexpected archive entry type: " + name)
            require(name not in files, "duplicate archive member: " + name)
            files[name] = (member.mode, archive.extractfile(member).read())
    return files


def command(*args):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    require(result.returncode == 0, "command failed: " + repr(args) + "\n" + result.stdout)
    return result.stdout


def build_id(path):
    match = re.search(r"Build ID: ([0-9a-f]+)", command("readelf", "-n", str(path)))
    require(match is not None, "missing build ID: " + str(path))
    return match[1]


def verify(source, runtime_tar, debug_tar, architecture):
    expected = read_contract(source)
    runtime = read_archive(runtime_tar)
    debug = read_archive(debug_tar)
    require(set(runtime) == set(expected),
            "runtime install mismatch: missing=" + repr(sorted(set(expected) - set(runtime))) +
            " extra=" + repr(sorted(set(runtime) - set(expected))))
    machine = {"amd64": 62, "arm64": 183}[architecture]
    pairs = []
    used_debug = set()
    with tempfile.TemporaryDirectory(prefix="swss-package-check-") as temporary:
        staging = Path(temporary)
        for path, (mode, origin) in expected.items():
            actual_mode, data = runtime[path]
            require(actual_mode == mode, "runtime permissions: " + path)
            if origin is not None:
                require(data == origin.read_bytes(), "installed data differs from source: " + path)
                continue
            require(data[:6] == b"\x7fELF\x02\x01", "expected little-endian ELF64: " + path)
            require(struct.unpack_from("<H", data, 18)[0] == machine, "wrong ELF architecture: " + path)
            binary = staging / path
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(data)
            identifier = build_id(binary)
            debug_path = "usr/lib/debug/.build-id/" + identifier[:2] + "/" + identifier[2:] + ".debug"
            require(debug_path in debug, "missing detached debug file: " + path)
            debug_data = debug[debug_path][1]
            symbols = staging / debug_path
            symbols.parent.mkdir(parents=True, exist_ok=True)
            symbols.write_bytes(debug_data)
            require(build_id(symbols) == identifier, "runtime/debug build IDs differ: " + path)
            require(".debug_info" not in command("readelf", "-SW", str(binary)), "runtime still contains debug info: " + path)
            require(".debug_info" in command("readelf", "-SW", str(symbols)), "debug file has no source information: " + path)
            link = staging / "debuglink"
            command("objcopy", "--dump-section", ".gnu_debuglink=" + str(link), str(binary))
            raw = link.read_bytes()
            end = raw.index(0)
            require(raw[:end].decode() == symbols.name, "debuglink filename: " + path)
            crc = struct.unpack_from("<I", raw, (end + 4) & ~3)[0]
            require(crc == zlib.crc32(debug_data), "debuglink checksum: " + path)
            used_debug.add(debug_path)
            pairs.append({"path": path, "build_id": identifier, "debug_path": debug_path})
        require(set(debug) == used_debug, "unexpected detached debug files")
        lookups = []
        for executable, symbol in (("orchagent", "main"), ("countersyncd", "countersyncd::main")):
            output = command("gdb", "--batch", "-nx", "-nh",
                             "-ex", "set debuginfod enabled off",
                             "-ex", "set auto-load off",
                             "-ex", "set debug-file-directory " + str(staging / "usr/lib/debug"),
                             "-ex", "file " + str(staging / "usr/bin" / executable),
                             "-ex", "info line " + symbol,
                             "-ex", "python print('SWSS_DEBUG_FILES=' + repr([f.filename for f in gdb.objfiles()]))")
            pair = next(item for item in pairs if item["path"] == "usr/bin/" + executable)
            require(str(staging / pair["debug_path"]) in output,
                    "GDB did not load the packaged symbols: " + output)
            require(re.search(r'Line [1-9][0-9]* of "[^"]+"', output), "GDB source-line lookup failed: " + output)
            lookups.append({"program": executable, "source_line": output.strip()})
    return {"architecture": architecture, "installed_files": len(runtime),
            "elf_pairs": pairs, "gdb": lookups}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--debug", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.source, args.runtime, args.debug, args.architecture)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()

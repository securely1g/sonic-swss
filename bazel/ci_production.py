#!/usr/bin/env python3
"""Build and inspect the production C++ graph used by SWSS CI."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_MAP = ROOT / "bazel/production_sources.bzl"
AGGREGATE = "//dist:cpp_binaries"
GCOV_PRELOAD_TEST = "//gcovpreload:gcovpreload_test"
CODEQL_COMPILE_TARGETS = [GCOV_PRELOAD_TEST]
NATIVE_TEST_TARGETS = [GCOV_PRELOAD_TEST]
CODEQL_TEST_SOURCES = {"gcovpreload/gcovpreload_test.cpp"}
CODEQL_FEATURE_SOURCES = {"gcovpreload/gcovpreload.cpp"}
FEATURE_SOURCE_BOUNDARIES = CODEQL_FEATURE_SOURCES | {"lib/asan.cpp", "lib/asan_ctor.cpp"}
NATIVE_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx")
ARCHITECTURES = {
    "amd64": ("x86_64", 62, "@sonic_build_infra//platforms:x86_64_trixie"),
    "arm64": ("aarch64", 183, "@sonic_build_infra//platforms:aarch64_trixie"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError(f"expected a normalized relative path: {value!r}")
    return value


def load_programs() -> dict[str, dict[str, Any]]:
    contents = SOURCE_MAP.read_text()
    matches = list(re.finditer(r"(?m)^SWSS_PROGRAMS = ", contents))
    if len(matches) != 1:
        raise ValueError("production_sources.bzl must contain one SWSS_PROGRAMS JSON literal")
    programs, _ = json.JSONDecoder().raw_decode(contents[matches[0].end():])
    if not isinstance(programs, dict) or not programs:
        raise ValueError("SWSS_PROGRAMS must be a nonempty object")
    for name, program in programs.items():
        if not re.fullmatch(r"[A-Za-z0-9_+.-]+", name) or not isinstance(program, dict):
            raise ValueError(f"invalid SWSS program: {name!r}")
        if not isinstance(program.get("directory"), str):
            raise ValueError(f"missing directory for {name}")
        relative_path(program["directory"])
        sources = program.get("sources")
        if not isinstance(sources, list) or not sources or not all(isinstance(item, str) for item in sources):
            raise ValueError(f"missing source labels for {name}")
        if len(sources) != len(set(sources)):
            raise ValueError(f"duplicate source labels for {name}")
    return programs


def program_labels(programs: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        name: f"//{'' if program['directory'] == '.' else program['directory']}:{name}"
        for name, program in sorted(programs.items())
    }


def tracked_paths(paths: set[str]) -> set[str]:
    for path in paths:
        relative_path(path)
        source = ROOT / path
        if not source.is_file() or not source.resolve().is_relative_to(ROOT):
            raise ValueError(f"source is missing or leaves the checkout: {path}")
    tracked = set(subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0"))
    missing = sorted(paths - tracked)
    if missing:
        raise ValueError(f"sources are not tracked by Git: {missing}")
    return paths


def tracked_source_paths(programs: dict[str, dict[str, Any]]) -> set[str]:
    paths = set()
    for program in programs.values():
        for label in program["sources"]:
            match = re.fullmatch(r"//([^:]*):(.+)", label)
            if match is None:
                raise ValueError(f"production source must be a local file label: {label}")
            package, name = match.groups()
            path = relative_path((PurePosixPath(package) / name).as_posix())
            paths.add(path)
    return tracked_paths(paths)


def tracked_native_source_paths() -> set[str]:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    return {path for path in tracked if path.endswith(NATIVE_SOURCE_SUFFIXES)}


def inspect_elf(path: Path, machine: int) -> dict[str, Any]:
    file_stat = path.stat()
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size < 64 or not file_stat.st_mode & 0o111:
        raise ValueError(f"output is not a nonempty executable file: {path}")
    with path.open("rb") as stream:
        header = stream.read(64)
        if header[:7] != b"\x7fELF\x02\x01\x01":
            raise ValueError(f"output is not a little-endian ELF64 file: {path}")
        fields = struct.unpack("<HHIQQQIHHHHHH", header[16:])
        elf_type, elf_machine = fields[:2]
        program_offset, header_size, entry_size, entry_count = fields[4], fields[7], fields[8], fields[9]
        if elf_machine != machine or elf_type != 3:
            raise ValueError(f"output is not a PIE for machine {machine}: {path}")
        if header_size != 64 or entry_size < 56 or not entry_count or program_offset + entry_size * entry_count > file_stat.st_size:
            raise ValueError(f"invalid ELF program header table: {path}")
        program_types = set()
        dynamic_segments = []
        for index in range(entry_count):
            stream.seek(program_offset + index * entry_size)
            entry = struct.unpack("<IIQQQQQQ", stream.read(56))
            program_types.add(entry[0])
            if entry[0] == 2:  # PT_DYNAMIC
                dynamic_segments.append((entry[2], entry[5]))
        if len(dynamic_segments) != 1:
            raise ValueError(f"output must have one ELF dynamic segment: {path}")
        dynamic_offset, dynamic_size = dynamic_segments[0]
        if dynamic_size % 16 or dynamic_offset + dynamic_size > file_stat.st_size:
            raise ValueError(f"invalid ELF dynamic segment: {path}")
        stream.seek(dynamic_offset)
        dynamic = {}
        for _ in range(dynamic_size // 16):
            tag, value = struct.unpack("<qQ", stream.read(16))
            if tag == 0:  # DT_NULL
                break
            dynamic[tag] = dynamic.get(tag, 0) | value
    if 1 not in program_types or 3 not in program_types:
        raise ValueError(f"output does not have PIE program headers: {path}")
    if 0x6474E552 not in program_types:
        raise ValueError(f"output is missing GNU_RELRO: {path}")
    # DT_BIND_NOW, DF_BIND_NOW in DT_FLAGS, or DF_1_NOW in DT_FLAGS_1.
    if 24 not in dynamic and not dynamic.get(30, 0) & 0x8 and not dynamic.get(0x6FFFFFFB, 0) & 0x1:
        raise ValueError(f"output is missing immediate binding (BIND_NOW): {path}")
    return {
        "elf_class": 64,
        "elf_machine": elf_machine,
        "elf_type": "ET_DYN",
        "pie": True,
        "gnu_relro": True,
        "bind_now": True,
        "mode": oct(stat.S_IMODE(file_stat.st_mode)),
        "size": file_stat.st_size,
        "sha256": sha256(path),
    }


def parse_cquery(contents: str, labels: set[str]) -> dict[str, list[str]]:
    outputs = {}
    for line in contents.splitlines():
        fields = line.split("\t")
        label = fields[0]
        if label.startswith("@@//"):
            label = label[2:]
        elif label.startswith("@//"):
            label = label[1:]
        if label not in labels or label in outputs:
            raise ValueError(f"unexpected or duplicate cquery label: {label}")
        outputs[label] = [relative_path(item) for item in fields[1:] if item]
    if set(outputs) != labels:
        raise ValueError(f"cquery did not return all requested labels: {sorted(labels - set(outputs))}")
    return outputs


def run(command: list[str], *, capture: bool = False, log: Path | None = None) -> str:
    display_command = "+ " + shlex.join(command)
    print(display_command, flush=True)
    if log is not None:
        if capture:
            raise ValueError("captured commands cannot also write a build log")
        with log.open("w") as stream:
            stream.write(display_command + "\n")
            process = subprocess.Popen(
                command, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                stream.write(line)
            returncode = process.wait()
        if returncode:
            raise subprocess.CalledProcessError(returncode, command)
        return ""
    result = subprocess.run(command, cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout or ""


def build(args: argparse.Namespace) -> None:
    host, machine, target_platform = ARCHITECTURES[args.architecture]
    if platform.system() != "Linux" or platform.machine() != host:
        raise ValueError(f"{args.architecture} CI requires a native Linux {host} runner")
    os_release = platform.freedesktop_os_release()
    if os_release.get("ID") != "debian" or os_release.get("VERSION_CODENAME") != "trixie":
        raise ValueError("production CI requires Debian Trixie")
    manifest = args.manifest.resolve(strict=True)
    if not manifest.is_file():
        raise ValueError(f"CI DEB manifest is not a file: {manifest}")
    native_contract = None
    if args.mode != "codeql":
        if args.native_contract is None:
            raise ValueError("native builds require --native-contract")
        native_contract = args.native_contract.resolve(strict=True)
        if not native_contract.is_file():
            raise ValueError(f"native build contract is not a file: {native_contract}")
    artifact_directory = args.artifact_directory.resolve()
    if artifact_directory.exists() and any(artifact_directory.iterdir()):
        raise ValueError(f"artifact directory must be empty: {artifact_directory}")
    artifact_directory.mkdir(parents=True, exist_ok=True)

    programs = load_programs()
    sources = tracked_source_paths(programs)
    labels = program_labels(programs)
    source_map_sha256 = sha256(SOURCE_MAP)
    manifest_sha256 = sha256(manifest)
    native_contract_sha256 = sha256(native_contract) if native_contract is not None else None
    bazel_version = (ROOT / ".bazelversion").read_text().strip()
    if run([args.bazel, "--version"], capture=True).strip() != f"bazel {bazel_version}":
        raise ValueError(f"CI requires Bazel {bazel_version}")
    options = [
        "--config=release",
        "--config=ci-debs",
        "--lockfile_mode=off",
        f"--platforms={target_platform}",
        f"--repo_env=SONIC_SWSS_CI_DEBS_MANIFEST={manifest}",
        "--noshow_progress",
        "--color=no",
        "--curses=no",
    ]
    bazel = [args.bazel]
    fresh_output_base = args.mode in ("clean", "codeql")
    if fresh_output_base:
        output_base = tempfile.mkdtemp(prefix=f"swss-{args.mode}-bazel-", dir=os.environ.get("RUNNER_TEMP"))
        bazel += [f"--output_base={output_base}", "--batch"]
        options += [
            "--nouse_action_cache",
            "--noremote_accept_cached",
            "--noremote_upload_local_results",
            "--disk_cache=",
            "--remote_cache=",
            "--remote_executor=",
        ]
    if args.mode == "codeql":
        options += ["--jobs=2", "--spawn_strategy=local"]
    else:
        options += ["--jobs=4"]
        if args.mode == "clean":
            options += ["--spawn_strategy=processwrapper-sandbox"]

    requested = list(labels.values()) + [AGGREGATE]
    additional_compile_targets = CODEQL_COMPILE_TARGETS if args.mode == "codeql" else []
    build_log = artifact_directory / "build.log"
    run(bazel + ["build"] + options + requested + additional_compile_targets, log=build_log)

    if args.mode == "codeql":
        runtime_tests = {
            "targets": [],
            "status": "not_run",
            "reason": "CodeQL compiles the selected GCOV preload test for extraction without executing it.",
        }
    else:
        native_test_log = artifact_directory / "native-tests.log"
        run(
            bazel + ["test"] + options + ["--nocache_test_results", "--test_output=errors"] + NATIVE_TEST_TARGETS,
            log=native_test_log,
        )
        runtime_tests = {
            "targets": NATIVE_TEST_TARGETS,
            "status": "passed",
            "output_base_and_action_cache_policy": "same_as_build",
            "test_result_reuse": "disabled",
            "log": {"artifact": native_test_log.name, "sha256": sha256(native_test_log)},
        }

    module_graph = run(
        bazel + [
            "mod", "graph", "--lockfile_mode=off",
            "--output=json", "--verbose", "--noshow_progress", "--color=no", "--curses=no",
        ],
        capture=True,
    )
    module_graph_data = json.loads(module_graph)
    if not isinstance(module_graph_data, dict) or not module_graph_data:
        raise ValueError("Bazel returned an empty or invalid resolved module graph")
    module_graph_path = artifact_directory / "module-graph.json"
    module_graph_path.write_text(json.dumps(module_graph_data, indent=2, sort_keys=True) + "\n")

    expression = "set(" + " ".join(requested) + ")"
    configured_rules_path = None
    native_contract_path = None
    if native_contract is not None:
        from compare_build_contract import cquery_expression
        from generate import MODULE_BUILD_MAPPINGS

        universe_scope = list(labels.values())
        query = cquery_expression(programs, MODULE_BUILD_MAPPINGS)
        configured_rules = json.loads(run(
            bazel + ["cquery"] + options + [
                query,
                "--universe_scope=" + ",".join(universe_scope),
                "--noimplicit_deps", "--notool_deps", "--consistent_labels", "--output=jsonproto",
            ],
            capture=True,
        ))
        if not isinstance(configured_rules, dict) or not configured_rules.get("results"):
            raise ValueError("Bazel returned empty or invalid configured production rules")
        repository_mapping = json.loads(run(
            bazel + [
                "mod", "dump_repo_mapping", "", "--lockfile_mode=off",
                "--noshow_progress", "--color=no", "--curses=no",
            ],
            capture=True,
        ))
        if not isinstance(repository_mapping, dict):
            raise ValueError("Bazel returned an invalid root repository mapping")
        configured_rules_path = artifact_directory / "configured-rules.json"
        configured_rules_path.write_text(json.dumps({
            "schema_version": 1,
            "query": query,
            "universe_scope": universe_scope,
            "root_repository_mapping": repository_mapping,
            "cquery": configured_rules,
        }, separators=(",", ":")) + "\n")
        native_contract_path = artifact_directory / "native-build-contract.json"
        shutil.copy2(native_contract, native_contract_path)

    output = run(
        bazel + ["cquery"] + options + [
            expression,
            "--output=starlark",
            '--starlark:expr=str(target.label) + "\\t" + "\\t".join([file.path for file in target.files.to_list()])',
        ],
        capture=True,
    )
    outputs = parse_cquery(output, set(requested))
    program_outputs = []
    for name, label in labels.items():
        if len(outputs[label]) != 1:
            raise ValueError(f"{label} must produce one executable, found {outputs[label]}")
        program_outputs.append(outputs[label][0])
    if len(program_outputs) != len(set(program_outputs)) or sorted(outputs[AGGREGATE]) != sorted(program_outputs):
        raise ValueError("//dist:cpp_binaries outputs do not match the explicit production programs")
    if sha256(SOURCE_MAP) != source_map_sha256 or sha256(manifest) != manifest_sha256:
        raise ValueError("the production source map or CI DEB manifest changed during the build")
    if native_contract is not None and (
        sha256(native_contract) != native_contract_sha256 or sha256(native_contract_path) != native_contract_sha256
    ):
        raise ValueError("the native build contract changed during the build")

    receipts = []
    if args.mode != "codeql":
        (artifact_directory / "bin").mkdir()
    for name, label in labels.items():
        output_path = outputs[label][0]
        receipt = {"name": name, "label": label, "bazel_output": output_path}
        receipt.update(inspect_elf(ROOT / output_path, machine))
        if args.mode != "codeql":
            destination = artifact_directory / "bin" / name
            shutil.copy2(ROOT / output_path, destination)
            if sha256(destination) != receipt["sha256"]:
                raise ValueError(f"artifact copy does not match {label}")
            receipt["artifact"] = f"bin/{name}"
        receipts.append(receipt)

    receipt = {
        "schema_version": 2,
        "artifact_type": "cpp_executables",
        "build_mode": args.mode,
        "cache_policy": {
            "fresh_output_base": fresh_output_base,
            "action_result_reuse": "disabled" if fresh_output_base else "allowed",
            "repository_download_reuse": "allowed",
            "spawn_strategy": {
                "codeql": "local",
                "clean": "processwrapper-sandbox",
                "normal": "native_default",
            }[args.mode],
        },
        "architecture": args.architecture,
        "target_platform": target_platform,
        "bazel_version": bazel_version,
        "git_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_map_sha256": source_map_sha256,
        "dependency_provider_mode": "ci_debs",
        "ci_debs_manifest_sha256": manifest_sha256,
        "build_log": {"artifact": build_log.name, "sha256": sha256(build_log)},
        "module_graph": {"artifact": module_graph_path.name, "sha256": sha256(module_graph_path)},
        "additional_compile_targets": additional_compile_targets,
        "runtime_tests": runtime_tests,
        "source_files": sorted(sources),
        "programs": receipts,
    }
    if configured_rules_path is not None:
        receipt["configured_rules"] = {"artifact": configured_rules_path.name, "sha256": sha256(configured_rules_path)}
        receipt["native_contract"] = {"artifact": native_contract_path.name, "sha256": native_contract_sha256}
    receipt_path = artifact_directory / "manifest.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    if configured_rules_path is not None:
        run([
            sys.executable, str(ROOT / "bazel/compare_build_contract.py"),
            "--architecture", args.architecture,
            "--native-contract", str(native_contract_path),
            "--configured-rules", str(configured_rules_path),
            "--build-receipt", str(receipt_path),
            "--output", str(artifact_directory / "native-contract-check.json"),
        ])
    print(f"Verified {len(receipts)} production C++ executables and {len(sources)} tracked source files")


def check_codeql(args: argparse.Namespace) -> None:
    selected = {
        "production": tracked_source_paths(load_programs()),
        "tests": tracked_paths(CODEQL_TEST_SOURCES),
        "features": tracked_paths(CODEQL_FEATURE_SOURCES),
    }
    expected = set().union(*selected.values())
    if sum(len(paths) for paths in selected.values()) != len(expected):
        raise ValueError("selected CodeQL source categories overlap")
    native_sources = tracked_native_source_paths()
    test_sources = {
        path for path in native_sources
        if path.startswith("tests/") or "/tests/" in path
        or re.search(r"_test\.(?:c|cc|cpp|cxx)$", path)
    }
    feature_sources = native_sources & FEATURE_SOURCE_BOUNDARIES
    observed = set()
    with args.csv.open(newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["path"]:
            raise ValueError(f"unexpected CodeQL source CSV columns: {reader.fieldnames}")
        for row in reader:
            if set(row) != {"path"} or not isinstance(row["path"], str):
                raise ValueError(f"invalid CodeQL source CSV row: {row}")
            observed.add(relative_path(row["path"]))
    missing = sorted(expected - observed)
    receipt = {
        "schema_version": 2,
        "source_map_sha256": sha256(SOURCE_MAP),
        "additional_compile_targets": CODEQL_COMPILE_TARGETS,
        "selected_source_files": {name: sorted(paths) for name, paths in selected.items()},
        "expected_source_files": sorted(expected),
        "observed_source_files": sorted(observed),
        "observed_sources_not_in_tracked_native_inventory": sorted(observed - native_sources),
        "observed_source_file_count": len(observed),
        "matched_source_file_count": len(expected) - len(missing),
        "missing_source_files": missing,
        "additional_observed_source_files": sorted(observed - expected),
        "excluded_source_files": {
            "tests": sorted(test_sources - selected["tests"]),
            "features": sorted(feature_sources - selected["features"]),
            "other_native": sorted(native_sources - expected - test_sources - feature_sources),
        },
        "native_source_inventory": {
            "scope": "Tracked native source paths in this checkout.",
            "suffixes": list(NATIVE_SOURCE_SUFFIXES),
            "test_path_rule": "A tests directory component or a filename ending in _test followed by a native source suffix.",
            "feature_paths": sorted(FEATURE_SOURCE_BOUNDARIES),
        },
        "exclusion_note": "Excluded files are not validated by this job; exclusion does not classify them as unsupported.",
        "generated_source_reporting": {
            "selected_source_requirement": "All selected SWSS translation units must be tracked source files.",
            "generation_status_of_tracked_sources": "not_classified",
            "outside_source_root_reporting": "not_verified",
        },
        "sarif_source_reporting": "not_verified_by_this_extraction_receipt",
        "historical_coverage_comparison": {
            "status": "not_performed",
            "reason": "This check has no prior-pipeline source inventory.",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    if missing:
        raise ValueError(f"CodeQL did not extract selected C++ sources: {missing}")
    print(
        f"CodeQL extracted all {len(expected)} selected C++ source files "
        f"({len(selected['production'])} production, {len(selected['tests'])} test, "
        f"{len(selected['features'])} feature)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    build_parser.add_argument("--manifest", type=Path, required=True)
    build_parser.add_argument("--native-contract", type=Path)
    build_parser.add_argument("--artifact-directory", type=Path, required=True)
    build_parser.add_argument("--mode", choices=("normal", "clean", "codeql"), default="normal")
    build_parser.add_argument("--bazel", default="bazel")
    coverage_parser = commands.add_parser("check-codeql")
    coverage_parser.add_argument("--csv", type=Path, required=True)
    coverage_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        (build if args.command == "build" else check_codeql)(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()

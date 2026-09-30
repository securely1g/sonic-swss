#!/usr/bin/env python3
"""Generate a native Bazel package from a configured public SWSS tree.

The configured Automake files remain the source of truth for selected programs,
sources, flags, and installed data. The output is an owned, synchronized Bazel
package; it never writes to either input tree.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
import tomllib
from typing import Any


MARKER = ".sonic-swss-bazel-package"
MARKER_CONTENT = b"sonic-swss-bazel-package-v1\n"
VENDOR_ARCHIVE = ".sonic-bazel-cargo-vendor.tar"
HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx", ".inc"}
CXX_SUFFIXES = {".cc", ".cpp", ".cxx", ".C"}
IGNORED_DIRECTORIES = {".git", ".deps", ".libs", "target", "autom4te.cache", "__pycache__"}
PRIMARY = re.compile(r"^((?:(?:dist|nodist|nobase)_)*)([A-Za-z][A-Za-z0-9_]*)_(PROGRAMS|DATA|SCRIPTS|HEADERS|LIBRARIES|LTLIBRARIES)$")
RESERVED_TARGETS = {"headers", "countersyncd"}
MODULE_INCLUDE_ROOTS = {
    "/usr/include": "<system-headers>",
    "/usr/include/libnl3": "<libnl3-headers>",
    "/usr/include/sai": "<sai-headers>",
    "/usr/include/swss": "<swsscommon-headers>",
}
# These mappings describe the selected normal-release module contract. Options
# absent from these classifications remain literal requirements for the Bazel
# configured-rule comparison; the generator does not discard unfamiliar flags.
MODULE_BUILD_MAPPINGS = {
    "reviewed_infra": {
        "module": "sonic-build-infra",
        "version": "0.0.9-c4175cb61c79b3b7b70901724fddbe2cd35ff86d",
    },
    "include_roots": {
        "<swss>": "component_headers",
        "<system-headers>": "managed_toolchain_and_declared_dependencies",
        "<libnl3-headers>": "@libnl3",
        "<sai-headers>": "@sonic_sairedis",
        "<swsscommon-headers>": "@sonic_swss_common",
    },
    "source_prefix_maps": ["-fdebug-prefix-map", "-ffile-prefix-map", "-fmacro-prefix-map"],
    "native_only_compile_options": ["-DHAVE_CONFIG_H"],
    "toolchain_compile_options": [
        "-O2", "-fstack-protector-strong", "-fstack-clash-protection",
    ],
    "target_toolchain_compile_options": {
        "aarch64": ["-mbranch-protection=standard"],
        "x86_64": ["-fcf-protection"],
    },
    "toolchain_link_options": ["-Wl,-z,relro"],
    "native_link_driver_options": [
        "-g", "-O2", "-fstack-protector-strong", "-fstack-clash-protection",
        "-Wformat", "-Werror=format-security",
    ],
    "compile_replacements": {
        "-D_FORTIFY_SOURCE=2": ["-U_FORTIFY_SOURCE", "-D_FORTIFY_SOURCE=3"],
        "-Wformat": ["-Wformat=2"],
        "-Werror=format-security": ["-Werror"],
    },
    "bazel_only_compile_options": ["-Wno-missing-include-dirs"],
    # Recorded under the reviewed infra boundary, not inspected in cquery.
    "reviewed_toolchain_additional_link_options": ["-Wl,-z,now", "-Wl,--as-needed"],
    "implicit_system_header_providers": {
        "all_programs": [
            "@trixie//libboost-dev:libboost",
            "@trixie//nlohmann-json3-dev:nlohmann-json3",
        ],
        "programs": {"orchagent": ["@swss_debian//libyaml-cpp-dev:libyaml-cpp"]},
    },
    "local_header_providers": [
        "//cfgmgr:production_headers",
        "//fdbsyncd:production_headers",
        "//fpmsyncd:production_headers",
        "//gearsyncd:production_headers",
        "//lib:production_headers",
        "//mclagsyncd:production_headers",
        "//natsyncd:production_headers",
        "//neighsyncd:production_headers",
        "//orchagent:production_headers",
        "//portsyncd:production_headers",
        "//teamsyncd:production_headers",
        "//tlm_teamd:production_headers",
        "//warmrestart:production_headers",
    ],
    "rule_support_providers": ["@rules_cc//:link_extra_lib"],
    "feature_support": {
        "debug": {"source_labels": [], "dependency_labels": []},
        "asan": {
            "source_labels": ["//lib:asan.cpp", "//lib:asan_ctor.cpp"],
            "dependency_labels": [],
        },
        "gcov": {
            "source_labels": ["//gcovpreload:gcovpreload.cpp"],
            "dependency_labels": ["//gcovpreload:gcovpreload_shared"],
        },
    },
    "library_providers": {
        "dashapi": "@sonic_dash_api//:dashapi",
        "hiredis": "@trixie//libhiredis-dev:libhiredis",
        "jansson": "@swss_debian//libjansson-dev:libjansson",
        "jemalloc": "@swss_debian//libjemalloc-dev:libjemalloc",
        "m": "toolchain:libm",
        "nl-3": "@libnl3//:libnl_3",
        "nl-genl-3": "@libnl3//:libnl_genl_3",
        "nl-nf-3": "@libnl3//:libnl_nf_3",
        "nl-route-3": "@libnl3//:libnl_route_3",
        "protobuf": "@swss_debian//libprotobuf-dev:libprotobuf",
        "pthread": "toolchain:libc",
        "saimeta": "@sonic_sairedis//meta:saimeta_shared",
        "saimetadata": "@sonic_sairedis//meta:saimetadata_shared",
        "sairedis": "@sonic_sairedis//lib:sairedis_shared",
        "swsscommon": "@sonic_swss_common//:libswsscommon_shared",
        "team": "@swss_debian//libteam-dev:libteam",
        "teamdctl": "@swss_debian//libteam-dev:libteam",
        "zmq": "@trixie//libzmq3-dev:libzmq3",
    },
}


def relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"invalid relative path: {value}")
    return path


def file_mode(path: Path) -> int:
    return 0o755 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o644


def package_mode(install_path: str, mode: int) -> int:
    # Files installed in executable directories must remain executable.
    executable_directories = ("bin/", "sbin/", "usr/bin/", "usr/sbin/", "usr/games/", "etc/init.d/")
    return 0o755 if install_path.startswith(executable_directories) else mode


def walk_files(root: Path, ignore_build_outputs: bool = True) -> list[Path]:
    files = []
    for directory, subdirectories, names in os.walk(root):
        if ignore_build_outputs:
            subdirectories[:] = sorted(
                name for name in subdirectories
                if name not in IGNORED_DIRECTORIES and not name.startswith("bazel-")
            )
        else:
            subdirectories.sort()
            if any((Path(directory) / name).is_symlink() for name in subdirectories):
                raise ValueError(f"Cargo vendor tree contains a symlinked directory: {directory}")
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_file():
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError(f"source symlink leaves the source tree: {path}")
                files.append(path)
    return files


def query_make(directory: Path) -> dict[str, tuple[str, str]]:
    makefile = directory / "Makefile"
    if not makefile.is_file():
        raise ValueError(f"missing configured Makefile: {makefile}")
    contents = makefile.read_text()
    if re.search(r"^install-(?:exec|data)-(?:local|hook)\s*:", contents, re.MULTILINE):
        raise ValueError(f"custom Automake install hooks are not supported: {makefile}")
    with tempfile.TemporaryDirectory(prefix="sonic-swss-make-") as temporary:
        work = Path(temporary)
        # Reading a copy prevents Make from rebuilding the configured Makefile.
        (work / "configured.mk").write_text(contents)
        output = work / "variables"
        variables = " ".join(
            [
                "SUBDIRS top_srcdir abs_top_srcdir top_builddir abs_top_builddir host_cpu",
                "DEFS DEFAULT_INCLUDES INCLUDES CPPFLAGS CFLAGS CXXFLAGS LDFLAGS LIBS LDADD",
                "AM_CPPFLAGS AM_CFLAGS AM_CXXFLAGS AM_LDFLAGS CC CXX EXEEXT BUILT_SOURCES",
                "CFLAGS_COMMON",
                "$(filter %dir %_PROGRAMS %_DATA %_SCRIPTS %_HEADERS %_LIBRARIES %_LTLIBRARIES %_SOURCES %_OBJECTS %_CPPFLAGS %_CFLAGS %_CXXFLAGS %_LDFLAGS %_LDADD %_DEPENDENCIES,$(.VARIABLES))",
            ]
        )
        # The public fpmsyncd tree contains fpm/fpm.h. Supply that local include
        # root for the optional legacy FPM_PATH only in this Bazel inventory
        # query; leave explicit Make/environment values and native builds alone.
        local_defaults = "FPM_PATH ?= $(abs_top_srcdir)/fpmsyncd\n" if directory.name == "fpmsyncd" else ""
        (work / "query.mk").write_text(
            local_defaults + ".PHONY: sonic-bazel-query\n"
            "sonic-bazel-query:\n"
            f"\t@$(file >{output},SONIC_BAZEL_QUERY_V1)"
            f"$(foreach v,$(sort {variables}),$(file >>{output},$(v)\t$(origin $(v))\t$(strip $($(v))))) :\n"
        )
        environment = os.environ.copy()
        environment.update(LC_ALL="C", MAKEFLAGS="", MFLAGS="")
        subprocess.run(
            ["make", "--silent", "--no-print-directory", "-f", str(work / "configured.mk"), "-f", str(work / "query.mk"), "sonic-bazel-query"],
            cwd=directory,
            env=environment,
            check=True,
        )
        lines = output.read_text().splitlines()
    if not lines or lines[0] != "SONIC_BAZEL_QUERY_V1":
        raise ValueError(f"invalid Make query output for {directory}")
    result = {}
    for line in lines[1:]:
        fields = line.split("\t", 2)
        if len(fields) != 3 or fields[0] in result:
            raise ValueError(f"unsupported multiline or duplicate Make value in {directory}: {line}")
        result[fields[0]] = (fields[1], fields[2])
    return result


def value(variables: dict[str, tuple[str, str]], name: str, fallback: str | None = None) -> str:
    origin, result = variables.get(name, ("undefined", ""))
    if origin == "undefined" and fallback is not None:
        return value(variables, fallback)
    return result


class Generator:
    def __init__(self, source: Path, configured: Path, package: str, vendor: Path | None = None, vendor_config: Path | None = None):
        self.source = source.resolve()
        self.configured = configured.resolve()
        self.vendor = vendor.resolve() if vendor is not None else None
        self.vendor_config = vendor_config.resolve() if vendor_config is not None else None
        self.package = relative_path(package).as_posix()
        self.inputs: dict[str, Path] = {}
        self.programs: list[dict[str, Any]] = []
        self.automake_install: list[dict[str, Any]] = []
        self.cargo_sources: set[str] = set()
        self.headers: set[str] = set()
        self.subdirectories: list[str] = []
        self.compilers: set[str] = set()
        self.target_cpus: set[str] = set()
        self.vendor_archive = b""
        self.vendor_file_count = 0
        self.cargo_config = b""

    def add_input(self, relative: str, path: Path) -> str:
        name = relative_path(relative).as_posix()
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError(f"missing declared SWSS input: {path}")
        if not (resolved.is_relative_to(self.source) or resolved.is_relative_to(self.configured)):
            raise ValueError(f"SWSS input leaves the source and configured trees: {path}")
        previous = self.inputs.get(name)
        if previous is not None and previous.read_bytes() != resolved.read_bytes():
            raise ValueError(f"source and configured inputs collide at {name}")
        self.inputs[name] = resolved
        return name

    def resolve_source(self, token: str, subdirectory: str) -> str:
        candidate = Path(token)
        candidates = [candidate] if candidate.is_absolute() else [
            self.configured / subdirectory / candidate,
            self.source / subdirectory / candidate,
        ]
        for path in candidates:
            resolved = path.resolve()
            if resolved.is_file():
                for root in (self.source, self.configured):
                    if resolved.is_relative_to(root):
                        return self.add_input(resolved.relative_to(root).as_posix(), resolved)
                raise ValueError(f"configured source leaves SWSS inputs: {token}")
        raise ValueError(f"configured source does not exist in {subdirectory}: {token}")

    def rebase_path(self, token: str, directory: Path, local_libraries: bool = False) -> str:
        path = Path(token)
        resolved = (path if path.is_absolute() else directory / path).resolve()
        for root in (self.source, self.configured):
            if resolved.is_relative_to(root):
                if local_libraries:
                    raise ValueError(f"local library paths need explicit Bazel targets: {token}")
                relative = resolved.relative_to(root).as_posix()
                return f"{self.package}/src" + (f"/{relative}" if relative != "." else "")
        if path.is_absolute():
            return token
        raise ValueError(f"relative compiler path leaves the SWSS inputs: {token}")

    def options(self, strings: list[str], subdirectory: str, link: bool = False) -> list[str]:
        tokens = shlex.split(" ".join(strings))
        result = []
        directory = self.configured / subdirectory
        path_options = ("-isystem", "-iquote", "-idirafter", "-I", "-L")
        index = 0
        while index < len(tokens):
            token = tokens[index]
            matched = False
            for option in path_options:
                if token == option:
                    index += 1
                    if index == len(tokens) or tokens[index].startswith("-"):
                        raise ValueError(f"missing path after {option} in {subdirectory}")
                    result.extend([option, self.rebase_path(tokens[index], directory, option == "-L")])
                    matched = True
                    break
                if token.startswith(option) and len(token) > len(option):
                    result.append(option + self.rebase_path(token[len(option):], directory, option == "-L"))
                    matched = True
                    break
            if not matched:
                prefix_map = re.match(r"^(-f(?:debug|file|macro)-prefix-map=)([^=]+)=(.*)$", token)
                if prefix_map:
                    result.append(prefix_map[1] + self.rebase_path(prefix_map[2], directory) + "=" + prefix_map[3])
                else:
                    if str(self.source) in token or str(self.configured) in token:
                        raise ValueError(f"unhandled configured path in compiler option: {token}")
                    if token in ("-include", "-imacros", "-isysroot", "--sysroot"):
                        raise ValueError(f"compiler option needs an explicit Bazel input: {token}")
                    if link and not token.startswith("-") and not Path(token).is_absolute():
                        raise ValueError(f"local link input needs an explicit Bazel target: {token}")
                    result.append(token)
            index += 1
        return result

    def installed_path(self, directory: str, source: str, nobase: bool) -> str:
        path = PurePosixPath(directory)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError(f"configured install directory must be absolute: {directory}")
        suffix = relative_path(source) if nobase else PurePosixPath(source).name
        return (path.relative_to("/") / suffix).as_posix()

    def read_program(self, name: str, directory: str, install_directory: str, variables: dict[str, tuple[str, str]]) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_+.-]+", name) or name in RESERVED_TARGETS:
            raise ValueError(f"unsupported or reserved program name: {name}")
        sources = []
        for token in shlex.split(value(variables, name + "_SOURCES") + " " + value(variables, "nodist_" + name + "_SOURCES")):
            relative = self.resolve_source(token, directory)
            suffix = Path(relative).suffix
            if suffix in HEADER_SUFFIXES:
                self.headers.add(relative)
            elif suffix in CXX_SUFFIXES:
                sources.append(relative)
            else:
                raise ValueError(f"unsupported source language for {name}: {relative}")
        if not sources or len(sources) != len(set(sources)):
            raise ValueError(f"empty or duplicate configured C++ source list for {name}")
        objects = shlex.split(value(variables, name + "_OBJECTS"))
        if len(objects) != len(sources) or any(not item.endswith(".o") for item in objects):
            raise ValueError(f"configured object list does not match C++ sources for {name}")
        if value(variables, name + "_DEPENDENCIES"):
            raise ValueError(f"local program dependencies need explicit Bazel targets: {name}")
        compile_options = self.options(
            [
                value(variables, "DEFS"), value(variables, "DEFAULT_INCLUDES"), value(variables, "INCLUDES"),
                value(variables, name + "_CPPFLAGS", "AM_CPPFLAGS"), value(variables, "CPPFLAGS"),
                value(variables, name + "_CXXFLAGS", "AM_CXXFLAGS"), value(variables, "CXXFLAGS"),
            ],
            directory,
        )
        link_options = self.options(
            [
                value(variables, name + "_CXXFLAGS", "AM_CXXFLAGS"), value(variables, "CXXFLAGS"),
                value(variables, name + "_LDFLAGS", "AM_LDFLAGS"), value(variables, "LDFLAGS"),
                value(variables, name + "_LDADD", "LDADD"), value(variables, "LIBS"),
            ],
            directory,
            link=True,
        )
        self.programs.append(
            {
                "name": name,
                "directory": directory,
                "sources": sources,
                "compile_options": compile_options,
                "link_options": link_options,
                "install_path": self.installed_path(install_directory, name, False),
            }
        )

    def read_make_tree(self) -> None:
        pending = ["."]
        visited = set()
        while pending:
            directory = pending.pop(0)
            if directory in visited:
                raise ValueError(f"duplicate or recursive configured SUBDIRS entry: {directory}")
            visited.add(directory)
            build_directory = self.configured / directory
            variables = query_make(build_directory)
            top_source = value(variables, "abs_top_srcdir") or value(variables, "top_srcdir")
            if not top_source or (build_directory / top_source).resolve() != self.source:
                raise ValueError(f"configured Makefile does not belong to --source: {build_directory}")
            if value(variables, "EXEEXT"):
                raise ValueError("the SWSS Bazel package supports Linux executables only")
            if value(variables, "BUILT_SOURCES"):
                raise ValueError(f"generated sources need explicit Bazel targets: {build_directory}")
            if "-fprofile-arcs" in value(variables, "CFLAGS_COMMON"):
                raise ValueError("GCOV is not supported by the generated Bazel release targets")
            self.subdirectories.append(directory)
            if value(variables, "CXX"):
                self.compilers.add(value(variables, "CXX"))
            if value(variables, "host_cpu"):
                self.target_cpus.add(value(variables, "host_cpu"))
            for child in shlex.split(value(variables, "SUBDIRS")):
                child_path = (PurePosixPath(directory) / child)
                relative_path(child_path.as_posix())
                pending.append(child_path.as_posix())
            for variable in sorted(variables):
                # Automake's install commands are not installed data primaries.
                if variable in ("INSTALL_DATA", "install_sh_DATA"):
                    continue
                match = PRIMARY.fullmatch(variable)
                if not match or variable.startswith("am__") or not value(variables, variable):
                    continue
                modifiers, prefix, kind = match.groups()
                if prefix in ("noinst", "check", "EXTRA"):
                    continue
                if kind in ("LIBRARIES", "LTLIBRARIES"):
                    raise ValueError(f"installed libraries need explicit Bazel targets: {variable}")
                install_directory = value(variables, prefix + "dir")
                if not install_directory:
                    raise ValueError(f"missing configured install directory for {variable}")
                for token in shlex.split(value(variables, variable)):
                    if kind == "PROGRAMS":
                        if "nobase_" in modifiers:
                            raise ValueError(f"nobase installed programs are not supported: {variable}")
                        self.read_program(token, directory, install_directory, variables)
                    else:
                        relative = self.resolve_source(token, directory)
                        install_path = self.installed_path(install_directory, token, "nobase_" in modifiers)
                        self.automake_install.append(
                            {
                                "source": relative,
                                "install_path": install_path,
                                "mode": package_mode(install_path, 0o755 if kind == "SCRIPTS" else 0o644),
                            }
                        )
        names = [program["name"] for program in self.programs]
        if not names or len(names) != len(set(names)):
            raise ValueError("configured SWSS programs are empty or have duplicate names")

    def read_headers_and_cargo(self) -> None:
        for path in walk_files(self.source):
            relative = path.relative_to(self.source).as_posix()
            if path.suffix in HEADER_SUFFIXES and relative != "config.h":
                self.headers.add(self.add_input(relative, path))
            if relative in ("Cargo.toml", "Cargo.lock") or relative.startswith("crates/") or relative.startswith(".cargo/"):
                self.cargo_sources.add(self.add_input(relative, path))
        self.headers.add(self.add_input("config.h", self.configured / "config.h"))
        for required in ("Cargo.toml", "Cargo.lock", "crates/countersyncd/Cargo.toml"):
            if required not in self.cargo_sources:
                raise ValueError(f"missing locked Cargo workspace input: {required}")

    def read_vendor(self) -> None:
        if self.vendor is None or self.vendor_config is None or not self.vendor.is_dir() or not self.vendor_config.is_file():
            raise ValueError("Cargo vendor directory and cargo vendor configuration are required")
        configuration_text = self.vendor_config.read_text()
        configuration = tomllib.loads(configuration_text)
        directory_sources = [
            source for source in configuration.get("source", {}).values()
            if isinstance(source, dict) and "directory" in source
        ]
        if len(directory_sources) != 1:
            raise ValueError("cargo vendor configuration must contain exactly one directory source")
        configured_directory = Path(directory_sources[0]["directory"])
        if not configured_directory.is_absolute():
            configured_directory = self.source / configured_directory
        if configured_directory.resolve() != self.vendor:
            raise ValueError("cargo vendor configuration does not identify --cargo-vendor")
        normalized, replacements = re.subn(r"^directory\s*=.*$", 'directory = "vendor"', configuration_text, flags=re.MULTILINE)
        if replacements != 1:
            raise ValueError("could not normalize cargo vendor directory configuration")
        original_config = self.source / ".cargo/config.toml"
        if (self.source / ".cargo/config").exists():
            raise ValueError("legacy .cargo/config must be migrated before adding the vendor configuration")
        combined = (original_config.read_text() + "\n" if original_config.is_file() else "") + normalized
        tomllib.loads(combined)
        for root in (self.source, self.configured, self.vendor):
            if str(root) in combined:
                raise ValueError("normalized Cargo configuration contains an absolute input path")
        self.cargo_config = combined.encode()
        self.cargo_sources.add(".cargo/config.toml")

        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in walk_files(self.vendor, ignore_build_outputs=False):
                relative = path.relative_to(self.vendor).as_posix()
                info = tarfile.TarInfo("vendor/" + relative_path(relative).as_posix())
                info.size = path.stat().st_size
                info.mode = file_mode(path)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
                self.vendor_file_count += 1
        self.vendor_archive = output.getvalue()
        self.cargo_sources.add(VENDOR_ARCHIVE)

    def production_inventory(self) -> dict[str, Any]:
        installed = [program["install_path"] for program in self.programs]
        installed += [entry["install_path"] for entry in self.automake_install]
        if len(installed) != len(set(installed)):
            raise ValueError("configured build has duplicate install paths")
        return {
            "programs": sorted(self.programs, key=lambda item: item["name"]),
            "automake_install": sorted(self.automake_install, key=lambda item: item["install_path"]),
        }

    def inventory(self, epoch: int) -> dict[str, Any]:
        production = self.production_inventory()
        return {
            "schema_version": 1,
            "configured_subdirectories": self.subdirectories,
            "configured_cxx": sorted(self.compilers),
            "programs": production["programs"],
            "automake_install": production["automake_install"],
            "cargo": {
                "label": f"//{self.package}:countersyncd",
                "locked": True,
                "offline": True,
                "vendor_archive": VENDOR_ARCHIVE,
                "vendor_files": self.vendor_file_count,
                "vendor_sha256": hashlib.sha256(self.vendor_archive).hexdigest(),
            },
            "source_date_epoch": epoch,
        }

    def module_sources(self, inventory: dict[str, Any]) -> str:
        def source_label(name: str) -> str:
            parts = relative_path(name).parts
            return "//" + parts[0] + ":" + "/".join(parts[1:]) if len(parts) > 1 else "//:" + parts[0]

        def local_include_directories(program: dict[str, Any]) -> list[str]:
            source_root = self.package + "/src"
            result = []
            options = program["compile_options"]
            index = 0
            while index < len(options):
                option = options[index]
                for prefix in ("-isystem", "-iquote", "-idirafter", "-I"):
                    if option == prefix:
                        index += 1
                        path = options[index]
                    elif option.startswith(prefix) and len(option) > len(prefix):
                        path = option[len(prefix):]
                    else:
                        continue
                    if path == source_root or path.startswith(source_root + "/"):
                        if prefix != "-I":
                            raise ValueError(f"local module include class needs an explicit mapping for {program['name']}: {prefix}")
                        relative = path[len(source_root):].removeprefix("/")
                        result.append(relative_path(relative).as_posix() if relative else ".")
                    break
                index += 1
            return result

        programs = {
            program["name"]: {
                "directory": program["directory"],
                "sources": [source_label(name) for name in program["sources"]],
                "install_path": program["install_path"],
                "local_include_directories": local_include_directories(program),
            }
            for program in inventory["programs"]
        }
        lines = [
            "# Generated by sonic-swss/bazel/generate.py. Do not edit.",
            "# Source labels use the module's top-level component packages.",
            "",
            "SWSS_PROGRAMS = " + json.dumps(programs, indent=4, sort_keys=True),
            "",
            "SWSS_AUTOMAKE_INSTALL = " + json.dumps(inventory["automake_install"], indent=4, sort_keys=True),
            "",
        ]
        return "\n".join(lines)

    def build_contract(self, inventory: dict[str, Any], source_map_sha256: str, git_revision: str) -> dict[str, Any]:
        """Describe this runner's native release options for Bazel comparison."""
        def normalized_path(path: str, include: bool = False) -> str:
            source_root = self.package + "/src"
            if path == source_root or path.startswith(source_root + "/"):
                return "<swss>" + path[len(source_root):]
            if include and path in MODULE_INCLUDE_ROOTS:
                return MODULE_INCLUDE_ROOTS[path]
            raise ValueError(f"native path has no module build-contract mapping: {path}")

        def normalized_options(options: list[str], link: bool) -> list[dict[str, str]]:
            result = []
            index = 0
            target_options = {
                option
                for values in MODULE_BUILD_MAPPINGS["target_toolchain_compile_options"].values()
                for option in values
            }
            while index < len(options):
                option = options[index]
                argument = option
                classification = "literal"
                for prefix in ("-isystem", "-iquote", "-idirafter", "-I", "-L"):
                    if option == prefix:
                        index += 1
                        path = options[index]
                    elif option.startswith(prefix) and len(option) > len(prefix):
                        path = option[len(prefix):]
                    else:
                        continue
                    if prefix == "-L":
                        raise ValueError(f"native library search path has no module build-contract mapping: {path}")
                    argument = prefix + normalized_path(path, include=True)
                    classification = "include"
                    break
                else:
                    prefix_map = re.match(r"^(-f(?:debug|file|macro)-prefix-map=)([^=]+)=(.*)$", option)
                    if prefix_map:
                        if Path(prefix_map[3]).is_absolute():
                            raise ValueError(f"native prefix-map destination has no module build-contract mapping: {prefix_map[3]}")
                        argument = prefix_map[1] + normalized_path(prefix_map[2]) + "=" + prefix_map[3]
                        classification = "source_prefix_map"
                    elif link and option.startswith("-l"):
                        if option[2:] not in MODULE_BUILD_MAPPINGS["library_providers"]:
                            raise ValueError(f"native library has no module build-contract provider: {option}")
                        classification = "library"
                    elif link and Path(option).is_absolute():
                        raise ValueError(f"native link input has no module build-contract provider: {option}")
                    elif link and option in MODULE_BUILD_MAPPINGS["native_link_driver_options"]:
                        classification = "native_link_driver"
                    elif link and option in target_options:
                        classification = "target_native_link_driver"
                    elif link and option in MODULE_BUILD_MAPPINGS["toolchain_link_options"]:
                        classification = "toolchain"
                    elif not link and option in MODULE_BUILD_MAPPINGS["native_only_compile_options"]:
                        classification = "native_only"
                    elif not link and option in MODULE_BUILD_MAPPINGS["compile_replacements"]:
                        classification = "replacement"
                    elif not link and option in MODULE_BUILD_MAPPINGS["toolchain_compile_options"]:
                        classification = "toolchain"
                    elif not link and option in target_options:
                        classification = "target_toolchain"
                result.append({"argument": argument, "classification": classification})
                index += 1
            return result

        def option_profiles(key: str, link: bool) -> tuple[dict[str, list[dict[str, str]]], dict[str, str]]:
            profiles: dict[str, list[dict[str, str]]] = {}
            references = {}
            for program in inventory["programs"]:
                options = normalized_options(program[key], link)
                profile = next((name for name, existing in profiles.items() if existing == options), None)
                if profile is None:
                    profile = program["name"]
                    profiles[profile] = options
                references[program["name"]] = profile
            return profiles, references

        compile_profiles, compile_references = option_profiles("compile_options", False)
        link_profiles, link_references = option_profiles("link_options", True)
        return {
            "schema_version": 1,
            "git_revision": git_revision,
            "source_map_sha256": source_map_sha256,
            "architecture": {
                "execution_machine": platform.machine(),
                "configured_target_cpus": sorted(self.target_cpus),
            },
            "compile_profiles": compile_profiles,
            "link_profiles": link_profiles,
            "mappings": MODULE_BUILD_MAPPINGS,
            "programs": {
                program["name"]: {
                    "compile_profile": compile_references[program["name"]],
                    "link_profile": link_references[program["name"]],
                }
                for program in inventory["programs"]
            },
        }

    def build_file(self, inventory: dict[str, Any]) -> str:
        def assignment(name: str, item: Any) -> list[str]:
            rendered = (["True" if item else "False"] if isinstance(item, bool) else json.dumps(item, indent=4, sort_keys=True).splitlines())
            return [f"    {name} = {rendered[0]}"] + ["    " + line for line in rendered[1:-1]] + (["    " + rendered[-1] + ","] if len(rendered) > 1 else [])

        def rule(kind: str, attributes: dict[str, Any]) -> list[str]:
            result = [kind + "("]
            for name, item in attributes.items():
                lines = assignment(name, item)
                if len(lines) == 1:
                    lines[0] += ","
                result.extend(lines)
            return result + [")", ""]

        lines = [
            '# Generated by sonic-swss/bazel/generate.py. Do not edit.',
            'load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library")',
            'load(":defs.bzl", "swss_cargo_binary")',
            "",
            'package(default_visibility = ["//visibility:public"])',
            'exports_files(["inventory.json", "production_sources.bzl", "tools/build_cargo.py"])',
            "",
        ]
        lines += rule("cc_library", {"name": "headers", "hdrs": ["src/" + name for name in sorted(self.headers)]})
        for program in inventory["programs"]:
            lines += rule(
                "cc_binary",
                {
                    "name": program["name"],
                    "srcs": ["src/" + name for name in program["sources"]],
                    "copts": program["compile_options"],
                    "linkopts": program["link_options"],
                    "deps": [":headers"],
                    "linkstatic": False,
                },
            )
        lines += rule(
            "swss_cargo_binary",
            {
                "name": "countersyncd",
                "srcs": ["src/" + name for name in sorted(self.cargo_sources)],
                "source_root": self.package + "/src",
                "source_date_epoch": str(inventory["source_date_epoch"]),
            },
        )
        return "\n".join(lines)

    def render(self, epoch: int) -> dict[str, tuple[bytes, int]]:
        self.read_make_tree()
        self.read_headers_and_cargo()
        self.read_vendor()
        inventory = self.inventory(epoch)
        rendered = {
            "src/" + name: (path.read_bytes(), file_mode(path))
            for name, path in sorted(self.inputs.items())
        }
        rendered["src/" + VENDOR_ARCHIVE] = (self.vendor_archive, 0o644)
        rendered["src/.cargo/config.toml"] = (self.cargo_config, 0o644)
        for name in ("defs.bzl", "build_cargo.py"):
            path = self.source / "bazel" / name
            if not path.is_file():
                raise ValueError(f"missing Bazel support input: {path}")
            destination = name if name.endswith(".bzl") else "tools/" + name
            rendered[destination] = (path.read_bytes(), 0o644 if name.endswith(".bzl") else 0o755)
        rendered["BUILD.bazel"] = (self.build_file(inventory).encode(), 0o644)
        rendered["production_sources.bzl"] = (self.module_sources(inventory).encode(), 0o644)
        rendered["inventory.json"] = ((json.dumps(inventory, indent=2, sort_keys=True) + "\n").encode(), 0o644)
        rendered[MARKER] = (MARKER_CONTENT, 0o644)
        for name in ("BUILD.bazel", "production_sources.bzl", "inventory.json"):
            for root in (self.source, self.configured):
                if str(root).encode() in rendered[name][0]:
                    raise ValueError(f"generated {name} contains an absolute input path")
        return rendered


def synchronize(output: Path, rendered: dict[str, tuple[bytes, int]]) -> None:
    if output.is_symlink():
        raise ValueError("output package must not be a symlink")
    output.mkdir(parents=True, exist_ok=True)
    marker = output / MARKER
    if any(output.iterdir()) and (not marker.is_file() or marker.read_bytes() != MARKER_CONTENT):
        raise ValueError(f"refusing to replace an unowned output package: {output}")
    for path in sorted(output.rglob("*"), reverse=True):
        relative = path.relative_to(output).as_posix()
        if path.is_symlink() or path.is_file():
            if relative not in rendered:
                path.unlink()
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                # Directories with retained generated files must remain in place.
                pass
    for name, (contents, mode) in sorted(rendered.items()):
        relative_path(name)
        destination = output / name
        for parent in destination.parents:
            if parent == output:
                break
            if parent.is_symlink():
                raise ValueError(f"output package contains a symlinked directory: {parent}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and not destination.is_symlink() and destination.read_bytes() == contents and stat.S_IMODE(destination.stat().st_mode) == mode:
            continue
        with tempfile.NamedTemporaryFile(prefix=".sonic-bazel-", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
        temporary.chmod(mode)
        os.replace(temporary, destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--configured-build", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output-package", type=Path)
    mode.add_argument("--check-production-sources", type=Path, metavar="FILE")
    parser.add_argument("--output-build-contract", type=Path, metavar="FILE")
    parser.add_argument("--git-revision")
    parser.add_argument("--cargo-vendor", type=Path)
    parser.add_argument("--cargo-vendor-config", type=Path)
    parser.add_argument("--workspace-package", default="swss")
    parser.add_argument("--source-date-epoch", type=int)
    args = parser.parse_args()
    if args.output_build_contract is not None:
        if args.check_production_sources is None or args.git_revision is None:
            parser.error("--output-build-contract requires --check-production-sources and --git-revision")
    elif args.git_revision is not None:
        parser.error("--git-revision requires --output-build-contract")
    source = args.source.resolve()
    configured = args.configured_build.resolve()
    if args.check_production_sources is not None:
        generator = Generator(source, configured, args.workspace_package)
        generator.read_make_tree()
        inventory = generator.production_inventory()
        generated = generator.module_sources(inventory).encode()
        if args.check_production_sources.read_bytes() != generated:
            parser.exit(1, f"{args.check_production_sources} does not match configured Automake production sources\n")
        if args.output_build_contract is not None:
            contract = generator.build_contract(inventory, hashlib.sha256(generated).hexdigest(), args.git_revision)
            args.output_build_contract.parent.mkdir(parents=True, exist_ok=True)
            args.output_build_contract.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
            print(f"Wrote normalized native build contract to {args.output_build_contract}")
        print(f"{args.check_production_sources} matches configured Automake production sources")
        return
    if args.cargo_vendor is None or args.cargo_vendor_config is None:
        parser.error("--cargo-vendor and --cargo-vendor-config are required with --output-package")
    vendor = args.cargo_vendor.resolve()
    output = args.output_package.absolute()
    if any(output.resolve().is_relative_to(root) for root in (source, configured, vendor)):
        raise ValueError("output package must be outside the source, configured, and vendor input trees")
    if vendor.is_relative_to(source) or vendor.is_relative_to(configured):
        raise ValueError("Cargo vendor directory must be outside the source and configured input trees")
    if args.cargo_vendor_config.resolve().is_relative_to(output.resolve()):
        raise ValueError("Cargo vendor configuration must be outside the generated output package")
    changelog = str(source / "debian/changelog")
    epoch = args.source_date_epoch
    if epoch is None:
        epoch = int(subprocess.check_output(["dpkg-parsechangelog", "-l", changelog, "-S", "Timestamp"], text=True).strip())
    if epoch < 0:
        raise ValueError("source date epoch must be non-negative")
    generator = Generator(source, configured, args.workspace_package, args.cargo_vendor, args.cargo_vendor_config)
    synchronize(output, generator.render(epoch))
    print(f"Generated {len(generator.programs)} C++ targets, //{generator.package}:countersyncd, and production_sources.bzl")


if __name__ == "__main__":
    main()

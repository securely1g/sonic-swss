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
import re
import shlex
import shutil
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
RESERVED_TARGETS = {"headers", "countersyncd", "debian_packages", "swss_deb", "swss_dbg_deb", "swss_package_manifest"}


def relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"invalid relative path: {value}")
    return path


def file_mode(path: Path) -> int:
    return 0o755 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o644


def package_mode(install_path: str, mode: int) -> int:
    # dh_fixperms makes files in these directories executable.
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
                "SUBDIRS top_srcdir abs_top_srcdir top_builddir abs_top_builddir",
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
    def __init__(self, source: Path, configured: Path, package: str, vendor: Path, vendor_config: Path, binary_label_prefix: str | None = None):
        self.source = source.resolve()
        self.configured = configured.resolve()
        self.vendor = vendor.resolve()
        self.vendor_config = vendor_config.resolve()
        self.package = relative_path(package).as_posix()
        if binary_label_prefix is not None and not re.fullmatch(r"(?:@[A-Za-z0-9_.+-]+)?", binary_label_prefix):
            raise ValueError(f"invalid binary label prefix: {binary_label_prefix}")
        self.binary_label_prefix = binary_label_prefix
        self.inputs: dict[str, Path] = {}
        self.programs: list[dict[str, Any]] = []
        self.automake_install: list[dict[str, Any]] = []
        self.debhelper_install: list[dict[str, Any]] = []
        self.package_sources: set[str] = set()
        self.cargo_sources: set[str] = set()
        self.headers: set[str] = set()
        self.subdirectories: list[str] = []
        self.compilers: set[str] = set()
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
                raise ValueError("GCOV packaging is not supported by the Bazel release package path")
            self.subdirectories.append(directory)
            if value(variables, "CXX"):
                self.compilers.add(value(variables, "CXX"))
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
                        self.package_sources.add(relative)
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
        if not self.vendor.is_dir() or not self.vendor_config.is_file():
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

    def read_debian(self) -> None:
        ignored_names = {"files", "debhelper-build-stamp", "autoreconf.before", "autoreconf.after"}
        ignored_suffixes = (".substvars", ".debhelper.log")
        for path in walk_files(self.source / "debian"):
            relative = path.relative_to(self.source).as_posix()
            parts = Path(relative).parts
            if len(parts) > 1 and parts[1] in {".debhelper", "swss", "swss-dbg"}:
                continue
            if path.name in ignored_names or path.name.endswith(ignored_suffixes):
                continue
            self.package_sources.add(self.add_input(relative, path))
        for required in ("debian/control", "debian/rules", "debian/changelog", "debian/compat", "debian/swss.install", "bazel/package_deb.py"):
            self.package_sources.add(self.add_input(required, self.source / required))
        for install_file in sorted((self.source / "debian").glob("*.install")):
            for line_number, line in enumerate(install_file.read_text().splitlines(), 1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                fields = shlex.split(line)
                if install_file.name != "swss.install" or len(fields) != 2 or any(character in line for character in "*?[]"):
                    raise ValueError(f"unsupported dh_install entry: {install_file}:{line_number}")
                source = relative_path(fields[0]).as_posix()
                destination = relative_path(fields[1]) / PurePosixPath(source).name
                cargo = source == "target/release/countersyncd"
                if cargo:
                    mode = 0o755
                else:
                    self.package_sources.add(self.add_input(source, self.source / source))
                    mode = file_mode(self.source / source)
                self.debhelper_install.append(
                    {"source": source, "install_path": destination.as_posix(), "mode": package_mode(destination.as_posix(), mode), "kind": "cargo" if cargo else "data"}
                )
        cargo_entries = [entry for entry in self.debhelper_install if entry["kind"] == "cargo"]
        if len(cargo_entries) != 1:
            raise ValueError("debian/swss.install must install exactly one target/release/countersyncd")

    def inventory(self, architecture: str, version: str, epoch: int, deb_build_options: str = "") -> dict[str, Any]:
        installed = [program["install_path"] for program in self.programs]
        installed += [entry["install_path"] for entry in self.automake_install + self.debhelper_install]
        if len(installed) != len(set(installed)):
            raise ValueError("configured package has duplicate install paths")
        filename_version = version.split(":", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9.+~_-]+", filename_version) or not re.fullmatch(r"[a-z0-9-]+", architecture):
            raise ValueError("invalid Debian version or architecture for generated output names")
        packages = [
            {
                "name": name,
                "version": version,
                "architecture": architecture,
                "filename": f"{name}_{filename_version}_{architecture}.deb",
                "label": f"//{self.package}:" + ("swss_deb" if name == "swss" else "swss_dbg_deb"),
            }
            for name in ("swss", "swss-dbg")
        ]
        return {
            "schema_version": 1,
            "configured_subdirectories": self.subdirectories,
            "configured_cxx": sorted(self.compilers),
            "programs": sorted(self.programs, key=lambda item: item["name"]),
            "automake_install": sorted(self.automake_install, key=lambda item: item["install_path"]),
            "debhelper_install": sorted(self.debhelper_install, key=lambda item: item["install_path"]),
            "package_source_files": sorted(self.package_sources),
            "cargo": {
                "label": f"//{self.package}:countersyncd",
                "package_source": "target/release/countersyncd",
                "locked": True,
                "offline": True,
                "vendor_archive": VENDOR_ARCHIVE,
                "vendor_files": self.vendor_file_count,
                "vendor_sha256": hashlib.sha256(self.vendor_archive).hexdigest(),
            },
            "packages": packages,
            "source_date_epoch": epoch,
            "deb_build_options": deb_build_options,
        }

    def module_sources(self, inventory: dict[str, Any]) -> str:
        def source_label(name: str) -> str:
            parts = relative_path(name).parts
            return "//" + parts[0] + ":" + "/".join(parts[1:]) if len(parts) > 1 else "//:" + parts[0]

        programs = {
            program["name"]: {
                "directory": program["directory"],
                "sources": [source_label(name) for name in program["sources"]],
                "install_path": program["install_path"],
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

    def binary_label(self, program: dict[str, Any]) -> str:
        if self.binary_label_prefix is None:
            return ":" + program["name"]
        directory = "" if program["directory"] == "." else program["directory"]
        return self.binary_label_prefix + "//" + directory + ":" + program["name"]

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
            'load(":defs.bzl", "swss_cargo_binary", "swss_debian_packages")',
            "",
            'package(default_visibility = ["//visibility:public"])',
            'exports_files(["inventory.json", "production_sources.bzl", "tools/build_cargo.py", "tools/package_deb.py"])',
            "",
        ]
        if self.binary_label_prefix is None:
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
        lines += rule(
            "swss_debian_packages",
            {
                "name": "debian_packages",
                "binaries": {self.binary_label(program): program["install_path"] for program in inventory["programs"]},
                "cargo": ":countersyncd",
                "srcs": ["src/" + name for name in inventory["package_source_files"]],
                "inventory": "inventory.json",
                "source_root": self.package + "/src",
                "swss_out": inventory["packages"][0]["filename"],
                "debug_out": inventory["packages"][1]["filename"],
                "manifest_out": "swss-package-manifest.json",
            },
        )
        for name, filename in (
            ("swss_deb", inventory["packages"][0]["filename"]),
            ("swss_dbg_deb", inventory["packages"][1]["filename"]),
            ("swss_package_manifest", "swss-package-manifest.json"),
        ):
            lines += rule("filegroup", {"name": name, "srcs": [filename]})
        return "\n".join(lines)

    def render(self, architecture: str, version: str, epoch: int, deb_build_options: str = "") -> dict[str, tuple[bytes, int]]:
        self.read_make_tree()
        self.read_headers_and_cargo()
        self.read_vendor()
        self.read_debian()
        inventory = self.inventory(architecture, version, epoch, deb_build_options)
        rendered = {
            "src/" + name: (path.read_bytes(), file_mode(path))
            for name, path in sorted(self.inputs.items())
        }
        rendered["src/" + VENDOR_ARCHIVE] = (self.vendor_archive, 0o644)
        rendered["src/.cargo/config.toml"] = (self.cargo_config, 0o644)
        for name in ("defs.bzl", "build_cargo.py", "package_deb.py"):
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
    parser.add_argument("--output-package", required=True, type=Path)
    parser.add_argument("--cargo-vendor", required=True, type=Path)
    parser.add_argument("--cargo-vendor-config", required=True, type=Path)
    parser.add_argument("--workspace-package", default="swss")
    parser.add_argument("--binary-label-prefix", help="use module binaries from this repository prefix, for example @sonic_swss")
    parser.add_argument("--architecture")
    parser.add_argument("--source-date-epoch", type=int)
    parser.add_argument("--deb-build-options", default="")
    args = parser.parse_args()
    source = args.source.resolve()
    configured = args.configured_build.resolve()
    vendor = args.cargo_vendor.resolve()
    output = args.output_package.absolute()
    if any(output.resolve().is_relative_to(root) for root in (source, configured, vendor)):
        raise ValueError("output package must be outside the source, configured, and vendor input trees")
    if vendor.is_relative_to(source) or vendor.is_relative_to(configured):
        raise ValueError("Cargo vendor directory must be outside the source and configured input trees")
    if args.cargo_vendor_config.resolve().is_relative_to(output.resolve()):
        raise ValueError("Cargo vendor configuration must be outside the generated output package")
    architecture = args.architecture or subprocess.check_output(["dpkg-architecture", "-qDEB_HOST_ARCH"], text=True).strip()
    changelog = str(source / "debian/changelog")
    version = subprocess.check_output(["dpkg-parsechangelog", "-l", changelog, "-S", "Version"], text=True).strip()
    epoch = args.source_date_epoch
    if epoch is None:
        epoch = int(subprocess.check_output(["dpkg-parsechangelog", "-l", changelog, "-S", "Timestamp"], text=True).strip())
    if epoch < 0:
        raise ValueError("source date epoch must be non-negative")
    generator = Generator(source, configured, args.workspace_package, args.cargo_vendor, args.cargo_vendor_config, args.binary_label_prefix)
    synchronize(output, generator.render(architecture, version, epoch, args.deb_build_options))
    print(f"Generated //{generator.package}:swss_deb and //{generator.package}:swss_dbg_deb from {len(generator.programs)} configured programs")


if __name__ == "__main__":
    main()

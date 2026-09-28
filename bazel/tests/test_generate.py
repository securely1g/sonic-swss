#!/usr/bin/env python3
"""Focused tests for the configured Automake to Bazel boundary."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock


BAZEL_DIRECTORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("swss_bazel_generate", BAZEL_DIRECTORY / "generate.py")
assert SPEC is not None and SPEC.loader is not None
generate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generate)


class Fixture:
    def __init__(self, root: Path):
        self.source = root / "source"
        self.configured = root / "configured"
        self.output = root / "workspace/swss"
        self.vendor = root / "vendor"
        self.vendor_config = root / "vendor.toml"
        for directory in (self.source, self.configured, self.source / "bazel"):
            directory.mkdir(parents=True)
        self.vendor.mkdir()
        self.vendor_config.write_text(
            '[source.crates-io]\nreplace-with = "vendored-sources"\n\n'
            + f'[source.vendored-sources]\ndirectory = "{self.vendor}"\n'
        )
        for name in ("defs.bzl", "build_cargo.py"):
            shutil.copyfile(BAZEL_DIRECTORY / name, self.source / "bazel" / name)
        files = {
            "app/main.cpp": '#include "config.h"\n#include "app/common.h"\nint main() { return answer(); }\n',
            "app/base.cpp": '#include "app/common.h"\nint answer() { return 0; }\n',
            "app/feature.cpp": "int feature() { return 1; }\n",
            "app/common.h": "int answer();\n",
            "app/data.lua": "return 1\n",
            "team/main.cpp": "int main() { return 0; }\n",
            "Cargo.toml": '[workspace]\nmembers = ["crates/countersyncd"]\n',
            "Cargo.lock": 'version = 3\n\n[[package]]\nname = "countersyncd"\nversion = "0.1.0"\n',
            "crates/countersyncd/Cargo.toml": '[package]\nname = "countersyncd"\nversion = "0.1.0"\nedition = "2021"\n',
            "crates/countersyncd/src/main.rs": "fn main() {}\n",
        }
        for name, contents in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        (self.configured / "config.h").write_text("#define FIXTURE 1\n")
        self.write_makefiles(team=True)

    def write_makefiles(self, team: bool) -> None:
        common = (
            f"abs_top_srcdir = {self.source}\nCXX = g++\nEXEEXT =\n"
            + "INSTALL_DATA = /usr/bin/install -c -m 644\ninstall_sh_DATA = ./install-sh -c -m 644\n"
        )
        (self.configured / "Makefile").write_text(
            common + "SUBDIRS = app\n" + f"WITH_TEAM = {'yes' if team else 'no'}\n"
            + "ifeq ($(WITH_TEAM),yes)\nSUBDIRS += team\nendif\n"
        )
        (self.configured / "app").mkdir(exist_ok=True)
        (self.configured / "app/Makefile").write_text(
            common
            + f"top_srcdir = {self.source}\n"
            + "DEFAULT_INCLUDES = -I. -I$(top_srcdir)/app -I..\n"
            + "INCLUDES = -I $(top_srcdir)\nDEFS = -DHAVE_CONFIG_H\n"
            + f"CXXFLAGS = -g -O2 -ffile-prefix-map={self.source}=.\n"
            + "CPPFLAGS = -DGLOBAL\nAM_CPPFLAGS = -DFALLBACK\nAM_CXXFLAGS = -Wall\n"
            + "AM_LDFLAGS = -Wl,--as-needed\nLIBS = -lm\n"
            + "bindir = /usr/bin\nbin_PROGRAMS = example\n"
            + "example_SOURCES = main.cpp base.cpp common.h\nexample_OBJECTS = example-main.o example-base.o\n"
            + "FEATURE = yes\nifeq ($(FEATURE),yes)\nexample_SOURCES += feature.cpp\nexample_OBJECTS += example-feature.o\nendif\n"
            + "example_CPPFLAGS = -DEXAMPLE\nexample_LDADD = -lm\n"
            + "datadir = /usr/share\nexampledir = $(datadir)/swss\ndist_example_DATA = data.lua\n"
        )
        (self.configured / "team").mkdir(exist_ok=True)
        (self.configured / "team/Makefile").write_text(
            common + "bindir = /usr/bin\nbin_PROGRAMS = team\nteam_SOURCES = main.cpp\nteam_OBJECTS = team-main.o\n"
        )

    def render(self) -> dict[str, tuple[bytes, int]]:
        return generate.Generator(self.source, self.configured, "swss", self.vendor, self.vendor_config).render(1457553600)


class GeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="swss-bazel-test-")
        self.fixture = Fixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_configured_selection_flags_and_install_inventory(self) -> None:
        rendered = self.fixture.render()
        inventory = json.loads(rendered["inventory.json"][0])
        self.assertEqual([item["name"] for item in inventory["programs"]], ["example", "team"])
        example = inventory["programs"][0]
        self.assertEqual(example["sources"], ["app/main.cpp", "app/base.cpp", "app/feature.cpp"])
        self.assertIn("-DEXAMPLE", example["compile_options"])
        self.assertNotIn("-DFALLBACK", example["compile_options"])
        self.assertIn("-DGLOBAL", example["compile_options"])
        self.assertIn("-ffile-prefix-map=swss/src=.", example["compile_options"])
        self.assertEqual(example["link_options"][-2:], ["-lm", "-lm"])
        self.assertEqual(inventory["automake_install"][0]["install_path"], "usr/share/swss/data.lua")
        self.assertNotIn(str(self.fixture.source).encode(), rendered["BUILD.bazel"][0])
        self.assertNotIn(str(self.fixture.configured).encode(), rendered["inventory.json"][0])
        self.assertTrue(inventory["cargo"]["offline"])
        self.assertIn(b'directory = "vendor"', rendered["src/.cargo/config.toml"][0])

    def test_vendor_bytes_are_deterministic_declared_inputs(self) -> None:
        crate = self.fixture.vendor / "fixture-1.0.0"
        crate.mkdir()
        (crate / "BUILD.bazel").write_text("not a generated workspace package\n")
        (crate / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
        first = self.fixture.render()
        second = self.fixture.render()
        archive = "src/" + generate.VENDOR_ARCHIVE
        self.assertEqual(first[archive], second[archive])
        self.assertNotIn("src/vendor/fixture-1.0.0/BUILD.bazel", first)
        (crate / "lib.rs").write_text("pub fn value() -> u8 { 2 }\n")
        changed = self.fixture.render()
        self.assertNotEqual(first[archive][0], changed[archive][0])

    def test_module_sources_preserve_configured_inventory(self) -> None:
        rendered = self.fixture.render()
        sources = rendered["production_sources.bzl"][0].decode()
        self.assertIn('"//app:main.cpp"', sources)
        self.assertIn('"//app:feature.cpp"', sources)
        self.assertIn('"install_path": "usr/bin/example"', sources)
        self.assertIn('"source": "app/data.lua"', sources)

    def test_check_production_sources_without_cargo_or_package(self) -> None:
        expected = self.fixture.render()["production_sources.bzl"][0]
        checked_in = self.fixture.source / "bazel/production_sources.bzl"
        checked_in.write_bytes(expected)
        shutil.rmtree(self.fixture.vendor)
        self.fixture.vendor_config.unlink()
        command = [
            sys.executable,
            str(BAZEL_DIRECTORY / "generate.py"),
            "--source", str(self.fixture.source),
            "--configured-build", str(self.fixture.configured),
            "--check-production-sources", str(checked_in),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.fixture.output.exists())

        makefile = self.fixture.configured / "app/Makefile"
        makefile.write_text(makefile.read_text().replace("main.cpp base.cpp common.h", "base.cpp main.cpp common.h"))
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match configured Automake production sources", result.stderr)
        self.assertEqual(checked_in.read_bytes(), expected)
        self.assertFalse(self.fixture.output.exists())

    def test_synchronization_preserves_unchanged_files_and_removes_stale_inputs(self) -> None:
        rendered = self.fixture.render()
        generate.synchronize(self.fixture.output, rendered)
        before = {path.relative_to(self.fixture.output): path.stat().st_mtime_ns for path in self.fixture.output.rglob("*") if path.is_file()}
        generate.synchronize(self.fixture.output, self.fixture.render())
        after = {path.relative_to(self.fixture.output): path.stat().st_mtime_ns for path in self.fixture.output.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.fixture.write_makefiles(team=False)
        generate.synchronize(self.fixture.output, self.fixture.render())
        self.assertFalse((self.fixture.output / "src/team/main.cpp").exists())
        inventory = json.loads((self.fixture.output / "inventory.json").read_text())
        self.assertEqual([item["name"] for item in inventory["programs"]], ["example"])

    def test_missing_or_unsupported_inputs_fail_before_generation(self) -> None:
        makefile = self.fixture.configured / "app/Makefile"
        original = makefile.read_text()
        makefile.write_text(original + "BUILT_SOURCES = generated.cpp\n")
        with self.assertRaisesRegex(ValueError, "generated sources"):
            self.fixture.render()
        makefile.write_text(original.replace("example-feature.o", ""))
        with self.assertRaisesRegex(ValueError, "object list"):
            self.fixture.render()
        makefile.write_text(original + "INCLUDES += -I\n")
        with self.assertRaisesRegex(ValueError, "missing path"):
            self.fixture.render()

    def test_fpmsyncd_query_defaults_only_unset_legacy_include_root(self) -> None:
        directory = self.fixture.configured / "fpmsyncd"
        directory.mkdir()
        makefile = directory / "Makefile"
        contents = f"abs_top_srcdir = {self.fixture.source}\nINCLUDES = -I $(FPM_PATH)\n"
        makefile.write_text(contents)
        with unittest.mock.patch.dict(os.environ):
            os.environ.pop("FPM_PATH", None)
            variables = generate.query_make(directory)
        self.assertEqual(generate.value(variables, "INCLUDES"), f"-I {self.fixture.source}/fpmsyncd")
        makefile.write_text(contents + "FPM_PATH = /usr/include/fpm\n")
        variables = generate.query_make(directory)
        self.assertEqual(generate.value(variables, "INCLUDES"), "-I /usr/include/fpm")
        self.assertEqual(makefile.read_text(), contents + "FPM_PATH = /usr/include/fpm\n")

    def test_synchronization_refuses_an_unowned_output_directory(self) -> None:
        self.fixture.output.mkdir(parents=True)
        (self.fixture.output / "keep.txt").write_text("user file\n")
        with self.assertRaisesRegex(ValueError, "unowned"):
            generate.synchronize(self.fixture.output, self.fixture.render())
        self.assertEqual((self.fixture.output / "keep.txt").read_text(), "user file\n")


if __name__ == "__main__":
    unittest.main()

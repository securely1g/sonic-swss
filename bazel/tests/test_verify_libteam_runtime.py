"""Regressions for SWSS's installed libteam consumer boundary."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_libteam_runtime import check_loaded, check_needed


class LibteamRuntimeTest(unittest.TestCase):
    def test_runtime_target_preserves_both_consumers_without_duplicate_labels(self):
        root = Path(__file__).resolve().parents[2]
        namespace = {}
        exec((root / "tools/bazel/deps.bzl").read_text(), namespace)
        expected = set(namespace["TEAM_DEPS"] + namespace["TLM_TEAM_DEPS"])
        captured = {}

        class Depset:
            def __init__(self, values):
                self.values = list(dict.fromkeys(values))

            def to_list(self):
                return self.values

        def runtime_libraries(name, deps):
            if len(deps) != len(set(deps)):
                raise ValueError("duplicate dependency label")
            captured[name] = deps

        namespace.update(load=lambda *args: None, cc_test=lambda **kwargs: None,
                         depset=Depset, runtime_libraries=runtime_libraries)
        build = (root / "bazel/BUILD.bazel").read_text()
        exec(build, namespace)
        actual = captured["libteam_consumer_runtime_libraries"]
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), len(expected))
        self.assertTrue({"@libteam//:libteam", "@libteam//:libteamdctl",
                         "@swss_debian//libjansson-dev:libjansson"} <= expected)
        # The former expression fails before compilation, just as Bazel's
        # label_list did in CodeQL job 127121481405.
        broken = build.replace("depset(TEAM_DEPS + TLM_TEAM_DEPS).to_list()",
                               "TEAM_DEPS + TLM_TEAM_DEPS")
        with self.assertRaisesRegex(ValueError, "duplicate dependency label"):
            exec(broken, namespace)

    def test_required_library_split_matches_the_native_consumers(self):
        team = "Shared library: [libteam.so.5]\nShared library: [libteamdctl.so.0]\nShared library: [libc.so.6]"
        self.assertEqual(check_needed("teamsyncd", team), ["libc.so.6", "libteam.so.5", "libteamdctl.so.0"])
        self.assertEqual(check_needed("tlm_teamd", "Shared library: [libteamdctl.so.0]"), ["libteamdctl.so.0"])
        self.assertEqual(check_needed("teammgrd", "Shared library: [libc.so.6]"), ["libc.so.6"])
        for program, changed in (("teamsyncd", "Shared library: [libteam.so.5]"),
                                 ("tlm_teamd", team), ("teammgrd", team)):
            with self.subTest(program=program), self.assertRaisesRegex(ValueError, "Unexpected libteam dependencies"):
                check_needed(program, changed)

    def test_loader_requires_the_installed_owner_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            libraries = {name: stage / name for name in ("libteam.so.5", "libteamdctl.so.0")}
            for path in libraries.values():
                path.write_bytes(b"owned payload")
            loaded = "\n".join(name + " => " + str(path) + " (0x0)" for name, path in libraries.items())
            check_loaded("teamsyncd", loaded, libraries)
            control = "libteamdctl.so.0 => " + str(libraries["libteamdctl.so.0"]) + " (0x0)"
            check_loaded("tlm_teamd", control, libraries)
            check_loaded("teammgrd", "libc.so.6 => /lib/libc.so.6 (0x0)", libraries)
            wrong = loaded.replace(str(stage), "/usr/lib/x86_64-linux-gnu")
            with self.assertRaisesRegex(ValueError, "outside the installed owner TAR"):
                check_loaded("teamsyncd", wrong, libraries)
            for invalid in (control, loaded + "\n" + control, loaded + "\nlibother.so => not found"):
                with self.subTest(loader=invalid), self.assertRaises(ValueError):
                    check_loaded("teamsyncd", invalid, libraries)


if __name__ == "__main__":
    unittest.main()

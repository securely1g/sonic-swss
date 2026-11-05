"""Regressions for SWSS's installed libteam consumer boundary."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_libteam_runtime import check_loaded, check_needed


class LibteamRuntimeTest(unittest.TestCase):
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

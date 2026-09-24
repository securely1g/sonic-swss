#!/usr/bin/env python3
"""Tests for the validated handoff from Bazel outputs to debhelper."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


BAZEL_DIRECTORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("swss_bazel_package", BAZEL_DIRECTORY / "package_deb.py")
assert SPEC is not None and SPEC.loader is not None
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class PackageStageTest(unittest.TestCase):
    def test_install_uses_only_the_validated_stage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swss-stage-test-") as temporary:
            root = Path(temporary)
            stage = root / "stage"
            binary = stage / "usr/bin/example"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"declared binary")
            binary.chmod(0o755)
            (stage / package.STAGE_MANIFEST).write_text(
                json.dumps({"schema_version": 1, "files": [{"path": "usr/bin/example", "mode": 0o755, "sha256": package.digest(binary)}]})
            )
            destination = root / "debian/swss"
            package.install_stage(stage, destination)
            self.assertEqual((destination / "usr/bin/example").read_bytes(), b"declared binary")
            self.assertEqual((destination / "usr/bin/example").stat().st_mode & 0o777, 0o755)
            binary.write_bytes(b"stale or unrelated binary")
            with self.assertRaisesRegex(ValueError, "staged input changed"):
                package.install_stage(stage, root / "second")
            binary.write_bytes(b"declared binary")
            (stage / "usr/bin/extra").write_bytes(b"undeclared")
            with self.assertRaisesRegex(ValueError, "inventory differs"):
                package.check_stage(stage)


if __name__ == "__main__":
    unittest.main()

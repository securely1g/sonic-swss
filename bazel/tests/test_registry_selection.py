"""Exercise the temporary registry selection used by both SWSS CI workflows."""

import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MAIN = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/main"
OBSERVED_MAIN = "a" * 40
WORKFLOWS = ("bazel.yml", "codeql-analysis.yml")


def selection_script(workflow):
    text = (ROOT / ".github/workflows" / workflow).read_text()
    start = "          python3 - <<'PYTHON'\n"
    assert text.count(start) == 1
    return textwrap.dedent(text.split(start, 1)[1].split("          PYTHON\n", 1)[0])


class RegistrySelectionTest(unittest.TestCase):
    def run_selection(self, workflow, declared, working):
        def git_output(command, **kwargs):
            if command == ["git", "show", "HEAD:.bazelrc"]:
                return declared
            if command == ["git", "ls-remote",
                           "https://github.com/securely1g/sonic-bazel-registry.git",
                           "refs/heads/main"]:
                return OBSERVED_MAIN + "\trefs/heads/main\n"
            self.fail("Unexpected subprocess: " + repr(command))

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / ".bazelrc").write_text(working)
            previous = Path.cwd()
            try:
                os.chdir(workspace)
                with mock.patch("subprocess.check_output", side_effect=git_output):
                    exec(compile(selection_script(workflow), workflow, "exec"), {})
                evidence = workspace / "artifacts/validation"
                return {
                    "declared": (evidence / "declared.bazelrc").read_text(),
                    "effective": (evidence / "effective.bazelrc").read_text(),
                    "working": (workspace / ".bazelrc").read_text(),
                    "receipt": json.loads((evidence / "registry-inputs.json").read_text()),
                }
            finally:
                os.chdir(previous)

    def test_both_workflows_replace_only_the_declared_endpoint(self):
        declared = (ROOT / ".bazelrc").read_text()
        self.assertEqual(declared.count(MAIN), 1)
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                result = self.run_selection(workflow, declared, declared)
                receipt = result["receipt"]
                self.assertRegex(receipt["observed_revision"], r"^[0-9a-f]{40}$")
                selected = MAIN.rsplit("/", 1)[0] + "/" + receipt["observed_revision"]
                self.assertEqual(receipt["registry"], selected)
                self.assertEqual(receipt["declared_registry"], MAIN)
                self.assertEqual(receipt["maintained_revision"], OBSERVED_MAIN)
                self.assertTrue(receipt["draft_validation"])
                self.assertRegex(receipt["prerequisite"],
                                 r"^https://github.com/securely1g/sonic-bazel-registry/pull/[0-9]+$")
                self.assertEqual(result["declared"], declared)
                self.assertEqual(result["effective"], declared.replace(MAIN, selected))
                self.assertEqual(result["working"], result["effective"])
                self.assertNotIn(MAIN, result["effective"])
                self.assertEqual(result["effective"].count(selected), 1)

    def test_unrelated_working_configuration_changes_are_rejected(self):
        declared = (ROOT / ".bazelrc").read_text()
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow), self.assertRaises(AssertionError):
                self.run_selection(workflow, declared, declared + "\nbuild --jobs=99\n")

    def test_missing_or_repeated_endpoint_is_rejected(self):
        declared = (ROOT / ".bazelrc").read_text()
        for invalid in (declared.replace(MAIN, "https://invalid.example/registry"),
                        declared + "\ncommon --registry=" + MAIN + "\n"):
            for workflow in WORKFLOWS:
                with self.subTest(workflow=workflow, invalid=invalid), self.assertRaises(AssertionError):
                    self.run_selection(workflow, invalid, invalid)


if __name__ == "__main__":
    unittest.main()

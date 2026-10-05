#!/usr/bin/env python3
"""Select the pending shared Serde registry for Draft PR validation only.

Remove this helper and its workflow steps once the registry entries land.
The tracked .bazelrc continues to select main for ordinary builds.
"""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

MAIN = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/main"
BRANCH = "codex/common-shared-serde"
URL = MAIN.removesuffix("main") + BRANCH

configuration = Path(".bazelrc").read_text()
if configuration.count("common --registry=" + MAIN + "\n") != 1:
    raise ValueError("Expected one maintained SONiC registry endpoint")
ref = "refs/heads/" + BRANCH
revision = subprocess.check_output([
    "git", "ls-remote", "https://github.com/securely1g/sonic-bazel-registry.git", ref,
], text=True).split()
if len(revision) != 2 or revision[1] != ref or len(revision[0]) != 40:
    raise ValueError("Could not record the Draft registry branch")
real_bazel = shutil.which("bazel")
if not real_bazel:
    raise ValueError("Set up Bazel before selecting the Draft registry")
directory = Path(os.environ["RUNNER_TEMP"]) / "shared-serde-registry"
directory.mkdir(parents=True, exist_ok=True)
rc = directory / "draft.bazelrc"
rc.write_text(configuration.replace(MAIN, URL))
bin_directory = directory / "bin"
bin_directory.mkdir(exist_ok=True)
wrapper = bin_directory / "bazel"
# Bazel recognizes --version only before startup options. It needs no registry.
wrapper.write_text('#!/bin/sh\nif [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then\n'
                  + "  exec " + shlex.quote(real_bazel) + ' "$@"\nfi\nexec ' + shlex.join([
    real_bazel, "--noworkspace_rc", "--bazelrc=" + str(rc),
]) + ' "$@"\n')
wrapper.chmod(0o755)
with open(os.environ["GITHUB_PATH"], "a") as output:
    print(bin_directory, file=output)
with open(os.environ["GITHUB_ENV"], "a") as output:
    print("SONIC_BAZEL_REGISTRY_URL=" + URL, file=output)
    print("SONIC_BAZEL_REGISTRY_REVISION=" + revision[0], file=output)
print("Draft registry:", URL, "observed revision:", revision[0])
artifacts = Path(sys.argv[1])
artifacts.mkdir(parents=True, exist_ok=True)
(artifacts / "effective.bazelrc").write_text(rc.read_text())
(artifacts / "draft-registry.json").write_text(json.dumps({
    "registry": URL, "observed_revision": revision[0], "draft_validation": True,
}, indent=2) + "\n")

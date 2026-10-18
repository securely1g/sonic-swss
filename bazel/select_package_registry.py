"""Select reviewed Draft package dependencies without changing the workspace RC."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess

BRANCH = "codex/source-debian-runtime-permissions"
REVISION = "7f8a0fd08ceb36cfaca9645a24e7f1dd174e0302"
REGISTRY = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"


def main():
    observed = subprocess.check_output([
        "git", "ls-remote", "https://github.com/securely1g/sonic-bazel-registry.git",
        "refs/heads/" + BRANCH,
    ], text=True).split()[0]
    if observed != REVISION:
        raise ValueError(f"Draft registry moved: expected {REVISION}, observed {observed}")
    directory = Path(os.environ["RUNNER_TEMP"]) / "package-registry"
    directory.mkdir()
    rc = directory / "effective.bazelrc"
    source = Path(".bazelrc").read_text()
    if REGISTRY + "main" not in source:
        raise ValueError("The workspace no longer uses the expected main registry")
    rc.write_text(source.replace(REGISTRY + "main", REGISTRY + BRANCH))
    binary = shutil.which("bazel")
    if not binary:
        raise ValueError("Set up Bazel before selecting the Draft registry")
    wrapper = directory / "bazel"
    quoted_binary = shlex.quote(binary)
    # Bazel accepts --version only before startup options. Keep the existing
    # CI version probe working without changing dependency selection for builds.
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then\n'
        + "  exec " + quoted_binary + ' "$@"\nfi\n'
        + "exec " + quoted_binary + " --noworkspace_rc --bazelrc="
        + shlex.quote(str(rc)) + ' "$@"\n')
    wrapper.chmod(0o755)
    with open(os.environ["GITHUB_PATH"], "a") as output:
        output.write(str(directory) + "\n")


if __name__ == "__main__":
    main()

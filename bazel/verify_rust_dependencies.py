"""Check that the real SWSS program and its Common test share Serde targets."""

import json


def serde_labels(output):
    labels = {line.split()[0] for line in output.splitlines() if line.strip()}
    return {
        name: sorted(label for label in labels if label.endswith(":" + name))
        for name in ("serde", "serde_core")
    }


def verify(options, artifact_directory, run):
    # Compiler and binding-generator toolchains can legitimately use private
    # Rust dependencies. Inspect the libraries' declared dependencies instead.
    prefix = ["bazel", "cquery", *options, "--noimplicit_deps", "--output=label"]
    graph = run(prefix + [
        'kind("rust_library rule", deps(set('
        '//crates/countersyncd:countersyncd '
        '//crates/countersyncd:common_rust_test)))',
    ], capture=True)
    (artifact_directory / "rust-dependencies.txt").write_text(graph)
    reference = run(prefix + [
        'kind("rust_library rule", deps(@sonic_swss_common//:serde))',
    ], capture=True)
    (artifact_directory / "common-serde-dependencies.txt").write_text(reference)
    actual = serde_labels(graph)
    expected = serde_labels(reference)
    if any(len(labels) != 1 for labels in expected.values()) or actual != expected:
        raise ValueError("SWSS and Common must use one shared serde and serde_core library")
    common = sorted({
        line.split()[0] for line in graph.splitlines()
        if line.split() and line.split()[0].endswith("//crates/swss-common:swss_common")
    })
    if len(common) != 1:
        raise ValueError("SWSS must compile Common through its public Rust library target")
    report = {"common_library": common[0], "shared_rust_libraries": actual}
    (artifact_directory / "rust-dependencies.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    from ci_production import ARCHITECTURES, run

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    parser.add_argument("--artifact-directory", type=Path, required=True)
    args = parser.parse_args()
    args.artifact_directory.mkdir(parents=True, exist_ok=True)
    verify([
        "--config=release",
        "--lockfile_mode=update",
        "--platforms=" + ARCHITECTURES[args.architecture][2],
    ], args.artifact_directory, run)

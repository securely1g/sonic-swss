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
        'kind("rust_library rule", deps(@sonic_rust_deps//:serde))',
    ], capture=True)
    (artifact_directory / "shared-serde-dependencies.txt").write_text(reference)
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

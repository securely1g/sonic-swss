#!/usr/bin/env python3
"""Retain the shared Rust graph and the Cargo inputs checked against it."""

import argparse
import json
from pathlib import Path
import shutil


def collect_consumer(consumer, directory):
    workspace = Path(consumer["workspace"])
    for name in {"Cargo.lock", *consumer["input_sha256"]}:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Consumer evidence must use paths inside its workspace")
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(workspace / relative, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text())
    artifact_directory = args.receipt.parent
    shared = Path(receipt["shared_dependency"]["workspace"])
    target = artifact_directory / "shared"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("Cargo.toml", "Cargo.lock", "Cargo.Bazel.lock", "MODULE.bazel.lock", "preparation.json"):
        shutil.copy2(shared / name, target / name)
    for index, consumer in enumerate(receipt["dependencies"]):
        collect_consumer(consumer, artifact_directory / f"consumer-{index}")
    collect_consumer(receipt, artifact_directory)
    shutil.copy2(Path(receipt["workspace"]) / "MODULE.bazel.lock", artifact_directory)


if __name__ == "__main__":
    main()

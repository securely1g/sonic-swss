#!/usr/bin/env python3
"""Run the pinned shared Rust metadata preparation before Bazel resolution."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request


# This bootstrap pin is separate from the Bazel module version: preparation runs
# before Bazel can resolve the module graph. Keep the source and hash together.
PREPARATION_REVISION = "ebe63820a323412eb93733dc8a0e9854e2b1d8fc"
PREPARATION_SHA256 = "8bedc0cc85e56fe548100b5246f8f52d5c198a60ed8c54f37c06e3421692fbd3"
PREPARATION_URL = (
    "https://raw.githubusercontent.com/securely1g/sonic-build-infra/"
    f"{PREPARATION_REVISION}/tools/rust/prepare.py"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--prepared-common", type=Path)
    args, forwarded = parser.parse_known_args()
    workspace = Path(__file__).resolve().parents[2]
    preparation = ["--overrides-rc", str(workspace / ".bazelrc.rust")]
    if args.prepared_common:
        # Image preparation has already verified this checkout and generated its
        # metadata. The image caller keeps the same Common module override.
        prepared = args.prepared_common.resolve()
        metadata = json.loads((prepared / "Cargo.Bazel.lock").read_text())
        if not metadata.get("crates"):
            raise SystemExit("Prepare Common's Rust metadata first")
        preparation.append(f"--bazel-arg=--override_module=sonic-swss-common={prepared}")
    else:
        preparation.extend([
            "--dependency=sonic-swss-common=sonic_swss_common",
            "--staging-dir", str(workspace / ".cargo-bazel-prep"),
        ])
    with urllib.request.urlopen(PREPARATION_URL, timeout=60) as response:
        source = response.read()
    if hashlib.sha256(source).hexdigest() != PREPARATION_SHA256:
        raise SystemExit("Shared Rust preparation checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="sonic-rust-preparation-") as temporary:
        helper = Path(temporary) / "prepare.py"
        helper.write_bytes(source)
        return subprocess.call([
            sys.executable,
            str(helper),
            "--workspace", str(workspace),
            "--repository", "crates",
            *preparation, *forwarded,
        ])


if __name__ == "__main__":
    sys.exit(main())

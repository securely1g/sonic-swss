#!/usr/bin/env bash
set -euo pipefail

test_binary="$1"
test_package="$2"
shift 2

workspace_runfiles="${TEST_SRCDIR:?}/${TEST_WORKSPACE:?}"
if [[ -n "${XML_OUTPUT_FILE:-}" && -z "${GTEST_OUTPUT:-}" ]]; then
    export GTEST_OUTPUT="xml:${XML_OUTPUT_FILE}"
fi

cd "${workspace_runfiles}/${test_package}"
exec "${workspace_runfiles}/${test_binary}" "$@"

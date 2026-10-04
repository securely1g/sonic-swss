# SWSS Bazel CI validation

For build commands and routine source, dependency or program changes, start
with the [maintainer guide](../tools/bazel/README.md). Normal builds load the
maintained component BUILD files directly. These helpers validate the same
feature in CI and should be updated alongside its build definitions.

## Run the production checks

Run helper tests without configuring Automake or compiling SWSS:

```sh
python3 -m unittest discover -s bazel/tests -v
```

To reproduce native CI, first [configure Automake and record its build contract](native-build-contract.md#configure-the-native-source-inventory)
in a native Debian Trixie environment. Then run from the repository root:

```sh
python3 bazel/ci_production.py build \
  --architecture amd64 \
  --native-contract /absolute/path/to/native-build-contract.json \
  --artifact-directory /absolute/path/to/empty-output-directory
```

Use `--architecture arm64` on a native ARM64 host. Add `--mode clean` to use a
fresh Bazel output base, disable action-result reuse and run the normal process
sandbox. The [workflow](../.github/workflows/bazel.yml) supports the same mode
through its `clean` input or a `Bazel-Clean: true` trailer on the PR head commit.

## What CI checks

The Bazel workflow builds the normal Trixie release configuration on native
AMD64 and ARM64 runners. It queries production programs through
`//dist:cpp_binaries`, builds each label and the aggregate, and verifies exactly
one executable per program. It checks ELF architecture, position-independent
executable format, RELRO and immediate symbol binding. Both architectures run
`//gcovpreload:gcovpreload_test` with test-result reuse disabled to prove the
constructor-only preload library installs its signal handlers at startup.
The workflow also runs the pinned Buildifier formatting check.

Each job configures Automake independently. The
[native contract comparison](native-build-contract.md) checks the selected
programs and sources, target compiler/linker options, local include order and
mapped dependency labels against the configured Bazel targets. It rejects
missing or extra programs and sources. The full reference documents the JSON
schema, option classifications, reviewed toolchain boundary and intentional
differences.

The uploaded inspection bundle contains executables, build/test logs, the
resolved module graph and generated `MODULE.bazel.lock`, the native contract,
the configured production graph and rule capture, the comparison result, and
a JSON build receipt binding inputs and outputs to checksums and the checkout
revision. CI resolves the SONiC registry `main` branch plus BCR; module versions,
source archives and checksums remain pinned.

The comparison does not inspect toolchain actions, response files, transitive
provider paths, external header precedence, native link ordering or runtime
equivalence. DEBUG, ASAN, GCOV, Rust and Cargo settings remain outside the
native C++ comparison.

## Rust validation

Each native AMD64/ARM64 job also builds `countersyncd` in the release
configuration, executes its version command and runs
`//crates/countersyncd:common_rust_test`. The test creates and clones a native
Common string, drops the original and roundtrips the surviving value through
SWSS's JSON serializer. This checks native linkage and shared Serde traits
without starting Redis.

The job inspects the declared Rust dependencies of both the program and test.
It requires Common's public library target and one shared pair of `serde` and
`serde_core` targets. The `sonic-swss-rust-metadata-*` artifacts retain the
program, test results, resolved target lists, tracked Cargo inputs and generated
`MODULE.bazel.lock`. No preparation script or `Cargo.Bazel.lock` is needed.

To repeat the Rust checks on native Debian Trixie:

```sh
bazel test --config=release --test_output=errors \
  //crates/countersyncd:countersyncd \
  //crates/countersyncd:common_rust_test
bazel run --config=release //crates/countersyncd:countersyncd -- --version
python3 bazel/verify_rust_dependencies.py \
  --architecture amd64 --artifact-directory artifacts/rust
```

On native ARM64, add `--config=aarch64` to the Bazel commands and use
`--architecture arm64`. These checks cover the program's build and Common
interface; they do not start the daemon or validate a container image.

## Runtime and debug package validation

Each native AMD64/ARM64 production job builds the runtime and detached-symbol
targets and checks all 65 installed files and 30 ELF/debug pairs, including C++
and Rust source lookup with GDB. Package validation is part of the production
check, after the independent C++ and Rust artifacts have been uploaded.

To build, validate and retain the same package evidence locally, configure
Automake and [record the native contract](native-build-contract.md#configure-the-native-source-inventory)
first, then run in native Debian Trixie:

```sh
python3 bazel/ci_runtime_package.py \
  --architecture amd64 \
  --native-contract /absolute/path/to/native-build-contract.json \
  --artifact-directory /absolute/path/to/empty-package-output-directory
```

Use `--architecture arm64` on a native ARM64 host. To inspect already-built
tars for installed paths, data bytes, ownership, permissions, ELF architecture,
build IDs, debug-link checksums and GDB source-line lookup, run:

```sh
python3 bazel/verify_runtime_package.py --architecture amd64 \
  --native-contract /absolute/path/to/native-build-contract.json \
  --runtime bazel-bin/dist/swss_pkg_rttar.tar \
  --debug bazel-bin/dist/swss_pkg.debug_symbols.tar
```

Use `bazel cquery --config=release //dist:swss_pkg --output=files` and the
`.debug_symbols` target for exact paths when the output layout differs. The
verifier requires a contract from the same checkout revision and native
architecture. It compares every installed path, mode and data file with the
native inventory and `debian/swss.install`.

The `sonic-swss-packages-amd64` and `sonic-swss-packages-arm64` artifacts contain
the runtime tar, matching symbols, native contract, validation report, resolved
module graph, generated lockfile, build log and dependency runtime evidence.
They also retain the Common/Serde test and graph results used by package
validation. Their manifest records the tested revision, native architecture,
target platform and file hashes. These tars do not establish full daemon
runtime or installed-image behavior.

## Source Protobuf runtime validation

SWSS and DASH share the source-built Protobuf 3.21.12 runtime from
`protobuf-legacy`, whose consumer target links `libprotobuf.so.32`. DASH's
compiler is built from the same upstream release. SWSS's APT dependency set
does not import Protobuf headers or runtime libraries. The selected Common
ZeroMQ transport uses its binary serializer; the selected SAI Redis libraries
do not enable the optional gRPC/DASH-SAI backend.

Native CI also runs `//bazel:protobuf_runtime_test` against orchagent's declared
dependency set. It serializes a DASH message and verifies that the loaded
Protobuf functions belong to one shared runtime. CI checks the actual orchagent
executable with the native loader's `--list` option using separately staged
source-built DASH and Protobuf runtime archives. The ELF bytes remain unchanged.
For this temporary stage, `--inhibit-rpath ""` and `--library-path` select the
staged libraries, and exact path checks reject fallback to another Protobuf or
DASH library. The native test also executes DASH serialization against these
packaged libraries.

Package CI repeats these checks on `usr/bin/orchagent` extracted from the SWSS
runtime tar. It stages DASH and Protobuf archives separately, executes the DASH
serialization probe and verifies the dependencies' matching debug pairs. The
package report records this as `protobuf_runtime`; retained dependency artifacts
live under `dependencies/`. SWSS's installed payload stays unchanged.

This does not check complete orchagent relocation, default installed-system
library search paths or Redis/SAI daemon behavior. Package and RPATH checks
remain separate. Both dependencies' matching debug archives, ELF identities,
build IDs and debug checksums are retained with the evidence. SWSS's installed
inventory stays separate from these dependency payloads.

## CodeQL coverage

The C++ CodeQL job builds the production programs, selected GCOV preload
sources and the DASH/Protobuf runtime test after analyzer initialization, with
action caches disabled. Test programs compile without execution; packaging
remains in native CI. The extraction receipt reads the retained production
graph from its own build receipt and requires both native test source files
alongside those production sources. It verifies the graph hash, checkout
revision and build-definition digest without configuring Automake.

Tracing excludes the pinned SAI and shared build-tools preparation actions,
which start no C/C++ compiler, so CodeQL's preload does not enter their strict
runtime dependency checks. This exception matches exact preparation scripts,
output paths, architectures and archive arguments; a Lua regression check runs
before analyzer initialization. SAI metadata and SWSS sources compile in
separate Bazel actions with the standard C++ matchers active. The receipt
requires extraction of all selected tracked sources and records additional
and excluded paths. It does not claim historical analyzer parity or runtime
coverage.

## Helper ownership

| Helper | Purpose |
| --- | --- |
| [native_build_contract.py](native_build_contract.py) | Reads configured Automake sources, install paths and compiler/linker settings into an independent JSON contract. |
| [production_graph.py](production_graph.py) | Captures configured Bazel programs and sources for native comparison and CodeQL coverage. |
| [compare_build_contract.py](compare_build_contract.py) | Compares the retained native contract and configured Bazel rules. |
| [ci_production.py](ci_production.py) | Builds, inspects and records production outputs and their validation receipts. |
| [verify_protobuf_runtime.py](verify_protobuf_runtime.py) | Checks staged DASH and Protobuf runtime libraries. |
| [verify_rust_dependencies.py](verify_rust_dependencies.py) | Checks that countersyncd and its Common test use the owning library and one set of Serde targets. |
| [verify_runtime_package.py](verify_runtime_package.py) | Validates installed SWSS files and matching C++/Rust debug symbols against the native contract. |
| [ci_runtime_package.py](ci_runtime_package.py) | Builds and validates runtime/debug archives, including installed-orchagent Protobuf checks, and records package evidence. |

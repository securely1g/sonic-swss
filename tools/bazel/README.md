# Building SWSS with Bazel

This repository defines Bazel module targets for the SWSS C++ programs. The
existing generator supplies their production source lists and retains the
locked Cargo action for `countersyncd`.
The Common dependency is fetched from
[securely1g/sonic-swss-common](https://github.com/securely1g/sonic-swss-common)
at revision `093a849f01722afb4730e685b3eb4f22a9bc9191`. That source owns its
Bazel targets and generates the configuration schema from source inputs. The
normal SWSS configuration enables Common's YANG C++ sources.

For an external module caller, use the
[canonical caller guide](../../bazel/README.md#build-c-targets-from-an-external-module-caller).

## Structure

- `MODULE.bazel` pins the toolchains and external dependencies.
- Each component directory contains its C++ program and header targets.
- `bazel/generate.py` reads the configured Automake build and emits
  `production_sources.bzl` plus generated C++ and Cargo targets. The module
  loads a copy of that source map from `bazel/production_sources.bzl`.
- `tools/bazel/cc.bzl` applies the common SWSS compiler options, release-specific
  fortification, and the native ASAN and GCOV source selections. The managed
  GCC toolchain supplies optimization and baseline hardening.
- `tools/bazel/deps.bzl` groups dependency labels by component.
- `dist/BUILD.bazel` packages the runtime payload and its matching debug symbols.

Each production binary uses its complete generated source list. Add or remove
production sources in Automake, then regenerate the source map. Shared sources
compile separately for each consuming program, matching the existing
generator. See [the generator documentation](../../bazel/README.md#reuse-the-inventory-from-a-swss-module)
for the preparation command and source-map update.

## Build environment

Use Bazelisk, which reads `.bazelversion` and runs Bazel 8.5.1. The selected
toolchains target Debian Trixie. They execute on the same CPU they target:
the default configuration is Linux x86-64, and `--config=aarch64` requires an
Arm64 build host.

The generated `//swss:countersyncd` target uses the existing locked, offline
Cargo action. The separate `//crates/countersyncd:countersyncd` target uses
`rules_rust`, bindgen, and Clang. `Cargo.Bazel.lock` records the Bazel crate
metadata needed when SWSS is consumed as a dependency; `Cargo.lock` remains
the source dependency lock. To refresh Bazel metadata from that lock, build
with `--repo_env=CARGO_BAZEL_REPIN=1` in the standalone SWSS workspace.

The consuming root supplies and registers a bindgen toolchain; standalone
SWSS uses LLVM 17.0.6 and bindgen 0.71.1. The example registration is in
`tools/bazel/rust/dev/BUILD.bazel`. LLVM selection is development-only because
its module extension requires the root module. Rust 1.90 with the C++ linker
also requires the root build option
`--@rules_rust//rust/settings:experimental_use_allocator_libraries_with_mangled_symbols=True`,
as set in the standalone `.bazelrc`.
An external caller must register the managed GCC toolchain in its root module;
the canonical caller guide includes that registration. The generator guide
describes the prepared environment required by its Cargo action.

## SONiC dependency inputs

The C++ module uses these public interfaces for programs and tests:

| Input | Public label |
| --- | --- |
| Common shared library | `@sonic_swss_common//:libswsscommon_shared` |
| SAI headers for tests | `@sai//:upstream_headers` |
| SAI metadata libraries | `@sonic_sairedis//meta:saimetadata_shared`, `@sonic_sairedis//meta:saimeta_shared` |
| sairedis shared library | `@sonic_sairedis//lib:sairedis_shared` |
| DASH generated headers and shared library | `@sonic_dash_api//:dashapi` |

Common revision `093a849f01722afb4730e685b3eb4f22a9bc9191` generates its
configuration schema from source inputs with YANG enabled. SAI and sairedis
provide their headers and libraries through their own module targets.

DASH retains an input package containing its generated headers and
`libdashapi.so`. Its module selects the native AMD64 or ARM64 package and checks
the recorded SHA-256 of `libdashapi_1.0.0_<architecture>.deb` before exposing
those files. The package is obtained from a pipeline artifact ZIP; the pinned
hash applies to the selected package member. SWSS declares protobuf separately
through `@swss_debian//libprotobuf-dev:libprotobuf`.

The remaining Debian development libraries use the declared `trixie` and
`swss_debian` dependency sets. `MODULE.bazel` and `.bazelrc` select these inputs
for normal C++ builds.

## Build commands

The repository's ordered immutable registries select `sonic-build-infra
0.0.9-c4175cb61c79b3b7b70901724fddbe2cd35ff86d`, `libnl3 3.7.0-sonic.2`, and
the declared Distroless, DASH, Common, SAI, and sairedis modules, followed by
BCR. The canonical caller guide includes the same registry order.
`MODULE.bazel` overrides libnl3's version because historical dotted version
requests from dependencies would otherwise outrank the new release. It also
selects RE2 `2024-07-02.bcr.1`, whose BCR metadata repair marks its obsolete
local C++ extension as a development dependency while retaining the same source
archive. External root callers need both overrides for this selection, as shown
in the canonical caller guide.

Run these commands from the repository root.

Build all 29 C++ programs:

```sh
bazel build --config=release //dist:cpp_binaries
```

Build one C++ program during development:

```sh
bazel build --config=release //orchagent:orchagent
```

Build the Rust program:

```sh
bazel build --config=release //crates/countersyncd:countersyncd
```

Build the runtime and matching detached debug tars:

```sh
bazel build --config=release //dist:swss_pkg //dist:swss_pkg.debug_symbols
```

`//:swss_pkg` is an alias for the same tar target. Use `bazel cquery` with the
same configuration and `--output=files` to print an artifact's output path.

## Build configurations

| Configuration | Effect |
| --- | --- |
| `--config=release` | Selects Bazel `opt` mode and adds SWSS release-specific `-Wdate-time` and `_FORTIFY_SOURCE=3`. The managed GCC toolchain supplies `-O2`, stack and architecture hardening, RELRO, immediate binding, and early `--as-needed`. |
| `--config=debug` | Selects Bazel `dbg` mode and the SWSS debug definitions, and preserves unoptimized debugging with `-O0` and fortification disabled. |
| `--config=asan` | Enables the existing SWSS C++ AddressSanitizer flags and startup sources. |
| `--config=gcov` | Enables SWSS C++ coverage instrumentation and `-O0`. |

The managed GCC optimization and baseline hardening apply in every mode;
SWSS debug targets explicitly override optimization and fortification.
ASAN and GCOV can be combined with `release`; both disable fortification for
their SWSS targets, and GCOV's per-target `-O0` overrides the toolchain's `-O2`.
The shared GCOV preload library is explicitly retained for its startup
constructor even with `--as-needed`. These flags do not instrument Rust,
matching the native build's separate Cargo invocation.

## CI validation

The Bazel workflow builds the normal Trixie release configuration on native
AMD64 and ARM64 runners. It derives explicit program labels from
`bazel/production_sources.bzl`, builds each label and `//dist:cpp_binaries`, and
checks that the aggregate contains exactly one executable per program. It also
checks the ELF architecture, position-independent executable format, RELRO,
and immediate symbol binding. Both architectures also execute
`//gcovpreload:gcovpreload_test` with test-result reuse disabled, proving that
the constructor-only preload library installs its signal handlers at startup.
Each native job configures Automake, verifies the generated source map, and
records that architecture's compiler and linker contract. The configured-rule
comparison checks the 29 Bazel programs' sources, target options, local include
order, and mapped dependency labels. The job also runs the pinned Buildifier
formatting check.

The uploaded inspection bundle contains the executables, build and test logs,
the resolved module graph, the normalized native contract, the configured-rule
capture, the comparison result, and a JSON build receipt binding inputs and
outputs to their checksums and source revision. Toolchain actions, response
files, transitive provider paths, external header precedence, native link
ordering, and runtime equivalence remain outside this check.

Run the same check in a native Debian Trixie environment after configuring
Automake, recording the native contract as described in the
[generator guide](../../bazel/README.md#compare-native-and-bazel-build-settings):

```sh
python3 bazel/ci_production.py build \
  --architecture amd64 \
  --native-contract /absolute/path/to/native-build-contract.json \
  --artifact-directory /absolute/path/to/empty-output-directory
```

Use `--architecture arm64` on a native ARM64 host. Add `--mode clean` to use a
fresh Bazel output base, disable action-result reuse, and run the normal process
sandbox. The workflow supports the same mode through its `clean` input or a
`Bazel-Clean: true` trailer on the PR head commit.

The C++ CodeQL job builds the production programs and selected GCOV preload
sources after analyzer initialization with action caches disabled. Its tracing
configuration excludes only SAI's locked tool preparation action, which starts
no compiler, so CodeQL's preload does not enter that action's strict runtime
dependency check. SAI metadata and SWSS sources compile in separate Bazel
actions with the standard C++ matchers still active. The receipt requires
extraction of all selected tracked sources and records additional and excluded
source paths. It does not claim
historical analyzer parity or runtime coverage.

## Runtime and matching debug packages

`//dist:swss_pkg` is a runtime tar containing 29 C++ programs, `countersyncd`,
two Python helpers, 32 Lua files, and the `netbouncer.json` configuration.
It installs these files under the same runtime paths described by Automake and
`debian/swss.install`. The C++ programs and data come from the same generated
Automake contract used by the compilation targets. The runtime tar carries
`DebugSymbolsInfo`, allowing the consuming OCI image to collect its symbols.

`//dist:swss_pkg.debug_symbols` contains the detached symbols under
`usr/lib/debug/.build-id`. Both tars are split from the same linked ELFs.
The shared packaging rule retains C++ debug information without changing the
selected optimization mode; the Rust targets retain their own source-line
information explicitly. Root aliases exist for both package targets.

Validate the installed paths, data bytes, ownership, permissions, ELF
architecture, build IDs, debug-link checksums, and GDB source-line lookup with:

```sh
python3 bazel/verify_runtime_package.py --architecture amd64 \
  --runtime bazel-bin/dist/swss_pkg_rttar.tar \
  --debug bazel-bin/dist/swss_pkg.debug_symbols.tar
```

Use `bazel cquery --config=release --output=files` for the exact output paths
when the output layout differs.

The tar has no Debian control metadata, maintainer scripts, or dependency
declarations.

The tar contains the SWSS payload, not the shared libraries supplied by its
runtime dependencies. The consuming image must provide the matching libraries.

## Caching

Bazel reuses unchanged local outputs automatically. For persistence across
worktrees or CI jobs on one host, configure a disk cache in the ignored
`.bazelrc.user` file or in the CI environment:

```text
build --disk_cache=/absolute/path/to/bazel-disk-cache
```

A shared CI cache can use an existing service that implements Bazel's remote
cache protocol. Configure its endpoint and authentication in CI, for example:

```text
build --remote_cache=grpcs://cache.example.com
```

Cache service deployment is independent of these SWSS build targets. See
[Bazel remote caching](https://bazel.build/remote/caching) for supported
backends and configuration.

## Package pull request CI

Bazel and CodeQL also run for pull requests targeting the stacked `bazel-bzlmod`
branch. Each native AMD64/ARM64 production job builds both runtime and detached
symbol targets, then checks all 65 installed files and 30 ELF/debug pairs,
including C++ and Rust source lookup with GDB. The package validation is part of
the required production check, so a failed package build or inspection prevents
that check from passing. The production GCOV preload runtime test remains in
the same job; CodeQL keeps its existing selected C++ and Python coverage.

The `sonic-swss-packages-amd64` and `sonic-swss-packages-arm64` artifacts contain
the runtime tar, matching symbols, validation report, resolved module graph,
build log, and a manifest with the tested revision, native architecture, target
platform, and file hashes. These source-owned tars do not establish full daemon
runtime or installed-image behavior.

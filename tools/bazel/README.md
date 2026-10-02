# Building SWSS with Bazel

This repository defines Bazel module targets for the SWSS C++ programs.
Each component BUILD file owns its source lists. CI compares the resolved
Bazel targets directly with independently configured Automake inputs.
The Common dependency is fetched from
[securely1g/sonic-swss-common](https://github.com/securely1g/sonic-swss-common)
at revision `5ee19a9375e667c0d507239927745de8fa29be07`. That source owns its
Bazel targets and generates the configuration schema from source inputs. The
normal SWSS configuration enables Common's YANG C++ sources.

For an external module caller, use the
[canonical caller guide](../../bazel/README.md#build-c-targets-from-an-external-module-caller).

## Structure

- `MODULE.bazel` pins the toolchains and external dependencies.
- Each component directory contains its C++ program and header targets.
- `bazel/generate.py` records configured Automake sources, install paths and
  native compiler/linker settings in a CI JSON artifact. It does not run during
  normal Bazel builds.
- `bazel/production_graph.py` reads the configured Bazel graph for native
  comparison and CodeQL source coverage.
- `tools/bazel/cc.bzl` applies the common SWSS compiler options, release-specific
  fortification, and the native ASAN and GCOV source selections. The managed
  GCC toolchain supplies optimization and baseline hardening.
- `tools/bazel/deps.bzl` groups dependency labels by component.
- `dist/BUILD.bazel` packages the runtime payload and its matching debug symbols.

Maintain production sources in the component BUILD files alongside their
Automake declarations. Small shared lists avoid repeated inputs, such as the
four sources common to cfgmgr programs. Keep each component's explicit local
include order. See the [maintenance guide](../../bazel/README.md#maintain-a-component)
for the workflow and CI comparison.

## Build environment

Use Bazelisk, which reads `.bazelversion` and runs Bazel 8.5.1. The selected
toolchains target Debian Trixie. They execute on the same CPU they target:
the default configuration is Linux x86-64, and `--config=aarch64` requires an
Arm64 build host.

The `//crates/countersyncd:countersyncd` target uses
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
the canonical caller guide includes that registration.

## SONiC dependency inputs

The C++ module uses these public interfaces for programs and tests:

| Input | Public label |
| --- | --- |
| Common shared library | `@sonic_swss_common//:libswsscommon_shared` |
| SAI headers for tests | `@sai//:upstream_headers` |
| SAI metadata libraries | `@sonic_sairedis//meta:saimetadata_shared`, `@sonic_sairedis//meta:saimeta_shared` |
| sairedis shared library | `@sonic_sairedis//lib:sairedis_shared` |
| DASH generated headers and shared library | `@sonic_dash_api//:dashapi` |

Common revision `5ee19a9375e667c0d507239927745de8fa29be07` generates its
configuration schema from source inputs with YANG enabled. SAI and sairedis
provide their headers and libraries through their own module targets.

DASH generates its headers and builds `libdashapi.so` from its pinned source
using the source-built Protobuf 3.21.12 compiler. SWSS and DASH both use the
shared runtime `@protobuf_legacy//:libprotobuf`, built from that same upstream
release; neither consumer links a separate static Protobuf implementation.

The remaining Debian development libraries use the declared `trixie` and
`swss_debian` dependency sets. `MODULE.bazel` and `.bazelrc` select these inputs
for normal C++ builds.

## Build commands

The shared `.bazelrc` uses the SONiC registry `main` branch, followed by BCR,
for CI and local commands.
That single endpoint preserves the selected `sonic-build-infra
0.0.14-f9876051703da05af745ffc781706e29fed7dd4b`, `libnl3 3.7.0-sonic.2`,
`rules_distroless 0.9.4-sonic.1`, and the declared DASH, Common, SAI and sairedis
entries. Module/source versions and hashes remain pinned. The canonical caller
guide uses the same endpoint.
`MODULE.bazel` overrides libnl3 and Distroless versions because higher-sorting
dependency requests would otherwise replace the selected SONiC fixes. Distroless
retains include fragments needed by shared infrastructure APT imports.
Protobuf headers and its runtime are now source-built. The module also
selects RE2 `2024-07-02.bcr.1`, whose BCR metadata repair marks its obsolete
local C++ extension as a development dependency while retaining the same source
archive. External root callers need all three overrides for this selection, as
shown in the canonical caller guide.

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
AMD64 and ARM64 runners. It queries the configured programs under
`//dist:cpp_binaries`, builds each label and the aggregate, and
checks that the aggregate contains exactly one executable per program. It also
checks the ELF architecture, position-independent executable format, RELRO,
and immediate symbol binding. Both architectures also execute
`//gcovpreload:gcovpreload_test` with test-result reuse disabled, proving that
the constructor-only preload library installs its signal handlers at startup.
Each native job configures Automake and records its independent source,
install, compiler and linker contract. The configured-rule
comparison checks the 29 Bazel programs' sources, target options, local include
order, and mapped dependency labels. The job also runs the pinned Buildifier
formatting check.

The uploaded inspection bundle contains the executables, build and test logs,
the resolved module graph, the native contract, the configured production graph
and rule capture, the comparison result, and a JSON build receipt binding inputs and
outputs to their checksums and source revision. Toolchain actions, response
files, transitive provider paths, external header precedence, native link
ordering, and runtime equivalence remain outside this check.

Run the same check in a native Debian Trixie environment after configuring
Automake, recording the native contract as described in the
[native-contract guide](../../bazel/README.md#compare-native-and-bazel-build-settings):

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

The C++ CodeQL job builds the production programs, selected GCOV preload
sources, and the DASH/Protobuf runtime test after analyzer initialization with
action caches disabled. The test programs are compiled without executing them;
packaging remains in native CI. The extraction receipt reads the retained production graph from its own build
receipt and requires both native test source files alongside those production
sources. It verifies the graph hash, checkout revision and build-definition
digest without configuring Automake. Its tracing
configuration excludes the pinned SAI and shared build-tools preparation
actions, which start no C/C++ compiler, so CodeQL's preload does not enter
their strict runtime dependency checks. The exception matches the exact
preparation scripts, output paths, architectures and archive arguments; a Lua
regression check runs before analyzer initialization. SAI metadata and SWSS
sources compile in separate Bazel actions with the standard C++ matchers still
active. The receipt requires
extraction of all selected tracked sources and records additional and excluded
source paths. It does not claim
historical analyzer parity or runtime coverage.

## Runtime and matching debug packages

`//dist:swss_pkg` is a runtime tar containing 29 C++ programs, `countersyncd`,
two Python helpers, 32 Lua files, and the `netbouncer.json` configuration.
It installs these files under the same runtime paths described by Automake and
`debian/swss.install`. The package declares its program and data lists directly
in `dist/BUILD.bazel`, including the three VS Lua names that install Mellanox
implementations. Validation compares those outputs with the independently
configured native build's JSON contract and the Debian install list.
The runtime tar carries
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
  --native-contract /absolute/path/to/native-build-contract.json \
  --runtime bazel-bin/dist/swss_pkg_rttar.tar \
  --debug bazel-bin/dist/swss_pkg.debug_symbols.tar
```

Generate the native contract in the same checkout and native architecture using
the [native build check guide](../../bazel/README.md#compare-native-and-bazel-build-settings).
The verifier rejects a contract for a different revision or architecture. It
compares every installed path, mode, and data file with the native inventory;
changing a BUILD list cannot change the verifier's expected results.

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

## Source Protobuf runtime validation

SWSS and DASH share the source-built Protobuf 3.21.12 runtime from
`protobuf-legacy`, whose consumer target links `libprotobuf.so.32`. The compiler
used by DASH is built from the same upstream release. SWSS's own APT dependency
set does not import Protobuf headers or runtime libraries. The selected Common
ZeroMQ transport uses its binary serializer; the selected SAI Redis libraries do
not enable the optional gRPC/DASH-SAI backend.

Native CI keeps the existing production and hardening checks, and also runs
`//bazel:protobuf_runtime_test` against orchagent's declared dependency set. It
serializes a DASH message and verifies that the loaded Protobuf functions belong
to one shared runtime. The actual orchagent executable is checked with the native
loader's `--list` option using separately staged source-built DASH and Protobuf
runtime archives. The ELF bytes remain unchanged. For this temporary stage,
`--inhibit-rpath ""` and `--library-path` select the staged libraries, and exact
path checks reject fallback to another Protobuf or DASH library. The separate
native test executes DASH serialization against these packaged libraries.
This does not check complete orchagent relocation, default installed-system
library search paths, or Redis/SAI daemon behavior. Existing package and RPATH
checks remain separate.
Both dependencies' matching debug archives, ELF identities, build IDs and debug
checksums are retained with the evidence. SWSS's own installed inventory stays
separate from these dependency payloads.

CI resolves the SONiC registry `main` branch plus BCR. It generates the ignored
`MODULE.bazel.lock` and retains it alongside the resolved module graph.

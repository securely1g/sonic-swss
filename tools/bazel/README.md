# Building and maintaining SWSS with Bazel

Component `BUILD.bazel` files own the C++ sources and dependencies. Normal Bazel
builds use those definitions directly. CI independently compares them with
Automake; there is no source-map generation step before a build.

## Build commands

Run from the repository root with Bazelisk, which selects Bazel 8.5.1 from
[.bazelversion](../../.bazelversion). The managed GCC toolchains target Debian
Trixie and execute on the same CPU they target. Linux x86-64 is the default;
use `--config=aarch64` on a native ARM64 host.

```sh
# Generate ignored Rust metadata and prepare the declared Common dependency.
python3 tools/bazel/prepare_rust.py --receipt artifacts/rust/preparation.json

# Build all 29 production C++ programs.
bazel build --config=release //dist:cpp_binaries

# Build one program while developing.
bazel build --config=release //orchagent:orchagent

# Build all C++ programs on an ARM64 host.
bazel build --config=release --config=aarch64 //dist:cpp_binaries

# Build the Rust program.
bazel build --config=release //crates/countersyncd:countersyncd

# Build the runtime tar and its matching detached debug symbols.
bazel build --config=release //dist:swss_pkg //dist:swss_pkg.debug_symbols

# Print an artifact's output path using the same build configuration.
bazel cquery --config=release //dist:swss_pkg --output=files
```

Building SWSS from another workspace needs root-module toolchain registration
and dependency overrides. Use the [external module guide](../../bazel/external-module.md)
for the complete caller configuration and current pins.

## Make a routine build change

Keep the native Automake declaration and the owning component's `BUILD.bazel`
in sync. [cfgmgr/BUILD.bazel](../../cfgmgr/BUILD.bazel) shows several programs
sharing source and dependency lists; [neighsyncd/BUILD.bazel](../../neighsyncd/BUILD.bazel)
is a smaller example.

### Add or remove a source

Update the program's `srcs` in its component BUILD file and its Automake
`*_SOURCES` declaration. If several programs compile the same files, use a
named list like `CFGMGR_COMMON_SRCS`. Shared sources compile separately with
each program's options. Export a source from its owning package when another
component refers to it, for example `//lib:recorder.cpp`.

Headers must be declared through the component's header targets. Production
programs depend on `:production_headers` and the corresponding targets in other
components. These targets supply file inputs without adding compiler options.
Keep `local_include_directories` in native order, including duplicates; paths
are relative to the SWSS root and `.` means that root. The shared macro also
makes these paths work for external module callers.

### Add a dependency

Add its public label to the program's `deps`. Put labels shared by several
programs in a named list in [deps.bzl](deps.bzl); keep component-specific choices
visible in the owning BUILD file. If the module or Debian package is new,
declare its pinned input in [MODULE.bazel](../../MODULE.bazel) and update the
native dependency declaration as well. The [dependency interfaces](../../bazel/external-module.md#dependency-interfaces)
list the existing Common, SAI, sairedis and DASH entry points.

When the new dependency introduces a native include root or library name,
update its explicit mapping in
[native_build_contract.py](../../bazel/native_build_contract.py). CI rejects
unmapped native inputs and checks that the declared provider is reachable.

### Add a program

Declare a `swss_cc_binary` beside its sources with explicit `srcs`, `deps` and
`local_include_directories`, following another program in that component.
The shared [cc.bzl](cc.bzl) and [flags.bzl](flags.bzl) supply common compiler
settings and conditional ASAN/GCOV startup sources. Set any program-specific
`ndebug`, `asan` or `gcov_preload` behavior to match Automake.

Add the program to Automake's installed program list and to `CPP_BINARIES` in
[dist/BUILD.bazel](../../dist/BUILD.bazel). That list selects both the production
build aggregate and the runtime tar's executables. CI discovers the programs
through `//dist:cpp_binaries` and compares them with Automake's installed list.

### Check the change

Build the affected program, then run the formatter and validation helper tests:

```sh
bazel run //tools/bazel/buildifier:buildifier.check
python3 -m unittest discover -s bazel/tests -v
```

Submit necessary validation and CI updates with the build change. The
[CI guide](../../bazel/README.md) describes the native AMD64/ARM64 build checks,
how to reproduce them, and their limits. The full
[native contract reference](../../bazel/native-build-contract.md) is useful when
changing compiler settings, dependency mappings or the comparison itself.

## Build configurations

| Configuration | Effect |
| --- | --- |
| `--config=release` | Selects Bazel `opt` mode and adds SWSS `-Wdate-time` and `_FORTIFY_SOURCE=3`. The managed GCC toolchain supplies `-O2`, stack and architecture hardening, RELRO, immediate binding, and early `--as-needed`. |
| `--config=debug` | Selects Bazel `dbg` mode and SWSS debug definitions, with `-O0` and fortification disabled. |
| `--config=asan` | Enables the SWSS C++ AddressSanitizer flags and startup sources. |
| `--config=gcov` | Enables SWSS C++ coverage instrumentation and `-O0`. |

The managed toolchain's optimization and baseline hardening apply in every
mode; SWSS debug targets explicitly override optimization and fortification.
ASAN and GCOV can combine with `release`; both disable fortification for SWSS
targets, and GCOV's `-O0` overrides the toolchain's `-O2`. The shared GCOV preload
library retains its startup constructor with `--as-needed`. These flags do not
instrument Rust, matching the native build's separate Cargo invocation. The
normal-release CI contract does not validate DEBUG, ASAN or GCOV settings.

## Rust and runtime/debug packages

`//crates/countersyncd:countersyncd` uses `rules_rust` and the public Rust library
`@sonic_swss_common//crates/swss-common:swss_common`. Common owns the Rust source,
generated bindings and native-library linkage. Its registry module and the Cargo
dependency select the same Common source revision.

SWSS redirects the Cargo `swss-common` dependency to that target. Serde and
`serde_core` also use Common's exported targets so Common's public types work
with SWSS's serializers. `//crates/countersyncd:common_rust_test` checks native
string ownership and a JSON roundtrip across this boundary without Redis; both
native package CI jobs run it.

[Cargo.lock](../../Cargo.lock) is the checked-in source dependency lock.
Run `python3 tools/bazel/prepare_rust.py --receipt artifacts/rust/preparation.json`
before the first Bazel command and after changing Cargo or module inputs.
The small launcher verifies a pinned helper from `sonic-build-infra`. It resolves
Common from this module's declared dependency, prepares a private writable copy
under `.cargo-bazel-prep`, and then generates SWSS's `Cargo.Bazel.lock` using the
upstream `rules_rust` generator. `.bazelrc.rust` applies that Common override to
subsequent commands. These generated files stay outside Git.

Preparation fails if `Cargo.lock` changes or generated package versions, sources
or checksums disagree with it. CI retains both modules' generated metadata and
preparation receipts, including selected package pins and input hashes. Regenerate
from tracked inputs even when restoring Bazel caches. External callers must
prepare their writable Common and SWSS checkouts first, then supply the
[Rust toolchain, overrides and root settings](../../bazel/external-module.md#rust-and-package-callers).


`//dist:swss_pkg` contains 29 C++ programs, `countersyncd`, two Python helpers,
32 Lua files and `netbouncer.json`, at the paths from Automake and
`debian/swss.install`. [dist/BUILD.bazel](../../dist/BUILD.bazel) declares the
program and data lists, including the three VS Lua names that install Mellanox
implementations. Keep these lists in sync when adding installed files.

`//dist:swss_pkg.debug_symbols` contains matching detached symbols under
`usr/lib/debug/.build-id`. Both tars are split from the same linked ELFs. The
shared rule retains C++ debug information without changing optimization mode;
Rust targets retain source-line information explicitly. Root aliases
`//:swss_pkg` and `//:swss_pkg.debug_symbols` select the same outputs. The runtime
target carries `DebugSymbolsInfo` so consuming OCI images can collect symbols.

The tar has no Debian control metadata, maintainer scripts or dependency
declarations. It contains the SWSS payload; the consuming image must provide
matching shared libraries. See the [package validation guide](../../bazel/README.md#runtime-and-debug-package-validation)
for installed-file, debug-symbol and runtime checks and their limits.

## Dependency resolution and caching

[MODULE.bazel](../../MODULE.bazel) pins module/source versions and hashes.
[.bazelrc](../../.bazelrc) uses the draft SONiC registry branch
`codex/common-rust-library`, followed by BCR. That branch adds Common's Rust
library registration; return to `main` when it lands. Common's YANG C++ sources
and generated schema remain enabled.
Keep the generated `MODULE.bazel.lock` out of Git; CI retains it with the
resolved module graph. The [external module guide](../../bazel/external-module.md)
explains the selected versions, required overrides and toolchain settings.

Bazel reuses unchanged local outputs automatically. For reuse across worktrees
or CI jobs on one host, set a disk cache in the ignored `.bazelrc.user` or CI:

```text
build --disk_cache=/absolute/path/to/bazel-disk-cache
```

A shared CI cache can use an existing Bazel remote-cache service. Configure its
endpoint and authentication in CI, for example:

```text
build --remote_cache=grpcs://cache.example.com
```

Cache service deployment is independent of these targets. See
[Bazel remote caching](https://bazel.build/remote/caching) for supported backends
and configuration.

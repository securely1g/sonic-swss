# Building SWSS with Bazel

This repository defines Bazel module targets for the SWSS C++ programs. The
existing generator supplies their production source lists and retains the
locked Cargo action for `countersyncd`.
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
- `bazel/generate.py` reads the configured Automake build and emits
  `production_sources.bzl` plus generated C++ and Cargo targets. The module
  loads a copy of that source map from `bazel/production_sources.bzl`.
- `tools/bazel/cc.bzl` applies the common SWSS compiler options, release-specific
  fortification, and the native ASAN and GCOV source selections. The managed
  GCC toolchain supplies optimization and baseline hardening.
- `tools/bazel/deps.bzl` groups dependency labels by component.
- `dist/BUILD.bazel` contains a separate runtime tar draft.

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
`rules_rust`, bindgen, and Clang and remains a draft migration.
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

The shared `.bazelrc` uses the reviewed SONiC registry branch
`codex/protobuf-312-integration`, followed by BCR, for CI and local commands.
That single endpoint preserves the selected `sonic-build-infra
0.0.14-f9876051703da05af745ffc781706e29fed7dd4b`, `libnl3 3.7.0-sonic.2`,
`rules_distroless 0.9.4-sonic.1`, and the declared DASH, Common, SAI and sairedis
entries. The proposed Protobuf and DASH entries are not yet on registry `main`.
Module/source versions and hashes remain pinned. The canonical caller guide
uses the same endpoint.
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

Build the separate `rules_rust` draft:

```sh
bazel build --config=release //crates/countersyncd:countersyncd
```

Build the separate runtime tar draft:

```sh
bazel build --config=release //dist:swss_pkg
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

## Separate runtime tar draft

`//dist:swss_pkg` is a runtime tar containing 29 C++ programs, `countersyncd`,
two Python helpers, 32 Lua files, and the `netbouncer.json` configuration.
It installs these files under the same runtime paths described by Automake and
`debian/swss.install`.

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


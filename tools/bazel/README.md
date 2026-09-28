# Building SWSS with Bazel

This repository defines Bazel module targets for the SWSS C++ programs. The
existing generator supplies their production source lists and retains the
locked Cargo action for `countersyncd`.
The Common dependency is fetched from
[securely1g/sonic-swss-common](https://github.com/securely1g/sonic-swss-common)
at revision `99572f5a34e7f408dee49eaf2a3ba60c5d443fb6`. That source owns the
Bazel build from
[securely1g/sonic-swss-common PR #1](https://github.com/securely1g/sonic-swss-common/pull/1);
the registry applies only the module-version patch.

For an external module caller, use the
[canonical caller guide](../../bazel/README.md#build-c-targets-from-an-external-module-caller).

## Structure

- `MODULE.bazel` pins the toolchains and external dependencies.
- Each component directory contains its C++ program and header targets.
- `bazel/generate.py` reads the configured Automake build and emits
  `production_sources.bzl` plus generated C++ and Cargo targets. The module
  loads a copy of that source map from `bazel/production_sources.bzl`.
- `tools/bazel/cc.bzl` applies the common SWSS compiler options, normal Trixie
  release hardening, and the native ASAN and GCOV source selections.
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

The current prototype imports the SAI, sairedis, DASH, and generated schema
inputs selected by native SWSS CI. Select it explicitly with both:

```text
--config=ci-debs
--repo_env=SONIC_SWSS_CI_DEBS_MANIFEST=/absolute/path/to/manifest.json
```

The manifest names local DEB files and their SHA-256 values. The repository
rule validates each file before exposing headers and shared libraries to Bazel.
It does not select or download a latest CI artifact. See the
[CI DEB import documentation](../../third_party/ci_debs/README.md) for the
manifest format, package provenance, and labels.

`ci-debs` remains an opt-in provider for module builds. Targets that require these inputs report a
configuration error when no provider is selected. The canonical caller guide
shows the provider and schema selection flags and the flag that includes
Common's existing YANG C++ sources.

## Build commands

These standalone commands select `libnl3 3.7.0-sonic.2` from the merged
`securely1g/sonic-bazel-registry` commit
`238be20f1517f9c859382f0a8d4c19183c968864`. The immutable combined registry
`97dea0f4d4254de4fe17c56a6ceaf17b001cc4ce` remains the fallback for pending
Distroless, build-infra, and Common module releases, followed by BCR.
`MODULE.bazel` overrides libnl3's version because historical dotted version
requests from dependencies would otherwise outrank the new release. External
root callers must repeat that override, as shown in the canonical caller guide.

Run these commands from the repository root. Replace the manifest path with a
local path whose DEBs are visible to the build process.

Build all 29 C++ programs:

```sh
bazel build --config=release --config=ci-debs \
  --repo_env=SONIC_SWSS_CI_DEBS_MANIFEST=/absolute/path/to/manifest.json \
  //dist:cpp_binaries
```

Build one C++ program during development:

```sh
bazel build --config=release --config=ci-debs \
  --repo_env=SONIC_SWSS_CI_DEBS_MANIFEST=/absolute/path/to/manifest.json \
  //orchagent:orchagent
```

Build the separate `rules_rust` draft:

```sh
bazel build --config=release //crates/countersyncd:countersyncd
```

Build the separate runtime tar draft:

```sh
bazel build --config=release --config=ci-debs \
  --repo_env=SONIC_SWSS_CI_DEBS_MANIFEST=/absolute/path/to/manifest.json \
  //dist:swss_pkg
```

`//:swss_pkg` is an alias for the same tar target. Use `bazel cquery` with the
same configuration and `--output=files` to print an artifact's output path.

## Build configurations

| Configuration | Effect |
| --- | --- |
| `--config=release` | Selects Bazel `opt` mode, adds `-O2` for C/C++, and preserves the normal Trixie release hardening flags. The selected GCC toolchain does not currently add optimization for `opt` itself. |
| `--config=debug` | Selects Bazel `dbg` mode and the SWSS debug definitions. |
| `--config=asan` | Enables the existing SWSS C++ AddressSanitizer flags and startup sources. |
| `--config=gcov` | Enables SWSS C++ coverage instrumentation and `-O0`. |

ASAN and GCOV can be combined with `release`; GCOV's per-target `-O0` takes
precedence for SWSS C++ compilation. These flags do not instrument Rust,
matching the native build's separate Cargo invocation.

## CI validation

The Bazel workflow builds the normal Trixie release configuration on native
AMD64 and ARM64 runners. It derives explicit program labels from
`bazel/production_sources.bzl`, builds each label and `//dist:cpp_binaries`, and
checks that the aggregate contains exactly one executable per program. It also
checks the ELF architecture, position-independent executable format, RELRO,
and immediate symbol binding. The uploaded inspection bundle contains the
executables and a JSON receipt with their checksums and source revision.

Run the same check in a native Debian Trixie environment after
[preparing the pinned CI inputs](../../third_party/ci_debs/README.md):

```sh
python3 bazel/ci_production.py build \
  --architecture amd64 \
  --manifest /absolute/path/to/swss-ci-debs/manifest.json \
  --artifact-directory /absolute/path/to/empty-output-directory
```

Use `--architecture arm64` on a native ARM64 host. The generator job compares
the tracked source map with a fresh Automake configuration. The C++ CodeQL job
builds the same explicit targets after analyzer initialization with action
caches disabled, then checks that every production C++ source was extracted
under the repository source root.

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

# Building SWSS with Bazel

This repository defines Bazel module targets for the SWSS C++ programs. The
existing generator supplies their production source lists and retains the
locked Cargo and Debian packaging actions used by sonic-buildimage.
It follows the Bazel 8.5.1 and Bzlmod approach used by
[sonic-swss-common PR #1215](https://github.com/sonic-net/sonic-swss-common/pull/1215).

The existing generator mode remains the default. For the optional module-backed
DEB caller, use the [canonical caller guide](../../bazel/README.md#build-debs-from-an-external-module-caller).

## Structure

- `MODULE.bazel` pins the toolchains and external dependencies.
- Each component directory contains its C++ program and header targets.
- `bazel/generate.py` reads the configured Automake build and emits
  `production_sources.bzl` plus the caller's generated DEB package. The module
  loads a copy of that source map from `bazel/production_sources.bzl`.
- `tools/bazel/cc.bzl` applies the common SWSS compiler options and the native
  ASAN and GCOV source selections.
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

The DEB path builds `countersyncd` through the existing locked, offline Cargo
action. The separate `//crates/countersyncd:countersyncd` target uses
`rules_rust`, bindgen, and Clang and remains a draft migration.
An external caller must register the managed GCC toolchain in its root module;
the canonical caller guide includes that registration and the prepared Cargo
and debhelper environment requirements.

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

`ci-debs` remains an opt-in provider for module builds. It does not change the
generator's default mode. Targets that require these inputs report a
configuration error when no provider is selected. The canonical caller guide
shows the provider and schema selection flags and the flag that includes
Common's existing YANG C++ sources.

## Build commands

These standalone commands use module versions from the companion `bazel-swss`
branch of `securely1g/sonic-bazel-registry`. Make that checkout visible to Bazel and put
`common --registry=file:///registry` first among the registry entries in the
SWSS root `.bazelrc`, adjusting `/registry` to the visible mount path as shown
in the canonical caller guide.

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
| `--config=release` | Selects Bazel `opt` mode and adds `-O2` for C/C++. The selected GCC toolchain does not currently add optimization for `opt` itself. |
| `--config=debug` | Selects Bazel `dbg` mode and the SWSS debug definitions. |
| `--config=asan` | Enables the existing SWSS C++ AddressSanitizer flags and startup sources. |
| `--config=gcov` | Enables SWSS C++ coverage instrumentation and `-O0`. |

ASAN and GCOV can be combined with `release`; GCOV's per-target `-O0` takes
precedence for SWSS C++ compilation. These flags do not instrument Rust,
matching the native build's separate Cargo invocation.

## Debian packaging

The preserved sonic-buildimage contract is `swss` and `swss-dbg` Debian
packages. The [canonical caller guide](../../bazel/README.md#build-debs-from-an-external-module-caller)
contains the module pins, root GCC registration, local registry setup,
generator invocation, and both DEB commands. Its `//swss-module` package
consumes the module's 29 C++ binaries while retaining the existing Cargo and
debhelper actions. The package and manifest stay in the caller's main workspace.

## Separate runtime tar draft

`//dist:swss_pkg` is a runtime tar containing 29 C++ programs, `countersyncd`,
two Python helpers, 32 Lua files, and the `netbouncer.json` configuration.
It installs these files under the same runtime paths described by Automake and
`debian/swss.install`.

The tar has no Debian control metadata, maintainer scripts, or dependency
declarations. The generated DEB action does not consume this target.

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

# SWSS Bazel generator

This directory generates Bazel C++ and Cargo targets from a configured SWSS
tree. It also emits `production_sources.bzl` for the module targets in this
repository. The configured Automake files remain the source of truth for
program selection, source membership, flags, and installed data.

## Generate C++ and Cargo targets

Run the generator inside the same prepared `sonic-slave` environment that will
run Bazel. The source tree must contain the local SWSS changes and the configured
tree must come from that source. An in-source configured tree is supported.
Prepare the locked dependency sources before generation. `cargo vendor` writes
its source replacement configuration to standard output.

```sh
cargo vendor --locked --versioned-dirs /work/target/bazel/cargo-vendor \
  > /work/target/bazel/cargo-vendor.toml
python3 bazel/generate.py \
  --source /work/inputs/sonic-swss \
  --configured-build /work/inputs/sonic-swss \
  --cargo-vendor /work/target/bazel/cargo-vendor \
  --cargo-vendor-config /work/target/bazel/cargo-vendor.toml \
  --output-package /work/target/bazel/workspace/swss
```

The vendor and output directories must be outside the source and configured
trees. The generator owns the output directory, preserves unchanged files, and
removes stale generated inputs. It refuses to replace a nonempty directory
without its ownership marker. Generated build files contain workspace-relative
source paths. For the public `fpmsyncd` inventory, an unset legacy `FPM_PATH`
uses the local `fpmsyncd` header directory. Explicit values are preserved.

The generator emits C++ targets in the generated package. The root workspace
must use Bazel 8.5.1 and declare:

```starlark
bazel_dep(name = "rules_cc", version = "0.1.1")
```

The configured C++ compiler and Bazel's local C++ toolchain must be the same for
the generated package. The external module caller described below uses the
Bazel-managed GCC toolchain and its own `rules_cc` pin.

## Prepared environment requirements

The generated Cargo action runs in the prepared `sonic-slave` environment.
The launcher must include that environment's identity in every action key,
for example with
`--action_env=SONIC_BAZEL_ENVIRONMENT_DIGEST=<digest>`. That identity must cover
the installed compiler, system headers, libraries, and Cargo toolchain.
`SOURCE_DATE_EPOCH` should also be explicit. The generator defaults the Cargo
epoch to the Debian changelog timestamp; the launcher can set it with
`--source-date-epoch`.

## Targets and outputs

The generated package uses these labels:

| Target | Output |
| --- | --- |
| `//swss:<program>` | One configured SWSS C++ program |
| `//swss:countersyncd` | Locked Cargo release binary |

`inventory.json` records the configured program sources, compiler and linker
options, Automake install paths, and Cargo vendor metadata. The C++ rules
compile each translation unit separately, so a source edit rebuilds the
affected objects and programs. A C++ edit can reuse the cached Cargo output.

The Cargo action runs
`cargo build --release --locked --offline --bin countersyncd` using the public
workspace, root `Cargo.lock`, and prepared vendor tree. The generator puts the
vendor files in a deterministic tar input and normalizes the vendor config to a
relative path. The tar avoids treating dependency `BUILD.bazel` files as nested
Bazel packages. Its digest covers every vendored filename, normalized mode, and
byte, and the action also declares the lockfile, config, and local Rust inputs.

Dependency downloads occur only during the explicit `cargo vendor --locked`
preparation step, where Cargo verifies registry checksums and selects the Git
revisions in the lockfile. That step may reuse an explicitly configured
`CARGO_HOME`. The build action uses an empty temporary Cargo home so mutable
global Cargo configuration cannot affect it, and has no dependency downloads.
It is one Bazel action, so a Rust source edit currently rebuilds that Cargo
action. Preserve the slave's `RUSTUP_HOME` and PATH in the action environment so
the public `cargo-auditable` wrapper remains active.

## Reuse the inventory from a SWSS module

Generation also writes `production_sources.bzl`. It records each selected
program's source labels, component directory, and install path, plus the
Automake data inventory. Source labels use the module's top-level component
packages, such as `//cfgmgr:vlanmgrd.cpp` and `//lib:recorder.cpp`.

The module's production binaries consume these complete source lists. This
keeps the existing generator's one-target-per-program layout: a source shared
by several programs is compiled separately for each program. Generate the map
from the normal Linux VS release configuration; the module's C++ macro adds
the ASAN and GCOV startup sources when those configurations are selected.

After running the generator, refresh the copy loaded by the module:

```sh
cp /work/target/bazel/workspace/swss/production_sources.bzl \
  /work/inputs/sonic-swss/bazel/production_sources.bzl
```

Source membership changes belong in Automake and are then regenerated here.

Check the tracked source map against a configured tree without generating a
package or preparing Cargo inputs:

```sh
python3 bazel/generate.py \
  --source /work/inputs/sonic-swss \
  --configured-build /work/inputs/sonic-swss \
  --check-production-sources bazel/production_sources.bzl
```

The check exits with an error when the generated map differs. It reads the same
configured Automake production inputs as full generation and leaves the tracked
file unchanged.

## Build C++ targets from an external module caller

This caller builds the SWSS module's C++ targets with the managed GCC toolchain.
The example uses Bazel 8.5.1 and paths visible inside the build environment;
adjust those paths to match your mounts.

### Caller module

Create the caller's `MODULE.bazel` with the current iteration pins:

```starlark
module(name = "sonic_swss_caller")

bazel_dep(name = "rules_cc", version = "0.2.16")
bazel_dep(name = "sonic-swss", version = "0.0.0", repo_name = "sonic_swss")
bazel_dep(
    name = "sonic-swss-common",
    version = "0.0.0-99572f5a34e7f408dee49eaf2a3ba60c5d443fb6",
    repo_name = "sonic_swss_common",
)
bazel_dep(
    name = "sonic-build-infra",
    version = "0.0.4-83b4e9d963f7f268d06983a8c954fc5d6d93ce2b.sonic.1",
    repo_name = "sonic_build_infra",
)

local_path_override(module_name = "sonic-swss", path = "/inputs/sonic-swss")

# Root overrides in imported modules do not propagate to this caller.
single_version_override(
    module_name = "libnl3",
    version = "3.7.0-sonic.2",
)

gcc = use_extension("@sonic_build_infra//toolchains/gcc:extensions.bzl", "gcc")
use_repo(gcc, "gcc_toolchains")
register_toolchains("@gcc_toolchains//:all")

ci_debs = use_extension(
    "@sonic_swss//third_party/ci_debs:extensions.bzl",
    "ci_debs",
)
use_repo(ci_debs, "sonic_ci_debs")
```

Register GCC in the root caller so the managed toolchain takes priority over
`local_config_cc`. The local SWSS override points to the source tree containing
the generated source map. Declaring `sonic_ci_debs` makes the repository available; the
configuration and manifest below select and supply its inputs.

### Caller Bazel configuration

Put `8.5.1` in the caller's `.bazelversion` and add these settings to its
`.bazelrc`:

```text
common --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/238be20f1517f9c859382f0a8d4c19183c968864
common --registry=file:///registry
common --registry=https://bcr.bazel.build/
common --lockfile_mode=off
common --noincompatible_disallow_empty_glob
common --platforms=@sonic_build_infra//platforms:x86_64_trixie

common:ci-debs --@sonic_swss//tools/bazel:ci_debs=True
common:ci-debs --@sonic_swss_common//tools/bazel:cfg_schema=@sonic_ci_debs//:cfg_schema
common:ci-debs --@sonic_swss_common//tools/bazel:yang_modules=True

build:release --compilation_mode=opt
build:release --copt=-O2
```

The first registry selects the merged `libnl3 3.7.0-sonic.2` release.
`/registry` is an example mount path for the combined
`securely1g/sonic-bazel-registry` checkout at commit
`97dea0f4d4254de4fe17c56a6ceaf17b001cc4ce`, retained by branch
`archive/pr-1-before-module-split-20260928`. Mount that checkout at `/registry`
for the pending Distroless, build-infra, and Common module releases. Use its
visible absolute path in the `file://` URL when your mount differs, or replace
that URL with the immutable raw GitHub registry URL from the SWSS `.bazelrc`.
BCR remains the final fallback. The root libnl3 override prevents historical
dotted versions requested by dependencies from winning version resolution.
Run `bazel mod graph` and confirm `libnl3@3.7.0-sonic.2` is selected.
The caller disables lockfile use for this local registry iteration. The pinned
Common entry fetches revision `99572f5a34e7f408dee49eaf2a3ba60c5d443fb6` from
`securely1g/sonic-swss-common`. Its source already owns the Bazel build; the
registry applies only the module-version patch.

The three `ci-debs` flags select the SWSS dependency provider, the imported
schema, and Common's existing YANG C++ sources. The explicit
`SONIC_SWSS_CI_DEBS_MANIFEST` below names the local package files and their hashes;
see the [CI DEB import guide](../third_party/ci_debs/README.md).

### Build the C++ programs

Run Bazel from the caller workspace, with the dependency manifest and its DEBs
visible to the build process:

```sh
bazel build --config=release --config=ci-debs \
  --repo_env=SONIC_SWSS_CI_DEBS_MANIFEST=/absolute/path/to/manifest.json \
  @sonic_swss//dist:cpp_binaries
```

This target selects all 29 C++ programs. For a single program, use a label such
as `@sonic_swss//orchagent:orchagent` with the same configuration.

The source map is generated from the normal Linux VS release configuration.
The generator rejects GCOV inputs, installed libraries, custom Automake install
hooks, generated `BUILT_SOURCES`, and local link dependencies until those inputs
have explicit Bazel rules.

## Validation

Run the focused generator tests with:

```sh
python3 -m unittest discover -s bazel/tests -v
```

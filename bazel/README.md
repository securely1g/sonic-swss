# SWSS Bazel package

This directory provides the SWSS package used by the opt-in Bazel build in
`sonic-buildimage`. The buildimage launcher prepares the normal public SONiC
build environment, configures SWSS, generates this package, and owns the
workspace-level Bazel configuration and image graph.

## Generate the package with default C++ targets

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
  --deb-build-options "$DEB_BUILD_OPTIONS" \
  --output-package /work/target/bazel/workspace/swss
```

The vendor and output directories must be outside the source and configured
trees. The generator owns the output directory, preserves unchanged files, and
removes stale generated inputs. It refuses to replace a nonempty directory
without its ownership marker. Generated build files contain workspace-relative
source paths. For the public `fpmsyncd` inventory, an unset legacy `FPM_PATH`
uses the local `fpmsyncd` header directory. Explicit values are preserved.

The default generator mode emits the C++ targets in the generated package. In
this mode, the root workspace must use Bazel 8.5.1 and declare:

```starlark
bazel_dep(name = "rules_cc", version = "0.1.1")
```

The configured C++ compiler and Bazel's local C++ toolchain must be the same in
default generator mode. The external module caller described below uses the
Bazel-managed GCC toolchain and its own `rules_cc` pin.

## Prepared environment requirements

Both modes run the Cargo and Debian actions in the prepared `sonic-slave`
environment. Use `build --strip=never` so debhelper receives the DWARF
information needed by `swss-dbg`.
The launcher must include the prepared environment identity in every action
key, for example with
`--action_env=SONIC_BAZEL_ENVIRONMENT_DIGEST=<digest>`. That identity must cover
the installed compiler, system headers, libraries, Cargo toolchain, and Debian
packaging tools. `SOURCE_DATE_EPOCH` should also be explicit. The generator
defaults the package epoch to the Debian changelog timestamp; the launcher can
set it with `--source-date-epoch`. `--architecture` defaults to
`dpkg-architecture -qDEB_HOST_ARCH`.
The launcher passes the evaluated release `DEB_BUILD_OPTIONS` with
`--deb-build-options`; direct generator use defaults to an empty value. The
generator records the exact value in `inventory.json`, and the package action
uses it for Debian packaging. The launcher validates supported release options.

## Targets and outputs

The default generated package uses these labels:

| Target | Output |
| --- | --- |
| `//swss:<program>` | One configured SWSS C++ program |
| `//swss:countersyncd` | Locked Cargo release binary |
| `//swss:swss_deb` | Versioned `swss` Debian package |
| `//swss:swss_dbg_deb` | Matching `swss-dbg` Debian package |
| `//swss:swss_package_manifest` | Package metadata, file inventory, and SHA-256 digests |

`inventory.json` records the configured program sources, compiler and linker
options, installed files, package names, architecture, and version. The C++
rules compile each translation unit separately, so a source edit rebuilds the
affected objects and programs. Package inputs contain the resulting binaries,
Debian metadata, and runtime data; they do not include unrelated C++ source
files. A C++ edit therefore reuses the cached Cargo output.

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

Use the source map and packaging inventory from the same generation run.
Source membership changes belong in Automake and are then regenerated here.

The default mode emits the C++ targets in the generated package. A caller that
already declares those binaries in a SWSS module can add:

```text
--binary-label-prefix @sonic_swss
```

This mode derives labels such as `@sonic_swss//cfgmgr:vlanmgrd` from the same
configured program inventory and passes them to the existing Debian rule. It
continues to generate the locked Cargo target, both DEB targets, and the package
manifest. The caller supplies the module repository and its dependency
configuration. Keep the generated package in the caller's main workspace;
its source bundle paths are workspace-relative. An empty prefix selects
component labels in the current repository.

## Build DEBs from an external module caller

This caller uses the SWSS module for the C++ binaries and keeps the generator's
locked Cargo action and Debian packaging. The generated package remains in the
caller's main workspace. The example uses Bazel 8.5.1 and paths visible inside
the prepared build environment; adjust those paths to match your mounts.

### Caller module

Create the caller's `MODULE.bazel` with the current iteration pins:

```starlark
module(name = "sonic_swss_generated")

bazel_dep(name = "rules_cc", version = "0.2.16")
bazel_dep(name = "sonic-swss", version = "0.0.0", repo_name = "sonic_swss")
bazel_dep(
    name = "sonic-swss-common",
    version = "0.0.0-10d14ae58ae73899a52a2447d1e791a2b7bd1a34",
    repo_name = "sonic_swss_common",
)
bazel_dep(
    name = "sonic-build-infra",
    version = "0.0.4-83b4e9d963f7f268d06983a8c954fc5d6d93ce2b.sonic.1",
    repo_name = "sonic_build_infra",
)

local_path_override(module_name = "sonic-swss", path = "/inputs/sonic-swss")

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
`local_config_cc`. The local SWSS override points to the source tree used for
generation. Declaring `sonic_ci_debs` makes the repository available; the
configuration and manifest below select and supply its inputs.

### Caller Bazel configuration

Put `8.5.1` in the caller's `.bazelversion` and add these settings to its
`.bazelrc`:

```text
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
build --strip=never
build --action_env=SONIC_BAZEL_ENVIRONMENT_DIGEST
build --action_env=SOURCE_DATE_EPOCH
build --action_env=PATH
build --action_env=RUSTUP_HOME
```

`/registry` is an example mount path for the companion
`securely1g/sonic-bazel-registry` checkout. Use branch `bazel-swss` at commit
`8ed1f2851f5b12c69d0ae276aa3871e28e02199e`, then mount that checkout at
`/registry` in the build environment. Use its visible absolute path in the
`file://` URL when your mount differs. BCR remains the fallback registry.
The caller disables lockfile use for this local registry iteration.

Set `SONIC_BAZEL_ENVIRONMENT_DIGEST` and `SOURCE_DATE_EPOCH` in the caller's
environment before running Bazel, using the requirements above. Preserve the
prepared environment's `PATH` and `RUSTUP_HOME`. The digest still covers the
installed inputs used by Cargo and Debian packaging, including library and
package metadata used by `dpkg-shlibdeps`.

The three `ci-debs` flags select the SWSS dependency provider, the imported
schema, and Common's existing YANG C++ sources. The explicit
`SONIC_SWSS_CI_DEBS_MANIFEST` below names the local package files and their hashes;
see the [CI DEB import guide](../third_party/ci_debs/README.md).

### Generate and build the DEBs

From `/inputs/sonic-swss`, use the configured Linux VS release source tree and
the prepared Cargo vendor directory described above:

```sh
python3 bazel/generate.py \
  --source /inputs/sonic-swss \
  --configured-build /inputs/sonic-swss \
  --cargo-vendor /work/target/bazel/cargo-vendor \
  --cargo-vendor-config /work/target/bazel/cargo-vendor.toml \
  --deb-build-options "$DEB_BUILD_OPTIONS" \
  --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --output-package /work/target/bazel/workspace/swss-module \
  --workspace-package swss-module \
  --binary-label-prefix @sonic_swss
cp /work/target/bazel/workspace/swss-module/production_sources.bzl \
  /inputs/sonic-swss/bazel/production_sources.bzl
```

Run Bazel from `/work/target/bazel/workspace`, with a manifest and DEBs visible
to the build process:

```sh
bazel build --config=release --config=ci-debs \
  --repo_env=SONIC_SWSS_CI_DEBS_MANIFEST=/absolute/path/to/manifest.json \
  //swss-module:swss_deb \
  //swss-module:swss_dbg_deb
```

The same action also writes `swss-package-manifest.json`, exposed as
`//swss-module:swss_package_manifest`. It retains the prepared Cargo and
debhelper environment described above; importing C++ headers and libraries
does not install packages into that environment.

## Debian packaging

The package action copies Bazel-built native executables and configured
Automake data into a validated staging tree. It puts `countersyncd` at the source
path expected by `debian/swss.install`, then runs:

```sh
dpkg-buildpackage -b -uc -us -nc
```

`SONIC_BAZEL_STAGEDIR` selects the packaging-only branches in `debian/rules`.
Those branches validate the staged files, skip the already completed native
build, and install the staged Automake output. The existing debhelper binary
sequence still installs `debian/swss.install`, computes shared-library
dependencies, strips binaries into `swss-dbg`, generates control metadata, and
builds both `.deb` files. Normal builds without `SONIC_BAZEL_STAGEDIR` retain the
existing Autotools and Cargo flow.

The action checks package name, version, architecture, root ownership, required
paths and modes, ELF payloads, and data contents. It requires a nonempty computed
`Depends` field for `swss`, a dependency on the matching `swss` version in
`swss-dbg`, and the presence of DWARF debug information before exposing the
outputs. The Cargo and Debian actions execute locally in the prepared slave and
can reuse Bazel cached outputs.

The first supported image configuration is the normal Linux VS release build.
The generator rejects GCOV packaging, installed libraries, custom Automake
install hooks, generated `BUILT_SOURCES`, local link dependencies, and complex
`dh_install` expressions until those inputs have explicit Bazel rules. This
prevents a new upstream build shape from silently omitting package content.

## Validation

Run the focused generator and staging tests with:

```sh
python3 -m unittest discover -s bazel/tests -v
```

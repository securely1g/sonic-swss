# SWSS Bazel package

This directory provides the SWSS package used by the opt-in Bazel build in
`sonic-buildimage`. The buildimage launcher prepares the normal public SONiC
build environment, configures SWSS, generates this package, and owns the
workspace-level Bazel configuration and image graph.

## Generate the package

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

The root workspace must use Bazel 8.5.1 and declare:

```starlark
bazel_dep(name = "rules_cc", version = "0.1.1")
```

The configured C++ compiler and Bazel's local C++ toolchain must be the same.
Use `build --strip=never` so debhelper receives the DWARF information needed by
`swss-dbg`.
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
paths and modes, ELF payloads, data contents, runtime dependencies, and debug
symbols before exposing the outputs. The Cargo and Debian actions execute
locally in the prepared slave and can reuse Bazel cached outputs.

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

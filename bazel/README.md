# SWSS Bazel generator

This directory generates Bazel C++ and Cargo targets from a configured SWSS
tree. It also emits `production_sources.bzl` for the module targets in this
repository. The configured Automake files remain the source of truth for
program selection, source membership, flags, and installed data.
The module also provides standalone Rust and runtime/debug package targets;
see [their build and validation instructions](../tools/bazel/README.md).
The normal C++ validation below does not assess Cargo, Rust, or runtime completion.

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
program's source labels, component directory, install path, and ordered local
include directories, plus the Automake data inventory. Source labels use the
module's top-level component packages, such as `//cfgmgr:vlanmgrd.cpp` and
`//lib:recorder.cpp`. `local_include_directories` comes from the program's
configured compile profile. It preserves the native `-I` order and duplicates;
`.` denotes the SWSS root. A different local include class requires an explicit
mapping before generation can continue.

The module's production binaries consume these complete source lists and local
include lists. Their `production_headers` dependencies declare header inputs
without adding include directories; the original header targets remain
available to tests and callers. This keeps the existing generator's
one-target-per-program layout: a source shared by several programs is compiled
separately for each program. Generate the map from the normal Linux VS release
configuration; the module's C++ macro adds the ASAN and GCOV startup sources
when those configurations are selected.

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

### Compare native and Bazel build settings

The source and install map, including local include order, is shared by the
native AMD64 and ARM64 configurations. Each native runner writes its own
normalized compiler and linker profiles because target hardening and other
configured options can differ. Add these arguments to the source check:

```sh
python3 bazel/generate.py \
  --source /work/inputs/sonic-swss \
  --configured-build /work/inputs/sonic-swss \
  --check-production-sources bazel/production_sources.bzl \
  --output-build-contract native-build-contract.json \
  --git-revision "$(git rev-parse HEAD)"
```

The JSON file is written only after the source map matches. It contains schema
version 1, the caller-supplied Git revision, the SHA-256 of the checked map, the
runner's machine name, and the configured Automake `host_cpu` values. The
generator records the revision argument; the comparison checks it against the
build receipt and current checkout. An AMD64 contract cannot validate ARM64.

`compile_profiles` and `link_profiles` contain ordered argument records with an
`argument` and `classification`. Identical argument lists share a profile named
after the first program using it. Each `programs` entry refers to one compile
profile and one link profile. This retains option order and duplicate native
library arguments while keeping repeated program settings in one place.

The normal-release comparison uses these classifications:

| Classification | Bazel comparison |
| --- | --- |
| `literal` | Compare the resolved target `cxxopts` or `linkopts`. An unfamiliar non-path flag remains literal. |
| `include` | Require exact local `-I` order and duplicates, or a reachable mapped external dependency label. |
| `source_prefix_map` | Record the native mapping with source and build roots replaced by `<swss>`; its toolchain-action equivalent is outside this check. |
| `toolchain` | Accept the mapped option under the exact reviewed infra version; toolchain actions are not inspected. |
| `target_toolchain` | Accept the mapped option only for the named native CPU and reviewed infra version. |
| `replacement` | Apply only the explicit native-to-Bazel replacement in `compile_replacements`. |
| `native_only` | Omit only the documented native option from target `cxxopts`. |
| `native_link_driver` | Omit the listed compile options repeated by Automake on an ordinary non-LTO link command from target `linkopts`. |
| `target_native_link_driver` | Apply the same omission only for the CPU named in `target_toolchain_compile_options`. |
| `library` | Require the mapped dependency label to be reachable, or record the named toolchain library under the reviewed infra boundary. |

The known include roots are explicit: `<swss>` names local component paths,
`<swsscommon-headers>` names Common, `<libnl3-headers>` names libnl3, and
`<sai-headers>` maps to the public sairedis header dependencies.
`<system-headers>` records the managed toolchain and named dependency headers
that replace native `/usr/include`. The optional contract output rejects an
include or library path without a mapping and a native library without a
provider. Full C++ and Cargo package generation keeps its existing configured
path behavior.

The current intentional differences are named in the contract and comparison
result. The selected production sources do not include `config.h`, so the
mapping omits `-DHAVE_CONFIG_H`; review that omission when source membership
changes. The target replaces native fortification level 2 with level 3 and uses
the listed `-Wformat=2` and `-Werror` replacements. It also adds
`-Wno-missing-include-dirs`. Repeated macro, warning, language-standard, debug,
and PIC controls compare by their final setting; other target compile literals
and all target link literals retain order. Local include order is checked
separately. `reviewed_toolchain_additional_link_options` records the intended
immediate-binding and `--as-needed` additions under the reviewed infra version;
this configured-rule check does not mechanically verify those options.

CI captures the root workspace's resolved production `cc_binary` rules with
the build's release, architecture, and dependency settings. The bounded query
uses the 29 generated program labels as its universe and follows paths only to
the explicitly mapped provider, local header, and rule-support labels. It uses
`--noimplicit_deps --notool_deps --consistent_labels --output=jsonproto`.
`configured-rules.json` wraps that JSON with schema version 1, the query string,
the ordered universe, and the root repository mapping from
`bazel mod dump_repo_mapping ''`. The build receipt records the native contract,
configured rules, and module graph paths and hashes.

The helper invokes the comparison after writing its build receipt. It can also
be run directly with those retained artifacts:

```sh
python3 bazel/compare_build_contract.py \
  --architecture amd64 \
  --native-contract artifacts/production/native-build-contract.json \
  --configured-rules artifacts/production/configured-rules.json \
  --build-receipt artifacts/production/manifest.json \
  --output artifacts/production/native-contract-check.json
```

The comparison requires the generated source set, resolved target `cxxopts`
and `linkopts`, local include order, and mapped dependency-label reachability
to match. It rejects an unclassified target option or direct dependency and
requires production header targets to add inputs without propagated options.
It verifies the checkout revision, source-map hash, native architecture, and
the exact reviewed `sonic-build-infra` version. The separate result records all
input hashes, the checked programs, and named intentional differences.

This evidence does not inspect toolchain actions, response files, transitive
provider header or library paths, or external header search precedence. It
collapses repeated dependency aliases only when their resolved `actual` label
agrees; configuration-specific provider behavior remains outside this check.
It does not establish native link ordering or runtime parity. DEBUG, ASAN,
GCOV, Rust, Cargo, and runtime targets remain separate validation scope; this
check does not assess their completion.

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
    version = "0.0.0-093a849f01722afb4730e685b3eb4f22a9bc9191",
    repo_name = "sonic_swss_common",
)
bazel_dep(
    name = "sonic-build-infra",
    version = "0.0.9-c4175cb61c79b3b7b70901724fddbe2cd35ff86d",
    repo_name = "sonic_build_infra",
)

local_path_override(module_name = "sonic-swss", path = "/inputs/sonic-swss")

# Root overrides in imported modules do not propagate to this caller.
single_version_override(
    module_name = "libnl3",
    version = "3.7.0-sonic.2",
)
single_version_override(
    module_name = "re2",
    version = "2024-07-02.bcr.1",
)

gcc = use_extension("@sonic_build_infra//toolchains/gcc:extensions.bzl", "gcc")
use_repo(gcc, "gcc_toolchains")
register_toolchains("@gcc_toolchains//:all")
```

Register GCC in the root caller so the managed toolchain takes priority over
`local_config_cc`. The local SWSS override points to the source tree containing
the generated source map. Declare Common directly so the caller can enable its
YANG C++ sources. Common revision `093a849f01722afb4730e685b3eb4f22a9bc9191`
generates the configuration schema from its source inputs. The RE2 override
selects the BCR metadata repair for its obsolete local C++ extension and retains
the same upstream source archive.

### Caller Bazel configuration

Put `8.5.1` in the caller's `.bazelversion` and add these settings to its
`.bazelrc`:

```text
common --check_direct_dependencies=off
common --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/2f4012b01f7a73f24b12de64a0a9ae86a06b0e89
common --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/97dea0f4d4254de4fe17c56a6ceaf17b001cc4ce
common --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/c999f3c9ebf59c5e2d8feaae8d5010fc9204ef92
common --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/473dda85dc87430fddfdf12ab55a05da21748069
common --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/482c2b1699423ab19c700f0e0ea8e2adb5915e27
common --registry=https://bcr.bazel.build/
common --lockfile_mode=off
common --noincompatible_disallow_empty_glob
common --platforms=@sonic_build_infra//platforms:x86_64_trixie
common --@sonic_swss_common//tools/bazel:yang_modules=True

build:release --compilation_mode=opt
```

Keep these immutable registries in the same order as the SWSS `.bazelrc`, with
BCR as the final fallback. Together they provide the selected build-infra,
libnl3, Distroless, DASH, Common, SAI, and sairedis entries. The root libnl3
override prevents historical dotted versions requested by dependencies from
winning version resolution.
Run `bazel mod graph` and confirm `libnl3@3.7.0-sonic.2` and
`sonic-build-infra@0.0.9-c4175cb61c79b3b7b70901724fddbe2cd35ff86d` are selected.
The managed GCC toolchain supplies `-O2`, stack and architecture hardening,
RELRO, immediate binding, and early `--as-needed`. SWSS adds its release-specific
`-Wdate-time` and `_FORTIFY_SOURCE=3` override.
The caller disables lockfile use for this registry iteration. The Common flag
enables its YANG C++ sources; this Common version generates the configuration
schema from source inputs.

### Build the C++ programs

Run Bazel from the caller workspace:

```sh
bazel build --config=release @sonic_swss//dist:cpp_binaries
```

This target selects all 29 C++ programs. Build a single program with:

```sh
bazel build --config=release @sonic_swss//orchagent:orchagent
```

The source map is generated from the normal Linux VS release configuration.
The generator rejects GCOV inputs, installed libraries, custom Automake install
hooks, generated `BUILT_SOURCES`, and local link dependencies until those inputs
have explicit Bazel rules.

## Validation

Run the focused generator tests with:

```sh
python3 -m unittest discover -s bazel/tests -v
```

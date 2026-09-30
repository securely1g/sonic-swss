# SWSS source map and native build checks

`generate.py` keeps the checked-in `bazel/production_sources.bzl` synchronized
with configured Automake inputs and records native build settings for CI.
Normal `bazel build` commands read the checked-in map and component BUILD files;
they do not run this script or require an Automake configuration step.

The module also provides standalone Rust and runtime/debug package targets;
see [their build and validation instructions](../tools/bazel/README.md).
The native C++ comparison below does not assess Rust or runtime completion.

## Configure the native source inventory

Run the commands below from the SWSS repository root in a native Linux build
environment with the Automake configuration dependencies installed. The
[Bazel workflow](../.github/workflows/bazel.yml) lists those host dependencies.
Use the normal Linux VS release configuration, without ASAN or GCOV:

```sh
./autogen.sh
debian/rules override_dh_auto_configure
```

This runs the native configuration step. It does not compile programs or create
a Debian package. The source and configured trees must come from the same
checkout; an in-source configured tree is supported.

## Update or check the source map

After changing production source membership in Automake, configure the tree and
update the checked-in map:

```sh
python3 bazel/generate.py \
  --source "$PWD" \
  --configured-build "$PWD" \
  --update-production-sources bazel/production_sources.bzl
```

Review and commit the source-map diff with the Automake change. The map records
each selected program's source labels, component directory, install path, and
ordered local include directories, plus the Automake data inventory. Labels
use the module's component packages, such as `//cfgmgr:vlanmgrd.cpp` and
`//lib:recorder.cpp`. Local include directories preserve the native `-I` order
and duplicates; `.` denotes the SWSS root. An unset legacy `FPM_PATH` uses the
local `fpmsyncd` header directory; explicit values are preserved.

Each production binary consumes its complete source list. Shared sources
compile separately for each consuming program. `production_headers` targets
declare header inputs without adding include directories. The C++ macro adds
the ASAN and GCOV startup sources when those configurations are selected.

CI checks the map without changing it:

```sh
python3 bazel/generate.py \
  --source "$PWD" \
  --configured-build "$PWD" \
  --check-production-sources bazel/production_sources.bzl
```

The check fails if the configured inventory differs from the tracked file.
Neither mode prepares Cargo inputs or generates BUILD files.

## Compare native and Bazel build settings

The source and install map, including local include order, is shared by the
native AMD64 and ARM64 configurations. Each native runner writes its own
normalized compiler and linker profiles because target hardening and other
configured options can differ. Add these arguments to the source check:

```sh
python3 bazel/generate.py \
  --source "$PWD" \
  --configured-build "$PWD" \
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
that replace native `/usr/include`. The contract output rejects an include or
library path without a mapping and a native library without a provider.

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
    version = "0.0.0-5ee19a9375e667c0d507239927745de8fa29be07",
    repo_name = "sonic_swss_common",
)
bazel_dep(
    name = "sonic-build-infra",
    version = "0.0.14-f9876051703da05af745ffc781706e29fed7dd4b",
    repo_name = "sonic_build_infra",
)

local_path_override(module_name = "sonic-swss", path = "/inputs/sonic-swss")

# Root overrides in imported modules do not propagate to this caller.
single_version_override(
    module_name = "libnl3",
    version = "3.7.0-sonic.2",
)
single_version_override(
    module_name = "rules_distroless",
    version = "0.9.4-sonic.1",
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
YANG C++ sources. Common revision `5ee19a9375e667c0d507239927745de8fa29be07`
generates the configuration schema from its source inputs. The RE2 override
selects the BCR metadata repair for its obsolete local C++ extension and retains
the same upstream source archive.

### Caller Bazel configuration

Put `8.5.1` in the caller's `.bazelversion` and add these settings to its
`.bazelrc`:

```text
common --check_direct_dependencies=off
common --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/main
common --registry=https://bcr.bazel.build/
common --lockfile_mode=update
common --noincompatible_disallow_empty_glob
common --platforms=@sonic_build_infra//platforms:x86_64_trixie
common --@sonic_swss_common//tools/bazel:yang_modules=True

build:release --compilation_mode=opt
```

Use the same single SONiC registry branch as SWSS `.bazelrc`, followed by BCR.
The maintained `main` branch contains the exact selected build-infra, libnl3,
Distroless, DASH, Common, SAI, sairedis and Protobuf registrations. The DASH
entry landed through
[registry #19](https://github.com/securely1g/sonic-bazel-registry/pull/19).
DASH version `0.0.4-2a6e390b96a4fc17c191fa0da4b7ed1f40aed069` selects the
landed source commit on its maintained `master` branch. CI and local commands
share this endpoint. Module versions, source archives/checksums, overlays and
toolchain inputs remain pinned. The root libnl3 and Distroless overrides prevent
higher-sorting dependency requests from replacing the selected SONiC fixes.
Distroless `0.9.4-sonic.1` retains include fragments for shared infrastructure
APT imports; Protobuf headers and its runtime are now source-built.
Run `bazel mod graph` and confirm `libnl3@3.7.0-sonic.2`,
`rules_distroless@0.9.4-sonic.1`, `protobuf-legacy@3.21.12-sonic.1`, and
`sonic-build-infra@0.0.14-f9876051703da05af745ffc781706e29fed7dd4b` are selected,
alongside `sonic-dash-api@0.0.4-2a6e390b96a4fc17c191fa0da4b7ed1f40aed069`.
The managed GCC toolchain supplies `-O2`, stack and architecture hardening,
RELRO, immediate binding, and early `--as-needed`. SWSS adds its release-specific
`-Wdate-time` and `_FORTIFY_SOURCE=3` override. Infrastructure source
[`f987605`](https://github.com/securely1g/sonic-build-infra/commit/f9876051703da05af745ffc781706e29fed7dd4b)
also supplies `-Wl,-rpath-link=/lib/<multiarch>` for link-time dependency lookup;
this does not embed an ELF runtime search path. The reviewed contract records
this architecture-specific setting. The unused opt-in `as_needed` feature alias
was removed upstream; the default `--as-needed` argument remains enabled.
Keep the generated `MODULE.bazel.lock` out of Git and retain it with CI
resolution artifacts. The Common flag
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

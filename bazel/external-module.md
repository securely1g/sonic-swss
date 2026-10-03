# Build SWSS from an external module

For builds within the SWSS checkout, use the [maintainer guide](../tools/bazel/README.md).

This caller builds the SWSS module's C++ targets with the managed GCC toolchain.
The example uses Bazel 8.5.1 and paths visible inside the build environment;
adjust those paths to match your mounts.

## Caller module

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
the component BUILD files. Declare Common directly so the caller can enable its
YANG C++ sources. Common revision `5ee19a9375e667c0d507239927745de8fa29be07`
generates the configuration schema from its source inputs. The RE2 override
selects the BCR metadata repair for its obsolete local C++ extension and retains
the same upstream source archive.

## Caller Bazel configuration

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

## Build the C++ programs

Run Bazel from the caller workspace:

```sh
bazel build --config=release @sonic_swss//dist:cpp_binaries
```

This target selects all 29 C++ programs. Build a single program with:

```sh
bazel build --config=release @sonic_swss//orchagent:orchagent
```

## Dependency interfaces

The C++ module uses these public interfaces for programs and tests:

| Input | Public label |
| --- | --- |
| Common shared library | `@sonic_swss_common//:libswsscommon_shared` |
| SAI metadata libraries | `@sonic_sairedis//meta:saimetadata_shared`, `@sonic_sairedis//meta:saimeta_shared` |
| sairedis shared library | `@sonic_sairedis//lib:sairedis_shared` |
| DASH generated headers and shared library | `@sonic_dash_api//:dashapi` |

Common is fetched from
[securely1g/sonic-swss-common](https://github.com/securely1g/sonic-swss-common).
SAI and sairedis provide their headers and libraries through their own module
targets. DASH generates its headers and builds `libdashapi.so` with the
source-built Protobuf 3.21.12 compiler. SWSS and DASH both use
`@protobuf_legacy//:libprotobuf`, built as a shared runtime from that same upstream
release; neither consumer links a separate static Protobuf implementation.
The remaining Debian development libraries come from the declared `trixie` and
`swss_debian` sets in [MODULE.bazel](../MODULE.bazel).

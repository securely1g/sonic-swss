# Explicit CI DEB import prototype

This prototype exposes the SONiC package payloads that the existing SWSS CI
selects before its native build. It requires a local manifest with exact
SHA-256 values. `prepare.py` downloads one of the pinned input sets described
below. The repository rule reads no default manifest and downloads no remote
artifacts.

## Prepare pinned inputs

Run the preparation helper from the SWSS checkout:

```sh
python3 third_party/ci_debs/prepare.py \
  --architecture amd64 \
  --output-directory /absolute/path/to/swss-ci-debs
```

Use `--architecture arm64` for the native ARM64 build. This selects the inputs;
an ARM64 build with SWSS as the root module also uses `--config=aarch64`.
The helper writes `manifest.json` and the seven declared DEB basenames into the
output directory.
It hashes an existing DEB before reusing it. For missing or mismatched DEBs, it
downloads each needed ZIP once, reads only the exact listed members, verifies
each package SHA-256, and atomically replaces the corresponding output file.
It preserves unrelated files in the output directory. A directory, symlink, or
other non-regular entry at a declared output path is reported as an error.

The helper uses Python's verified HTTPS handling and requires no Azure token
for these public artifacts. It does not install packages. The Bazel importer
continues to validate package names, control metadata, and architecture.

## Select the prototype

When SWSS is the root module, use its local extension label in `MODULE.bazel`:

```starlark
ci_debs = use_extension("//third_party/ci_debs:extensions.bzl", "ci_debs")
use_repo(ci_debs, "sonic_ci_debs")
```

An external caller uses the SWSS repository label instead:

```starlark
ci_debs = use_extension(
    "@sonic_swss//third_party/ci_debs:extensions.bzl",
    "ci_debs",
)
use_repo(ci_debs, "sonic_ci_debs")
```

Pass the absolute manifest path when a target uses the repository:

```sh
bazel build \
  --repo_env=SONIC_SWSS_CI_DEBS_MANIFEST=/absolute/path/to/manifest.json \
  @sonic_ci_debs//:saimetadata
```

Declaring the repository does not read the environment variable. Bazel fetches
it when a label in the repository is used; that fetch reports an error if the
manifest is missing.

The `ci-debs` configuration selects the SWSS provider, the imported schema,
and Common's existing YANG C++ sources. In an external caller, it contains
these three flags:

```text
--@sonic_swss//tools/bazel:ci_debs=True
--@sonic_swss_common//tools/bazel:cfg_schema=@sonic_ci_debs//:cfg_schema
--@sonic_swss_common//tools/bazel:yang_modules=True
```

When SWSS is the root module, the first flag is
`--//tools/bazel:ci_debs=True`; the other two flags are unchanged.
`--repo_env=SONIC_SWSS_CI_DEBS_MANIFEST=...` supplies the manifest and files to
the repository rule. It does not select the provider. See the
[canonical external caller guide](../../bazel/README.md#build-c-targets-from-an-external-module-caller)
for the complete module and Bazel configuration.

These imports provide C++ headers and shared libraries to Bazel actions. They
do not install packages into the prepared build environment. The locked Cargo
action uses that environment's installed inputs. Keep those inputs in the
prepared environment and its action environment digest.

## Manifest

`manifest.amd64.json` records the exact Trixie amd64 packages selected by
successful SWSS CI build 1231297: sairedis build 1230428, DASH build 1231190,
and swss-common build 1229287. `manifest.arm64.json` records the corresponding
ARM64 artifacts from those same producer builds and source commits. Both sets
were downloaded and their package hashes verified on 2026-09-27.

These are pinned CI artifacts whose upstream retention remains an availability
dependency. They are not durable releases. The helper uses their exact download
URLs and does not select the latest artifact from a branch.

Both manifests retain `schema_version: 1` and the same seven production package
keys. Each package's `artifact` names an entry in `provenance`, and `member` is
the exact ZIP member read by the helper. The provenance entries retain the
producer build and source commit and add the artifact ID and download URL.
`prepare.py` copies the selected manifest to the output as `manifest.json`.

For manual preparation, place the seven listed DEBs next to a copy of either
manifest, or replace each `path` with an absolute local path. Relative package
paths resolve against the manifest directory. The importer consumes `path` and
`sha256`; the optional `provenance` object is copied to the generated
`IMPORTS.json` for inspection. The SHA-256 values select package content.

The rule accepts one Trixie architecture per manifest: `amd64` or `arm64`.
Each package's control metadata must match the manifest architecture and the
package key. Targets carry the corresponding Linux CPU constraint.

The rule watches the manifest and every listed package. On each repository
fetch it reads the local DEB with Bazel's downloader, compares the returned
SHA-256 with the manifest, then uses `repository_ctx.extract` for the DEB and
its control and data archives. The downloader is called without a cache lookup
checksum so a changed local file cannot be replaced by an older repository
cache entry before validation. Extracted headers and libraries are ordinary
Bazel source inputs, so their content participates in action cache keys.

## Production labels

| Label | Package payload |
| --- | --- |
| `@sonic_ci_debs//:cfg_schema` | Only `cfg_schema.h` from `libswsscommon-dev` |
| `@sonic_ci_debs//:sai_headers` | SAI headers from `libsaivs-dev` |
| `@sonic_ci_debs//:saimetadata` | Metadata headers, transitive `sairedis.h`, and `libsaimetadata.so.0` |
| `@sonic_ci_debs//:saimeta` | `libsaimeta.so.0`, plus `:saimetadata` |
| `@sonic_ci_debs//:sairedis` | `sairedis.h` and `libsairedis.so.0` |
| `@sonic_ci_debs//:dashapi` | Generated DASH headers and `libdashapi.so` |

The imports use the `.so.0` paths that match the inspected SAI libraries'
SONAMEs. DASH's inspected library has no SONAME and retains `libdashapi.so`.
The dev packages' unversioned symlinks are not used for linking because their
targets live in separate runtime packages.

`saimetadata` depends on `sairedis_headers`, so consumers receive the
`sairedis.h` include required by its metadata headers without adding a separate
header dependency.

The imported schema defines `CFG_MCLAG_UNIQUE_IP_TABLE_NAME` and
`CFG_DEVICE_METADATA_TABLE_NAME`, which the reference's no-YANG stub omits.
The import discards the rest of the `libswsscommon-dev` payload after copying
this generated header.

Root consumer dependency lists must also supply the selected swss-common shared
library for `saimeta` and `sairedis`, and Debian protobuf headers and
`libprotobuf.so.32` for DASH. The prototype does not package these libraries
into the SWSS distribution.

## Test-only follow-up

The production import uses `libsaivs-dev` only for SAI headers. Tests later need
`libsaivs.so.0` from `libsaivs`; the inspected amd64 library also has direct
runtime dependencies on `libvlib.so.26.10`, `libvlibapi.so.26.10`,
`libvppapiclient.so.26.10`, `libvlibmemoryclient.so.26.10`, and
`libvppinfra.so.26.10`. This prototype exposes no `saivs` or VPP label.

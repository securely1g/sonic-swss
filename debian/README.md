# SWSS Debian packages from Bazel outputs

`//debian:swss_deb` packages the existing `//dist:swss_pkg` runtime.
`//debian:swss_dbg_deb` packages its matching debug symbols. Both use the
original `1.0.0` version, source control/changelog and Debian helper sequence.
The existing source-contract CI checks runtime files against Automake and the
supplemental `debian/swss.install` entries.

The original `dh_fixperms` helper normalizes permissions before packaging:
configuration and documentation files use `0644`, and programs use `0755`.
It runs in Debian's rootless mode; the archive rule records root ownership.

Debian's `dh_installdeb` registers configuration files, including
`/etc/swss/config.d/netbouncer.json`. The shared metadata rule derives runtime
package dependencies from the actual ELF files and matching Common, sairedis,
DASH, protobuf, libnl and external library metadata. The debug package keeps the original
`Depends: swss (= 1.0.0)` contract.

Build `//debian:swss_check` and `//debian:swss_dbg_check` to compare actual DEBs
with their declared payload, generated control, file ownership, modes, checksums
and native architecture. Package installation must additionally be checked with
the real dependency DEBs in a fresh native Debian container.

CI preserves the existing source, runtime and debug checks, then builds both
DEBs on native AMD64 and ARM64. A separate fresh Debian container installs the
real dependency packages, checks that every packaged ELF program loads its
libraries, and tests reinstall, configuration preservation, removal and purge.
The dependency closure includes the matching source-built `libnl-cli` package:
Debian's `libteam5` requires it, and it must match the other libnl package versions.
These packages retain the source version `1.0.0`; reinstall is checked without
claiming a transition between different package versions.

"""Package the existing SWSS runtime and debug TARs with original Debian metadata."""

load("@sonic_build_infra//deb:metadata.bzl", "debian_metadata")
load("@sonic_build_infra//deb:sonic_deb.bzl", "sonic_deb")
load("@sonic_build_infra//deb:validation.bzl", "validate_debian_package")

def swss_debian_package(name, package_name, data, dependencies = [], conffiles = False):
    """Use the original Debian control and helpers for one existing payload.

    Args:
        name: Prefix for the package, metadata and validation targets.
        package_name: Binary package name from the original Debian control.
        data: Existing runtime or debug payload TAR.
        dependencies: Library metadata providers for ELF dependency analysis.
        conffiles: Whether the payload has configuration files under /etc.
    """
    native.genrule(
        name = name + "_stage",
        srcs = [data, "control", "changelog", "compat"],
        outs = [name + "/data.tar", name + "/stage.json"] + ([name + "/conffiles"] if conffiles else []),
        tools = [":stage", "@sonic_build_infra//tools/build_tools:dh_fixperms", "@sonic_build_infra//tools/build_tools:dh_installdeb"],
        toolchains = ["@bsd_tar_toolchains//:resolved_toolchain"],
        cmd = select({
            "@platforms//cpu:x86_64": "arch=amd64\n",
            "@platforms//cpu:aarch64": "arch=arm64\n",
        }) + """
set -eu
$(execpath :stage) --data $(location {data}) \\
  --source "$$(dirname $(location control))" \\
  --package {package_name} --architecture "$$arch" --tar "$(BSDTAR_BIN)" \\
  --fixperms $(execpath @sonic_build_infra//tools/build_tools:dh_fixperms) \\
  --installdeb $(execpath @sonic_build_infra//tools/build_tools:dh_installdeb) \\
  --output-data $(location {name}/data.tar) --receipt $(location {name}/stage.json) {extra}
""".format(
            name = name,
            package_name = package_name,
            data = data,
            extra = "--conffiles $(location " + name + "/conffiles)" if conffiles else "",
        ),
    )
    debian_metadata(
        name = name + "_metadata",
        control = "control",
        changelog = "changelog",
        data = ":" + name + "/data.tar",
        package = package_name,
        dependencies = dependencies,
    )
    control_files = {":" + name + "/conffiles": "conffiles"} if conffiles else {}
    sonic_deb(
        name = name + "_deb",
        package = package_name,
        version = "1.0.0",
        data = ":" + name + "/data.tar",
        control_file = ":" + name + "_metadata",
        control_files = control_files,
        tags = ["manual"],
    )
    validate_debian_package(
        name = name + "_check",
        package = package_name,
        version = "1.0.0",
        deb = ":" + name + "_deb",
        data = ":" + name + "/data.tar",
        control = ":" + name + "_metadata",
        control_files = control_files,
        tags = ["manual"],
    )

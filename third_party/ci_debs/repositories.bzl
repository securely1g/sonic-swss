"""Explicit local import of the SONiC DEBs already selected by CI."""

_MANIFEST_ENV = "SONIC_SWSS_CI_DEBS_MANIFEST"

_PACKAGES = [
    "libdashapi",
    "libsaimetadata",
    "libsaimetadata-dev",
    "libsairedis",
    "libsairedis-dev",
    "libsaivs-dev",
    "libswsscommon-dev",
]

_ARCHITECTURES = {
    "amd64": struct(cpu = "x86_64", multiarch = "x86_64-linux-gnu"),
    "arm64": struct(cpu = "arm64", multiarch = "aarch64-linux-gnu"),
}

_ARCHIVE_SUFFIXES = [".xz", ".gz", ".zst", ""]

def _file_url(path):
    value = str(path)
    # These are the characters that cannot be used literally in a file URL's
    # path. Keep '/' intact so absolute paths remain absolute.
    for char, escaped in {
        "%": "%25",
        " ": "%20",
        "#": "%23",
        "?": "%3F",
        "[": "%5B",
        "]": "%5D",
        "{": "%7B",
        "}": "%7D",
        "|": "%7C",
        "\\": "%5C",
        "^": "%5E",
        "`": "%60",
        "\"": "%22",
        "<": "%3C",
        ">": "%3E",
        "\n": "%0A",
        "\r": "%0D",
        "\t": "%09",
    }.items():
        value = value.replace(char, escaped)
    return "file://" + value

def _archive_in(directory, basename):
    matches = [
        directory.get_child(basename + suffix)
        for suffix in _ARCHIVE_SUFFIXES
        if directory.get_child(basename + suffix).exists
    ]
    if len(matches) != 1:
        fail("Expected one {} archive in {}, found {}".format(basename, directory, matches))
    return matches[0]

def _control_fields(content):
    fields = {}
    for line in content.splitlines():
        if not line or line.startswith(" ") or line.startswith("\t"):
            continue
        parts = line.split(":", 1)
        if len(parts) == 2:
            fields[parts[0]] = parts[1].strip()
    return fields

def _validate_sha256(package, sha256):
    if type(sha256) != "string" or len(sha256) != 64:
        fail("{} must have a 64-character lowercase SHA-256".format(package))
    if any([char not in "0123456789abcdef" for char in sha256.elems()]):
        fail("{} must have a 64-character lowercase SHA-256".format(package))

def _import_package(rctx, manifest_path, package, entry, architecture):
    if type(entry) != "dict":
        fail("Manifest entry for {} must be an object".format(package))
    path_value = entry.get("path")
    if type(path_value) != "string" or not path_value:
        fail("Manifest entry for {} must have a local path".format(package))
    sha256 = entry.get("sha256")
    _validate_sha256(package, sha256)

    package_path = rctx.path(path_value) if path_value.startswith("/") else manifest_path.dirname.get_child(path_value)
    rctx.watch(package_path)
    if not package_path.exists:
        fail("Manifest package does not exist: {}".format(package_path))

    archive = "_archives/{}.deb".format(package)
    # Read the selected local file on every repository fetch. Passing sha256 to
    # download() could satisfy the request from the repository cache without
    # reading a changed local file; compare the returned digest instead.
    downloaded = rctx.download(
        url = _file_url(package_path),
        output = archive,
    )
    if downloaded.sha256 != sha256:
        fail("SHA-256 mismatch for {}: expected {}, got {}".format(package_path, sha256, downloaded.sha256))

    deb_dir = "_archives/{}".format(package)
    rctx.extract(archive, output = deb_dir, watch_archive = "no")
    deb_path = rctx.path(deb_dir)

    control_dir = "_control/{}".format(package)
    rctx.extract(_archive_in(deb_path, "control.tar"), output = control_dir, watch_archive = "no")
    control = _control_fields(rctx.read(control_dir + "/control", watch = "no"))
    if control.get("Package") != package:
        fail("{} contains package {}, expected {}".format(package_path, control.get("Package"), package))
    if control.get("Architecture") != architecture:
        fail("{} has architecture {}, expected {}".format(package_path, control.get("Architecture"), architecture))

    payload_dir = "packages/{}".format(package)
    rctx.extract(_archive_in(deb_path, "data.tar"), output = payload_dir, watch_archive = "no")
    if package == "libswsscommon-dev":
        # Only the generated schema is selected from this package. The native
        # common library and the rest of its headers stay outside this import.
        schema = payload_dir + "/usr/include/swss/cfg_schema.h"
        if not rctx.path(schema).exists:
            fail("{} is missing usr/include/swss/cfg_schema.h".format(package_path))
        rctx.file("cfg_schema.h", rctx.read(schema, watch = "no"), executable = False)
        rctx.delete(payload_dir)
    return {
        "architecture": control["Architecture"],
        "sha256": sha256,
        "version": control.get("Version"),
    }

def _ci_debs_repository_impl(rctx):
    manifest_value = rctx.os.environ.get(_MANIFEST_ENV)
    if not manifest_value:
        fail("This prototype requires --repo_env={}=/absolute/path/to/manifest.json when @{} is used".format(_MANIFEST_ENV, rctx.name))
    if not manifest_value.startswith("/"):
        fail("{} must be an absolute local manifest path".format(_MANIFEST_ENV))

    manifest_path = rctx.path(manifest_value)
    rctx.watch(manifest_path)
    if not manifest_path.exists:
        fail("{} does not exist: {}".format(_MANIFEST_ENV, manifest_path))
    manifest = json.decode(rctx.read(manifest_path, watch = "no"))
    if type(manifest) != "dict" or manifest.get("schema_version") != 1:
        fail("CI DEB manifest must be an object with schema_version 1")
    if manifest.get("distribution") != "trixie":
        fail("This prototype accepts Trixie SONiC packages")
    architecture = manifest.get("architecture")
    if architecture not in _ARCHITECTURES:
        fail("CI DEB manifest architecture must be one of {}".format(sorted(_ARCHITECTURES.keys())))
    packages = manifest.get("packages")
    if type(packages) != "dict" or sorted(packages.keys()) != _PACKAGES:
        fail("CI DEB manifest packages must be exactly {}".format(_PACKAGES))

    imported = {}
    for package in _PACKAGES:
        imported[package] = _import_package(rctx, manifest_path, package, packages[package], architecture)

    platform = _ARCHITECTURES[architecture]
    required_files = [
        "cfg_schema.h",
        "packages/libdashapi/usr/include/dash_api/types.pb.h",
        "packages/libdashapi/usr/lib/libdashapi.so",
        "packages/libsaimetadata-dev/usr/include/sai/saimetadata.h",
        "packages/libsaimetadata/usr/lib/{}/libsaimeta.so.0".format(platform.multiarch),
        "packages/libsaimetadata/usr/lib/{}/libsaimetadata.so.0".format(platform.multiarch),
        "packages/libsairedis-dev/usr/include/sai/sairedis.h",
        "packages/libsairedis/usr/lib/{}/libsairedis.so.0".format(platform.multiarch),
        "packages/libsaivs-dev/usr/include/sai/sai.h",
    ]
    for path in required_files:
        if not rctx.path(path).exists:
            fail("Imported package payload is missing {}".format(path))

    rctx.template(
        "BUILD.bazel",
        rctx.attr._build_template,
        substitutions = {
            "{CPU}": platform.cpu,
            "{MULTIARCH}": platform.multiarch,
        },
        executable = False,
    )
    rctx.file("IMPORTS.json", json.encode_indent({
        "architecture": architecture,
        "distribution": manifest["distribution"],
        "packages": imported,
        "provenance": manifest.get("provenance", {}),
        "schema_version": 1,
    }) + "\n", executable = False)
    rctx.delete("_archives")
    rctx.delete("_control")

ci_debs_repository = repository_rule(
    implementation = _ci_debs_repository_impl,
    attrs = {
        "_build_template": attr.label(default = Label("//third_party/ci_debs:BUILD.template.bazel")),
    },
    environ = [_MANIFEST_ENV],
    local = True,
    doc = "Imports explicitly selected local SONiC DEBs using the manifest named by SONIC_SWSS_CI_DEBS_MANIFEST.",
)

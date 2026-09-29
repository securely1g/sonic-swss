"""Small C++ macros that preserve SWSS's native build configurations."""

load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library", "cc_test")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel:production_sources.bzl", "SWSS_PROGRAMS")
load(
    ":flags.bzl",
    "ASAN_COPTS",
    "ASAN_LINKOPTS",
    "CXXFLAGS_COMMON",
    "DBGFLAGS",
    "DBGFLAGS_NDEBUG",
    "GCOV_COPTS",
    "GCOV_LINKOPTS",
    "RELEASE_HARDENING_COPTS",
)

_SWSS_REPOSITORY = Label("//:MODULE.bazel")

def _cxxopts(cxxopts, ndebug, asan):
    return CXXFLAGS_COMMON + (DBGFLAGS_NDEBUG if ndebug else DBGFLAGS) + (ASAN_COPTS if asan else []) + GCOV_COPTS + cxxopts

def _linkopts(linkopts, asan):
    return (ASAN_LINKOPTS if asan else []) + GCOV_LINKOPTS + linkopts

def _production_include_cxxopts(name):
    """Returns native local header search paths for a production program."""
    program = SWSS_PROGRAMS.get(name)
    if (
        program == None or
        program["directory"] != native.package_name() or
        native.repository_name().lstrip("@") != _SWSS_REPOSITORY.repo_name
    ):
        return []

    options = []
    repository_root = _SWSS_REPOSITORY.workspace_root
    for directory in program["local_include_directories"]:
        path = directory
        if repository_root:
            path = repository_root if directory == "." else repository_root + "/" + directory
        options.append("-I" + path)
    return options

def swss_cc_library(name, srcs = [], hdrs = [], deps = [], cxxopts = [], linkopts = [], ndebug = False, asan = True, **kwargs):
    """Declares an internal SWSS library with the native warning flags."""
    cc_library(
        name = name,
        srcs = srcs,
        hdrs = hdrs,
        deps = deps,
        cxxopts = _cxxopts(cxxopts, ndebug, asan),
        linkopts = _linkopts(linkopts, asan),
        **kwargs
    )

def swss_headers(name, hdrs, includes):
    """Declares compatible public headers and production header inputs.

    The public target retains its propagated include directories. Production
    binaries use the companion target so their generated native -I options
    determine the local header search order.

    Args:
      name: Public header target name.
      hdrs: Header labels shared by both targets.
      includes: Include directories propagated by the public target.
    """
    swss_cc_library(
        name = name,
        hdrs = hdrs,
        asan = False,
        includes = includes,
    )

    cc_library(
        name = "production_" + name,
        hdrs = hdrs,
    )

def swss_cc_binary(name, srcs = [], deps = [], cxxopts = [], linkopts = [], ndebug = False, asan = True, gcov_preload = "static", **kwargs):
    """Declares a daemon and retains its ASAN/GCOV startup objects.

    Args:
      name: Bazel target name.
      srcs: C++ source labels.
      deps: C++ dependency labels.
      cxxopts: Additional C++ compiler options.
      linkopts: Additional linker options.
      ndebug: Whether to define NDEBUG outside the debug configuration.
      asan: Whether to apply ASAN flags and startup sources when enabled.
      gcov_preload: GCOV startup mode: "static", "shared", or "none".
      **kwargs: Additional cc_binary attributes.
    """
    if gcov_preload not in ["static", "shared", "none"]:
        fail("gcov_preload must be static, shared, or none")

    if asan:
        srcs = srcs + select({
            "//tools/bazel:asan_enabled": [
                "//lib:asan.cpp",
                "//lib:asan_ctor.cpp",
            ],
            "//conditions:default": [],
        })

    if gcov_preload == "static":
        srcs = srcs + select({
            "//tools/bazel:gcov_enabled": ["//gcovpreload:gcovpreload.cpp"],
            "//conditions:default": [],
        })
    elif gcov_preload == "shared":
        deps = deps + select({
            "//tools/bazel:gcov_enabled": ["//gcovpreload:gcovpreload_shared"],
            "//conditions:default": [],
        })

    cc_binary(
        name = name,
        srcs = srcs,
        deps = deps,
        cxxopts = _cxxopts(cxxopts, ndebug, asan) + _production_include_cxxopts(name) + RELEASE_HARDENING_COPTS,
        linkopts = _linkopts(linkopts, asan),
        **kwargs
    )

def swss_cc_test(name, srcs = [], deps = [], cxxopts = [], linkopts = [], ndebug = False, asan = False, data = [], args = [], **kwargs):
    """Declares a native test and runs it from its package's runfiles directory.

    Args:
      name: Public test target name.
      srcs: C++ source labels.
      deps: C++ dependency labels.
      cxxopts: Additional C++ compiler options.
      linkopts: Additional linker options.
      ndebug: Whether to define NDEBUG outside the debug configuration.
      asan: Whether to apply ASAN flags when enabled.
      data: Runfiles required by the binary and wrapper.
      args: Arguments passed to the native test binary.
      **kwargs: Additional test attributes forwarded to the binary or wrapper.
    """
    wrapper_attrs = {}
    for attr_name in [
        "env",
        "env_inherit",
        "exec_compatible_with",
        "exec_properties",
        "flaky",
        "local",
        "shard_count",
        "size",
        "timeout",
        "visibility",
    ]:
        if attr_name in kwargs:
            wrapper_attrs[attr_name] = kwargs.pop(attr_name)

    tags = kwargs.pop("tags", [])
    target_compatible_with = kwargs.pop("target_compatible_with", [])
    binary_name = "_" + name + "_binary"

    cc_test(
        name = binary_name,
        srcs = srcs,
        deps = deps,
        data = data,
        cxxopts = _cxxopts(cxxopts, ndebug, asan),
        linkopts = _linkopts(linkopts, asan),
        tags = ["manual"],
        target_compatible_with = target_compatible_with,
        visibility = ["//visibility:private"],
        **kwargs
    )

    sh_test(
        name = name,
        srcs = ["//tools/bazel:run_cc_test.sh"],
        args = [
            "$(rootpath :" + binary_name + ")",
            native.package_name(),
        ] + args,
        data = [":" + binary_name] + data,
        tags = tags,
        target_compatible_with = target_compatible_with,
        **wrapper_attrs
    )

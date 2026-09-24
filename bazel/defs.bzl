"""Actions used by the generated SONiC SWSS Bazel package."""


def _swss_cargo_binary_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name)
    args = ctx.actions.args()
    args.add("--source-root", ctx.attr.source_root)
    args.add_all(ctx.files.srcs, before_each = "--source")
    args.add("--output", output.path)
    args.add("--source-date-epoch", ctx.attr.source_date_epoch)
    ctx.actions.run_shell(
        command = 'exec python3 "$@"',
        arguments = [ctx.file._builder.path, args],
        inputs = depset(ctx.files.srcs),
        tools = [ctx.file._builder],
        outputs = [output],
        mnemonic = "SwssCargo",
        progress_message = "Building locked countersyncd release binary",
        use_default_shell_env = True,
        execution_requirements = {
            "no-remote-exec": "1",
            "no-sandbox": "1",
        },
    )
    return [DefaultInfo(files = depset([output]))]


swss_cargo_binary = rule(
    implementation = _swss_cargo_binary_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True, mandatory = True),
        "source_root": attr.string(mandatory = True),
        "source_date_epoch": attr.string(mandatory = True),
        "_builder": attr.label(
            default = Label("//swss:tools/build_cargo.py"),
            allow_single_file = True,
        ),
    },
)


def _swss_debian_packages_impl(ctx):
    swss = ctx.outputs.swss_out
    debug = ctx.outputs.debug_out
    manifest = ctx.outputs.manifest_out
    args = ctx.actions.args()
    args.add("build")
    args.add("--inventory", ctx.file.inventory.path)
    args.add("--source-root", ctx.attr.source_root)
    args.add_all(ctx.files.srcs, before_each = "--source")
    args.add("--cargo", ctx.file.cargo.path)
    args.add("--swss-output", swss.path)
    args.add("--debug-output", debug.path)
    args.add("--manifest-output", manifest.path)

    binaries = []
    by_install_path = {}
    for target, install_path in ctx.attr.binaries.items():
        if install_path in by_install_path:
            fail("duplicate SWSS install path: %s" % install_path)
        by_install_path[install_path] = target
    for install_path in sorted(by_install_path):
        target = by_install_path[install_path]
        executable = target[DefaultInfo].files_to_run.executable
        if executable == None:
            fail("SWSS program does not provide an executable: %s" % target.label)
        binaries.append(executable)
        args.add("--binary", "%s=%s" % (install_path, executable.path))

    ctx.actions.run_shell(
        command = 'exec python3 "$@"',
        arguments = [ctx.file._builder.path, args],
        inputs = depset(ctx.files.srcs + binaries + [ctx.file.cargo, ctx.file.inventory]),
        tools = [ctx.file._builder],
        outputs = [swss, debug, manifest],
        mnemonic = "SwssDebianPackages",
        progress_message = "Packaging SWSS and debug symbols with debhelper",
        use_default_shell_env = True,
        execution_requirements = {
            "no-remote-exec": "1",
            "no-sandbox": "1",
        },
    )
    return [DefaultInfo(files = depset([swss, debug, manifest]))]


swss_debian_packages = rule(
    implementation = _swss_debian_packages_impl,
    attrs = {
        "binaries": attr.label_keyed_string_dict(mandatory = True),
        "cargo": attr.label(allow_single_file = True, mandatory = True),
        "srcs": attr.label_list(allow_files = True, mandatory = True),
        "inventory": attr.label(allow_single_file = True, mandatory = True),
        "source_root": attr.string(mandatory = True),
        "swss_out": attr.output(mandatory = True),
        "debug_out": attr.output(mandatory = True),
        "manifest_out": attr.output(mandatory = True),
        "_builder": attr.label(
            default = Label("//swss:tools/package_deb.py"),
            allow_single_file = True,
        ),
    },
)

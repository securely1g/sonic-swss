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
            default = Label(":tools/build_cargo.py"),
            allow_single_file = True,
        ),
    },
)

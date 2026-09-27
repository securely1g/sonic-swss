"""Stages generated bindings in the OUT_DIR layout used by swss-common."""

def _bindings_directory_impl(ctx):
    out = ctx.actions.declare_directory(ctx.label.name)
    args = ctx.actions.args()
    args.add(ctx.file.src)
    args.add_all([out], expand_directories = False)

    ctx.actions.run(
        executable = ctx.attr._stage_bindings[DefaultInfo].files_to_run,
        inputs = [ctx.file.src],
        outputs = [out],
        arguments = [args],
        mnemonic = "StageRustBindings",
        progress_message = "Staging Rust bindings for %{label}",
    )

    return [DefaultInfo(files = depset([out]))]

bindings_directory = rule(
    implementation = _bindings_directory_impl,
    attrs = {
        "src": attr.label(allow_single_file = [".rs"], mandatory = True),
        "_stage_bindings": attr.label(
            default = Label("//tools/bazel/rust:stage_bindings"),
            executable = True,
            cfg = "exec",
        ),
    },
)

"""Module extension for the explicitly selected local CI DEB prototype."""

load(":repositories.bzl", "ci_debs_repository")

def _ci_debs_impl(_module_ctx):
    ci_debs_repository(name = "sonic_ci_debs")

ci_debs = module_extension(implementation = _ci_debs_impl)

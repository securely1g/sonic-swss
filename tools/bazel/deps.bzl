"""Dependencies shared by SWSS targets.

The common set uses the SONiC and Debian modules from the reference
sonic-swss-common migration.
"""

SWSS_DEPS = [
    "@sonic_swss_common//:libswsscommon_shared",
    "@libnl3//:libnl_3",
    "@libnl3//:libnl_genl_3",
    "@libnl3//:libnl_nf_3",
    "@libnl3//:libnl_route_3",
    "@trixie//libboost-dev:libboost",
    "@trixie//libhiredis-dev:libhiredis",
    "@trixie//libzmq3-dev:libzmq3",
    "@trixie//nlohmann-json3-dev:nlohmann-json3",
]

_CI_DEBS_REQUIRED = "This target needs SONiC dependency inputs. The current prototype uses --config=ci-debs with --repo_env=SONIC_SWSS_CI_DEBS_MANIFEST=/absolute/path/to/manifest.json."

CFGMGR_DEPS = SWSS_DEPS + select({
    "//tools/bazel:ci_debs_enabled": ["@sonic_ci_debs//:saimeta"],
}, no_match_error = _CI_DEBS_REQUIRED)

ORCHAGENT_DEPS = CFGMGR_DEPS + select({
    "//tools/bazel:ci_debs_enabled": [
        "@sonic_ci_debs//:dashapi",
        "@sonic_ci_debs//:sairedis",
    ],
}, no_match_error = _CI_DEBS_REQUIRED) + [
    "@swss_debian//libjemalloc-dev:libjemalloc",
    "@swss_debian//libprotobuf-dev:libprotobuf",
    "@swss_debian//libyaml-cpp-dev:libyaml-cpp",
]

# rules_distroless exposes libteam and libteamdctl through one public target.
TEAM_DEPS = SWSS_DEPS + ["@swss_debian//libteam-dev:libteam"]

TLM_TEAM_DEPS = TEAM_DEPS + ["@swss_debian//libjansson-dev:libjansson"]

TEST_DEPS = SWSS_DEPS + select({
    "//tools/bazel:ci_debs_enabled": ["@sonic_ci_debs//:sai_headers"],
}, no_match_error = _CI_DEBS_REQUIRED) + [
    "@com_google_googletest//:gtest",
    "@com_google_googletest//:gtest_main",
]

P4_TEST_DEPS = TEST_DEPS + select({
    "//tools/bazel:ci_debs_enabled": [
        "@sonic_ci_debs//:saimeta",
        "@sonic_ci_debs//:sairedis",
    ],
}, no_match_error = _CI_DEBS_REQUIRED) + [
    "@com_google_googletest//:gmock",
    "@com_google_googletest//:gmock_main",
]

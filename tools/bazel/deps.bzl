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

CFGMGR_DEPS = SWSS_DEPS + ["@sonic_sairedis//meta:saimeta_shared"]

ORCHAGENT_DEPS = CFGMGR_DEPS + [
    "@sonic_dash_api//:dashapi",
    "@sonic_sairedis//lib:sairedis_shared",
    "@swss_debian//libjemalloc-dev:libjemalloc",
    "@protobuf_legacy//:libprotobuf",
    "@swss_debian//libyaml-cpp-dev:libyaml-cpp",
]

# rules_distroless exposes libteam and libteamdctl through one public target.
TEAM_DEPS = SWSS_DEPS + ["@swss_debian//libteam-dev:libteam"]

TLM_TEAM_DEPS = TEAM_DEPS + ["@swss_debian//libjansson-dev:libjansson"]

TEST_DEPS = SWSS_DEPS + [
    "@com_google_googletest//:gtest",
    "@com_google_googletest//:gtest_main",
    "@sai//:upstream_headers",
]

P4_TEST_DEPS = TEST_DEPS + [
    "@com_google_googletest//:gmock",
    "@com_google_googletest//:gmock_main",
    "@sonic_sairedis//lib:sairedis_shared",
    "@sonic_sairedis//meta:saimeta_shared",
]

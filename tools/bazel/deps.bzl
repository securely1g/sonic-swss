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
    "@trixie//nlohmann-json3-dev:nlohmann-json3",
]

CFGMGR_DEPS = SWSS_DEPS + [
    "@sonic_sairedis//meta:saimeta_shared",
    "@trixie//libzmq3-dev:libzmq3",
]

ORCHAGENT_DEPS = CFGMGR_DEPS + [
    "@sonic_dash_api//:dashapi",
    "@sonic_sairedis//lib:sairedis_shared",
    "@swss_debian//libjemalloc-dev:libjemalloc",
    "@protobuf_legacy//:libprotobuf",
    "@swss_debian//libyaml-cpp-dev:libyaml-cpp",
]

# SONiC-patched libteam owns the headers, shared libraries and runtime TARs.
TEAM_DEPS = SWSS_DEPS + [
    "@libteam//:libteam",
    "@libteam//:libteamdctl",
]

TLM_TEAM_DEPS = SWSS_DEPS + [
    "@libteam//:libteamdctl",
    "@swss_debian//libjansson-dev:libjansson",
]

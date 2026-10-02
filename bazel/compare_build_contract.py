#!/usr/bin/env python3
"""Compare native release settings with resolved production Bazel rules.

This deliberately checks target attributes and dependency-label reachability.
It does not inspect toolchain actions, response files, or provider paths.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from ci_production import ARCHITECTURES, ROOT, SOURCE_MAP, load_programs, program_labels, relative_path, sha256
from generate import MODULE_BUILD_MAPPINGS


DEPENDENCY_ATTRIBUTES = ("actual", "deps", "dynamic_deps", "exports", "implementation_deps")
UNVERIFIED_SCOPE = [
    "Toolchain actions and command-line response files are not inspected.",
    "Transitive provider include, header, and library paths are not inspected.",
    "Repeated aliases with the same resolved actual label are collapsed for label reachability; configuration-specific provider behavior is not inspected.",
    "External header search precedence and native link-order/runtime parity are not established.",
    "DEBUG, ASAN, GCOV, Rust, Cargo, and runtime targets are outside this normal-release comparison.",
]
INTENTIONAL_DIFFERENCES = {
    "native_config_header": "The selected production sources do not include config.h, so target cxxopts omit -DHAVE_CONFIG_H.",
    "target_fortification": "The target uses fortification level 3 in place of the standalone native configure profile's level 2.",
    "target_warnings": "The existing -Wformat=2 and -Werror controls cover the mapped native warning options; Bazel also disables missing-include-directory warnings.",
    "native_link_driver_flags": "Mapped ordinary compile flags repeated on the native link driver are omitted from target linkopts.",
    "source_prefix_maps": "Native source prefix maps are not target cxxopts; this check does not verify their toolchain-action equivalent.",
    "reviewed_toolchain": "Mapped optimization and hardening options, including the recorded additional link options, are accepted for the exact reviewed infra version without inspecting its actions.",
    "dependency_labels": "Native libraries and system include roots map to reviewed reachable labels; provider paths and link ordering are outside this check.",
    "local_header_inputs": "Named production header targets add declared inputs without propagating include directories or options.",
    "rule_support": "The named rules_cc link support label is retained separately from native libraries.",
}


def canonical_label(label: str, repository_mapping: dict[str, str]) -> str:
    if label.startswith("@@"):
        return label
    if label.startswith("//"):
        return "@@" + label
    match = re.fullmatch(r"@([^/]+)(//.+)", label)
    if match is None or match[1] not in repository_mapping:
        raise ValueError(f"label has no root repository mapping: {label}")
    return "@@" + repository_mapping[match[1]] + match[2]


def mapped_labels(mappings: dict[str, Any]) -> list[str]:
    system = mappings["implicit_system_header_providers"]
    labels = [label for label in mappings["library_providers"].values() if not label.startswith("toolchain:")]
    labels += system["all_programs"]
    labels += [label for values in system["programs"].values() for label in values]
    labels += mappings["local_header_providers"] + mappings["rule_support_providers"]
    return sorted(set(labels))


def cquery_expression(programs: dict[str, dict[str, Any]], mappings: dict[str, Any]) -> str:
    selected = "set(" + " ".join(program_labels(programs).values()) + ")"
    endpoints = "set(" + " ".join(mapped_labels(mappings)) + ")"
    return selected + " union allpaths(" + selected + ", " + endpoints + ")"


def list_attribute(rule: dict[str, Any], name: str) -> list[str]:
    attribute = rule["attributes"].get(name)
    if attribute is None:
        return []
    values = attribute.get("stringListValue", [])
    if attribute.get("type") not in ("LABEL_LIST", "STRING_LIST") or not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"invalid {name} attribute for {rule['name']}")
    return values


def rule_graph(evidence: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    data = evidence["cquery"]
    configurations = {str(item["id"]): item for item in data["configurations"]}
    if len(configurations) != len(data["configurations"]):
        raise ValueError("duplicate cquery configuration ID")
    rules = {}
    for item in data["results"]:
        rule = item["target"].get("rule")
        if not isinstance(rule, dict) or not rule.get("name", "").startswith("@@"):
            raise ValueError("configured-rule evidence must contain consistently labeled rules")
        name = rule["name"]
        attributes = {attribute["name"]: attribute for attribute in rule.get("attribute", [])}
        if len(attributes) != len(rule.get("attribute", [])):
            raise ValueError(f"duplicate configured attribute for {name}")
        configuration = str(item.get("configurationId"))
        if configuration not in configurations:
            raise ValueError(f"missing configured identity for {name}")
        edges = set()
        for key in DEPENDENCY_ATTRIBUTES:
            attribute = attributes.get(key, {})
            if attribute.get("type") == "LABEL":
                if attribute.get("stringValue"):
                    edges.add(attribute["stringValue"])
            elif attribute.get("type") == "LABEL_LIST":
                edges.update(attribute.get("stringListValue", []))
        if not all(isinstance(edge, str) and edge.startswith("@@") for edge in edges):
            raise ValueError(f"inconsistent dependency labels for {name}")
        if name in rules:
            previous = rules[name]
            actual = attributes.get("actual", {})
            previous_actual = previous["attributes"].get("actual", {})
            if (
                previous["kind"] != "alias" or rule["ruleClass"] != "alias"
                or actual.get("type") != "LABEL" or edges != {actual.get("stringValue")}
                or previous_actual.get("type") != "LABEL" or previous["edges"] != {previous_actual.get("stringValue")}
                or previous["edges"] != edges or configuration in previous["configurations"]
            ):
                raise ValueError(f"selected label has multiple configured results: {name}")
            previous["configurations"].add(configuration)
            continue
        rules[name] = {"name": name, "kind": rule["ruleClass"], "attributes": attributes, "configuration": configuration, "configurations": {configuration}, "edges": edges}
    return rules, configurations


def reachable(rules: dict[str, dict[str, Any]], start: str) -> set[str]:
    found = set()
    pending = [start]
    while pending:
        label = pending.pop()
        if label in found:
            continue
        found.add(label)
        if label in rules:
            pending.extend(rules[label]["edges"])
    return found


def normalize_compile_options(options: list[str]) -> dict[str, Any]:
    """Normalize only the repeated option families used by this release."""
    states = {}
    literals = []
    for option in options:
        macro = re.fullmatch(r"-([DU])([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?", option)
        if macro:
            states["macro:" + macro[2]] = option
        elif option in ("-g", "-g0", "-g1", "-g2", "-g3", "-ggdb"):
            states["debug"] = option
        elif option.startswith("-std="):
            states["standard"] = option
        elif option in ("-fPIC", "-fpic", "-fno-PIC", "-fno-pic"):
            states["pic"] = option
        elif option.startswith("-W") and not option.startswith("-Wl,"):
            warning = option[2:]
            disabled = warning.startswith("no-")
            if disabled:
                warning = warning[3:]
            if warning.startswith("error="):
                key = "warning-error:" + warning[6:]
            else:
                key = "warning:" + warning.split("=", 1)[0]
            states[key] = ("disabled:" if disabled else "enabled:") + warning
        else:
            literals.append(option)
    return {"states": states, "ordered_literals": literals}


def native_options(native: dict[str, Any], name: str, cpu: str) -> tuple[list[str], list[str], list[str], set[str], set[str]]:
    mappings = native["mappings"]
    program = native["programs"][name]
    compile_options = []
    local_includes = []
    include_repositories = set()
    for record in native["compile_profiles"][program["compile_profile"]]:
        option, kind = record["argument"], record["classification"]
        if kind == "include":
            match = re.fullmatch(r"(-I|-isystem|-iquote|-idirafter)(<[^>]+>)(/.*)?", option)
            if match is None or match[2] not in mappings["include_roots"]:
                raise ValueError(f"unmapped native include for {name}: {option}")
            if match[2] == "<swss>":
                if match[1] != "-I":
                    raise ValueError(f"unsupported native local include class for {name}: {option}")
                local_includes.append(match[3][1:] if match[3] else ".")
            elif mappings["include_roots"][match[2]].startswith("@"):
                include_repositories.add(mappings["include_roots"][match[2]])
        elif kind == "literal":
            compile_options.append(option)
        elif kind == "replacement" and option in mappings["compile_replacements"]:
            compile_options.extend(mappings["compile_replacements"][option])
        elif kind == "native_only" and option in mappings["native_only_compile_options"]:
            pass
        elif kind == "toolchain" and option in mappings["toolchain_compile_options"]:
            pass
        elif kind == "target_toolchain" and option in mappings["target_toolchain_compile_options"].get(cpu, []):
            pass
        elif kind == "source_prefix_map" and option.split("=", 1)[0] in mappings["source_prefix_maps"]:
            pass
        else:
            raise ValueError(f"unclassified native compile option for {name}: {record}")
    compile_options.extend(mappings["bazel_only_compile_options"])
    link_options = []
    libraries = set()
    for record in native["link_profiles"][program["link_profile"]]:
        option, kind = record["argument"], record["classification"]
        if kind == "library" and option.startswith("-l") and option[2:] in mappings["library_providers"]:
            label = mappings["library_providers"][option[2:]]
            if not label.startswith("toolchain:"):
                libraries.add(label)
        elif kind == "literal":
            link_options.append(option)
        elif kind == "native_link_driver" and option in mappings["native_link_driver_options"]:
            pass
        elif kind == "target_native_link_driver" and option in mappings["target_toolchain_compile_options"].get(cpu, []):
            pass
        elif kind == "toolchain" and option in mappings["toolchain_link_options"]:
            pass
        elif kind == "source_prefix_map" and option.split("=", 1)[0] in mappings["source_prefix_maps"]:
            pass
        else:
            raise ValueError(f"unclassified native link option for {name}: {record}")
    return compile_options, link_options, local_includes, libraries, include_repositories


def target_compile_options(rule: dict[str, Any]) -> tuple[list[str], list[str]]:
    options = list_attribute(rule, "cxxopts")
    semantic = []
    includes = []
    index = 0
    while index < len(options):
        option = options[index]
        for prefix in ("-isystem", "-iquote", "-idirafter", "-I"):
            if option == prefix:
                index += 1
                if index == len(options):
                    raise ValueError(f"missing target include path for {rule['name']}")
                path = options[index]
            elif option.startswith(prefix) and len(option) > len(prefix):
                path = option[len(prefix):]
            else:
                continue
            if prefix != "-I":
                raise ValueError(f"unexpected target include class for {rule['name']}: {prefix}")
            includes.append(path)
            break
        else:
            semantic.append(option)
        index += 1
    return semantic, includes


def require_input_only_headers(rule: dict[str, Any]) -> None:
    if rule["kind"] != "cc_library":
        raise ValueError(f"local production headers must be a cc_library: {rule['name']}")
    for name in ("srcs", "deps", "copts", "cxxopts", "linkopts", "includes", "defines", "local_defines", "features"):
        if list_attribute(rule, name):
            raise ValueError(f"local production headers propagate {name}: {rule['name']}")
    for name in ("include_prefix", "strip_include_prefix"):
        if rule["attributes"].get(name, {}).get("stringValue", ""):
            raise ValueError(f"local production headers propagate {name}: {rule['name']}")
    package = rule["name"].split(":", 1)[0] + ":"
    if any(not label.startswith(package) for label in list_attribute(rule, "hdrs")):
        raise ValueError(f"local production headers leave their package: {rule['name']}")


def check_configured_rules(native: dict[str, Any], evidence: dict[str, Any], programs: dict[str, dict[str, Any]], architecture: str) -> list[dict[str, Any]]:
    mappings = native["mappings"]
    labels = program_labels(programs)
    if evidence.get("schema_version") != 1 or evidence.get("query") != cquery_expression(programs, mappings) or evidence.get("universe_scope") != list(labels.values()):
        raise ValueError("configured-rule evidence does not match the selected production query")
    repository_mapping = evidence["root_repository_mapping"]
    canonical = lambda label: canonical_label(label, repository_mapping)
    rules, configurations = rule_graph(evidence)
    local_headers = {canonical(label) for label in mappings["local_header_providers"]}
    rule_support = {canonical(label) for label in mappings["rule_support_providers"]}
    provider_labels = {label: canonical(label) for label in mapped_labels(mappings)}
    feature_sources = {canonical(label) for feature in mappings["feature_support"].values() for label in feature["source_labels"]}
    feature_dependencies = {canonical(label) for feature in mappings["feature_support"].values() for label in feature["dependency_labels"]}
    cpu, _, platform = ARCHITECTURES[architecture]
    checked = []
    for name, program in programs.items():
        label = canonical(labels[name])
        rule = rules.get(label)
        if rule is None or rule["kind"] != "cc_binary":
            raise ValueError(f"missing resolved production cc_binary: {label}")
        configuration = configurations[rule["configuration"]]
        options = {option["name"]: option.get("value") for fragment in configuration.get("fragmentOptions", []) for option in fragment.get("options", [])}
        if options.get("compilation_mode") != "opt" or options.get("platforms") != "[" + canonical(platform) + "]":
            raise ValueError(f"production rule is not in the selected release platform: {label}")
        if any(options.get(key) != "[]" for key in ("copt", "cxxopt", "linkopt", "per_file_copt", "features")):
            raise ValueError(f"production rule has unclassified command-line options: {label}")
        sources = set(list_attribute(rule, "srcs"))
        if sources & feature_sources:
            raise ValueError(f"feature support sources are selected in normal release: {label}")
        if sources != {canonical(source) for source in program["sources"]}:
            raise ValueError(f"resolved production sources differ from the generated map: {label}")
        for attribute in ("copts", "defines", "local_defines", "includes", "features"):
            if list_attribute(rule, attribute):
                raise ValueError(f"production rule uses unclassified {attribute}: {label}")
        expected_compile, expected_link, expected_includes, libraries, include_repositories = native_options(native, name, cpu)
        if expected_includes != program["local_include_directories"]:
            raise ValueError(f"native local includes differ from the generated map: {name}")
        actual_compile, actual_includes = target_compile_options(rule)
        if actual_includes != expected_includes:
            raise ValueError(f"resolved local include order differs from native: {label}")
        actual_normalized = normalize_compile_options(actual_compile)
        expected_normalized = normalize_compile_options(expected_compile)
        if actual_normalized != expected_normalized:
            differences = {}
            states = {
                key: {"expected": expected_normalized["states"].get(key), "actual": actual_normalized["states"].get(key)}
                for key in sorted(expected_normalized["states"].keys() | actual_normalized["states"].keys())
                if expected_normalized["states"].get(key) != actual_normalized["states"].get(key)
            }
            if states:
                differences["states"] = states
            if actual_normalized["ordered_literals"] != expected_normalized["ordered_literals"]:
                differences["ordered_literals"] = {"expected": expected_normalized["ordered_literals"], "actual": actual_normalized["ordered_literals"]}
            raise ValueError(f"resolved target cxxopts differ from native mapping: {label}; differences={json.dumps(differences, sort_keys=True)}")
        if list_attribute(rule, "linkopts") != expected_link:
            raise ValueError(f"resolved target linkopts differ from native mapping: {label}")
        system = mappings["implicit_system_header_providers"]
        implicit = set(system["all_programs"] + system["programs"].get(name, []))
        required = {canonical(value) for value in libraries | implicit}
        available = reachable(rules, label)
        missing = required - (available & rules.keys())
        if missing:
            raise ValueError(f"mapped dependencies are not reachable from {label}: {sorted(missing)}")
        for repository in include_repositories:
            candidates = {value for public, value in provider_labels.items() if public.startswith(repository + "//")}
            if not candidates & available & rules.keys():
                raise ValueError(f"native include repository is not reachable from {label}: {repository}")
        accepted_providers = set().union(*(reachable(rules, dependency) for dependency in required)) if required else set()
        accepted_providers &= set(provider_labels.values()) & rules.keys()
        dependencies = set(list_attribute(rule, "deps"))
        if dependencies & feature_dependencies:
            raise ValueError(f"feature support dependency is selected in normal release: {label}")
        unclassified = dependencies - local_headers - rule_support - accepted_providers
        if unclassified:
            raise ValueError(f"unclassified direct production dependencies for {label}: {sorted(unclassified)}")
        for header in dependencies & local_headers:
            if header not in rules:
                raise ValueError(f"missing local production header rule: {header}")
            require_input_only_headers(rules[header])
        checked.append({
            "name": name,
            "label": labels[name],
            "configuration_checksum": configuration["checksum"],
            "local_include_directories": expected_includes,
            "required_dependency_labels": sorted(libraries),
            "implicit_system_header_labels": sorted(implicit),
            "local_header_labels": sorted(dependencies & local_headers),
            "rule_support_labels": sorted(dependencies & rule_support),
        })
    return checked


def validate_provenance(native: dict[str, Any], receipt: dict[str, Any], module_graph: dict[str, Any], architecture: str, revision: str, source_map_sha256: str) -> None:
    cpu, _, platform = ARCHITECTURES[architecture]
    if native.get("schema_version") != 1 or native.get("mappings") != MODULE_BUILD_MAPPINGS:
        raise ValueError("native build contract uses a different schema or mapping")
    if native.get("git_revision") != revision or receipt.get("git_revision") != revision:
        raise ValueError("native contract, build receipt, and checkout revisions must match")
    if native.get("source_map_sha256") != source_map_sha256 or receipt.get("source_map_sha256") != source_map_sha256:
        raise ValueError("native contract, build receipt, and checkout source maps must match")
    if receipt.get("architecture") != architecture or receipt.get("target_platform") != platform or native.get("architecture") != {"execution_machine": cpu, "configured_target_cpus": [cpu]}:
        raise ValueError("native contract and build receipt must use the selected native architecture")
    reviewed = native["mappings"]["reviewed_infra"]
    versions = set()
    pending = [module_graph]
    while pending:
        node = pending.pop()
        if node.get("name") == reviewed["module"]:
            versions.add(node.get("version"))
        pending.extend(node.get("dependencies", []) + node.get("indirectDependencies", []))
    if versions != {reviewed["version"]}:
        raise ValueError(f"toolchain mappings require the reviewed infra version: {reviewed}")


def receipt_artifact(receipt_path: Path, receipt: dict[str, Any], key: str, supplied: Path | None = None) -> Path:
    record = receipt[key]
    path = receipt_path.parent / relative_path(record["artifact"])
    if supplied is not None and supplied.resolve() != path.resolve():
        raise ValueError(f"{key} input does not match the build receipt path")
    if sha256(path) != record["sha256"]:
        raise ValueError(f"{key} input does not match the build receipt hash")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", required=True, choices=sorted(ARCHITECTURES))
    parser.add_argument("--native-contract", required=True, type=Path)
    parser.add_argument("--configured-rules", required=True, type=Path)
    parser.add_argument("--build-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    receipt = json.loads(args.build_receipt.read_text())
    native_path = receipt_artifact(args.build_receipt, receipt, "native_contract", args.native_contract)
    native = json.loads(native_path.read_text())
    rules_path = receipt_artifact(args.build_receipt, receipt, "configured_rules", args.configured_rules)
    module_path = receipt_artifact(args.build_receipt, receipt, "module_graph")
    evidence = json.loads(rules_path.read_text())
    module_graph = json.loads(module_path.read_text())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source_map_sha256 = sha256(SOURCE_MAP)
    validate_provenance(native, receipt, module_graph, args.architecture, revision, source_map_sha256)
    programs = load_programs()
    if set(native["programs"]) != set(programs):
        raise ValueError("native contract program set differs from the generated source map")
    expected_labels = program_labels(programs)
    if {item["name"]: item["label"] for item in receipt["programs"]} != expected_labels:
        raise ValueError("build receipt program set differs from the generated source map")
    checked = check_configured_rules(native, evidence, programs, args.architecture)
    result = {
        "schema_version": 1,
        "status": "passed",
        "verification_scope": "Configured normal-release production cc_binary target sources, local include order, cxxopts, linkopts, and mapped dependency-label reachability.",
        "unverified_scope": UNVERIFIED_SCOPE,
        "intentional_differences": INTENTIONAL_DIFFERENCES,
        "git_revision": revision,
        "source_map_sha256": source_map_sha256,
        "architecture": args.architecture,
        "reviewed_infra": native["mappings"]["reviewed_infra"],
        "inputs": {
            key: {"artifact": path.name, "sha256": sha256(path)}
            for key, path in {
                "native_contract": native_path,
                "configured_rules": rules_path,
                "build_receipt": args.build_receipt,
                "module_graph": module_path,
            }.items()
        },
        "programs": checked,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

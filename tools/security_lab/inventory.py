from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from .git import GitError, GitRepository
from .upstream_ledger import _gitmodules, _toolchain_snapshot


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = ROOT / "security" / "dependency-policy.json"
DEFAULT_OUTPUT = ROOT / "security" / "generated" / "dependency-inventory.json"
DEFAULT_SPDX_OUTPUT = ROOT / "security" / "generated" / "sbom.spdx.json"
DEFAULT_LICENSES = ROOT / "security" / "dependency-licenses.json"


def _canonical(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _cargo_packages(repo: GitRepository, ref: str) -> list[dict[str, Any]]:
    text = repo.show_text(ref, "native/src/Cargo.lock")
    if not text:
        return []
    lock = tomllib.loads(text)
    result: list[dict[str, Any]] = []
    for package in lock.get("package", []):
        source = package.get("source")
        if source is None and package.get("name") != "cxx":
            continue
        result.append(
            {
                "ecosystem": "cargo",
                "name": str(package["name"]),
                "version": str(package["version"]),
                "source": source or "vendored-submodule",
                "checksum": package.get("checksum"),
                "direct": False,
            }
        )
    direct_text = repo.show_text(ref, "native/src/Cargo.toml")
    direct_names: set[str] = set()
    if direct_text:
        root = tomllib.loads(direct_text)
        direct_names.update(root.get("workspace", {}).get("dependencies", {}).keys())
    for package in result:
        package["direct"] = package["name"] in direct_names
    return sorted(result, key=lambda item: (item["name"], item["version"], str(item["source"])))


def _resolved_gradle_packages(text: str) -> list[dict[str, Any]]:
    packages: dict[tuple[str, str], dict[str, Any]] = {}
    pattern = re.compile(
        r"(?:---|\\---)\s+([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([^\s()]+)(?:\s+->\s+([^\s()]+))?"
    )
    for match in pattern.finditer(text):
        group, name, requested, selected = match.groups()
        version = selected or requested
        if version in {"FAILED", "unspecified"} or version.startswith("project"):
            continue
        key = (f"{group}:{name}", version)
        packages[key] = {
            "ecosystem": "gradle",
            "name": key[0],
            "version": version,
            "source": "resolved-debug-runtime-classpath",
            "checksum": None,
            "direct": False,
            "requested_version": requested if selected else None,
        }
    return sorted(packages.values(), key=lambda item: (item["name"], item["version"]))


def _gradle_catalog(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    catalog = tomllib.loads(text)
    versions = catalog.get("versions", {})
    result = []
    for _alias, value in catalog.get("libraries", {}).items():
        if isinstance(value, str):
            parts = value.split(":")
            if len(parts) < 3:
                continue
            module = ":".join(parts[:2])
            version = parts[2]
        else:
            module = value.get("module")
            if not module and value.get("group") and value.get("name"):
                module = f"{value['group']}:{value['name']}"
            version_value = value.get("version")
            if isinstance(version_value, dict):
                version = versions.get(version_value.get("ref"))
            elif version_value is not None:
                version = version_value
            else:
                version = versions.get(value.get("version.ref"))
        if module and version:
            result.append(
                {
                    "ecosystem": "gradle",
                    "name": str(module),
                    "version": str(version),
                    "source": "version-catalog",
                    "checksum": None,
                    "direct": True,
                }
            )
    return result


def _declared_gradle_packages(repo: GitRepository, ref: str) -> list[dict[str, Any]]:
    packages: dict[tuple[str, str], dict[str, Any]] = {}
    catalog = repo.show_text(ref, "app/gradle/libs.versions.toml")
    for package in _gradle_catalog(catalog):
        packages[(package["name"], package["version"])] = package

    files = [path for path in repo.list_files(ref) if path.endswith(".gradle.kts")]
    for path in files:
        text = repo.show_text(ref, path)
        variables = dict(re.findall(r'val\s+(\w+)\s*=\s*"([^"]+)"', text))
        for coordinate in re.findall(
            r'(?:implementation|api|kapt|ksp|testImplementation|classpath)\("([^"]+)"\)',
            text,
        ):
            for variable, version in variables.items():
                coordinate = coordinate.replace(f"${{{variable}}}", version)
            parts = coordinate.split(":")
            if len(parts) != 3 or "$" in coordinate:
                continue
            name = ":".join(parts[:2])
            package = {
                "ecosystem": "gradle",
                "name": name,
                "version": parts[2],
                "source": path,
                "checksum": None,
                "direct": True,
            }
            packages[(name, parts[2])] = package
    return sorted(packages.values(), key=lambda item: (item["name"], item["version"]))


def _merge_gradle_packages(
    declared: Iterable[dict[str, Any]],
    resolved: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep selected runtime versions plus declared build/test-only coordinates."""
    declared = list(declared)
    resolved = [dict(item) for item in resolved]
    direct_names = {item["name"] for item in declared}
    resolved_names = {item["name"] for item in resolved}
    for item in resolved:
        item["direct"] = item["name"] in direct_names
    # A resolved name supersedes its requested declaration even when conflict
    # resolution selected another version. Declarations absent from this
    # runtime configuration (plugins, kapt/codegen, tests) remain inventoried.
    merged = resolved + [item for item in declared if item["name"] not in resolved_names]
    return sorted(merged, key=lambda item: (item["name"], item["version"]))


def _actions(repo: GitRepository, ref: str) -> list[dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for path in repo.list_files(ref):
        if not path.startswith(".github/") or not path.endswith((".yml", ".yaml")):
            continue
        text = repo.show_text(ref, path)
        for action, version in re.findall(r"uses:\s*([^\s@]+)@([^\s#]+)", text):
            if action.startswith("./"):
                continue
            result[(action, version)] = {
                "ecosystem": "action",
                "name": action,
                "version": version,
                "source": path,
                "checksum": version if re.fullmatch(r"[a-f0-9]{40}", version) else None,
                "direct": True,
            }
    return sorted(result.values(), key=lambda item: (item["name"], item["version"]))


def _submodules(repo: GitRepository, ref: str) -> list[dict[str, Any]]:
    urls = _gitmodules(repo.show_text(ref, ".gitmodules"))
    return [
        {
            "ecosystem": "submodule",
            "name": path,
            "version": commit,
            "source": urls.get(path) or "NOASSERTION",
            "checksum": commit,
            "direct": True,
        }
        for path, commit in repo.submodule_entries(ref)
    ]


def _snapshot(
    repo: GitRepository,
    ref: str,
    *,
    gradle_report: str | None = None,
) -> dict[str, Any]:
    packages = _cargo_packages(repo, ref)
    declared_gradle = _declared_gradle_packages(repo, ref)
    if gradle_report:
        packages.extend(
            _merge_gradle_packages(
                declared_gradle,
                _resolved_gradle_packages(gradle_report),
            )
        )
    else:
        packages.extend(declared_gradle)
    packages.extend(_actions(repo, ref))
    packages.extend(_submodules(repo, ref))
    return {
        "ref": ref,
        "commit": repo.resolve(ref),
        "toolchain": _toolchain_snapshot(repo, repo.resolve(ref)),
        "packages": sorted(
            packages,
            key=lambda item: (item["ecosystem"], item["name"], item["version"]),
        ),
    }


def _key(package: Mapping[str, Any]) -> str:
    return f"{package['ecosystem']}:{package['name']}"


def _comparison(
    current: Mapping[str, Any],
    stable: Mapping[str, Any],
    master: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    def versions(snapshot: Mapping[str, Any]) -> dict[str, list[str]]:
        grouped: defaultdict[str, set[str]] = defaultdict(set)
        for package in snapshot["packages"]:
            grouped[_key(package)].add(str(package["version"]))
        return {key: sorted(value) for key, value in grouped.items()}

    current_versions = versions(current)
    stable_versions = versions(stable)
    master_versions = versions(master)
    overrides = policy.get("overrides", {})
    defaults = policy["default_decisions"]
    result = []
    for key in sorted(set(current_versions) | set(stable_versions) | set(master_versions)):
        if key in overrides:
            decision = overrides[key]["decision"]
            reason = overrides[key]["reason"]
        elif key not in current_versions:
            decision = defaults["stable_only"]
            reason = "Dependency is introduced by the selected official stable or observed master."
        elif key not in stable_versions:
            decision = defaults["fork_only"]
            reason = "Dependency is fork-only relative to the selected stable."
        elif current_versions[key] == stable_versions[key]:
            decision = defaults["same_as_stable"]
            reason = "Current and official stable resolve the same declared version."
        else:
            decision = defaults["different_from_stable"]
            reason = "Version differs from the official stable; inherit the coherent baseline first."
        if stable_versions.get(key) != master_versions.get(key) and key not in overrides:
            reason += " Master differs from stable and remains observation-only."
        result.append(
            {
                "key": key,
                "current": current_versions.get(key, []),
                "stable": stable_versions.get(key, []),
                "master": master_versions.get(key, []),
                "decision": decision,
                "reason": reason,
            }
        )
    return result


def _license_for(
    package: Mapping[str, Any],
    policy: Mapping[str, Any],
    licenses: Mapping[str, Any],
) -> tuple[str, str | None]:
    exact = f"{_key(package)}@{package['version']}"
    entry = licenses.get("packages", {}).get(exact)
    if entry and entry.get("license"):
        return str(entry["license"]), entry.get("evidence")
    hint = policy.get("license_hints", {}).get(_key(package))
    return (str(hint), "dependency-policy.json") if hint else ("NOASSERTION", None)


def build_inventory(
    repo: GitRepository,
    policy: Mapping[str, Any],
    *,
    baseline_ref: str,
    stable_ref: str,
    master_ref: str,
    gradle_report: str | None,
    licenses: Mapping[str, Any],
) -> dict[str, Any]:
    current = _snapshot(repo, baseline_ref, gradle_report=gradle_report)
    stable = _snapshot(repo, stable_ref)
    master = _snapshot(repo, master_ref)
    for package in current["packages"]:
        package["license"], package["license_evidence"] = _license_for(
            package, policy, licenses
        )
        package["license_status"] = (
            "declared" if package["license"] != "NOASSERTION" else "requires_resolution"
        )
    unresolved = [
        _key(package) + "@" + package["version"]
        for package in current["packages"]
        if package["license"] == "NOASSERTION"
    ]
    return {
        "schema_version": 1,
        "baseline": current,
        "official_stable": stable,
        "observed_master": master,
        "comparison": _comparison(current, stable, master, policy),
        "license_coverage": {
            "package_count": len(current["packages"]),
            "declared_count": len(current["packages"]) - len(unresolved),
            "unresolved": sorted(unresolved),
            "policy": "NOASSERTION is a blocking inventory gap, never an inferred license.",
        },
        "reproducibility": {
            "gradle_runtime_resolution_supplied": gradle_report is not None,
            "gradle_locking_present": any(
                path.endswith("gradle.lockfile") for path in repo.list_files(baseline_ref)
            ),
            "cargo_lock_present": bool(repo.show_text(baseline_ref, "native/src/Cargo.lock")),
            "submodule_pins_immutable": True,
        },
    }


def _spdx_id(package: Mapping[str, Any], index: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]", "-", f"{package['ecosystem']}-{package['name']}")
    return f"SPDXRef-Package-{safe}-{index}"


def build_spdx(inventory: Mapping[str, Any]) -> dict[str, Any]:
    baseline = inventory["baseline"]
    packages = []
    for index, package in enumerate(baseline["packages"], start=1):
        ecosystem = package["ecosystem"]
        name = package["name"]
        version = package["version"]
        purl_type = {"cargo": "cargo", "gradle": "maven"}.get(ecosystem)
        external_refs = []
        if purl_type:
            if ecosystem == "gradle":
                group, artifact = name.split(":", 1)
                purl_name = f"{quote(group, safe='.')}/{quote(artifact, safe='')}"
            else:
                purl_name = quote(name, safe="")
            external_refs.append(
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:{purl_type}/{purl_name}@{quote(version, safe='')}",
                }
            )
        packages.append(
            {
                "SPDXID": _spdx_id(package, index),
                "name": f"{ecosystem}:{name}",
                "versionInfo": version,
                "downloadLocation": package["source"] if "://" in str(package["source"]) else "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": package["license"],
                "licenseDeclared": package["license"],
                "copyrightText": "NOASSERTION",
                "externalRefs": external_refs,
                "checksums": (
                    [{"algorithm": "SHA256", "checksumValue": package["checksum"]}]
                    if package.get("checksum") and re.fullmatch(r"[a-fA-F0-9]{64}", package["checksum"])
                    else []
                ),
            }
        )
    digest = hashlib.sha256(str(baseline["commit"]).encode()).hexdigest()
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"KitsuneMagisk-{str(baseline['commit'])[:12]}",
        "documentNamespace": f"https://github.com/Jordan231111/KitsuneMagisk/sbom/{digest}",
        "creationInfo": {
            "creators": ["Tool: tools.security_lab.inventory-v1"],
            "created": "2026-07-22T00:00:00Z"
        },
        "packages": packages,
        "documentDescribes": [package["SPDXID"] for package in packages],
    }


def _write_or_check(path: Path, content: str, check: bool) -> None:
    if check:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            raise ValueError(f"{path} is stale; regenerate the dependency inventory")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate dependency/toolchain inventory and SPDX")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--spdx-output", type=Path, default=DEFAULT_SPDX_OUTPUT)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--stable-ref")
    parser.add_argument("--master-ref")
    parser.add_argument("--gradle-report", type=Path)
    parser.add_argument("--licenses", type=Path, default=DEFAULT_LICENSES)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        policy = _load_json(args.policy)
        licenses = _load_json(args.licenses) if args.licenses.exists() else {"packages": {}}
        report = args.gradle_report.read_text(encoding="utf-8") if args.gradle_report else None
        inventory = build_inventory(
            GitRepository(args.repo.resolve()),
            policy,
            baseline_ref=args.baseline_ref or policy["baseline_ref"],
            stable_ref=args.stable_ref or policy["stable_ref"],
            master_ref=args.master_ref or policy["master_ref"],
            gradle_report=report,
            licenses=licenses,
        )
        _write_or_check(args.output, _canonical(inventory), args.check)
        _write_or_check(args.spdx_output, _canonical(build_spdx(inventory)), args.check)
        if inventory["license_coverage"]["unresolved"]:
            print(
                f"warning: {len(inventory['license_coverage']['unresolved'])} package licenses remain NOASSERTION",
                file=sys.stderr,
            )
    except (OSError, ValueError, KeyError, GitError, tomllib.TOMLDecodeError) as exc:
        print(f"dependency inventory failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

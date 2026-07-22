from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Iterable

from .git import GitRepository
from .inventory import (
    DEFAULT_POLICY,
    ROOT,
    _canonical,
    _declared_gradle_packages,
    _load_json,
    _merge_gradle_packages,
    _resolved_gradle_packages,
)


DEFAULT_OUTPUT = ROOT / "security" / "dependency-licenses.json"


LICENSE_ALIASES = {
    "apache license, version 2.0": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "the apache software license, version 2.0": "Apache-2.0",
    "the apache license, version 2.0": "Apache-2.0",
    "bouncy castle licence": "MIT",
    "bsd license": "BSD-3-Clause",
    "eclipse public license 1.0": "EPL-1.0",
    "eclipse public license - v 1.0": "EPL-1.0",
    "eclipse public license - v 2.0": "EPL-2.0",
    "mit license": "MIT",
    "mit": "MIT",
    "gnu lesser general public license": "LGPL-2.1-or-later",
    "gnu lesser general public license, version 2.1": "LGPL-2.1-only",
}


def _cargo_licenses(root: Path) -> dict[str, dict[str, Any]]:
    command = [
        "cargo",
        "metadata",
        "--format-version=1",
        "--locked",
        "--manifest-path",
        str(root / "native" / "src" / "Cargo.toml"),
    ]
    proc = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"cargo metadata failed: {proc.stderr.strip()}")
    metadata = json.loads(proc.stdout)
    result = {}
    for package in metadata["packages"]:
        source = package.get("source")
        if source is None and package["name"] != "cxx":
            continue
        key = f"cargo:{package['name']}@{package['version']}"
        manifest = Path(package["manifest_path"])
        try:
            evidence = str(manifest.relative_to(root))
        except ValueError:
            evidence = package.get("repository") or source or "cargo-metadata"
        result[key] = {
            "license": package.get("license") or "NOASSERTION",
            "evidence": evidence,
            "source": "cargo-metadata",
        }
    return result


def _pom_urls(name: str, version: str) -> Iterable[str]:
    group, artifact = name.split(":", 1)
    path = f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.pom"
    yield f"https://repo.maven.apache.org/maven2/{path}"
    yield f"https://dl.google.com/dl/android/maven2/{path}"
    yield f"https://jitpack.io/{path}"


def _map_license(names: list[str]) -> str:
    mapped = []
    for name in names:
        normalized = re.sub(r"\s+", " ", name.strip().lower())
        license_id = LICENSE_ALIASES.get(normalized)
        if license_id:
            mapped.append(license_id)
    return " AND ".join(sorted(set(mapped))) if mapped else "NOASSERTION"


def _fetch_gradle_license(name: str, version: str) -> dict[str, Any]:
    errors = []
    for url in _pom_urls(name, version):
        request = urllib.request.Request(url, headers={"User-Agent": "KitsuneMagisk-SBOM/1"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read(2 * 1024 * 1024)
        except (OSError, urllib.error.HTTPError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            errors.append(f"{url}: invalid XML: {exc}")
            continue
        names = []
        urls = []
        for element in root.iter():
            local = element.tag.rsplit("}", 1)[-1]
            if local == "license":
                for child in element:
                    child_local = child.tag.rsplit("}", 1)[-1]
                    if child_local == "name" and child.text:
                        names.append(child.text)
                    elif child_local == "url" and child.text:
                        urls.append(child.text)
        return {
            "license": _map_license(names),
            "evidence": url,
            "source": "maven-pom",
            "declared_names": sorted(set(names)),
            "declared_urls": sorted(set(urls)),
        }
    return {
        "license": "NOASSERTION",
        "evidence": None,
        "source": "maven-pom-unavailable",
        "errors": errors[-3:],
    }


def build_license_snapshot(
    root: Path,
    gradle_report: str | None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    packages = _cargo_licenses(root)
    declared = (
        _declared_gradle_packages(GitRepository(root), str(policy["baseline_ref"]))
        if policy
        else []
    )
    resolved = _resolved_gradle_packages(gradle_report) if gradle_report else []
    for package in _merge_gradle_packages(declared, resolved):
        key = f"gradle:{package['name']}@{package['version']}"
        packages[key] = _fetch_gradle_license(package["name"], package["version"])
    if policy:
        hints = policy.get("license_hints", {})
        evidence = policy.get("license_evidence", {})
        for key, package in packages.items():
            if package["license"] != "NOASSERTION":
                continue
            hint_key = key.rsplit("@", 1)[0]
            if hint_key not in hints:
                continue
            package.update(
                {
                    "license": hints[hint_key],
                    "evidence": evidence.get(hint_key, "security/dependency-policy.json"),
                    "source": "reviewed-policy",
                }
            )
    return {
        "schema_version": 1,
        "packages": dict(sorted(packages.items())),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve reproducible dependency license evidence")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--gradle-report", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = args.gradle_report.read_text(encoding="utf-8") if args.gradle_report else None
        policy = _load_json(args.policy)
        snapshot = build_license_snapshot(args.repo.resolve(), report, policy)
        payload = _canonical(snapshot)
        if args.check:
            if not args.output.exists() or args.output.read_text(encoding="utf-8") != payload:
                raise ValueError(f"{args.output} is stale")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        unresolved = [
            key for key, value in snapshot["packages"].items() if value["license"] == "NOASSERTION"
        ]
        if unresolved:
            print(f"warning: unresolved licenses: {', '.join(unresolved)}", file=sys.stderr)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"license inventory failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

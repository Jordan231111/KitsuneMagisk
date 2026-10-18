#!/usr/bin/env python3
"""Bind release eligibility to the tested source, APKs and retained evidence."""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import re
import subprocess
import uuid

try:
    from tools import build_info, device_qualification, next_system_baseline as baseline, port_ledger
except ImportError:
    import build_info
    import device_qualification
    import next_system_baseline as baseline
    import port_ledger

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/next-system-pr16.json"
CHECKS = {"host", "clippy", "lint", "build-debug", "build-release"}
LOCKFILES = ("native/src/Cargo.lock", "tools/elf-cleaner/Cargo.lock", "tools/rustup-wrapper/Cargo.lock")


def read_evidence(entry: dict, directory: Path) -> bytes:
    if set(entry) != {"path", "sha256"} or not isinstance(entry["path"], str):
        raise ValueError("Evidence needs a relative path and SHA-256")
    relative = Path(entry["path"])
    file = (directory / relative).resolve()
    if relative.is_absolute() or not file.is_relative_to(directory.resolve()) or not file.is_file():
        raise ValueError("Evidence is missing or outside its directory")
    if baseline.sha256(file) != entry["sha256"]:
        raise ValueError(f"Evidence bytes changed: {relative}")
    return file.read_bytes()


def verify_acceptance(acceptance: dict, directory: Path, info: dict,
                      artifacts: dict, policy: dict) -> None:
    if acceptance.get("schema") != 1 or acceptance.get("source_commit") != info["source_commit"]:
        raise ValueError("Acceptance must identify this exact source commit")
    checks = acceptance.get("checks", {})
    if set(checks) != CHECKS:
        raise ValueError("Candidate build, host, Clippy and lint results are required")
    for name, result in checks.items():
        if type(result.get("exit_code")) is not int or result["exit_code"] != 0 or not result.get("command"):
            raise ValueError(f"Candidate check failed or was not run: {name}")
        if not read_evidence(result["log"], directory).strip():
            raise ValueError(f"Candidate check has no output: {name}")
    runtime = acceptance.get("runtime", {})
    if set(runtime) != set(policy["runtime"]):
        raise ValueError("Runtime results do not cover the candidate matrix")
    for name, entry in runtime.items():
        record = json.loads(read_evidence(entry, directory))
        expected = policy["runtime"][name]
        variant = expected["variant"]
        if record.get("schema") != device_qualification.SCHEMA:
            raise ValueError(f"Invalid runtime record: {name}")
        if (record["artifact"]["sha256"] != artifacts[variant]["sha256"] or
                record["artifact"]["build"] != build_info.variant_info(info, variant)):
            raise ValueError(f"Runtime record tested different bytes: {name}")
        for field, value in expected["guest"].items():
            if record["guest"].get(field) != value:
                raise ValueError(f"Runtime target differs from the release matrix: {name}/{field}")
        boots = record["boots"]
        if (len(boots) < expected["cold_boots"] or len({b["id"] for b in boots}) != len(boots)
                or boots[0]["phase"] != "setup" or boots[-1]["phase"] != "accepted"):
            raise ValueError(f"Runtime record lacks accepted distinct cold boots: {name}")
        for boot in boots:
            uuid.UUID(boot["id"])
    lifecycle = json.loads(read_evidence(acceptance["mumu_lifecycle"], directory))
    if (lifecycle.get("source_commit") != info["source_commit"] or
            lifecycle.get("apk_sha256") != artifacts["debug"]["sha256"] or
            any(lifecycle.get(key) is not True for key in
                ("suite_passed", "upgrade_preserved_data", "vendor_root_disabled",
                 "uninstall_passed", "original_disks_restored", "original_settings_restored"))):
        raise ValueError("MuMu installation, upgrade, uninstall and restoration must pass")


def verify_audits(acceptance: dict, directory: Path, root: Path = ROOT) -> None:
    audits = acceptance.get("audits", {})
    if set(audits) != set(LOCKFILES):
        raise ValueError("Every owned Rust lockfile needs an advisory audit")
    for lockfile, entry in audits.items():
        if entry["lock_sha256"] != baseline.sha256(root / lockfile):
            raise ValueError(f"Dependency audit predates the lockfile: {lockfile}")
        report = json.loads(read_evidence(entry["report"], directory))
        vulnerabilities = report["vulnerabilities"]
        findings = vulnerabilities["list"]
        if (type(vulnerabilities["count"]) is not int or vulnerabilities["count"] != len(findings)
                or vulnerabilities["found"] is not bool(findings)):
            raise ValueError("Inconsistent dependency audit")
        for finding in findings:
            if (lockfile != LOCKFILES[0] or finding["advisory"]["id"] != "RUSTSEC-2023-0071"
                    or finding["package"]["name"] != "rsa"):
                raise ValueError(f"Unreviewed security advisory in {lockfile}")
        if any(report.get("warnings", {}).values()):
            raise ValueError(f"Unreviewed dependency warning in {lockfile}")
        checked = datetime.datetime.fromisoformat(entry["checked_at"])
        age = datetime.datetime.now(datetime.timezone.utc) - checked
        if not datetime.timedelta(0) <= age <= datetime.timedelta(days=7):
            raise ValueError("Dependency advisories must be rechecked within seven days")


def verify_ci(run_id: int, info: dict, repository: str) -> dict:
    def get(endpoint: str) -> dict:
        return json.loads(subprocess.check_output(["gh", "api", f"repos/{repository}/{endpoint}"],
                                                 cwd=ROOT, text=True))
    run = get(f"actions/runs/{run_id}")
    if run["event"] != "pull_request" or run["conclusion"] != "success" or run["status"] != "completed":
        raise ValueError("A completed passing pull-request matrix is required")
    if run["path"] != ".github/workflows/build.yml":
        raise ValueError("CI result is not the product build workflow")
    if run["head_sha"] != info["source_commit"]:
        parents = baseline.run(("git", "show", "-s", "--format=%P", run["head_sha"])).split()
        if info["source_commit"] not in parents:
            raise ValueError("CI did not test the candidate source")
        # A test merge may add unrelated base changes. Require the same tree.
        baseline.run(("git", "diff", "--exit-code", info["source_commit"], run["head_sha"], "--"))
    jobs = get(f"actions/runs/{run_id}/jobs?per_page=100")
    if jobs["total_count"] != len(jobs["jobs"]) or not jobs["jobs"]:
        raise ValueError("Incomplete CI job list")
    if any(job["conclusion"] != "success" for job in jobs["jobs"]):
        raise ValueError("The complete CI matrix must pass without skipped jobs")
    if not any(job["name"] == "Product gate" for job in jobs["jobs"]):
        raise ValueError("The aggregate product gate is missing")
    return {"url": run["html_url"], "head_sha": run["head_sha"],
            "jobs": [{"name": j["name"], "conclusion": j["conclusion"]} for j in jobs["jobs"]]}


def verify_receipt(receipt: dict, info: dict, apk: Path, stub: Path, channel: str) -> None:
    if (receipt.get("schema") != "kitsune.release.v1" or receipt.get("source") != info
            or info["source_dirty"] is not False):
        raise ValueError("Release receipt does not match the clean candidate source")
    if channel not in receipt.get("eligible_channels", []):
        raise ValueError(f"Candidate is not qualified for {channel}")
    if channel == "stable" and (receipt.get("production_blockers") or
            receipt.get("historical_gate", {}).get("passed") is not True or
            not re.fullmatch(r"\d+\.\d+\.\d+", info["product_version"])):
        raise ValueError("Stable promotion still has unresolved gates")
    for name, path in (("release", apk), ("stub_release", stub)):
        if baseline.sha256(path) != receipt["artifacts"][name]["sha256"]:
            raise ValueError("Publication bytes differ from the tested candidate")
    checked = datetime.datetime.fromisoformat(receipt["verified_at"])
    age = datetime.datetime.now(datetime.timezone.utc) - checked
    if not datetime.timedelta(0) <= age <= datetime.timedelta(days=7):
        raise ValueError("Release qualification must be refreshed within seven days")


def verify(args) -> None:
    info = build_info.identity()
    if info["source_dirty"]:
        raise ValueError("Commit the reviewed source before qualifying release bytes")
    manifest = baseline.read_manifest(MANIFEST)
    source = baseline.verify_source(ROOT, manifest)
    artifacts = baseline.verify_artifacts(ROOT, args.artifacts, manifest,
                                         baseline.find_build_tool("aapt", None),
                                         baseline.find_build_tool("apksigner", None))
    trust = json.loads((ROOT / "updates/trust.json").read_text())
    if artifacts["release"]["certificate_sha256"] != trust["certificate_sha256"]:
        raise ValueError("Distribution requires the production certificate")
    acceptance = json.loads(args.acceptance.read_text())
    policy = json.loads((ROOT / "docs/release-policy.json").read_text())
    verify_acceptance(acceptance, args.acceptance.parent, info, artifacts, policy)
    verify_audits(acceptance, args.acceptance.parent)
    ci = verify_ci(acceptance["ci_run"], info, trust["repository"])
    ledger = port_ledger.audit(ROOT, port_ledger.read_json(ROOT / "docs/port-ledger.json"), release=True)
    blockers = policy["production_blockers"]
    eligible = ["beta", "canary"]
    if not blockers and ledger["passed"] and re.fullmatch(r"\d+\.\d+\.\d+", info["product_version"]):
        eligible.append("stable")
    report = {"schema": "kitsune.release.v1", "source": info, "artifacts": artifacts,
              "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "acceptance_sha256": baseline.sha256(args.acceptance), "ci": ci,
              "source_contract": source, "historical_gate": {k: v for k, v in ledger.items() if k != "history"},
              "production_blockers": blockers, "eligible_channels": eligible}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build_info.write_if_changed(args.output, json.dumps(report, indent=2) + "\n")
    print(f"Qualified {info['release_tag']} for {', '.join(eligible)}; receipt: {args.output}")


def inventory(args) -> None:
    info = build_info.identity()
    components = {}
    for lock in LOCKFILES:
        manifest = ROOT / lock.replace("Cargo.lock", "Cargo.toml")
        raw = subprocess.check_output([str(ROOT / "scripts/env.py"), "cargo", "metadata", "--locked",
                                       "--format-version", "1", "--manifest-path", str(manifest)], cwd=ROOT)
        for package in json.loads(raw)["packages"]:
            reference = f"pkg:cargo/{package['name']}@{package['version']}"
            component = {"type": "library", "bom-ref": reference,
                         "name": package["name"], "version": package["version"]}
            if package["source"] and package["source"].startswith("registry+"):
                component["purl"] = reference
            elif package["source"]:
                component["externalReferences"] = [{"type": "vcs", "url": package["source"].removeprefix("git+")}]
            if package["license"]:
                component["licenses"] = [{"expression": package["license"]}]
            components[reference] = component
    report = {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
              "metadata": {"component": {"type": "application", "name": info["product_name"],
                                         "version": info["version_name"]},
                           "properties": [{"name": "kitsune:source", "value": info["source_commit"]},
                                          {"name": "kitsune:scope", "value": "Rust workspaces and build tools"}]},
              "components": [components[k] for k in sorted(components)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Wrote {len(components)} Rust components to {args.output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--acceptance", type=Path, required=True)
    check.add_argument("--artifacts", type=Path, default=ROOT / "out")
    check.add_argument("--output", type=Path, default=ROOT / "out/release-info.json")
    check.set_defaults(run=verify)
    sbom = commands.add_parser("inventory")
    sbom.add_argument("--output", type=Path, default=ROOT / "out/rust-sbom.cdx.json")
    sbom.set_defaults(run=inventory)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()

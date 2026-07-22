from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = ROOT / "security" / "rustsec-dispositions.json"
DEFAULT_LOCK = ROOT / "native" / "src" / "Cargo.lock"
DEFAULT_OUTPUT = ROOT / "security" / "generated" / "rustsec-report.json"


def _canonical(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def run_audit(lock: Path) -> dict[str, Any]:
    command = ["cargo", "audit", "--file", str(lock), "--json"]
    proc = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"cargo audit failed ({proc.returncode}): {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cargo audit emitted invalid JSON: {exc}") from exc


def _observed(audit: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in audit.get("vulnerabilities", {}).get("list", []):
        result.append(
            {
                "id": item["advisory"]["id"],
                "kind": "vulnerability",
                "package": item["package"]["name"],
                "version": item["package"]["version"],
                "title": item["advisory"]["title"],
            }
        )
    for kind, items in audit.get("warnings", {}).items():
        for item in items:
            result.append(
                {
                    "id": item["advisory"]["id"],
                    "kind": kind,
                    "package": item["package"]["name"],
                    "version": item["package"]["version"],
                    "title": item["advisory"]["title"],
                }
            )
    return sorted(result, key=lambda value: (value["id"], value["package"], value["version"]))


def _reachability(root: Path) -> dict[str, Any]:
    product_files = [
        path
        for path in (root / "native" / "src").rglob("*")
        if path.is_file() and "external" not in path.parts and path.suffix in {".rs", ".cpp", ".hpp"}
    ]
    let_cxx_string = []
    thread_rng = []
    for path in product_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "let_cxx_string" in text:
            let_cxx_string.append(str(path.relative_to(root)))
        if "thread_rng" in text or "rand::rng" in text:
            thread_rng.append(str(path.relative_to(root)))
    sign_source = (root / "native" / "src" / "boot" / "sign.rs").read_text(
        encoding="utf-8", errors="replace"
    )
    return {
        "cxx_let_cxx_string_product_uses": sorted(let_cxx_string),
        "rand_thread_rng_product_uses": sorted(thread_rng),
        "rsa_private_signing_location": (
            "native/src/boot/sign.rs" if "RsaPrivateKey" in sign_source else None
        ),
        "panic_profile": "abort",
    }


def build_report(
    audit: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    root: Path,
) -> tuple[dict[str, Any], list[str]]:
    if policy.get("audit_ignore_allowed") is not False:
        raise ValueError("RustSec policy must explicitly forbid audit ignores")
    observed = _observed(audit)
    dispositions = policy.get("advisories", {})
    errors = []
    records = []
    seen = set()
    for finding in observed:
        advisory_id = finding["id"]
        seen.add(advisory_id)
        disposition = dispositions.get(advisory_id)
        if disposition is None:
            errors.append(f"undisposed advisory {advisory_id} ({finding['package']})")
            records.append({**finding, "disposition": None})
            continue
        if disposition["package"] != finding["package"]:
            errors.append(
                f"{advisory_id} policy package {disposition['package']} != {finding['package']}"
            )
        records.append({**finding, "disposition": disposition})
    for advisory_id, disposition in dispositions.items():
        if advisory_id not in seen:
            records.append(
                {
                    "id": advisory_id,
                    "kind": disposition["kind"],
                    "package": disposition["package"],
                    "version": None,
                    "title": None,
                    "present": False,
                    "disposition": disposition,
                }
            )
    database = audit.get("database", {})
    report = {
        "schema_version": 1,
        "audit_command": "cargo audit --file native/src/Cargo.lock --json",
        "audit_ignore_used": False,
        "database": {
            "advisory_count": database.get("advisory-count"),
            "commit": database.get("last-commit"),
        },
        "lockfile_dependency_count": audit.get("lockfile", {}).get("dependency-count"),
        "finding_count": len(observed),
        "findings": sorted(records, key=lambda value: value["id"]),
        "reachability": _reachability(root),
        "validation": {
            "unknown_advisories": errors,
            "all_observed_findings_have_dispositions": not errors,
        },
    }
    return report, errors


def _write_or_check(path: Path, content: str, check: bool) -> None:
    if check:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            raise ValueError(f"{path} is stale; regenerate the RustSec report")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate cargo-audit against reviewed dispositions")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--lockfile", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--audit-json", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--shipping-current",
        action="store_true",
        help="also fail on known fixed advisories held only because the old current core is frozen",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        audit = _load(args.audit_json) if args.audit_json else run_audit(args.lockfile)
        policy = _load(args.policy)
        report, errors = build_report(audit, policy, root=args.repo.resolve())
        if args.shipping_current:
            held = [
                item["id"]
                for item in report["findings"]
                if item.get("version")
                and item["disposition"]["disposition"]
                in {"retire_with_current_core", "fix_after_pr6_baseline_before_port"}
            ]
            if held:
                errors.append("current core cannot ship with held advisories: " + ", ".join(held))
        _write_or_check(args.output, _canonical(report), args.check)
        if errors:
            raise ValueError("; ".join(errors))
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"RustSec validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

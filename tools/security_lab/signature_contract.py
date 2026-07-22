from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

from .git import GitError, GitRepository


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "security" / "generated" / "signature-contract.json"


def _canonical(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def analyze_ref(repo: GitRepository, ref: str) -> dict[str, Any]:
    commit = repo.resolve(ref)
    cpp = repo.show_text(commit, "native/src/core/package.cpp")
    rust = repo.show_text(commit, "native/src/core/package.rs")
    cargo = repo.show_text(commit, "native/src/core/Cargo.toml")
    if cpp:
        match = re.search(r"^#define\s+ENFORCE_SIGNATURE\s+(.+)$", cpp, flags=re.MULTILINE)
        value = match.group(1).strip() if match else None
        globally_disabled = value == "0"
        return {
            "ref": ref,
            "commit": commit,
            "implementation": "cpp-compile-constant",
            "release_signature_enforced": value == "(!MAGISK_DEBUG)",
            "global_bypass_detected": globally_disabled,
            "normal_replacement_rejected": (
                not globally_disabled
                and "APK signature mismatch" in cpp
                and "uninstall_pkg(JAVA_PACKAGE_NAME)" in cpp
            ),
            "hidden_dyn_replacement_rejected": (
                not globally_disabled
                and "dyn APK signature mismatch" in cpp
                and "clear_pkg(mgr_pkg->data(), u)" in cpp
            ),
            "hidden_identity_bound": "cert != *mgr_cert" in cpp and "app_id != mgr_app_id" in cpp,
            "trusted_stub_recovery_present": "install_stub()" in cpp and "preserve_stub_apk" in cpp,
            "source_evidence": {"ENFORCE_SIGNATURE": value},
        }
    if rust:
        default_match = re.search(r"^default\s*=\s*\[([^]]+)]", cargo, flags=re.MULTILINE)
        default_features = default_match.group(1) if default_match else ""
        feature_default = '"check-signature"' in default_features
        cfg_count = rust.count(
            '#[cfg(all(feature = "check-signature", not(debug_assertions)))]'
        )
        return {
            "ref": ref,
            "commit": commit,
            "implementation": "rust-default-feature",
            "release_signature_enforced": feature_default and cfg_count >= 2,
            "global_bypass_detected": False,
            "normal_replacement_rejected": (
                "pkg: APK signature mismatch" in rust
                and "uninstall_pkg(cstr!(APP_PACKAGE_NAME))" in rust
                and "Status::CertMismatch" in rust
            ),
            "hidden_dyn_replacement_rejected": (
                "pkg: dyn APK signature mismatch" in rust and cfg_count >= 2
            ),
            "hidden_identity_bound": (
                "pkg == self.repackaged_pkg" in rust
                and "cert != self.repackaged_cert" in rust
                and "repackaged_app_id" in rust
            ),
            "trusted_stub_recovery_present": (
                "fn install_stub" in rust
                and "preserve_stub_apk" in rust
                and "self.install_stub()" in rust
            ),
            "source_evidence": {
                "check_signature_default": feature_default,
                "release_only_rejection_cfg_count": cfg_count,
            },
        }
    raise ValueError(f"{ref} has no recognized package identity implementation")


def build_report(repo: GitRepository, current: str, stable: str, master: str) -> dict[str, Any]:
    snapshots = {
        "current": analyze_ref(repo, current),
        "stable": analyze_ref(repo, stable),
        "master": analyze_ref(repo, master),
    }
    errors = []
    if not snapshots["current"]["global_bypass_detected"]:
        errors.append("current baseline no longer matches the audited global bypass; review the threat model")
    for lane in ("stable", "master"):
        snapshot = snapshots[lane]
        for field in (
            "release_signature_enforced",
            "normal_replacement_rejected",
            "hidden_dyn_replacement_rejected",
            "hidden_identity_bound",
            "trusted_stub_recovery_present",
        ):
            if not snapshot[field]:
                errors.append(f"{lane} lacks {field}")
    return {
        "schema_version": 1,
        "evidence_level": "source-contract",
        "snapshots": snapshots,
        "runtime_scenarios_required_in_pr6": [
            "trusted-original-manager-accepted",
            "untrusted-original-replacement-rejected",
            "trusted-hidden-manager-accepted",
            "hidden-dyn-certificate-change-rejected",
            "trusted-stub-recovery-restores-control",
        ],
        "validation": {"passed": not errors, "errors": errors},
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify manager signature enforcement across lanes")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--current", default="f943ecddd11d0e648877ffb1f917c16767b8f7cc")
    parser.add_argument("--stable", default="v30.7")
    parser.add_argument("--master", default="upstream/master")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_report(
            GitRepository(args.repo.resolve()), args.current, args.stable, args.master
        )
        payload = _canonical(report)
        if args.check:
            if not args.output.exists() or args.output.read_text(encoding="utf-8") != payload:
                raise ValueError(f"{args.output} is stale")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        if not report["validation"]["passed"]:
            raise ValueError("; ".join(report["validation"]["errors"]))
    except (OSError, ValueError, GitError) as exc:
        print(f"signature contract failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

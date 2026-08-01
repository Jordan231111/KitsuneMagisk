from __future__ import annotations

import argparse
import configparser
from dataclasses import dataclass
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.request
from typing import Any, Iterable, Mapping

from .git import GitError, GitRepository


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = ROOT / "security" / "upstream-policy.json"
DEFAULT_OUTPUT = ROOT / "security" / "generated" / "upstream-ledger.json"


@dataclass(frozen=True)
class PathRule:
    id: str
    patterns: tuple[str, ...]
    owner: str
    sensitive: bool
    gates: tuple[str, ...]

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "PathRule":
        return cls(
            id=str(value["id"]),
            patterns=tuple(str(item) for item in value["patterns"]),
            owner=str(value["owner"]),
            sensitive=bool(value["sensitive"]),
            gates=tuple(str(item) for item in value["gates"]),
        )

    def matches(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.patterns)


def _canonical_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _gitmodules(text: str) -> dict[str, str]:
    if not text.strip():
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    result: dict[str, str] = {}
    for section in parser.sections():
        if parser.has_option(section, "path") and parser.has_option(section, "url"):
            result[parser.get(section, "path")] = parser.get(section, "url")
    return result


def _first_match(text: str, patterns: Iterable[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.MULTILINE)
        if match:
            return match.group(1)
    return None


def _toolchain_snapshot(repo: GitRepository, ref: str) -> dict[str, Any]:
    build_py = repo.show_text(ref, "build.py")
    setup_candidates = (
        repo.show_text(ref, "buildSrc/src/main/java/Setup.kt")
        + repo.show_text(ref, "buildSrc/src/main/kotlin/Setup.kt")
        + repo.show_text(ref, "buildSrc/src/main/kotlin/Config.kt")
        + repo.show_text(ref, "app/buildSrc/src/main/java/Setup.kt")
        + repo.show_text(ref, "app/buildSrc/src/main/kotlin/Setup.kt")
    )
    gradle_props = repo.show_text(ref, "gradle.properties") + repo.show_text(
        ref, "app/gradle.properties"
    )
    wrapper = repo.show_text(ref, "gradle/wrapper/gradle-wrapper.properties") or repo.show_text(
        ref, "app/gradle/wrapper/gradle-wrapper.properties"
    )
    cargo_paths = [path for path in repo.list_files(ref) if path.endswith("Cargo.toml")]
    cargo = "\n".join(repo.show_text(ref, path) for path in cargo_paths)
    rust_toolchain_text = "\n".join(
        repo.show_text(ref, path)
        for path in ("rust-toolchain.toml", "rust-toolchain", ".rust-toolchain")
    )
    setup_action = repo.show_text(ref, ".github/actions/setup/action.yml")
    all_android = "\n".join(
        repo.show_text(ref, path)
        for path in repo.list_files(ref)
        if path.endswith(".gradle.kts")
    )
    android_text = setup_candidates + "\n" + all_android

    abi_match = re.search(r"archs\s*=\s*\[([^]]+)]", build_py, flags=re.DOTALL)
    abis = re.findall(r'["\']([^"\']+)["\']', abi_match.group(1)) if abi_match else []
    if not abis:
        support_abis = re.search(r"support_abis\s*=\s*\{(.*?)}", build_py, flags=re.DOTALL)
        if support_abis:
            abis = re.findall(r'["\']([^"\']+)["\']\s*:', support_abis.group(1))
    if not abis:
        abis = sorted(set(re.findall(r'"(armeabi-v7a|arm64-v8a|x86|x86_64)"', android_text)))

    return {
        "python_minimum": _first_match(build_py, (r"sys\.version_info\s*>=\s*\(([^)]+)\)",)),
        "jdk": _first_match(
            setup_action + "\n" + android_text,
            (r'java-version:\s*["\']?([^"\'\s]+)', r"jvmToolchain\((\d+)\)"),
        ),
        "gradle_distribution": _first_match(wrapper, (r"distributionUrl=.*?/([^/]+\.zip)",)),
        "compile_sdk": _first_match(
            android_text,
            (
                r"compileSdk(?:Version)?\s*[=(]\s*(\d+)",
                r"compileSdk\s*\{[\s\S]*?release\((\d+)\)",
            ),
        ),
        "target_sdk": _first_match(android_text, (r"targetSdk(?:Version)?\s*[=(]\s*(\d+)",)),
        "min_sdk": _first_match(android_text, (r"minSdk(?:Version)?\s*[=(]\s*(\d+)",)),
        "build_tools": _first_match(android_text, (r'buildToolsVersion\s*=\s*"([^"]+)"',)),
        "ndk": _first_match(android_text, (r'ndkVersion\s*=\s*"([^"]+)"',)),
        "ondk": _first_match(
            gradle_props + "\n" + build_py,
            (r"^magisk\.ondkVersion\s*=\s*(\S+)", r'ondk_version\s*=\s*"([^"]+)"'),
        ),
        "rust_editions": sorted(set(re.findall(r'^edition\s*=\s*"([^"]+)"', cargo, re.MULTILINE))),
        "rust_toolchain": _first_match(
            rust_toolchain_text,
            (r'^channel\s*=\s*"([^"]+)"', r'^\s*([^#\s]+)\s*$'),
        ),
        "rust_toolchain_pinned": bool(rust_toolchain_text.strip()),
        "abis": abis,
    }


def _path_metadata(paths: Iterable[str], rules: list[PathRule]) -> dict[str, Any]:
    owners: dict[str, int] = {}
    surfaces: dict[str, int] = {}
    unowned: list[str] = []
    sensitive = False
    gates: set[str] = set()
    for path in paths:
        matched = [rule for rule in rules if rule.matches(path)]
        if not matched:
            unowned.append(path)
            continue
        for rule in matched:
            owners[rule.owner] = owners.get(rule.owner, 0) + 1
            surfaces[rule.id] = surfaces.get(rule.id, 0) + 1
            sensitive = sensitive or rule.sensitive
            if rule.sensitive:
                gates.update(rule.gates)
    return {
        "owners": dict(sorted(owners.items())),
        "surfaces": dict(sorted(surfaces.items())),
        "sensitive": sensitive,
        "required_gates": sorted(gates),
        "unowned_paths": sorted(unowned),
    }


def _commit_records(
    repo: GitRepository,
    revision_range: str,
    lane: str,
    rules: list[PathRule],
    disposition: Mapping[str, str],
) -> dict[str, Any]:
    commits = repo.commits(revision_range)
    sensitive: list[dict[str, Any]] = []
    unowned_commits = 0
    for commit in commits:
        paths = repo.commit_paths(commit)
        path_metadata = _path_metadata(paths, rules)
        if path_metadata["unowned_paths"]:
            unowned_commits += 1
        if not path_metadata["sensitive"]:
            continue
        record = repo.commit_metadata(commit)
        record.update(
            {
                "lane": lane,
                "paths": paths,
                "ownership": path_metadata,
                "disposition": {
                    "decision": disposition["decision"],
                    "rationale": disposition["rationale"],
                },
            }
        )
        sensitive.append(record)
    return {
        "revision_range": revision_range,
        "commit_count": len(commits),
        "sensitive_commit_count": len(sensitive),
        "commits_with_unowned_paths": unowned_commits,
        "sensitive_commits": sensitive,
    }


def _submodules(repo: GitRepository, ref: str) -> list[dict[str, Any]]:
    urls = _gitmodules(repo.show_text(ref, ".gitmodules"))
    result = []
    for path, commit in repo.submodule_entries(ref):
        result.append(
            {
                "path": path,
                "url": urls.get(path),
                "commit": commit,
            }
        )
    return result


def _release_metadata(path: Path | None, url: str | None) -> dict[str, Any] | None:
    if path is None and url is None:
        return None
    if path is not None:
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        request = urllib.request.Request(
            str(url),
            headers={"Accept": "application/vnd.github+json", "User-Agent": "KitsuneMagisk-ledger"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    return {
        "tag_name": payload.get("tag_name"),
        "published_at": payload.get("published_at"),
        "html_url": payload.get("html_url"),
        "draft": payload.get("draft"),
        "prerelease": payload.get("prerelease"),
    }


def build_ledger(
    repo: GitRepository,
    policy: Mapping[str, Any],
    *,
    fork_ref: str,
    stable_ref: str,
    master_ref: str,
    previous_stable_ref: str | None = None,
    release: Mapping[str, Any] | None,
) -> dict[str, Any]:
    rules = [PathRule.from_json(value) for value in policy["path_rules"]]
    refs = {
        "fork": repo.resolve(fork_ref),
        "stable": repo.resolve(stable_ref),
        "master": repo.resolve(master_ref),
    }
    if previous_stable_ref:
        refs["previous_stable"] = repo.resolve(previous_stable_ref)
    configured_commits = {
        "fork": policy.get("fork_baseline_commit"),
        "stable": policy.get("stable_commit"),
        "master": policy.get("master_commit"),
        "previous_stable": policy.get("previous_stable_commit"),
    }
    for lane, expected in configured_commits.items():
        if expected and lane in refs and refs[lane] != expected:
            raise ValueError(
                f"{lane} resolved to {refs[lane]}, expected policy commit {expected}"
            )
    common_stable = repo.merge_base(refs["fork"], refs["stable"])
    common_master = repo.merge_base(refs["fork"], refs["master"])
    if common_stable != common_master:
        raise ValueError("stable and master do not share the same fork merge base")
    if common_stable != policy["audited_common_ancestor"]:
        raise ValueError(
            f"common ancestor changed to {common_stable}; update the audited baseline policy"
        )

    latest_tag = release.get("tag_name") if release else None
    latest_matches = latest_tag is None or latest_tag == policy["stable_tag"]
    if release and (release.get("draft") or release.get("prerelease")):
        raise ValueError("latest release API returned a draft or prerelease")

    snapshots: dict[str, Any] = {}
    ref_labels = {
        "fork": fork_ref,
        "stable": stable_ref,
        "master": master_ref,
        "previous_stable": previous_stable_ref,
    }
    for lane, ref in refs.items():
        paths = repo.changed_files(common_stable, ref)
        snapshots[lane] = {
            "ref": ref_labels[lane],
            "commit": ref,
            "commit_time": repo.commit_metadata(ref)["commit_time"],
            "changed_paths_from_common_ancestor": len(paths),
            "path_ownership": _path_metadata(paths, rules),
            "toolchain": _toolchain_snapshot(repo, ref),
            "submodules": _submodules(repo, ref),
        }

    stable_range = f"{common_stable}..{refs['stable']}"
    master_range = f"{refs['stable']}..{refs['master']}"
    fork_range = f"{common_stable}..{refs['fork']}"
    ranges = {
        "stable": _commit_records(
            repo,
            stable_range,
            "stable",
            rules,
            policy["dispositions"]["stable"],
        ),
        "master": _commit_records(
            repo,
            master_range,
            "master",
            rules,
            policy["dispositions"]["master"],
        ),
        "fork": _commit_records(
            repo,
            fork_range,
            "fork",
            rules,
            policy["dispositions"]["fork"],
        ),
    }
    if "previous_stable" in refs:
        release_range = f"{refs['previous_stable']}..{refs['stable']}"
        ranges["release_to_release"] = _commit_records(
            repo,
            release_range,
            "stable-release",
            rules,
            policy["dispositions"]["stable"],
        )

    range_diff = repo.run(
        "range-diff",
        "--no-color",
        fork_range,
        stable_range,
    )
    return {
        "schema_version": 1,
        "official_repository": policy["official_repository"],
        "inputs": {
            "fork_ref": fork_ref,
            "stable_ref": stable_ref,
            "master_ref": master_ref,
            "previous_stable_ref": previous_stable_ref,
            "path_policy_sha256": _sha256_text(_canonical_json(policy)),
        },
        "release_resolution": {
            "previous_stable_tag": policy.get("previous_stable_tag"),
            "previous_stable_commit": policy.get("previous_stable_commit"),
            "configured_stable_tag": policy["stable_tag"],
            "configured_stable_commit": policy["stable_commit"],
            "latest_release": release,
            "latest_matches_configured_stable": latest_matches,
        },
        "common_ancestor": common_stable,
        "audited_common_ancestor_matches": common_stable == policy["audited_common_ancestor"],
        "counts": {
            "fork_vs_stable": repo.count_range(refs["fork"], refs["stable"]),
            "fork_vs_master": repo.count_range(refs["fork"], refs["master"]),
            **(
                {
                    "previous_vs_stable": repo.count_range(
                        refs["previous_stable"], refs["stable"]
                    )
                }
                if "previous_stable" in refs
                else {}
            ),
        },
        "snapshots": snapshots,
        "ranges": ranges,
        "range_diff": {
            "command": f"git range-diff {fork_range} {stable_range}",
            "sha256": _sha256_text(range_diff),
            "line_count": len(range_diff.splitlines()),
        },
        "safety_policy": {
            "automatic_merge": False,
            "sensitive_commit_requires_targeted_gates": True,
            "unowned_path_requires_maintainer_classification": True,
        },
    }


def _write_or_check(output: Path, payload: str, check: bool) -> None:
    if check:
        if not output.exists():
            raise ValueError(f"missing generated ledger: {output}")
        existing = output.read_text(encoding="utf-8")
        if existing != payload:
            raise ValueError(
                f"{output} is stale; regenerate it with python3 -m tools.security_lab.upstream_ledger"
            )
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")


def _discard_verified_release_metadata(ledger: dict[str, Any]) -> None:
    """Keep a successful live release probe from changing reproducible evidence."""
    resolution = ledger["release_resolution"]
    if resolution["latest_matches_configured_stable"]:
        resolution["latest_release"] = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the Kitsune upstream delta ledger")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--fork-ref",
        help="audited fork ref (defaults to fork_baseline_commit in the policy)",
    )
    parser.add_argument("--stable-ref")
    parser.add_argument("--previous-stable-ref")
    parser.add_argument("--master-ref", default="upstream/master")
    release_group = parser.add_mutually_exclusive_group()
    release_group.add_argument("--release-json", type=Path)
    release_group.add_argument("--resolve-latest", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--allow-new-stable",
        action="store_true",
        help="generate evidence for review instead of failing when the latest stable changed",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        policy = _load_json(args.policy)
        release = _release_metadata(
            args.release_json,
            policy["release_api"] if args.resolve_latest else None,
        )
        ledger = build_ledger(
            GitRepository(args.repo.resolve()),
            policy,
            fork_ref=args.fork_ref or policy["fork_baseline_commit"],
            stable_ref=args.stable_ref or policy["stable_tag"],
            master_ref=args.master_ref,
            previous_stable_ref=(
                args.previous_stable_ref or policy.get("previous_stable_tag")
            ),
            release=release,
        )
        if not ledger["release_resolution"]["latest_matches_configured_stable"]:
            message = (
                "latest official stable changed from "
                f"{policy['stable_tag']} to {release['tag_name']}; rerun the baseline audit"
            )
            if not args.allow_new_stable:
                raise ValueError(message)
            print(f"warning: {message}", file=sys.stderr)
        _discard_verified_release_metadata(ledger)
        _write_or_check(args.output, _canonical_json(ledger), args.check)
    except (GitError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"upstream ledger failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

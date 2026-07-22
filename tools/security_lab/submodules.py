from __future__ import annotations

import argparse
import configparser
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Iterable

from .git import GitError, GitRepository
from .upstream_ledger import ROOT, _canonical_json, _gitmodules


DEFAULT_OUTPUT = ROOT / "security" / "generated" / "submodule-availability.json"
DEFAULT_POLICY = ROOT / "security" / "submodule-policy.json"


def parse_raw_gitlinks(output: str) -> list[tuple[str, str]]:
    result = []
    for line in output.splitlines():
        if not line.startswith(":") or "\t" not in line:
            continue
        metadata, path = line[1:].split("\t", 1)
        fields = metadata.split()
        if len(fields) < 5:
            continue
        old_mode, new_mode, old_object, new_object = fields[:4]
        if old_mode == "160000" and old_object != "0" * 40:
            result.append((path, old_object))
        if new_mode == "160000" and new_object != "0" * 40:
            result.append((path, new_object))
    return sorted(set(result))


def _url_candidates(
    repo: GitRepository, refs: list[str], historical: bool
) -> dict[str, list[str]]:
    candidates: dict[str, set[str]] = {}

    def add(snapshot: str) -> None:
        for path, url in _gitmodules(snapshot).items():
            candidates.setdefault(path, set()).add(url)

    for ref in refs:
        add(repo.show_text(repo.resolve(ref), ".gitmodules"))
    if historical:
        commits = repo.run("log", "--format=%H", *refs, "--", ".gitmodules").splitlines()
        for commit in commits:
            add(repo.show_text(commit, ".gitmodules"))
    return {path: sorted(urls) for path, urls in sorted(candidates.items())}


def collect_gitlinks(repo: GitRepository, refs: Iterable[str], historical: bool) -> list[dict[str, Any]]:
    refs = list(refs)
    urls = _url_candidates(repo, refs, historical)
    found: set[tuple[str, str]] = set()
    for ref in refs:
        resolved = repo.resolve(ref)
        found.update(repo.submodule_entries(resolved))
    if historical:
        commits = repo.run("rev-list", "--topo-order", *refs).splitlines()
        for commit in commits:
            raw = repo.run("diff-tree", "--root", "--raw", "-r", "-m", commit)
            found.update(parse_raw_gitlinks(raw))
    return [
        {
            "path": path,
            "commit": commit,
            "url": urls.get(path, [None])[0],
            "url_candidates": urls.get(path, []),
        }
        for path, commit in sorted(found)
    ]


def _local_object(repo: GitRepository, path: str, commit: str) -> bool:
    submodule = repo.path / path
    if not submodule.exists():
        return False
    return GitRepository(submodule).exists(f"{commit}^{{commit}}")


def _probe_remote(url: str, commit: str, cache: Path, timeout: int) -> tuple[bool, str | None]:
    # Each arbitrary shallow SHA gets an isolated repository. Reusing one
    # shallow file for disconnected historical commits can make git abort with
    # "shallow file has changed since we read it" and falsely mark live pins
    # unavailable.
    cache_id = hashlib.sha256(f"{url}\0{commit}".encode()).hexdigest()[:24]
    repository = cache / cache_id
    if not repository.exists():
        init = subprocess.run(
            ["git", "init", "--bare", str(repository)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if init.returncode != 0:
            return False, init.stderr.strip()
    if GitRepository(repository).exists(f"{commit}^{{commit}}"):
        return True, None
    try:
        fetch = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "fetch",
                "--no-tags",
                "--depth=1",
                url,
                commit,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"fetch timed out after {timeout}s"
    if fetch.returncode == 0 and GitRepository(repository).exists(f"{commit}^{{commit}}"):
        return True, None
    error = fetch.stderr.strip().splitlines()
    return False, error[-1] if error else f"git fetch exited {fetch.returncode}"


def build_report(
    repo: GitRepository,
    refs: list[str],
    *,
    historical: bool,
    probe_remote: bool,
    timeout: int,
) -> dict[str, Any]:
    entries = collect_gitlinks(repo, refs, historical)
    missing_url = 0
    locally_missing = 0
    remotely_missing = 0
    with tempfile.TemporaryDirectory(prefix="kitsune-submodule-probe-") as temp:
        cache = Path(temp)
        for entry in entries:
            entry["local_object_present"] = _local_object(repo, entry["path"], entry["commit"])
            if not entry["local_object_present"]:
                locally_missing += 1
            if not entry["url_candidates"]:
                missing_url += 1
                entry["remote_reachable"] = None
                entry["remote_error"] = "no URL found in audited .gitmodules history"
            elif probe_remote:
                errors = []
                for url in entry["url_candidates"]:
                    reachable, error = _probe_remote(url, entry["commit"], cache, timeout)
                    if reachable:
                        entry["url"] = url
                        entry["remote_reachable"] = True
                        entry["remote_error"] = None
                        break
                    errors.append(f"{url}: {error}")
                else:
                    entry["remote_reachable"] = False
                    entry["remote_error"] = "; ".join(errors)
                    remotely_missing += 1
            else:
                entry["remote_reachable"] = None
                entry["remote_error"] = "not probed"
    return {
        "schema_version": 1,
        "refs": {ref: repo.resolve(ref) for ref in refs},
        "historical_gitlinks_included": historical,
        "remote_probe_performed": probe_remote,
        "summary": {
            "gitlink_count": len(entries),
            "missing_url_count": missing_url,
            "local_object_missing_count": locally_missing,
            "remote_object_missing_count": remotely_missing if probe_remote else None,
        },
        "gitlinks": entries,
    }


def validate_missing(report: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    expected_entries = policy.get("expected_unavailable", [])
    expected = {(str(item["path"]), str(item["commit"])) for item in expected_entries}
    if len(expected) != len(expected_entries):
        raise ValueError("submodule policy contains duplicate path/commit entries")
    observed = {
        (str(item["path"]), str(item["commit"]))
        for item in report["gitlinks"]
        if item.get("remote_reachable") is False
    }
    unknown = sorted(observed - expected)
    restored = sorted(expected - observed)
    errors = []
    if unknown:
        errors.append(
            "new unavailable gitlinks: "
            + ", ".join(f"{path}@{commit}" for path, commit in unknown)
        )
    if restored:
        errors.append(
            "stale unavailable-gitlink dispositions: "
            + ", ".join(f"{path}@{commit}" for path, commit in restored)
        )
    report["validation"] = {
        "expected_unavailable_count": len(expected),
        "observed_unavailable_count": len(observed),
        "unknown_unavailable": [
            {"path": path, "commit": commit} for path, commit in unknown
        ],
        "restored_but_still_disposed": [
            {"path": path, "commit": commit} for path, commit in restored
        ],
        "passed": not errors,
    }
    return errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit exact tip and historical submodule objects")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument(
        "--ref",
        action="append",
        dest="refs",
        default=[],
        help="ref to audit; repeat for multiple lanes",
    )
    parser.add_argument("--historical", action="store_true")
    parser.add_argument("--probe-remote", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument(
        "--enforce-policy",
        action="store_true",
        help="fail on a new missing pin or a stale missing-pin disposition",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    refs = args.refs or ["f943ecddd11d0e648877ffb1f917c16767b8f7cc", "v30.7", "upstream/master"]
    try:
        report = build_report(
            GitRepository(args.repo.resolve()),
            refs,
            historical=args.historical,
            probe_remote=args.probe_remote,
            timeout=args.timeout,
        )
        errors: list[str] = []
        if args.probe_remote:
            policy = json.loads(args.policy.read_text(encoding="utf-8"))
            errors = validate_missing(report, policy)
        elif args.enforce_policy:
            raise ValueError("--enforce-policy requires --probe-remote")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(_canonical_json(report), encoding="utf-8")
        if args.probe_remote and report["summary"]["remote_object_missing_count"]:
            print(
                f"warning: {report['summary']['remote_object_missing_count']} gitlinks are not fetchable",
                file=sys.stderr,
            )
        if args.enforce_policy and errors:
            raise ValueError("; ".join(errors))
    except (OSError, ValueError, GitError, subprocess.SubprocessError) as exc:
        print(f"submodule audit failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

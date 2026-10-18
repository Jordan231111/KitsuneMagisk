#!/usr/bin/env python3
"""Audit historical Kitsune observations without treating ancestry as feature parity."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FEATURE_ID = re.compile(r"[A-Z]+-[0-9]{3}(?:-[a-z0-9]+)?\Z")
DECISION = re.compile(
    r"(?:inherited-and-tested|ported-and-tested-in-PR[0-9]+|"
    r"deferred-to-PR(?:9|1[0-5])|unqualified-target|retired-with-evidence)\Z"
)


class LedgerError(ValueError):
    pass


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise LedgerError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def read_json(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value = {}
        for key, item in pairs:
            if key in value:
                raise LedgerError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise LedgerError("unsupported port ledger schema")
    return value


def roadmap_seed(text: str) -> dict[str, tuple[str, str]]:
    try:
        section = text.split("## Mandatory PR8 atomic seed\n", 1)[1]
        section = section.split("# Proposed pull-request sequence", 1)[0]
    except IndexError as exc:
        raise LedgerError("roadmap has no mandatory atomic seed") from exc
    rows = re.findall(r"^\| `([A-Z]+-[0-9]{3})` \| (.+?) \| (.+?) \|$", section, re.M)
    if not rows or len({row[0] for row in rows}) != len(rows):
        raise LedgerError("roadmap atomic seed is empty or contains duplicate IDs")
    return {key: (behavior, owner) for key, behavior, owner in rows}


def release_seed(text: str) -> set[str]:
    marker = "| Ledger tag / observed incarnation | Exact commit |"
    if marker not in text:
        raise LedgerError("roadmap has no frozen release observations")
    table = text.split(marker, 1)[1].split("\n\n", 1)[0]
    commits = set(re.findall(r"\| `([0-9a-f]{40})` \|", table))
    if not commits:
        raise LedgerError("frozen release observations are empty")
    return commits


def require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{label} must be nonempty text")
    return value


def require_digest(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise LedgerError(f"{label} must be a full lowercase digest")
    return value


def relative_path(value: Any) -> str:
    value = require_text(value, "evidence path")
    if (
        value.startswith("/") or "\\" in value or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise LedgerError(f"evidence path is not a repository-relative path: {value}")
    return value


def indexed(rows: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise LedgerError(f"{label} must be a nonempty list")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise LedgerError(f"invalid {label} row")
        key = require_text(row.get("id"), f"{label} ID")
        if key in result:
            raise LedgerError(f"duplicate {label} ID: {key}")
        result[key] = row
    return result


def validate_ledger(ledger: dict[str, Any], seed: dict[str, tuple[str, str]]) -> None:
    if type(ledger.get("schema")) is not int or ledger["schema"] != 1:
        raise LedgerError("unsupported port ledger schema")
    for name in ("ancestor", "endpoint", "upstream", "candidate"):
        require_digest(ledger.get(name), SHA1, name)
    observations = indexed(ledger.get("observations"), "observation")
    for key, row in observations.items():
        require_text(row.get("label"), f"{key} label")
        if row.get("kind") == "git":
            require_digest(row.get("commit"), SHA1, f"{key} commit")
        elif row.get("kind") == "archive":
            require_digest(row.get("sha256"), SHA256, f"{key} digest")
            relative_path(row.get("path"))
            require_text(row.get("origin"), f"{key} provenance")
            if type(row.get("size")) is not int or row["size"] < 1:
                raise LedgerError(f"{key} size must be a positive integer")
        else:
            raise LedgerError(f"{key} has an unknown observation kind")
    features = indexed(ledger.get("features"), "feature")
    missing = sorted(set(seed) - features.keys())
    if missing:
        raise LedgerError(f"missing mandatory historical features: {', '.join(missing)}")
    for key, row in features.items():
        if FEATURE_ID.fullmatch(key) is None:
            raise LedgerError(f"invalid feature ID: {key}")
        for name in ("behavior", "upstream_overlap", "affected_users", "migration"):
            require_text(row.get(name), f"{key} {name}")
        decision = require_text(row.get("decision"), f"{key} decision")
        if DECISION.fullmatch(decision) is None:
            raise LedgerError(f"{key} has an unowned or invalid decision: {decision}")
        owner = row.get("owner_pr")
        if type(owner) is not int or not 7 <= owner <= 16:
            raise LedgerError(f"{key} needs a numbered owner PR")
        if decision.startswith("deferred-to-PR") and int(decision.split("PR")[1]) != owner:
            raise LedgerError(f"{key} deferral does not match its owner")
        if decision == "unqualified-target":
            blocker = row.get("qualification_blocker")
            if owner != 16 or not isinstance(blocker, dict) or blocker.get("implementation_retained") is not True:
                raise LedgerError(f"{key} needs an explicit PR16 target qualification blocker")
            for field in ("target", "reason", "evidence"):
                require_text(blocker.get(field), f"{key} qualification blocker {field}")
            relative_path(blocker["evidence"])
            if row.get("acceptance_evidence"):
                raise LedgerError(f"{key} cannot claim acceptance while its target is unqualified")
        status = row.get("historical_status")
        if (not isinstance(status, list) or not status
                or any(not isinstance(item, str) or item not in {"E", "O", "N"} for item in status)
                or len(set(status)) != len(status)):
            raise LedgerError(f"{key} needs explicit endpoint/tag-only status")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise LedgerError(f"{key} is missing evidence")
        for item in evidence:
            if item not in observations:
                raise LedgerError(f"{key} references an unknown observation: {item}")
        for field in ("first_shipped", "last_shipped"):
            if field not in row or (row[field] is not None and row[field] not in observations):
                raise LedgerError(f"{key} {field} needs an observation or explicit null")
            if row[field] is not None and row[field] not in evidence:
                raise LedgerError(f"{key} {field} must be included in its evidence")
        if not isinstance(row.get("upstream_evidence"), list):
            raise LedgerError(f"{key} needs an upstream comparison evidence list")
        for item in row["upstream_evidence"]:
            require_text(item, f"{key} upstream evidence")
        # An observation is source provenance, never proof that the new code works.
        if not decision.startswith("deferred-to-") and decision != "unqualified-target":
            require_text(row.get("acceptance_evidence"), f"{key} acceptance evidence")
        for commit in row.get("commits", []):
            require_digest(commit, SHA1, f"{key} historical commit")
        for path in row.get("paths", []):
            relative_path(path)
    gaps = ledger.get("provenance_gaps", [])
    if not isinstance(gaps, list):
        raise LedgerError("provenance_gaps must be a list")
    for gap in gaps:
        require_text(gap, "provenance gap")
    limits = ledger.get("archive_limits", [])
    if not isinstance(limits, list):
        raise LedgerError("archive_limits must be a list")
    limit_ids = set()
    for limit in limits:
        key = require_text(limit.get("id"), "archive limit ID")
        if key in limit_ids:
            raise LedgerError(f"duplicate archive limit: {key}")
        limit_ids.add(key)
        if type(limit.get("owner_pr")) is not int or not 9 <= limit["owner_pr"] <= 15:
            raise LedgerError(f"{key} needs a numbered archive disposition owner")
        for field in ("finding", "disposition"):
            require_text(limit.get(field), f"{key} {field}")
        for field, allowed in (("features", features), ("evidence", observations)):
            values = limit.get(field)
            if not isinstance(values, list) or not values or any(v not in allowed for v in values):
                raise LedgerError(f"{key} needs valid {field}")
    reviews = ledger.get("path_reviews", [])
    if not isinstance(reviews, list):
        raise LedgerError("path_reviews must be a list")
    seen = set()
    for review in reviews:
        key = (require_digest(review.get("commit"), SHA1, "review commit"),
               relative_path(review.get("path")))
        if key in seen:
            raise LedgerError(f"duplicate path review: {key}")
        seen.add(key)
        require_digest(review.get("patch_sha256"), SHA256, "review patch digest")
        require_text(review.get("rationale"), "path review rationale")
        linked = review.get("features", [])
        excluded = review.get("exclusion")
        if not isinstance(linked, list) or any(item not in features for item in linked):
            raise LedgerError(f"unknown feature in path review: {key}")
        if bool(linked) == bool(excluded):
            raise LedgerError(f"path review needs features or an explicit exclusion: {key}")


def history(root: Path, ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """Cover every fork-only commit, including releases outside the endpoint graph."""
    tips = {ledger["endpoint"]}
    tips.update(row["commit"] for row in ledger["observations"] if row["kind"] == "git")
    for commit in sorted(tips | {ledger["ancestor"], ledger["upstream"]}):
        resolved = git(root, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
        if resolved != commit:
            raise LedgerError(f"observation did not resolve to its pinned commit: {commit}")
    commits = git(root, "rev-list", "--reverse", "--topo-order", *sorted(tips),
                  "--not", ledger["upstream"], ledger["ancestor"]).decode().splitlines()
    result = []
    for commit in commits:
        parents = git(root, "rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
        # Diff each parent, including merge conflict resolutions. Do not infer
        # "no behavior" from a merge subject, a revert, or a dependency label.
        paths = set()
        for parent in parents:
            paths.update(path.decode("utf-8") for path in
                         git(root, "diff", "--no-renames", "--name-only", "-z", parent, commit).split(b"\0")
                         if path)
        changed = []
        for path in sorted(paths):
            digest = hashlib.sha256()
            for parent in parents:
                digest.update(parent.encode("ascii") + b"\0")
                digest.update(git(root, "diff", "--no-ext-diff", "--no-textconv", "--binary",
                                  "--full-index", "--no-renames", parent, commit, "--", path))
            changed.append({"path": path, "patch_sha256": digest.hexdigest()})
        result.append({
            "commit": commit,
            "subject": git(root, "show", "-s", "--format=%s", commit).decode("utf-8").strip(),
            "paths": changed,
        })
    return result


def audit_assets(root: Path, ledger: dict[str, Any], roadmap: str) -> list[dict[str, Any]]:
    """Check referenced bytes and failed retrievals, not just an archive's wrapper hash."""
    observations = {o["id"]: o for o in ledger["observations"]}
    marker = "| Published channel/version | Version code | Immutable metadata commit |"
    if marker not in roadmap and "asset-references" not in observations:
        return []
    required = {"full-lite-assets", "moved-k4-assets", "original-stable", "metadata-index",
                "asset-references", "module-template"}
    missing = required - observations.keys()
    if missing:
        return [{"kind": "missing-asset-inventory", "observation": key} for key in sorted(missing)]

    def record(key: str) -> Any:
        return json.loads((root / observations[key]["path"]).read_text(encoding="utf-8"))

    try:
        assets = record("full-lite-assets")["artifacts"]
        moved = record("moved-k4-assets")["artifacts"]
        stable = record("original-stable")
        index = record("metadata-index")
        reference_record = record("asset-references")
        trees = {r["commit"]: {e["path"]: e for e in r["entries"]}
                 for r in reference_record["trees"]}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LedgerError(f"invalid asset inventory: {exc}") from exc

    gaps = []
    available = set()
    for row in assets + moved + stable["artifacts"] + [record("module-template")]:
        digest = require_digest(row.get("sha256"), SHA256, "archived artifact digest")
        size = row.get("size")
        if type(size) is not int or size <= 0:
            raise LedgerError("archived artifact needs a byte length")
        require_text(row.get("archive_url", row.get("origin", row.get("url"))), "artifact origin")
        available.add((size, digest))
    # These frozen byte seeds live outside the generated inventory. Removing an
    # inventory row cannot quietly shrink the set this check must account for.
    for size, digest in re.findall(r"\| `([0-9,]+)` \| `([0-9a-f]{64})` \|", roadmap):
        if (int(size.replace(",", "")), digest) not in available:
            gaps.append({"kind": "missing-frozen-artifact", "sha256": digest})

    indexed_metadata = {(r["commit"], r["path"]): r for r in index}
    if len(indexed_metadata) != len(index):
        raise LedgerError("duplicate metadata inventory entry")
    if marker in roadmap:
        table = roadmap.split(marker, 1)[1].split("\n\n", 1)[0]
        commits = set(re.findall(r"`([0-9a-f]{40})`", table))
        names = {"stable.json", "beta.json", "canary.json", "debug.json", "note.md",
                 "stable_lite.json", "beta_lite.json", "canary_lite.json", "debug_lite.json",
                 "note_lite.md"}
        for commit in sorted(commits):
            for name in sorted(names):
                if (commit, name) not in indexed_metadata:
                    gaps.append({"kind": "missing-frozen-metadata", "commit": commit, "path": name})

    refs = reference_record["references"]
    by_reference = {(r["metadata_commit"], r["metadata_path"], r["role"]): r for r in refs}
    if len(by_reference) != len(refs):
        raise LedgerError("duplicate metadata asset reference")
    observed = set()
    archived_paths = {o.get("path"): o for o in observations.values() if o["kind"] == "archive"}
    for (commit, name), row in indexed_metadata.items():
        tree = trees.get(commit, {})
        if row["status"] == "absent-at-commit":
            if row.get("http_status") != 404 or name in tree or not tree:
                gaps.append({"kind": "unproven-metadata-absence", "commit": commit, "path": name})
            continue
        if row["status"] != "archived":
            raise LedgerError(f"unreviewed metadata status: {commit}/{name}")
        path = f"docs/port-history/metadata/{commit}/{relative_path(name)}"
        observation = archived_paths.get(path)
        if observation is None or any(observation.get(k) != row.get(k) for k in ("sha256", "size")):
            gaps.append({"kind": "unbound-metadata-record", "commit": commit, "path": name})
            continue
        if not name.endswith(".json"):
            continue
        metadata = json.loads((root / path).read_text(encoding="utf-8"))
        for role in ("magisk", "stub"):
            key = (commit, name, role)
            observed.add(key)
            ref = by_reference.get(key)
            url = metadata[role]["link"]
            if ref is None or ref.get("advertised_url") != url:
                gaps.append({"kind": "unaccounted-asset-reference", "commit": commit,
                             "path": name, "role": role})
                continue
            if ref["status"] == "lost-mutable-endpoint":
                witness = [r for r in stable.get("dead_endpoints", []) if r["url"] == url]
                if (len(witness) != 1 or witness[0].get("http_status") != 404
                        or ref.get("evidence") != "original-stable" or ref.get("owner_pr") != 10
                        or not ref.get("disposition")):
                    gaps.append({"kind": "unproven-lost-asset", "url": url})
                continue
            filename = urlsplit(url).path.rsplit("/", 1)[-1]
            matching = [a for a in assets if a["file"] == filename
                        and a["sha256"] == ref.get("sha256") and a["size"] == ref.get("size")]
            entry = tree.get(filename, {})
            if (ref["status"] != "archived" or not matching
                    or entry.get("sha") != ref.get("git_blob") or entry.get("type") != "blob"
                    or entry.get("size") != ref.get("size")):
                gaps.append({"kind": "unbound-asset-reference", "commit": commit,
                             "path": name, "role": role})
    for commit, name, role in sorted(by_reference.keys() - observed):
        gaps.append({"kind": "stale-asset-reference", "commit": commit, "path": name, "role": role})
    return gaps


def audit(root: Path, ledger: dict[str, Any], *, release: bool = False) -> dict[str, Any]:
    roadmap = (root / "DEVELOPMENT_ROADMAP.md").read_text(encoding="utf-8")
    seed = roadmap_seed(roadmap)
    validate_ledger(ledger, seed)
    observed_releases = {row["commit"] for row in ledger["observations"] if row["kind"] == "git"}
    missing_releases = release_seed(roadmap) - observed_releases
    if missing_releases:
        raise LedgerError(f"missing frozen release observations: {', '.join(sorted(missing_releases))}")
    changes = history(root, ledger)
    reviews = {(row["commit"], row["path"]): row for row in ledger.get("path_reviews", [])}
    gaps = []
    observed = set()
    for change in changes:
        for path in change["paths"]:
            key = (change["commit"], path["path"])
            observed.add(key)
            review = reviews.get(key)
            if review is None:
                gaps.append({"kind": "unreviewed-path", "commit": key[0], **path})
            elif review["patch_sha256"] != path["patch_sha256"]:
                gaps.append({"kind": "changed-review", "commit": key[0], **path})
    for commit, path in sorted(reviews.keys() - observed):
        gaps.append({"kind": "stale-review", "commit": commit, "path": path})
    for observation in ledger["observations"]:
        if observation["kind"] != "archive":
            continue
        path = root / relative_path(observation["path"])
        try:
            path.resolve().relative_to(root.resolve())
            content = path.read_bytes()
        except (OSError, ValueError):
            gaps.append({"kind": "missing-archive", "observation": observation["id"]})
            continue
        if len(content) != observation["size"] or hashlib.sha256(content).hexdigest() != observation["sha256"]:
            gaps.append({"kind": "changed-archive", "observation": observation["id"]})
        # A locally correct digest is insufficient if Git's text conversion
        # changes the retained bytes when another host checks out the record.
        raw_blob = git(root, "hash-object", "--no-filters", str(path))
        stored_blob = git(root, "hash-object", "--path", observation["path"], str(path))
        if raw_blob != stored_blob:
            gaps.append({"kind": "archive-checkout-changes-bytes", "observation": observation["id"]})
    for gap in ledger.get("provenance_gaps", []):
        gaps.append({"kind": "provenance", "detail": gap})
    gaps.extend(audit_assets(root, ledger, roadmap))
    for row in ledger["features"]:
        if row["first_shipped"] is None or row["last_shipped"] is None:
            gaps.append({"kind": "unverified-shipment", "feature": row["id"]})
        if not row["upstream_evidence"]:
            gaps.append({"kind": "unreviewed-upstream-behavior", "feature": row["id"]})
        if row["decision"] == "unqualified-target":
            evidence = (root / row["qualification_blocker"]["evidence"]).resolve()
            if not evidence.is_relative_to(root.resolve()) or not evidence.is_file():
                gaps.append({"kind": "missing-qualification-blocker", "feature": row["id"]})
    if release:
        for limit in ledger.get("archive_limits", []):
            gaps.append({"kind": "unresolved-archive-disposition", "limit": limit["id"]})
        for row in ledger["features"]:
            if row["decision"].startswith("deferred-to-"):
                gaps.append({"kind": "deferred-feature", "feature": row["id"]})
            elif row["decision"] == "unqualified-target":
                gaps.append({"kind": "unqualified-target", "feature": row["id"]})
    return {
        "schema": 1, "gate": "PR16" if release else "PR8", "passed": not gaps,
        "candidate": ledger["candidate"], "feature_count": len(ledger["features"]),
        "decisions": dict(sorted(Counter(row["decision"] for row in ledger["features"]).items())),
        "commit_count": len(changes), "changed_path_count": len(observed),
        "archive_limit_count": len(ledger.get("archive_limits", [])),
        "gaps": gaps, "history": changes,
    }


def render_markdown(ledger: dict[str, Any], report: dict[str, Any]) -> str:
    features = ledger["features"]
    shipments = sum(row["first_shipped"] is not None and row["last_shipped"] is not None
                    for row in features)
    comparisons = sum(bool(row["upstream_evidence"]) for row in features)
    lines = [
        "# Historical Kitsune port ledger", "",
        ("Status: PR8 inventory accounted for; " if report["passed"] else "Status: PR8 audit has open gates; ")
        + "runtime parity and production promotion are separate gates.", "",
        "The [machine-readable ledger](port-ledger.json) preserves all mandatory atomic IDs,",
        "both moved-k4 source incarnations, and the original Full/Lite metadata and assets.",
        "Commit/path assignments identify the responsible behavior; they do not establish that",
        "the maintained implementation reproduces it. First/last fields are bounded observations",
        "of published contracts and associated source, with per-row evidence limits. They are not",
        "globally earliest release dates or compiled-APK/runtime attestations. Discontinuous changes",
        "remain visible in the per-observation [source review](port-history/shipment-source-review.json).", "",
        f"Recorded source comparisons: {comparisons}/{len(features)}. Reviewed shipment ranges:",
        f"{shipments}/{len(features)}. These are source observations, not runtime acceptance.",
        "The JSON retains exact historical paths/commits, upstream blob digests, affected users,",
        "migration guidance and observation links for every row.", "",
        "Run `python tools/port_ledger.py --report out/pr8/port-audit.json` from the repository",
        "root. The command fails for unaccounted or changed paths, missing/corrupt archives,",
        "missing frozen releases/assets, unaccounted metadata references and provenance gaps.",
        "Failed retrievals have explicit evidence and a numbered disposition; they never become",
        "recovered-byte claims. Add `--release` to reject every deferred feature, unqualified target and unresolved",
        "archive disposition. Full Git history is required; this check uses no network access.",
        "After a fresh clone, import the retained original Lite branch with:", "",
        "```sh",
        "git bundle verify docs/port-history/original-lite.bundle",
        "git fetch --no-tags docs/port-history/original-lite.bundle refs/audit/pr8/original-lite:refs/audit/pr8/original-lite",
        "```", "",
        "## Archive dispositions", "",
    ]
    lines.extend(f"- {gap}" for gap in ledger.get("provenance_gaps", []))
    for limit in ledger.get("archive_limits", []):
        lines.append(f"- **{limit['id']} (PR{limit['owner_pr']}):** {limit['finding']} {limit['disposition']}")
    lines += [
        "", "## Atomic decisions", "",
        "Each deferral or unqualified target has an owner and migration guidance in the JSON. These",
        "are not acceptance results, and they cannot pass PR16. The [branch decision](pr8-branch-decision.md)",
        "selects the development foundation while retaining these explicit product gates.", "",
        "| ID | Status | Historical behavior | Observed shipment range | Decision | Owner |",
        "|---|---|---|---|---|---|",
    ]
    for row in features:
        behavior = row["behavior"].replace("|", "\\|").replace("\n", " ")
        status = "/".join(row["historical_status"])
        shipment = (f"`{row['first_shipped']}` → `{row['last_shipped']}`"
                    if row["first_shipped"] is not None and row["last_shipped"] is not None
                    else "Unverified")
        lines.append(f"| `{row['id']}` | {status} | {behavior} | {shipment} | "
                     f"`{row['decision']}` | PR{row['owner_pr']} |")
    lines += [
        "", "## Archive limits", "",
        "Metadata bytes are retained under `port-history/metadata/` with immutable commit URLs",
        "and SHA-256 digests. APK and source-ZIP observations record exact byte digests and",
        "signer checks; downloaded binary/source archives remain in the ignored local lab cache.",
        "A mirror, matching version label, or co-published source ZIP does not by itself prove",
        "the original publisher or a reproducible source-to-APK relationship. The unresolved",
        "dispositions above block the release check. Original metadata and module assets are attributed to HuskyDG",
        "and their recorded source repositories; the project retains its GPL licensing.", "",
        "Generated by `python tools/port_ledger.py --markdown docs/port-ledger.md`.", "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=ROOT / "docs/port-ledger.json")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--markdown", type=Path, help="render the review inventory; does not waive audit failures")
    parser.add_argument("--release", action="store_true", help="reject deferred features and unqualified targets")
    args = parser.parse_args(argv)
    try:
        ledger = read_json(args.ledger)
        report = audit(ROOT, ledger, release=args.release)
        if args.markdown:
            args.markdown.write_bytes(render_markdown(ledger, report).encode("utf-8"))
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        counts = Counter(row["kind"] for row in report["gaps"])
        print(json.dumps({key: value for key, value in report.items() if key not in {"gaps", "history"}}, sort_keys=True))
        if counts:
            print("Open audit evidence: " + ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items())))
        return 0 if report["passed"] else 1
    except (LedgerError, OSError) as exc:
        print(f"port ledger: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

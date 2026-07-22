from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Iterable


class GitError(RuntimeError):
    """A git command failed or produced an unusable result."""


@dataclass(frozen=True)
class GitRepository:
    path: Path

    def run(
        self,
        *args: str,
        check: bool = True,
        input_text: str | None = None,
    ) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and proc.returncode != 0:
            rendered = " ".join(("git", *args))
            raise GitError(f"{rendered} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    def resolve(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def exists(self, object_name: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(self.path), "cat-file", "-e", object_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return proc.returncode == 0

    def show_text(self, ref: str, path: str, *, required: bool = False) -> str:
        spec = f"{ref}:{path}"
        if not self.exists(spec):
            if required:
                raise GitError(f"missing required blob {spec}")
            return ""
        return self.run("show", spec)

    def list_files(self, ref: str) -> list[str]:
        output = self.run("ls-tree", "-r", "--name-only", ref)
        return [line for line in output.splitlines() if line]

    def changed_files(self, old: str, new: str) -> list[str]:
        output = self.run("diff", "--name-only", old, new, "--")
        return [line for line in output.splitlines() if line]

    def commits(self, revision_range: str) -> list[str]:
        output = self.run("rev-list", "--reverse", "--topo-order", revision_range)
        return [line for line in output.splitlines() if line]

    def commit_paths(self, commit: str) -> list[str]:
        output = self.run(
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-m",
            commit,
        )
        return sorted(set(line for line in output.splitlines() if line))

    def commit_metadata(self, commit: str) -> dict[str, object]:
        separator = "%x00"
        output = self.run(
            "show",
            "-s",
            f"--format=%H{separator}%P{separator}%ct{separator}%s",
            commit,
        ).rstrip("\n")
        fields = output.split("\0", 3)
        if len(fields) != 4:
            raise GitError(f"cannot parse metadata for {commit}")
        return {
            "commit": fields[0],
            "parents": fields[1].split() if fields[1] else [],
            "commit_time": int(fields[2]),
            "subject": fields[3],
        }

    def submodule_entries(self, ref: str) -> list[tuple[str, str]]:
        output = self.run("ls-tree", "-r", ref)
        entries: list[tuple[str, str]] = []
        for line in output.splitlines():
            metadata, separator, path = line.partition("\t")
            if not separator:
                continue
            mode, kind, object_id = metadata.split()
            if mode == "160000" and kind == "commit":
                entries.append((path, object_id))
        return sorted(entries)

    def count_range(self, left: str, right: str) -> dict[str, int]:
        output = self.run("rev-list", "--left-right", "--count", f"{left}...{right}")
        left_count, right_count = output.split()
        return {"left": int(left_count), "right": int(right_count)}

    def merge_base(self, *refs: str) -> str:
        return self.run("merge-base", *refs).strip()

    def current_branch(self) -> str:
        return self.run("branch", "--show-current").strip()

    def ensure_clean(self, ignored_paths: Iterable[str] = ()) -> None:
        ignored = set(ignored_paths)
        lines = self.run("status", "--porcelain=v1", "--untracked-files=all").splitlines()
        dirty = [line for line in lines if line[3:] not in ignored]
        if dirty:
            raise GitError("source tree is not clean:\n" + "\n".join(dirty))

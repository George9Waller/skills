"""Git access for immutable review snapshots and legacy branch diagnostics."""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int


class SnapshotError(RuntimeError):
    """A review target could not be proved safe to inspect."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        return {"status": "invalid-base", "code": self.code, "reason": self.message, **self.details}


@dataclass(frozen=True)
class ReviewSnapshot:
    """Pinned committed input for one review."""

    schema_version: str
    repository_root: str
    repository_identity: str
    mode: str
    requested_base_ref: str
    base_sha: str
    merge_base_sha: str
    head_sha: str
    diff_base_sha: str
    diff_head_sha: str
    changed_paths: tuple[str, ...]
    changed_entries: tuple[dict, ...]
    snapshot_id: str
    worktree_dirty: bool
    source_root: str

    def public_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "repository_identity": self.repository_identity,
            "mode": self.mode,
            "requested_base_ref": self.requested_base_ref,
            "snapshot_base_sha": self.base_sha,
            "diff_merge_base_sha": self.merge_base_sha,
            "head_sha": self.head_sha,
            "current_target_sha": self.base_sha,
            "base_sha": self.base_sha,  # compatibility alias; new callers use snapshot_base_sha
            "merge_base_sha": self.merge_base_sha,
            "diff_base_sha": self.diff_base_sha,
            "diff_head_sha": self.diff_head_sha,
            "effective_range": f"{self.diff_base_sha}..{self.diff_head_sha}",
            "changed_paths": list(self.changed_paths),
            "changed_entries": [dict(item) for item in self.changed_entries],
            "snapshot_id": self.snapshot_id,
            "worktree_dirty": self.worktree_dirty,
        }


_snapshot_dirs: set[str] = set()


@atexit.register
def _cleanup_snapshot_dirs() -> None:
    for directory in tuple(_snapshot_dirs):
        shutil.rmtree(directory, ignore_errors=True)


def release_snapshot(snapshot: ReviewSnapshot) -> None:
    """Release one materialized tree when its review state is discarded."""
    shutil.rmtree(snapshot.source_root, ignore_errors=True)
    _snapshot_dirs.discard(snapshot.source_root)


def _git_checked(root: str, *args: str, text: bool = True, timeout: int = 15) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=text,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SnapshotError("git-failed", f"git {' '.join(args)} could not run: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() if text else result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotError("git-failed", f"git {' '.join(args)} failed", stderr=stderr)
    return result.stdout


def _resolve_commit(root: str, value: str, label: str) -> str:
    try:
        return str(_git_checked(root, "rev-parse", "--verify", f"{value}^{{commit}}")).strip()
    except SnapshotError as exc:
        raise SnapshotError("unavailable-ref", f"{label} is unavailable: {value}", ref=value) from exc


def _parse_name_status(value: str) -> tuple[tuple[dict, ...], tuple[str, ...]]:
    entries: list[dict] = []
    paths: list[str] = []
    for line in value.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status[:1] in {"R", "C"} and len(parts) >= 3:
            entry = {"status": status, "old_path": parts[1], "path": parts[2]}
            paths.append(parts[2])
        else:
            entry = {"status": status, "path": parts[1]}
            paths.append(parts[1])
        entries.append(entry)
    return tuple(entries), tuple(sorted(dict.fromkeys(paths)))


def _materialize_revision(root: str, revision: str, snapshot_id: str) -> str:
    archive = _git_checked(root, "archive", "--format=tar", revision, text=False, timeout=30)
    if not isinstance(archive, bytes):  # narrows the overload for type checkers
        raise SnapshotError("git-failed", "git archive returned text instead of bytes")
    directory = tempfile.mkdtemp(prefix=f"gw-review-{snapshot_id}-")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            destination = Path(directory).resolve()
            for member in bundle.getmembers():
                target = (destination / member.name).resolve()
                if not target.is_relative_to(destination):
                    raise SnapshotError("invalid-archive", "Git archive contains an unsafe path", path=member.name)
            bundle.extractall(directory, filter="data")
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    _snapshot_dirs.add(directory)
    return directory


def prepare_snapshot(
    root: str,
    *,
    mode: str,
    base_ref: str | None = None,
    expected_base_sha: str | None = None,
    expected_head_sha: str | None = None,
    expected_changed_paths: list[str] | None = None,
) -> ReviewSnapshot:
    """Resolve and independently validate one committed diff target."""
    if mode not in {"pr", "branch"}:
        raise SnapshotError("invalid-mode", "mode must be 'pr' or 'branch'")
    if mode == "pr" and not base_ref:
        raise SnapshotError("missing-base", "PR review requires base_ref")
    if mode == "pr" and not expected_head_sha:
        raise SnapshotError("missing-head", "PR review requires expected_head_sha")

    repository_root = str(_git_checked(root, "rev-parse", "--show-toplevel")).strip()
    if not base_ref:
        base_ref, reason = resolve_base_ref(repository_root)
        if not base_ref:
            raise SnapshotError("unavailable-ref", reason or "no branch-review base is available")
    base_sha = _resolve_commit(repository_root, base_ref, "base ref")
    if expected_base_sha:
        expected_base = _resolve_commit(repository_root, expected_base_sha, "expected base")
        if expected_base != base_sha:
            raise SnapshotError(
                "base-moved", "base_ref does not match expected_base_sha",
                expected_base_sha=expected_base, actual_base_sha=base_sha,
            )
    head_sha = _resolve_commit(repository_root, "HEAD", "HEAD")
    if expected_head_sha:
        expected = _resolve_commit(repository_root, expected_head_sha, "expected head")
        if expected != head_sha:
            raise SnapshotError("head-mismatch", "HEAD does not match expected_head_sha",
                                expected_head_sha=expected, actual_head_sha=head_sha)
    merge_base = str(_git_checked(repository_root, "merge-base", base_sha, head_sha)).strip()
    if not merge_base:
        raise SnapshotError("no-merge-base", "base and head have no merge base")

    name_status = str(_git_checked(repository_root, "diff", "--name-status", merge_base, head_sha))
    entries, paths = _parse_name_status(name_status)
    independent = tuple(sorted(filter(None, str(_git_checked(
        repository_root, "diff", "--name-only", merge_base, head_sha,
    )).splitlines())))
    if set(paths) != set(independent):
        raise SnapshotError("inventory-mismatch", "independent Git inventories disagree",
                            name_status_paths=list(paths), name_only_paths=list(independent))
    if expected_changed_paths is not None:
        expected_paths = tuple(sorted(dict.fromkeys(expected_changed_paths)))
        if set(expected_paths) != set(paths):
            raise SnapshotError("changed-path-mismatch", "prepared inventory does not match expected_changed_paths",
                                expected_changed_paths=list(expected_paths), actual_changed_paths=list(paths))

    repository_identity = os.path.realpath(repository_root)
    identity = {
        "repository": repository_identity, "mode": mode, "base": base_sha,
        "merge_base": merge_base, "head": head_sha, "paths": paths,
    }
    snapshot_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    dirty = bool(str(_git_checked(repository_root, "status", "--porcelain", "--untracked-files=normal")).strip())
    source_root = _materialize_revision(repository_root, head_sha, snapshot_id)
    return ReviewSnapshot(
        schema_version="review-snapshot.v1", repository_root=repository_root,
        repository_identity=repository_identity, mode=mode, requested_base_ref=base_ref,
        base_sha=base_sha, merge_base_sha=merge_base, head_sha=head_sha,
        diff_base_sha=merge_base, diff_head_sha=head_sha, changed_paths=paths,
        changed_entries=entries, snapshot_id=snapshot_id, worktree_dirty=dirty,
        source_root=source_root,
    )


def read_file_at(root: str, revision: str, file_path: str) -> str | None:
    """Read UTF-8 text from one Git object without consulting the worktree."""
    if not file_path or Path(file_path).is_absolute() or ".." in Path(file_path).parts:
        return None
    try:
        value = _git_checked(root, "show", f"{revision}:{file_path}", text=False)
        if not isinstance(value, bytes):
            return None
        return value.decode("utf-8")
    except (SnapshotError, UnicodeDecodeError):
        return None


def match_pathspec_at(root: str, revision: str, pathspec: str, cap: int = 100) -> dict:
    """Deterministically resolve a Git pathspec at a pinned tree."""
    if not pathspec or pathspec.startswith("-"):
        return {"pathspec": pathspec, "matches": [], "reason": "pathspec must be non-empty and not an option"}
    try:
        output = str(_git_checked(root, "ls-tree", "-r", "--name-only", revision, "--", pathspec))
    except SnapshotError as exc:
        return {"pathspec": pathspec, "matches": [], "reason": exc.message}
    all_matches = sorted(filter(None, output.splitlines()))
    return {"pathspec": pathspec, "matches": all_matches[:max(1, min(cap, 200))],
            "total_matches": len(all_matches), "truncated": len(all_matches) > cap}


def revision_fingerprint(root: str, base: str) -> dict[str, str]:
    """Return the concrete revisions and working-tree state for a review.

    A review plan is a snapshot, not merely a comparison to a named base.
    Keeping this here makes cache invalidation work for branch switches in a
    long-lived MCP process as well as for uncommitted edits.
    """
    def git(*args: str) -> str:
        try:
            result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                    text=True, timeout=5, check=False)
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    base_sha = git("rev-parse", "--verify", base) or base
    head_sha = git("rev-parse", "--verify", "HEAD") or "__no_head__"
    # Include staged, unstaged, and untracked state.  Hashing content rather
    # than just status means a second edit invalidates an already cached plan.
    worktree = "\n".join((
        git("diff", "--binary", "HEAD"),
        git("ls-files", "--others", "--exclude-standard", "-z"),
    ))
    return {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "working_tree_sha": __import__("hashlib").sha256(worktree.encode("utf-8")).hexdigest()[:16],
    }


def get_file_diff(root: str, file: str, base: str = "master", context_lines: int = 5,
                  head: str = "HEAD") -> str:
    """Return only `file`'s unified diff against `base...HEAD`.

    Uses the same missing-ref fallback chain as :func:`get_diff`, with a git
    pathspec after ``--`` so callers never have to fetch and split a whole-repo
    diff just to inspect one changed file.
    """
    return _git_diff(root, base, context_lines, pathspec=file, head=head, allow_fallback=head == "HEAD")


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def get_diff_hunks(root: str, base: str = "master", head: str = "HEAD") -> dict[str, list[Hunk]]:
    """Return per-file changed-line ranges (old and new) against `base`.

    Uses `-U0` (no context padding) so each hunk's start/len is the exact
    span of lines git considers changed — callers intersect these against
    symbol line ranges to classify which symbols a diff actually touched,
    so padding with unrelated context lines would produce false positives.
    Same fallback chain as get_file_diff/get_changed_files. Returns {} on failure
    or when there's nothing to diff — never raises.
    """
    diff_text = _git_diff(root, base, context_lines=0, name_only=False, head=head, allow_fallback=head == "HEAD")
    if not diff_text.strip():
        return {}
    return _parse_hunks(diff_text)


def _strip_diff_prefix(path: str) -> str | None:
    """Strip git's a/ or b/ prefix from a --- / +++ diff header path.

    Returns None for /dev/null (the added- or deleted-file case).
    """
    path = path.strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path.split("/", 1)[1]
    return path


def _parse_hunks(diff_text: str) -> dict[str, list[Hunk]]:
    """Parse unified diff text into {file: [Hunk, ...]}.

    Tracks both --- and +++ headers so a deleted file (+++ /dev/null) still
    resolves to a file identity via its --- path, and a new file (---
    /dev/null) resolves via its +++ path.
    """
    hunks: dict[str, list[Hunk]] = {}
    pre_path: str | None = None
    current_file: str | None = None

    for line in diff_text.split("\n"):
        if line.startswith("--- "):
            pre_path = _strip_diff_prefix(line[4:])
        elif line.startswith("+++ "):
            post_path = _strip_diff_prefix(line[4:])
            current_file = post_path if post_path is not None else pre_path
        elif line.startswith("@@") and current_file is not None:
            m = _HUNK_HEADER_RE.match(line)
            if not m:
                continue
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            hunks.setdefault(current_file, []).append(Hunk(old_start, old_len, new_start, new_len))

    return hunks


def get_changed_files(root: str, base: str = "master", head: str = "HEAD") -> list[str]:
    """Return the list of file paths changed between `base` and HEAD.

    Runs `git diff --name-only <base>...HEAD` with cwd=root. Same fallback
    behavior as get_diff for a missing base ref. Paths are relative to `root`.
    Returns empty list if there's nothing to compare or git fails.
    """
    diff_output = _git_diff(root, base, context_lines=0, name_only=True, head=head, allow_fallback=head == "HEAD")
    if not diff_output.strip():
        return []
    return [line.strip() for line in diff_output.strip().split("\n") if line.strip()]


def resolve_base_ref(root: str, explicit: str | None = None) -> tuple[str | None, str | None]:
    """Resolve the comparison base once, preferring an explicit override.

    ``origin/HEAD`` is authoritative when configured.  The fallback order is
    local ``main``, local ``master``, then ``HEAD~1``; each candidate is
    verified so callers can report an unavailable base rather than silently
    reviewing an empty diff.
    """
    candidates = [explicit] if explicit else []
    if not explicit:
        try:
            out = subprocess.run(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=root,
                                 capture_output=True, text=True, timeout=5, check=False).stdout.strip()
            if out:
                candidates.append(out)
        except (OSError, subprocess.SubprocessError):
            pass
        candidates.extend(["main", "master", "HEAD~1"])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            check = subprocess.run(["git", "rev-parse", "--verify", candidate], cwd=root,
                                   capture_output=True, text=True, timeout=5, check=False)
            if check.returncode == 0:
                return candidate, None
        except (OSError, subprocess.SubprocessError):
            continue
    return None, "no explicit base, origin/HEAD, main, master, or HEAD~1 ref is available"


def _git_diff(
    root: str,
    base: str = "master",
    context_lines: int = 5,
    name_only: bool = False,
    pathspec: str | None = None,
    head: str = "HEAD",
    allow_fallback: bool = True,
) -> str:
    """Internal: run git diff with fallback chain for missing base refs.

    Tries: <base> -> origin/<base> -> first commit. Each attempt returns
    immediately on success. Falls through to return "" if all fail.
    """
    if not allow_fallback:
        args = ["diff", f"-U{context_lines}"]
        if name_only:
            args.append("--name-only")
        args.extend([base, head])
        if pathspec is not None:
            args.extend(["--", pathspec])
        return str(_git_checked(root, *args, timeout=10))
    try:
        # Try the base ref as given.
        output = _run_git_diff(root, base, context_lines, name_only, pathspec, head)
        if output is not None:
            return output

        # Try origin/<base> if base lookup failed.
        if allow_fallback and "/" not in base:
            output = _run_git_diff(root, f"origin/{base}", context_lines, name_only, pathspec, head)
            if output is not None:
                return output

        # Last resort: diff against the first commit in the repo.
        first_commit = _get_first_commit(root) if allow_fallback else None
        if first_commit:
            output = _run_git_diff(root, first_commit, context_lines, name_only, pathspec, head)
            if output is not None:
                return output

        return ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _run_git_diff(
    root: str,
    ref: str,
    context_lines: int,
    name_only: bool,
    pathspec: str | None = None,
    head: str = "HEAD",
) -> str | None:
    """Run git diff against a single ref. Return None if the ref doesn't exist."""
    cmd = ["git", "diff", f"-U{context_lines}"]
    if name_only:
        cmd.append("--name-only")
    cmd.extend([ref, head])
    if pathspec is not None:
        cmd.extend(["--", pathspec])

    try:
        result = subprocess.run(
            cmd,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        # Return None if the ref doesn't exist; empty string is still a valid result.
        if "fatal:" in result.stderr and "bad revision" in result.stderr:
            return None
        # Other git errors are still errors, but we treat them as "no output".
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.SubprocessError, OSError):
        return None


def _get_first_commit(root: str) -> str | None:
    """Get the hash of the first commit in the repo, or None if repo is empty."""
    try:
        # Reverse log to get the oldest commit.
        result = subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.SubprocessError, OSError):
        return None


if __name__ == "__main__":
    import sys

    root = sys.argv[1] if len(sys.argv) > 1 else "."
    base = sys.argv[2] if len(sys.argv) > 2 else "master"

    print(f"[git_analyzer] root={root}, base={base}\n")

    print("== changed files ==")
    changed = get_changed_files(root, base)
    for f in changed[:20]:
        print(f"  {f}")
    if len(changed) > 20:
        print(f"  ... and {len(changed) - 20} more")

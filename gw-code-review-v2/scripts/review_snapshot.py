#!/usr/bin/env python3
"""Capture a complete, content-addressed review input from Git."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


class SnapshotError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode:
        raise SnapshotError(proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def parse_name_status(raw: bytes) -> list[dict[str, str]]:
    fields = raw.split(b"\0")
    out: list[dict[str, str]] = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i].decode("ascii", "replace")
        i += 1
        if status.startswith(("R", "C")):
            old, new = fields[i].decode("utf-8", "surrogateescape"), fields[i + 1].decode("utf-8", "surrogateescape")
            i += 2
            out.append({"status": status, "path": new, "old_path": old})
        else:
            path = fields[i].decode("utf-8", "surrogateescape")
            i += 1
            out.append({"status": status, "path": path})
    return out


def safe_path(root: Path, raw: str, *, follow_leaf: bool = True) -> Path:
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts:
        raise SnapshotError(f"unsafe repository path: {raw!r}")
    target = root.joinpath(*rel.parts)
    resolved = target.resolve(strict=False) if follow_leaf else target.parent.resolve(strict=False) / target.name
    if resolved.is_relative_to(root.resolve()):
        return target
    raise SnapshotError(f"path escapes repository: {raw!r}")


def capture(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    out = Path(args.output).resolve()
    root = Path(git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if out == root or out.is_relative_to(root):
        raise SnapshotError("output directory must be outside the reviewed repository")

    base_sha = resolve(root, args.base)
    head_sha = resolve(root, args.head) if args.head else resolve(root, "HEAD")
    worktree = args.worktree
    if not worktree:
        diff = git(root, "diff", "--no-ext-diff", "--binary", "--no-renames", f"{base_sha}..{head_sha}", "--")
        raw_names = git(root, "diff", "--name-status", "-z", "--no-renames", f"{base_sha}..{head_sha}", "--")
        entries = parse_name_status(raw_names)
    else:
        if args.head:
            raise SnapshotError("--head cannot be combined with --worktree")
        start_status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        diff = git(root, "diff", "--no-ext-diff", "--binary", "--no-renames", base_sha, "--")
        raw_names = git(root, "diff", "--name-status", "-z", "--no-renames", base_sha, "--")
        entries = parse_name_status(raw_names)
        known = {entry["path"] for entry in entries}
        untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        for raw in untracked:
            if raw:
                path = raw.decode("utf-8", "surrogateescape")
                if path not in known:
                    entries.append({"status": "?", "path": path})
        end_status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if start_status != end_status:
            raise SnapshotError("worktree changed during capture; retry from a stable checkout")

    files: dict[str, dict[str, str | int]] = {}
    snapshots: dict[str, bytes] = {}
    for entry in entries:
        if entry["status"].startswith("D"):
            continue
        path = entry["path"]
        if worktree:
            target = safe_path(root, path, follow_leaf=False)
            if target.is_symlink():
                data = os.fsencode(os.readlink(target))
                kind = "symlink"
            elif target.is_file():
                data = target.read_bytes()
                kind = "file"
            else:
                raise SnapshotError(f"changed path is missing from worktree: {path}")
        else:
            spec = f"{head_sha}:{path}"
            proc = subprocess.run(["git", "-C", str(root), "show", spec], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode:
                # A deleted path has no head-side content.
                if entry["status"].startswith("D"):
                    continue
                raise SnapshotError(f"cannot read {path!r} at pinned head: {proc.stderr.decode('utf-8', 'replace').strip()}")
            data = proc.stdout
            tree_entry = git(root, "ls-tree", "-z", head_sha, "--", path).split(b"\0", 1)[0]
            mode = tree_entry.split(b" ", 1)[0] if tree_entry else b""
            kind = "symlink" if mode == b"120000" else "submodule" if mode == b"160000" else "file"
        snapshots[path] = data
        files[path] = {"sha256": sha(data), "bytes": len(data), "kind": kind}

    if worktree:
        diff += untracked_patch(root, entries)
        # Recheck every captured file to close the status-only race for content edits.
        for path, data in snapshots.items():
            target = safe_path(root, path, follow_leaf=False)
            if files[path]["kind"] == "symlink":
                unchanged = target.is_symlink() and os.fsencode(os.readlink(target)) == data
            else:
                unchanged = not target.is_symlink() and target.is_file() and target.read_bytes() == data
            if not unchanged:
                raise SnapshotError(f"worktree file changed during capture: {path}")

    if not diff and not entries:
        diff = b""
    manifest = {
        "schema": "gw-review-snapshot.v1",
        "repo": str(root),
        "base_ref": args.base,
        "base_sha": base_sha,
        "head_ref": args.head or "HEAD",
        "head_sha": head_sha,
        "scope": "worktree" if worktree else "commits",
        "inventory": entries,
        "files": files,
        "diff_sha256": sha(diff),
        "diff_bytes": len(diff),
        "complete": True,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise SnapshotError(f"output already exists: {out}")
    stage = Path(tempfile.mkdtemp(prefix=f".{out.name}.capture-", dir=out.parent))
    try:
        (stage / "diff.patch").write_bytes(diff)
        for path, data in snapshots.items():
            target = safe_path(stage / "files", path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.rename(stage, out)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(json.dumps({"manifest": str(out / "manifest.json"), "diff": str(out / "diff.patch"), "files": len(files), "inventory": len(entries)}))


def untracked_patch(root: Path, entries: list[dict[str, str]]) -> bytes:
    """Render text untracked files as patches; binary contents stay in files/."""
    import difflib

    chunks: list[str] = []
    for entry in entries:
        if entry["status"] != "?":
            continue
        path = entry["path"]
        target = safe_path(root, path, follow_leaf=False)
        if target.is_symlink():
            chunks.append(f"\nUntracked symlink (mode 120000): {path} -> {os.readlink(target)}\n")
            continue
        if not target.is_file():
            raise SnapshotError(f"untracked path is missing from worktree: {path}")
        data = target.read_bytes()
        try:
            text = data.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            chunks.append(f"\nBinary untracked file stored at files/{path} (sha256 {sha(data)})\n")
            continue
        chunks.extend(difflib.unified_diff([], text, fromfile="/dev/null", tofile=f"b/{path}", lineterm="\n"))
    return "".join(chunks).encode("utf-8", "surrogateescape")


def read_source(args: argparse.Namespace) -> None:
    manifest_path = Path(args.snapshot).resolve() / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    path = args.path
    inventory_paths = {item["path"] for item in manifest["inventory"]}
    inventory_paths.update(item["old_path"] for item in manifest["inventory"] if "old_path" in item)
    if path not in inventory_paths:
        raise SnapshotError(f"path is not part of the captured inventory: {path}")
    if args.side == "head" and manifest["scope"] == "worktree":
        file_info = manifest["files"].get(path)
        if file_info is None:
            removed = any(
                (item["path"] == path and item["status"].startswith("D")) or item.get("old_path") == path
                for item in manifest["inventory"]
            )
            message = "path is deleted in the captured worktree" if removed else "head-side source was not captured"
            raise SnapshotError(f"{message}: {path}")
        data = safe_path(manifest_path.parent / "files", path).read_bytes()
        if sha(data) != file_info["sha256"]:
            raise SnapshotError("captured source hash mismatch")
    else:
        rev = manifest["head_sha"] if args.side == "head" else manifest["base_sha"]
        data = git(Path(manifest["repo"]), "show", f"{rev}:{path}")
    sys.stdout.buffer.write(data)


def check_anchor(args: argparse.Namespace) -> None:
    snapshot = Path(args.snapshot).resolve()
    manifest = json.loads((snapshot / "manifest.json").read_text())
    if args.path not in manifest["files"]:
        raise SnapshotError("anchor path is not in the changed-file inventory")
    source = safe_path(snapshot / "files", args.path).read_bytes()
    if sha(source) != manifest["files"][args.path]["sha256"]:
        raise SnapshotError("captured source hash mismatch")
    lines = source.splitlines()
    if args.start < 1 or args.end < args.start or args.end > len(lines):
        raise SnapshotError(f"invalid line range {args.start}-{args.end}; source has {len(lines)} lines")
    changed = added_line_numbers((snapshot / "diff.patch").read_bytes(), args.path)
    if not any(line in changed for line in range(args.start, args.end + 1)):
        raise SnapshotError("anchor range does not include a changed head-side line")
    print(f"verified {args.path}:{args.start}-{args.end} at pinned head {manifest['head_sha']}")


def added_line_numbers(patch: bytes, wanted_path: str) -> set[int]:
    """Return added head-side line numbers for a path in a unified Git patch."""
    import re

    current_path: str | None = None
    new_line: int | None = None
    added: set[int] = set()
    for raw in patch.splitlines():
        if raw.startswith(b"+++ b/"):
            current_path = raw[6:].decode("utf-8", "surrogateescape")
            new_line = None
            continue
        if raw.startswith(b"@@ "):
            match = re.match(rb"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            new_line = int(match.group(1)) if match and current_path == wanted_path else None
            continue
        if new_line is None:
            continue
        if raw.startswith(b"+") and not raw.startswith(b"+++"):
            added.add(new_line)
            new_line += 1
        elif raw.startswith(b" "):
            new_line += 1
        elif raw.startswith(b"-") and not raw.startswith(b"---"):
            continue
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("capture", help="capture pinned commit or worktree review input")
    snap.add_argument("--repo", default=".")
    snap.add_argument("--base", required=True)
    snap.add_argument("--head", help="commit review head; omitted for HEAD or --worktree")
    snap.add_argument("--worktree", action="store_true", help="include staged, unstaged, and untracked changes against base")
    snap.add_argument("--output", required=True)
    snap.set_defaults(func=capture)
    src = sub.add_parser("source", help="read a changed file at the pinned base or head")
    src.add_argument("--snapshot", required=True)
    src.add_argument("--side", choices=("base", "head"), required=True)
    src.add_argument("path")
    src.set_defaults(func=read_source)
    anchor = sub.add_parser("check-anchor", help="check a finding's line range at captured head")
    anchor.add_argument("--snapshot", required=True)
    anchor.add_argument("--path", required=True)
    anchor.add_argument("--start", type=int, required=True)
    anchor.add_argument("--end", type=int, required=True)
    anchor.set_defaults(func=check_anchor)
    args = parser.parse_args()
    try:
        args.func(args)
    except (SnapshotError, OSError, ValueError, KeyError) as exc:
        print(f"snapshot error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "review_snapshot.py"


def run(*args: str, ok: bool = True) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if ok and proc.returncode:
        raise AssertionError(proc.stderr.decode())
    if not ok and not proc.returncode:
        raise AssertionError("command unexpectedly succeeded")
    return proc


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Snapshot Test")
    (repo / "a.txt").write_text("before\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "base")
    return repo


def test_commit_capture_pins_both_revisions_and_complete_diff(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.txt").write_text("after\n" + "x" * 200_000 + "\n")
    (repo / "many.txt").write_text("one\ntwo\n")
    os.symlink("a.txt", repo / "link-to-a")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    original_head = git(repo, "rev-parse", "HEAD")
    out = tmp_path / "capture"
    run("capture", "--repo", str(repo), "--base", "base", "--head", "HEAD", "--output", str(out))

    manifest = json.loads((out / "manifest.json").read_text())
    diff = (out / "diff.patch").read_bytes()
    assert manifest["base_sha"] == git(repo, "rev-parse", "base")
    assert manifest["head_sha"] == original_head
    assert manifest["complete"] is True
    assert manifest["diff_bytes"] == len(diff) > 200_000
    assert manifest["diff_sha256"] == hashlib.sha256(diff).hexdigest()
    assert {item["path"] for item in manifest["inventory"]} == {"a.txt", "many.txt", "link-to-a"}
    assert manifest["files"]["link-to-a"]["kind"] == "symlink"
    assert (out / "files" / "link-to-a").read_bytes() == b"a.txt"

    # Moving the symbolic ref cannot mix later source into the saved review.
    (repo / "a.txt").write_text("later\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "later")
    assert run("source", "--snapshot", str(out), "--side", "head", "a.txt").stdout.startswith(b"after\n")


def test_worktree_capture_includes_staged_unstaged_and_untracked(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.txt").write_text("staged\n")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("final\n")
    (repo / "new file.txt").write_text("new content\n")
    (repo / "binary.bin").write_bytes(b"\x00\xff")
    os.symlink("a.txt", repo / "link-to-a")
    os.symlink("../outside-target", repo / "link-outside-repo")
    out = tmp_path / "capture"
    run("capture", "--repo", str(repo), "--base", "base", "--worktree", "--output", str(out))
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["scope"] == "worktree"
    assert {item["path"] for item in manifest["inventory"]} == {
        "a.txt", "new file.txt", "binary.bin", "link-to-a", "link-outside-repo"
    }
    assert (out / "files" / "a.txt").read_bytes() == b"final\n"
    patch = (out / "diff.patch").read_bytes()
    assert b"new content" in patch
    assert b"binary.bin" in patch
    assert b"Untracked symlink (mode 120000): link-to-a -> a.txt" in patch
    assert manifest["files"]["link-to-a"]["kind"] == "symlink"
    assert manifest["files"]["link-outside-repo"]["kind"] == "symlink"
    assert (out / "files" / "link-to-a").read_bytes() == b"a.txt"
    assert run("source", "--snapshot", str(out), "--side", "head", "a.txt").stdout == b"final\n"


def test_deleted_worktree_source_is_reported_absent_not_read_from_head(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.txt").unlink()
    out = tmp_path / "capture"
    run("capture", "--repo", str(repo), "--base", "base", "--worktree", "--output", str(out))

    result = run("source", "--snapshot", str(out), "--side", "head", "a.txt", ok=False)

    assert b"path is deleted in the captured worktree" in result.stderr


def test_anchor_validation_uses_captured_head_lines(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.txt").write_text("one\ntwo\nthree\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "head")
    out = tmp_path / "capture"
    run("capture", "--repo", str(repo), "--base", "base", "--head", "HEAD", "--output", str(out))
    run("check-anchor", "--snapshot", str(out), "--path", "a.txt", "--start", "2", "--end", "3")
    run("check-anchor", "--snapshot", str(out), "--path", "a.txt", "--start", "0", "--end", "1", ok=False)
    run("check-anchor", "--snapshot", str(out), "--path", "missing", "--start", "1", "--end", "1", ok=False)


def test_anchor_must_touch_an_added_line(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.txt").write_text("before\nafter\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "head")
    out = tmp_path / "capture"
    run("capture", "--repo", str(repo), "--base", "base", "--head", "HEAD", "--output", str(out))
    run("check-anchor", "--snapshot", str(out), "--path", "a.txt", "--start", "2", "--end", "2")
    run("check-anchor", "--snapshot", str(out), "--path", "a.txt", "--start", "1", "--end", "1", ok=False)


def test_rejects_moving_ref_capture_and_output_inside_repo(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run("capture", "--repo", str(repo), "--base", "missing", "--output", str(tmp_path / "bad"), ok=False)
    run("capture", "--repo", str(repo), "--base", "base", "--output", str(repo / "nested"), ok=False)

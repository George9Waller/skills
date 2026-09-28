"""Deterministic review-lane routing and bounded lens assignments."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

LENS_BUDGET_CEILING = 24

_DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt")
_INFRA_SUFFIXES = (".tf", ".tfvars")
_INFRA_NAMES = ("pulumi.yaml", "pulumi.yml", "databricks.yml", "databricks.yaml", "bundle.yml", "bundle.yaml")
_DEPENDENCY_NAMES = {"pyproject.toml", "requirements.txt", "requirements-dev.txt", "poetry.lock", "uv.lock",
                     "pdm.lock", "pipfile", "pipfile.lock", "package.json", "package-lock.json",
                     "yarn.lock", "pnpm-lock.yaml", "cargo.toml", "cargo.lock", "go.mod", "go.sum",
                     ".python-version", ".tool-versions"}
_DB_MARKERS = re.compile(r"\b(?:select_related|prefetch_related|queryset|session\.execute|cursor\.execute|"
                         r"select\s+.+\s+from|insert\s+into|update\s+.+\s+set|delete\s+from|"
                         r"migrations\.|alembic|create_table|add_column)\b", re.IGNORECASE)
_EXTERNAL_MARKERS = re.compile(r"\b(?:requests\.|httpx\.|boto3\.|\.invoke\(|\.send\(|\.publish\(|"
                               r"\.put_object\(|\.get_object\(|\.call\()", re.IGNORECASE)
_WORKER_MARKERS = re.compile(r"\b(?:celery|apply_async|\.delay\(|send_task|enqueue|queue\.|worker)\b", re.IGNORECASE)
_INPUT_MARKERS = re.compile(r"\b(?:request\.|query_params|request\.body|depends\(|security\(|permission|"
                            r"authorization|os\.environ|secret|subprocess|shell|serialize|deserialize)\b", re.IGNORECASE)
_API_MARKERS = re.compile(r"\b(?:@(?:app|router)\.(?:get|post|put|patch|delete)|fastapi|apirouter|"
                          r"serializer|request\.query_params|request\.body)\b", re.IGNORECASE)


def _is_docs(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(_DOC_SUFFIXES) or "/docs/" in lowered or lowered.startswith("docs/")


def _is_executable_or_config(path: str) -> bool:
    return not _is_docs(path) and not path.endswith((".png", ".jpg", ".svg", ".lock"))


def _is_test(path: str) -> bool:
    lowered = path.lower()
    return "/tests/" in lowered or lowered.startswith("tests/") or lowered.rsplit("/", 1)[-1].startswith("test_")


def _hunk_text(hunks: dict[str, str] | None, file: str) -> str:
    return (hunks or {}).get(file, "")


def _row_text(rows: Iterable[dict]) -> str:
    return " ".join(str(row.get("symbol", "")) for row in rows)


def _looks_api(path: str, rows: list[dict], hunk: str) -> bool:
    path_signal = any(part in path.lower() for part in ("/api/", "router", "route", "handler", "controller", "serializer"))
    return path_signal or bool(_API_MARKERS.search(f"{_row_text(rows)}\n{hunk}"))


def _looks_db(rows: list[dict], hunk: str) -> bool:
    """Require a query or migration construct, never a filename like models.py."""
    return bool(_DB_MARKERS.search(f"{_row_text(rows)}\n{hunk}"))


def _looks_external(rows: list[dict], hunk: str) -> bool:
    return bool(_EXTERNAL_MARKERS.search(f"{_row_text(rows)}\n{hunk}"))


def _looks_worker(rows: list[dict], hunk: str) -> bool:
    return bool(_WORKER_MARKERS.search(f"{_row_text(rows)}\n{hunk}"))


def _is_infra(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(_INFRA_SUFFIXES) or lowered.endswith(_INFRA_NAMES)


def _is_dependency_file(path: str) -> bool:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    return name in _DEPENDENCY_NAMES or "dependabot" in lower or "renovate" in lower or "/dependencies" in lower


def _entry(assigned: list[str], checks: list[str], enabled: bool = True,
           unavailable: str = "technology not detected", empty: str = "no relevant changed files") -> dict:
    return {
        "run": bool(enabled and assigned),
        "reason": "assigned changed files" if enabled and assigned else (unavailable if not enabled else empty),
        "assigned_files": assigned if enabled else [],
        "activated_checks": checks if enabled and assigned else [],
    }


def route_review_lanes(change_inventory: dict, tech: dict, hunks: dict[str, str] | None = None) -> dict[str, dict]:
    """Route compact review lanes from changed behaviour, not path fragments."""
    files = list(change_inventory.get("changed_files", []))
    rows = change_inventory.get("changes", [])
    by_file = {path: [row for row in rows if row.get("file") == path] for path in files}
    capabilities = {item.get("file"): item.get("capability") for item in change_inventory.get("file_capabilities", [])}
    executable = [path for path in files if _is_executable_or_config(path) and capabilities.get(path) != "unsupported"]

    core_checks = ["Logic & Edge Case"]
    api_files = [path for path in executable if _looks_api(path, by_file[path], _hunk_text(hunks, path))]
    input_files = [path for path in executable if _INPUT_MARKERS.search(
        f"{_row_text(by_file[path])}\n{_hunk_text(hunks, path)}"
    )]
    if api_files:
        core_checks.append("Contract")
    if input_files:
        core_checks.append("AppSec")
    dependency_files = [path for path in files if _is_dependency_file(path)]
    if dependency_files:
        core_checks.append("Dependency Compatibility")
    core_files = list(dict.fromkeys(executable + [path for path in files if _is_test(path)] + dependency_files))

    docs_files = [path for path in files if _is_docs(path)]
    # A changed implementation can invalidate an unchanged guide or comment.
    # Give Docs bounded behavior context for every non-test executable change;
    # the lane still reports an explicit empty result when no stale text exists.
    consistency_files = list(dict.fromkeys(
        docs_files + [path for path in executable if not _is_test(path)]
    ))
    consistency_checks: list[str] = []
    if consistency_files:
        consistency_checks.append("Repo Consistency")
    if docs_files or consistency_files:
        consistency_checks.append("Docs Drift")

    db_files = [path for path in executable if _looks_db(by_file[path], _hunk_text(hunks, path))]
    external_files = [path for path in executable if _looks_external(by_file[path], _hunk_text(hunks, path))]
    worker_files = [path for path in executable if _looks_worker(by_file[path], _hunk_text(hunks, path))]
    infra_files = [path for path in files if _is_infra(path)]
    operations_files = list(dict.fromkeys(db_files + external_files + worker_files + infra_files))
    operations_checks: list[str] = []
    if db_files:
        operations_checks.append("Performance & DB")
    if external_files:
        operations_checks.append("Load & Growth")
    if worker_files:
        operations_checks.append("Worker Fan-out")
    if infra_files:
        operations_checks.append("Impact")

    return {
        "Core Review": _entry(core_files, core_checks),
        "Consistency & Docs": _entry(consistency_files, consistency_checks),
        "Operations": _entry(operations_files, operations_checks),
    }


def shard_assignment(assigned_files: list[str], changes: list[dict], ceiling: int = LENS_BUDGET_CEILING) -> dict:
    """Stable non-overlapping file shards using Phase 1's symbol budget."""
    ordered = sorted(dict.fromkeys(assigned_files))
    symbols = [row for row in changes if row.get("file") in ordered]
    budget = 8 + math.ceil(len(symbols) / 20)
    if budget <= ceiling:
        return {"sharded": False, "budget": budget, "ceiling": ceiling,
                "shards": [{"id": "1", "assigned_files": ordered, "symbols": symbols}]}
    max_symbols = max(1, (ceiling - 8) * 20)
    shards, current_files, current_symbols = [], [], []
    by_file = {path: [row for row in symbols if row.get("file") == path] for path in ordered}
    for path in ordered:
        file_symbols = by_file[path]
        if len(file_symbols) > max_symbols:
            if current_files:
                shards.append({"id": str(len(shards) + 1), "assigned_files": current_files, "symbols": current_symbols})
                current_files, current_symbols = [], []
            for start in range(0, len(file_symbols), max_symbols):
                shards.append({"id": str(len(shards) + 1), "assigned_files": [path],
                               "symbols": file_symbols[start:start + max_symbols]})
            continue
        if current_files and len(current_symbols) + len(file_symbols) > max_symbols:
            shards.append({"id": str(len(shards) + 1), "assigned_files": current_files, "symbols": current_symbols})
            current_files, current_symbols = [], []
        current_files.append(path)
        current_symbols.extend(file_symbols)
    if current_files:
        shards.append({"id": str(len(shards) + 1), "assigned_files": current_files, "symbols": current_symbols})
    return {"sharded": True, "budget": budget, "ceiling": ceiling, "shards": shards,
            "reason": "assignment exceeds named 24-call/turn ceiling; split by stable file groups"}


def docs_drift_files(change_inventory: dict) -> list[str]:
    """Return actual documentation files; private docstrings stay with Core."""
    return [file for file in change_inventory.get("documentation_files", []) if _is_docs(file)]


def build_docs_drift_bundle(change_inventory: dict, per_file_changes: dict[str, dict]) -> dict:
    """Build a documentation-only fact bundle for compatibility callers."""
    files = docs_drift_files(change_inventory)
    return {
        "files": files,
        "changes": [row for row in change_inventory.get("changes", []) if row.get("file") in files],
        "file_changes": [per_file_changes[file] for file in files if file in per_file_changes],
        "applicable": bool(files),
    }

"""Statically diff API contract surface (DRF serializers, FastAPI routes, Celery tasks).

Detects field/parameter changes by reading source text with ast.parse, without
Django/DRF/FastAPI imports or schema-generation subprocess. This is a deliberate
tradeoff: real schema generation needs a booted app with database + settings,
which this tool must work without. We accept the cost of some false negatives
(e.g., dynamic fields added via property overrides) to gain the speed and
portability of static analysis.

For changed files matching API contract patterns, diffs against the base ref
via `git show`. Compares against generated TypeScript clients when found
(openapi/**/*.ts, *.schemas.ts, query/**/*.ts) to flag coherence issues.
"""

from __future__ import annotations

import ast
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Directories never worth descending into: build output and dependency
# trees. glob("**/...") patterns walk into these fully before filtering —
# on a pnpm monorepo, node_modules alone can be several hundred thousand
# directories, turning a sub-second scan into a multi-minute hang. Every
# repo-wide walk below uses os.walk with in-place dirnames pruning instead,
# which never enters a skipped directory in the first place.
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build", ".venv", "vendor"}


@dataclass
class FieldInfo:
    name: str
    type: str
    flags: dict = field(default_factory=dict)


@dataclass
class ParamInfo:
    name: str
    type: str | None = None
    required: bool = False
    default: str | None = None


def get_contract_changes(root: str, modified_file_paths: list[str] | None = None,
                         base: str | None = None, source_root: str | None = None) -> dict:
    """Detect DRF serializer, FastAPI route, and Celery task signature changes.

    Only inspects files under modified_file_paths if given; else attempts
    best-effort repo-wide scan for generated-client comparison (capped at 500
    Python/FastAPI files to avoid slowness). For files matching one of the
    contract patterns, diffs against `base` using git and computes field/
    parameter-level changes (added, removed, retyped, required-changed).

    Args:
        root: Repository root path.
        modified_file_paths: List of file paths to inspect. If None, scans repo.
        base: Git ref to diff against (default "master").

    Returns:
        Dict with keys "serializer_changes", "fastapi_route_changes",
        "celery_task_changes", "scanned_generated_client_files".
    """
    from .git_analyzer import resolve_base_ref
    root_path = Path(source_root or root)
    base, base_reason = resolve_base_ref(root, base)
    result = {
        "serializer_changes": [],
        "fastapi_route_changes": [],
        "celery_task_changes": [],
        "scanned_generated_client_files": 0,
        "found": False,
        "capability": "python_ast",
    }
    if base is None:
        result["reason"] = base_reason
        return result

    # Determine which Python files to scan.
    if modified_file_paths is None:
        # Best-effort scan: Python files only, capped at 500 to avoid slowness.
        # Pruning walk, not glob("**/*.py") — see _SKIP_DIRS comment above.
        py_files = []
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for filename in filenames:
                if filename.endswith(".py"):
                    py_files.append(str((Path(dirpath) / filename).relative_to(root_path)))
                    if len(py_files) >= 500:
                        break
            if len(py_files) >= 500:
                break
    else:
        py_files = [p for p in modified_file_paths if p.endswith(".py")]

    # Scan for contract changes in each Python file.
    for file_path in py_files:
        full_path = root_path / file_path
        if not full_path.exists():
            continue

        try:
            current = full_path.read_text(errors="ignore")
            prior = _get_file_history(root, file_path, base)
        except (OSError, subprocess.CalledProcessError):
            continue

        # Parse current and prior versions.
        try:
            current_tree = ast.parse(current)
            prior_tree = ast.parse(prior) if prior else None
        except SyntaxError:
            continue

        # Detect DRF serializers.
        _detect_serializer_changes(file_path, current_tree, prior_tree, result)

        # Detect FastAPI routes.
        _detect_fastapi_changes(file_path, current_tree, prior_tree, result)

        # Detect Celery tasks.
        _detect_celery_changes(file_path, current_tree, prior_tree, result)

    # Cross-check against generated TS files (best-effort).
    gen_ts_files = _find_generated_ts_files(str(root_path))
    result["scanned_generated_client_files"] = len(gen_ts_files)

    for serializer in result["serializer_changes"]:
        # Check if serializer class name appears in any TS file.
        class_name = serializer["class_name"]
        found = any(class_name in Path(f).read_text(errors="ignore") for f in gen_ts_files)
        serializer["generated_client_reference"] = "found" if found else "not_found"

    for route in result["fastapi_route_changes"]:
        # Check if route function name appears in TS files.
        func_name = route["function_name"]
        found = any(func_name in Path(f).read_text(errors="ignore") for f in gen_ts_files)
        route["generated_client_reference"] = "found" if found else "not_found"

    result["found"] = bool(result["serializer_changes"] or result["fastapi_route_changes"] or result["celery_task_changes"])
    if not result["found"]:
        result["reason"] = "no supported Python API-contract symbols changed"
    return result


def _detect_serializer_changes(file_path: str, current: ast.Module, prior: ast.Module | None, result: dict) -> None:
    """Detect DRF serializer field changes."""
    current_serializers = _extract_serializers(current, file_path)
    prior_serializers = _extract_serializers(prior, file_path) if prior else {}

    for class_name, current_fields in current_serializers.items():
        prior_fields = prior_serializers.get(class_name, {})

        # Find added, removed, changed fields.
        added = [f for name, f in current_fields.items() if name not in prior_fields]
        removed = [f for name, f in prior_fields.items() if name not in current_fields]
        changed = []

        for name in current_fields:
            if name in prior_fields:
                before = prior_fields[name]
                after = current_fields[name]
                if before.type != after.type or before.flags != after.flags:
                    changed.append({
                        "name": name,
                        "before": {"type": before.type, "flags": before.flags},
                        "after": {"type": after.type, "flags": after.flags},
                    })

        if added or removed or changed:
            result["serializer_changes"].append({
                "file": file_path,
                "class_name": class_name,
                "line": current_serializers[class_name],  # Simplified: store line
                "fields_added": [{"name": f.name, "type": f.type, "flags": f.flags} for f in added],
                "fields_removed": [{"name": f.name, "type": f.type} for f in removed],
                "fields_changed": changed,
                "generated_client_reference": "not_checked",
            })


def _detect_fastapi_changes(file_path: str, current: ast.Module, prior: ast.Module | None, result: dict) -> None:
    """Detect FastAPI route signature changes."""
    current_routes = _extract_fastapi_routes(current, file_path)
    prior_routes = _extract_fastapi_routes(prior, file_path) if prior else {}

    for func_name, route_info in current_routes.items():
        if func_name in prior_routes:
            prior_info = prior_routes[func_name]

            # Diff parameters.
            current_params = {p.name: p for p in route_info["params"]}
            prior_params = {p.name: p for p in prior_info["params"]}

            added = [p for name, p in current_params.items() if name not in prior_params]
            removed = [p for name, p in prior_params.items() if name not in current_params]
            changed = []

            for name in current_params:
                if name in prior_params:
                    c, p = current_params[name], prior_params[name]
                    if c.type != p.type or c.required != p.required:
                        changed.append({
                            "name": name,
                            "before": {"type": p.type, "required": p.required},
                            "after": {"type": c.type, "required": c.required},
                        })

            if added or removed or changed:
                result["fastapi_route_changes"].append({
                    "file": file_path,
                    "function_name": func_name,
                    "line": route_info["line"],
                    "path": route_info.get("path"),
                    "method": route_info.get("method"),
                    "params_added": [{"name": p.name, "type": p.type, "required": p.required} for p in added],
                    "params_removed": [{"name": p.name, "type": p.type} for p in removed],
                    "params_changed": changed,
                    "generated_client_reference": "not_checked",
                })
        else:
            # New route: check if it has required params.
            required_params = [p for p in route_info["params"] if p.required]
            if required_params:
                result["fastapi_route_changes"].append({
                    "file": file_path,
                    "function_name": func_name,
                    "line": route_info["line"],
                    "path": route_info.get("path"),
                    "method": route_info.get("method"),
                    "params_added": [{"name": p.name, "type": p.type, "required": p.required} for p in route_info["params"]],
                    "params_removed": [],
                    "params_changed": [],
                    "generated_client_reference": "not_checked",
                })


def _detect_celery_changes(file_path: str, current: ast.Module, prior: ast.Module | None, result: dict) -> None:
    """Detect Celery task signature changes."""
    current_tasks = _extract_celery_tasks(current, file_path)
    prior_tasks = _extract_celery_tasks(prior, file_path) if prior else {}

    for func_name, task_info in current_tasks.items():
        prior_info = prior_tasks.get(func_name)

        if prior_info:
            current_params = {p.name: p for p in task_info["params"]}
            prior_params = {p.name: p for p in prior_info["params"]}

            added = [p for name, p in current_params.items() if name not in prior_params]
            removed = [p for name, p in prior_params.items() if name not in current_params]

            # Signature risk: breaking if required param added or param removed.
            risk = "unknown"
            if removed or any(p.required for p in added):
                risk = "breaking"
            elif added:
                risk = "additive"

            if added or removed:
                result["celery_task_changes"].append({
                    "file": file_path,
                    "function_name": func_name,
                    "line": task_info["line"],
                    "params_added": [{"name": p.name, "type": p.type, "required": p.required} for p in added],
                    "params_removed": [{"name": p.name, "type": p.type} for p in removed],
                    "params_changed": [],
                    "signature_risk": risk,
                })


def _extract_serializers(tree: ast.Module | None, file_path: str) -> dict[str, dict]:
    """Extract DRF serializer classes and their fields from AST."""
    if tree is None:
        return {}

    serializers: dict[str, dict] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # Check if it's a serializer (base ends with "Serializer" or file is serializers.py).
        is_serializer = (
            "serializer" in file_path.lower() or
            any("Serializer" in base.id if isinstance(base, ast.Name) else False for base in node.bases)
        )

        if not is_serializer:
            continue

        fields: dict[str, FieldInfo] = {}

        for item in node.body:
            # Check for annotated fields: `field_name: FieldType = ...`.
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                name = item.target.id
                type_name = _get_annotation_name(item.annotation)
                flags = _extract_field_flags(item.value)
                fields[name] = FieldInfo(name=name, type=type_name, flags=flags)

            # Check for assigned fields: `field_name = serializers.Field(...)`.
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        name = target.id
                        type_name = _get_call_name(item.value)
                        flags = _extract_field_flags(item.value)
                        fields[name] = FieldInfo(name=name, type=type_name, flags=flags)

        if fields:
            serializers[node.name] = fields

    return serializers


def _extract_fastapi_routes(tree: ast.Module | None, file_path: str) -> dict[str, dict]:
    """Extract FastAPI route functions from AST."""
    if tree is None:
        return {}

    routes: dict[str, dict] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        # Check if decorated with @router.get/post/etc or @app.get/post/etc.
        for decorator in node.decorator_list:
            # Look for pattern: <something>.get/post/put/patch/delete(...).
            if isinstance(decorator, ast.Call):
                if isinstance(decorator.func, ast.Attribute):
                    if decorator.func.attr in ("get", "post", "put", "patch", "delete"):
                        # Extract path from first arg if available.
                        path = None
                        if decorator.args:
                            path_arg = decorator.args[0]
                            if isinstance(path_arg, ast.Constant):
                                path = path_arg.value

                        # Extract parameters.
                        params = _extract_function_params(node)

                        routes[node.name] = {
                            "line": node.lineno,
                            "path": path,
                            "method": decorator.func.attr.upper(),
                            "params": params,
                        }

    return routes


def _extract_celery_tasks(tree: ast.Module | None, file_path: str) -> dict[str, dict]:
    """Extract Celery task functions from AST."""
    if tree is None:
        return {}

    tasks: dict[str, dict] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        # Check if decorated with @shared_task or @app.task/@celery_app.task.
        for decorator in node.decorator_list:
            is_task = False

            # @shared_task
            if isinstance(decorator, ast.Name) and decorator.id == "shared_task":
                is_task = True

            # @<something>.task
            if isinstance(decorator, ast.Call):
                if isinstance(decorator.func, ast.Name) and decorator.func.id == "shared_task":
                    is_task = True
                elif isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "task":
                    is_task = True

            if is_task:
                params = _extract_function_params(node)
                tasks[node.name] = {
                    "line": node.lineno,
                    "params": params,
                }

    return tasks


def _extract_function_params(func: ast.FunctionDef) -> list[ParamInfo]:
    """Extract parameter info from a function definition."""
    params = []

    for arg in func.args.args:
        type_name = None
        if arg.annotation:
            type_name = _get_annotation_name(arg.annotation)

        # Determine if required: no default value, not `None`.
        required = True
        # Check defaults list (covers the last N args).
        defaults_offset = len(func.args.args) - len(func.args.defaults)
        if func.args.args.index(arg) >= defaults_offset:
            default_idx = func.args.args.index(arg) - defaults_offset
            default_val = func.args.defaults[default_idx]
            if isinstance(default_val, ast.Constant) and default_val.value is None:
                required = False

        params.append(ParamInfo(name=arg.arg, type=type_name, required=required))

    return params


def _get_annotation_name(annotation: ast.expr) -> str:
    """Extract a human-readable name from a type annotation."""
    if isinstance(annotation, ast.Name):
        return annotation.id
    elif isinstance(annotation, ast.Attribute):
        return f"{_get_annotation_name(annotation.value)}.{annotation.attr}"
    elif isinstance(annotation, ast.Constant):
        return str(annotation.value)
    else:
        return "Any"


def _get_call_name(expr: ast.expr) -> str:
    """Extract the name of a function call (e.g., 'CharField' from serializers.CharField(...))."""
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Attribute):
            return expr.func.attr
        elif isinstance(expr.func, ast.Name):
            return expr.func.id
    return "Unknown"


def _extract_field_flags(expr: ast.expr) -> dict:
    """Extract keyword flags from a field call (e.g., required=True, read_only=False)."""
    flags = {}

    if isinstance(expr, ast.Call):
        for keyword in expr.keywords:
            if isinstance(keyword.value, ast.Constant):
                flags[keyword.arg] = keyword.value.value

    return flags


def _get_file_history(root: str, file_path: str, base: str) -> str:
    """Get file content from base ref using `git show base:<path>`."""
    from .git_analyzer import read_file_at
    return read_file_at(root, base, file_path) or ""


def _find_generated_ts_files(root: str) -> list[str]:
    """Find generated TypeScript files (openapi, *.schemas.ts, query).

    Single pruning os.walk pass, not glob("**/openapi/**/*.ts") — see
    _SKIP_DIRS comment at the top of this module. This is the one call in
    this module guaranteed to run on every invocation (unlike the
    modified_file_paths=None branch above, which only fires without a
    diff), so an unbounded glob here is the single costliest mistake this
    module could make.
    """
    root_path = Path(root)
    ts_files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        dir_parts = Path(dirpath).relative_to(root_path).parts
        in_openapi_dir = "openapi" in dir_parts
        in_query_dir = "query" in dir_parts
        for filename in filenames:
            if not filename.endswith(".ts"):
                continue
            if in_openapi_dir or in_query_dir or filename.endswith(".schemas.ts"):
                ts_files.append(str(Path(dirpath) / filename))
        if len(ts_files) >= 200:
            break

    return ts_files[:200]


if __name__ == "__main__":
    import json
    import sys

    root = sys.argv[1] if len(sys.argv) > 1 else "."
    base = sys.argv[2] if len(sys.argv) > 2 else "master"
    files = sys.argv[3:] if len(sys.argv) > 3 else None

    changes = get_contract_changes(root, modified_file_paths=files, base=base)
    print(json.dumps(changes, indent=2))

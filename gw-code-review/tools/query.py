"""Bounded, deterministic lookup tools for code-review lenses.

These calls are deliberately narrower than the change-graph tools: each
answers one source or repository question without returning a repo-wide fact
dump.  They share the Phase 2 caches and AST parser where possible.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
from pathlib import Path

from . import ast_parser, change_graph, git_analyzer, schema_diff

MAX_CHANGE_LINES = 400
MAX_CHANGE_CHARS = 48_000
MAX_SOURCE_LINES = 2_000
MAX_QUERY_CAP = 500
MAX_STRUCTURE_ITEMS = 30
MAX_SUMMARY_ITEMS = 12

_LANG_EXTENSIONS = {
    "py": {".py"},
    "ts": {".ts", ".tsx"},
    "yaml": {".yml", ".yaml"},
    "tf": {".tf"},
}

_source_cache: dict[tuple[str, str, int, int], str] = {}


def _repo_file(root: str, file: str) -> tuple[Path | None, str | None]:
    """Resolve a repo-relative path without permitting traversal outside root."""
    root_path = Path(root).resolve()
    if not file:
        return None, "file must be a non-empty repo-relative path"
    candidate = (root_path / file).resolve()
    try:
        candidate.relative_to(root_path)
    except ValueError:
        return None, "file must resolve inside the repository root"
    return candidate, None


def _read_repo_file(root: str, file: str) -> tuple[str | None, str | None]:
    path, error = _repo_file(root, file)
    if error:
        return None, error
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, f"file not found in working tree: {file}"
    except (OSError, UnicodeDecodeError):
        return None, f"file is unreadable as UTF-8 text: {file}"


def _bounded_text(
    text: str,
    max_lines: int = MAX_CHANGE_LINES,
    max_chars: int = MAX_CHANGE_CHARS,
) -> tuple[str, bool]:
    lines = text.splitlines()
    line_limit = max(1, min(int(max_lines), MAX_CHANGE_LINES))
    char_limit = max(1, min(int(max_chars), MAX_CHANGE_CHARS))
    clipped = "\n".join(lines[:line_limit])
    if len(clipped) > char_limit:
        clipped = clipped[:char_limit]
        return clipped, True
    return clipped, len(lines) > line_limit


def _change_metadata(result: dict) -> dict:
    """Attach stable size metadata to all successful change responses."""
    result["returned_items"] = 1
    result["total_items"] = 1
    result["payload_bytes"] = len(str(result).encode("utf-8"))
    return result


def _slice(source: str | None, symbol: ast_parser.Symbol | None, context: int) -> tuple[str, list[int] | None]:
    if source is None or symbol is None:
        return "", None
    lines = source.splitlines()
    start = max(1, symbol.line - context)
    end = min(len(lines), (symbol.end_line or symbol.line) + context)
    return "\n".join(lines[start - 1:end]), [start, end]


def _change_error(selector: str, reason: str) -> dict:
    return {
        "selector": selector,
        "before": "",
        "after": "",
        "hunk": "",
        "truncated": False,
        "reason": reason,
    }


def get_change(
    root: str,
    base: str,
    symbol: str | None = None,
    file: str | None = None,
    with_context: int = 0,
    view: str = "hunk",
    max_lines: int = MAX_CHANGE_LINES,
    max_chars: int = MAX_CHANGE_CHARS,
    *,
    head: str = "HEAD",
    source_root: str | None = None,
    cache_key: str | None = None,
) -> dict:
    """Return one changed symbol or file with an explicitly bounded payload.

    ``view='hunk'`` is the compact default for review bundles. ``view='full'``
    and ``view='symbol'`` retain before/after source for a concrete follow-up.
    """
    if bool(symbol) == bool(file):
        return _change_error("", "exactly one of symbol or file must be provided")
    if view not in {"hunk", "full", "symbol"}:
        return _change_error("", "view must be one of hunk, full, or symbol")

    context = max(0, min(int(with_context), 100))
    source_root = source_root or root
    data = change_graph._computed(root, base, head=head, source_root=source_root, cache_key=cache_key)

    if file:
        selector = f"file:{file}"
        if file not in data["changed_files"]:
            return _change_error(selector, f"file is not changed against {base}: {file}")

        after, after_error = _read_repo_file(source_root, file)
        before = schema_diff._get_file_history(root, file, base)
        if after is None and not before:
            return _change_error(selector, after_error or f"no current or prior content found for {file}")

        hunk = git_analyzer.get_file_diff(root, file, base, context_lines=context, head=head)
        before_text, before_truncated = _bounded_text(before, max_lines, max_chars)
        after_text, after_truncated = _bounded_text(after or "", max_lines, max_chars)
        hunk_text, hunk_truncated = _bounded_text(hunk, max_lines, max_chars)
        result = {
            "selector": selector,
            "file": file,
            "symbol": None,
            "enclosing_symbol": None,
            "hunk": hunk_text,
            "view": view,
            "truncated": hunk_truncated,
            "truncated_fields": ["hunk"] if hunk_truncated else [],
        }
        if view in {"full", "symbol"}:
            result.update(
                before=before_text,
                after=after_text,
                truncated=before_truncated or after_truncated or hunk_truncated,
                truncated_fields=[
                    name
                    for name, was_truncated in (
                        ("before", before_truncated),
                        ("after", after_truncated),
                        ("hunk", hunk_truncated),
                    )
                    if was_truncated
                ],
            )
        return _change_metadata(result)

    selector = f"symbol:{symbol}"
    try:
        rel_file, name = symbol.split(":", 1)
    except (AttributeError, ValueError):
        return _change_error(selector, "symbol must be 'path/to/file.py:Name'")

    file_symbols = data["file_symbols"].get(rel_file)
    if file_symbols is None:
        return _change_error(selector, f"no changed Python file found for symbol: {rel_file}")

    current_symbol = file_symbols.current.get(name)
    prior_symbol = file_symbols.prior.get(name)
    if current_symbol is None and prior_symbol is None:
        return _change_error(selector, f"symbol not found in current or prior version: {symbol}")

    current_source, _ = _read_repo_file(source_root, rel_file)
    prior_source = schema_diff._get_file_history(root, rel_file, base) or None
    before, before_lines = _slice(prior_source, prior_symbol, context)
    after, after_lines = _slice(current_source, current_symbol, context)
    hunk = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{rel_file}:{name}",
            tofile=f"b/{rel_file}:{name}",
            n=context,
            lineterm="",
        )
    )
    before_text, before_truncated = _bounded_text(before, max_lines, max_chars)
    after_text, after_truncated = _bounded_text(after, max_lines, max_chars)
    hunk_text, hunk_truncated = _bounded_text(hunk, max_lines, max_chars)
    result = {
        "selector": selector,
        "file": rel_file,
        "symbol": name,
        "enclosing_symbol": name.rsplit(".", 1)[0] if "." in name else None,
        "before_lines": before_lines,
        "after_lines": after_lines,
        "hunk": hunk_text,
        "view": view,
        "truncated": hunk_truncated,
        "truncated_fields": ["hunk"] if hunk_truncated else [],
    }
    if view in {"full", "symbol"}:
        result.update(
            before=before_text,
            after=after_text,
            truncated=before_truncated or after_truncated or hunk_truncated,
            truncated_fields=[
                field_name
                for field_name, was_truncated in (
                    ("before", before_truncated),
                    ("after", after_truncated),
                    ("hunk", hunk_truncated),
                )
                if was_truncated
            ],
        )
    return _change_metadata(result)


def get_changes(
    root: str,
    base: str,
    files: list[str],
    *,
    with_context: int = 0,
    view: str = "hunk",
    max_lines: int = 160,
    max_chars: int = 12_000,
    head: str = "HEAD",
    source_root: str | None = None,
    cache_key: str | None = None,
) -> dict:
    """Return compact changes for several known changed files in one response."""
    ordered = list(dict.fromkeys(file for file in files if isinstance(file, str) and file))
    changes = [
        get_change(
            root, base, file=file, with_context=with_context, view=view,
            max_lines=max_lines, max_chars=max_chars,
            head=head, source_root=source_root, cache_key=cache_key,
        )
        for file in ordered
    ]
    result = {
        "changes": changes,
        "returned_items": len(changes),
        "total_items": len(ordered),
        "truncated": any(change.get("truncated", False) for change in changes),
    }
    result["payload_bytes"] = len(str(result).encode("utf-8"))
    return result


def _definition_matches(symbol: ast_parser.Symbol, name: str) -> bool:
    return symbol.name == name if "." in name else symbol.name.split(".")[-1] == name


def find_symbol(
    root: str,
    name: str,
    kind: str | None = None,
    scope: str | None = None,
    cap: int = 10,
) -> dict:
    """Find Python function/class/method definitions by name across the repo."""
    if not name:
        return {"matches": [], "total_matches": 0, "truncated": False, "reason": "name must not be empty"}
    if kind and kind not in {"function", "async_function", "class", "method"}:
        return {
            "matches": [],
            "total_matches": 0,
            "truncated": False,
            "reason": f"unsupported kind: {kind}",
        }

    limit = max(1, min(int(cap), MAX_QUERY_CAP))
    bare_name = name.rsplit(".", 1)[-1]
    definition_re = re.compile(r"^\s*(?:async\s+def|def|class)\s+" + re.escape(bare_name) + r"\b")
    matches: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ast_parser.SKIP_DIRS)
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            abs_file = os.path.join(dirpath, filename)
            rel_file = os.path.relpath(abs_file, root)
            if scope and not rel_file.startswith(scope):
                continue
            try:
                source = Path(abs_file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not any(definition_re.match(line) for line in source.splitlines()):
                continue
            symbols = ast_parser.parse_symbols(source)
            if not symbols:
                continue
            source_lines = source.splitlines()
            for found in symbols:
                if not _definition_matches(found, name) or (kind and found.kind != kind):
                    continue
                matches.append(
                    {
                        "name": found.name,
                        "file": rel_file,
                        "line": found.line,
                        "end_line": found.end_line or found.line,
                        "kind": found.kind,
                        "signature": found.signature,
                        "snippet": source_lines[found.line - 1].strip(),
                    }
                )

    matches.sort(key=lambda match: (match["file"], match["line"], match["name"]))
    total = len(matches)
    result = {
        "matches": matches[:limit],
        "total_matches": total,
        "truncated": total > limit,
    }
    if not matches:
        qualifier = f" within scope {scope!r}" if scope else ""
        result["reason"] = f"no Python definition named {name!r}{qualifier}"
    return result


def get_source(
    root: str,
    file: str,
    around_symbol: str | None = None,
    line_range: list[int] | tuple[int, int] | None = None,
    context: int = 5,
    max_lines: int = 400,
) -> dict:
    """Return one bounded current-tree source range, memoized in process."""
    if around_symbol and line_range:
        return {
            "file": file,
            "source": "",
            "truncated": False,
            "reason": "around_symbol and line_range are mutually exclusive",
        }

    source, error = _read_repo_file(root, file)
    if source is None:
        return {"file": file, "source": "", "truncated": False, "reason": error}
    lines = source.splitlines()
    if not lines:
        return {"file": file, "source": "", "truncated": False, "reason": "file is empty"}

    if around_symbol:
        symbols = ast_parser.parse_symbols(source)
        if symbols is None:
            return {"file": file, "source": "", "truncated": False, "reason": "file has invalid Python syntax"}
        found = next((item for item in symbols if item.name == around_symbol), None)
        if found is None:
            return {
                "file": file,
                "source": "",
                "truncated": False,
                "reason": f"symbol not found in {file}: {around_symbol}",
            }
        requested_start, requested_end = found.line, found.end_line or found.line
    elif line_range:
        if not isinstance(line_range, (list, tuple)) or len(line_range) != 2:
            return {
                "file": file,
                "source": "",
                "truncated": False,
                "reason": "line_range must contain exactly [start, end]",
            }
        try:
            requested_start, requested_end = int(line_range[0]), int(line_range[1])
        except (TypeError, ValueError):
            return {
                "file": file,
                "source": "",
                "truncated": False,
                "reason": "line_range values must be integers",
            }
        if requested_start < 1 or requested_end < requested_start:
            return {
                "file": file,
                "source": "",
                "truncated": False,
                "reason": "line_range must satisfy 1 <= start <= end",
            }
        if requested_start > len(lines):
            return {
                "file": file,
                "source": "",
                "truncated": False,
                "reason": f"line_range starts beyond end of file ({len(lines)} lines)",
            }
    else:
        requested_start, requested_end = 1, len(lines)

    padding = max(0, min(int(context), 100))
    limit = max(1, min(int(max_lines), MAX_SOURCE_LINES))
    start = max(1, requested_start - padding)
    desired_end = min(len(lines), requested_end + padding)
    end = min(desired_end, start + limit - 1)
    key = (str(Path(root).resolve()), file, start, end)
    if key not in _source_cache:
        _source_cache[key] = "\n".join(lines[start - 1:end])

    return {
        "file": file,
        "around_symbol": around_symbol,
        "requested_range": [requested_start, requested_end],
        "start_line": start,
        "end_line": end,
        "source": _source_cache[key],
        "truncated": end < desired_end,
    }


def _top_level_structure(source: str) -> set[tuple[str, str]] | None:
    symbols = ast_parser.parse_symbols(source)
    if symbols is None:
        return None
    return {(symbol.name, symbol.kind) for symbol in symbols if "." not in symbol.name}


def _structure_label(items: set[tuple[str, str]]) -> str:
    ordered = sorted(items)
    shown = ", ".join(f"{name} ({kind})" for name, kind in ordered[:MAX_SUMMARY_ITEMS])
    remaining = len(ordered) - MAX_SUMMARY_ITEMS
    return f"{shown}, ... (+{remaining} more)" if remaining > 0 else shown


def find_siblings(root: str, base: str, file: str, cap: int = 5) -> dict:
    """Find structurally comparable Python files using simple path heuristics."""
    source, error = _read_repo_file(root, file)
    if source is None:
        return {"file": file, "siblings": [], "total_candidates": 0, "truncated": False, "reason": error}
    if not file.endswith(".py"):
        return {
            "file": file,
            "siblings": [],
            "total_candidates": 0,
            "truncated": False,
            "reason": "find_siblings currently compares Python symbols only",
        }
    own_structure = _top_level_structure(source)
    if own_structure is None:
        return {
            "file": file,
            "siblings": [],
            "total_candidates": 0,
            "truncated": False,
            "reason": f"failed to parse Python symbols from {file}",
        }

    target = Path(file)
    target_parent = target.parent.as_posix()
    target_service = change_graph._service_root(file)
    candidates: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ast_parser.SKIP_DIRS)
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, filename), root)
            if rel == file:
                continue
            rel_path = Path(rel)
            if rel_path.name == target.name and rel_path.parent.as_posix() != target_parent:
                candidates[rel] = "same_basename"
            elif rel_path.parent.as_posix() == target_parent:
                candidates[rel] = "same_directory"

    ordered = sorted(
        candidates.items(),
        key=lambda item: (
            0 if item[1] == "same_basename" else 1,
            0 if change_graph._service_root(item[0]) == target_service else 1,
            item[0],
        ),
    )
    limit = max(1, min(int(cap), MAX_QUERY_CAP))
    siblings: list[dict] = []
    parse_failures = 0
    for sibling_file, relationship in ordered:
        sibling_source, _ = _read_repo_file(root, sibling_file)
        if sibling_source is None:
            continue
        sibling_structure = _top_level_structure(sibling_source)
        if sibling_structure is None:
            parse_failures += 1
            continue
        shared = own_structure & sibling_structure
        sibling_only = sibling_structure - own_structure
        this_only = own_structure - sibling_structure
        differences: list[str] = []
        if sibling_only:
            differences.append(f"sibling defines {_structure_label(sibling_only)}; this file doesn't")
        if this_only:
            differences.append(f"this file defines {_structure_label(this_only)}; sibling doesn't")
        if not differences:
            differences.append("top-level symbol names and kinds match")
        siblings.append(
            {
                "file": sibling_file,
                "relationship": relationship,
                "service_root": change_graph._service_root(sibling_file),
                "shared_structure": [
                    {"name": name, "kind": symbol_kind}
                    for name, symbol_kind in sorted(shared)[:MAX_STRUCTURE_ITEMS]
                ],
                "shared_structure_total": len(shared),
                "shared_structure_truncated": len(shared) > MAX_STRUCTURE_ITEMS,
                "differences_summary": "; ".join(differences),
            }
        )

    total = len(siblings)
    result = {
        "file": file,
        "base": base,
        "service_root": target_service,
        "siblings": siblings[:limit],
        "total_candidates": total,
        "truncated": total > limit,
    }
    if not siblings:
        suffix = f"; {parse_failures} candidate(s) could not be parsed" if parse_failures else ""
        result["reason"] = f"no comparable Python siblings found for {file}{suffix}"
    return result


def _is_excluded(rel_path: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(Path(rel_path).name, pattern)
        for pattern in patterns
    )


def grep_repo(
    root: str,
    pattern: str,
    langs: list[str] | None = None,
    exclude: list[str] | None = None,
    path: str | None = None,
    cap: int = 50,
) -> dict:
    """Regex-search repository text files while retaining the true match count."""
    root = str(Path(root).resolve())
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return {"matches": [], "total_matches": 0, "truncated": False, "reason": f"invalid regex: {exc}"}

    if path:
        candidate = Path(root, path).resolve()
        root_path = Path(root)
        try:
            candidate.relative_to(root_path)
        except ValueError:
            return {"matches": [], "total_matches": 0, "truncated": False,
                    "reason": "path must be a normalized path inside the repository"}
        if not candidate.exists():
            return {"matches": [], "total_matches": 0, "truncated": False,
                    "reason": f"search path does not exist: {path}"}
        search_root = str(candidate)
    else:
        search_root = root
    requested_langs = langs or []
    unknown_langs = sorted(set(requested_langs) - set(_LANG_EXTENSIONS))
    if unknown_langs:
        return {
            "matches": [],
            "total_matches": 0,
            "truncated": False,
            "reason": f"unsupported langs: {', '.join(unknown_langs)}",
        }
    extensions = set().union(*(_LANG_EXTENSIONS[lang] for lang in requested_langs)) if requested_langs else None
    excludes = exclude or []
    limit = max(1, min(int(cap), MAX_QUERY_CAP))
    matches: list[dict] = []
    total = 0

    for dirpath, dirnames, filenames in os.walk(search_root):
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            rel_dir = os.path.relpath(os.path.join(dirpath, dirname), root)
            if dirname in ast_parser.SKIP_DIRS or _is_excluded(rel_dir, excludes):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            abs_file = os.path.join(dirpath, filename)
            rel_file = os.path.relpath(abs_file, root)
            if _is_excluded(rel_file, excludes):
                continue
            if extensions is not None and Path(filename).suffix.lower() not in extensions:
                continue
            try:
                with open(abs_file, "r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not regex.search(line):
                            continue
                        total += 1
                        if len(matches) < limit:
                            matches.append(
                                {"file": rel_file, "line": line_number, "snippet": line.rstrip("\r\n")[:300]}
                            )
            except (OSError, UnicodeDecodeError):
                continue

    result = {"matches": matches, "total_matches": total, "truncated": total > limit,
              "path": path or "."}
    if total == 0:
        result["reason"] = "pattern did not match any searched repository line"
    return result


def _normalized(text: str) -> str:
    return " ".join(text.split())


def verify_anchor(root: str, file: str, line: int, expected_snippet: str) -> dict:
    """Verify a finding's source anchor and suggest a nearby corrected line."""
    source, error = _read_repo_file(root, file)
    if source is None:
        return {"ok": False, "actual": None, "suggested_line": None, "reason": error}
    return _verify_anchor_text(source, line, expected_snippet)


def _verify_anchor_text(source: str, line: int, expected_snippet: str) -> dict:
    """Verify one line against already-pinned source text."""
    if not expected_snippet or not _normalized(expected_snippet):
        return {
            "ok": False,
            "actual": None,
            "suggested_line": None,
            "reason": "expected_snippet must not be empty",
        }
    if "\n" in expected_snippet:
        return {
            "ok": False,
            "actual": None,
            "suggested_line": None,
            "reason": "expected_snippet must be exactly one source line; use context separately",
        }

    lines = source.splitlines()
    try:
        requested_line = int(line)
    except (TypeError, ValueError):
        return {"ok": False, "actual": None, "suggested_line": None, "reason": "line must be an integer"}

    actual = lines[requested_line - 1] if 1 <= requested_line <= len(lines) else None
    expected_normalized = _normalized(expected_snippet)
    if actual is not None and _normalized(actual) == expected_normalized:
        return {"ok": True, "actual": actual, "suggested_line": None}

    low = max(1, requested_line - 15)
    high = min(len(lines), requested_line + 15)
    nearby = [
        candidate_line
        for candidate_line in range(low, high + 1)
        if expected_normalized in _normalized(lines[candidate_line - 1])
    ]
    suggested = min(nearby, key=lambda candidate: (abs(candidate - requested_line), candidate)) if nearby else None
    result = {"ok": False, "actual": actual, "suggested_line": suggested}
    if suggested is None:
        result["reason"] = "expected snippet was not found at the cited line or within +/-15 lines"
    return result


def verify_anchors(root: str, findings: list[dict]) -> dict:
    """Verify a batch of one-line finding anchors in one deterministic call."""
    results = []
    for index, finding in enumerate(findings):
        anchor = finding.get("changed_cause_anchor", finding)
        snippet = anchor.get("anchor_snippet", anchor.get("snippet", ""))
        result = verify_anchor(root, anchor.get("file", ""), anchor.get("line", 0), snippet)
        results.append({"index": index, "file": anchor.get("file"), "line": anchor.get("line"), **result})
    return {"results": results, "returned_items": len(results), "total_items": len(findings)}


def verify_changed_anchors(root: str, base: str, findings: list[dict], *, head: str = "HEAD",
                           source_root: str | None = None) -> dict:
    """Verify old/new-side anchors against the pinned diff endpoints.

    ``changed_cause_anchor.side='old'`` permits a cause anchored to deleted code. The
    default remains the current/new side. Affected-site validation continues
    to use committed head source.
    """
    checked: list[dict] = []
    hunks = git_analyzer.get_diff_hunks(root, base, head=head)
    for index, finding in enumerate(findings):
        anchor = finding.get("changed_cause_anchor", {})
        side = anchor.get("side", "new")
        file = anchor.get("file", "")
        line = anchor.get("line", 0)
        snippet = anchor.get("anchor_snippet", anchor.get("snippet", ""))
        if side not in {"old", "new"}:
            result = {"ok": False, "actual": None, "suggested_line": None,
                      "reason_code": "source-anchor-invalid", "reason": "anchor side must be 'old' or 'new'"}
        elif side == "old":
            prior = git_analyzer.read_file_at(root, base, file)
            if prior is None:
                result = {"ok": False, "actual": None, "suggested_line": None,
                          "reason_code": "source-anchor-invalid",
                          "reason": "file is absent or unreadable at the pinned diff base"}
            else:
                result = _verify_anchor_text(prior, line, snippet)
        else:
            result = verify_anchor(source_root or root, file, line, snippet)
        if not result.get("ok"):
            result["reason_code"] = "source-anchor-invalid"
        else:
            changed_ranges = hunks.get(file, [])
            is_changed = any(
                (hunk.old_len and hunk.old_start <= line < hunk.old_start + hunk.old_len)
                if side == "old"
                else (hunk.new_len and hunk.new_start <= line < hunk.new_start + hunk.new_len)
                for hunk in changed_ranges
            )
            if not is_changed:
                result.update(ok=False, reason_code="diff-anchor-invalid",
                              reason="cause source exists but is outside the authoritative diff",
                              suggested_line=None)
        cause_result = {"file": file, "line": line, "side": side, **result}
        affected_results: list[dict] = []
        for site in finding.get("affected_site_anchors", []):
            if not isinstance(site, dict):
                affected_results.append({"ok": False, "reason_code": "affected-source-invalid",
                                         "reason": "affected site must be an object"})
                continue
            site_file, site_line = site.get("file", ""), site.get("line", 0)
            site_snippet = site.get("anchor_snippet", site.get("snippet", ""))
            if site_snippet:
                site_result = verify_anchor(source_root or root, site_file, site_line, site_snippet)
            else:
                site_source, site_error = _read_repo_file(source_root or root, site_file)
                try:
                    site_line_number = int(site_line)
                except (TypeError, ValueError):
                    site_line_number = 0
                line_count = len(site_source.splitlines()) if site_source is not None else 0
                site_result = (
                    {"ok": True, "actual": site_source.splitlines()[site_line_number - 1], "suggested_line": None}
                    if 1 <= site_line_number <= line_count
                    else {"ok": False, "actual": None, "suggested_line": None,
                          "reason": site_error or "affected-site line is outside pinned head source"}
                )
            affected_results.append({"file": site_file, "line": site_line, **site_result})
        for site_result in affected_results:
            if not site_result.get("ok"):
                site_result["reason_code"] = "affected-source-invalid"
        affected_ok = all(site.get("ok") for site in affected_results)
        overall_code = cause_result.get("reason_code") if not cause_result.get("ok") else (
            "affected-source-invalid" if not affected_ok else None
        )
        checked.append({
            "index": index, "ok": bool(cause_result.get("ok") and affected_ok),
            "reason_code": overall_code,
            "reason": cause_result.get("reason") if not cause_result.get("ok") else (
                "one or more affected sites are absent from pinned head source" if not affected_ok else None
            ),
            "cause_anchor_result": cause_result, "affected_site_results": affected_results,
        })
    return {"results": checked, "returned_items": len(checked), "total_items": len(findings),
            "base": base, "batch": True}

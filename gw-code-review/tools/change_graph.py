"""Symbol-level change graph: what changed, and what depends on it.

Three tools, split from the old eager structural-impact dump:

  list_changes()      -- what changed (M1). Diffs the current and prior AST
                          of every changed .py file, classified against the
                          diff's own hunk ranges so only symbols the diff
                          actually touched are reported (not every symbol in
                          a touched file, which is what made the old tool's
                          payload 329x the size of the diff it described).
  trace()              -- who's affected (M3). A directional, depth-bounded
                          walk from one symbol: direction="in" is blast
                          radius (who calls this), direction="out" is what
                          this calls. Depth >1 is genuine dependency
                          exploration -- nothing else in this server can do
                          that at all.
  change_coherence()   -- the verdict (M4). The intersection of the change
                          set with the dependency graph: callers that didn't
                          move when a signature changed, references left
                          dangling by a deletion, new symbols no test
                          mentions, and blast radius crossing a service
                          boundary. Deterministic output, not raw data to
                          reason over.

Python-only for now (see the `reason` field list_changes/change_coherence
emit on a non-Python diff) -- multi-language symbol extraction is a later
phase; the reference-finding regex layer underneath is already
language-agnostic since it operates on raw text, not an AST.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import ast_parser, extractors, git_analyzer, schema_diff

MAX_CHANGES = 500
MAX_TRACE_NODES = 60

# Directories that hold an app/service name as their next path segment,
# rather than being a service boundary themselves -- "apps/billing/x.py"
# and "apps/recommender/y.py" should compare as different services, not as
# the same one just because both start with "apps".
_CONTAINER_DIRS = {"apps", "services", "packages", "modules", "libs"}

_CONFIG_MARKERS = ("settings.py", "conftest.py", "/config/", "config.py", "pyproject.toml")
_CONFIG_SUFFIXES = (".yaml", ".yml", ".env")
_DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt")
_DOC_BASENAMES = {"readme", "changelog", "contributing", "architecture", "runbook"}

_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|tests?/.*\.py)$")

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------

@dataclass
class _FileSymbols:
    current: dict[str, ast_parser.Symbol]
    prior: dict[str, ast_parser.Symbol]


_cache: dict[tuple[str, str, str], dict] = {}


def _computed(root: str, base: str, head: str = "HEAD", source_root: str | None = None,
              cache_key: str | None = None) -> dict:
    """Memoized per (root, base): changed files, diff hunks, and current/prior
    symbol tables for every changed .py file.

    list_changes() and change_coherence() both need this; computing it once
    per review instead of once per tool call is the I6 (deterministic and
    cached) invariant in practice.
    """
    source_root = source_root or root
    key = (os.path.abspath(root), cache_key or base, head)
    if key in _cache:
        return _cache[key]

    changed_files = git_analyzer.get_changed_files(root=root, base=base, head=head)
    hunks = git_analyzer.get_diff_hunks(root=root, base=base, head=head)
    py_files = [f for f in changed_files if f.endswith(".py")]

    file_symbols: dict[str, _FileSymbols] = {}
    parse_errors: list[dict] = []

    for rel_path in py_files:
        current_source = _read_file(os.path.join(source_root, rel_path))
        prior_source = schema_diff._get_file_history(root, rel_path, base) or None

        current_symbols = ast_parser.parse_symbols(current_source) if current_source is not None else None
        prior_symbols = ast_parser.parse_symbols(prior_source) if prior_source is not None else None

        if current_source is not None and current_symbols is None:
            parse_errors.append({"file": rel_path, "error": "failed to parse current version"})
        if prior_source is not None and prior_symbols is None:
            parse_errors.append({"file": rel_path, "error": "failed to parse prior version"})

        file_symbols[rel_path] = _FileSymbols(
            current={s.name: s for s in (current_symbols or [])},
            prior={s.name: s for s in (prior_symbols or [])},
        )

    result = {
        "changed_files": changed_files,
        "hunks": hunks,
        "file_symbols": file_symbols,
        "parse_errors": parse_errors,
    }
    _cache[key] = result
    return result


def _read_file(abs_path: str) -> str | None:
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            source = f.read()
            return None if "\x00" in source else source
    except (OSError, UnicodeDecodeError):
        return None


def _service_root(rel_path: str) -> str:
    """First path segment, or the first two when the first is a generic
    container directory (apps/, services/, ...), so the comparison lands on
    the actual service/app name rather than the shared container.
    """
    parts = Path(rel_path).parts
    if not parts:
        return ""
    if parts[0] in _CONTAINER_DIRS and len(parts) > 1:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def _crosses_boundary(file_a: str | None, file_b: str | None) -> bool:
    if not file_a or not file_b:
        return False
    return _service_root(file_a) != _service_root(file_b)


def _is_config_file(rel_path: str) -> bool:
    lowered = rel_path.lower()
    return any(marker in lowered for marker in _CONFIG_MARKERS) or lowered.endswith(_CONFIG_SUFFIXES)


def _is_documentation_file(rel_path: str) -> bool:
    """Recognize documentation files without treating arbitrary prose as code."""
    path = Path(rel_path)
    stem = path.stem.lower()
    return (
        path.suffix.lower() in _DOC_SUFFIXES
        or stem in _DOC_BASENAMES
        or any(part.lower() in {"docs", "doc", "runbooks"} for part in path.parts)
    )


def _docstring_ranges(source: str | None) -> list[tuple[int, int]]:
    """Return source spans for module/class/function docstrings, if parseable."""
    if source is None:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(getattr(first, "value", None), ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ranges.append((first.lineno, getattr(first, "end_lineno", first.lineno)))
    return ranges


def _file_touches_docs(
    rel_path: str,
    hunks: list,
    current_source: str | None,
    prior_source: str | None,
) -> bool:
    new_ranges = [(h.new_start, h.new_len) for h in hunks]
    old_ranges = [(h.old_start, h.old_len) for h in hunks]
    return (
        _is_documentation_file(rel_path)
        or any(_touched(new_ranges, start, end) for start, end in _docstring_ranges(current_source))
        or any(_touched(old_ranges, start, end) for start, end in _docstring_ranges(prior_source))
    )


def _enclosing_symbol(root: str, file: str | None, line: int) -> str | None:
    """The innermost function/method/class whose span contains `line`."""
    if not file:
        return None
    source = _read_file(os.path.join(root, file))
    if source is None:
        return None
    symbols = ast_parser.parse_symbols(source)
    if not symbols:
        return None
    best: ast_parser.Symbol | None = None
    for s in symbols:
        end = s.end_line or s.line
        if s.line <= line <= end:
            if best is None or (end - s.line) < ((best.end_line or best.line) - best.line):
                best = s
    return best.name if best else None


# ---------------------------------------------------------------------------
# M1 -- list_changes
# ---------------------------------------------------------------------------

def _touched(hunk_starts_lens: list[tuple[int, int]], start: int, end: int) -> bool:
    for h_start, h_len in hunk_starts_lens:
        if h_len <= 0:
            continue
        h_end = h_start + h_len - 1
        if h_start <= end and start <= h_end:
            return True
    return False


def _row(rel_path: str, sym: ast_parser.Symbol, change: str, touches_docs: bool) -> dict:
    return {
        "file": rel_path,
        "symbol": sym.name,
        "kind": sym.kind,
        "lines": [sym.line, sym.end_line or sym.line],
        "change": change,
        "is_public": not sym.name.split(".")[-1].startswith("_"),
        "touches_config": _is_config_file(rel_path),
        "touches_docs": touches_docs,
        "capability": "semantic",
    }


def _classify_file(rel_path: str, current: dict[str, ast_parser.Symbol], prior: dict[str, ast_parser.Symbol],
                    hunks: list, current_source: str | None, prior_source: str | None) -> list[dict]:
    new_ranges = [(h.new_start, h.new_len) for h in hunks]
    old_ranges = [(h.old_start, h.old_len) for h in hunks]
    touches_docs = _file_touches_docs(rel_path, hunks, current_source, prior_source)
    rows: list[dict] = []

    for name, sym in current.items():
        end = sym.end_line or sym.line
        if name not in prior:
            if _touched(new_ranges, sym.line, end):
                rows.append(_row(rel_path, sym, "added", touches_docs))
            continue
        prior_sym = prior[name]
        if prior_sym.signature != sym.signature:
            rows.append(_row(rel_path, sym, "signature_changed", touches_docs))
        elif _touched(new_ranges, sym.line, end):
            rows.append(_row(rel_path, sym, "modified", touches_docs))
        # else: untouched despite the file changing elsewhere -- omitted.

    for name, sym in prior.items():
        if name in current:
            continue
        end = sym.end_line or sym.line
        if _touched(old_ranges, sym.line, end):
            rows.append(_row(rel_path, sym, "deleted", touches_docs))

    return rows


def _classify_extracted_file(rel_path: str, current_source: str | None, prior_source: str | None,
                             hunks: list, touches_docs: bool) -> tuple[list[dict], str | None]:
    """Classify a non-Python file through the common extractor interface."""
    extractor = extractors.for_path(rel_path)
    if current_source is None and prior_source is None:
        return [], "source is unavailable or unsupported for semantic/hunk analysis"
    current = extractor.inventory(current_source)
    prior = extractor.inventory(prior_source)
    new_ranges = [(h.new_start, h.new_len) for h in hunks]
    old_ranges = [(h.old_start, h.old_len) for h in hunks]
    rows: list[dict] = []
    if extractor.capability != "semantic":
        # A bounded hunk row is useful review input, but expressly not a symbol claim.
        for index, hunk in enumerate(hunks[:20], 1):
            start = hunk.new_start if hunk.new_len else hunk.old_start
            end = start + max(hunk.new_len, hunk.old_len, 1) - 1
            rows.append({"file": rel_path, "symbol": f"<hunk {index}>", "kind": "file_hunk",
                         "lines": [start, end], "change": "modified", "is_public": None,
                         "touches_config": _is_config_file(rel_path), "touches_docs": touches_docs,
                         "capability": extractor.capability, "reason": current.reason})
        return rows, current.reason
    before = {symbol.name: symbol for symbol in prior.symbols}
    for symbol in current.symbols:
        previous = before.pop(symbol.name, None)
        if previous is None:
            change = "added"
        elif previous.signature != symbol.signature:
            change = "signature_changed"
        elif not _touched(new_ranges, symbol.line, symbol.end_line):
            continue
        else:
            change = "modified"
        if change == "signature_changed" or _touched(new_ranges, symbol.line, symbol.end_line):
            rows.append({"file": rel_path, "symbol": symbol.name, "kind": symbol.kind,
                         "lines": [symbol.line, symbol.end_line], "change": change,
                         "is_public": not symbol.name.startswith("_"), "touches_config": _is_config_file(rel_path),
                         "touches_docs": touches_docs, "capability": current.capability, "reason": current.reason})
    for symbol in before.values():
        if _touched(old_ranges, symbol.line, symbol.end_line):
            rows.append({"file": rel_path, "symbol": symbol.name, "kind": symbol.kind,
                         "lines": [symbol.line, symbol.end_line], "change": "deleted",
                         "is_public": not symbol.name.startswith("_"), "touches_config": _is_config_file(rel_path),
                         "touches_docs": touches_docs, "capability": current.capability, "reason": current.reason})
    return rows, current.reason


def _file_capability(rel_path: str, current_source: str | None, prior_source: str | None,
                     parse_errors: list[dict]) -> tuple[str, str | None]:
    if rel_path.endswith(".py"):
        error = next((item["error"] for item in parse_errors if item["file"] == rel_path), None)
        return ("hunk-only", error) if error else ("semantic", None)
    if ((current_source is None and prior_source is None)
            or any("\x00" in source for source in (current_source, prior_source) if source)):
        return "unsupported", "source is unavailable or unsupported for semantic/hunk analysis"
    extraction = extractors.for_path(rel_path).inventory(current_source if current_source is not None else prior_source)
    return extraction.capability, extraction.reason


def list_changes(root: str, base: str, scope: str | None = None, kind: str | None = None,
                 *, head: str = "HEAD", source_root: str | None = None,
                 cache_key: str | None = None) -> dict:
    """Symbol-level change inventory: what changed, not what it touches.

    `scope` filters to files whose path starts with the given prefix.
    `kind` filters to one of function|async_function|class|method.
    """
    source_root = source_root or root
    data = _computed(root, base, head=head, source_root=source_root, cache_key=cache_key)
    rows: list[dict] = []
    documentation_files: list[str] = []
    extraction_reasons: list[dict] = []
    file_capabilities: list[dict] = []
    for rel_path in data["changed_files"]:
        current_source = _read_file(os.path.join(source_root, rel_path))
        prior_source = schema_diff._get_file_history(root, rel_path, base)
        file_hunks = data["hunks"].get(rel_path, [])
        capability, capability_reason = _file_capability(rel_path, current_source, prior_source, data["parse_errors"])
        file_capabilities.append({"file": rel_path, "capability": capability, "reason": capability_reason})
        touches_docs = _file_touches_docs(rel_path, file_hunks, current_source, prior_source)
        if touches_docs:
            documentation_files.append(rel_path)
        if rel_path.endswith(".py"):
            syms = data["file_symbols"].get(rel_path, _FileSymbols({}, {}))
            rows.extend(_classify_file(rel_path, syms.current, syms.prior, file_hunks, current_source, prior_source))
        else:
            extracted, reason = _classify_extracted_file(rel_path, current_source, prior_source, file_hunks, touches_docs)
            rows.extend(extracted)
            if reason:
                extraction_reasons.append({"file": rel_path, "reason": reason})
    documentation_files.extend(
        f for f in data["changed_files"] if not f.endswith(".py") and _is_documentation_file(f)
    )

    if scope:
        rows = [r for r in rows if r["file"].startswith(scope)]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]

    total = len(rows)
    truncated = total > MAX_CHANGES
    rows = rows[:MAX_CHANGES]

    result = {
        "changed_files": data["changed_files"],
        "changes": rows,
        "total": total,
        "truncated": truncated,
        "parse_errors": data["parse_errors"],
        "non_python_files_skipped": [],  # retained for compatibility; portable extractors now cover these files
        "documentation_files": documentation_files,
        "capability": ("semantic" if rows and all(r.get("capability") == "semantic" for r in rows)
                       else "hunk-only" if rows else "unsupported"),
        "file_capabilities": file_capabilities,
        "extraction_reasons": extraction_reasons,
        "found": bool(rows),
    }

    if not rows:
        if not data["changed_files"]:
            result["reason"] = "no changed files against base"
        elif not any(f.endswith(".py") for f in data["changed_files"]):
            result["reason"] = "no bounded changed hunks could be extracted from the non-Python diff"
        else:
            result["reason"] = "changed .py files parsed cleanly but no symbol spans were touched by the diff"

    return result


# ---------------------------------------------------------------------------
# M3 -- trace
# ---------------------------------------------------------------------------

def _resolve_current_symbol(root: str, file: str, name: str) -> ast_parser.Symbol | None:
    source = _read_file(os.path.join(root, file))
    if source is None:
        return None
    symbols = ast_parser.parse_symbols(source)
    if not symbols:
        return None
    return next((s for s in symbols if s.name == name), None)


def _trace_in_batch(root: str, targets: list[tuple[str, str]]) -> dict[tuple[str, str], list[dict]]:
    """One combined repo-wide grep pass for every (file, name) target at this
    depth level -- not one pass per symbol, which is exactly the subprocess
    fan-out ast_parser.attach_references's own docstring was written to
    eliminate.
    """
    pairs: list[tuple[str, ast_parser.Symbol]] = []
    for file, name in targets:
        sym = _resolve_current_symbol(root, file, name)
        if sym is not None:
            pairs.append((file, sym))

    if pairs:
        ast_parser.attach_references(pairs, root)

    out: dict[tuple[str, str], list[dict]] = {}
    for file, sym in pairs:
        out[(file, sym.name)] = [
            {
                "file": ref.file,
                "line": ref.line,
                "snippet": ref.snippet,
                "resolved_via": ref.match_kind,
                "confidence": ref.confidence,
                "owning_module": ref.owning_module,
                "supporting_source_span": list(ref.source_span or (ref.line, ref.line)),
                "crosses_service_boundary": _crosses_boundary(file, ref.file),
                "enclosing_symbol": _enclosing_symbol(root, ref.file, ref.line),
            }
            for ref in sym.references
        ]
    return out


def _callee_name(call: ast.Call) -> tuple[str | None, str]:
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in ("delay", "apply_async") and isinstance(func.value, (ast.Name, ast.Attribute)):
            base = func.value.id if isinstance(func.value, ast.Name) else func.value.attr
            return base, "celery_dispatch"
        return func.attr, "call"
    if isinstance(func, ast.Name):
        return func.id, "call"
    return None, "call"


def _find_definitions(root: str, name: str, cap: int = 3) -> list[tuple[str, int]]:
    if len(name) < 3 or name in ast_parser.EXCLUDE_BARE_NAMES:
        return []
    pattern = re.compile(r"^\s*(?:async\s+def|def|class)\s+" + re.escape(name) + r"\b")
    hits: list[tuple[str, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ast_parser.SKIP_DIRS]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            abs_file = os.path.join(dirpath, filename)
            rel_file = os.path.relpath(abs_file, root)
            try:
                with open(abs_file, "r", encoding="utf-8") as f:
                    for line_num, line in enumerate(f, start=1):
                        if pattern.match(line):
                            hits.append((rel_file, line_num))
                            if len(hits) >= cap:
                                return hits
            except (OSError, UnicodeDecodeError):
                continue
    return hits


def _trace_out_one(root: str, file: str, name: str) -> list[dict]:
    source = _read_file(os.path.join(root, file))
    if source is None:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    target = _resolve_current_symbol(root, file, name)
    if target is None:
        return []

    node = _find_node(tree, target.name, target.line)
    if node is None:
        return []

    hits: list[dict] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        callee, kind = _callee_name(call)
        if not callee:
            continue
        definitions = _find_definitions(root, callee)
        if not definitions:
            hits.append({
                "file": None, "line": call.lineno, "snippet": callee,
                "resolved_via": kind, "crosses_service_boundary": False, "enclosing_symbol": None,
            })
            continue
        for def_file, def_line in definitions:
            hits.append({
                "file": def_file, "line": def_line, "snippet": callee,
                "resolved_via": kind,
                "crosses_service_boundary": _crosses_boundary(file, def_file),
                "enclosing_symbol": callee,
            })
    return hits


def _find_node(tree: ast.Module, name: str, line: int) -> ast.AST | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name and node.lineno == line:
            return node
        if isinstance(node, ast.ClassDef):
            if node.name == name and node.lineno == line:
                return node
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if f"{node.name}.{item.name}" == name and item.lineno == line:
                        return item
    return None


def trace(root: str, symbol: str, direction: str = "in", depth: int = 1,
          cross_boundary_only: bool = False, min_confidence: str | None = None) -> dict:
    """Directional, depth-bounded dependency walk from one symbol.

    Operates on the current tree, not a diff -- "who calls this" is a
    question about the repo as it stands, independent of what changed. No
    `base` parameter for that reason (unlike list_changes/change_coherence).

    `symbol` must be qualified as "path/to/file.py:Name" (dotted for a
    method, e.g. "app/serializers.py:ListingSerializer.validate") -- exactly
    the (file, symbol) pair list_changes() returns per row. Bare-name lookup
    across the whole repo arrives with find_symbol in a later phase; for now
    resolve the file yourself from list_changes()'s output first.

    direction="in"  -- who calls/references this symbol (blast radius).
    direction="out" -- what this symbol calls (its own dependencies).
    depth 2-3 recurses onto each hit's enclosing symbol.
    """
    if direction not in ("in", "out"):
        return {"error": f"direction must be 'in' or 'out', got {direction!r}",
                 "hits": [], "total": 0, "truncated": False}
    depth = max(1, min(int(depth), 3))

    try:
        file, name = symbol.split(":", 1)
    except ValueError:
        return {"error": "symbol must be 'path/to/file.py:Name' (see list_changes output)",
                 "hits": [], "total": 0, "truncated": False}

    seen: set[tuple[str, str]] = {(file, name)}
    frontier: list[tuple[str, str]] = [(file, name)]
    hits: list[dict] = []

    for level in range(1, depth + 1):
        if not frontier or len(hits) >= MAX_TRACE_NODES:
            break

        if direction == "in":
            level_map = _trace_in_batch(root, frontier)
        else:
            level_map = {t: _trace_out_one(root, t[0], t[1]) for t in frontier}

        next_frontier: list[tuple[str, str]] = []
        for target_hits in level_map.values():
            for h in target_hits:
                h["depth"] = level
                hits.append(h)
                enclosing = h.get("enclosing_symbol")
                if enclosing and h.get("file") and (h["file"], enclosing) not in seen:
                    seen.add((h["file"], enclosing))
                    next_frontier.append((h["file"], enclosing))
        frontier = next_frontier

    if cross_boundary_only:
        hits = [h for h in hits if h.get("crosses_service_boundary")]
    if min_confidence:
        floor = _CONFIDENCE_RANK.get(min_confidence, 0)
        hits = [h for h in hits if _CONFIDENCE_RANK.get(h.get("confidence", "low"), 0) >= floor]

    total = len(hits)
    truncated = total > MAX_TRACE_NODES
    hits = hits[:MAX_TRACE_NODES]

    result = {"symbol": symbol, "direction": direction, "depth": depth,
              "hits": hits, "total": total, "truncated": truncated}
    if not hits:
        result["reason"] = f"no {'callers/references' if direction == 'in' else 'outbound calls'} found for {symbol}"
    return result


# ---------------------------------------------------------------------------
# M4 -- change_coherence
# ---------------------------------------------------------------------------

def _resolve_symbol_for_trace(data: dict, file: str, name: str, change: str) -> ast_parser.Symbol | None:
    syms = data["file_symbols"].get(file)
    if not syms:
        return None
    # A deleted symbol no longer exists in the current tree, so its
    # (possibly now-stale) prior span is the only one we have to search
    # from; a signature-changed symbol still exists, so use its current one.
    return syms.prior.get(name) if change == "deleted" else syms.current.get(name)


def _batch_trace_in(root: str, data: dict, targets: list[tuple[str, str, str]]) -> dict[tuple[str, str], list[dict]]:
    pairs: list[tuple[str, ast_parser.Symbol]] = []
    for file, name, change in targets:
        sym = _resolve_symbol_for_trace(data, file, name, change)
        if sym is not None:
            pairs.append((file, sym))

    if pairs:
        ast_parser.attach_references(pairs, root)

    out: dict[tuple[str, str], list[dict]] = {}
    for file, sym in pairs:
        out[(file, sym.name)] = [
            {
                "file": ref.file,
                "line": ref.line,
                "snippet": ref.snippet,
                "resolved_via": ref.match_kind,
                "confidence": ref.confidence,
                "owning_module": ref.owning_module,
                "supporting_source_span": list(ref.source_span or (ref.line, ref.line)),
                "crosses_service_boundary": _crosses_boundary(file, ref.file),
            }
            for ref in sym.references
        ]
    return out


def _names_referenced_in_tests(root: str, names: list[str]) -> set[str]:
    if not names:
        return set()
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(set(names))) + r")\b")
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ast_parser.SKIP_DIRS]
        for filename in filenames:
            rel = os.path.relpath(os.path.join(dirpath, filename), root)
            if not _TEST_FILE_RE.search(rel):
                continue
            content = _read_file(os.path.join(root, rel))
            if content:
                found.update(pattern.findall(content))
    return found


def change_coherence(root: str, base: str, *, head: str = "HEAD",
                     source_root: str | None = None, cache_key: str | None = None) -> dict:
    """The intersection of the change set with the dependency graph,
    expressed as verdicts rather than raw data to reason over.
    """
    source_root = source_root or root
    data = _computed(root, base, head=head, source_root=source_root, cache_key=cache_key)
    changes = list_changes(root, base, head=head, source_root=source_root, cache_key=cache_key)["changes"]
    changed_files = set(data["changed_files"])

    python_changes = [c for c in changes if c.get("capability") == "semantic"]
    risky = [(c["file"], c["symbol"], c["change"]) for c in python_changes if c["change"] in ("signature_changed", "deleted")]
    refs_by_target = _batch_trace_in(source_root, data, risky)

    unchanged_dependents: list[dict] = []
    orphaned_references: list[dict] = []
    cross_boundary_impact: list[dict] = []
    low_confidence_leads: list[dict] = []

    for file, name, change in risky:
        for hit in refs_by_target.get((file, name), []):
            entry = {
                "symbol": name, "file": file, "change": change,
                "caller_file": hit["file"], "caller_line": hit["line"], "caller_snippet": hit["snippet"],
                "resolved_via": hit["resolved_via"], "confidence": hit.get("confidence", "low"),
                "owning_module": hit.get("owning_module"), "supporting_source_span": hit.get("supporting_source_span"),
            }
            if hit.get("confidence") != "high":
                low_confidence_leads.append(entry)
                continue
            if change == "deleted":
                orphaned_references.append(entry)
            elif hit["file"] not in changed_files:
                unchanged_dependents.append(entry)
            if hit.get("crosses_service_boundary"):
                cross_boundary_impact.append(entry)

    added_rows = [c for c in python_changes if c["change"] == "added" and c["kind"] in ("function", "async_function", "method")]
    candidate_names = sorted({c["symbol"].split(".")[-1] for c in added_rows if len(c["symbol"].split(".")[-1]) >= 3})
    tested_names = _names_referenced_in_tests(source_root, candidate_names)
    untested_new_symbols = [
        {"symbol": c["symbol"], "file": c["file"], "line": c["lines"][0]}
        for c in added_rows
        if c["symbol"].split(".")[-1] not in tested_names
    ]

    def unique(rows: list[dict]) -> list[dict]:
        by_edge = {(row.get("file"), row.get("symbol"), row.get("caller_file"), row.get("caller_line")): row for row in rows}
        return [by_edge[key] for key in sorted(by_edge, key=str)]

    result = {
        "unchanged_dependents": unique(unchanged_dependents),
        "orphaned_references": unique(orphaned_references),
        "low_confidence_leads": unique(low_confidence_leads),
        "untested_new_symbols": untested_new_symbols,
        "cross_boundary_impact": cross_boundary_impact,
        "found": bool(unchanged_dependents or orphaned_references or untested_new_symbols or cross_boundary_impact),
        "capability": "python_semantic" if python_changes else "limited",
    }
    if not result["found"]:
        result["reason"] = ("no Python semantic symbols were available for coherence analysis"
                            if changes and not python_changes else
                            "no signature/deletion blast radius, untested new symbols, or cross-boundary references found in this diff")
    return result


if __name__ == "__main__":
    import json
    import sys

    root_arg = sys.argv[1] if len(sys.argv) > 1 else "."
    base_arg = sys.argv[2] if len(sys.argv) > 2 else "master"

    print(f"[change_graph] root={root_arg}, base={base_arg}\n")

    print("== list_changes ==")
    lc = list_changes(root_arg, base_arg)
    print(json.dumps(lc, indent=2)[:4000])

    print("\n== change_coherence ==")
    cc = change_coherence(root_arg, base_arg)
    print(json.dumps(cc, indent=2)[:4000])

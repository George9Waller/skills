"""Extract Python symbols from changed files and find cross-references via grep.

This uses AST parsing to find function/class/method definitions, then greps the
repo for plausible string-keyed references (Django ForeignKey('app.Model'),
reverse('url-name'), Celery task names). Grep-based cross-referencing is
deliberate here: a full import-graph would miss string-keyed edges, which are
exactly the highest-consequence ones in a Django monolith. The tool prioritizes
correctness (no false negatives on Django string refs) over false-positive noise
(common names like 'get' will match a lot, but only if they're quoted or
Celery-adjacent — see EXCLUDE_BARE_NAMES).
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass


# Symbols too common to match bare (but still useful quoted or Celery-dispatched).
# Monorepo has tons of generic names; exclude them from bare matching.
EXCLUDE_BARE_NAMES = {
    "__init__",
    "__str__",
    "__repr__",
    "get",
    "save",
    "delete",
    "create",
    "update",
    "client",
    "models",
    "views",
    "utils",
    "config",
    "settings",
}

# Directories to skip during grep traversal.
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build", ".venv", ".env"}

# Max references per symbol to keep the output sane.
MAX_REFS_PER_SYMBOL = 25


@dataclass
class SymbolRef:
    file: str
    line: int
    snippet: str
    match_kind: str  # resolution method, not a proof for low-confidence leads
    confidence: str = "low"
    owning_module: str | None = None
    source_span: tuple[int, int] | None = None


@dataclass
class Symbol:
    name: str
    kind: str  # "function" | "async_function" | "class" | "method"
    line: int
    end_line: int | None
    references: list[SymbolRef]
    references_truncated: bool = False
    signature: str = ""  # args/decorators/bases fingerprint — see parse_symbols


def parse_symbols(source: str) -> list[Symbol] | None:
    """Parse Python source text into top-level function/class/method symbols.

    Pure function over source text, not a file path — the same code path
    parses either the working-tree copy of a file or a `git show
    base:<path>` blob, which is what change_graph.py needs to classify a
    symbol as added/modified/deleted/signature_changed by comparing the two.
    Returns None on a syntax error.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    symbols: list[Symbol] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_function_symbol(node))
        elif isinstance(node, ast.ClassDef):
            symbols.append(_class_symbol(node))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_function_symbol(item, prefix=node.name))

    return symbols


def _function_symbol(node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str | None = None) -> Symbol:
    is_async = isinstance(node, ast.AsyncFunctionDef)
    if prefix:
        kind = "async_function" if is_async else "method"
    else:
        kind = "async_function" if is_async else "function"
    return Symbol(
        name=f"{prefix}.{node.name}" if prefix else node.name,
        kind=kind,
        line=node.lineno,
        end_line=node.end_lineno,
        references=[],
        signature=_function_signature(node),
    )


def _class_symbol(node: ast.ClassDef) -> Symbol:
    return Symbol(
        name=node.name,
        kind="class",
        line=node.lineno,
        end_line=node.end_lineno,
        references=[],
        signature=_class_signature(node),
    )


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """A comparable fingerprint of a function's args/decorators/return type.

    Deliberately excludes the body: this string differing between two
    versions of the same symbol is exactly what change_graph.py treats as
    "signature_changed" rather than a plain "modified" (body-only change).
    """
    try:
        parts = [ast.unparse(node.args)]
        if node.returns is not None:
            parts.append(f"-> {ast.unparse(node.returns)}")
        parts.extend(f"@{ast.unparse(d)}" for d in node.decorator_list)
        return " ".join(parts)
    except Exception:
        return ""


def _class_signature(node: ast.ClassDef) -> str:
    try:
        parts = [ast.unparse(b) for b in node.bases]
        parts.extend(f"@{ast.unparse(d)}" for d in node.decorator_list)
        return ", ".join(parts)
    except Exception:
        return ""


def _extract_symbols(abs_path: str, repo_root: str) -> list[Symbol] | None:
    """Parse a Python file on disk and extract its function/class/method symbols.

    `repo_root` is accepted but unused — kept for call-site compatibility
    with callers that pass it alongside a path derived from it.
    """
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    return parse_symbols(source)


def attach_references(all_symbols: list[tuple[str, Symbol]], repo_root: str) -> None:
    """Find cross-references for every symbol in one repo-wide pass.

    The original design ran a separate grep subprocess per (symbol, pattern)
    pair — 3-4 subprocesses per symbol. A diff touching a handful of files
    can easily introduce several dozen symbols (one serializer with a dozen
    methods is a dozen symbols), which meant hundreds of grep invocations
    and multi-minute runs dominated by process fork/exec overhead, not
    actual searching. Instead: build one combined regex per match kind
    across every requested symbol, then walk the repo exactly once, testing
    each line against those three compiled regexes. Cost scales with repo
    size once, not with (symbols x patterns).

    Mutates each Symbol in `all_symbols` in place, setting `.references` and
    `.references_truncated`.
    """
    # First record AST-resolved calls. Regex matches below remain deliberately
    # low-confidence leads: a spelling match is not a dependency edge.
    collected = _resolved_call_references(all_symbols, repo_root)

    # bare_name / quoted_string / celery_dispatch, each name -> owning
    # (file, Symbol) pairs. Multiple symbols can share a bare name (e.g. two
    # serializers each with a `validate` method) — a matched line becomes a
    # reference for all of them, same as running the search separately for
    # each would have.
    bare_owners: dict[str, list[tuple[str, Symbol]]] = {}
    quoted_owners: dict[str, list[tuple[str, Symbol]]] = {}
    celery_owners: dict[str, list[tuple[str, Symbol]]] = {}

    for owning_file, symbol in all_symbols:
        if len(symbol.name) < 3 and symbol.name not in ("__init__", "__str__"):
            continue
        bare_name = symbol.name.split(".")[-1]

        if bare_name not in EXCLUDE_BARE_NAMES:
            bare_owners.setdefault(bare_name, []).append((owning_file, symbol))

        quoted_owners.setdefault(symbol.name, []).append((owning_file, symbol))
        quoted_owners.setdefault(bare_name, []).append((owning_file, symbol))

        if symbol.kind in ("function", "async_function"):
            celery_owners.setdefault(bare_name, []).append((owning_file, symbol))

    if not (bare_owners or quoted_owners or celery_owners):
        return

    bare_re = _alternation_regex(bare_owners, r"\b({})\b")
    quoted_re = _alternation_regex(quoted_owners, r"""['"]({})['"]""")
    celery_re = _alternation_regex(celery_owners, r"({})\s*\.(?:delay|apply_async)\s*\(")

    def record(matched_name: str, owners: dict[str, list[tuple[str, Symbol]]], match_kind: str,
               ref_file: str, line_num: int, snippet: str) -> None:
        for owning_file, symbol in owners.get(matched_name, []):
            # Skip only the symbol's own definition span, not the whole
            # file — a class referenced again later in the same file (a
            # legitimate usage) should still show up. `ref_file` and
            # `owning_file` are both already relative to repo_root here, so
            # a direct string comparison is correct without abspath: an
            # earlier version of this check ran abspath(ref_file) against
            # the cwd instead of repo_root and never matched, silently
            # letting the symbol's own def line through as a "reference".
            if ref_file == owning_file and symbol.line <= line_num <= (symbol.end_line or symbol.line):
                continue
            key = (owning_file, symbol.name)
            _append_reference(collected, key, SymbolRef(
                file=ref_file, line=line_num, snippet=snippet, match_kind=match_kind,
                confidence="medium" if match_kind == "celery_dispatch" else "low",
                owning_module=_module_name(owning_file), source_span=(line_num, line_num),
            ))

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            abs_file = os.path.join(dirpath, filename)
            rel_file = os.path.relpath(abs_file, repo_root)
            try:
                with open(abs_file, "r", encoding="utf-8") as f:
                    for line_num, line_text in enumerate(f, start=1):
                        snippet = line_text.strip()[:100]
                        if bare_re and (m := bare_re.search(line_text)):
                            record(m.group(1), bare_owners, "bare_name", rel_file, line_num, snippet)
                        if quoted_re and (m := quoted_re.search(line_text)):
                            record(m.group(1), quoted_owners, "quoted_string", rel_file, line_num, snippet)
                        if celery_re and (m := celery_re.search(line_text)):
                            record(m.group(1), celery_owners, "celery_dispatch", rel_file, line_num, snippet)
            except (OSError, UnicodeDecodeError):
                pass

    for owning_file, symbol in all_symbols:
        refs = collected.get((owning_file, symbol.name), [])
        symbol.references = refs
        # We stop collecting per-symbol at MAX_REFS_PER_SYMBOL above, so a
        # full bucket means there may have been more we didn't count —
        # report it as "truncated" rather than claiming a precise total.
        symbol.references_truncated = len(refs) >= MAX_REFS_PER_SYMBOL


def _module_name(path: str) -> str:
    """Return a portable Python module spelling for a repository-relative path."""
    without_suffix = path[:-3] if path.endswith(".py") else path
    return without_suffix.replace("/", ".").removesuffix(".__init__")


def _append_reference(collected: dict[tuple[str, str], list[SymbolRef]], key: tuple[str, str], ref: SymbolRef) -> None:
    """Deduplicate target/caller edges, retaining the strongest resolution."""
    bucket = collected.setdefault(key, [])
    for index, existing in enumerate(bucket):
        if existing.file == ref.file and existing.line == ref.line:
            ranks = {"high": 3, "medium": 2, "low": 1}
            if ranks.get(ref.confidence, 0) > ranks.get(existing.confidence, 0):
                bucket[index] = ref
            return
    if len(bucket) < MAX_REFS_PER_SYMBOL:
        bucket.append(ref)


def _resolved_call_references(all_symbols: list[tuple[str, Symbol]], repo_root: str) -> dict[tuple[str, str], list[SymbolRef]]:
    """Resolve direct/imported/qualified Python calls without treating definitions as calls.

    The resolver intentionally stops when a binding is ambiguous. Its purpose is
    dependable coherence edges, while the grep pass supplies separately labelled
    leads for an agent to investigate.
    """
    targets = {(file, symbol.name): symbol for file, symbol in all_symbols}
    by_module: dict[str, dict[str, tuple[str, Symbol]]] = {}
    for (file, name), symbol in targets.items():
        by_module.setdefault(_module_name(file), {})[name] = (file, symbol)
    collected: dict[tuple[str, str], list[SymbolRef]] = {}

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [directory for directory in dirnames if directory not in SKIP_DIRS]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            absolute = os.path.join(dirpath, filename)
            rel_file = os.path.relpath(absolute, repo_root)
            try:
                with open(absolute, "r", encoding="utf-8") as source_file:
                    source = source_file.read()
                tree = ast.parse(source)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            imports: dict[str, tuple[str, str | None]] = {}
            classes: dict[str, ast.ClassDef] = {}
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports[alias.asname or alias.name.split(".")[0]] = (alias.name, None)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        imports[alias.asname or alias.name] = (node.module, alias.name)
                elif isinstance(node, ast.ClassDef):
                    classes[node.name] = node
            parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

            def containing_class(node: ast.AST) -> ast.ClassDef | None:
                while node in parent:
                    node = parent[node]
                    if isinstance(node, ast.ClassDef):
                        return node
                return None

            def resolve(module: str, symbol_name: str) -> tuple[str, Symbol] | None:
                options = by_module.get(module, {})
                return options.get(symbol_name) or options.get(symbol_name.split(".")[-1])

            def add(target: tuple[str, Symbol] | None, call: ast.Call, method: str) -> None:
                if target is None:
                    return
                target_file, target_symbol = target
                key = (target_file, target_symbol.name)
                _append_reference(collected, key, SymbolRef(
                    file=rel_file, line=call.lineno, snippet=ast.get_source_segment(source, call) or "<call>",
                    match_kind=method, confidence="high", owning_module=_module_name(target_file),
                    source_span=(call.lineno, getattr(call, "end_lineno", call.lineno)),
                ))

            for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
                func = call.func
                if isinstance(func, ast.Name):
                    binding = imports.get(func.id)
                    if binding:
                        module, imported = binding
                        add(resolve(module, imported or func.id), call, "imported_call")
                    else:
                        add(resolve(_module_name(rel_file), func.id), call, "direct_call")
                elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    base, attr = func.value.id, func.attr
                    if base in imports:
                        module, imported = imports[base]
                        module = f"{module}.{imported}" if imported else module
                        add(resolve(module, attr), call, "qualified_call")
                    elif base in {"self", "cls"}:
                        owner = containing_class(call)
                        if owner:
                            target = resolve(_module_name(rel_file), f"{owner.name}.{attr}")
                            method = "qualified_call"
                            if target is None:
                                for parent_class in owner.bases:
                                    if isinstance(parent_class, ast.Name):
                                        target = resolve(_module_name(rel_file), f"{parent_class.id}.{attr}")
                                        if target is None and parent_class.id in imports:
                                            parent_module, imported_class = imports[parent_class.id]
                                            target = resolve(parent_module, f"{imported_class or parent_class.id}.{attr}")
                                        if target:
                                            method = "inherited_method"
                                            break
                            add(target, call, method)
                    elif base in classes:
                        add(resolve(_module_name(rel_file), f"{base}.{attr}"), call, "qualified_call")
    return collected


def _alternation_regex(owners: dict[str, list], group_template: str) -> re.Pattern | None:
    """Build one compiled regex matching any key in `owners`, longest first.

    Longest-first ordering matters for the quoted-string case: a symbol name
    like "ListingSerializer" and its bare form "get" might both be keys, and
    regex alternation matches the first alternative that fits at a given
    position — without ordering, the short "get" could shadow a longer,
    more specific match starting at the same character.
    """
    if not owners:
        return None
    names = sorted(owners.keys(), key=len, reverse=True)
    pattern = group_template.format("|".join(re.escape(n) for n in names))
    return re.compile(pattern)


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python3 ast_parser.py <file.py>")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        source = f.read()

    symbols = parse_symbols(source)
    if symbols is None:
        print("syntax error: could not parse file")
        sys.exit(1)

    print(json.dumps([
        {"name": s.name, "kind": s.kind, "line": s.line, "end_line": s.end_line, "signature": s.signature}
        for s in symbols
    ], indent=2))

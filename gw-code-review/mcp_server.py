"""gw-code-review MCP server.

Exposes deterministic, read-only context tools to Claude: a symbol-level
change graph, bounded source/repository queries, DRF/FastAPI <-> TS contract
diffing, and technology detection. All reasoning about *what these findings
mean* happens in the skill (SKILL.md) via lens subagents — this server only
computes facts.

Run standalone for a smoke test:
    python3 mcp_server.py --self-test [--root /path/to/repo] [--base master]

Register with an MCP client by pointing it at this file with `python3`.
See SETUP.md for the exact config block.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

sys.path.insert(0, str(Path(__file__).parent))

from tools import change_graph, dispatch_adapter, git_analyzer, query, review_plan, review_results, schema_diff, tech_detector, transport  # noqa: E402

# `MCPServer` is the mcp 2.x name for what used to be `FastMCP`. Import
# failure is tolerated so that --self-test still works on a checkout where
# dependencies were never installed; `_MCP_IMPORT_ERROR` keeps the real reason
# so the warning can say whether the package is missing or just incompatible.
_MCP_IMPORT_ERROR: str | None = None
try:
    from mcp.server.mcpserver import MCPServer
except ImportError as exc:  # pragma: no cover
    MCPServer = None
    _MCP_IMPORT_ERROR = str(exc)


# This skill is installed once at user level (~/.claude/skills/gw-code-review)
# and reused across every repo it's invoked in, so there's no fixed
# repo-relative path to derive a default root from — unlike a skill that
# lives inside one specific repo's .claude/, this one has no "parent repo"
# to walk up to. Default to the current working directory, same convention
# git itself uses: run it from inside the repo you want reviewed, or set
# GW_REVIEW_ROOT / --root explicitly to point elsewhere.
DEFAULT_BASE = ""  # resolved once from origin/HEAD, then deterministic fallbacks
DEFAULT_PROBE_TIMEOUT_SECONDS = 5
MAX_PROBE_TIMEOUT_SECONDS = 15
MAX_PROBE_OUTPUT_BYTES = 16_384
_base_cache: dict[tuple[str, str | None], str] = {}


class QuestionResolution(TypedDict):
    question_id: str
    outcome: Literal["resolved-no-finding", "resolved-new-candidate"]
    evidence: str
    resolving_phase: str
    candidate_id: NotRequired[str]


class HostObservation(TypedDict):
    lane_id: str
    phase: Literal["lane", "verifier"]
    model_id: str
    response_count: int
    tool_calls: list[str]
    aggregate_waits: int
    poll_wakeups: int
    rejected_tool_calls: NotRequired[int]


def _default_root() -> str:
    return os.getcwd()


def _repo_root() -> str:
    return os.environ.get("GW_REVIEW_ROOT", _default_root())


def _base_ref() -> str:
    explicit = os.environ.get("GW_REVIEW_BASE")
    key = (_repo_root(), explicit)
    if key not in _base_cache:
        base, _ = git_analyzer.resolve_base_ref(*key)
        _base_cache[key] = base or "__gw_unavailable_base__"
    return _base_cache[key]


def _bound_probe_output(output: bytes) -> str:
    """Decode and cap probe output without adding fields to its contract."""
    decoded = output.decode("utf-8", errors="replace")
    if len(decoded.encode("utf-8")) <= MAX_PROBE_OUTPUT_BYTES:
        return decoded
    marker = "\n[gw-code-review probe output truncated]\n"
    kept = MAX_PROBE_OUTPUT_BYTES - len(marker.encode("utf-8"))
    return decoded.encode("utf-8")[:kept].decode("utf-8", errors="ignore") + marker


def run_probe(
    root: str,
    command: str | list[str],
    timeout: int | float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    working_directory: str = "",
    stage: str = "pre_dispatch",
) -> dict[str, str | int | bool]:
    """Run one bounded command below the review root.

    Strings are parsed as argv with ``shlex.split`` and never run via a shell.
    Every return path has the same four-field shape so probe evidence can be
    included in a finding without leaking process metadata.
    """
    result: dict[str, str | int | bool] = {
        "stdout": "", "stderr": "", "exit_code": 2, "timed_out": False,
    }
    try:
        if stage not in {"pre_dispatch", "verification"}:
            raise ValueError("probe stage must be pre_dispatch or verification")
        root_path = Path(root).resolve(strict=True)
        relative_dir = Path(working_directory or ".")
        if relative_dir.is_absolute():
            raise ValueError("working_directory must be relative to the repository root")
        cwd = (root_path / relative_dir).resolve(strict=True)
        if not cwd.is_relative_to(root_path) or not cwd.is_dir():
            raise ValueError("working_directory must resolve to a directory below the repository root")

        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise ValueError("command must be a non-empty command string or argv list")
        requested_timeout = float(timeout)
        if requested_timeout <= 0:
            raise ValueError("timeout must be positive")
        bounded_timeout = min(requested_timeout, MAX_PROBE_TIMEOUT_SECONDS)
    except (OSError, TypeError, ValueError) as exc:
        result["stderr"] = f"invalid probe request: {exc}"
        return result

    try:
        completed = subprocess.run(
            argv, cwd=cwd, capture_output=True, timeout=bounded_timeout, check=False,
        )
        result.update(
            stdout=_bound_probe_output(completed.stdout),
            stderr=_bound_probe_output(completed.stderr),
            exit_code=completed.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        result.update(
            stdout=_bound_probe_output(exc.stdout or b""),
            stderr=_bound_probe_output(exc.stderr or b""),
            exit_code=124,
            timed_out=True,
        )
    except OSError as exc:
        result["stderr"] = f"probe could not start: {exc}"
    return result


def _contract_changes_with_reason(root: str, modified_file_paths: list[str] | None,
                                  *, base: str | None = None,
                                  source_root: str | None = None) -> dict:
    """schema_diff.get_contract_changes, plus a `reason` when all three lists
    are empty (I4: never return a bare empty) -- shared so the MCP tool and
    --self-test can't drift on this.
    """
    result = schema_diff.get_contract_changes(
        root=root, modified_file_paths=modified_file_paths,
        base=base or _base_ref(), source_root=source_root,
    )
    if not (result["serializer_changes"] or result["fastapi_route_changes"] or result["celery_task_changes"]):
        result.setdefault("reason", "no DRF serializers, FastAPI routes, or Celery tasks changed in the given files")
    return result


def _respond(value: object, endpoint: str = "mcp", review_id: str = "") -> str:
    """Serialize through the universal actual-envelope byte guard."""
    inferred = value.get("review_id", "") if isinstance(value, dict) else ""
    return transport.respond(value, endpoint=endpoint, review_id=review_id or str(inferred) or "global")


def _build_server():
    server = MCPServer("gw-code-review")

    @server.tool()
    def review_capabilities() -> str:
        """Return the running tool/policy contract before any review dispatch."""
        tools = sorted(dispatch_adapter.LANE_TOOLS | {
            "prepare_review", "get_review_bundle", "get_lane_dispatch", "get_verifier_dispatch",
            "begin_verification", "register_verifier_result", "verify_candidate_source_facts",
        })
        policy_revision = "review-policy.2026-09-25.en-1143"
        schema_hash = "sha256:" + hashlib.sha256(json.dumps({
            "schema": dispatch_adapter.LOOKUP_SCHEMA_VERSION, "tools": tools, "policy": policy_revision,
        }, sort_keys=True).encode()).hexdigest()
        return _respond({"server_build": "gw-code-review.2026-09-25", "schema_version": dispatch_adapter.LOOKUP_SCHEMA_VERSION,
                         "policy_revision": policy_revision, "tool_names": tools, "schema_hash": schema_hash},
                        "review_capabilities")

    @server.tool()
    def list_changes(review_id: str, scope: str = "", kind: str = "") -> str:
        """Symbol-level change inventory: what changed in this diff, not what
        it touches.

        For each changed .py file, classifies every symbol the diff actually
        touched (not every symbol in a touched file) as added, modified,
        deleted, or signature_changed, by comparing the current and prior
        AST against the diff's own hunk ranges. No cross-references here —
        call `trace` for those. `documentation_files` includes changed docs
        plus Python files with touched docstrings. `scope` filters to a file-path prefix,
        `kind` to one of function|async_function|class|method.
        """
        snapshot = review_plan.review_snapshot(review_id)
        if snapshot is None:
            return json.dumps({"code": "UNKNOWN_REVIEW", "reason": "unknown or evicted review_id"})
        result = change_graph.list_changes(
            snapshot.repository_root, snapshot.diff_base_sha,
            scope=scope or None, kind=kind or None, head=snapshot.diff_head_sha,
            source_root=snapshot.source_root, cache_key=snapshot.snapshot_id,
        )
        return _respond(result, "list_changes", review_id)

    @server.tool()
    def trace(review_id: str, symbol: str, direction: str = "in", depth: int = 1,
              cross_boundary_only: bool = False, min_confidence: str = "") -> str:
        """Directional, depth-bounded dependency walk from one symbol.

        `symbol` must be "path/to/file.py:Name" (dotted for a method, e.g.
        "app/serializers.py:ListingSerializer.validate") — exactly the
        (file, symbol) pair `list_changes` returns per row.

        direction="in" is blast radius (who calls/references this).
        direction="out" is what this symbol itself calls. depth 2-3 recurses
        onto each hit's enclosing symbol — genuine dependency exploration,
        which nothing else in this server can do.
        """
        snapshot = review_plan.review_snapshot(review_id)
        if snapshot is None:
            return json.dumps({"code": "UNKNOWN_REVIEW", "reason": "unknown or evicted review_id"})
        result = change_graph.trace(
            snapshot.source_root, symbol, direction=direction, depth=depth,
            cross_boundary_only=cross_boundary_only, min_confidence=min_confidence or None,
        )
        return _respond(result, "trace", review_id)

    @server.tool()
    def change_coherence(review_id: str) -> str:
        """Precomputed suspicion list: the intersection of the change set
        with the dependency graph, as verdicts rather than raw data.

        Returns callers that didn't move when a signature changed
        (unchanged_dependents), references left dangling by a deletion
        (orphaned_references), new functions/methods no test file mentions
        (untested_new_symbols), and blast radius crossing a service boundary
        (cross_boundary_impact).
        """
        snapshot = review_plan.review_snapshot(review_id)
        if snapshot is None:
            return json.dumps({"code": "UNKNOWN_REVIEW", "reason": "unknown or evicted review_id"})
        result = change_graph.change_coherence(
            snapshot.repository_root, snapshot.diff_base_sha,
            head=snapshot.diff_head_sha, source_root=snapshot.source_root,
            cache_key=snapshot.snapshot_id,
        )
        return _respond(result, "change_coherence", review_id)

    def legacy_get_change(
        symbol: str = "",
        file: str = "",
        with_context: int = 0,
        view: str = "hunk",
        max_lines: int = 400,
        max_chars: int = 48_000,
    ) -> str:
        """Return one changed symbol or file with a bounded representation.

        Provide exactly one of `symbol` ("path/file.py:Name", dotted for a
        method) or `file`. `view="hunk"` is the compact default; use
        `view="full"` only for a concrete before/after question.
        """
        root = _repo_root()
        result = query.get_change(
            root,
            _base_ref(),
            symbol=symbol or None,
            file=file or None,
            with_context=with_context,
            view=view,
            max_lines=max_lines,
            max_chars=max_chars,
        )
        return json.dumps(result)

    def get_changes(
        files: list[str],
        with_context: int = 0,
        view: str = "hunk",
        max_lines: int = 160,
        max_chars: int = 12_000,
    ) -> str:
        """Return compact changes for several already-known changed files.

        Prefer this over repeated `get_change` calls when reviewing a prepared
        bundle. File paths must be repository-relative and changed against the
        review base.
        """
        return json.dumps(
            query.get_changes(
                _repo_root(), _base_ref(), files, with_context=with_context,
                view=view, max_lines=max_lines, max_chars=max_chars,
            )
        )

    def legacy_find_symbol(name: str, kind: str = "", scope: str = "", cap: int = 10) -> str:
        """Find Python function, class, or method definitions by name.

        A dotted name restricts a method to its class. Optional `kind` and
        file-prefix `scope` filters keep the repo-wide lookup narrow.
        """
        root = _repo_root()
        return json.dumps(query.find_symbol(root, name, kind=kind or None, scope=scope or None, cap=cap))

    def legacy_get_source(
        file: str,
        around_symbol: str = "",
        line_range: list[int] | None = None,
        context: int = 5,
        max_lines: int = 400,
    ) -> str:
        """Return one bounded current-tree source range.

        Provide exactly one of `around_symbol` or inclusive `line_range`
        (`[start, end]`). Repeated requests for an identical resolved range
        are served from an in-process cache.
        """
        root = _repo_root()
        return json.dumps(
            query.get_source(
                root,
                file,
                around_symbol=around_symbol or None,
                line_range=line_range,
                context=context,
                max_lines=max_lines,
            )
        )

    def legacy_find_siblings(file: str, cap: int = 5) -> str:
        """Find structurally comparable Python files for a changed file.

        Candidates share the basename under another parent or live beside
        the target file. Results summarize shared and differing top-level
        symbol names/kinds and are capped.
        """
        root = _repo_root()
        return json.dumps(query.find_siblings(root, _base_ref(), file, cap=cap))

    def legacy_grep_repo(
        pattern: str,
        langs: list[str] | None = None,
        exclude: list[str] | None = None,
        cap: int = 50,
    ) -> str:
        """Regex-search the repo with language, exclusion, and result caps.

        Supported language filters are `py`, `ts`, `yaml`, and `tf`.
        `total_matches` remains exact even when returned matches are capped.
        """
        root = _repo_root()
        return json.dumps(query.grep_repo(root, pattern, langs=langs, exclude=exclude, cap=cap))

    def legacy_verify_anchor(file: str, line: int, expected_snippet: str) -> str:
        """Verify a finding's file/line/snippet anchor.

        Whitespace-normalized exact matches pass. A mismatch searches a
        bounded +/-15-line window and returns `suggested_line` when found.
        """
        root = _repo_root()
        return json.dumps(query.verify_anchor(root, file, line, expected_snippet))

    @server.tool()
    def probe(
        review_id: str,
        command: str,
        timeout: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
        working_directory: str = "",
        stage: str = "pre_dispatch",
    ) -> str:
        """Run one short, bounded verification command below the repository root.

        Restricted to pre_dispatch orchestrator probes and verification-stage
        checks; it is never supplied to ordinary lenses.
        Command strings are argv-parsed, not shell-evaluated; working_directory
        must be relative to the review root. The result always contains only
        stdout, stderr, exit_code, and timed_out.
        """
        snapshot = review_plan.review_snapshot(review_id)
        if snapshot is None:
            return json.dumps({"code": "UNKNOWN_REVIEW", "reason": "unknown or evicted review_id"})
        return _respond(run_probe(snapshot.source_root, command, timeout, working_directory, stage), "probe", review_id)

    @server.tool()
    def get_contract_changes(review_id: str, modified_file_paths: list[str] | None = None) -> str:
        """Static diff of the API contract surface for changed files.

        Covers whichever of these are present and touched: DRF serializer
        fields, FastAPI route signatures, Celery task signatures. Diffs
        DRF/FastAPI definitions against the committed generated TS client
        (if any) so breaking changes are visible without booting the app or
        regenerating a schema.
        """
        snapshot = review_plan.review_snapshot(review_id)
        if snapshot is None:
            return json.dumps({"code": "UNKNOWN_REVIEW", "reason": "unknown or evicted review_id"})
        return _respond(_contract_changes_with_reason(
            snapshot.repository_root, modified_file_paths,
            base=snapshot.diff_base_sha, source_root=snapshot.source_root,
        ), "get_contract_changes", review_id)

    @server.tool()
    def get_tech_profile(review_id: str, compact: bool = True) -> str:
        """Detected technology stack for this repo (used for lens routing).

        Compact output is the default: three representative evidence paths
        and a count per flag. Set compact=false only for diagnostics that
        genuinely need complete provenance.
        """
        snapshot = review_plan.review_snapshot(review_id)
        if snapshot is None:
            return json.dumps({"code": "UNKNOWN_REVIEW", "reason": "unknown or evicted review_id"})
        profile = tech_detector.detect(snapshot.source_root)
        return _respond(profile.to_compact_dict() if compact else profile.to_dict(), "get_tech_profile", review_id)

    @server.tool()
    def prepare_review(mode: str, base_ref: str = "", expected_base_sha: str = "",
                       expected_head_sha: str = "",
                       expected_changed_paths: list[str] | None = None) -> str:
        """Prepare compact facts for an explicit committed review target.

        PR mode requires base_ref and expected_head_sha. Supply
        expected_base_sha when target metadata provides it so a moved target
        ref fails closed. Branch mode may use default base discovery. Returns
        pinned SHAs, deterministic lane decisions, and bundle IDs, or an
        explicit invalid-base error.
        Follow with `get_review_bundle(review_id, lane_id)` for active opaque
        lane IDs; repeat fetches are byte-identical and safe after transport failure.
        """
        result = review_plan.prepare_review(
            _repo_root(), mode=mode, base_ref=base_ref or None,
            expected_base_sha=expected_base_sha or None,
            expected_head_sha=expected_head_sha or None,
            expected_changed_paths=expected_changed_paths,
        )
        return _respond(result, "prepare_review", str(result.get("review_id") or "global"))

    @server.tool()
    def get_review_bundle(review_id: str, lane_id: str) -> str:
        """Return one compact, lane-specific review bundle from prepare_review."""
        return _respond(review_plan.get_review_bundle(review_id, lane_id), "get_review_bundle", review_id)

    @server.tool()
    def get_lane_dispatch(review_id: str, lane_id: str, guidance: str = "") -> str:
        """Return an explicit-model dispatch envelope containing the untouched bundle."""
        bundle = review_plan.get_review_bundle(review_id, lane_id)
        if bundle.get("code") or not bundle.get("bundle_digest"):
            return _respond(bundle, "get_lane_dispatch", review_id)
        return _respond(review_plan.build_lane_dispatch(bundle, guidance), "get_lane_dispatch", review_id)

    @server.tool()
    def acknowledge_lane_dispatch(review_id: str, lane_id: str, embedded_payload: str,
                                  embedded_digest: str) -> str:
        """Attest the exact prompt payload bytes embedded by the launcher."""
        return _respond(review_plan.acknowledge_lane_dispatch(
            review_id, lane_id, embedded_payload, embedded_digest,
        ), "acknowledge_lane_dispatch", review_id)

    @server.tool()
    def get_evidence_item(review_id: str, lane_id: str, item_id: str) -> str:
        """Retrieve one exact evidence item named by a compact bundle."""
        return _respond(review_plan.get_evidence_item(review_id, lane_id, item_id),
                        "get_evidence_item", review_id)

    @server.tool()
    def get_evidence_page(review_id: str, lane_id: str, section: str,
                          cursor: str = "", limit: int = 5) -> str:
        """Retrieve a deterministic page of exact omitted section evidence."""
        return _respond(review_plan.get_evidence_page(review_id, lane_id, section, cursor, limit),
                        "get_evidence_page", review_id)

    @server.tool()
    def get_transport_page(review_id: str, cursor: str) -> str:
        """Retrieve a byte-bounded page from an automatically compacted MCP response."""
        return _respond(transport.get_page(review_id, cursor), "get_transport_page", review_id)

    @server.tool()
    def record_review_dispatch(review_id: str, lane_ids: list[str], observed_models: dict[str, str] | None = None,
                               aggregate_waits: int = 1, poll_wakeups: int = 0, fallback_agents: int = 0) -> str:
        """Deprecated: caller-supplied dispatch counters are never authoritative."""
        return _respond(review_plan.record_dispatch_cycle(
            review_id, lane_ids, observed_models, aggregate_waits, poll_wakeups, fallback_agents,
        ), "record_review_dispatch", review_id)

    @server.tool()
    def ingest_dispatch_observations(review_id: str, observations: list[HostObservation]) -> str:
        """Ingest host observations as self-reported evidence; MCP cannot verify host enforcement."""
        return _respond(review_plan.ingest_host_observations(
            review_id, observations, provenance="self-reported",
        ), "ingest_dispatch_observations", review_id)

    def _review_lookup(
        review_id: str,
        lane_id: str,
        operation: str,
        question: str,
        arguments: dict | None = None,
        verification_id: str = "",
        candidate_id: str = "",
    ) -> str:
        """Perform one budgeted, concrete follow-up lookup for a review lane.

        This is the only repository lookup available to review and verifier
        agents. It rejects vague questions, unsupported operations, and calls
        beyond four per lane or six per registered verification candidate.
        """
        if not question or not question.strip():
            review_plan.record_lookup_rejection(review_id, lane_id, operation)
            return json.dumps({"code": "MALFORMED_REQUEST", "reason": "question must name the concrete missing fact", "quota_consumed": False,
                               "remaining_lookup_calls": review_plan.remaining_lookup_budget(review_id, lane_id, verification_id=verification_id, candidate_id=candidate_id)})
        allowed = {"get_change", "find_symbol", "get_source", "find_siblings", "grep_repo", "verify_anchor", "changed_file", "match_pathspec"}
        if operation not in allowed:
            review_plan.record_lookup_rejection(review_id, lane_id, operation)
            return json.dumps({"code": "UNSUPPORTED_OPERATION", "reason": "unsupported review lookup operation", "allowed_operations": sorted(allowed), "quota_consumed": False,
                               "remaining_lookup_calls": review_plan.remaining_lookup_budget(review_id, lane_id, verification_id=verification_id, candidate_id=candidate_id)})
        args = arguments or {}
        schemas = {
            "get_change": ({"symbol", "file", "with_context", "view", "max_lines", "max_chars"},),
            "find_symbol": ({"name", "kind", "scope", "cap"},),
            "get_source": ({"file", "around_symbol", "line_range", "context", "max_lines"},),
            "find_siblings": ({"file", "cap"},),
            "grep_repo": ({"pattern", "langs", "exclude", "path", "cap"},),
            "verify_anchor": ({"file", "line", "expected_snippet"},),
            "changed_file": ({"file"},),
            "match_pathspec": ({"pathspec", "cap"},),
        }
        allowed_args = schemas[operation][0]
        unknown = sorted(set(args) - allowed_args)
        malformed = bool(unknown)
        if operation == "get_change":
            malformed |= bool(args.get("symbol")) == bool(args.get("file"))
        elif operation == "get_source":
            malformed |= not args.get("file") or (bool(args.get("around_symbol")) and bool(args.get("line_range")))
        elif operation == "verify_anchor":
            malformed |= not (args.get("file") and isinstance(args.get("line"), int) and args.get("expected_snippet"))
        else:
            required_key = {"find_symbol": "name", "find_siblings": "file", "grep_repo": "pattern", "changed_file": "file", "match_pathspec": "pathspec"}[operation]
            malformed |= not args.get(required_key)
        if malformed:
            review_plan.record_lookup_rejection(review_id, lane_id, operation)
            detail = f"unsupported arguments: {', '.join(unknown)}" if unknown else f"{operation} arguments violate its operation-specific schema"
            return json.dumps({"code": "MALFORMED_REQUEST", "reason": detail, "quota_consumed": False,
                               "remaining_lookup_calls": review_plan.remaining_lookup_budget(review_id, lane_id, verification_id=verification_id, candidate_id=candidate_id)})
        try:
            for key in {"with_context", "max_lines", "max_chars", "cap", "context", "line"} & set(args):
                int(args[key])
            if args.get("line_range") is not None and (not isinstance(args["line_range"], list) or len(args["line_range"]) != 2):
                raise ValueError("line_range")
        except (TypeError, ValueError):
            review_plan.record_lookup_rejection(review_id, lane_id, operation)
            return json.dumps({"code": "MALFORMED_REQUEST", "reason": "numeric arguments or line_range have invalid types", "quota_consumed": False,
                               "remaining_lookup_calls": review_plan.remaining_lookup_budget(review_id, lane_id, verification_id=verification_id, candidate_id=candidate_id)})
        snapshot = review_plan.review_snapshot(review_id)
        if snapshot is None:
            return json.dumps({"code": "UNKNOWN_REVIEW", "reason": "unknown or evicted review_id",
                               "quota_consumed": False})
        root = snapshot.repository_root
        source_root = snapshot.source_root
        # Path containment and existence failures are rejected before quota use.
        path_arg = args.get("path") if operation == "grep_repo" else args.get("file")
        if path_arg:
            try:
                candidate = Path(source_root, path_arg).resolve()
                candidate.relative_to(Path(source_root).resolve())
                if operation in {"get_source", "find_siblings", "verify_anchor", "grep_repo"} and not candidate.exists():
                    raise ValueError("path does not exist")
            except (TypeError, ValueError):
                review_plan.record_lookup_rejection(review_id, lane_id, operation)
                return json.dumps({"code": "MALFORMED_REQUEST", "reason": "path must exist inside the repository", "quota_consumed": False,
                                   "remaining_lookup_calls": review_plan.remaining_lookup_budget(review_id, lane_id, verification_id=verification_id, candidate_id=candidate_id)})
        budget = review_plan.consume_lookup_budget(
            review_id, lane_id, verification_id=verification_id, candidate_id=candidate_id,
        )
        if not budget["ok"]:
            return json.dumps({**budget, "quota_consumed": False, "remaining_lookup_calls": budget.get("remaining")})
        base = snapshot.diff_base_sha
        if operation == "get_change":
            result = query.get_change(root, base, symbol=args.get("symbol") or None, file=args.get("file") or None,
                                      with_context=args.get("with_context", 0), view=args.get("view", "full"),
                                      max_lines=min(int(args.get("max_lines", 160)), 400), max_chars=min(int(args.get("max_chars", 12_000)), 48_000),
                                      head=snapshot.diff_head_sha, source_root=source_root, cache_key=snapshot.snapshot_id)
        elif operation == "find_symbol":
            result = query.find_symbol(source_root, args.get("name", ""), kind=args.get("kind") or None,
                                       scope=args.get("scope") or None, cap=min(int(args.get("cap", 10)), 20))
        elif operation == "get_source":
            result = query.get_source(source_root, args.get("file", ""), around_symbol=args.get("around_symbol") or None,
                                      line_range=args.get("line_range"), context=min(int(args.get("context", 5)), 20),
                                      max_lines=min(int(args.get("max_lines", 160)), 400))
        elif operation == "find_siblings":
            result = query.find_siblings(source_root, base, args.get("file", ""), cap=min(int(args.get("cap", 5)), 10))
        elif operation == "grep_repo":
            result = query.grep_repo(source_root, args.get("pattern", ""), langs=args.get("langs"), exclude=args.get("exclude"), path=args.get("path"), cap=min(int(args.get("cap", 20)), 50))
        elif operation == "changed_file":
            result = review_plan.changed_file_lookup(review_id, args.get("file", ""))
        elif operation == "match_pathspec":
            result = git_analyzer.match_pathspec_at(root, snapshot.diff_head_sha, args.get("pathspec", ""),
                                                    min(int(args.get("cap", 100)), 200))
        else:
            result = query.verify_anchor(source_root, args.get("file", ""), args.get("line", 0), args.get("expected_snippet", ""))
        receipt = None
        if verification_id and candidate_id:
            receipt = review_plan.record_verification_lookup(
                review_id, verification_id, candidate_id, operation, result,
            )
        review_plan.record_agent_response(review_id, operation, lane_id)
        return _respond({"result": result, "quota_consumed": True, "remaining_lookup_calls": budget["remaining"],
                         "verification_receipt_id": receipt},
                        "review_lookup", review_id)

    @server.tool()
    def review_lookup(review_id: str, lane_id: str, operation: str, question: str,
                      arguments: dict | None = None, verification_id: str = "",
                      candidate_id: str = "") -> str:
        """Compatibility wrapper for the versioned typed lookup tools."""
        return _review_lookup(review_id, lane_id, operation, question, arguments,
                              verification_id, candidate_id)

    @server.tool()
    def get_change(review_id: str, lane_id: str, question: str, symbol: str = "", file: str = "",
                   with_context: int = 0, view: Literal["hunk", "full", "symbol"] = "hunk",
                   max_lines: int = 160, max_chars: int = 12_000,
                   verification_id: str = "", candidate_id: str = "") -> str:
        """Read one pinned changed symbol or changed file using the shared lookup quota."""
        return _review_lookup(review_id, lane_id, "get_change", question, {
            "symbol": symbol, "file": file, "with_context": with_context, "view": view,
            "max_lines": max_lines, "max_chars": max_chars,
        }, verification_id, candidate_id)

    @server.tool()
    def find_symbol(review_id: str, lane_id: str, question: str, name: str, kind: str = "",
                    scope: str = "", cap: int = 10, verification_id: str = "", candidate_id: str = "") -> str:
        """Find a definition in the pinned head snapshot using the shared lookup quota."""
        return _review_lookup(review_id, lane_id, "find_symbol", question,
                              {"name": name, "kind": kind, "scope": scope, "cap": cap}, verification_id, candidate_id)

    @server.tool()
    def get_source(review_id: str, lane_id: str, question: str, file: str, around_symbol: str = "",
                   line_range: list[int] | None = None, context: int = 5, max_lines: int = 160,
                   verification_id: str = "", candidate_id: str = "") -> str:
        """Read a bounded pinned-head source range using the shared lookup quota."""
        return _review_lookup(review_id, lane_id, "get_source", question, {
            "file": file, "around_symbol": around_symbol, "line_range": line_range,
            "context": context, "max_lines": max_lines,
        }, verification_id, candidate_id)

    @server.tool()
    def find_siblings(review_id: str, lane_id: str, question: str, file: str, cap: int = 5,
                      verification_id: str = "", candidate_id: str = "") -> str:
        """Find comparable pinned-head source files using the shared lookup quota."""
        return _review_lookup(review_id, lane_id, "find_siblings", question,
                              {"file": file, "cap": cap}, verification_id, candidate_id)

    @server.tool()
    def grep_repo(review_id: str, lane_id: str, question: str, pattern: str,
                  langs: list[str] | None = None, exclude: list[str] | None = None,
                  path: str = "", cap: int = 20, verification_id: str = "", candidate_id: str = "") -> str:
        """Search Git-pinned source with explicit filters and the shared lookup quota."""
        return _review_lookup(review_id, lane_id, "grep_repo", question, {
            "pattern": pattern, "langs": langs, "exclude": exclude, "path": path, "cap": cap,
        }, verification_id, candidate_id)

    @server.tool()
    def verify_anchor(review_id: str, lane_id: str, question: str, file: str, line: int,
                      expected_snippet: str, verification_id: str = "", candidate_id: str = "") -> str:
        """Verify one pinned-head anchor using the shared lookup quota."""
        return _review_lookup(review_id, lane_id, "verify_anchor", question, {
            "file": file, "line": line, "expected_snippet": expected_snippet,
        }, verification_id, candidate_id)

    @server.tool()
    def changed_file(review_id: str, lane_id: str, question: str, file: str,
                     verification_id: str = "", candidate_id: str = "") -> str:
        """Check changed-file membership in the prepared snapshot using the shared quota."""
        return _review_lookup(review_id, lane_id, "changed_file", question,
                              {"file": file}, verification_id, candidate_id)

    @server.tool()
    def match_pathspec(review_id: str, lane_id: str, question: str, pathspec: str, cap: int = 100,
                       verification_id: str = "", candidate_id: str = "") -> str:
        """Resolve a Git pathspec against the pinned head before making a command-semantics claim."""
        return _review_lookup(review_id, lane_id, "match_pathspec", question,
                              {"pathspec": pathspec, "cap": cap}, verification_id, candidate_id)

    @server.tool()
    def verify_anchors(review_id: str, findings: list[dict]) -> str:
        """Verify all one-line anchors at once before root-cause deduplication."""
        snapshot = review_plan.review_snapshot(review_id)
        if snapshot is None:
            return json.dumps({"code": "UNKNOWN_REVIEW", "reason": "unknown or evicted review_id"})
        return _respond(query.verify_changed_anchors(
            snapshot.repository_root, snapshot.diff_base_sha, findings,
            head=snapshot.diff_head_sha, source_root=snapshot.source_root,
        ), "verify_anchors", review_id)

    @server.tool()
    def begin_verification(review_id: str, candidates: list[dict]) -> str:
        """Register one batched verification pass with six lookups per candidate."""
        return _respond(review_plan.begin_verification(review_id, candidates), "begin_verification", review_id)

    @server.tool()
    def get_verifier_dispatch(review_id: str, verification_id: str, guidance: str = "") -> str:
        """Return the candidate-scoped typed-tool verifier dispatch payload."""
        return _respond(review_plan.build_verifier_dispatch(review_id, verification_id, guidance),
                        "get_verifier_dispatch", review_id)

    @server.tool()
    def verify_candidate_source_facts(review_id: str, verification_id: str, candidate_id: str) -> str:
        """Issue a server-deterministic pinned-source verdict for one registered candidate."""
        return _respond(review_plan.deterministic_source_verification(
            review_id, verification_id, candidate_id,
        ), "verify_candidate_source_facts", review_id)

    @server.tool()
    def register_verifier_result(review_id: str, verification_id: str, candidate_id: str,
                                 verdict: Literal["CONFIRMED", "PLAUSIBLE", "REFUTED"],
                                 receipt_ids: list[str]) -> str:
        """Register a verifier verdict backed by candidate-scoped typed-tool receipts."""
        return _respond(review_plan.register_verifier_result(
            review_id, verification_id, candidate_id, verdict, receipt_ids,
        ), "register_verifier_result", review_id)

    @server.tool()
    def consolidate_review_results(review_id: str, lane_results: list[dict], verification_results: list[dict] | None = None,
                                   resolutions: list[dict] | None = None) -> str:
        """Atomically validate anchors, deduplicate causes, select candidates, and retain open questions."""
        snapshot = review_plan.review_snapshot(review_id)
        if snapshot is None:
            return json.dumps({"code": "UNKNOWN_REVIEW", "reason": "unknown or evicted review_id"})
        if resolutions:
            return _respond({
                "code": "RESOLUTION_ONLY_REQUIRED",
                "reason": "submit question resolutions through resolve_review_questions",
                "state_unchanged": True,
            }, "consolidate_review_results", review_id)
        root, base = snapshot.repository_root, snapshot.diff_base_sha
        accepted, rejected = [], []
        for lane_result in lane_results:
            check = review_plan.validate_lane_result(review_id, lane_result)
            (accepted if check["ok"] else rejected).append(check["result"] if check["ok"] else check)
        prior = review_plan._consolidations.get(review_id)
        registered_verdicts, rejected_verdicts = review_plan.registered_verification_results(
            review_id, verification_results,
        )
        result = review_results.consolidate(
            root, accepted, registered_verdicts if verification_results is not None else None, None, review_id=review_id,
            previous=prior, base=base, head=snapshot.diff_head_sha,
            source_root=snapshot.source_root,
        )
        result["rejected_lane_results"] = rejected
        result["rejected_verification_results"] = rejected_verdicts
        review_plan._consolidations[review_id] = result
        review_plan.record_consolidation(review_id, result)
        return _respond(result, "consolidate_review_results", review_id)

    @server.tool()
    def resolve_review_questions(review_id: str, resolutions: list[QuestionResolution]) -> str:
        """Validate and commit one resolution-only batch as a copy-on-write transaction."""
        prior = review_plan._consolidations.get(review_id)
        if prior is None:
            return _respond({"code": "UNKNOWN_REVIEW_STATE", "state_unchanged": True},
                            "resolve_review_questions", review_id)
        transaction = review_results.resolve_questions(prior, resolutions)
        if not transaction["ok"]:
            return _respond({"ok": False, "errors": transaction["errors"], "state_unchanged": True},
                            "resolve_review_questions", review_id)
        review_plan._consolidations[review_id] = transaction["state"]
        review_plan.record_question_resolutions(review_id, resolutions)
        return _respond({"ok": True, "resolved_question_ids": transaction["resolved_question_ids"],
                         "unanswered": transaction["state"].get("unanswered", [])},
                        "resolve_review_questions", review_id)

    @server.tool()
    def get_review_metrics(review_id: str) -> str:
        """Return authoritative counters captured for this review snapshot."""
        return _respond(review_plan.get_metrics(review_id), "get_review_metrics", review_id)

    @server.tool()
    def get_review_status(review_id: str) -> str:
        """Return authoritative coverage status independently of the finding count."""
        return _respond(review_plan.get_review_status(review_id), "get_review_status", review_id)

    @server.tool()
    def render_review_report(review_id: str) -> str:
        """Render an authoritative stored review; caller counts and findings are not accepted."""
        return render_consolidated_review_report(review_id)

    @server.tool()
    def render_consolidated_review_report(review_id: str) -> str:
        """Render server-retained findings and all unresolved questions for a review."""
        state = review_plan._consolidations.get(review_id)
        if not state:
            return "Review state is unavailable; consolidate results before rendering.\n"
        context = review_plan.authoritative_report_context(review_id)
        return _respond(review_results.render_markdown(
            state.get("findings", []), context["reviewed_files"], context["active_lanes"],
            state.get("unanswered", []), state.get("optional_notes", []), context["status"],
        ), "render_consolidated_review_report", review_id)

    return server


def _self_test(root: str, base: str) -> None:
    print(f"[gw-code-review] self-test against root={root} base={base}\n")

    profile = tech_detector.detect(root)
    print("== tech profile ==")
    print(json.dumps(profile.to_dict(), indent=2))

    changed = git_analyzer.get_changed_files(root=root, base=base)
    print(f"\n== changed files vs {base} ({len(changed)}) ==")
    for f in changed[:20]:
        print(f"  {f}")
    if len(changed) > 20:
        print(f"  ... and {len(changed) - 20} more")

    print("\n== list_changes ==")
    changes = change_graph.list_changes(root, base)
    print(json.dumps(changes, indent=2)[:2000])

    print("\n== change_coherence ==")
    print(json.dumps(change_graph.change_coherence(root, base), indent=2)[:2000])

    print("\n== contract changes ==")
    print(json.dumps(_contract_changes_with_reason(root, changed or None), indent=2)[:2000])

    first_change = changes.get("changes", [{}])[0] if changes.get("changes") else {}
    first_file = next((path for path in changed if (Path(root) / path).is_file()), "")
    first_py_file = next(
        (path for path in changed if "/tools/" in path and path.endswith(".py") and (Path(root) / path).is_file()),
        next((path for path in changed if path.endswith(".py") and (Path(root) / path).is_file()), ""),
    )

    print("\n== get_change (file) ==")
    if first_file:
        print(json.dumps(query.get_change(root, base, file=first_file, with_context=2), indent=2)[:2000])
    else:
        print(json.dumps(query.get_change(root, base, file="__gw_missing_file__"), indent=2)[:2000])

    print("\n== get_change (symbol) ==")
    if first_change:
        qualified_symbol = f"{first_change['file']}:{first_change['symbol']}"
        print(json.dumps(query.get_change(root, base, symbol=qualified_symbol, with_context=2), indent=2)[:2000])
    else:
        print(json.dumps(query.get_change(root, base, symbol="__gw_missing_file__.py:missing"), indent=2)[:2000])

    print("\n== find_symbol ==")
    symbol_name = first_change.get("symbol", "__gw_missing_symbol__")
    print(json.dumps(query.find_symbol(root, symbol_name, cap=3), indent=2)[:2000])

    print("\n== get_source ==")
    if first_py_file:
        print(json.dumps(query.get_source(root, first_py_file, line_range=[1, 5], context=1), indent=2)[:2000])
    else:
        print(json.dumps(query.get_source(root, "__gw_missing_file__", line_range=[1, 5]), indent=2)[:2000])

    print("\n== find_siblings ==")
    if first_py_file:
        print(json.dumps(query.find_siblings(root, base, first_py_file, cap=3), indent=2)[:2000])
    else:
        print(json.dumps(query.find_siblings(root, base, "__gw_missing_file__.py"), indent=2)[:2000])

    print("\n== grep_repo ==")
    print(json.dumps(query.grep_repo(root, r"^\s*(?:async\s+def|def|class)\s+", langs=["py"], cap=3), indent=2)[:2000])

    print("\n== verify_anchor ==")
    if first_file:
        first_line = (Path(root) / first_file).read_text(errors="ignore").splitlines()
        snippet = first_line[0] if first_line else "__gw_missing_snippet__"
        anchors = {
            "exact": query.verify_anchor(root, first_file, 1, snippet),
            "shifted": query.verify_anchor(root, first_file, 2, snippet),
        }
        print(json.dumps(anchors, indent=2)[:2000])
    else:
        print(json.dumps(query.verify_anchor(root, "__gw_missing_file__", 1, "missing"), indent=2)[:2000])

    print("\n== probe ==")
    print(json.dumps(run_probe(root, [sys.executable, "-c", "print('gw-probe-ok')"]), indent=2))

    print("\n== empty-result reasons ==")
    absent_pattern = "__gw_" + "pattern_that_should_not_exist__"
    empty_checks = {
        "find_symbol": query.find_symbol(root, "__gw_symbol_that_should_not_exist__"),
        "grep_repo": query.grep_repo(root, absent_pattern, cap=1),
    }
    print(json.dumps(empty_checks, indent=2)[:2000])

    if MCPServer is None:
        print(f"\n[warning] cannot import the MCP server class — the server itself "
              f"cannot run, but all tool implementations above executed directly.\n"
              f"          reason: {_MCP_IMPORT_ERROR}")
    else:
        print("\n[ok] MCP server class importable; server can be started for real.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run all tools directly and print output, no MCP transport",
    )
    parser.add_argument("--root", default=_default_root(), help="Repo root to operate on (default: cwd)")
    parser.add_argument("--base", default=DEFAULT_BASE, help="Git ref to diff against")
    args = parser.parse_args()

    if args.self_test:
        _self_test(args.root, args.base)
        return

    if MCPServer is None:
        print(f"error: cannot import the MCP server class ({_MCP_IMPORT_ERROR}). Run "
              f"`uv sync` in this directory, or `pip install 'mcp[cli]>=2'`. Use "
              f"--self-test to exercise the tool implementations without it.", file=sys.stderr)
        sys.exit(1)

    import os

    os.environ.setdefault("GW_REVIEW_ROOT", args.root)
    os.environ.setdefault("GW_REVIEW_BASE", args.base)
    server = _build_server()
    server.run()


if __name__ == "__main__":
    main()

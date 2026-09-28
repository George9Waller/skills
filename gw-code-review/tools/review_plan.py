"""Compact, deterministic review-plan construction for the MCP server."""

from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from pathlib import Path

from . import change_graph, dispatch_adapter, git_analyzer, query, routing, schema_diff, tech_detector, transport

_plans: dict[str, dict] = {}
_lookup_usage: dict[tuple[str, str], int] = {}
_verification_batches: dict[str, dict] = {}
_verification_results: dict[tuple[str, str, str], dict] = {}
_verification_lookup_receipts: dict[tuple[str, str, str], list[str]] = {}
_metrics: dict[str, dict] = {}
_consolidations: dict[str, dict] = {}
_evidence_items: dict[tuple[str, str], dict] = {}
_evidence_sections: dict[tuple[str, str, str], list[str]] = {}
_evidence_cursors: dict[str, tuple[str, str, str, int]] = {}
MAX_SECTION_BYTES = 3_000
INLINE_ITEM_BYTES = 800


def _payload_bytes(value: object) -> int:
    return len(json.dumps(value, sort_keys=True, default=str).encode("utf-8"))


def canonical_json(value: object) -> str:
    """Stable JSON used as the byte-for-byte review-bundle identity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def bundle_digest(bundle: dict) -> str:
    """Digest a bundle excluding its self-referential digest field."""
    payload = {key: value for key, value in bundle.items() if key != "bundle_digest"}
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _lane_id(review_id: str, display_name: str) -> str:
    digest = hashlib.sha256(f"{review_id}\0{display_name}".encode()).hexdigest()[:20]
    return f"lane_{digest}"


def _item_id(review_id: str, lane_id: str, section: str, value: object) -> str:
    payload = canonical_json(value)
    digest = hashlib.sha256(f"{review_id}\0{lane_id}\0{section}\0{payload}".encode()).hexdigest()[:24]
    return f"item_{digest}"


def _section_cursor(review_id: str, lane_id: str, section: str, offset: int) -> str:
    raw = f"{review_id}\0{lane_id}\0{section}\0{offset}"
    token = "evidence_" + hashlib.sha256(raw.encode()).hexdigest()[:24]
    _evidence_cursors[token] = (review_id, lane_id, section, offset)
    return token


def _coherence_rank(value: dict) -> tuple[int, str]:
    confidence = str(value.get("confidence", "")).lower()
    resolution = str(value.get("resolution", value.get("match", ""))).lower()
    text = canonical_json(value).lower()
    if resolution in {"direct", "resolved", "import", "qualified"} or confidence == "high":
        rank = 0
    elif any(marker in text for marker in ("direct", "resolved", "import", "qualified", "celery")):
        rank = 1
    elif confidence == "medium":
        rank = 2
    else:
        rank = 3
    return rank, canonical_json(value)


def _section_items(section: str, value: object) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return [{"value": item} for item in value]
    if isinstance(value, dict):
        rows: list[dict] = []
        for category, items in sorted(value.items()):
            if isinstance(items, list):
                rows.extend({"category": category, "value": item} for item in items)
            elif section == "contract_changes" and category in {"found", "capability", "reason"}:
                continue
            else:
                rows.append({"category": category, "value": items})
        return rows
    return [{"value": value}]


def _compact_section(review_id: str, lane_id: str, section: str, value: object, *,
                     applicable: bool = True, failed: bool = False) -> dict:
    """Store exact evidence and return a ranked, bounded manifest."""
    if failed:
        _evidence_sections[(review_id, lane_id, section)] = []
        return {"status": "failed", "returned_items": 0, "total_items": 0,
                "items": [], "omission_reasons": ["evidence computation failed"]}
    if not applicable:
        _evidence_sections[(review_id, lane_id, section)] = []
        return {"status": "not_applicable", "returned_items": 0, "total_items": 0,
                "items": [], "omission_reasons": []}
    rows = _section_items(section, value)
    if section == "coherence":
        rows.sort(key=lambda row: _coherence_rank(row.get("value", {})))
    else:
        rows.sort(key=canonical_json)
    ids: list[str] = []
    compact: list[dict] = []
    used = 0
    previewed_ids: list[str] = []
    for row in rows:
        item_id = _item_id(review_id, lane_id, section, row)
        _evidence_items[(review_id, item_id)] = deepcopy(row)
        ids.append(item_id)
        encoded = canonical_json(row).encode("utf-8")
        item = {"item_id": item_id}
        if len(encoded) <= INLINE_ITEM_BYTES:
            item.update(deepcopy(row))
        else:
            item.update({"preview": encoded[:INLINE_ITEM_BYTES].decode("utf-8", errors="ignore"),
                         "item_bytes": len(encoded), "omission_reason": "item exceeds inline preview limit"})
            previewed_ids.append(item_id)
        size = _payload_bytes(item)
        if compact and used + size > MAX_SECTION_BYTES:
            break
        compact.append(item)
        used += size
    _evidence_sections[(review_id, lane_id, section)] = ids
    omitted_rows = len(rows) - len(compact)
    truncated = omitted_rows > 0 or bool(previewed_ids)
    reasons = []
    if omitted_rows:
        reasons.append("section exceeds inline byte budget")
    if previewed_ids:
        reasons.append("one or more items exceed the inline preview limit")
    return {
        "status": "truncated" if truncated else ("complete" if rows else "empty"),
        "returned_items": len(compact), "total_items": len(rows), "items": compact,
        "omitted_count": omitted_rows, "previewed_count": len(previewed_ids),
        "omission_reasons": reasons, "previewed_item_ids": previewed_ids,
        "cursor": _section_cursor(review_id, lane_id, section, len(compact)) if omitted_rows else None,
    }


def get_evidence_item(review_id: str, lane_id: str, item_id: str) -> dict:
    plan = _plan_for(review_id)
    if not plan or lane_id not in plan["bundles"]:
        return {"code": "UNKNOWN_SCOPE", "reason": "unknown review_id or lane_id"}
    owned = any(item_id in ids for (candidate_review, candidate_lane, _), ids in _evidence_sections.items()
                if candidate_review == review_id and candidate_lane == lane_id)
    if not owned:
        return {"code": "UNKNOWN_ITEM", "reason": "item_id does not belong to this lane"}
    value = _evidence_items.get((review_id, item_id))
    if value is None:
        return {"code": "UNKNOWN_ITEM", "reason": "item_id does not belong to this review"}
    return {"review_id": review_id, "lane_id": lane_id, "item_id": item_id, "payload": deepcopy(value)}


def get_evidence_page(review_id: str, lane_id: str, section: str, cursor: str = "", limit: int = 5) -> dict:
    plan = _plan_for(review_id)
    if not plan or lane_id not in plan["bundles"]:
        return {"code": "UNKNOWN_SCOPE", "reason": "unknown review_id or lane_id"}
    key = (review_id, lane_id, section)
    ids = _evidence_sections.get(key)
    if ids is None:
        return {"code": "UNKNOWN_SECTION", "reason": "section does not belong to this lane"}
    offset = 0
    if cursor:
        state = _evidence_cursors.get(cursor)
        if state is None or state[:3] != key:
            return {"code": "INVALID_CURSOR", "reason": "cursor does not belong to this section"}
        offset = state[3]
    bounded_limit = max(1, min(int(limit), 20))
    selected = ids[offset:offset + bounded_limit]
    next_offset = offset + len(selected)
    return {
        "review_id": review_id, "lane_id": lane_id, "section": section,
        "returned_items": len(selected), "total_items": len(ids),
        "items": [{"item_id": item_id, "payload": deepcopy(_evidence_items[(review_id, item_id)])}
                  for item_id in selected],
        "cursor": _section_cursor(review_id, lane_id, section, next_offset) if next_offset < len(ids) else None,
    }


def _hunks_by_file(snapshot: git_analyzer.ReviewSnapshot, files: list[str]) -> dict[str, str]:
    changes = query.get_changes(
        snapshot.repository_root, snapshot.diff_base_sha, files, view="hunk",
        max_lines=160, max_chars=12_000, head=snapshot.diff_head_sha,
        source_root=snapshot.source_root, cache_key=snapshot.snapshot_id,
    )["changes"]
    return {item["file"]: item.get("hunk", "") for item in changes if item.get("file")}


def _filter_coherence(coherence: dict, files: set[str]) -> dict:
    return {
        name: [item for item in values if item.get("file") in files or item.get("caller_file") in files]
        for name, values in coherence.items()
        if isinstance(values, list)
    }


def _filter_contract(contract: dict, files: set[str]) -> dict:
    result: dict = {}
    for name, values in contract.items():
        if isinstance(values, list):
            result[name] = [item for item in values if item.get("file") in files]
        elif name in {"found", "capability", "reason"}:
            result[name] = values
    return result


def _response_budget(hunks: dict[str, str], assigned_files: list[str]) -> int:
    """Return total turns: semantic investigation plus two protected outputs."""
    executable = [path for path in assigned_files if not path.lower().endswith((".json", ".yaml", ".yml", ".schema"))]
    changed_lines = sum(hunks.get(path, "").count("\n") + 1 for path in executable if hunks.get(path))
    investigative = 4 if changed_lines <= 150 else 6 if changed_lines <= 600 else 8
    return investigative + 2


def _dependency_summary(files: list[str], hunks: dict[str, str]) -> dict | None:
    """Compact deterministic evidence for manifests/locks; never embeds a lockfile."""
    relevant = [path for path in files if "Dependency Compatibility" in routing.route_review_lanes(
        {"changed_files": [path], "changes": []}, {}, {path: hunks.get(path, "")}
    ).get("Core Review", {}).get("activated_checks", [])]
    if not relevant:
        return None
    changes, resolved_versions, override_rules, required_packages = [], [], [], set()
    for path in relevant:
        lines = [line for line in hunks.get(path, "").splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        package = None
        pending: dict[str, str] = {}
        for raw in hunks.get(path, "").splitlines():
            text = raw[1:].strip() if raw[:1] in {"+", "-", " "} else raw.strip()
            name_match = re.match(r'(?:name|package)\s*=\s*["\']([^"\']+)', text, re.I)
            if name_match:
                package = name_match.group(1)
                required_packages.add(package)
            version_match = re.match(r'version\s*=\s*["\']([^"\']+)', text, re.I)
            if version_match and raw[:1] in {"+", "-"}:
                key = package or path
                pending[f"{key}:{raw[:1]}"] = version_match.group(1)
            if raw[:1] in {"+", "-"} and any(token in text.lower() for token in ("override", "resolution", "constraint", "force")):
                override_rules.append({"file": path, "change": raw[:300]})
        for key in sorted({item.rsplit(":", 1)[0] for item in pending}):
            old, new = pending.get(f"{key}:-"), pending.get(f"{key}:+")
            if old or new:
                resolved_versions.append({"package": key, "previous": old, "new": new, "file": path})
        changes.append({"file": path, "changed_constraints": lines[:80], "changed_line_count": len(lines),
                        "full_lockfile_embedded": False})
    return {"active": True, "files": relevant, "changes": changes,
            "changed_direct_constraints": [item for item in changes if not item["file"].lower().endswith(".lock")],
            "changed_override_rules": override_rules, "resolved_versions": resolved_versions,
            "relevant_extras_or_features": [], "transitive_dependency_changes": resolved_versions,
            "required_packages_seen": sorted(required_packages),
            "full_lockfile_embedded": False,
            "note": "Evidence is derived only from changed dependency lines; confirm package presence and forced-override effects before reporting."}


def _build_plan(snapshot: git_analyzer.ReviewSnapshot) -> dict:
    root, base = snapshot.repository_root, snapshot.diff_base_sha
    inventory = change_graph.list_changes(
        root, base, head=snapshot.diff_head_sha, source_root=snapshot.source_root,
        cache_key=snapshot.snapshot_id,
    )
    if set(inventory.get("changed_files", [])) != set(snapshot.changed_paths):
        raise git_analyzer.SnapshotError(
            "inventory-mismatch", "semantic inventory differs from the independently prepared inventory",
            prepared_paths=list(snapshot.changed_paths), semantic_paths=inventory.get("changed_files", []),
        )
    profile = tech_detector.detect(snapshot.source_root).to_compact_dict()
    files = list(inventory.get("changed_files", []))
    hunks = _hunks_by_file(snapshot, files) if files else {}
    coherence = change_graph.change_coherence(
        root, base, head=snapshot.diff_head_sha, source_root=snapshot.source_root,
        cache_key=snapshot.snapshot_id,
    )
    contract = schema_diff.get_contract_changes(
        root, modified_file_paths=files, base=base, source_root=snapshot.source_root,
    )
    if not contract.get("found"):
        contract.setdefault("reason", "no supported Python API-contract symbols changed")
    lanes = routing.route_review_lanes(inventory, profile, hunks)
    dependency = _dependency_summary(files, hunks)
    review_id = snapshot.snapshot_id
    bundles: dict[str, dict] = {}
    opaque_routing: dict[str, dict] = {}
    for lane, decision in lanes.items():
        lane_id = _lane_id(review_id, lane)
        assigned = decision["assigned_files"]
        assigned_set = set(assigned)
        filtered_coherence = _filter_coherence(coherence, assigned_set)
        filtered_contract = _filter_contract(contract, assigned_set)
        sections = {
            "files": _compact_section(review_id, lane_id, "files", assigned),
            "changes": _compact_section(
                review_id, lane_id, "changes",
                [row for row in inventory.get("changes", []) if row.get("file") in assigned_set],
            ),
            "hunks": _compact_section(
                review_id, lane_id, "hunks",
                [{"file": file, "hunk": hunks.get(file, "")} for file in assigned],
            ),
            "coherence": _compact_section(review_id, lane_id, "coherence", filtered_coherence),
            "contract_changes": _compact_section(
                review_id, lane_id, "contract_changes", filtered_contract,
                applicable="Contract" in decision["activated_checks"],
                failed=filtered_contract.get("capability") == "failed",
            ),
            "dependency_summary": _compact_section(
                review_id, lane_id, "dependency_summary", dependency,
                applicable=lane == "Core Review" and dependency is not None,
            ),
        }
        omitted = [
            {"section": name, "reason": ", ".join(section["omission_reasons"]),
             "cursor": section.get("cursor"), "item_ids": section.get("previewed_item_ids", []),
             "omitted_count": section.get("omitted_count", 0)}
            for name, section in sections.items() if section["status"] == "truncated"
        ]
        bundle = {
            "review_id": review_id,
            "lane_id": lane_id,
            "lane_display_name": lane,
            "base_sha": snapshot.base_sha,
            "merge_base_sha": snapshot.merge_base_sha,
            "head_sha": snapshot.head_sha,
            "run": decision["run"],
            "reason": decision["reason"],
            "activated_checks": decision["activated_checks"],
            "sections": sections,
            "lookup_budget": 4,
            "response_budget": _response_budget(hunks, assigned),
            "investigative_response_budget": _response_budget(hunks, assigned) - 2,
            "reserved_response_budget": 2,
            "omitted_sections": omitted,
            "suggested_lookup_questions": [
                (f"Retrieve omitted {item['section']} evidence with cursor {item['cursor']}"
                 if item["cursor"] else
                 f"Retrieve previewed {item['section']} items by ID: {', '.join(item['item_ids'])}")
                for item in omitted
            ],
            "delivery_status": "unverified",
            "coverage_receipt": [
                f"{row.get('file')}:{row.get('symbol', '<hunk>')}"
                for row in inventory.get("changes", []) if row.get("file") in assigned_set
            ],
        }
        bundle["payload_bytes"] = _payload_bytes(bundle)
        bundle["bundle_digest"] = bundle_digest(bundle)
        if transport.envelope_bytes(canonical_json(bundle)) > transport.MAX_MCP_RESPONSE_BYTES:
            raise RuntimeError(f"compact bundle exceeds MCP response cap: {lane}")
        bundles[lane_id] = bundle
        opaque_routing[lane_id] = {**deepcopy(decision), "lane_id": lane_id, "display_name": lane,
                                   "assigned_file_count": len(assigned)}
        opaque_routing[lane_id].pop("assigned_files", None)
    assigned_all = set().union(*(set(item["assigned_files"]) for item in lanes.values())) if lanes else set()
    excluded = [
        {"file": path, "reason": "binary or unsupported non-text artifact; no semantic review evidence available"}
        for path in files if path not in assigned_all
    ]
    plan = {
        "review_id": review_id,
        "base": snapshot.requested_base_ref,
        "revision": snapshot.public_dict(),
        "snapshot": snapshot,
        "inventory": inventory,
        "summary": {
            "changed_file_count": len(files),
            "changed_symbol_count": inventory.get("total", 0),
            "parse_errors": inventory.get("parse_errors", []),
            "technology": profile,
            "coherence_counts": {
                name: len(value) for name, value in coherence.items() if isinstance(value, list)
            },
        },
        "routing": opaque_routing,
        "unassigned_changed_files": [],
        "excluded_changed_files": excluded,
        "bundle_ids": {lane_id: f"{review_id}:{lane_id}" for lane_id in bundles},
        "bundles": bundles,
    }
    plan["payload_bytes"] = _payload_bytes({key: value for key, value in plan.items() if key != "bundles"})
    active = {name: bundle for name, bundle in bundles.items() if bundle["run"]}
    _metrics[review_id] = {"review_id": review_id, "successful_lookup_count": 0,
                           "actual_model_responses": None, "response_budget": sum(bundle["response_budget"] for bundle in active.values()),
                           "verifier_model_responses": 0,
                           "lanes": {name: {"display_name": bundle["lane_display_name"],
                                             "configured_model": "haiku", "observed_model": None,
                                             "response_budget": bundle["response_budget"], "lookup_limit": 4,
                                             "lookup_attempts": 0, "executed_lookups": 0, "rejected_lookups": 0,
                                             "lookup_quota_consumed": 0, "tool_calls": {}, "bundle_bytes": 0,
                                             "fetched": False, "fetch_count": 0, "acknowledged": False,
                                             "delivery_status": "unverified"}
                                     for name, bundle in active.items()},
                           "lookups": {"accepted": {}, "rejected": {}, "exhausted": {}}, "tool_names": [],
                           "bundle_bytes_delivered": 0, "truncated_sections": sum(len(b["omitted_sections"]) for b in bundles.values()),
                           "verification_candidates": [], "verdicts": {}, "unanswered_opened": [], "unanswered_resolved": [],
                           "consolidation_passes": 0, "anchor_verification_batch_count": 0,
                           "lane_agents": len(active), "verifier_agents": 0, "aggregate_wait_count": 0,
                           "poll_wakeup_count": 0, "fallback_agent_count": 0,
                           "host_observations": [], "policy_violations": [],
                           "provenance": {"dispatch": "unavailable", "model": "unavailable",
                                          "tools": "unavailable", "responses": "unavailable",
                                          "waits": "unavailable"}}
    _metrics[review_id]["created_at"] = time.monotonic()
    return plan


def _plan_for(review_id: str) -> dict | None:
    return _plans.get(review_id)


def review_snapshot(review_id: str) -> git_analyzer.ReviewSnapshot | None:
    plan = _plan_for(review_id)
    return plan.get("snapshot") if plan else None


def discard_review(review_id: str) -> bool:
    """Evict a snapshot and every piece of state keyed to it."""
    plan = _plans.pop(review_id, None)
    if plan is None:
        return False
    git_analyzer.release_snapshot(plan["snapshot"])
    _metrics.pop(review_id, None)
    _consolidations.pop(review_id, None)
    for key in [key for key in _lookup_usage if key[0] == review_id or key[0].startswith(f"{review_id}:")]:
        _lookup_usage.pop(key, None)
    for verification_id, batch in list(_verification_batches.items()):
        if batch.get("review_id") == review_id:
            _verification_batches.pop(verification_id, None)
    for key in [key for key in _verification_results if key[0] == review_id]:
        _verification_results.pop(key, None)
    for key in [key for key in _verification_lookup_receipts if key[0] == review_id]:
        _verification_lookup_receipts.pop(key, None)
    for key in [key for key in change_graph._cache if key[1] == review_id]:
        change_graph._cache.pop(key, None)
    snapshot_root = plan["snapshot"].source_root
    for key in [key for key in query._source_cache if key[0] == snapshot_root]:
        query._source_cache.pop(key, None)
    for key in [key for key in _evidence_items if key[0] == review_id]:
        _evidence_items.pop(key, None)
    for key in [key for key in _evidence_sections if key[0] == review_id]:
        _evidence_sections.pop(key, None)
    for key, state in list(_evidence_cursors.items()):
        if state[0] == review_id:
            _evidence_cursors.pop(key, None)
    transport.discard_review(review_id)
    return True


def prepare_review(root: str, *, mode: str, base_ref: str | None = None,
                   expected_base_sha: str | None = None,
                   expected_head_sha: str | None = None,
                   expected_changed_paths: list[str] | None = None) -> dict:
    """Prepare one immutable committed review snapshot."""
    snapshot: git_analyzer.ReviewSnapshot | None = None
    try:
        snapshot = git_analyzer.prepare_snapshot(
            root, mode=mode, base_ref=base_ref, expected_base_sha=expected_base_sha,
            expected_head_sha=expected_head_sha,
            expected_changed_paths=expected_changed_paths,
        )
        plan = _plans.get(snapshot.snapshot_id)
        if plan is None:
            plan = _build_plan(snapshot)
            _plans[snapshot.snapshot_id] = plan
        else:
            # Dirtiness is diagnostic metadata, not review identity. Refresh
            # only that flag while retaining the original immutable source.
            plan["revision"]["worktree_dirty"] = snapshot.worktree_dirty
            git_analyzer.release_snapshot(snapshot)
    except git_analyzer.SnapshotError as exc:
        if snapshot is not None and snapshot.snapshot_id not in _plans:
            git_analyzer.release_snapshot(snapshot)
        return {"review_id": None, "routing": {}, "bundle_ids": {}, "payload_bytes": 0, **exc.as_dict()}
    return {
        "status": "prepared",
        "review_id": plan["review_id"],
        "base": plan["base"],
        "revision": plan["revision"],
        "summary": plan["summary"],
        "routing": plan["routing"],
        "bundle_ids": plan["bundle_ids"],
        "payload_bytes": plan["payload_bytes"],
    }


def get_review_bundle(review_id: str, lane_id: str) -> dict:
    """Retrieve a bundle only from the snapshot named by ``review_id``."""
    plan = _plan_for(review_id)
    if plan is None:
        return {
            "review_id": review_id,
            "lane_id": lane_id,
            "reason": "unknown or evicted review_id",
            "payload_bytes": 0,
        }
    if lane_id not in plan["bundles"]:
        return {
            "review_id": review_id,
            "lane_id": lane_id,
            "reason": "unknown opaque lane_id",
            "available_lane_ids": sorted(plan["bundles"]),
            "payload_bytes": 0,
        }
    bundle = plan["bundles"][lane_id]
    record_bundle_delivery(review_id, bundle)
    return deepcopy(bundle)


def build_lane_dispatch(bundle: dict, guidance: str = "") -> dict:
    """Create an attestable dispatch payload whose exact bytes are recorded."""
    payload = canonical_json({"schema_version": dispatch_adapter.LOOKUP_SCHEMA_VERSION,
                              "bundle": bundle, "guidance": guidance})
    digest = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    plan = _plan_for(str(bundle.get("review_id", "")))
    if plan is not None and bundle.get("lane_id") in plan["bundles"]:
        plan.setdefault("dispatches", {})[bundle["lane_id"]] = {
            "payload": payload, "digest": digest, "payload_bytes": len(payload.encode("utf-8")),
        }
    return {"schema_version": dispatch_adapter.LOOKUP_SCHEMA_VERSION,
            "model": dispatch_adapter.CONFIGURED_MODEL, "lane_id": bundle.get("lane_id"),
            "lane_display_name": bundle.get("lane_display_name"), "prompt_payload": payload,
            "prompt_payload_digest": digest, "prompt_payload_bytes": len(payload.encode("utf-8")),
            "delivery_status": "unverified", "dispatch_adapter": {
                "phase": "lane", "allowed_tools": sorted(dispatch_adapter.LANE_TOOLS),
                "response_budget": bundle.get("response_budget"),
                "host_enforcement": "unavailable until a RestrictedHost integration launches this request",
            }}


def build_verifier_dispatch(review_id: str, verification_id: str, guidance: str = "") -> dict:
    """Build the production verifier payload from registered candidate schema.

    Candidate IDs and anchor IDs are server generated so a verifier never has
    to reconstruct old-side line numbers or guess severity reason codes.
    """
    batch = _verification_batches.get(verification_id)
    if not batch or batch.get("review_id") != review_id:
        return {"code": "UNKNOWN_SCOPE", "reason": "unknown verification batch"}
    from . import review_results
    candidates = []
    for candidate_id, candidate in sorted(batch["candidates"].items()):
        item = deepcopy(candidate)
        cause = item.get("changed_cause_anchor", {})
        item["cause_anchor_id"] = "anchor_" + hashlib.sha256(canonical_json(cause).encode()).hexdigest()[:20]
        item["affected_anchor_ids"] = [
            "anchor_" + hashlib.sha256(canonical_json(anchor).encode()).hexdigest()[:20]
            for anchor in item.get("affected_site_anchors", [])
        ]
        item["valid_severity_reason_codes"] = {
            severity: sorted(codes) for severity, codes in review_results.SEVERITY_REASON_CODES.items()
        }
        candidates.append(item)
    payload = canonical_json({"schema_version": dispatch_adapter.LOOKUP_SCHEMA_VERSION,
                              "review_id": review_id, "verification_id": verification_id,
                              "candidates": candidates, "guidance": guidance})
    digest = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
    return {"schema_version": dispatch_adapter.LOOKUP_SCHEMA_VERSION, "review_id": review_id,
            "verification_id": verification_id, "model": dispatch_adapter.CONFIGURED_MODEL,
            "prompt_payload": payload, "prompt_payload_digest": digest,
            "prompt_payload_bytes": len(payload.encode()), "candidates": candidates,
            "dispatch_adapter": {"phase": "verifier", "allowed_tools": sorted(dispatch_adapter.VERIFIER_TOOLS),
                                 "response_budget": 6,
                                 "host_enforcement": "unavailable until a capable RestrictedHost launches this request"}}


def acknowledge_lane_dispatch(review_id: str, lane_id: str, embedded_payload: str,
                              embedded_digest: str) -> dict:
    """Verify the exact payload embedded by the launcher, not a reconstructed object."""
    plan = _plan_for(review_id)
    expected = plan.get("dispatches", {}).get(lane_id) if plan else None
    if expected is None:
        return {"ok": False, "delivery_status": "rejected", "reason": "dispatch was not issued"}
    actual_digest = "sha256:" + hashlib.sha256(embedded_payload.encode("utf-8")).hexdigest()
    ok = embedded_payload == expected["payload"] and embedded_digest == expected["digest"] == actual_digest
    lane = _metrics.get(review_id, {}).get("lanes", {}).get(lane_id)
    if lane:
        lane["acknowledged"] = ok
        lane["delivery_status"] = "verified" if ok else "rejected"
        lane["embedded_payload_bytes"] = len(embedded_payload.encode("utf-8"))
        lane["expected_payload_bytes"] = expected["payload_bytes"]
    return {"ok": ok, "review_id": review_id, "lane_id": lane_id,
            "delivery_status": "verified" if ok else "rejected",
            "expected_payload_bytes": expected["payload_bytes"],
            "embedded_payload_bytes": len(embedded_payload.encode("utf-8")),
            "reason": None if ok else "embedded payload bytes or digest differ from issued dispatch"}


def record_lane_model(review_id: str, lane_id: str, observed_model: str | None) -> None:
    """Launcher hook; omitted observations remain an explicit policy violation."""
    record = _metrics.get(review_id, {}).get("lanes", {}).get(lane_id)
    if record:
        record["observed_model"] = observed_model


def record_dispatch_cycle(review_id: str, lane_ids: list[str], observed_models: dict[str, str] | None = None,
                          aggregate_waits: int = 1, poll_wakeups: int = 0, fallback_agents: int = 0) -> dict:
    """Deprecated caller-supplied counters; retained only as a non-compliant compatibility response."""
    metrics = _metrics.get(review_id)
    if not metrics:
        return {"ok": False, "reason": "unknown review"}
    return {"ok": False, "code": "HOST_EVENT_INGESTION_REQUIRED",
            "reason": "caller-supplied dispatch counters are not authoritative; ingest host observations instead"}


def ingest_host_observations(review_id: str, observations: list[dict], *,
                             provenance: str = "self-reported") -> dict:
    """Ingest normalized host events; only an in-process adapter may mark them verified."""
    metrics = _metrics.get(review_id)
    if not metrics:
        return {"ok": False, "reason": "unknown review"}
    if provenance not in {"verified", "self-reported"}:
        return {"ok": False, "reason": "invalid observation provenance"}
    errors: list[dict] = []
    accepted: list[dict] = []
    prepared: list[tuple[dict, list[str]]] = []
    total_responses = 0
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            errors.append({"index": index, "reason": "observation must be an object"})
            continue
        lane_id = observation.get("lane_id")
        phase = observation.get("phase")
        if lane_id not in metrics["lanes"] or phase not in {"lane", "verifier"}:
            errors.append({"index": index, "reason": "unknown lane_id or phase"})
            continue
        response_count = observation.get("response_count")
        if not isinstance(response_count, int) or response_count < 0:
            errors.append({"index": index, "reason": "response_count must be a non-negative integer"})
            continue
        tools = observation.get("tool_calls", [])
        if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
            errors.append({"index": index, "reason": "tool_calls must be a string list"})
            continue
        model_id = observation.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            errors.append({"index": index, "reason": "model_id is required"})
            continue
        allowed = dispatch_adapter.VERIFIER_TOOLS if phase == "verifier" else dispatch_adapter.LANE_TOOLS
        exact_tools = observation.get("tool_observation_exact", False)
        if not isinstance(exact_tools, bool):
            errors.append({"index": index, "reason": "tool_observation_exact must be boolean"})
            continue
        forbidden = sorted(set(tools) - allowed) if exact_tools else []
        waits = observation.get("aggregate_waits", 0)
        polls = observation.get("poll_wakeups", 0)
        rejected_tools = observation.get("rejected_tool_calls", 0)
        if (not isinstance(waits, int) or waits < 0 or not isinstance(polls, int) or polls < 0
                or not isinstance(rejected_tools, int) or rejected_tools < 0):
            errors.append({"index": index, "reason": "wait counts must be non-negative integers"})
            continue
        total_responses += response_count
        prepared.append((deepcopy(observation), forbidden))
    if errors:
        return {"ok": False, "errors": errors, "accepted_count": 0}
    for observation, forbidden in prepared:
        lane_id = observation["lane_id"]
        lane = metrics["lanes"][lane_id]
        lane["observed_model"] = observation["model_id"]
        lane["observed_response_count"] = observation["response_count"]
        lane["observed_tool_calls"] = observation["tool_calls"]
        lane["tool_observation_exact"] = observation.get("tool_observation_exact", False)
        lane["rejected_tool_calls"] = observation.get("rejected_tool_calls", 0)
        lane["observation_provenance"] = provenance
        lane["tool_policy"] = "fail" if forbidden else "pass"
        if not observation.get("tool_observation_exact", False):
            metrics["policy_violations"].append({"lane_id": lane_id, "kind": "telemetry-unverified",
                                                  "provenance": provenance})
        if forbidden:
            metrics["policy_violations"].append({"lane_id": lane_id, "kind": "forbidden-tool",
                                                  "tools": forbidden, "provenance": provenance})
        if observation.get("rejected_tool_calls", 0):
            metrics["policy_violations"].append({"lane_id": lane_id, "kind": "rejected-tool-call",
                                                  "count": observation["rejected_tool_calls"],
                                                  "provenance": provenance})
        if observation["model_id"] != lane["configured_model"]:
            metrics["policy_violations"].append({"lane_id": lane_id, "kind": "model-policy",
                                                  "observed": observation["model_id"],
                                                  "expected": lane["configured_model"], "provenance": provenance})
        metrics["aggregate_wait_count"] += observation.get("aggregate_waits", 0)
        metrics["poll_wakeup_count"] += observation.get("poll_wakeups", 0)
        if observation["phase"] == "verifier":
            metrics["verifier_model_responses"] += observation["response_count"]
        accepted.append(observation)
    metrics["host_observations"].extend(accepted)
    metrics["actual_model_responses"] = (metrics["actual_model_responses"] or 0) + total_responses
    for field in ("dispatch", "model", "tools", "responses", "waits"):
        metrics["provenance"][field] = provenance
    return {"ok": True, "accepted_count": len(accepted), "provenance": provenance}


def validate_lane_result(review_id: str, result: dict) -> dict:
    """Reject cross-review/lane results before any finding enters consolidation."""
    plan = _plan_for(review_id)
    if plan is None:
        return {"ok": False, "reason": "unknown or evicted review_id", "result": deepcopy(result)}
    lane_id = result.get("lane_id")
    expected = plan["bundles"].get(lane_id)
    required = ("review_id", "lane_id")
    missing = [field for field in required if not result.get(field)]
    if missing:
        return {"ok": False, "reason": "missing identity fields: " + ", ".join(missing), "result": deepcopy(result)}
    if review_id != plan["review_id"] or result["review_id"] != review_id:
        return {"ok": False, "reason": "review_id does not match authoritative review", "result": deepcopy(result)}
    if not expected:
        return {"ok": False, "reason": "lane does not exist in authoritative review", "result": deepcopy(result)}
    lane_metrics = _metrics.get(review_id, {}).get("lanes", {}).get(lane_id, {})
    if not lane_metrics.get("acknowledged"):
        return {"ok": False, "reason": "lane dispatch was not acknowledged", "result": deepcopy(result)}
    bound = deepcopy(result)
    for field in ("base_sha", "head_sha", "bundle_digest"):
        if field in result and result[field] != expected[field]:
            return {"ok": False, "reason": f"{field} does not match authoritative bundle", "result": deepcopy(result)}
        bound[field] = expected[field]
    bound["snapshot_base_sha"] = expected.get("base_sha")
    bound["diff_merge_base_sha"] = review_snapshot(review_id).merge_base_sha if review_snapshot(review_id) else None
    if result.get("findings") == []:
        receipt = result.get("coverage")
        if not isinstance(receipt, list):
            return {"ok": False, "reason": "zero-finding lane requires coverage receipt", "result": deepcopy(result)}
        statuses = {"reviewed", "irrelevant", "retrieved", "unresolved"}
        classified = {item.get("id") for item in receipt if isinstance(item, dict) and item.get("status") in statuses}
        missing_coverage = set(expected.get("coverage_receipt", [])) - classified
        if missing_coverage:
            return {"ok": False, "reason": "coverage receipt leaves changed causes unclassified",
                    "missing_coverage": sorted(missing_coverage), "result": deepcopy(result)}
        if any(item.get("status") == "unresolved" for item in receipt if isinstance(item, dict)):
            return {"ok": False, "reason": "coverage receipt contains unresolved changed causes", "result": deepcopy(result)}
        bound["coverage"] = deepcopy(receipt)
    return {"ok": True, "result": bound}


def changed_file_lookup(review_id: str, file: str) -> dict:
    """Answer membership/status questions from the prepared diff, never content search."""
    plan = _plan_for(review_id)
    if plan is None:
        return {"code": "UNKNOWN_REVIEW", "quota_consumed": False}
    normalized = Path(file).as_posix()
    rows = [row for row in plan["inventory"].get("changes", []) if row.get("file") == normalized]
    lanes = [lane_id for lane_id, item_ids in (
        (candidate, _evidence_sections.get((review_id, candidate, "files"), []))
        for candidate in plan["bundles"]
    ) if any(_evidence_items[(review_id, item_id)].get("value") == normalized for item_id in item_ids)]
    return {"file": normalized, "changed": bool(rows), "status": rows[0].get("change") if rows else None,
            "lane_ids": lanes, "quota_consumed": True}


def begin_verification(review_id: str, candidates: list[dict]) -> dict:
    """Register one bounded verification batch and return its opaque ID."""
    plan = _plan_for(review_id)
    if plan is None:
        return {"reason": "unknown or evicted review_id"}
    safe_candidates = [candidate for candidate in candidates if isinstance(candidate.get("candidate_id"), str)]
    if not safe_candidates:
        _metrics[review_id]["verification_candidates"] = []
        return {"verification_id": None, "candidates": [], "max_lookup_calls_per_candidate": 6,
                "dispatch_required": False, "reason": "no surviving candidates require verification"}
    digest = hashlib.sha256(json.dumps(safe_candidates, sort_keys=True, default=str).encode()).hexdigest()[:16]
    verification_id = f"{review_id}:verify:{digest}"
    _verification_batches[verification_id] = {
        "review_id": review_id,
        "candidate_ids": {candidate["candidate_id"] for candidate in safe_candidates},
        "candidates": {candidate["candidate_id"]: deepcopy(candidate) for candidate in safe_candidates},
    }
    _metrics[review_id]["verification_candidates"] = [candidate["candidate_id"] for candidate in safe_candidates]
    return {"verification_id": verification_id, "candidates": safe_candidates, "max_lookup_calls_per_candidate": 6,
            "dispatch_required": True}


def _verification_candidate(review_id: str, verification_id: str, candidate_id: str) -> dict | None:
    batch = _verification_batches.get(verification_id)
    if not batch or batch.get("review_id") != review_id:
        return None
    return batch.get("candidates", {}).get(candidate_id)


def record_verification_lookup(review_id: str, verification_id: str, candidate_id: str,
                               operation: str, result: object) -> str | None:
    """Issue an opaque receipt for a successful candidate-scoped typed call."""
    if _verification_candidate(review_id, verification_id, candidate_id) is None:
        return None
    payload = canonical_json({"review_id": review_id, "verification_id": verification_id,
                              "candidate_id": candidate_id, "operation": operation, "result": result})
    receipt = "vr_" + hashlib.sha256(payload.encode()).hexdigest()[:24]
    key = (review_id, verification_id, candidate_id)
    _verification_lookup_receipts.setdefault(key, []).append(receipt)
    return receipt


def deterministic_source_verification(review_id: str, verification_id: str, candidate_id: str) -> dict:
    """Verify registered source anchors without trusting orchestrator prose.

    This deliberately yields PLAUSIBLE rather than claiming an end-to-end
    causal confirmation: it establishes pinned source facts only.
    """
    candidate = _verification_candidate(review_id, verification_id, candidate_id)
    snapshot = review_snapshot(review_id)
    if candidate is None or snapshot is None:
        return {"code": "UNKNOWN_SCOPE", "reason": "unknown verification candidate"}
    from . import query
    claim = {"changed_cause_anchor": candidate.get("changed_cause_anchor", {}),
             "affected_site_anchors": candidate.get("affected_site_anchors", [])}
    checks = query.verify_changed_anchors(snapshot.repository_root, snapshot.diff_base_sha, [claim],
                                          head=snapshot.diff_head_sha, source_root=snapshot.source_root)
    anchor = checks.get("results", [{}])[0]
    verdict = "PLAUSIBLE" if anchor.get("ok") else "REFUTED"
    result = {"candidate_id": candidate_id, "verdict": verdict,
              "evidence": [anchor], "provenance": "server-deterministic-source-facts",
              "verification_id": verification_id}
    _verification_results[(review_id, verification_id, candidate_id)] = deepcopy(result)
    return result


def register_verifier_result(review_id: str, verification_id: str, candidate_id: str,
                             verdict: str, receipt_ids: list[str]) -> dict:
    """Register a verifier verdict only when it cites recorded typed-call receipts."""
    if verdict not in {"CONFIRMED", "PLAUSIBLE", "REFUTED"}:
        return {"code": "INVALID_VERDICT", "reason": "verdict must be CONFIRMED, PLAUSIBLE, or REFUTED"}
    if _verification_candidate(review_id, verification_id, candidate_id) is None:
        return {"code": "UNKNOWN_SCOPE", "reason": "unknown verification candidate"}
    issued = set(_verification_lookup_receipts.get((review_id, verification_id, candidate_id), []))
    if not receipt_ids or not all(isinstance(item, str) and item in issued for item in receipt_ids):
        return {"code": "UNREGISTERED_EVIDENCE", "reason": "verifier results require candidate-scoped typed-call receipts"}
    result = {"candidate_id": candidate_id, "verdict": verdict, "evidence": list(receipt_ids),
              "provenance": "registered-typed-tool-verifier", "verification_id": verification_id}
    _verification_results[(review_id, verification_id, candidate_id)] = deepcopy(result)
    return result


def registered_verification_results(review_id: str, proposed: list[dict] | None) -> tuple[list[dict], list[dict]]:
    """Return only server-issued or receipt-backed verdicts for consolidation."""
    accepted, rejected = [], []
    for item in proposed or []:
        if not isinstance(item, dict):
            rejected.append({"reason": "verification result must be an object", "result": item})
            continue
        key = (review_id, item.get("verification_id"), item.get("candidate_id"))
        registered = _verification_results.get(key)
        if registered is None or canonical_json(registered) != canonical_json(item):
            rejected.append({"reason": "verification result is not server-issued or registered", "result": deepcopy(item)})
        else:
            accepted.append(deepcopy(registered))
    return accepted, rejected


def consume_lookup_budget(review_id: str, scope: str, *, verification_id: str = "", candidate_id: str = "") -> dict:
    """Atomically consume a lane or candidate lookup allowance."""
    if verification_id:
        batch = _verification_batches.get(verification_id)
        if not batch or batch["review_id"] != review_id or candidate_id not in batch["candidate_ids"]:
            return {"ok": False, "reason": "unknown verification candidate", "code": "UNKNOWN_SCOPE"}
        key, limit = (verification_id, candidate_id), 6
    else:
        plan = _plan_for(review_id)
        if not plan or scope not in plan["bundles"]:
            return {"ok": False, "reason": "unknown review lane", "code": "UNKNOWN_SCOPE"}
        key, limit = (review_id, scope), plan["bundles"][scope]["lookup_budget"]
    used = _lookup_usage.get(key, 0)
    if used >= limit:
        _metrics.get(review_id, {}).get("lookups", {}).get("exhausted", {}).setdefault(scope or candidate_id, 0)
        _metrics[review_id]["lookups"]["exhausted"][scope or candidate_id] += 1
        lane = _metrics[review_id].get("lanes", {}).get(scope)
        if lane:
            lane["lookup_attempts"] += 1
            lane["rejected_lookups"] += 1
        return {"ok": False, "reason": "lookup budget exhausted", "remaining": 0, "code": "BUDGET_EXHAUSTED"}
    _lookup_usage[key] = used + 1
    _metrics[review_id]["lookups"]["accepted"].setdefault(scope or candidate_id, 0)
    _metrics[review_id]["lookups"]["accepted"][scope or candidate_id] += 1
    lane = _metrics[review_id].get("lanes", {}).get(scope)
    if lane:
        lane["lookup_attempts"] += 1
        lane["executed_lookups"] += 1
        lane["lookup_quota_consumed"] += 1
    return {"ok": True, "remaining": limit - used - 1}


def remaining_lookup_budget(review_id: str, scope: str, *, verification_id: str = "", candidate_id: str = "") -> int | None:
    """Report remaining quota without mutating it, including malformed attempts."""
    if verification_id:
        batch = _verification_batches.get(verification_id)
        if not batch or batch["review_id"] != review_id or candidate_id not in batch["candidate_ids"]:
            return None
        return max(0, 6 - _lookup_usage.get((verification_id, candidate_id), 0))
    plan = _plan_for(review_id)
    if not plan or scope not in plan["bundles"]:
        return None
    return max(0, plan["bundles"][scope]["lookup_budget"] - _lookup_usage.get((review_id, scope), 0))


def record_lookup_rejection(review_id: str, scope: str, operation: str) -> None:
    metrics = _metrics.get(review_id)
    if metrics:
        metrics["tool_names"].append(operation)
        metrics["lookups"]["rejected"][scope] = metrics["lookups"]["rejected"].get(scope, 0) + 1
        lane = metrics.get("lanes", {}).get(scope)
        if lane:
            lane["lookup_attempts"] += 1
            lane["rejected_lookups"] += 1
            lane["tool_calls"][operation] = lane["tool_calls"].get(operation, 0) + 1


def record_bundle_delivery(review_id: str, bundle: dict) -> None:
    if review_id in _metrics:
        _metrics[review_id]["bundle_bytes_delivered"] += bundle.get("payload_bytes", 0)
        lane = _metrics[review_id].get("lanes", {}).get(bundle.get("lane_id"))
        if lane:
            lane["bundle_bytes"] += bundle.get("payload_bytes", 0)
            lane["fetched"] = True
            lane["fetch_count"] += 1


def record_agent_response(review_id: str, tool_name: str = "", lane_name: str = "") -> None:
    if review_id in _metrics:
        _metrics[review_id]["successful_lookup_count"] += 1
        if tool_name:
            _metrics[review_id]["tool_names"].append(tool_name)
            lane = _metrics[review_id].get("lanes", {}).get(lane_name)
            if lane:
                lane["tool_calls"][tool_name] = lane["tool_calls"].get(tool_name, 0) + 1


def get_metrics(review_id: str) -> dict:
    metrics = deepcopy(_metrics.get(review_id, {"review_id": review_id, "code": "UNKNOWN_REVIEW"}))
    if "code" in metrics:
        return metrics
    lanes = metrics.get("lanes", {}).values()
    responses = metrics.get("actual_model_responses")
    metrics["compliance"] = {
        "lookup_budget": "pass" if all(item["executed_lookups"] <= item["lookup_limit"] for item in lanes) else "fail",
        "response_budget": "unavailable" if responses is None else (
            "pass" if responses <= metrics.get("response_budget", 0) else "fail"
        ),
        "aggregate_wait": "pass" if metrics.get("aggregate_wait_count") <= 1 else "fail",
        "legacy_fallback": "pass" if metrics.get("fallback_agent_count") == 0 else "fail",
        "anchor_batching": "pass" if metrics.get("anchor_verification_batch_count", 0) <= metrics.get("consolidation_passes", 0) else "fail",
        "model_policy": "unavailable" if metrics["provenance"].get("model") == "unavailable" else (
            "pass" if all(item.get("observed_model") == item.get("configured_model") for item in lanes) else "fail"
        ),
        "tool_policy": "unavailable" if metrics["provenance"].get("tools") == "unavailable" else (
            "pass" if not any(item.get("kind") in {"forbidden-tool", "rejected-tool-call"}
                              for item in metrics["policy_violations"]) else "fail"
        ),
    }
    metrics["duration_seconds"] = round(time.monotonic() - metrics.get("created_at", time.monotonic()), 6)
    metrics["token_categories"] = {"bundle": "unavailable", "lane": "unavailable", "verifier": "unavailable"}
    return metrics


def get_review_status(review_id: str) -> dict:
    """Return stored coverage and a status independent of the finding count."""
    plan = _plan_for(review_id)
    if plan is None:
        return {"review_id": review_id, "review_status": "invalid-base",
                "reason": "unknown or evicted review; no authoritative snapshot exists"}
    metrics = get_metrics(review_id)
    snapshot = plan["snapshot"]
    try:
        current_target_sha = git_analyzer._resolve_commit(snapshot.repository_root, snapshot.requested_base_ref, "current target")
    except git_analyzer.SnapshotError:
        current_target_sha = None
    stale_target = current_target_sha is not None and current_target_sha != snapshot.base_sha
    state = _consolidations.get(review_id, {})
    active = [(lane_id, item) for lane_id, item in plan["bundles"].items() if item.get("run")]
    skipped = [item["lane_display_name"] for _, item in plan["bundles"].items() if not item.get("run")]
    rejected_lookups = sum(metrics.get("lookups", {}).get("rejected", {}).values())
    evidence_unverified = [lane_id for lane_id, _ in active
                           if not metrics["lanes"].get(lane_id, {}).get("acknowledged")]
    degraded_reasons: list[str] = []
    if evidence_unverified:
        degraded_reasons.append("lane evidence delivery is unverified")
    if any(value != "verified" for value in metrics.get("provenance", {}).values()):
        degraded_reasons.append("host dispatch observations are not verified")
    if metrics["compliance"].get("model_policy") == "fail":
        degraded_reasons.append("observed model violates configured policy")
    if metrics["compliance"].get("tool_policy") == "fail":
        degraded_reasons.append("observed tool use violates restricted allowlist")
    if any(item.get("kind") == "telemetry-unverified" for item in metrics.get("policy_violations", [])):
        degraded_reasons.append("telemetry-unverified")
    if metrics.get("provenance", {}).get("dispatch") != "verified":
        degraded_reasons.append("dispatch-contract-mismatch")
    if metrics["compliance"].get("response_budget") == "fail":
        degraded_reasons.append("observed responses exceed the advisory budget")
    if rejected_lookups and not metrics.get("successful_lookup_count"):
        degraded_reasons.append("all attempted lookups were rejected")
    if stale_target:
        degraded_reasons.append("stale-pr: current target differs from snapshot base; delta coverage is not integration coverage")
    if degraded_reasons:
        status = "degraded"
    elif review_id not in _consolidations:
        status = "incomplete"
    elif state.get("rejected_lane_results") or state.get("unanswered") or any(item.get("disposition") == "unresolved"
                                         for item in state.get("candidate_dispositions", [])):
        status = "incomplete"
    else:
        status = "complete"
    omitted = sum(len(item.get("omitted_sections", [])) for _, item in active)
    return {
        "review_id": review_id, "review_status": status, "degraded_reasons": degraded_reasons,
        "snapshot_base_sha": plan["revision"].get("snapshot_base_sha"),
        "diff_merge_base_sha": plan["revision"].get("diff_merge_base_sha"),
        "head_sha": plan["revision"].get("head_sha"), "current_target_sha": current_target_sha,
        "coverage_mode": "stale-pr-delta" if stale_target else "pr-delta",
        "mode": plan["revision"].get("mode"), "changed_files": plan["summary"].get("changed_file_count", 0),
        "active_lanes": [item["lane_display_name"] for _, item in active], "skipped_lanes": skipped,
        "evidence": {"omitted_sections": omitted, "fetched_lanes": [lane_id for lane_id, _ in active
                     if metrics["lanes"].get(lane_id, {}).get("fetched")],
                     "acknowledged_lanes": [lane_id for lane_id, _ in active
                     if metrics["lanes"].get(lane_id, {}).get("acknowledged")]},
        "lookups": {"successful": metrics.get("successful_lookup_count", 0), "rejected": rejected_lookups,
                    "exhausted": sum(metrics.get("lookups", {}).get("exhausted", {}).values())},
        "rejected_candidates": len(state.get("rejected_candidates", [])),
        "verifier": {"candidates": len(state.get("verification_candidates", [])),
                     "results": len(state.get("verification_results", [])),
                     "model_responses": metrics.get("verifier_model_responses", 0)},
        "open_questions": len(state.get("unanswered", [])), "metrics_provenance": metrics.get("provenance"),
    }


def authoritative_report_context(review_id: str) -> dict:
    plan = _plan_for(review_id)
    if plan is None:
        return {"code": "UNKNOWN_REVIEW"}
    active_lanes = [item["lane_display_name"] for item in plan["bundles"].values() if item.get("run")]
    return {"reviewed_files": plan["summary"].get("changed_file_count", 0), "active_lanes": active_lanes,
            "status": get_review_status(review_id)}


def record_consolidation(review_id: str, result: dict, resolutions: list[dict] | None = None) -> None:
    metrics = _metrics.get(review_id)
    if not metrics:
        return
    metrics["consolidation_passes"] += 1
    metrics["anchor_verification_batch_count"] += 1
    metrics["verification_candidates"] = [item.get("candidate_id") for item in result.get("verification_candidates", [])]
    known_open = {item.get("question_id") for item in metrics["unanswered_opened"]}
    metrics["unanswered_opened"].extend(item for item in result.get("all_questions", result.get("unanswered", [])) if item.get("question_id") not in known_open)
    known_resolved = {item.get("question_id") for item in metrics["unanswered_resolved"]}
    metrics["unanswered_resolved"].extend(item for item in (resolutions or []) if item.get("question_id") not in known_resolved)
    for finding in result.get("findings", []):
        if finding.get("verdict"):
            anchor = finding.get("changed_cause_anchor", finding)
            key = f"{anchor.get('file', '')}:{anchor.get('line', '')}:{finding.get('root_cause') or finding.get('summary', '')}"
            metrics["verdicts"][key] = finding["verdict"]


def record_question_resolutions(review_id: str, resolutions: list[dict]) -> None:
    """Record only a resolution batch already committed by the state transaction."""
    metrics = _metrics.get(review_id)
    if not metrics:
        return
    known = {item.get("question_id") for item in metrics["unanswered_resolved"]}
    metrics["unanswered_resolved"].extend(
        deepcopy(item) for item in resolutions if item.get("question_id") not in known
    )

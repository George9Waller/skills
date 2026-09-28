"""Validation, deduplication, verification planning, and report rendering."""

from __future__ import annotations

from copy import deepcopy
import hashlib

VERDICTS = frozenset({"CONFIRMED", "PLAUSIBLE", "REFUTED"})
VERIFIED_SEVERITIES = frozenset({"blocking", "warning"})
REACHABILITY_KINDS = frozenset({
    "existing-caller", "supported-api-request", "supported-input", "scheduled-job",
    "worker-task", "deployed-environment", "future-only", "unknown",
})
SEVERITY_REASON_CODES = {
    "blocking": frozenset({"current-production-failure", "broken-existing-contract", "authority-boundary"}),
    "warning": frozenset({"bounded-current-risk", "environment-precondition-unverified"}),
    "refining": frozenset({"maintenance-cost", "future-only", "low-impact"}),
}


def _question_id(review_id: str, lane: str, question: str) -> str:
    normalized = " ".join(question.split()).casefold()
    return "q:" + hashlib.sha256(f"{review_id}\0{lane}\0{normalized}".encode()).hexdigest()[:20]


def normalize_unanswered(review_id: str, lane: str, item: object) -> tuple[dict | None, str | None]:
    """Normalize legacy strings; malformed values are reported, never dropped."""
    if isinstance(item, str) and item.strip():
        question = " ".join(item.split())
        return ({"question_id": _question_id(review_id, lane, question), "lane": lane,
                 "question": question, "why_unresolved": "legacy string-form unanswered question",
                 "suggested_resolution": ""}, "normalized string-form unanswered question")
    if not isinstance(item, dict):
        return None, "unanswered question must be an object or non-empty string"
    question = item.get("question")
    if not isinstance(question, str) or not question.strip():
        return None, "unanswered question is missing concrete question text"
    question = " ".join(question.split())
    normalized = {"question_id": _question_id(review_id, lane, question), "lane": lane,
                  "question": question,
                  "why_unresolved": str(item.get("why_unresolved") or "not supplied"),
                  "suggested_resolution": str(item.get("suggested_resolution") or "")}
    warning = None
    if item.get("question_id") and item["question_id"] != normalized["question_id"]:
        warning = "question_id is server-assigned; supplied ID was replaced"
    elif any(key not in item for key in ("question_id", "lane", "why_unresolved", "suggested_resolution")):
        warning = "unanswered question was normalized to the strict schema"
    return normalized, warning


def finding_id(finding: dict) -> str:
    anchor = finding.get("changed_cause_anchor", {})
    return f"{anchor.get('file', finding.get('file', ''))}:{anchor.get('line', finding.get('line', ''))}:{finding.get('root_cause') or finding.get('summary', '')}"


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _evidence_list(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(
        _nonempty_string(item) or (
            isinstance(item, dict) and _nonempty_string(item.get("reference"))
            and _nonempty_string(item.get("provenance"))
        ) for item in value
    )


def _anchor_errors(anchor: object, label: str) -> list[dict]:
    if not isinstance(anchor, dict):
        return [{"code": "schema-invalid", "field": label, "reason": f"{label} must be an object"}]
    errors = []
    if not _nonempty_string(anchor.get("file")):
        errors.append({"code": "schema-invalid", "field": f"{label}.file", "reason": "file is required"})
    if not isinstance(anchor.get("line"), int) or anchor["line"] < 1:
        errors.append({"code": "schema-invalid", "field": f"{label}.line", "reason": "line must be a positive integer"})
    snippet = anchor.get("anchor_snippet", anchor.get("snippet"))
    if not _nonempty_string(snippet) or "\n" in str(snippet):
        errors.append({"code": "schema-invalid", "field": f"{label}.anchor_snippet",
                       "reason": "anchor_snippet must be one non-empty source line"})
    if label == "changed_cause_anchor" and anchor.get("side", "new") not in {"old", "new"}:
        errors.append({"code": "schema-invalid", "field": f"{label}.side", "reason": "side must be old or new"})
    return errors


def validate_finding(finding: dict) -> dict:
    """Mechanically validate structured claim facts without judging prose keywords."""
    errors = _anchor_errors(finding.get("changed_cause_anchor"), "changed_cause_anchor")
    sites = finding.get("affected_site_anchors", [])
    if not isinstance(sites, list):
        errors.append({"code": "schema-invalid", "field": "affected_site_anchors", "reason": "must be a list"})
        sites = []
    for index, site in enumerate(sites):
        errors.extend(_anchor_errors(site, f"affected_site_anchors[{index}]"))
    if finding.get("severity") not in {"blocking", "warning", "refining"}:
        errors.append({"code": "schema-invalid", "field": "severity", "reason": "invalid severity"})
    if not _nonempty_string(finding.get("summary")):
        errors.append({"code": "schema-invalid", "field": "summary", "reason": "summary is required"})
    reachability = finding.get("reachability")
    if not isinstance(reachability, dict) or reachability.get("kind") not in REACHABILITY_KINDS:
        errors.append({"code": "schema-invalid", "field": "reachability.kind",
                       "reason": "a supported reachability kind is required"})
    elif not _nonempty_string(reachability.get("actual_caller")) and not _nonempty_string(reachability.get("supported_input")):
        errors.append({"code": "schema-invalid", "field": "reachability",
                       "reason": "actual_caller or supported_input is required"})
    for field in ("precondition", "observable_impact"):
        if not _nonempty_string(finding.get(field)):
            errors.append({"code": "schema-invalid", "field": field, "reason": f"{field} is required"})
    if not _evidence_list(finding.get("evidence_refs")):
        errors.append({"code": "schema-invalid", "field": "evidence_refs",
                       "reason": "at least one evidence reference is required"})
    if not isinstance(finding.get("unknowns"), list):
        errors.append({"code": "schema-invalid", "field": "unknowns", "reason": "unknowns must be a list"})
    if not _nonempty_string(finding.get("cheapest_falsifying_check")):
        errors.append({"code": "schema-invalid", "field": "cheapest_falsifying_check",
                       "reason": "the cheapest falsifying check is required"})
    causal_link = finding.get("causal_link")
    if sites and (not isinstance(causal_link, dict) or not _nonempty_string(causal_link.get("mechanism"))
                  or not _evidence_list(causal_link.get("evidence_refs"))):
        errors.append({"code": "unsupported-causal-link", "field": "causal_link",
                       "reason": "affected sites require a mechanism and cited evidence"})
    severity_reason = finding.get("severity_reason")
    allowed_codes = SEVERITY_REASON_CODES.get(finding.get("severity"), frozenset())
    if not isinstance(severity_reason, dict) or severity_reason.get("code") not in allowed_codes \
            or not _nonempty_string(severity_reason.get("rationale")):
        errors.append({"code": "schema-invalid", "field": "severity_reason",
                       "reason": "severity requires a matching reason code and rationale"})
    return {"valid": not errors, "errors": errors, "finding": deepcopy(finding)}


def deduplicate_findings(findings: list[dict]) -> list[dict]:
    """Merge one root cause across files while retaining sites and lanes."""
    groups: dict[str, list[dict]] = {}
    for finding in findings:
        key = str(finding.get("root_cause") or finding.get("summary", "")).strip().casefold()
        if key:
            groups.setdefault(key, []).append(finding)
    merged = []
    for group in groups.values():
        primary = deepcopy(sorted(group, key=finding_id)[0])
        lenses, sites = [], []
        for finding in group:
            lens = finding.get("lens")
            lenses.extend(lens if isinstance(lens, list) else [lens])
            sites.extend(finding.get("affected_site_anchors", []))
            sites.append(finding.get("changed_cause_anchor", {}))
        primary["lens"] = sorted({lens for lens in lenses if isinstance(lens, str)})
        unique_sites = {}
        for site in sites:
            if isinstance(site, dict) and site.get("file") and site.get("line"):
                unique_sites[(site["file"], site["line"])] = deepcopy(site)
        primary["affected_site_anchors"] = [unique_sites[key] for key in sorted(unique_sites)]
        merged.append(primary)
    return sorted(merged, key=lambda item: (
        item.get("changed_cause_anchor", {}).get("file", ""),
        item.get("changed_cause_anchor", {}).get("line", 0),
    ))


def _recorded_independent_evidence(finding: dict) -> bool:
    evidence = finding.get("independent_evidence")
    return isinstance(evidence, list) and any(
        isinstance(item, dict)
        and item.get("provenance") in {"recorded-tool", "independent-source"}
        and _nonempty_string(item.get("reference"))
        for item in evidence
    )


def build_verification_batch(findings: list[dict]) -> dict:
    """Select candidates and give one verifier only their falsifying checks."""
    candidates, skipped = [], []
    for finding in findings:
        severity = finding.get("severity")
        required = severity == "blocking" or (severity == "warning" and not _recorded_independent_evidence(finding))
        if not required:
            skipped.append(finding_id(finding))
            continue
        candidates.append({
            "candidate_id": finding_id(finding), "claim": finding.get("summary"),
            "changed_cause_anchor": deepcopy(finding.get("changed_cause_anchor")),
            "affected_site_anchors": deepcopy(finding.get("affected_site_anchors", [])),
            "reachability": deepcopy(finding.get("reachability")),
            "precondition": finding.get("precondition"),
            "observable_impact": finding.get("observable_impact"),
            "failure_scenario": finding.get("failure_scenario"),
            "critical_assumptions": finding.get("critical_assumptions", []),
            "evidence_refs": finding.get("evidence_refs", []),
            "unknowns": finding.get("unknowns", []),
            "falsifying_checks": finding.get("falsifying_checks", []),
            "cheapest_falsifying_check": finding.get("cheapest_falsifying_check"),
            "max_lookup_calls": 6,
        })
    return {"candidates": candidates, "skipped_candidate_ids": skipped}


def apply_verdicts(findings: list[dict], verification_results: list[dict]) -> dict:
    """Retain only valid, appropriately verified findings for the report."""
    by_id = {item.get("candidate_id"): item for item in verification_results if item.get("verdict") in VERDICTS}
    retained = []
    for finding in findings:
        copied = deepcopy(finding)
        if copied.get("severity") not in VERIFIED_SEVERITIES:
            copied.pop("verdict", None)  # ReportFindings omits verdict for refinements.
            retained.append(copied)
            continue
        if copied.get("severity") == "warning" and _recorded_independent_evidence(copied):
            copied.pop("verdict", None)
            retained.append(copied)
            continue
        result = by_id.get(finding_id(copied))
        if result and result["verdict"] in {"CONFIRMED", "PLAUSIBLE"}:
            copied["verdict"] = result["verdict"]
            copied["verification_provenance"] = result.get("provenance", "unavailable")
            retained.append(copied)
    return {"findings": retained, "verification_output": [deepcopy(item) for item in verification_results]}


def _disposition(finding: dict, disposition: str, evidence: object) -> dict:
    return {"candidate_id": finding_id(finding), "disposition": disposition,
            "evidence": deepcopy(evidence), "finding": deepcopy(finding)}


def validate_question_resolutions(state: dict, resolutions: list[dict]) -> list[dict]:
    """Validate an entire resolution batch before any state or metric mutation."""
    questions = {item.get("question_id"): item for item in state.get("unanswered", [])}
    already = {item.get("question_id") for item in state.get("resolutions", [])}
    seen: set[str] = set()
    errors: list[dict] = []
    for index, resolution in enumerate(resolutions):
        if not isinstance(resolution, dict):
            errors.append({"index": index, "field": "resolution", "reason": "must be an object"})
            continue
        question_id = resolution.get("question_id")
        if not _nonempty_string(question_id):
            errors.append({"index": index, "field": "question_id", "reason": "is required"})
        elif question_id not in questions:
            errors.append({"index": index, "field": "question_id", "reason": "unknown or already resolved"})
        elif question_id in seen or question_id in already:
            errors.append({"index": index, "field": "question_id", "reason": "duplicate resolution"})
        if resolution.get("outcome") not in {"resolved-no-finding", "resolved-new-candidate"}:
            errors.append({"index": index, "field": "outcome", "reason": "invalid outcome"})
        if not _nonempty_string(resolution.get("evidence")):
            errors.append({"index": index, "field": "evidence", "reason": "is required"})
        if not _nonempty_string(resolution.get("resolving_phase")):
            errors.append({"index": index, "field": "resolving_phase", "reason": "is required"})
        if "candidate_id" in resolution and not _nonempty_string(resolution.get("candidate_id")):
            errors.append({"index": index, "field": "candidate_id", "reason": "must be a non-empty string"})
        if isinstance(question_id, str):
            seen.add(question_id)
    return errors


def resolve_questions(state: dict, resolutions: list[dict]) -> dict:
    """Apply a resolution batch copy-on-write; invalid input returns the original unchanged."""
    original = deepcopy(state)
    errors = validate_question_resolutions(original, resolutions)
    if errors:
        return {"ok": False, "errors": errors, "state": original}
    resolved_ids = {item["question_id"] for item in resolutions}
    updated = deepcopy(original)
    updated["unanswered"] = [item for item in updated.get("unanswered", [])
                             if item.get("question_id") not in resolved_ids]
    updated["resolutions"] = updated.get("resolutions", []) + deepcopy(resolutions)
    return {"ok": True, "errors": [], "state": updated,
            "resolved_question_ids": sorted(resolved_ids)}


def consolidate(
    root: str, lane_results: list[dict], verification_results: list[dict] | None = None,
    resolutions: list[dict] | None = None, *, review_id: str = "", previous: dict | None = None,
    base: str | None = None, head: str = "HEAD", source_root: str | None = None,
) -> dict:
    """Deterministically validate, anchor, deduplicate, and preserve questions."""
    from . import query  # avoids a module cycle at server import time
    previous = deepcopy(previous or {})
    old_lanes = previous.get("lane_results", [])
    lane_by_name = {lane.get("lane_id", lane.get("lane", f"prior:{index}")): lane for index, lane in enumerate(old_lanes) if isinstance(lane, dict)}
    lane_by_name.update({lane.get("lane_id", lane.get("lane", f"new:{index}")): lane for index, lane in enumerate(lane_results) if isinstance(lane, dict)})
    effective_lanes = list(lane_by_name.values())
    validation = [validate_finding(finding) for lane in effective_lanes for finding in lane.get("findings", []) if isinstance(finding, dict)]
    valid = [item["finding"] for item in validation if item["valid"]]
    anchor_results = (
        query.verify_changed_anchors(root, base, valid, head=head, source_root=source_root)
        if base else query.verify_anchors(source_root or root, valid)
    ).get("results", [])
    anchored, rejected, dispositions = [], [], []
    for item in validation:
        if not item["valid"]:
            rejected.append({"finding": item["finding"], "reasons": item["errors"]})
            error_codes = {error.get("code") for error in item["errors"] if isinstance(error, dict)}
            disposition = "unsupported-causal-link" if "unsupported-causal-link" in error_codes else "schema-invalid"
            dispositions.append(_disposition(item["finding"], disposition, item["errors"]))
    for finding, anchor in zip(valid, anchor_results):
        if not anchor.get("ok"):
            reason_code = anchor.get("reason_code") or "source-anchor-invalid"
            rejected.append({"finding": finding, "reasons": [anchor]})
            dispositions.append(_disposition(finding, reason_code, anchor))
            continue
        anchored.append(finding)
    reviewable, optional_notes = [], []
    for finding in anchored:
        if finding.get("reachability", {}).get("kind") == "future-only":
            dispositions.append(_disposition(finding, "future-only", finding.get("reachability")))
        elif finding.get("finding_kind") == "editorial" or finding.get("severity_reason", {}).get("code") == "low-impact":
            optional_notes.append(deepcopy(finding))
            dispositions.append(_disposition(finding, "low-impact", finding.get("severity_reason")))
        else:
            reviewable.append(finding)
    groups: dict[str, list[dict]] = {}
    for finding in reviewable:
        groups.setdefault(str(finding.get("root_cause") or finding.get("summary", "")).strip().casefold(), []).append(finding)
    for group in groups.values():
        ordered_group = sorted(group, key=finding_id)
        for duplicate in ordered_group[1:]:
            dispositions.append(_disposition(
                duplicate, "deduplicated", {"retained_candidate_id": finding_id(ordered_group[0])},
            ))
    merged = deduplicate_findings(reviewable)
    batch = build_verification_batch(merged)
    all_verdicts = previous.get("verification_results", []) + list(verification_results or [])
    retained = apply_verdicts(merged, all_verdicts) if verification_results is not None or previous.get("verification_results") else {"findings": merged}
    verdict_by_id = {item.get("candidate_id"): item for item in all_verdicts}
    for finding in merged:
        verdict = verdict_by_id.get(finding_id(finding), {}).get("verdict")
        if verdict == "REFUTED":
            dispositions.append(_disposition(finding, "refuted", verdict_by_id[finding_id(finding)]))
        elif finding.get("severity") == "warning" and _recorded_independent_evidence(finding):
            dispositions.append(_disposition(finding, "retained", {
                "independent_evidence": finding.get("independent_evidence"),
            }))
        elif finding.get("severity") in VERIFIED_SEVERITIES and verdict not in {"CONFIRMED", "PLAUSIBLE"}:
            dispositions.append(_disposition(finding, "unresolved", {
                "reason": "verification required", "verification_candidate": finding_id(finding),
            }))
        else:
            dispositions.append(_disposition(finding, "retained", {"verdict": verdict}))
    questions, warnings = [], []
    for lane in effective_lanes:
        lane_name = str(lane.get("lane_display_name") or lane.get("lane") or "Unknown lane")
        for item in lane.get("unanswered", []):
            normalized, warning = normalize_unanswered(review_id, lane_name, item)
            if normalized:
                questions.append(normalized)
            if warning:
                warnings.append({"lane": lane_name, "warning": warning, "value": item})
    prior_resolutions = deepcopy(previous.get("resolutions", []))
    prior_resolved = {item.get("question_id") for item in prior_resolutions}
    unanswered = [item for item in questions if item["question_id"] not in prior_resolved]
    result = {"findings": retained["findings"], "optional_notes": optional_notes,
            "candidate_dispositions": dispositions,
            "verification_candidates": batch["candidates"], "rejected_candidates": rejected,
            "unanswered": unanswered, "all_questions": questions, "resolutions": prior_resolutions,
            "resolution_errors": [], "normalization_warnings": warnings,
            "lane_results": deepcopy(effective_lanes), "verification_results": deepcopy(all_verdicts),
            "validation_count": len(validation)}
    if resolutions:
        transaction = resolve_questions(result, resolutions)
        if transaction["ok"]:
            return transaction["state"]
        result["resolution_errors"] = transaction["errors"]
    return result


def render_markdown(findings: list[dict], reviewed_files: int, active_lanes: list[str],
                    unanswered: list[dict] | None = None,
                    optional_notes: list[dict] | None = None,
                    review_status: dict | None = None) -> str:
    """Render a host-independent report; refinements intentionally omit verdicts."""
    tiers = {"blocking": [], "warning": [], "refining": []}
    for finding in findings:
        tiers.get(finding.get("severity"), tiers["refining"]).append(finding)
    lines = [
        f"Reviewed {reviewed_files} files; lanes: {', '.join(active_lanes)}. "
        f"Findings: {len(tiers['blocking'])} blocking, {len(tiers['warning'])} warnings, {len(tiers['refining'])} refinements."
    ]
    if review_status:
        lines.extend([
            f"Review status: {review_status.get('review_status', 'incomplete')}.",
            *(f"Coverage note: {reason}." for reason in review_status.get("degraded_reasons", [])),
        ])
    for severity, label in (("blocking", "Tier 1"), ("warning", "Tier 2")):
        for finding in sorted(tiers[severity], key=lambda item: (
            item.get("changed_cause_anchor", {}).get("file", ""),
            item.get("changed_cause_anchor", {}).get("line", 0),
        )):
            lenses = finding.get("lens", [])
            lenses = ", ".join(lenses) if isinstance(lenses, list) else str(lenses)
            anchor = finding.get("changed_cause_anchor", finding)
            lines.extend(["", f"### [{label}] {finding['summary']}",
                          f"**File:** `{anchor.get('file')}:{anchor.get('line')}` · **Lane:** {lenses}",
                          f"**Verdict:** {finding.get('verdict', 'independently evidenced')}"
                          f" · **Provenance:** {finding.get('verification_provenance', 'independently evidenced')}",
                          finding.get("observable_impact", "")])
    if tiers["refining"]:
        lines.extend(["", f"<details><summary>Refinements ({len(tiers['refining'])})</summary>"])
        for finding in sorted(tiers["refining"], key=lambda item: finding_id(item)):
            lines.extend(["", f"### [Tier 3] {finding['summary']}", finding.get("observable_impact", "")])
        lines.extend(["", "</details>"])
    if optional_notes:
        lines.extend(["", f"<details><summary>Optional notes ({len(optional_notes)})</summary>"])
        for finding in sorted(optional_notes, key=finding_id):
            lines.extend(["", f"- {finding.get('summary', 'Low-impact note')}"])
        lines.extend(["", "</details>"])
    if unanswered:
        lines.extend(["", "## Open questions"])
        lines.extend(f"- {item.get('question', item)}" for item in unanswered)
    return "\n".join(lines).strip() + "\n"

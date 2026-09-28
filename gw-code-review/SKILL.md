---
name: gw-code-review
description: Efficient, evidence-led review of a git diff. Uses compact semantic lanes for correctness, integration/docs, and operations; verifies substantive findings without rerunning CI. Use for PR or branch reviews, not lint or style-only checks.
---

# gw-code-review

Review for actionable defects a strong human reviewer would raise. Do not lint,
re-run CI, assess commit messages, or manufacture style observations.

## Start

Require these `gw-code-review` MCP tools: `prepare_review`,
`get_review_bundle`, `get_lane_dispatch`, `acknowledge_lane_dispatch`,
`get_evidence_item`, `get_evidence_page`, `get_transport_page`,
`ingest_dispatch_observations`, `get_review_status`, `review_lookup`, `begin_verification`,
`consolidate_review_results`, `resolve_review_questions`, `get_review_metrics`,
and `render_consolidated_review_report`.
If they are unavailable, explain that the server
is not registered and point to `SETUP.md`; do not recreate its work in Bash.

For a PR, call `prepare_review(mode="pr", base_ref=...,
expected_base_sha=..., expected_head_sha=..., expected_changed_paths=...)`
once. `base_ref` and `expected_head_sha` are required; pass the target's base
SHA and changed paths whenever PR metadata supplies them so a moved base ref
fails closed. Use
`mode="branch"` only for an explicitly requested local branch review, where
default-base discovery is permitted. The returned review ID identifies pinned
base, merge-base, and head commits; dirty worktree bytes are never review
evidence. If preparation returns `invalid-base` for a missing/moved ref, Git
failure, head mismatch, or inventory mismatch, stop before lane dispatch and
report that status rather than a clean review.

Announce the active lanes and meaningful skips in one short line. Dispatch only
lanes whose `run` is true:

- **Core Review** — logic plus only the AppSec/Contract/Dependency Compatibility checks activated in its bundle.
- **Consistency & Docs** — only meaningful convention, public documentation, schema, example, or runbook drift.
- **Operations** — only the DB/load/worker/impact checks activated in its bundle.

Run at most three restricted Haiku agents in parallel: one per active lane.
Enable only the typed review tools (`get_change`, `find_symbol`, `get_source`,
`find_siblings`, `grep_repo`, `verify_anchor`, and `changed_file`) for them;
do not enable Bash, filesystem
read/search tools, or agent spawning. Do not create separate specialists or a
legacy mode.

Use one aggregate completion wait for those agents; do not poll individual
agents or schedule wakeups. Address every server call with the opaque
`lane_id`; use `display_name` only in user-facing prose. Bundle fetches are
idempotent, so retry a failed transport safely instead of reconstructing or
trimming a response.

Generate each dispatch with `get_lane_dispatch`. Embed its `prompt_payload`
byte-for-byte, without parsing, reformatting, escaping, or shortening it, then
call `acknowledge_lane_dispatch` with those exact embedded bytes and the
returned `prompt_payload_digest`. Do not launch the lane unless acknowledgement
returns `verified`. A merely fetched bundle remains `unverified`.
Launch the acknowledged request through the server's restricted dispatch adapter:
it must enforce the exact `review_lookup` allowlist and observe the launched
model, response count, tool calls, and aggregate waits. Feed those host events
to `ingest_dispatch_observations`. If the host cannot both enforce and observe
these controls, do not represent them as verified; ingestion is self-reported
and the review status is deliberately `degraded`.
The dispatch envelope declares lookup schema version `review-lookup.v1`; reject
an envelope with any other version before launch. The lane model is explicitly the configured restricted model (currently
Haiku); do not inherit the orchestrator model. Record both configured and
observed model; a missing override is policy non-compliance. Where response controls exist, reserve two responses for synthesis and final
JSON: allow 4, 6, or 8 investigative responses based on semantic change size,
then disable evidence tools and request final JSON. Do not count transport
recovery against that allowance.

## Lane-agent contract

Generate dispatch through `get_lane_dispatch` and its exact server payload; never transcribe
hunks or repository facts into the prompt. Give each lane agent the exact
`review_id`, `lane_id`, `lane_display_name`, `base_sha`, `head_sha`, and
`bundle_digest` from that embedded bundle. It must read only the relevant
lens references for the bundle's `activated_checks`, plus
`references/severity.md`.

The bundle is authoritative. Every section declares `complete`, `empty`,
`not_applicable`, `truncated`, or `failed`. For a truncated section, follow its
cursor with `get_evidence_page`; retrieve an exact previewed item with
`get_evidence_item`. If any endpoint returns `RESPONSE_COMPACTED`, follow
`get_transport_page` until its cursor is exhausted. These recovery calls do not
consume the investigative lookup quota. Never silently drop an omitted item or
trim to a first-N subset in the orchestrator. The
agent must not reconstruct the diff, call
repo-wide discovery, use Bash, write files, or spawn agents. It is a bounded
classifier over supplied facts, not an independent investigator. For one
concrete missing fact, it may call `review_lookup` with a question, operation,
and its required arguments only for legacy compatibility. Use the matching
typed tool for new dispatches. Malformed attempts do not consume the server's four
successful lookups per lane. On exhaustion, return
the unresolved question in `unanswered`; do not guess or pad the review.

Treat coherence entries according to their confidence. `high` entries are
AST-resolved direct, imported, qualified, or inherited calls with an owning
module and exact supporting source span. Bare-name and quoted-string matches
are `low`-confidence leads only: they are not callers, orphaned references, or
findings without independent confirmation. Each changed file declares
`semantic`, `hunk-only`, or `unsupported` analysis capability; report limited
coverage explicitly rather than treating an empty semantic result as clean.

Each lane returns exactly:

```json
{
  "review_id": "...",
  "lane_id": "lane_...",
  "lane_display_name": "Core Review",
  "base_sha": "...",
  "head_sha": "...",
  "bundle_digest": "sha256:...",
  "findings": [{
    "changed_cause_anchor": {"file": "path", "line": 1, "side": "new", "anchor_snippet": "one exact changed source line"},
    "affected_site_anchors": [{"file": "unchanged/or/changed/path", "line": 1, "anchor_snippet": "one exact pinned-head source line"}],
    "summary": "actionable title",
    "root_cause": "stable cause shared by affected sites",
    "reachability": {"kind": "supported-api-request", "actual_caller": "known caller, when any", "supported_input": "supported input or request, when any"},
    "precondition": "concrete state required to trigger the behavior",
    "observable_impact": "externally or operationally observable result",
    "failure_scenario": "current caller/input/environment and impact",
    "severity": "blocking|warning|refining",
    "severity_reason": {"code": "documented reason code", "rationale": "why it applies"},
    "confidence": "high|medium|low",
    "critical_assumptions": [],
    "evidence_refs": ["bundle or lookup reference"],
    "unknowns": [],
    "causal_link": {"mechanism": "how the changed cause reaches affected sites", "evidence_refs": ["trace reference"]},
    "falsifying_checks": [],
    "cheapest_falsifying_check": "smallest check that could refute the claim",
    "lens": "Core Review"
  }],
  "inputs_used": [],
  "unanswered": []
}
```

`inputs_used` must name the bundle sections consulted and explicitly record a
relevant empty or inapplicable section. `unanswered` entries must include the
lane, concrete question, why it remains unresolved, and a bounded suggested
resolution. The server assigns `question_id` and normalizes legacy strings with
a warning; it never silently drops malformed entries. A supported API request
is a current reachable path even when no first-party caller is known. Keep a
hypothetical future model or configuration separate and do not promote it.
Affected sites may be unchanged, but each needs pinned-head source evidence and
a cited causal mechanism. Put true low-impact editorial items in the optional
note category, not the main findings.

## Deterministic consolidation

Before dispatch, call `review_capabilities` and compare its schema hash and
tool list with the launch contract. A missing prompted tool is a launch error,
not a reason to fall back to `ToolSearch`, Read, or Bash. For a clean lane,
submit a `coverage` receipt that classifies every server-provided coverage ID
as `reviewed`, `irrelevant` (with reason), or `retrieved`; unresolved or
omitted changed causes keep the review incomplete. Lane identity is bound from
the acknowledged dispatch, so responses must not rewrite snapshot SHAs.

Call `consolidate_review_results(review_id, lane_results)` once with every
lane response. It mechanically validates structured claims, checks changed
cause anchors against the pinned diff and affected-site anchors against pinned
head source, deduplicates root causes, and preserves a disposition with
evidence for every candidate: `schema-invalid`, `source-anchor-invalid`,
`diff-anchor-invalid`, `affected-source-invalid`, `unsupported-causal-link`,
`future-only`, `low-impact`, `deduplicated`, `refuted`, `unresolved`, or
`retained`. Results whose identity does not match the authoritative bundle are rejected before their findings enter consolidation.
Do not reproduce this sequencing in orchestration prose.

## Batched independent verification

Build one candidate list from the deduplicated findings:

- Verify every blocking finding.
- Verify a warning unless it has recorded tool or independent-source provenance.
- Do not agent-verify refinements.

Call `begin_verification(review_id, candidates)` once and dispatch one
restricted Haiku verifier for the whole batch. It has only candidate-scoped
typed lookup tools (`get_change`, `get_source`, `verify_anchor`, and the
other registered lookup operations) and receives the complete minimal claim: changed cause,
affected path, reachability, precondition, observable impact, affected sites,
evidence, unknowns, and the cheapest falsifying check. It may use
typed lookup tools only with the returned verification ID and candidate ID; the
server enforces six lookups per candidate. It must test the minimum facts that
could refute the claim, not re-review the repository.
Each successful typed call returns a receipt ID. Submit an agent verdict only
through `register_verifier_result` with those receipts, or use
`verify_candidate_source_facts` for a server-issued pinned-source result.
`consolidate_review_results` rejects free-form verifier overrides.
Obtain the launch payload through `get_verifier_dispatch(review_id,
verification_id)`: it generates stable cause/affected-anchor IDs and valid
severity reason codes. Do not hand-write a verifier prompt or include the
legacy `review_lookup` wrapper. A host must report exact per-tool telemetry;
approximate lists are `telemetry-unverified`, while unavailable host
enforcement is a `dispatch-contract-mismatch` degraded status rather than a
claimed tool-policy violation.
If no candidate survives deterministic validation and root-cause
deduplication, `begin_verification` returns `dispatch_required=false`; do not
launch a blanket verifier.

Verifier response:

```json
{
  "results": [{
    "candidate_id": "...",
    "verdict": "CONFIRMED|PLAUSIBLE|REFUTED",
    "evidence": [],
    "inputs_used": []
  }]
}
```

Drop refuted or unverified blocking/warning findings. Retain refinements
without a verdict field.

## Report

Call `consolidate_review_results` again with verification results. Resolve
questions separately with `resolve_review_questions`; never mix resolutions
into consolidation. The resolution tool validates the entire batch before it
commits anything. A resolution must reference exactly one existing question
ID, evidence, resolving phase, optional candidate, and `resolved-no-finding`
or `resolved-new-candidate`. Unknown, missing, and duplicate IDs leave review
state and metrics byte-for-byte unchanged. Call
`render_consolidated_review_report(review_id)` with no caller-supplied counts
or lane names: it derives findings, unanswered questions, coverage, and review
status from stored review state. It emits Tier 1/2 findings in full and
collapses Tier 3. Do not retry a host-specific report schema; use this Markdown
result when a reporting tool rejects an optional field.

After the one aggregate wait, ingest the host observations. Do not use
`record_review_dispatch`: caller-supplied counters are non-authoritative and it
returns `HOST_EVENT_INGESTION_REQUIRED`. After rendering, call
`get_review_status(review_id)` and `get_review_metrics(review_id)`. The status
is independent of finding count: it is `invalid-base`, `degraded`,
`incomplete`, or `complete`, and exposes the pinned base/head, diff mode,
changed-file count, lane coverage, evidence delivery, lookup failures,
candidate and verifier coverage, open questions, and field provenance. Metrics
retain host-observed response/wait/tool/model data (or `unavailable`), lookup
attempts/rejections/quota use, payload bytes, truncation, verification, and
unresolved questions across passes.

Before changing response or evidence limits, read
[`references/replay-cases.md`](references/replay-cases.md) and replay the
captured cases it identifies.

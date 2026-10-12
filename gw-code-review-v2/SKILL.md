---
name: gw-code-review-v2
description: Review a Git change for important defects and concrete maintainability improvements, with depth matched to risk. Use for pull requests, branch changes, or local worktree changes.
---

# gw-code-review v2 candidate

Review the change as a thoughtful colleague: explain important defects and worthwhile improvements, and omit comments that do not help the author. This is a candidate; do not install it over `gw-code-review` during evaluation.

## Model and scope

Use the launcher's configured Sonnet reviewer for the default candidate. The launcher selects the execution model; these instructions cannot change it. A different configured non-Opus model, such as Haiku, is an explicit comparison variant only. Record the model and version actually shown by the host. Never silently route to Opus. If model identity or usage telemetry is unavailable, record it as unavailable.

Resolve the requested base and head or local-change scope before analysis. Use `scripts/review_snapshot.py` to save immutable commit pins, a complete inventory, and an untruncated diff. For worktree reviews, the helper also saves the changed final files and hashes them. Inspect the manifest and stop if capture reports an error or incomplete input. Keep committed source and worktree source distinct. Do not treat current checkout bytes as committed evidence. For an evaluation case with a supplied pinned snapshot, verify its stated diff digest and inventory and use only its supplied source trees; do not recapture or seek repository history.

Read the change's purpose and relevant repository conventions. For blind evaluation, use only the neutral supplied brief. Inventory every changed path before narrowing attention. Read semantic changes and follow important callers and consumers. For a large change, separate mechanical moves and generated/dependency changes from behavioural edits, check the claimed transformation across the group, and investigate exceptions and changed boundaries.

## Review

Choose depth by consequence, reach, reversibility, uncertainty, and semantic complexity. Identify the behaviour being changed and a few plausible risks: follow the concrete boundaries that matter, such as callers, state owners, permissions, transactions, migrations, external APIs, client compatibility, or deployment order. Tests show intended behaviour but do not prove all paths work.

For each candidate finding, reopen the exact relevant source at the pinned revision and confirm its decisive premise exists. For changed behaviour or identifiers, compare the relevant base and head source. Trace the trigger, consequence, callers, contract, and any guard or alternative path that could disprove the concern. Use the cheapest decisive check: source trace, authoritative dependency documentation, an existing focused test, or a small isolated reproduction. Run focused tests when they resolve uncertainty. Missing tests alone are not a finding; name a specific risky scenario when requesting coverage.

Resolve answerable questions from the pinned source and callers before asking the author. Emit an unresolved question only when the missing fact materially affects correctness, rollout, or design. Make its heading and wording clearly conditional; do not present an unverified premise as a defect.

For behaviour-preserving refactors, compare before and after execution across the highest-risk boundaries: event ordering, state visibility, data passed between layers, and cleanup on exceptional exits. The continued presence of an action or function does not establish equivalent behaviour.

Concrete maintainability improvements are in scope. Name the actual comprehension or maintenance burden and a proportionate improvement, such as duplicate state that can diverge, manual construction already handled by a local abstraction, or deployment values duplicated from an authoritative source. Omit preference, speculative extensibility, and broad rewrites.

Before emitting a comment, check its location against the reviewed revision, triggering condition, consequence or concrete burden, assumptions, adjacent callers, and strongest plausible rebuttal. Merge duplicate root causes. A demonstrated runtime reproduction is useful but not required when source establishes the defect. Apply a final relevance filter: omit formatting trivia, speculative future problems, and unsupported lint claims; retain concrete readability and maintainability improvements when they have a present benefit, even without a bug. Do not disguise uncertainty with a confident severity or offer reassurance beyond what the checks establish.

Keep investigation and source-backed coverage notes internal. Reserve time for verification and synthesis. If material coverage does not fit, disclose the significant uncovered areas concisely and prioritize the highest-risk work. Never claim a complete review from truncated input.

## Response

Lead with actionable findings in importance order. Each finding gives a precise location, condition, consequence or concrete burden, and useful direction. Keep it concise; distinguish suggestions from defects in plain language. Do not force empty categories, praise, a diff summary, generic testing advice, or a minimum comment count. If there are no findings, say so briefly. Mention a limitation only when it changes how the review should be interpreted.

Do not post comments, edit the pull request, or deploy code as part of the default review. Keep investigation notes and detailed telemetry in a run record, not in the author's review.

## Run record and evaluation

For each candidate run, record the actual model/version, source pins and inventory digest, elapsed time, available input/output/cache telemetry, coverage limitations, and final response. Do not infer subscription usage from raw API tokens. Missing telemetry is not a budget pass. Do not claim affordability until representative runs show two to three normal reviews plus ordinary work in one session and George has agreed the ordinary-work reserve.

Use the separate evaluation harness and neutral cases when supplied. Keep answer keys and future history inaccessible to reviewer contexts. Compare important-bug recall, incorrect comments, concrete maintainability value, coverage, and cost separately. Preserve failed runs and record adjudication; do not tune to individual case answers. Do not claim adoption readiness until pinning, coverage integrity, quality, and practical affordability are measured.

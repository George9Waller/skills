# Captured replay cases

Use these fixed cases before changing response or lookup enforcement. Keep the
capture directory with the test run configuration; do not treat an old reported
finding as a recall oracle when the reviewed revision changed.

The current delivery revision and deterministic-fixture scope are recorded in
[delivery evidence](delivery-evidence-2026-09-25.md). Historical captures are
not equivalent to replaying their original Git commits.

| Case | Capture | Required outcome |
| --- | --- | --- |
| Updated EV-704 | `2026-09-11-gw-code-review-EV-704-e7082728` | Clean, or at most a low-priority refinement. |
| EV-721 | `2026-09-11_EV-721-personalize-resource-names` | Refute the JSON-validation warning; do not promote the intentional shadow subset or generic direct-test concern. |
| Original EV-704 | `2026-09-09-gw-code-review-EV-704` | Efficiency baseline only; do not require its historical findings. |
| PR 14720 coherence | deterministic mini-repo fixture in `tests/test_query.py` | Same-named test definitions and quoted labels remain low-confidence leads, never caller edges. |
| PR 14660 coherence | deterministic mini-repo fixture in `tests/test_query.py` | Raw reference counts are not dangling calls without high-confidence resolution. |

For each replay, record actionable-finding precision, unsupported-warning rate,
unanswered-question retention, model responses, successful evidence operations,
bundle payload bytes, lookup success, verifier cost, and cost per confirmed
actionable finding. Record each candidate disposition and analysis capability.
A budget increase or decrease requires evidence that an actionable finding was
lost specifically to budget exhaustion.

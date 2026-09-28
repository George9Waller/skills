# Delivery evidence — 2026-09-25

This record freezes the implementation evidence for the integrity through
quality-release phases.

| Item | Value |
| --- | --- |
| Base skill revision | `d20d580af2aa78cc5e464093ec07aa04ea1d80f5` |
| Working-tree diff record | `git diff --binary` at delivery validation; the artifact itself is part of that diff, so a self-hash would be unstable |
| Contract | `review-snapshot.v1`; `review-lookup.v1`; receipt-bound verifier results |
| Deterministic suite | `tests/test_query.py` |

The suite covers pinned parent/child snapshots, dirty worktrees, moved bases,
Git failure, bounded evidence transport, typed lookup operations, claim
dispositions, restricted dispatch behavior, low-confidence coherence leads,
semantic/hunk-only/unsupported analysis coverage, and the EV-752 verifier
boundary: deterministic source verification, candidate-scoped typed-call
receipts, rejection of a forged verifier verdict, and a verifier-dispatch
fixture that exercises the same `DispatchAdapter` launch contract.

Validation at this delivery: `UV_CACHE_DIR=/private/tmp/gw-code-review-uv-cache
uv run python -m unittest tests/test_query.py` (49 tests) and `uv run ruff
check .`; both passed. The production status remains deliberately `degraded`
until a capable `RestrictedHost` supplies verified dispatch observations.

Historical repositories for EV-704, EV-721, PR 14660, PR 14720, and EN-187
are not available as frozen Git checkouts in this workspace. Their captured
logs are preserved under `/Users/georgewaller/code-review-runs`; the known
faults are replayed here with deterministic mini-repositories instead. Do not
claim a historical replay result until its exact commit is recovered and run
against this revision.

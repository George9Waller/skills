# Severity and actionability

Rate the current change from structured reachability and impact evidence, not
keywords in prose or an imagined future caller or configuration.

- **`blocking`** — a currently reachable production failure, broken existing
  contract, or meaningful trust/authority-boundary violation. Name the current
  caller, input, task, or deployed dependency that reaches the failure.
- **`warning`** — a real current risk with bounded impact, or a regression
  whose runtime/environment precondition cannot be established from the repo.
  State the missing fact explicitly.
- **`refining`** — a safe improvement with a demonstrated maintenance or
  correctness benefit. It is not a naming, formatting, or local-preference nit.

## Reachability

Any request or input supported by the public or internal API contract is a
current reachable path, even when the repository contains no known first-party
caller. Distinguish it from an unsupported constructible input and from a
hypothetical future model/configuration. Separately record whether the caller
is anonymous, external, authenticated internal, scheduled, or a worker. An
internal API key alone does not make malformed input blocking unless the change
crosses an authority boundary or has demonstrated production impact.

A branch that no current caller reaches, an exception no current model can
raise, or a defect requiring a future stricter configuration is a refinement
at most. Report it only if its future cost is concrete and useful.

Environment-dependent changes may be warnings: explain the verified code
mechanism and say which secret, deployment setting, traffic shape, or data fact
is unavailable. Do not present that uncertainty as confirmation.

## Reason codes

- `blocking`: `current-production-failure`, `broken-existing-contract`, or
  `authority-boundary`.
- `warning`: `bounded-current-risk` or
  `environment-precondition-unverified`.
- `refining`: `maintenance-cost`, `future-only`, or `low-impact`.

Record a rationale with the code. True editorial issues use `low-impact` and
appear only in optional notes by default.

## Examples

- An existing client still sends a removed required API parameter: `blocking`.
- A new query is N+1 only on an admin view: `warning`.
- A new validation fallback matters only if a later filter type becomes strict:
  normally `refining`.
- "Use T instead of C for a generic parameter": not a finding.

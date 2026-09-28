# lens-docs-drift: Documentation Agreement

## Portable reasoning boundaries

**Transferable principles:** compare a changed claim with the authoritative changed interface or configuration. **Stack-specific triggers:** Python docstrings, TypeScript API examples, Go package docs, Terraform variable/module documentation, and Databricks bundle README/target examples. Keep each comparison in its own stack context.

Detect documentation that no longer agrees with the changed code. This lens is
always routed, but receives only documentation-adjacent files and the minimum
code/config/API context needed to compare them.

## How to use this lens

Check documentation against code, docstrings against signatures, README and
runbooks against runtime configuration, API documentation against routes, and
examples against current schemas. Anchor findings to a changed documentation or
docstring line; a code-only mismatch may be cited as supporting evidence but
is not a docs-drift finding unless documentation changed or is the concrete
artifact being reviewed.

## What to check

- Parameter, return, exception, or type claims in a changed docstring that
  diverge from the changed callable signature.
- README/setup/runbook commands, environment names, defaults, or required
  services that differ from runtime configuration.
- Changed API route/method/status/request/response documentation that differs
  from the route or contract fact.
- Changed JSON/YAML/code examples that violate an adjacent serializer, schema,
  or documented command interface.
- A code/config/API change paired with an updated nearby doc that retains an
  old name, default, endpoint, or configuration key.

## When no catalogue example matches

Use transferable reasoning: state the documentation claim, the authoritative
bounded code/config/schema fact, and the exact divergence. Do not turn a
stylistic preference or missing optional prose into a finding. If those facts
cannot be gathered with the assigned bundle and bounded lookup, return `[]`
and put the question in `unanswered`.

## Empty-input protocol

If `documentation_files` is empty and no changed row has `touches_docs: true`,
state that the lens is inapplicable in `inputs_used` and return an empty
findings list. For docs-only diffs, review those docs rather than skipping the
lens because no executable symbol changed.

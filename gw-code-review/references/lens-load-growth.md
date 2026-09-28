# lens-load-growth: Stack-neutral Load and Growth

## Portable reasoning boundaries

**Transferable principles:** quantify the changed work per request/item and show why its input, retry, or shared-state behaviour lacks a bound. **Stack-specific triggers:** Python services, TypeScript handlers, Go loops, Terraform provider/data-source operations, and Databricks task/pipeline settings are illustrative routes to that evidence, not universal checks.

Review how changed code behaves as input size, request volume, or dependency
latency grows. This lens always applies to executable/config changes; it does
not require Celery. Compare the changed pattern with neighbouring code before
raising a finding, so a load-neutral diff returns `[]`.

## How to use this lens

Check only changed request handlers, services, clients, collection-building,
retry/configuration, and shared-state code. Look for unbounded collections,
per-item external calls, request-time configuration loading, new fan-out,
uncapped retries, and non-atomic shared state. Obtain bounded evidence with
`get_source` or `find_siblings` when the supplied hunk does not establish the
neighbouring pattern.

External-service latency and boto-style client behaviour belong here, not to
Logic or Worker Fan-out. Flag a new request-time AWS/HTTP call only when the
diff and a nearby established client pattern show a concrete latency, timeout,
pooling, pagination, or retry regression.

## What to check

- A list/dict/set that accumulates an unbounded query, upload, or response;
  require evidence that input is unbounded and that nearby code streams,
  pages, or caps it.
- An external API, boto client, or storage call inside a per-item loop; compare
  with neighbouring batch/pagination/concurrency limits before finding.
- New fan-out from one request/event into many calls or jobs without an
  explicit cap, batching, or backpressure.
- Retry loops without a finite attempt limit, bounded delay, or a terminating
  error path.
- Configuration or client construction performed for every request when a
  neighbouring module initializes/reuses it safely; do not report a deliberate
  request-scoped dependency injection pattern without evidence of cost.
- Read-modify-write of cache, files, or shared records without an atomic
  primitive or lock where concurrent calls can lose an update.

## When no catalogue example matches

Use transferable reasoning: identify a growth dimension, the changed work per
unit, and a bounded source fact showing why it is unbounded or non-atomic.
Do not invent an adjacent performance concern because the code "looks busy".
If that evidence cannot be obtained with the supplied bundle and bounded
lookup, return `[]` and record the gap in `unanswered`.

## Empty-input protocol

If the assigned diff has no executable/config code in these categories, state
that in `inputs_used` and return an empty findings list. An always-routed lens
is not required to manufacture an observation.

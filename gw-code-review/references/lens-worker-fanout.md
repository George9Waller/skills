# lens-worker-fanout: Queues, Workers, and Background Fan-out

## Portable reasoning boundaries

**Transferable principles:** demonstrate a producer, multiplier, consumer capacity, and absent cap/backpressure before finding fan-out risk. **Stack-specific triggers:** Celery tasks, TypeScript queue workers, Go goroutines/channels, Terraform queue settings, and Databricks job/task concurrency. Only inspect mechanisms represented in the diff.

Review queue-specific scalability only when the diff changes worker, task,
queue, concurrency, or batch behaviour. This lens complements, rather than
duplicates, stack-neutral Load & Growth.

## How to use this lens

Compare task dispatch and worker configuration with neighbouring tasks before
reporting. Focus on per-task blocking work, unbounded task creation, missing
backpressure, unsafe task retries, and state races across workers. Do not own
ordinary external-client latency; that belongs to Load & Growth.

## What to check

- New fan-out that emits a task per unbounded item without chunking, rate
  control, or queue backpressure.
- A worker task that blocks on synchronous per-item work rather than an
  existing batch/chunk pattern, where the input is demonstrably unbounded.
- Retry configuration that can storm a queue or lacks a finite termination
  condition.
- Cross-worker read-modify-write state without an atomic update, lock, or
  idempotency protection.
- A changed queue/concurrency/prefetch setting that conflicts with nearby
  worker configuration and creates starvation or backlog.

## When no catalogue example matches

Use transferable reasoning with bounded evidence: name the producer, worker,
and load multiplier or shared-state interleaving. Do not infer a queue problem
from the existence of a task alone. If the bundle and bounded lookups cannot
show that path, return `[]` and record the unresolved question.

## Empty-input protocol

If no worker/queue-adjacent hunk is assigned, say so in `inputs_used` and
return an empty findings list.

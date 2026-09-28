# lens-performance-db: Database Query Shape and Migrations

## Portable reasoning boundaries

**Transferable principles:** establish cardinality, work per item, and an absent bound/index/plan before claiming a production cost. **Stack-specific triggers:** Django ORM and migrations; TypeScript query builders; Go `database/sql` loops; Terraform state/provider data sources; Databricks jobs, clusters, and pipeline concurrency. A Django migration rule does not apply to another stack without analogous evidence.

N+1 queries, missing indexes, and unsafe migrations—correctness bugs that manifest under realistic data volume. A query that works on a test database with 100 rows may timeout in production with 10 million.

## How to use this lens

Your operations bundle is pre-routed only when changed hunks contain a query or
migration construct. The bug is usually in the shape of that query, not its
correctness; do not infer database work from a filename such as `models.py`.

If the supplied change facts are empty and the diff has no queryset, ORM, or migration code, say so explicitly and return an empty findings list — do not substitute unrelated observations to justify the dispatch.

## What to check

**ORM queries inside loops without select_related() or prefetch_related().** Each iteration runs a separate database round-trip. Example:

```python
# views.py:34
orders = Order.objects.filter(status='pending')
for order in orders:
    customer = order.customer  # FINDING: SELECT per iteration
    # or
    for invoice in order.invoices.all():  # FINDING: SELECT per order
        print(invoice.total)
```

This is an N+1 query. Use bounded source lookup for the object fields accessed inside the loop. If they are foreign key relationships (`.customer`, `.invoices`), the loop likely needs `select_related('customer')` or `prefetch_related('invoices')` on the initial queryset.

**Unbounded .all() loaded fully into memory.** If a view or task loads a large result set without pagination or `.iterator()`, it consumes memory proportional to the result size. Example:

```python
# reports.py:67
all_transactions = Transaction.objects.all()  # FINDING: could be 10M rows
for txn in all_transactions:
    if txn.amount > threshold:
        process(txn)
```

Better: `.iterator()` for streaming, or `.values_list()` to load only needed columns, or explicit pagination.

**Migration adding a non-nullable column without a default value.** Django must rewrite the entire table, acquiring a table lock on databases like PostgreSQL, blocking all reads and writes. Example:

```python
# migrations/0042_add_column.py:10
operations = [
    migrations.AddField(
        model_name='account',
        name='account_type',
        field=models.CharField(max_length=32),  # FINDING: null=False, no default
    ),
]
```

This locks the table until the migration completes. On a large table, that is a production outage. Require a default value or `null=True` initially, then backfill and make non-nullable in a second migration.

**Migration adding a foreign key without db_index=True.** The PR then queries by that FK, causing a full table scan. Example:

```python
# migrations/0041_add_fk.py:10
operations = [
    migrations.AddField(
        model_name='order_line_item',
        name='warehouse',
        field=models.ForeignKey(..., db_index=False),  # FINDING: no index
    ),
]

# Then in views.py later in the same PR:
items = OrderLineItem.objects.filter(warehouse=w)  # FINDING: full table scan
```

**Using .count() on a queryset when len(queryset) is used.** If the code iterates the queryset afterwards, `.count()` runs a separate query; better to evaluate the queryset once and call Python's `len()`. Example:

```python
# analytics.py:56
if orders.count() > 0:  # FINDING: separate SELECT COUNT
    for order in orders:
        process(order)  # Re-evaluates queryset
```

Conversely, if only the count is needed, `.count()` is cheaper than materialising the full queryset.

**Raw SQL without parameterized queries.** Also covered by lens-appsec, but mentioned here for completeness: `.raw()`, `extra()`, or f-string SQL on user input is both a security and performance bug (no query plan caching). Example:

```python
# models.py:128
results = User.objects.raw(
    f"SELECT * FROM users WHERE created_at > '{date_str}'"
)
```

**Indexes missing for new filtering or sorting columns.** If a PR adds a `queryset.filter(status=...)` or `.order_by('created_at')` and the column has no index, that query is now slow. Example:

```python
# views.py:45
# New in this PR:
reports = Report.objects.filter(department_id=dept).order_by('created_at')
```

Check the model definition: `department_id` and `created_at` should have `db_index=True` or be part of a multi-column index.

## When no catalogue example matches

Use transferable query-shape reasoning: identify the changed query or
migration, its data-volume multiplier, and bounded evidence of the extra
round-trip, scan, lock, or materialisation. Do not invent a database finding
from an ORM method name alone. If those facts are unavailable, return `[]` and
record the gap in `unanswered`.

## What this lens does NOT check

- Correctness of the filter logic itself (is the WHERE clause correct? That's lens-logic-tests)
- Schema design and denormalisation trade-offs (architectural, not a code-review finding)
- Query cache eviction or Redis cache key collisions (covered by lens-load-growth)
- Transaction isolation and deadlock risks (rare in most codebases; if present, requires domain knowledge)

# lens-contract: Service Boundaries and Intent

## Portable reasoning boundaries

**Transferable principles:** a changed input, output, name, or required setting is breaking only when an existing consumer can still use the old form. **Stack-specific triggers:** DRF/FastAPI/Celery signatures; TypeScript exported APIs; Go exported functions or JSON structs; Terraform module variables/outputs; Databricks bundle target and variable contracts. Inspect only the triggers present in the assigned diff.

Whether an intentional change to one part of the system correctly propagates (or fails to propagate) across service boundaries—or, conversely, whether a change meant to be local escapes to unintended callers. This is distinct from a single field rename (a linter catches that); it is about the *reasoning* being correct across API contracts, task signatures, and shared code.

## How to use this lens

Your compact lane bundle contains contract facts and relevant coherence entries.
For Celery, also consider deployment independence: workers and callers deploy
at different times and must handle version mismatch.

If `get_contract_changes` and the supplied coherence/trace facts both came back empty or irrelevant to this diff, say so explicitly and return an empty findings list — do not substitute unrelated observations to justify the dispatch.

## What to check

**Breaking changes to DRF serializer field names or types.** If a read-only field was renamed or its type changed (e.g. `IntegerField` to `CharField`), verify that consumers (the frontend TypeScript client, or another backend service) still match. Example:

```python
# serializers.py:45
class OrderSerializer(serializers.ModelSerializer):
    # Was: total_price_cents = serializers.IntegerField()
    total_price_cents = serializers.DecimalField(...)  # FINDING: type changed
```

Check the supplied coherence/trace facts for cross-file references to this serializer. If a frontend or backend client passes a different type, flag it.

**New required serializer fields or route parameters with no default.** If a field became required and no default was provided, old clients cannot send POST/PUT requests. Example:

A documented or otherwise supported request is current reachability even if no
first-party caller is found. Do not downgrade it to hypothetical solely because
the caller inventory is empty. Conversely, a path requiring a future
serializer, model, or configuration remains future-only.

```python
# serializers.py:62
class UserProfileSerializer(serializers.ModelSerializer):
    phone_number = serializers.CharField(required=True)  # FINDING: was required=False
```

**FastAPI route signature changes.** If a required query parameter or path parameter was added, old clients will 400. If a response status code changed (e.g. a 201 Created became 200 OK), downstream routing/retry logic may break. Example:

```python
# routes/invoices.py:18
async def create_invoice(
    user_id: int,
    amount: float,  # FINDING: new required parameter
):
    return {"status": "ok"}
```

**Celery task signature changes—especially add/remove of arguments.** Tasks are registered globally; old workers running against a new caller (or vice versa) cause runtime errors. If a `.delay()` or `.apply_async()` call site passes a new required argument that old worker code does not accept, or if the task definition removed an argument that callers still pass, flag it as a deployment ordering hazard. Example:

```python
# tasks.py:88
@celery_app.task
def process_payment(order_id, gateway_id):  # FINDING: added gateway_id
    pass

# somewhere in views.py or elsewhere, the old caller still does:
# process_payment.delay(order_id=123)  -- will fail
```

Use the supplied coherence/trace facts to find all `.delay()` and `.apply_async()` call sites for this task.

**Shared code changes with ambiguous scope.** A serializer used by multiple API versions, or a utility used by multiple call sites, changed in a way that affects only one intended caller but actually reaches all. Example:

```python
# shared/serializers.py:30
class AddressSerializer(serializers.Serializer):
    country_code = serializers.CharField(required=True)  # FINDING: added required
```

If the supplied coherence/trace facts show this serializer is used in both `v1_api` and `v2_api`, but only v2 intends the required field, flag the risk: v1 is now broken. Better to create a v2-specific subclass.

**Removal of optional fields that callers might reference.** Even if a field was optional and had no default, callers may check for its presence. If removed, conditionals like `if data.get('legacy_field')` now always evaluate False. Example:

```python
# serializers.py:15
class PaymentSerializer(serializers.Serializer):
    # Removed: refund_notes = serializers.CharField(required=False, allow_blank=True)
    pass
```

Check git history or tests for callers that read this field.

**HTTP status code changes without documentation.** If a view returned 200 and now returns 202 (Accepted) for an async operation, callers checking for `if response.status_code == 200:` will miss the new case. Example:

```python
# views.py:52
def handle_bulk_export(request):
    task.delay(...)
    return Response(status=202)  # FINDING: was 200
```

**Base/override return-shape divergence.** When a changed base method or
override returns a different shape from another implementation, inspect each
internal call through the base type as well as direct callers. A subclass that
returns `None` or a mapping where sibling overrides return a domain object
breaks callers that correctly rely on the base contract.

**Internal cross-class Liskov breaks.** Check changed inheritance and protocol
implementations for narrower accepted inputs, newly raised exceptions, or
weaker postconditions than the base class advertises. This applies inside one
repository too: internal users of a base class are real callers, not an
architectural hypothetical. Anchor the finding to the changed override and
cite the base declaration plus a concrete polymorphic call path.

## When no catalogue example matches

Use transferable contract reasoning: identify the producer's promised shape,
the consumer or base-type caller, and bounded evidence that the changed
implementation diverges. Do not invent a boundary merely because a method is
public. If the supplied facts and bounded lookup cannot establish both sides,
return `[]` and list the unresolved question in `unanswered`.

## What this lens does NOT check

- Internal refactoring of a function that no external code calls (that's code quality)
- Rearrangement of private attributes or internal helper functions
- Performance changes within a function body (covered by lens-performance-db)
- Correctness of the logic itself (covered by lens-logic-tests)
- Whether the deployment order is *safe* given the signature change (that is a release/ops decision, documented separately)

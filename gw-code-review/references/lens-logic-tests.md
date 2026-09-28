# lens-logic-tests: Correctness and Test Adequacy

## Portable reasoning boundaries

**Transferable principles:** identify a changed branch, boundary, failure mode, and the test or caller that establishes its expected behaviour. **Stack-specific triggers:** Python exceptions, TypeScript promises, Go error returns, Terraform validation/preconditions, and Databricks task parameters. Do not carry an exception-handling expectation into Go without considering its explicit error-return model.

Unhandled exceptions, missing edge cases, and business logic introduced without corresponding test coverage. This lens is about whether the code will behave correctly under realistic conditions, not whether it follows style conventions.

## How to use this lens

Your compact lane bundle contains changed symbols, hunks, coherence candidates,
and relevant tests. Identify changed control flow and current boundary failures;
then check whether tests exercise those paths.

If the supplied change facts are empty and the diff itself has no new functions or modified control flow, say so explicitly and return an empty findings list — do not substitute unrelated observations to justify the dispatch.

## What to check

**Unhandled exceptions from third-party API calls.** Stripe, external HTTP endpoints, and message queues can fail: connection errors, timeouts, rate limits, and server errors. If a view or task calls an external API without try/except, the error propagates to the caller with no recovery. Example:

```python
# views.py:78
def process_payment(request):
    response = stripe.Charge.create(
        amount=amount,
        currency='gbp',
        card=token,
    )  # FINDING: no try/except for stripe.error.CardError, timeout, etc.
    return JsonResponse(response)
```

Better: catch `stripe.error.CardError`, `stripe.error.RateLimitError`, and generic request timeouts; log; return a user-friendly error or enqueue a retry task.

**Missing edge-case handling: None, empty list, zero.** Boolean logic often fails at boundaries. Example:

```python
# calculations.py:45
def calculate_discount(items):
    total = sum(item.price for item in items)
    discount_rate = 0.1 if total > 100 else 0.05
    return total * discount_rate
    # FINDING: items is [] → total is 0 → return 0, which is fine
    # But what if items is None? AttributeError. Is None valid input?
```

Check the function signature and docstring. If None is a possibility, add `if items is None:`.

**Async code in React with unhandled promise rejections.** A fetch or `useEffect` that does not handle `.catch()` or provide error boundaries. Example:

```typescript
// components/PaymentForm.tsx:34
useEffect(() => {
    fetch('/api/payment-methods')
        .then(r => r.json())
        .then(data => setMethods(data))
        // FINDING: no .catch(); network error silently fails
    }, [])
```

Or in a click handler:

```typescript
// components/PaymentForm.tsx:67
async function submitPayment() {
    const result = await api.post('/charge', {...})
    // FINDING: if post rejects, component unmounts with unhandled rejection
}
```

**Conditional logic with no explicit else or fallthrough.** Code branches but does not handle all cases. Example:

```python
# email_service.py:23
def send_notification(user, event_type):
    if event_type == 'welcome':
        template = 'welcome_email'
    elif event_type == 'payment_received':
        template = 'receipt_email'
    # FINDING: event_type='reminder' reaches the end with template undefined
    send_email(user, template)
```

**Complex new business logic with no corresponding test changes.** If a PR adds a multi-branch function (discount calculation, eligibility check, state machine transition) and the test files have no new test cases for that function, flag it. Example:

```python
# eligibility.py:10 (new in PR)
def can_upgrade_plan(user, new_plan):
    if user.subscription_status != 'active':
        return False
    if new_plan.tier <= user.plan.tier:
        return False
    if user.days_since_signup() < 30:
        return False
    # ... more branches
    return True
```

Check the diff for a corresponding test file change. If there is no `test_eligibility.py` change, or if the test file exists but is not updated, flag: "New eligibility logic introduced with no test coverage."

**Off-by-one errors in range/iteration.** Example:

```python
# batch_processor.py:34
for i in range(len(items)):
    process(items[i])
    if i == len(items):  # FINDING: never true; should be i == len(items) - 1
        finalize()
```

**Missing default return statement.** A function might reach the end without explicitly returning. In Python, this implicitly returns None. Example:

```python
def get_user_role(user):
    if user.is_admin:
        return 'admin'
    if user.is_staff:
        return 'staff'
    # FINDING: normal users fall through, return None
```

**Datetime comparisons without timezone awareness.** If a timezone-naive and timezone-aware datetime are compared, raises TypeError. Example:

```python
# validators.py:56
from datetime import datetime
expiry = datetime(2025, 12, 31)  # FINDING: naive
if datetime.now() > expiry:  # If now() is timezone-aware, TypeError
    return False
```

## When no catalogue example matches

Use transferable correctness reasoning: name the changed input/state, the
specific execution path, and a bounded source fact that reaches the faulty
operation. Do not invent a nearby edge case merely because control flow was
edited. If the path cannot be established, return `[]` and put the question in
`unanswered`.

## What this lens does NOT check

- Code style or naming conventions (linter job)
- Performance characteristics (covered by lens-performance-db and lens-load-growth)
- Security implications of logic (covered by lens-appsec)
- Whether a test *passes* (CI/test runner job)
- API contract correctness (covered by lens-contract)

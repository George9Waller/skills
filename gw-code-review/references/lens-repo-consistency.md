# lens-repo-consistency: Reinvented Wheels and Pattern Drift

## Portable reasoning boundaries

**Transferable principles:** report duplication only with an anchored existing equivalent and a material divergence. **Stack-specific triggers:** Python utilities, TypeScript shared packages, Go internal packages, Terraform modules, and Databricks bundle includes. Naming conventions from one ecosystem are not repository-wide laws.

Duplicating utility functions, validation helpers, or date parsers that already exist in shared locations. This lens catches the smaller code-quality issues: not bugs, but missed opportunities to keep the codebase coherent and maintainable.

## How to use this lens

Your compact lane bundle contains new public abstractions, configuration, and
actual documentation. Report duplication only with an anchored existing
equivalent and a material divergence. Naming, casing, and local style choices
are not findings unless they create a demonstrated correctness or maintenance
cost.

If nothing in the diff resembles reusable logic, or `find_symbol` reports no matching definition for the plausible utility names you probed, say so explicitly and return an empty findings list. Likewise, an empty `find_siblings` result is evidence that no comparable file was found, not permission to substitute an unrelated observation merely to justify the dispatch.

## What to check

**Hand-rolled date/time parsing instead of using a shared utility.** If a PR adds `datetime.strptime(date_str, '%Y-%m-%d')` and a shared utility already exists that does this with a consistent format, flag it. Example:

```python
# services/billing/reconciliation.py:42 (new in PR)
def parse_billing_date(date_string):
    return datetime.strptime(date_string, '%d/%m/%Y')  # FINDING: bespoke

# But shared/date_utils.py:18 (already exists) has:
def parse_iso_date(date_string):
    return datetime.fromisoformat(date_string)  # Shared utility
```

Flag: "Hand-rolled date parser at services/billing/reconciliation.py:42 duplicates shared/date_utils.py:18. Use parse_iso_date() instead."

**Custom HTTP retry logic instead of using a shared client.** Example:

```python
# integrations/payment_gateway.py:67 (new in PR)
def call_gateway(endpoint, payload):
    for attempt in range(3):
        try:
            return requests.post(endpoint, json=payload, timeout=5)
        except requests.exceptions.Timeout:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    # FINDING: bespoke retry loop

# But shared/http_client.py:10 (already exists) has:
class RetryableClient:
    def post(self, url, **kwargs):
        # Exponential backoff already implemented
```

**Validation function duplicated.** A new email validator, phone number parser, or postal code checker that resembles existing shared validation. Example:

```python
# validators/address.py:50 (new in PR)
def validate_postcode(code):
    return bool(re.match(r'^\d{5}(-\d{4})?$', code))

# But shared/validators.py:120 (already exists) has:
def is_valid_us_zip(code):
    return bool(re.match(r'^\d{5}(-\d{4})?$', code))
```

**Auth or permission-checking helper duplicated.** A new function checking user roles or permissions that overlaps with an existing one. Example:

```python
# views/admin.py:15 (new in PR)
def is_admin(user):
    return user.groups.filter(name='admin').exists()

# But shared/auth_utils.py:8 (already exists) has:
def user_in_group(user, group_name):
    return user.groups.filter(name=group_name).exists()
```

**Inconsistent naming or casing within a single PR.** Functions in the same file or related modules use different verb tenses or conventions. Example:

```python
# services/notification.py:10
def send_email(user, template):  # verb + object
    pass

# services/notification.py:45
def create_sms_notification(user, message):  # verb with "create_" prefix; inconsistent
    pass
```

If other functions in the file use `send_*`, flag the inconsistency. If the PR adds three similar functions—two as `send_*` and one as `create_*`—standardise to one pattern.

**Async helper duplicated.** A task wrapper, queue initialiser, or async context manager duplicated across modules when a shared version exists. Example:

```python
# workers/order_processor.py:12 (new in PR)
async def with_retry(coro, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            return await coro
        except Exception:
            if attempt == max_attempts - 1:
                raise

# But shared/async_utils.py:30 (already exists) has:
async def retry_async(coro, max_attempts=3):
    # Same logic
```

**Logging configuration duplicated.** A new module setting up logging with custom formatters, handlers, or filters, when shared logging config already exists. Example:

```python
# integrations/external_api.py:1 (new in PR)
logging.basicConfig(format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# But shared/logging_config.py:10 (already exists) exports:
logger = get_logger('integrations.external_api')  # Pre-configured
```

**Builtin shadowing and naming hygiene.** Check new local names, parameters,
or attributes that shadow Python builtins (`id`, `list`, `dict`, `type`,
`input`, `format`, etc.) or obscure a clear established domain name. Report
only when `find_siblings()` shows the neighbouring convention or when the
shadowing changes a concrete call/read in the changed scope; do not turn every
unusual identifier into a style finding.

## When no catalogue example matches

Use transferable consistency reasoning: obtain bounded evidence of the local
addition and a real sibling/shared alternative before finding drift. If no
sibling or equivalent can be established, return `[]` rather than inventing an
adjacent convention, and record the lookup result in `inputs_used`.

## What this lens does NOT check

- Whether duplicated code is *correct* (that is lens-logic-tests)
- Whether shared utilities are poorly designed (that is architectural)
- Whether performance differs between custom and shared (covered by lens-performance-db)
- Import cycles or module-level side effects (architectural/linting)
- Intentional specialisation (sometimes a custom validator exists for a reason)

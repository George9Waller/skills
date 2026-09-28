# lens-appsec: Authentication, PII, and Injection

## Portable reasoning boundaries

**Transferable principles:** trace untrusted input to an authority, query, shell, template, or secret; require an anchored data-flow fact before reporting. **Stack-specific triggers:** Python `Depends`/DRF permissions; TypeScript Express handlers and `process.env`; Go `net/http` handlers and `os.Getenv`; Terraform interpolation and secret outputs; Databricks bundle variables, permissions, and service-principal configuration. These are examples, never universal rules.

Authentication bypass, credential exposure, and injection vulnerabilities—the class of bugs where a correct-looking change silently creates a security gap. These are high-confidence findings: if a test is passing but a permission check or input validation is gone, the finding is not theoretical.

## How to use this lens

Your compact lane bundle contains the relevant contract facts, changed hunks,
and coherence entries. Trace a changed input or authorization boundary from a
current supported entry path to a sensitive sink; a known first-party caller
is useful but not required for a supported API request. Do not broaden the search.

If both inputs came back empty or nothing in this diff touches auth, permissions, or user input, say so explicitly and return an empty findings list — do not substitute unrelated observations to justify the dispatch.

## What to check

**Permission checks on DRF views.** If a view's `permission_classes` was removed or weakened (changed from `IsAuthenticated` to `AllowAny`, or from `IsAdminUser` to a custom checker), flag it. Verify the PR author intended this. Example:

```python
# services/api/views.py:87
class ExpenseReportViewSet(viewsets.ModelViewSet):
    permission_classes = []  # FINDING: was IsAuthenticated before
    queryset = Expense.objects.all()
```

Require file:line and the permission class that was removed.

**FastAPI Depends() dependencies removed or simplified.** If an endpoint's parameter list had `Depends(get_current_user)` removed, or if `Depends(check_admin_role)` was replaced with `Depends(lambda: True)`, flag it.

```python
# services/api/routes/payments.py:42
async def refund_payment(
    payment_id: int,
    # Depends(get_current_user) was here
):
    pass
```

**Raw SQL string interpolation.** Flag `.raw()`, `extra()`, or f-string SQL that interpolates user input without parameterized queries. Example:

```python
# models.py:156
query = f"SELECT * FROM users WHERE email = '{email}'"
# or
User.objects.raw(f"SELECT * FROM users WHERE id={request.GET['id']}")
```

**Serializer fields exposing sensitive data.** Check `get_contract_changes` for new fields added to serializers. Flag if password hashes, session tokens, internal IDs, or billing details are now readable. Look for `fields = '__all__'` that wasn't there before, or a forgotten `write_only=True` on a sensitive field.

```python
# serializers.py:34
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = '__all__'  # FINDING: password_hash is now exposed
```

**Hardcoded secrets in source code.** Flag API keys, database passwords, webhook signing keys, or OAuth secrets that appear as string literals (not env vars). Example:

```python
# config.py:12
STRIPE_SECRET_KEY = "sk_live_abcd1234"
```

**mark_safe() and format_html() misuse.** Flag these Django utilities if they receive user input or database fields without prior HTML escaping or explicit allowlisting. Example:

```python
# templates.py:89
return format_html('<div>{}</div>', user_comment)  # FINDING: user_comment is unescaped
```

## When no catalogue example matches

Use transferable security reasoning: identify the attacker-controlled source,
the changed sink or authorization boundary, and bounded evidence connecting
them. Do not invent an adjacent vulnerability from a sensitive-looking name.
If that path cannot be established from the bundle and bounded lookup, return
`[]` and record the uncertainty in `unanswered`.

## What this lens does NOT check

- SQL injection via ORM method names (that's rare in modern Django/DRF and requires contrived cases)
- Timing-based logic bugs in auth (constant-time comparison is framework-level)
- Missing CSRF tokens on forms (framework default in Django)
- Cryptographic strength of password hashing (framework-level in Django auth)
- Rate limiting or brute-force protection (covered by lens-load-growth)
- TLS/certificate pinning in external API calls (DevOps/infrastructure concern)

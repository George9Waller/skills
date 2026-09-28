# lens-impact: Deployment and Infrastructure

## Portable reasoning boundaries

**Transferable principles:** a new runtime dependency must have a matching deploy-time resource, permission, configuration value, and rollout path. **Stack-specific triggers:** Python environment/config; TypeScript deployment config; Go service flags; Terraform resources/variables; Databricks bundle resources, targets, and workspace paths. Pulumi examples below are conditional examples only.

The infrastructure required for new code to run in production—ensuring that application code changes are paired with the necessary environment, resources, and configuration. This is about whether production *has what the code needs*, not whether the deploy order is safe (covered by lens-contract).

## How to use this lens

Search the diff for: environment variable references (`os.environ`, `os.getenv`, settings config), new AWS resource usage (S3 bucket, DynamoDB table, SQS queue), Celery task definitions, and any external dependencies. Then check Pulumi files (CloudFormation or CDK-style infrastructure code) for corresponding resource provisioning.

If the diff has no new environment variables, external resources, or infrastructure-adjacent code, say so explicitly and return an empty findings list — do not substitute unrelated observations to justify the dispatch.

## What to check

**New environment variable referenced in application code without a Pulumi stack config entry.** If code does `api_key = os.environ['EXTERNAL_API_KEY']`, but the Pulumi stack config does not set this var, the application crashes at runtime with KeyError. Example:

```python
# services/payment_gateway.py:12
class StripeConfig:
    secret_key = os.environ['STRIPE_SECRET_KEY']  # FINDING: new in PR

# Check pulumi stack YAML (e.g. Pulumi.production.yaml):
# config:
#   stripe_secret_key: ...
# If missing or named differently, flag it.
```

**New AWS resource referenced in code with no corresponding Pulumi resource.** A new S3 bucket write, a new DynamoDB table query, or a new SQS queue reference without Pulumi provisioning it. Example:

```python
# workers/export_service.py:45
s3_client.put_object(
    Bucket='company-exports-prod',  # FINDING: new in PR
    Key=f'exports/{export_id}.csv',
    Body=data,
)

# Check Pulumi code (e.g. infra/stacks/storage.py):
# If aws.s3.Bucket('company-exports-prod') is not defined, flag it.
```

**New Celery task not registered in worker process definition.** A task added to `tasks.py` but the Pulumi task definition for the Celery worker service does not include it. Example:

```python
# tasks.py:100 (new in PR)
@celery_app.task
def process_bulk_export(export_id):
    # New task
    pass

# Check Pulumi infra/services/celery_worker.py:
# If the task module is not imported or the worker process doesn't run this task, flag it.
```

**Config drift between staging and production Pulumi stack files.** A resource, environment variable, or setting differs between `Pulumi.staging.yaml` and `Pulumi.production.yaml`, and the PR doesn't update both consistently. Example:

```yaml
# Pulumi.staging.yaml
config:
  database_replica_count: 1
  cache_ttl_seconds: 300

# Pulumi.production.yaml (unchanged in PR)
config:
  database_replica_count: 1  # FINDING: should this differ from staging after the PR?
  cache_ttl_seconds: 300     # If yes, should be updated here too
```

If the PR changes a setting in one stack file, verify it is intentional and applied to both.

**New external service dependency without credentials provisioned.** If code calls a third-party API (Stripe, SendGrid, Slack), the authentication token or API key must be in the environment. Example:

```python
# integrations/analytics.py:23
segment_client = analytics.Client(
    write_key=os.environ['SEGMENT_WRITE_KEY']  # FINDING: new dependency
)

# Check Pulumi for the Segment write key being set. If missing, flag it.
```

**New Lambda function or service definition referencing a code asset that is not built or deployed.** Pulumi should package and upload the code to the appropriate location (S3, Lambda layer, container registry). Example:

```python
# infra/stacks/lambdas.py (new in PR)
export_lambda = aws.lambda_.Function(
    'export-handler',
    runtime='python3.9',
    handler='handlers.export.handler',
    code=pulumi.FileArchive('../services/export_handler'),  # FINDING: does this path exist?
    # and does CI build and upload this artifact?
)
```

**New Redis connection without provisioning the Redis cluster or configuring cache replication.** If code adds a Redis dependency but Pulumi does not create the ElastiCache cluster or a local Redis container in the environment, the application fails. Example:

```python
# cache.py:5 (new in PR)
redis_client = redis.Redis(host='redis-cluster.internal', port=6379)

# Check Pulumi for aws.elasticache.Cluster or similar provisioning the cluster.
```

**Database migration file in PR without corresponding Pulumi RDS/database update if schema versioning is managed there.** Some teams manage schema via Pulumi; others via Django migrations. If the team uses Pulumi for schema management and the PR adds a migration file, verify Pulumi is updated. Example:

```python
# migrations/0043_add_column.py (new in PR)
# Check Pulumi database schema definition; if it's managed there, it must be updated too.
```

**Feature flag or A/B test config added to code without a way to toggle it in production.** If code checks `if settings.ENABLE_NEW_FEATURE:` but settings are not configurable via environment variable or admin console, the feature is always on (or always off) with no way to control it post-deploy. Example:

```python
# features.py:12
ENABLE_BULK_UPLOAD = os.environ.get('ENABLE_BULK_UPLOAD', 'false').lower() == 'true'

# Check: is there a Pulumi entry for ENABLE_BULK_UPLOAD?
# Or a feature-flag admin interface?
# If neither, flag: feature cannot be toggled without re-deploying.
```

## When no catalogue example matches

Use transferable deployment reasoning: identify the changed runtime
dependency, the concrete provisioning/configuration artifact it needs, and
bounded evidence that the artifact is absent or incompatible. Do not infer
missing infrastructure from a library import alone. If the relationship cannot
be established with the bundle and bounded lookup, return `[]` and record the
question in `unanswered`.

## What this lens does NOT check

- Whether deployment *order* is safe (e.g., must RDS migrate before Lambda deploys; that is lens-contract)
- Whether the Pulumi code itself has bugs or security issues (code review of infra is separate)
- Cloud cost or resource over-provisioning (financial/ops decision)
- Whether production secrets are *stored securely* (that is lens-appsec's hardcoded-secret check)
- Monitoring, logging, and observability instrumentation (DevOps concern, not blocking)

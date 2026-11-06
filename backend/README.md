# ORCA research-foundation backend

Python **3.12.12**; exact dependencies in `uv.lock`. P01 is complete and P02 code is present, but G02 remains blocked until the dedicated staging PostGIS/RLS and object-store checks run. No provider connector or scientific calculation is implemented yet.

From `backend/`:

```powershell
$env:UV_CACHE_DIR = (Join-Path (Split-Path $PWD) '.cache/uv')
$env:UV_PYTHON_INSTALL_DIR = (Join-Path (Split-Path $PWD) '.cache/python')
uv python install 3.12.12
uv sync --locked
$env:ORCA_MONITORING_TOKEN = (uv run python -c "import secrets; print(secrets.token_urlsafe(48))")
uv run uvicorn orca.bootstrap:create_application --factory --host 127.0.0.1
```

GET `/livez` and `/v1/meta` are public. GET `/readyz` requires the generated token as `Authorization: Bearer ...` and returns **503** while G02 lacks live evidence. This operational token does not authenticate application users. GET `/openapi.json` uses the same token unless `ORCA_DOCS_PUBLIC=true` in development. Production forbids public schema exposure. Do not put tokens in query strings.

The runtime OpenAPI contains only implemented health/metadata and project operations. The historical design inventory preserves all 220 old operations; `contracts/designed/research-route-dispositions.json` records the exact research-only decision, and `orca-research-selected.json` contains the 165 CORE/FED/OPS design ceiling. Neither design file claims those routes are implemented. Marine companion, alerts, consumer conversations/voice and model-training routes are absent from the selected and runtime contracts.

## Frontend test harness

```powershell
uv run uvicorn mock_server:create_mock_app --app-dir tests/support --factory --host 127.0.0.1 --port 8001
```

Read `/scenarios/success`, `/scenarios/empty`, `/scenarios/partial`, `/scenarios/error`. Responses are labeled synthetic test fixtures; this service is deliberately separate from production and absent from wheel/sdist. Generated types are in `../contracts/typescript/orca.ts`; authoritative validation schemas are in `../contracts/schemas/common.json`. Start with fetch-based HTTP; SSE belongs to P05.

## Verification

```powershell
uv run python ../tools/run_p00_gate.py
uv run python scripts/export_openapi.py
uv run python scripts/run_phase_gate.py P01
```

Gate outputs are in `../docs/gates/P01/`. Default Python tests deny network access. Clean lock installs use an isolated environment; local gates use cached dependencies offline, while a fresh CI worker may download only versions fixed by `uv.lock`. Do not mark the gate PASS when a required package, prerequisite, test or output is missing.

HTTP tests allow loopback solely for Windows' asyncio socket pair; requests to the app itself use an in-process ASGI transport. Windows sandbox policies that block even loopback can stall event-loop startup; run the gate in a normal local terminal. No external API credentials are needed for P01. The current Starlette version emits a deprecation warning for its supported httpx test transport; a later toolchain update can migrate that test dependency without changing production HTTP behavior.

## Folder boundaries and extending the backend

```text
backend/
  src/orca/
    bootstrap.py          # Application factory; validates configuration at startup
    config.py             # Runtime settings and secret handling
    api/                  # HTTP routes and sanitized problem responses
    contracts/            # Common wire types and validation, independent of persistence
  scripts/                # Gate runner, boundary/header checks, contract exporter
  tests/unit/             # Configuration, install and toolchain tests
  tests/contracts/        # Schemas, runtime API, packaging and gate behavior
  tests/support/          # Separately launched frontend mock server
  fixtures/api/           # Labeled synthetic scenarios; excluded from release artifacts
contracts/                # At repository root: generated schemas, OpenAPI and TypeScript
docs/gates/P01/            # At repository root: computed test and installation evidence
```

Later phases introduce domain/application/infrastructure modules when implementing their approved use cases. Add a typed contract, application service and route with its own phase tests, then include it in the runtime router and regenerated implemented OpenAPI. Provider I/O belongs in infrastructure adapters; domain/science modules cannot import HTTP or persistence frameworks. Register a capability only after its gate passes, with live verification separately recorded. The design-only inventory is a planning reference, not a route registry that automatically enables handlers.

Reference decisions: [P01 reconciliation](../docs/decisions/P01-contract-reconciliation.md), [main plan](../main_implementation%20plan.md), [API design](../api%20design.md). P02 is authentication, tenant-isolated persistence, projects and uploads. Its database and identity-provider decisions are intentionally deferred to that phase.

## P02 live environment

P02 uses PostgreSQL 17 with PostGIS 3.5 and versioned S3-compatible object storage. The compose file binds both services to loopback and requires generated local credentials:

```powershell
$env:ORCA_DB_PASSWORD = (uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
$env:ORCA_RUNTIME_PASSWORD = (uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
$env:ORCA_S3_ACCESS_KEY = 'orca-local-test'
$env:ORCA_S3_SECRET_KEY = (uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
docker compose -f ../infra/compose.yaml up -d --wait
$env:ORCA_TEST_DATABASE_URL = "postgresql+psycopg://orca_migrator:$($env:ORCA_DB_PASSWORD)@127.0.0.1:55432/orca_test"
$env:ORCA_TEST_RUNTIME_DATABASE_URL = "postgresql+psycopg://orca_runtime:$($env:ORCA_RUNTIME_PASSWORD)@127.0.0.1:55432/orca_test"
$env:ORCA_TEST_S3_ENDPOINT = 'http://127.0.0.1:59000'
$env:ORCA_TEST_S3_ACCESS_KEY = $env:ORCA_S3_ACCESS_KEY
$env:ORCA_TEST_S3_SECRET_KEY = $env:ORCA_S3_SECRET_KEY
uv run python scripts/run_p02_gate.py
```

The migration tests refuse a database whose name does not end in `_test`. G02 reports `BLOCKED` when these services are unavailable and never converts skipped live tests into a pass. The local cryptographic issuer tests prove verifier behavior; deployment still needs a selected issuer, audience and HTTPS JWKS URL.

## Supabase production and staging

ORCA supports Supabase as the managed production data layer: PostgreSQL/PostGIS,
Auth/JWKS and the Storage S3 endpoint. The FastAPI application remains a separate
service and must be deployed to an application host. Use two Supabase projects:

- `orca-production` contains real data and is never assigned to an `ORCA_TEST_*` variable.
- `orca-staging` is disposable integration infrastructure. P02 tests drop and rebuild
  the `iam` and `research` schemas, so do not point them at production.

Run `../infra/supabase/bootstrap.sql` once against each project with an admin/direct
connection and a generated `runtime_password`. Run Alembic from a deployment job with
the admin/direct or session-pooler URL. The running API receives only the custom
`orca_runtime` role URL, which is deliberately non-owner and cannot bypass RLS.

For a dedicated staging project, set the admin and runtime database URLs plus the
explicit safety acknowledgement before running G02:

```powershell
$env:ORCA_TEST_DATABASE_PURPOSE = 'dedicated-staging'
$env:ORCA_TEST_DATABASE_URL = '<staging admin/session-pooler URL>'
$env:ORCA_TEST_RUNTIME_DATABASE_URL = '<staging orca_runtime session-pooler URL>'
```

Production startup additionally requires `ORCA_ENVIRONMENT=production`, the runtime
database URL, Supabase Auth issuer/audience/JWKS URL, and Storage S3 endpoint/access
keys. Store these in the deployment platform's secret manager; never commit or paste
them into logs. Supabase Storage does not expose native S3 bucket versioning, so ORCA
publishes verified objects to immutable SHA-256-addressed final keys and records the
digest as the application-level version. Local MinIO may still enable native versions
by constructing `S3ObjectStore(..., native_versioning=True)`.

## P03 acquisition worker (implemented, deployment acceptance pending)

The API and acquisition worker are separate processes sharing the non-owner database
URL and private S3 configuration. The worker does not load a local `.env` implicitly;
inject environment variables through the deployment secret manager or your launcher.
Do not use the admin migration database URL for either runtime process.

1. Register the real tenant catalog product/version, exact ERDDAP metadata-response
   SHA-256 and affirmative source-preservation license evidence. This batch does not
   seed products, approve licenses or invent baseline records.
2. Provision a private storage bucket and restrict its credentials to the backend.
3. Set `ORCA_ACQUISITION_SOURCES` to a JSON array of objects with `tenant_id`,
   `version_id`, `host` and `dataset`. Allowed hosts are `coastwatch.pfeg.noaa.gov`
   and `erddap.incois.gov.in`; identifiers must match stored tenant versions.
4. Supply `ORCA_OBJECT_STORE_REGION` (for this project, `ap-south-1`), endpoint,
   bucket and credentials, plus `ORCA_ACQUISITION_ENABLED=true` in both processes.
5. Start `python -m orca.worker --tenant <tenant-uuid> --watch`. Omit `--watch` for
   a single durable request. Start the API with
   `uvicorn orca.bootstrap:create_application --factory --host 0.0.0.0 --port 8000`.

Enabling acquisition is an operator acknowledgement that the worker is deployed;
it is not a heartbeat check. Requests for unconfigured versions return 422. Source
validation failures become sanitized failures; infrastructure interruptions leave
leases recoverable. Monitor worker exits and restart it through your host supervisor.

The adapter accepts points and rectangular polygons, at most 2 degrees per axis,
31 days, 100,000 CSV rows and 8 MiB. Large high-resolution requests need smaller
windows. Out-of-scope nearest-coordinate rounding, changed metadata and ambiguous
dimensions are rejected. Raw snapshots remain scientifically `not_validated`:
preserving bytes is not QC, a compatible seasonal baseline or a scientific result.

Fitness and compatibility APIs evaluate stored metadata and report conflicts or
unknowns. They cannot report overall compatibility until method/QC evidence exists.
Project-scoped grants, durable source probes and full G03 remain unfinished. See
`docs/decisions/delivery-progress.json` for phase counts and
`docs/decisions/P03-cloud-acquisition-and-fitness.json` for exact limitations.

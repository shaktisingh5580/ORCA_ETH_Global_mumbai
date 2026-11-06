# ORCA P01 backend

Python **3.12.12**; exact dependencies in `uv.lock`. No database, provider connector or scientific calculation is implemented at P01.

From `backend/`:

```powershell
$env:UV_CACHE_DIR = (Join-Path (Split-Path $PWD) '.cache/uv')
$env:UV_PYTHON_INSTALL_DIR = (Join-Path (Split-Path $PWD) '.cache/python')
uv python install 3.12.12
uv sync --locked
$env:ORCA_MONITORING_TOKEN = (uv run python -c "import secrets; print(secrets.token_urlsafe(48))")
uv run uvicorn orca.bootstrap:create_application --factory --host 127.0.0.1
```

GET `/livez` and `/v1/meta` are public. GET `/readyz` requires the generated token as `Authorization: Bearer ...` and returns **503** because core G02–G08 capabilities are not implemented. This operational token does not authenticate application users; OIDC/membership work is P02. GET `/openapi.json` uses the same token unless `ORCA_DOCS_PUBLIC=true` in development. Production forbids public schema exposure. Do not put tokens in query strings.

The implemented OpenAPI contains four operations. The separate design-only document inventories all 220 proposed operations with source metadata; later DTO schemas/handlers are not fabricated. Shared typed models include projects, errors, time/geometry/IDs, budgets and scientific outcome envelopes. They do not claim a working project or science endpoint.

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

Gate outputs are in `../docs/gates/P01/`. Default tests deny network access. Clean lock installs use an isolated environment and offline cached dependencies; an uncached platform must first `uv sync --locked`. CI performs that acquisition before offline checks. Do not mark the gate PASS when a required package, prerequisite, test or output is missing.

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

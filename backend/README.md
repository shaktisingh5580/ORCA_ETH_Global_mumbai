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

## Durable operator metadata probes (P03)

Assign the explicit `source_operator` organization membership role to authorized
operators and issue tokens with `source:manage`. Set `ORCA_SOURCE_PROBES_ENABLED=true`
and the approved `ORCA_ACQUISITION_SOURCES` bindings in the API and probe worker.
Start `python -m orca.probe_worker --tenant <tenant-uuid> --watch` using the runtime
database URL. Snapshot acquisition and S3 configuration are not required for probes.

`POST /v1/catalog/source-probes` accepts organization/provider IDs, `distribution_id`
(the configured version UUID), region, time window and optional byte/time limits;
it requires `Idempotency-Key`. Poll `GET /v1/catalog/source-probes/{id}` with
`organization_id`. Requests are durable, tenant isolated and recoverable after
expired leases. Status reads require the same operator authorization.

This workflow checks metadata access. Region/time are recorded context; evidence
explicitly says they have not been verified. Metadata drift is reported as a
nonmatching digest. A completed access probe does not validate scientific coverage,
source rights, QC, seasonal baselines or P03 readiness. Deploy rate quotas and
supervise worker health before enabling it in production.

## Accelerated research + Arkiv demo

This opt-in slice is descriptive SST research with a minimal public Arkiv reference.
It is not the completed P04/P11 release. Automated test execution was deferred at
the user's request; static checks and actual Supabase materialization are recorded
separately in `docs/gates/DEMO/manifest.json`.

The approved cloud setup has applied migrations through `0008_demo_results` and
created private `.env.demo`. Real MUR access-sample bytes and a descriptive result
are in Supabase. Local IDs are in `.cache/demo/cloud-demo.json`, the full private
result in `.cache/demo/research-result.json`, and the minimal approved public
reference in `.cache/demo/public-result.json`. These files are intentionally ignored.

From the repository root, start the API:

```powershell
backend/.venv/Scripts/python -m uvicorn orca.bootstrap:create_application --factory --env-file .env.demo --host 127.0.0.1 --port 8000
```

Create a real account in Supabase Authentication. Obtain its Auth UUID, then run:

```powershell
backend/.venv/Scripts/python backend/scripts/bind_demo_user.py --subject <real-auth-user-uuid>
backend/.venv/Scripts/python backend/scripts/demo_login.py
$token = Get-Content .cache/demo/access-token.txt -Raw
$demo = Get-Content .cache/demo/cloud-demo.json -Raw | ConvertFrom-Json
Invoke-RestMethod "http://127.0.0.1:8000/v1/demo/research-runs/$($demo.result_id)?organization_id=$($demo.organization_id)" -Headers @{Authorization="Bearer $token"}
```

`POST /v1/demo/research-runs` accepts `organization_id` and `snapshot_id`; it reads
and hashes private cloud bytes and persists a deterministic immutable result.
`GET /v1/demo/research-runs/{id}` returns the authorized result.
`POST /v1/demo/research-runs/{id}/public-export` accepts the same IDs and explicitly
approves the returned minimal public reference, subject to redistribution rights.
The full source/result, coordinates, user identity and credentials stay off Arkiv.

The dedicated local demo wallet lives in ignored `.env.arkiv`. Its public address is
`0x8cBFE2bD01C7FDfc500e62A1cF99A01B6A11bCBe`. Obtain Rabdi tGLM from
https://hub.arkiv.network/. If the faucet requires a connected wallet, import this
dedicated test account into your wallet using the private key from that local file;
do not paste it into chat or a website. An optional RPC access key goes into
`ARKIV_ACCESS_KEY` in the same file. Rabdi chain ID is `7794095`.

From `arkiv/`:

```powershell
npm ci --ignore-scripts
node --env-file=../.env.arkiv src/cli.mjs status
node src/cli.mjs inspect --file ../.cache/demo/public-result.json
node --env-file=../.env.arkiv src/cli.mjs publish --file ../.cache/demo/public-result.json --approve-public --receipt ../.cache/demo/arkiv-receipt.json
```

Publication writes a read-only seven-day entity, saves the transaction receipt,
then reads it back from the chain and verifies owner, schema and expected export.
Independently read it without private keys using the receipt's entity key and owner:

```powershell
node --env-file=../.env.arkiv src/cli.mjs read --key <entity-key> --owner <receipt-owner> --file ../.cache/demo/public-result.json
```

Wallet ownership does not establish ENS or institutional authority. Full federation,
ENS binding, shared task/assignment coordination and review remain future work.
Never blindly retry a `submission_started` receipt: reconcile wallet transactions
first to avoid duplicate writes after ambiguous network failures. A confirmed
receipt survives a later read-back failure. Public records expire; this is not a
permanent archive. The demo reference is now live on Rabdi; the confirmed transaction
and separate read-back evidence are in `docs/gates/DEMO/arkiv-publication.json`.
The receipt is already present locally, so do not rerun publication to the same
receipt path. Use the read command to inspect the existing record.

To package the API, build `docker build -f infra/Dockerfile.demo -t orca-demo .`
and inject `.env.demo` through the deployment platform's private environment.
This image configuration has not yet been built/deployment-verified. Run migrations
as a separate controlled admin step; the API always uses `orca_runtime`.

## Frontend connection

The local API base URL is `http://127.0.0.1:8000`. Configure `ORCA_CORS_ORIGINS`
as a JSON array of exact frontend origins. The local `.env.demo` allows localhost
and 127.0.0.1 on ports 3000 and 5173. Bearer-token requests do not use cross-origin
cookies. Production requires an HTTPS API deployment and the exact deployed
frontend origin; localhost does not point to this machine for remote users.

Sign in using the same Supabase project's regular user authentication, retrieve
`session.access_token`, and send `Authorization: Bearer <access_token>` with each
private backend request. Never use a service-role or monitoring token in the browser.
The session's subject must have an active organization membership.

The demo consumer reads `GET /v1/demo/research-runs/{result_id}?organization_id={organization_id}`.
Render `sample_mean`, `minimum`, `maximum`, `finite_count`, `units`,
`daily_sample_means`, `measurement_definition`, `scientific_status` and `limitations`.
Recompute via `POST /v1/demo/research-runs` with `organization_id` and `snapshot_id`.
The live IDs are in local `.cache/demo/cloud-demo.json`. The demo-specific schema
is `contracts/openapi/orca-demo.json`; the core profile schema is `orca-v1.json`.

Only implemented routes in these contracts are available. The legacy Marine
advisory/observation UI is excluded from the selected research product and must not
call `/v1/marine/*`. A planned route in a design document is not a live endpoint.

The workspace selector now has implemented `GET /v1/me` and
`GET /v1/organizations?limit=20&cursor=<optional-last-id>` endpoints. Both require a
real bearer user session. `me` returns `user_id`, `orgs`, `roles`, `capabilities` and
an optional continuation cursor; the organization endpoint returns
`{items,next_cursor,page_size}`. Use the organization's `id` for `organization_id`
in subsequent requests. An empty organization array differs from a failed request;
show authentication/network errors instead of labeling them "no organizations".

`contracts/javascript/orca-client.mjs` is a reusable browser client. Supply its
`getAccessToken` callback using your initialized Supabase client:

```javascript
const api = createOrcaClient({
  baseUrl: "http://127.0.0.1:8000",
  getAccessToken: async () => {
    const { data: { session }, error } = await supabase.auth.getSession();
    if (error) throw error;
    return session?.access_token;
  },
});
const me = await api.me();
const { items: organizations } = await api.organizations();
```

For the private local demo, `ORCA_CORS_LOCAL_DEVELOPMENT=true` allows HTTP browser
origins on localhost/127.0.0.1 ports, including caller `X-Request-ID` and
`X-Organization-ID` headers. These headers do not confer permissions. Production
rejects that setting; configure exact HTTPS frontend origins instead. There is
no need to change Supabase RLS or expose private research schemas to browser roles.

## Same-Wi-Fi dashboard integration

The selected demo setup runs the frontend locally on the other laptop and the API
on this laptop. The observed Wi-Fi API address is `http://10.71.71.246:8000`;
`/livez` responds on that address from the server laptop. The frontend developer
must check `/livez` from their own laptop before authentication troubleshooting.
Guest/event Wi-Fi may isolate clients; server-side checks cannot prove peer reachability.

`backend/scripts/start_lan_server.ps1` runs the server hidden on `0.0.0.0:8000` and
writes PID/logs under ignored `.cache/lan-server/`. A scoped Windows firewall rule
`ORCA-Demo-LAN-8000` allows inbound TCP 8000 from LocalSubnet; its setup helper is
`backend/scripts/configure_lan_firewall.ps1` and requires administrator permission.
No router port forwarding or Public-to-Private network profile change was made.

The private demo environment enables `ORCA_CORS_LAN_DEVELOPMENT=true` for HTTP
RFC1918 frontend origins in addition to localhost. Both development CORS switches
are rejected in production. The Wi-Fi IP may change; update the frontend API base
URL after reconnecting to another network.

The comprehensive local handoff is `FRONTEND_DASHBOARD_INTEGRATION_HANDOFF.md` in the
workspace root. It is intentionally ignored under the user's root Markdown policy;
send that file directly to the frontend developer/AI assistant. It covers actual
routes, dashboard changes, session headers, charts, unsupported features, LAN
diagnostics and an optional later Vercel/HTTPS tunnel approach. The prepared tunnel
helper has not been executed and no public tunnel is active.

## Terminal-only research and Arkiv demonstration

From the repository root:

```powershell
backend/.venv/Scripts/python backend/scripts/demo_terminal.py
```

For a presentation that waits between stages and prints the daily table:

```powershell
backend/.venv/Scripts/python backend/scripts/demo_terminal.py --pause --show-daily
```

It reads real snapshot/result records using the tenant-scoped cloud runtime role,
reads and hashes the private source bytes, recomputes the descriptive result, compares
the result digest, and reads the existing Arkiv entity through a separate SDK process.
That subprocess receives only the RPC access key, not wallet or Supabase credentials.
The command does not require the frontend or FastAPI process, and creates no new
chain transactions. It is an operator demo using private runtime configuration;
it does not replace the authenticated browser/API acceptance flow or full scientific
validation. Private `.env.demo`, `.env.arkiv` and prepared local resource IDs are
required. On RPC failure or entity expiry it stops without claiming Arkiv success.

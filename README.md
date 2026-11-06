# ORCA: inspectable collaboration between marine research agents

ORCA connects a bounded marine research calculation to signed agent claims, an exclusive assignment, authenticated A2A execution, numerical reproduction/review and independently queryable Arkiv records. The hackathon prototype uses Supabase Auth/Postgres/private storage and separately keyed, team-operated provider processes.

## What actually works

- Real preserved NASA JPL MUR inputs: January 2025 in a small North Arabian Sea region against matching January days from 2003–2022.
- Authorized question/plan/approval/run/report workflow and private project/file APIs.
- Official A2A 0.3.0 JSON-RPC transport, pinned Python SDK 0.3.22; persistent provider task stores.
- Task/claim/exclusive assignment/result/review coordination with real Rabdi writes and owner/signature/digest read-back.
- Read-only verification without the ORCA API/database. See `docs/gates/HACKATHON/H04-live-coordination.json` and `H04-standalone-verification.json`.
- H05 adds an isolated intentional-input-bias demonstration and capability-based public task discovery. See the phase's current gate before claiming a deployed or validated feature.

The original real-data rehearsal produced about +0.623°C. This is a bounded prototype comparison, not an independently qualified scientific finding or a 30-year climate normal. The providers are team-operated; no ENS or institutional verification is claimed. Optional Gemini explains the fixed plan when configured; deterministic code computes the values.

## Deployment

The frontend is a separate existing Vercel application. The selected backend destination is now Oracle Always Free, configured in `infra/oracle/compose.yaml` with a Caddy HTTPS proxy. Its cloud container supervises the API, scientific/coordinator/upload workers and loopback A2A providers. Supabase remains the managed data layer. Docker volumes on the VM disk preserve SQLite and transaction-reservation recovery. The operator must select resources within the account's Always Free allowance. `render.yaml` remains an alternative paid deployment, not the selected destination.

`infra/Dockerfile.render` excludes demonstration tooling. `infra/Dockerfile.hackathon` deliberately includes separately labelled synthetic demonstration tooling, while the production Python wheel remains fixture-free. Neither image contains credentials or captured research files. Real source bytes are restored from private cloud storage and checked against pinned digests at runtime.

The live backend origin is https://orca.130.210.15.82.sslip.io; `/livez` returns 200 over trusted HTTPS and was opened successfully in a browser. The exact Vercel origin `https://orca-seven-phi.vercel.app` is allowed by CORS. Private local deployment/handoff documents are excluded from this public repository by user policy; request those from the project operator.

Oracle ARM build, public HTTPS/preflight, real research execution/report generation, actual private A2A disagreement/correction, historical Arkiv reads, and persistence across a backend restart have passed. Vercel URL configuration, proposal-shaped frontend adapters and the authenticated frontend walkthrough remain pending. See `docs/gates/HACKATHON/H05-oracle-runtime.json`; browser liveness is not full frontend workflow acceptance.

## Reproduce the development environment

Use Python 3.12.12, Node 22+, and uv 0.9.30:

```sh
uv sync --project backend --locked
npm --prefix arkiv ci --ignore-scripts
npm --prefix arkiv run check
```

Runtime configuration requires the restricted database role, Supabase Auth settings, private S3-compatible storage and dedicated testnet signing environments. Do not point destructive integration tests at the real Supabase project. `.env` files, wallet keys, caches and root design documents remain excluded from Git.

## Arkiv network and actual writer addresses

Network: **Rabdi**, chain ID **7794095**. SDK: `@arkiv-network/sdk` **1.0.1**.

- Requester/coordinator: `0x8cBFE2bD01C7FDfc500e62A1cF99A01B6A11bCBe`
- Ocean provider: `0x16E488B2A757B6dE437c66c33b1C58025F351915`
- Reviewer: `0x28B4e240a1DB273eaE78d66DD11013B5ED2279A4`

One actual completed task is `84410849-a102-4269-b52f-d7d3333ec626`. Entities are expiring testnet records; availability must be checked live.

To query this task, set only `ARKIV_ACCESS_KEY` in your terminal if required by the RPC service:

```sh
echo '{"command":"query","task_id":"84410849-a102-4269-b52f-d7d3333ec626"}' | node arkiv/src/coordination.mjs
```

To discover v2 advertisements by capability, including their explicit source labels:

```sh
echo '{"command":"discover","capability":"sst_reproduction","source_mode":"real","region":"north_arabian_sea","owner":"0x8cBFE2bD01C7FDfc500e62A1cF99A01B6A11bCBe"}' | node arkiv/src/coordination.mjs
```

Discovery can legitimately return an empty board if no matching unexpired open task is advertised. The first example reads the retained task history; the second uses the new capability index. For application-level owner/signature filtering and assignment exclusion, use the provider discovery CLI or authenticated board API.

## Source, license and AI disclosure

Real data: [NASA JPL MUR via NOAA ERDDAP](https://coastwatch.pfeg.noaa.gov/erddap/info/jplMURSST41/index.html). Retained input metadata records source licensing and NASA/JPL acknowledgment. Scientific source data is not replaced by the code license. Intentional synthetic cases are marked `synthetic_demo` and are not observational marine findings.

Code licensing is in `LICENSE`. Codex/AI assistance was used for architecture review, implementation, tests, documentation and debugging. Runtime Gemini planning is optional and is labelled according to whether an actual provider call was used. Team review, actual test evidence and observed chain transactions remain distinct from generated suggestions.

Sponsor submission details: [schema](arkiv/schema.md), [feedback](arkiv/friction.md), [reach](arkiv/reach.md), [why Arkiv](arkiv/why-arkiv.md), [actual use](arkiv/how-you-use-arkiv.md).

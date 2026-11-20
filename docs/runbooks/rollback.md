# ORCA staging rollback

Purpose: stop an unsafe release without losing accepted research evidence. References: P14.5; D-DB4 §§7–8; API design §§14,20.

1. Before rollout, record the prior and candidate API/worker image digests, schema revision and object-store inventory. Take and verify a coherent database/object restore point in isolation. See [disaster recovery](disaster_recovery.md).
2. On incident, freeze release changes, close write admission and fence workers before changing database or image targets. Record the cutoff and preserve run, publication and outbox evidence. Investigate unresolved Arkiv transactions by readback.
3. Roll back application images alone only if the exact prior API and workers passed staging against the **current** schema and write formats. Verify the prior instance consumes the correct schema; never route old and new writers together during recovery.
4. An incompatible or unknown schema requires an isolated verified restore or an approved forward fix. The current identity-binding migration refuses downgrade. Do not stamp Alembic revisions, drop protected tables, or point the old release at an untested migrated database. Record writes accepted after the recovery point and obtain approval for reconciliation or loss before cutover.
5. Verify readiness, two-tenant access checks, stable artifact hashes, one durable checkpoint recovery, budget settlement, and publication reconciliation. Measure downtime and unrecovered writes. Reopen traffic only after platform and release owners record approval and keep a rollback record.

The local P14.5 database test checks that Alembic refuses the protected downgrade and leaves the schema and recorded revision intact. Staging still needs the image compatibility and full rollback/restore drill with the exact selected deployment.

To repeat the local drill, provision a disposable PostgreSQL/PostGIS database named `orca_p145_*_test` on `127.0.0.1` with an explicit port and the normal restricted `orca_runtime` role. Set `ORCA_TEST_DATABASE_URL` and `ORCA_TEST_RUNTIME_DATABASE_URL` to that database, then run from the repository root:

```powershell
& .\backend\.venv\Scripts\python.exe -m pytest -c backend/pyproject.toml backend/tests/e2e/test_operational_handover.py backend/tests/integration/test_run_rate_limit_database.py -q
```

The quota test seeds and resets test data. Use a new disposable database, never a shared staging or production database.

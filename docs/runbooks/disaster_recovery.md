# ORCA disaster recovery

This runbook covers the `research-federated` release profile. The database, versioned object store, and method/runtime registry are the recovery inputs. Arkiv public records are references and can have their own retention limits.

## Capture a coherent recovery point

1. Record the cutover owner, source revision, migration revision, image digests, UTC time, tenant scope, and approved retention and recovery targets.
2. Pause writers that change database object references and wait for active writers to stop. Keep the PostgreSQL capture and object backup within that frozen write window. For storage that uses native versions, retain the version IDs referenced by the database. Keep the provider's encryption keys and documented restore procedures available through its recovery process.
3. Set `ORCA_RESTORE_DATABASE_URL` through the operator's secret store. Capture the immutable references and save the reported digest in the independent backup record.

   ```powershell
   & .\backend\.venv\Scripts\python.exe backend/scripts/restore_verify.py capture references.json
   ```

4. Create the approved PostgreSQL backup and version-preserving object backup. Track elapsed time, completeness, archive digests and custody outside the database. Distinguish the captured cutoff from any later committed writes.

## Restore and verify

1. Prepare a clean, isolated database and object store. Provision the intended runtimes, roles and extensions before restoring. Use only authorized archives and block publication and production traffic to the new environment.
2. Restore the database and the object backup. Native versions must retain their original identities. A copy that creates a new latest version cannot satisfy a pinned database reference. Content-addressed snapshots are checked against their exact bytes.
3. Point `ORCA_RESTORE_DATABASE_URL` and `ORCA_RESTORE_S3_ENDPOINT`, `ORCA_RESTORE_S3_BUCKET`, `ORCA_RESTORE_S3_ACCESS_KEY`, `ORCA_RESTORE_S3_SECRET_KEY` and `ORCA_RESTORE_S3_REGION` at the isolated target. Verify using the digest recorded at the source.

   ```powershell
   & .\backend\.venv\Scripts\python.exe backend/scripts/restore_verify.py verify references.json --expected-digest $recordedDigest
   ```

   Expect `REFERENCES_AND_OBJECT_BYTES_VERIFIED` and the captured object count. An error blocks cutover. Keep manifests and archive contents in restricted incident storage.

4. Test the restored runtime role with distinct tenants. Run a preserved-input science check, verify outbox recovery and inspect recorded events. Measure time from restore start to verified readiness and compare restored state against committed source changes. Use the resulting times and losses for this drill only.
5. Fence all workers and writers from the old database before opening the target. Wait for recorded leases to expire or use the approved administrative recovery workflow. Verify that a new lease generation reuses the committed checkpoint, rejects the stale generation and settles the budget once. Bring access and traffic back after the on-call owner approves the result.

## Dependency interruptions

- **Arkiv unavailable.** Keep remote publication in reconciliation and use readback when connectivity returns. Authorized core analysis can continue with retained, verified source bytes. Keep the publication outcome separate from the scientific outcome.
- **ERDDAP returns 429.** The snapshot request records `SOURCE_RATE_LIMITED` and publishes no snapshot. Wait according to the provider's published retry policy before scheduling a new request. A 404 or 410 records `SOURCE_UNAVAILABLE`. Use an authorized exact archived input if the method permits it. If required evidence is unavailable, keep the analysis blocked.
- **Pinned native version unavailable.** Identify the missing version in the authorized backup inventory. Recover that version and rerun verification before enabling traffic.

## Local drill

Use two disposable loopback PostgreSQL clusters with the runtime role provisioned, a clean target, a local MinIO server image and a separate local toolbox image with `tar`. Identify both images by ID. The tests require source databases named `orca_p14_source_*_test` and targets named `orca_p14_restore_*_test`. The drill deliberately disables connections to the named source test database after the capture. Set `ORCA_TEST_DATABASE_URL`, `ORCA_TEST_RUNTIME_DATABASE_URL`, `ORCA_TEST_RESTORE_DATABASE_URL`, `ORCA_TEST_RESTORE_RUNTIME_URL`, `ORCA_TEST_PG_BIN`, `ORCA_TEST_RESTORE_IMAGE` and `ORCA_TEST_RESTORE_TOOL_IMAGE`, then run:

```powershell
& .\backend\.venv\Scripts\python.exe -m pytest -c backend/pyproject.toml backend/tests/recovery/test_disaster_recovery.py backend/tests/recovery/test_restore_verify.py backend/tests/science/test_real_reference_replay.py backend/tests/unit/test_acquisition_adapters.py -q
```

The local drill uses a private test archive and records a logical restore and cutover. Production acceptance requires an approved encrypted backup, a deployed-target restore, measured recovery objectives and operator signoff. [Local restore evidence](../gates/P14/restore-manifest.json)

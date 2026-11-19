# Arkiv verification and reconciliation

Use the independent verifier with the approved public intent and the entity key. Obtain the intent through the authorized ORCA workflow or another trusted handoff. Treat the expected wallet and attributes as part of that handoff.

```powershell
node verifier/verify-arkiv.mjs <intent.json> <entity-key>
node verifier/verify-arkiv.mjs <intent.json> <entity-key> --tx <transaction-hash>
node verifier/verify-arkiv.mjs <intent.json> <entity-key> --canonical-artifact <artifact-payload.jcs>
```

Run from the repository root with the locked dependencies installed under `arkiv/`. The verifier reads Rabdi directly and needs only a read access key if the public RPC is rate limited. It does not call the ORCA API. For the optional artifact check, supply RFC 8785 canonical bytes of the signed envelope's `payload`. The complete envelope and a pretty-printed JSON export have different byte sequences. Keep restricted artifact files local.

The verifier reports the current entity observation, the creation receipt check when a transaction hash is provided, and the optional artifact digest check. A matching digest supports byte integrity. Scientific validation and institutional approval require their own evidence.

## An uncertain write

Keep the original public intent, writer state directory, and any transaction hash. Run read-only reconciliation on the same host or against its preserved state directory.

```powershell
Get-Content -Raw <intent.json> | node arkiv/src/public-outbox.mjs reconcile <state-directory>
```

| Outcome | Meaning |
| --- | --- |
| `indexed` with transaction hash | The current entity matches the reservation and the transaction contains a matching creation event in the observed canonical block. |
| `indexed` with null transaction hash | The reserved key and attributes match a current entity. The transaction receipt still needs to be located. |
| `unresolved` | Preserve the reservation. The entity or creation receipt is missing, expired, or inconsistent with the current chain view. |
| `manual_review` | The returned key or transaction hash differs from the reservation. Investigate the writer's nonce, competing writes and transaction history. |
| `unreserved` | This directory has no readable reservation for the given intent. Verify the state directory and original operator before drawing conclusions. |

An absent entity after its recorded creation expiry might have expired, been deleted, or been extended and later removed. Treat the recorded expiry as historical evidence. A changed block hash or missing receipt requires another read after the chain settles.

For an unresolved submission, retain the writer lock and preserve its files. The legacy `reconcile_agent_operation.py --resume` command rejects Arkiv v1 operations.

For an authorized ORCA operation with `ARKIV_V1_RECONCILIATION_REQUIRED`, the original requester can call `POST /v1/network/operations/{operation_id}/arkiv-recovery?project_id={project_id}`. The request queues one worker attempt under the existing deadline and attempt budget. Poll the operation with the existing GET route. The worker reconstructs the same intent and checks both the signed publication and its v1 companion from their stored reservations. A missing or unreliable observation leaves the operation on hold. An assignment reaches A2A dispatch only after both observations succeed. Repeated recovery requests while the operation is queued or running reuse the same operation.

The API leaves writer locks and reservations intact. If a write has been independently identified and the wallet remains locked, retain its proof and coordinate wallet recovery with the operator while the writer processes are stopped.

Read-only RPC requests retry recognized rate limits at most twice with a five-second cumulative wait budget. An excessive `Retry-After` or a different transport failure returns an error. Chain submission uses the existing write reservation and never inherits this retry policy.

[Arkiv creation events](https://stage.docs.arkiv.network/json-rpc/mutating-entities/) · [Rabdi access keys](https://docs.arkiv.network/start-here/access-keys/)

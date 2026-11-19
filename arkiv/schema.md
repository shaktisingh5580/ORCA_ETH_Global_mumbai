# ORCA Arkiv schema and ownership

## P11 public records

The main-plan adapter defines `orca.agent.v1`, `orca.task.v1`,
`orca.claim.v1`, `orca.assignment.v1`, `orca.result.v1` and
`orca.review.v1`. Each profile accepts an exact set of attributes. Task
geography is limited to a named ocean basin or `global`. Creation options
use Arkiv read-only entities with lifetimes capped at 3 days for tasks,
claims and assignments, 7 days for agent references and 28 days for
result and review references.

Run from the repository root with the pinned SDK installed:

```text
npm --prefix arkiv run rabdi:preflight
node arkiv/src/public-client.mjs read <kind> <entity-key> <expected-wallet>
node arkiv/src/public-client.mjs query-task <kind> <task-id> <expected-wallet>
```

The reader checks the expected creator and current owner against the chain.
Supply that wallet from an independently trusted source. The task query
works for schemas indexed by task ID and returns one bounded page with the
query block, current observed block and any lag. Absence is reported as
`not_found`. An available expiration block allows an expired result to
be labeled `expired`. Chain observations carry no institutional,
assignment or scientific approval.

Official references: [Rabdi](https://docs.arkiv.network/networks/rabdi/),
[limits](https://docs.arkiv.network/reference/limits/),
[queries](https://docs.arkiv.network/reference/query-language/) and
[expiration](https://docs.arkiv.network/writing/expiration/).

### P11 task, claim and assignment publication

In the authenticated network API, create a task with both
`approve_public_reference: true` and `publish_arkiv_v1: true`. The
worker publishes the existing signed task and the minimal `orca.task.v1`
companion. The task's timeline receipt contains the companion under
`arkiv_v1`. The v1 companion uses `regionCode: global` to omit local
geography.

On the provider host, run the existing discovery command with
`--claim-task <task-id> --arkiv-v1-task-key <task-entity-key>`. The host
verifies the signed task and companion against the pinned requester, then
publishes both claim references with its own wallet. Import the returned
`claim_entity_key` as `entity_key` and the companion as
`arkiv_v1_entity_key` in the existing authenticated claim import.

Selecting the accepted claim uses ORCA's database assignment path. The
requester's worker checks both references and publishes an
`orca.assignment.v1` companion with the selected claim key and signed
assignment digest before asking the provider to execute. The older signed
record remains the execution proof. This path uses the current operator-pinned
providers while institutional credential acceptance is pending.

The standalone operator adapter accepts a consented intent on standard
input. It expects `kind`, `owner`, `lifetimeSeconds`, and the exact
`attributes` for that public schema. A claim also needs `parent` with
the task entity `key` and its independently known requester `owner`.
The claimant wallet must differ from the requester's wallet. The adapter
checks the current parent record and caps lifetime at the remaining task
deadline. An assignment takes the same parent and a `selectedClaim` with
its expected provider `owner` and exact public claim `attributes`.

Use the same private, durable state directory for every invocation by a
given writer. Keep `ARKIV_PRIVATE_KEY` on its operator's host and out of
the input document. The read-only reconciliation command uses the public
intent and the corresponding state directory.

```powershell
Get-Content -Raw <intent.json> | node arkiv/src/public-outbox.mjs publish <state-directory> --approve-public
Get-Content -Raw <intent.json> | node arkiv/src/public-outbox.mjs reconcile <state-directory>
```

An unresolved send keeps the wallet lock, the predicted entity key, and
the reserved intent. Reconciliation compares current chain readback with
the reserved owner and attributes. An absent record can mean index lag,
expiration or a missing transaction, so it leaves publication unresolved.
If recovery finds the entity after the response was lost, the result can
include the entity key with a null transaction hash. The operator must
review the retained lock before allowing further wallet writes. A public
claim remains a proposal until ORCA selects the assignment in its database.

See [Arkiv's create options](https://docs.arkiv.network/typescript-sdk/api-reference/main/type-aliases/createentityoptions/)
and [key prediction](https://docs.arkiv.network/typescript-sdk/api-reference/main/functions/predictentitykey/).

## H04 coordination records

`orca.coordination.hackathon.v1` is the deployed H04 coordination schema. V2, `orca.coordination.hackathon.v2`, adds indexed `capability`, `region` and `source_mode` for direct task discovery. Both versions remain readable; old records are not rewritten.

Common fields include schema/domain/chain/audience, event/task/actor/binding/subject IDs, assignment generation, parent entity key, recipe/reference digests, issue time and task deadline. Detached Ed25519 metadata includes the content digest, key ID, public key and signature. The public payload is canonicalized under the restricted string-only RFC8785-compatible profile. Python uses `rfc8785`; Python/Node signature vectors are tested.

Indexed fields are task ID, kind, actor ID and event ID; V2 additionally indexes scientific capability, broad region and source profile. No precise private AOI, question, source CSV or credential is stored in these entities. `source_mode=synthetic_demo` explicitly marks intentionally mocked input demonstrations.

| Kind | Writer | Meaning |
|---|---|---|
| task | Requester | Consented minimal task advertisement |
| claim | Provider | Provider-owned proposal; not authority to execute |
| assignment | Requester | Exclusive provider selection for a generation |
| result | Producer | Signed artifact digest/reference |
| review | Reviewer | Signed numerical review digest/reference |
| cancellation | Requester | Cancellation reference; not deletion of history |

Writes request readonly entities with a seven-day lifetime. Logical deadlines can be shorter. Old receipts are historical observations; a fresh read is required to claim current availability. Entity expiration is not a permanent research archive. Full private evidence remains off-chain.

A public schema tag is not an authenticated namespace. Consumers check pinned owners, service keys, binding versions and signatures. PostgreSQL serializes the requester's assignment selection; Arkiv is not a distributed mutex. Conflicting requester-signed selections halt acceptance rather than resolving by query order.

Implementation: `src/coordination.mjs`, `backend/src/orca/infrastructure/federation/public_events.py`, `signing.py`, `discovery.py`, and `backend/src/orca/network_worker.py`.

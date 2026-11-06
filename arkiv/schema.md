# ORCA Arkiv schema and ownership

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

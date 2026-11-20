# ORCA release ownership and data handling

Purpose: assign handover roles and required decisions for the research-federated release. References: P14.5; D-IMPL5 §§2,6; D-DB4 §§7–8; API design §§14,20.

| Role | Owns | Evidence before release |
| --- | --- | --- |
| Release owner | Selected scope, decision log, risk acceptance and cutover | Prerequisite gates, signed release certificate, named primary and backup |
| Platform on-call | Availability, scheduler, backup, worker fences and rollback | Alert receipt from primary and backup; isolated restore and staging rollback drill |
| Security and identity owner | Auth, RLS, key rotation, incident coordination | Live isolation, rotation drill, resolved or accepted findings |
| Privacy and data steward | Access, retention, data requests, processor and contract review | Approved class schedule and exceptions; isolated privacy and retention drill |
| Source and scientific owner | Source rights, pinned bytes, method and limitations | Source permits, signed method review, reference results |
| Federation and protocol owners | A2A services, ENS authority, public Arkiv scope and reconciliation | Named endpoint and wallet operators, scope approval and live receipts |

These are roles. Named primary and backup contacts and their acknowledgements are outstanding; keep the actual contact list restricted.

| Data class | Handling and release decision |
| --- | --- |
| Staging uploads and scratch/cache data | Clear only eligible unreferenced data after the approved grace period. Use the existing expired-upload cleanup. Verify a durable object is not referenced before removing it. |
| Private research evidence | Retain version and digest pins while an authorized claim references them, subject to the approved rights and retention schedule. If rights or retention remove bytes, record unavailable evidence and affected claims. |
| Review and reference exports | Authorize each request. Reference ZIPs contain a manifest only and use `private, no-store`. Expired source grants stop issuance. Previously downloaded copies and outstanding file links require separate handling. |
| Audit and backups | Restrict access; record policy version, holds, encryption/key custody, and expiry. Verify restores do not re-enable revoked access. Do not delete records under hold. |
| Public ENS / Arkiv data | Publish only approved public fields. Record chain-specific limits and expiry. Never promise that expiry removes copies held by observers. |

For a privacy request, the steward verifies identity and tenant, collects the permitted scope, checks restrictions and holds, and uses a private authenticated channel. Record what was disclosed or withheld and the basis, purging any temporary package under the approved schedule. A reference export is not a full data-subject export. An organization must approve its own request process and exercise it in staging.

Record the controller and processor roles, hosting regions, subprocessors, transfers and agreements with the responsible reviewer before live release. Keep third-party component notices with the shipped release and inventory research sources separately by license, attribution, access and redistribution rights. Stop transfers or publication where permissions are missing or withdrawn. This record does not sign a DPA, grant a source license, establish a production SLA, or certify the testnet as a production service.

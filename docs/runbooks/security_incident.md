# ORCA security incident response

This runbook covers the `research-federated` profile. The release operator coordinates containment, the affected institution owns its provider credentials and data decisions, and an independent reviewer records release closure. Capture UTC timestamps, affected scope, build revision, and an incident reference. Keep credentials, raw prompts, restricted observations, and participant identifiers out of shared evidence.

1. **Contain access.** Disable new dispatches or publication for affected bindings or writers. Remove affected grants, suspend exposed integrations, and preserve the core private workspace when its authorization boundary is unaffected. Use the [key rotation runbook](key_rotation.md) for each affected credential.
2. **Preserve evidence.** Record authorized audit event references, worker lease/generation, task IDs, deployment image identity, and approved public receipts. Keep restricted originals in the institution's incident store. Do not republish or blindly retry an uncertain Arkiv write. Use readback to establish its outcome.
3. **Check exposure.** Scope affected tenants, storage objects, operations, and event streams. Treat public-chain disclosures as already shared and verify the actual chain/projection state before describing containment. Evaluate notification obligations with the responsible institution.
4. **Verify recovery.** Check denied access with an old credential in a controlled environment, expected access with its replacement, tenant separation, and denied late results. Confirm that logs and new Arkiv intents contain only approved public fields. Record results without secret material.
5. **Close the incident.** Record the scope, rotation or revocation references, affected versions, release decision, residual risks, and reviewer signoff. A critical unmitigated finding, missing rotation evidence for a suspected exposure, or unresolved cross-tenant access blocks production designation.

## Release threat review

| Boundary | Evidence available | Closure required for release |
| --- | --- | --- |
| OIDC issuer and browser SSE | Local signed-token tests and a Chrome replay over loopback | Verify real issuer rotation, cache refresh across instances, authenticated replay, and live revocation or expiry against the selected deployment. |
| Tenant database and object access | Disposable PostgreSQL roles, forced RLS, cross-tenant denial and prior storage access checks | Review deployed role grants, pooler behavior, storage policy, backup access, and restore isolation. |
| Source and delegated service egress | Prior source connector, signed A2A and revoked-result checks | Confirm egress policy and failure behavior in the selected deployment and separately operated provider. |
| ENS controller binding | Local EOA signature recovery, untrusted signer rejection, and prior finalized-state checks | Verify configured contract capabilities. An ERC-1271 claim requires deployed support, positive and negative contract-signature evidence, and review. |
| Arkiv data disclosure | Strict public schema, durable outbox checks, and local projection tests | Inspect real selected-network records and receipts under approved publication authority. Review permitted fields for data minimization. |
| Dependency and build integrity | Locked Python and npm advisory checks, tested local image digests, a Debian advisory inventory, and limited tracked credential-pattern search | Resolve or document applicable advisory matches. Verify deployed image digests, review CI dependencies and injected secrets, and obtain independent signoff. |

See the [P14 security audit evidence](../gates/P14/security-audit.json) for observed results and remaining acceptance.

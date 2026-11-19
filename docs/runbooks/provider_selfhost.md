# Self-host an institutional provider

Use the provider operator's host and private storage. The supplied Compose service binds to host loopback. Route its Agent Card and A2A traffic through an operator-controlled HTTPS proxy. The directory stores the provider key, approved inputs and its task journal. Keep coordinator signing keys, Arkiv keys and any other institution's storage credentials outside the provider.

## Start and register

From the repository root, build the service and initialize its private volume once. Use the public HTTPS endpoint that the coordinator will pin.

```powershell
docker compose -f providers/institutional/compose.yaml build
docker compose -f providers/institutional/compose.yaml run --rm provider backend/.venv/bin/python -m orca.institution_provider init --directory /var/data/provider --endpoint https://provider.example/a2a
docker compose -f providers/institutional/compose.yaml up -d
```

Register the public descriptor from `describe`. Keep `provider.json` on the operator's host. Complete the existing key challenge and endpoint qualification for the exact public URL and capability.

## Approve research release locally

Configure the current binding and exact local plan as described in [Provider research readback](../gates/P09/provider-readback.txt). The plan pins the organization ID, project ID and ordered snapshot IDs. Record the local source license and operator's release decision under the rights record before approval. Retain that record on the provider host.

Create `research-policy.json` in the provider's private directory. The structure below shows placeholders. Replace the keys and values with the actual approved digests and sample threshold.

```json
{
  "schema": "orca.provider-release.v1",
  "plans": {
    "<approved-plan-digest>": {
      "rights_record_digest": "<local-approval-record-digest>",
      "min_samples": 3
    }
  }
}
```

Use `sha256:` followed by 64 lowercase hex characters for digests. Choose a minimum approved for the data and use case. The service supports thresholds from 2 through 1,000. A minimum cohort size alone provides no privacy guarantee. Review whether repeated queries, counts, averages or the input digest could disclose restricted values before approving a plan.

Missing, malformed or empty policies prevent research result release. To revoke a plan at the provider, publish an updated policy file with the plan removed. The provider rechecks it for new tasks, before persisting an output, and on task readback. Also revoke the coordinator's project authorization to stop new dispatch and acceptance. Previously released outputs follow the project's separate retention and incident response rules.

The coordinator sends a signed grant referencing the approved IDs. The operator-approved temperature values stay on the provider host. The current A2A summary carries its count, mean and digest. Public Arkiv result references carry only the approved identifiers, status, method and evidence digest.

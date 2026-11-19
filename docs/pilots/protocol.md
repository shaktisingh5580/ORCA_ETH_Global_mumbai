# P13.1 controlled research pilot

Compare an existing research script, a general-purpose agent, and ORCA on the same frozen cases. The replay tool scores the collected outputs. Run each workflow in its own approved environment and export one record per workflow and case.

## Freeze before collecting results

- Select bounded scientific cases and a common input package for each. Record the exact bytes, units, acceptable numerical error and permitted access in the case manifest. Include cases where the available evidence requires abstention.
- Freeze the actual workflow configurations. Record script versions, model/tool settings, and ORCA workflow versions separately. Keep case inputs identical across workflows. Record case order and assignment of operators before collection.
- Have a separate reference custodian produce and retain the held-out answers and provenance. Record its digest before any pilot run. Use a null reference value only where an independent review has determined that the expected response is abstention.
- Register the exact protocol digest separately before collecting outputs. Confirm the supplied case digests against the approved input packages and the workflow digests against the configured versions.

## Replay inputs

| File | Required content |
| --- | --- |
| Protocol | `schema: orca.pilot.protocol.v1`, `mode` of `rehearsal` or `controlled_pilot`, `reference_digest`, `workflow_digests` keyed by `scripts`, `general_agent` and `orca`, and `cases` with `id`, `input_digest`, `unit` and `tolerance`. |
| Run bundle | `schema: orca.pilot.runs.v1`, `protocol_digest`, and `runs` containing `case_id`, `arm`, pseudonymous `operator`, `input_digest`, `workflow_digest`, `outcome`, active `researcher_seconds` and `correction_seconds`. Use `prediction` for `answered`. Use `abstained` or `failed` for other outcomes. Optionally include `output_digest` and `replay_digest` for independently rerun outputs. |
| Reference | `schema: orca.pilot.reference.v1`, `kind` of `held_out` or `synthetic`, pseudonymous `custodian`, `provenance_digest`, and `expected` keyed by case ID. Each value is a reference number or null for an expected abstention. |

Use `sha256:` followed by the lowercase SHA-256 hex digest of the exact file bytes for digest fields. Pseudonyms and case IDs should be short opaque labels. During execution, collect active researcher time and correction time using the same rules for all arms. Retain unedited source outputs and replay artifacts privately.

```powershell
$digest = (Get-Content -Raw -LiteralPath .\registered-protocol.digest).Trim()
$orcaPython = '.\backend\.venv\Scripts\python.exe'
& $orcaPython backend/scripts/pilot_replay.py --protocol .\protocol.json --protocol-digest $digest --runs .\runs.json score --reference .\reference.json
& $orcaPython backend/scripts/pilot_replay.py --protocol .\protocol.json --protocol-digest $digest --runs .\runs.json blind --packet .\rater-packet.json --private-key-map .\private\key-map.json
```

Use the independently registered digest in the evaluation. Send only the packet to the rater before rating. Keep the private key map in a restricted directory, including an appropriately restricted ACL on Windows. The packet uses a randomized token and order and omits workflow names, operators, timings and answer keys. Record when an output, format, or participant could reveal its origin. Keep rubric decisions and discussion of any reveal with the study record.

## Interpretation

Valid outcomes include answers within the preregistered tolerance and correct abstentions. Report all cases, including failed and abstained runs. Numerical error metrics use the same paired answered cases across all workflows and are grouped by unit. Report coverage alongside errors. Replay counts compare the reported original and rerun digests. Verify the underlying artifact bytes separately.

Results describe the frozen cases and measured workflows. Synthetic data is restricted to rehearsal and G13 requires actual approved observations, independent review and selected prerequisite acceptance. Retain the consent, reference provenance, raw measurements and redacted report as separate study records.

[Original scientific provenance requirements](https://teampflresearch.vercel.app/scientific.html#49-provenance-requirements)

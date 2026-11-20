# ORCA sponsor evidence index

| What to show | Evidence | What it establishes |
| --- | --- | --- |
| Real-source prototype result | [Recorded NOAA run](../gates/HACKATHON/H02-live.json) | The operator path ran on real inputs in the January 2025 North Arabian Sea study. It is not signed-in browser acceptance or independent scientific validation. |
| ENS identity guard | [Finalized observation and setup receipts](../gates/P14/sponsor-proof.json), [ENS gate record](../gates/P10/progress.json), [reader](../../arkiv/src/ens.mjs) | Setup transactions succeeded on Sepolia. The observed analyst name lacks all five service records; delegated permission, affiliation, and authority are not verified. |
| Shared Arkiv coordination | [Historical H04 chain trace](../gates/HACKATHON/H04-live-coordination.json), [Rabdi result transaction](https://rabdi.explorer.arkiv.network/tx/0x869dec7e45e226548c2e65b07eb46b255a891302e90fcf50f7f8abaec4128afd), [current Arkiv gate](../gates/P11/progress.json) | Separate team wallets posted claims, assignments, and result/review references. A fresh query for the historical task yielded zero active results. The historical rehearsal does not establish current P11 lifecycle acceptance. |
| Read without ORCA | [Historical direct readback](../gates/HACKATHON/H04-standalone-verification.json), [standalone verifier](../../verifier/verify-arkiv.mjs) | Eight public coordination records were read without ORCA’s API or database. A fresh v1 result with authorized canonical bytes is still needed for current offline-verifier acceptance. |
| Reviewer challenges a result | [Synthetic rehearsal evidence](../gates/HACKATHON/collaboration-simulation.json), [source-label regression](../../backend/tests/e2e/test_sponsor_demo_labels.py) | The synthetic rehearsal illustrates coordination behavior. Keep it visibly separate from the real NOAA study. |

The recorded workspace result concerns a bounded January surface-temperature comparison with a January 2003–2022 baseline. Disclose the bounded source and study dates; do not expose the underlying private workspace or full datasets. The named provider and reviewer in the H04 trace were team operated. A transaction, returned digest, or separate wallet does not certify institutional independence or scientific correctness.

Fresh signed-in browser and API response screenshots are outstanding. See the [demo runbook](demo_runbook.md) for the recording sequence and [G14](../gates/P14/manifest.json) for remaining acceptance work.

# ORCA sponsor demo runbook

Use the [evidence index](evidence_index.md) to choose what to show. Open the Research result and the saved ENS and Arkiv checks before recording. Keep any real dataset download, private investigation details, credentials, or unpublished artifact bytes off screen.

| Time | Show | Say |
| --- | --- | --- |
| 0:00–0:30 | Research workspace | “Research teams can share a result in seconds. Figuring out what went into it, who checked it, and whether you can reproduce it takes much longer. ORCA gives teams one place to follow that trail.” |
| 0:30–1:30 | Saved real-source run, approved plan, result and limitations | “Here is a January 2025 sea-surface temperature comparison for one small area of the North Arabian Sea. ORCA keeps the plan and source evidence with the calculation. This recorded prototype run found a difference of about 0.62 °C against its January baseline. That describes this comparison; it is not a regional climate conclusion or an independently validated method.” |
| 1:30–2:20 | Agent onboarding, then finalized ENS observation | “If another institution offers an agent, we need a way to tell that its service matches the name it claims. ORCA checks the public records behind that name before it can trust that link. Here the analyst name exists, but the service records are empty, so ORCA keeps research dispatch disabled.” |
| 2:20–3:25 | Historical Arkiv task-to-result trace, then fresh query | “Arkiv is the shared noticeboard. The requester posts a task, a separately keyed provider claims it, the requester assigns it, and the provider posts a reference to its result. An outside reader can check the public references without an ORCA login. The old records expired, so this fresh query is empty. Here is what the verifier observed while they were available.” |
| 3:25–4:00 | Review and evidence index | “The original data stays private. A public reference can show that a result matches the bytes a team chose to share. It cannot tell you whether the science is right. ORCA keeps those two questions separate so another team knows what it can verify and what it still needs to review.” |

If you show the intentionally biased rehearsal, label the slide and narration **Synthetic demo**. Use it to show a reviewer challenging a result and a correction being recorded. Do not display rehearsal outputs as part of the real-source result. If the browser, science source, or chain read is unavailable, present the corresponding dated evidence and say which check you could not repeat.

Run these read-only commands from the repository root. The filtered-query arguments are record kind, task ID, and expected owner, in that order. Use the authorized public RPC environment if needed; keep its access key out of the recording.

```powershell
node arkiv/src/ens.mjs inspect analyst.orca-research-lab.eth
node arkiv/src/public-client.mjs query-task task 84410849-a102-4269-b52f-d7d3333ec626 0x8cBFE2bD01C7FDfc500e62A1cF99A01B6A11bCBe
```

For a **fresh** approved public result, use a trusted public intent file, the recorded entity key and creation transaction, and separately authorized canonical bytes. Compare the chain reference and bytes independently; an absent or expired record cannot verify current availability. The CLI requires no ORCA API or database configuration.

```powershell
node verifier/verify-arkiv.mjs public-intent.json '<entity-key>' --tx '<transaction-hash>' --canonical-artifact public-artifact.bin
```

The opt-in P14.4 check uses `ORCA_SPONSOR_LIVE_EVIDENCE` to locate a JSON file with `ens: {name, siblingName, delegate, grantTx, revokeTx}` and `arkiv: {intent, entityKey, txHash, canonicalArtifact}`. The artifact path is relative to that JSON file. Use separately authorized genuine testnet evidence; keep tokens and private keys out of the packet. Run `node --test arkiv/tests/sponsor-live.test.mjs`. The tests perform readback only; missing sections explicitly skip their live cases.

Before claiming fresh integrated proof, capture authenticated API or dashboard responses for the correct profile, include the relevant chain URLs and finalized observations, and record the scope of the source and project disclosures. Include the actual delegated write and sibling denial if claiming those behaviors. Any prize eligibility depends on the applicable official rules.

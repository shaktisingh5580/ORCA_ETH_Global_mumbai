# ORCA P13.4 failure taxonomy

Record the failure class, observable state, and next action for each pilot trial. Keep raw source text, provider replies, credentials, and participant details out of the shared report. The trial evidence records outcomes from local source, model, and transport fixtures and disposable PostgreSQL.

| Failure | What ORCA observes | What to do |
| --- | --- | --- |
| Provider changes the temperature units | The source response disagrees with the pinned metadata. The acquisition adapter raises `SOURCE_VALIDATION_FAILED` and returns no data to publish. | Compare the provider metadata and response. If the source legitimately changed, qualify and register the new version before retrying. |
| Requested depth differs from the pinned source | The adapter raises `SOURCE_VALIDATION_FAILED` before contacting the source. | Select a verified source at the requested depth or revise the analysis scope. |
| A source was unavailable by the requested cutoff | A retrieval after the cutoff or missing availability evidence raises `SOURCE_VALIDATION_FAILED` before contacting the source. | Find a preserved version with documented availability by the cutoff. Keep the historical analysis blocked if the evidence is missing. |
| Source text attempts to direct the planner | The planner can inspect the text as data. The gateway rejects a request for an unapproved tool, and the numerical preview rejects the poisoned observation. A supported proposal cannot use that failed preview. | Inspect and correct the source. Request a new proposal after a valid preview. |
| Authorization is revoked while a provider is computing | A correctly signed response arrives after revocation. The worker records `failed` with `AUTHORITY_OR_ARTIFACT_CHANGED`, and the result digest and verified response remain empty. Readback remains unresolved after revocation without contacting the provider. | Inspect the authorization and dispatch history. Any further delegation requires a current authorization and a new authorized request. |
| A reviewer claims disagreement with a matching result | The deterministic comparison returns no findings. The review gateway rejects the unsupported challenge and the coordinator's proposed corrective execution. | Keep the original result and record the grounded decision. Reopen the question only when new evidence warrants review. |
| A provider reports a conflicting value and the reviewer claims agreement | The comparison records a numerical mismatch against the approved evidence. The review gateway rejects both the unsupported agreement and a proposal to dismiss the mismatch. | Preserve the original and competing claim. Review the inputs and tolerance, then approve a separate recomputation if appropriate. |

An agreement verdict establishes what the comparison found for the supplied evidence. Independent numerical verification requires a separately established reference calculation and recorded provenance. Independence of an institution or operator requires separate evidence. Multiple agreeing model or agent outputs leave both questions open.

Use the [P13.4 trial evidence](../gates/P13/failure-trials.json) for exact cases and results. Repeat these trials with separately operated services and approved real pilot data before drawing institutional readiness conclusions.

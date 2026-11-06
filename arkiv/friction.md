# Arkiv feedback from implementing ORCA

These are engineering observations, not claims of confirmed Arkiv defects.

1. **Uncertain-send recovery needs first-class examples.** Our application must persist an intent before a chain call. A timeout may occur before `createEntity` returns a transaction hash. We implemented predicted entity keys, durable pre-send reservations and writer locks; a read-back reconciliation path would be easier to adopt with a complete official example covering process crashes and nonce changes. Reproduction: reserve an event, interrupt the caller around submission/confirmation, then restart without blindly creating another entity. See the coordination adapter and recovery script.
2. **Separate keyed reads from query visibility in examples.** Our providers check both an entity by key and its presence in a filtered task board. Documentation should clearly explain any indexing/visibility timing differences and bounded retry recommendations. We do not infer a network guarantee from one successful read.
3. **Schema evolution deserves a migration recipe.** We retained V1 records while adding V2 capability/source-profile indexes. An example showing multi-version reads, typed schema mismatches and deduplication would help applications evolve without rewriting signed history.
4. **Owner/signature filtering is application work.** The SDK makes writing a schema straightforward; an application must still reject strangers reusing that schema tag. A concise signed-record example with explicit wallet/service binding boundaries would help developers avoid treating a schema name as authentication.

Observed network/SDK and record evidence are in `contracts/deployments/agent-demo-rabdi.json` and `docs/gates/HACKATHON/`. We have not run a formal latency or throughput benchmark and do not advertise one.

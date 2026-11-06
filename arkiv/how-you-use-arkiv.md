# Actual Arkiv integration

The H04 live rehearsal published task → provider claim → requester assignment → provider result, followed by another task → reviewer claim → assignment → review. Requester, producer and reviewer use three dedicated Rabdi wallet addresses listed in the root README.

The application does real reads: providers read the requester-owned advertisement/assignment and query the board before executing; the coordinator verifies the provider's result reference; an authenticated timeline can refresh from Arkiv; the standalone verifier reads retained entity keys without the ORCA API/database.

V2 adds capability, broad-region and real/synthetic source-profile indexes. The provider discovery CLI reads Arkiv directly and can publish its own claim, which the requester explicitly imports/selects. Assignment is still an authorized requester decision; discovering a public record does not grant data access.

Files: `src/coordination.mjs`; Python `agent_discovery.py`, `network_worker.py`, `infrastructure/federation/*`; scripts `verify_agent_evidence.py`, `reconcile_agent_operation.py`, and `rehearse_agent_coordination.py`.

The UI should show real owner addresses, entity keys, expiry, a query/verification timestamp, source labels and the separation between transport completion and numerical verdict. Fresh-query failure must not display a fabricated verified state. A backend receipt does not establish a completed browser demonstration: frontend acceptance has its own gate.

The full real-data evidence is `docs/gates/HACKATHON/H04-live-coordination.json`. The independent API-free read-back evidence is `H04-standalone-verification.json`. Later-phase gate files state whether their code, live integration or deployment actually passed. These files are inspectable claims, not a substitute for a live query.

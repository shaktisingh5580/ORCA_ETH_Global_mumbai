# Why Arkiv is part of ORCA

ORCA keeps authentication, project permissions, private research context, bulk source captures and durable scientific records in Supabase/private storage. Those are not uploaded to a public chain.

Arkiv supplies the public collaboration board: task advertisements, separately wallet-owned claims, requester assignments and provider result/review references. A provider can query the board by capability and publish its own claim. A verifier can read public records without ORCA's API or database. Task/claim ownership and expiry affect actual discovery and execution checks.

Replacing this board with an ORCA-only Postgres API would make public discovery and history inspection dependent on ORCA's service and access policy. Another signed replicated protocol could provide similar properties, but we would have to build and operate it. We do not claim collaboration is impossible without Arkiv.

Arkiv does not run scientific computations, establish institutional identity, validate an SST method or supply a global assignment mutex. Those boundaries are explicit. Its public records bind identities and evidence digests; numerical methods and separately checked source bytes establish what comparison was actually computed.

The seven-day lifetime is deliberate for transient coordination. Long-term private research evidence stays in the durable data layer, and fresh chain verification distinguishes live availability from a saved historical receipt.

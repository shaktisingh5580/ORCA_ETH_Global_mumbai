# Repository working agreement

## Phase delivery and GitHub history

The user explicitly requests the following workflow for this repository:

- For each implementation phase, organize its subphase work into 5–10 meaningful commits across that phase. Keep unrelated subphases separate where practical; do not create empty commits to meet a quota.
- Before declaring a phase complete, run its mandatory tests, required prior regressions and gate checks. Development commits may be pushed before full phase acceptance when relevant local checks pass and outstanding requirements are recorded explicitly.
- Commit and push completed phase/subphase work to `https://github.com/shaktisingh5580/ORCA_ETH_Global_mumbai.git`. The user has authorized these routine pushes; do not ask again unless scope, destination or a destructive action changes.
- Use normal pushes. Never force-push or rewrite published history without explicit authorization.
- Exclude credentials, local environments, caches and build outputs. The user also excludes root design/research Markdown and the reference copy in `docs/sources/` from Git. Include code, tests, generated contracts and machine-readable gate evidence.
- Preserve local registered source documents byte-for-byte; their checksums are part of local phase gates. `.gitattributes` disables automatic newline conversion for this reason. CI can run code checks but must not claim a full gate when uncommitted source references are absent.

## Implementation requirements

Use `main_implementation plan.md` and `api design.md`, reconciled through `docs/adr/` and `docs/decisions/`, to determine phase scope. Every authored code file must explain its purpose, behavior, reference documents and phase at the top. Synthetic fixtures and the frontend mock server belong only in test tooling, never in production runtime paths or release packages.

## Development sequencing and deferred Oracle deployment

The user's latest instruction supersedes strict gate-by-gate development sequencing:

- Continue implementation through P05, P06, P07 and the remaining main-plan phases while earlier acceptance items remain open. Build required dependencies as they are needed; record missing dependencies and unfinished work honestly.
- Development authorization does not mark a gate PASS, approve a scientific method, or establish production readiness. Complete the outstanding acceptance requirements before claiming the corresponding phase or release is complete.
- Prioritize implementation. Run focused checks for changed behavior and affected security, isolation and scientific calculations. Reuse recorded evidence where applicable; do not repeatedly run unchanged broad suites without a new change or failure that warrants it. Consolidate full gate verification at completion milestones.
- Defer Oracle deployment, remote worker setup, production migrations and cloud recovery acceptance until the system build is ready and the user resumes deployment work. Record these items as deferred, not passed. They must not block local implementation or reviewed GitHub development pushes.
- Continue normal GitHub pushes of meaningful, locally checked implementation increments to the authorized repository. Label incomplete phase work as development progress, with remaining checks recorded. Do not wait for Oracle acceptance to push code.
- Resume with P05 durable planning, approval, jobs, recovery and progress events, integrating the existing P03/P04 modules. Then proceed through P06 evidence/report/export and P07 reproduction/review, retaining the full P00–P14 scope.

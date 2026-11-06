# Repository working agreement

## Phase delivery and GitHub history

The user explicitly requests the following workflow for this repository:

- For each implementation phase, organize its subphase work into 5–10 meaningful commits across that phase. Keep unrelated subphases separate where practical; do not create empty commits to meet a quota.
- Run the phase's mandatory tests, required prior regressions and gate checks before declaring the phase complete or pushing its completed work.
- Commit and push completed phase/subphase work to `https://github.com/shaktisingh5580/ORCA_ETH_Global_mumbai.git`. The user has authorized these routine pushes; do not ask again unless scope, destination or a destructive action changes.
- Use normal pushes. Never force-push or rewrite published history without explicit authorization.
- Exclude credentials, local environments, caches and build outputs. The user also excludes root design/research Markdown and the reference copy in `docs/sources/` from Git. Include code, tests, generated contracts and machine-readable gate evidence.
- Preserve local registered source documents byte-for-byte; their checksums are part of local phase gates. `.gitattributes` disables automatic newline conversion for this reason. CI can run code checks but must not claim a full gate when uncommitted source references are absent.

## Implementation requirements

Use `main_implementation plan.md` and `api design.md`, reconciled through `docs/adr/` and `docs/decisions/`, to determine phase scope. Do not advance to the next phase until the current gate passes. Every authored code file must explain its purpose, behavior, reference documents and phase at the top. Synthetic fixtures and the frontend mock server belong only in test tooling, never in production runtime paths or release packages.

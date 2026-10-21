# Security scope

This runtime is for trusted local plans, tools, operators and storage. It executes
adapter code with the process user's permissions. It is not a sandbox. Logs contain
arguments and results in plaintext. Do not put secrets in plans or evidence notes.
The event hash chain is an accidental corruption check, not authentication; valid
prefix truncation and an attacker who rewrites hashes are not detected.

Report reproducible problems through a GitHub issue without secrets or private data.
No independent security audit, penetration test or human review is claimed.

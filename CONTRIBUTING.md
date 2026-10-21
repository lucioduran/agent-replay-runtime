# Contributing

Use Python 3.11 or newer. Install requirements-dev.txt in a local virtual environment.
Run the README testing commands before proposing changes. Add regression tests for
new behavior and include actual subprocess failures when changing recovery semantics.
Keep adapter contracts and durability limitations in docs/design.md. Do not claim
exactly-once execution from a retry test. Never store credentials or customer data.

The fixed crash matrix is small, serial and free. Large load tests in the author's
multi-project workspace must use its shared benchmark_lock.py wrapper. Other users
should serialize resource-heavy measurements on their own hosts.

Evidence changes require a fresh reproduction after source changes. Source hashes
cover runtime and experiment modules; the recorded revision identifies the clean
source commit before committing generated results. CI replays evidence and verifies
those hashes. It also independently executes real process crash tests on three OSes.

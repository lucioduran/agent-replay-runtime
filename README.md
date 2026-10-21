# Agent Replay Runtime

[![CI](https://github.com/lucioduran/agent-replay-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/lucioduran/agent-replay-runtime/actions/workflows/ci.yml)

A local event-sourced runtime for resuming tool execution after a process crash.

## Problem

A tool can commit an effect just before its caller dies. The caller then has no
acknowledgment and cannot safely infer whether retrying will duplicate the effect.
This repository makes that interval reproducible and records how it is reconciled.

## Scope

Project 03 of the engineering evidence series. A Python library and CLI execute
finite ordered JSON plans, persist requests and results, replay history, preserve
idempotency keys, and pause uncertain calls to tools without idempotency support.
The included SQLite ledger is an actual independent local effect store. There are
no runtime dependencies, model calls, hosted services, credentials or paid APIs.
Planning and inference are outside scope; an agent can submit its recorded decisions.

## Quick start

Use Python 3.11 or newer. The published local evidence uses Python 3.11.13; CI uses
3.11.9 on Linux, macOS and Windows.

```sh
git clone https://github.com/lucioduran/agent-replay-runtime.git
cd agent-replay-runtime
python -m agent_replay --db runtime.sqlite --ledger effects.sqlite submit demo examples/plan.json
python -m agent_replay --db runtime.sqlite --ledger effects.sqlite resume demo
python -m agent_replay --db runtime.sqlite --ledger effects.sqlite trace demo
```

Repeating `resume demo` returns the recorded results without dispatching completed
steps. Keep both databases. Paths bind the demo adapter to its effect store.

## Usage

Each step has a unique `id`, a versioned `tool`, object `args`, and boolean
`idempotent`. The CLI provides `ledger.v1` and `unsafe-ledger.v1`. The latter requires
`idempotent: false`. Reusing a run ID with a changed plan or adapter is rejected.
Run IDs must be unique across runtime databases sharing an adapter's receipt namespace.
Use UUIDs in applications. The library supports your own adapters:

```python
from pathlib import Path
from agent_replay import Runtime
from agent_replay.ledger import LedgerTool

runtime = Runtime(Path("runtime.sqlite"), {"ledger.v1": LedgerTool(Path("effects.sqlite"))})
runtime.submit("unique-run-id", [
    {"id": "save", "tool": "ledger.v1", "args": {"message": "Hello"}, "idempotent": True}
])
state = runtime.resume("unique-run-id")
print(state.results)
```

Implement the `Tool` protocol with a stable `identity`, `idempotent` boolean and
`invoke(key, args)` returning finite JSON. See [the adapter contract](docs/design.md).
An exception after dispatch leaves the request pending. Inspect the log and external
system before resolving an unsafe request:

```sh
python -m agent_replay --db runtime.sqlite --ledger effects.sqlite inspect demo
python -m agent_replay --db runtime.sqlite --ledger effects.sqlite reconcile RUN_ID --result '{"effect_id":1,"payload":{}}' --evidence 'Verified the external receipt and payload'
```

The reconciliation example is a template, not proof for any run. Use the actual result
from the effect system. It records an operator assertion and skips dispatch. Then
resume the run. Do not resolve absent effects with invented receipts.

CLI exit codes: `0` success, `2` input/storage error, `3` uncertain effect, `4` busy
writer. A process-lifetime file lock excludes overlapping writers. Do not delete it.

## Architecture

```text
Persist plan -> commit request -> invoke external tool -> commit result -> next step
                       |                  |
                       +-- crash window --+
                                  |
                    replay pending request
                       /                
             idempotent adapter       unsafe adapter
             retry same key           stop for evidence
```

The event stream is the state source. Replay validates transitions and hashes without
calling a tool. Adapter identity and capability are pinned in the submitted plan.
SQLite WAL with synchronous FULL persists each event transaction. The independent
ledger atomically stores idempotency receipts and effects. Design and operational
limits are in [docs/design.md](docs/design.md).

## Reproduction

```sh
python -m experiments.reproduce --output work/crash-matrix.json
python -m experiments.verify work/crash-matrix.json
```

The parent waits for a flushed checkpoint message and kills the child process:
SIGKILL on POSIX, TerminateProcess on Windows. It restarts a fresh worker and reads
the separate ledger. The matrix covers three interruption points for each adapter:
after request commit, after effect commit, and after acknowledgment commit.
The unsafe baseline blindly repeats an effect after a lost acknowledgment. The safe
and unsafe cases use the same payload. An uninterrupted control checks one effect.

In the author's shared workspace, reproduction is run under the exclusive wrapper:

```sh
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 python3 /Users/luchoduran/Sites/open-source-projects/.coordination/benchmark_lock.py .venv/bin/python -m experiments.reproduce --output results/crash-matrix.json
```

[Raw evidence](results/crash-matrix.json) includes full before/after event streams,
effect rows, killed process statuses, reconciliation notes, source fingerprints,
revision, UTC timestamp, hardware and versions. Temporary paths, platform termination
codes and metadata vary across reproductions, so full report bytes are not expected
to match. All effect payloads are original synthetic fixtures under MIT. No random
seed, training split, model, API call or performance timing is involved.

## Results

The recorded fixed matrix observes:

| Case | Final effects | Outcome |
| --- | ---: | --- |
| Idempotent: after request | 1 | Same key resumed successfully |
| Idempotent: after effect | 1 | Existing receipt returned |
| Idempotent: after acknowledgment | 1 | Recorded result replayed |
| Unsafe: after request | 0 | Paused, unresolved |
| Unsafe: after effect | 1 | Paused, then resolved from ledger evidence |
| Unsafe: after acknowledgment | 1 | Recorded result replayed |
| Unsafe blind-retry baseline | 2 | Duplicate effect reproduced |

These are six fixed crash cases and one negative baseline, not a statistical reliability
estimate. The local recording uses an Intel Xeon W-3223, 16 logical CPUs and 48 GiB RAM;
workers execute serially with one thread. See raw metadata for OS and SQLite versions.
The reconciliation is automated fixture inspection, not a human review.

## Testing

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m coverage run -m unittest discover -v
python -m coverage report
python -m experiments.verify results/crash-matrix.json
python -m build
```

On Windows activate with `.venv\Scripts\Activate.ps1`. Tests cover real subprocess
crashes, writer exclusion, concurrent receipt reuse, lost responses, repeated retries,
unsafe reconciliation, malformed plans, changed identities, invalid JSON, corrupt
history, namespace separation, CLI behavior, and replay consistency. CI enforces 90%
combined statement/branch coverage. Tool versions and action commits are pinned.

## Limitations

- This is not a universal exactly-once system. Duplicate prevention depends on the
  external adapter's atomic idempotency contract and receipt retention.
- A process crash is tested; power failure, filesystem corruption, remote providers,
  partitions, expiring receipts and database restore divergence are not tested.
- A finite sequential plan is supported. There is no dynamic branching, scheduler,
  retry backoff, distributed lock, compensation or built-in model integration.
- One writer owns the whole runtime database while executing tools. A hung tool
  blocks progress until its process exits. Use trusted local filesystems only.
- History replay is linear; repeated append validation makes long runs quadratic.
  No throughput or scale claim is made. Logs are unbounded plaintext.
- Hashes detect accidental edits, not malicious rewrites or valid-prefix truncation.
- Manual resolution accepts an operator's assertion. It cannot verify external truth.
- Changing adapter code while retaining its identity can violate the contract. Version
  identities and use unique run IDs across stores sharing external receipt namespaces.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Include regression
tests and update the design contract when changing recovery semantics.

## License

Code, documentation, original fixtures and recorded evidence use the [MIT license](LICENSE).
Python and development tools retain their upstream licenses.

# Runtime contract

Version 1 executes a finite, ordered JSON plan. Planning is outside the runtime:
record model decisions before submission. No inference or network access is required.
A plan contains unique step IDs, versioned tool names, JSON arguments and the tool's
idempotency capability. Changing a submitted plan is rejected.

SQLite stores the complete event stream. Every request is committed before tool
invocation. Every result is committed before progressing. Replay validates event
order, hashes, plan identity and step transitions, then reconstructs progress without
calling tools. A hash chain detects accidental edits, not malicious rewriting.

A process-held file lock serializes all mutations to one runtime database, including
reconciliation. OS process death releases the lock. There are no time-based leases,
so a paused worker cannot lose ownership and later execute stale work. A stuck tool
blocks that database until its process exits. Local filesystems only; do not remove
the lock file, use aliases/hard links, or replace a live database. Use the same
canonical database path in every process. The lock file persists intentionally.

# The uncertain interval

A crash after request commit and before result commit leaves a pending request.
For an idempotent adapter the same stable key and arguments are retried. The adapter
must atomically bind the key to its effect and original result, reject key reuse
with different arguments, retain the receipt, and handle concurrent requests.
A capability flag is a contract supplied by the integrator, not a verified fact.

An unsafe adapter is never retried automatically after a pending request. An operator
must supply an externally verified result and a nonempty evidence note. This records
a resolution and skips dispatch. The runtime cannot verify the note or prove an effect
occurred. If the effect did not occur, submit a new run only after external investigation.
Never acknowledge an uncertain effect just to unblock a run.

A successful call with an unencodable result or an exception is also uncertain. It
leaves the request pending. Adapters must return finite JSON data. Null is a valid
result. There is no universal exactly-once guarantee, distributed transaction, rollback,
automatic compensation, idempotency expiration management, or remote fencing.

# Demonstration adapter

The SQLite ledger is an independent effect store. For its idempotent operation, one
transaction inserts the receipt and effect. The unsafe operation intentionally ignores
keys and inserts on every call. Both use the same payload and effect representation.
Its identity includes the canonical ledger path to reject accidentally resuming against
a different store. Real adapters should bind their identity to endpoint, account,
operation version, and receipt namespace. Credentials must never be logged.

SQLite uses WAL and synchronous FULL. Tests kill processes, not disks or power. These
settings do not establish power-loss durability on every operating system or storage
stack. Restoring a runtime log without the corresponding external receipts can duplicate
effects. Backups, disk corruption recovery, secrets, access controls, encryption,
untrusted input size limits, scheduling and multi-host execution are outside scope.

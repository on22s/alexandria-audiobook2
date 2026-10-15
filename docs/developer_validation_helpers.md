# Developer validation helpers

`app/experiments/validation_helpers.py` preserves reusable checks developed while
testing ownership and input provenance. These are opt-in building blocks for
development harnesses. Application dispatch and settings remain unchanged.

Run the synthetic regressions from `app/` with the project's Python environment:

```sh
python -m unittest tests.test_validation_helpers
```

The tests use fake process handles and tiny temporary files. They require no
model, GPU, network or evaluation dataset. Static file-binding tests require
POSIX paths and symlinks and are explicitly skipped on other platforms; process
registration and receipt tests are portable.

## Retain a handle before dispatch

```python
from experiments.validation_helpers import (
    get_process_holder, start_registered_process,
)

custody, processes = [], []
fake_handle = object()
handle = start_registered_process(
    custody, processes, lambda command: fake_handle, ["synthetic"]
)
holder = get_process_holder(custody, handle)
assert holder["process"] is handle
```

The pending holder exists before the constructor runs. A constructor exception
keeps the pending unknown outcome; failure to append to the secondary process
list keeps the returned handle in its original holder. Lookup uses object
identity and rejects missing or duplicate holders.

The registries are intentionally shared mutable state. The caller must retain
the holder until its existing native cleanup proves retirement. These helpers
do not stop, poll, signal, retry, release custody or supply a timeout. A pending
holder with no handle does not establish that no child was created.
Prepare the caller's passive custody-hold capability before dispatch so a
secondary allocation failure cannot make unknown ownership escape that hold.

## Validate returned retirement evidence

`is_retirement_evidence_valid(receipt, worker_count)` checks these fields:

- `owned`: ordered worker records followed by one server record, each with a
  distinct positive integer `pid`, exact integer `returncode` and
  `native_retired: true`.
- `cleanup_dispatch_PIDs`: the same distinct PID population, in any order.
- `custody_retained_PIDs`: an empty list.

Worker exit codes must be zero. The server's exit code may be an integer signal
exit such as `-15`. Boolean PIDs or return codes, omitted records, duplicate or
foreign dispatches, failed workers and retained custody refuse validation.
An explicit `worker_count=0` requires one returned server record.

This checks the shape and consistency of returned evidence. It cannot establish
that the operating system retired a process, that cleanup occurred only once,
or that a workload succeeded. The harness still needs its original native
ownership, source, deadline, resource-cap and completion checks.

## Bind static POSIX runtime files

Build the runtime digest map prospectively from trusted inputs. For one declared
relative symlink, `get_alias_ledger(runtime, alias_path)` reads its literal link
text, canonical target and inode identities. Add the physical target and its
expected digest to the caller's frozen file map.

`get_provenance_observation(runtime, files, ledger)` hashes each literal file-map
key once with the existing `experiments.provenance.file_sha256` helper. It checks
runtime identities and link text before and after hashing. Both the literal
alias and physical target must match their expected content digests;
`runtime_only=True` still checks both. The bounds remain 13 runtime keys and
one declared alias. Undeclared links, link chains, replaced inodes and malformed
bindings refuse. Inputs are read without mutation.

The returned `runtime_unchanged` flag must be checked explicitly. Malformed
bindings and changed identities raise; a validly shaped observation containing
changed content reports `runtime_unchanged=False`. Compare `observed_sha256`
against the complete frozen map when qualifying nonruntime source files too.

These are before/after static observations, not an atomic filesystem snapshot,
a dependency discovery mechanism or proof of the dynamic loader's selected
libraries. Hashing runtime-only scope does not qualify other source files.
The caller owns file-map provenance, read permissions, overall resource limits,
and any required live-runtime checks.

Host-specific compiler policies, binary allowlists, approval records and private
evaluation fixtures are outside this module. Adding these helpers does not
resolve a TTS memory investigation or establish model accuracy.

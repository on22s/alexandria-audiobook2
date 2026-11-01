# Native subprocess ownership checks

The Linux release suite covers its kernel ownership implementation and the
controlled Win32 API boundary cases. It does not verify the Windows kernel.

The required `windows-owner` CI job runs these nine checks on `windows-latest`,
through both its base interpreter and a real virtual environment:

```text
cd app
python tests/native_windows_owner_checks.py
python -m venv --system-site-packages .native-owner-venv
.native-owner-venv/Scripts/python.exe tests/native_windows_owner_checks.py
```

Set `PYTHONPATH` to `.` when invoking the script. The script rejects a non-Windows
host with exit 2 and accepts only nine successful tests with zero skips. It uses
Python's standard library; it does not load models or perform GPU inference.
It verifies detached descendants with inherited and closed pipes, high-bit exit
status, controller-death cleanup, preservation of an unrelated process, and
private export-stage hard-exit, live-owner and finish behavior.
The current-interpreter check also preserves the venv identity, explicit executable
override, caller environment, cwd, Unicode arguments, stdin and declared inherited
handle. Detached tests identify actual interpreter/redirector PIDs and retain
process handles through shutdown, including any additional console-host members.
The executable option configures the trusted supervisor; the worker command's
first argument selects its interpreter. The native matrix exercises both base
and current-venv workers with inherited/current/base/alias supervisor choices, and
checks that Python consumes the private launcher variable before worker work.
Equivalent override paths are identity-checked and replaced by the trusted
runtime path before launch. Venv bootstrapping captures the native inherited
environment so hidden per-drive directory entries survive; a real child-process
test includes a supplementary Unicode character in that inherited value.

Before that native CI job succeeds, Windows kernel verification remains pending.
The current macOS process-group fallback still lacks exclusive lifetime ownership
of escaped descendants (review finding #369); neither Linux nor Windows results
resolve that implementation gap.

## Export staging ownership and cleanup

The same `windows-owner` job also runs the existing export staging suite:

```text
python -m unittest tests.test_export_staging -v
```

This exercises normal cleanup, live-owner preservation, abandoned-owner
reclamation, marker validation, and the Windows handle's rejection of directory
retargeting. POSIX-only retirement tests are explicitly skipped on Windows;
their Linux results do not qualify Windows behavior. Private Windows creation/retirement now has hard-exit, finish, live-owner and
recovery checks in this gate. Power-loss directory durability and the legacy
public-stage finalization path remain unqualified.

## Adapter publication file flushing

The `windows-owner` job runs the adapter tree sync regression tests:

```text
python -m unittest tests.test_adapter_publication_sync -v
```

These five tests cover native file flushing without truncation, handle closure,
platform-specific access modes, and propagation of open, file-flush, and
directory-flush failures. Windows file flushing requires writable payload files;
read-only attributes or denied write access still abort publication. POSIX keeps
read-only access. Windows directory-entry and power-loss rename durability remain
unqualified; this gate verifies file flushing, not those guarantees.

## Book transaction, recovery archive and cancellation handles

Windows-only checks for these areas live in a script that unit discovery
does not collect, so the Linux release shards (which fail on any skipped test)
never see them:

```text
cd app
python tests/native_windows_transaction_checks.py
```

Set `PYTHONPATH` to `.`. The `windows-owner` job runs it after installing the
script-router dependencies it imports. It rejects a non-Windows host with exit 2
and accepts only 28 successful tests with zero skips: recovery-archive directory
guards and unsafe-entry observations (11), native staging identities and handles in the book transaction (5),
book-move cleanup and sharing handles (4), published-script cancellation
through the owned job (3), short-name/ancestor path admission (2), and sound-upload
stage hardlink, contention and retained-delete-failure contracts (3). The
short-name check requires a volume providing a distinct DOS short spelling;
absence fails native qualification rather than passing a skipped check.
Do not add `skipUnless` Windows guards to discovered
`test_*.py` modules; put native-only checks here instead.

## Aliased temporary roots

`SoundDesignAssetStore` deliberately refuses a store root whose real path
differs from the supplied path, so upload fixtures must canonicalize their
temporary roots. A runner whose `TEMP` is a junction alias (hosted Windows uses
a short-name path) exposes any fixture that forgets to. This script pins that
behavior:

```text
cd app
python tests/native_windows_sound_temp_root_checks.py
```

The child prepends the application directory to the existing `PYTHONPATH`.
It rejects a non-Windows host with exit 2 before any
third-party import, creates its own unique OS temporary root and a real
junction inside it, assigns `tempfile.tempdir` to the aliased path without
resolving it first, and verifies that a temporary directory created under it
is genuinely aliased. The junction comes from one deferred helper that calls
the native `_winapi.CreateJunction(target, junction)` primitive with the
target path first and the junction path second, matching the stock CPython
tests (`Lib/test/test_os.py`, `Lib/test/test_shutil.py`). The module therefore
imports on a non-Windows host, and a missing primitive or an `OSError` is a
loud refusal rather than a fallback. No command shell is involved, so no
quoting, `%VAR%` expansion or `cmd` escaping can reach the paths. Before
accepting the junction the gate reads `os.lstat` on the link and requires the
mount-point reparse tag (`IO_REPARSE_TAG_MOUNT_POINT`) on a directory, so a
symbolic link cannot be accepted in its place. Before loading the suite,
it requires the production store to accept the canonical root and reject the
aliased root. It then runs, in a child
interpreter, exactly the four modules the upload-route step runs
(`tests.test_sound_design_stage_ownership`,
`tests.test_sound_design_upload_ownership`, `tests.test_source_upload_stage`,
`tests.test_sound_design_asset_routes`).

The gate pins the sorted identities of those 55 unittest methods in the
script. On every host the guard derives the same identities from the four
module sources and requires exact equality, so removing a Windows-relevant
method and adding another cannot keep the gate green. The worker also binds
the loaded suite to that same identity before it accepts the run. The 55 is
the unittest method count across those four modules, not a count of 55
distinct Windows-effective bodies: several of those methods branch on
`os.name` and only some of their assertions run on each host. The gate does
not measure Windows-effective bodies.

The parent then builds a second junction beneath the same owned base whose
target and link names contain `&`, the literal string `%AA2_HOSTILE_EXPANSION%`,
`!`, `^` and parentheses, with that variable temporarily set to a path
outside the base. It requires the link to resolve to the literal target, the
payload written into the target to be reachable through the link, and the path
outside the base not to appear, then removes the hostile junction before the
owned tree and restores the variable if it was already set. Those are the
characters a command-shell hop would rewrite or expand; the primitive receives
both paths directly, so a rewritten path fails loudly instead of passing. No
path is created outside the owned base.

The parent/child handshake itself runs only on Windows. On a non-Windows host
the discovered-module guard now launches this script as a real subprocess with
no arguments and requires exit 2 with `nothing was verified` on stderr, so an
eager import or a module-level side effect cannot masquerade as the refusal.
The Windows arm of that subprocess control is not run by the guard: a
no-argument Windows run is the native flow and is measured by the separate CI
step below. The receipt, containment and in-process argv controls mock the
junction helper, the child, the removal calls and `sys.platform`; a fake
`_winapi` module pins the native argument order, the unavailable-primitive
refusal and the `OSError` text without calling the primitive. They remain
synthetic controls and are not native evidence. Any worker invocation other than
`--run-modules-under-alias <aliased-root>` is refused with exit 2, so an
invalid argv never falls through to the parent gate. Junction, root and
temporary state are removed afterwards. The `native audio integrity` job runs
this as a Windows-only step after the upload-route dependencies are installed,
so a router, shared-upload or fixture change that drops root canonicalization
fails there instead of only on a runner with an aliased `TEMP`.

Remaining uncertainty: the primitive is exercised only on the hosted Windows
runner and on a developer Windows host. A host whose `_winapi` lacks
`CreateJunction`, or whose temporary volume rejects the primitive, fails loudly
instead of falling back, so that environment stays unqualified rather than
silently skipped. The hostile-name check measures the temporary volume that
host provides, not every filesystem an operator might select as a temporary
root. Neither the 600-second child timeout, the 55-method identity nor the
receipt success fields changed.

`app/tests/windows_only_skip_baseline.txt` is in that workflow's
`pull_request` paths list, and the guard asserts the entry, so a registry-only
edit still runs the native job. The test modules containing registry sites and
their in-repo dependencies are not individually listed: `tests.yml` triggers on every pull request without a
paths filter, and the census is platform-independent, while a registry edit is
the one input that can falsify the native step contract without touching a
listed file.

The discovered-module guard reads the Windows-only gate from the step's own
top-level keys at the step's key indentation; a nested `if` under `env` does
not satisfy it and fails with a named assertion. Quoted step names, `${{ }}`
gate expressions, trailing YAML comments and `#` inside quoted values are
handled, and a changed `run:` command is rejected.

The discovered-module guard recursively censuses every `test_*.py` source under
`app/tests`, including its own file and untracked files. This deliberately includes
plain directories that unittest discovery does not enter: a skip site there must
have a registry row rather than disappearing from the source scan. Duplicate
basename/class/site keys fail loudly instead of replacing a site. Tests outside
`app/tests` remain outside this static census; runtime discovery starts at `app/`.
On Windows, directory junctions can lead this read-only scan to source outside
the physical test tree; this is not a filesystem-containment check.
The walk rejects a repeated physical directory in its ancestor chain before
reading test sources, while sibling aliases remain visible to duplicate-key
validation. Unreadable directories fail loudly. Traversal also refuses beyond
10,000 directories or 100,000 entries rather than silently truncating the census.
Every decorated class or function whose callee
statically resolves to `unittest.skip`, `skipIf` or `skipUnless` through module
aliases, `from unittest import *`, an alias of the constructor itself
(`SU = unittest.skipUnless`, including a tuple-unpacked positional binding), an
in-repo imported decorator, or a class attribute holding the constructor or a
skip call (`class Holder: WIN = unittest.skipUnless(...)`, used as
`@Holder.WIN`) is one site, and every site must have exactly one registry row
in `app/tests/windows_only_skip_baseline.txt`. A class-attribute decorator
resolves in the class-body binding table a method decorator already uses, so
nested classes (`@Outer.Inner.WIN`) and classes imported by name or through
their module (`@helper.Holder.WIN`) compose through the existing symbol
resolution, and the censused condition is the aliased call's own argument,
judged by the same refusal table. There is no second platform table and no
text scan for the alias. An attribute chain whose leaf has no static class-body
binding, including an attribute assigned after the class body
(`Holder.WIN = ...`), is reported unresolved instead of skipped; several
possible bindings, or a conditionally bound attribute, are reported as an
ambiguous skip kind, and a row cannot authorize either. A class attribute
holding a plain value is not a site. The traversal enters function bodies as
well as class bodies, so a skip-decorated class or
function nested inside a function is censused. Function-scoped sites are
reported as unresolved in addition to requiring a row, because the static model
does not claim to resolve function locals; a matching row cannot authorize one.
A site that cannot be resolved, or a closure that exceeds the visited bounds
(depth 12, 24 in-repo definitions and 40000 AST nodes per site), is reported as
unresolved or truncated and fails the registry check. Inline
assignment-applied skips whose value is a double call are reported as
unresolved and must be rewritten as decorators; a registry row cannot authorize
that unsupported shape, and that shape includes a class attribute holding the
constructor (`test_case = Holder.SW(cond, reason)(test_case)`). A skip applied
by assignment through a named decorator alias or through a single class-member
call (`test_case = Holder.WIN(test_case)`) is outside this static scan. The
removed whole-text literal scan could detect some of these forms; Linux
zero-skip shards remain the runtime authority for this explicit coverage
limitation. Class-reference assignment aliases (`ALIAS = Holder; @ALIAS.WIN`)
and dynamically constructed classes (`Holder = type(...)`) with non-skip-named
decorator attributes are also outside the static resolver. These shapes can
produce no site or unresolved entry; Linux zero-skip shards remain their runtime
backstop. An inherited attribute on a statically resolved class is unresolved,
including when the class root is bound in an enclosing class-decorator scope.

Each row is `<file>:<QualName>:<decorator-index> = <sha256>  # <declaration>`.
The digest covers the normalized decorator AST, the ordered source closure and
the reviewed declaration. The closure pins every in-repo definition the
predicate or callee chain reaches, including helper function bodies, together
with the complete enclosing control statements for conditionally bound names,
so changing only an `if` predicate changes the digest. Statements reached
through a class body are pinned too: a class-scope constant or alias used as a
predicate, and the class-body assignments it depends on, are part of the
closure. A class-attribute decorator pins the class-body alias assignment as its
own closure entry and, through the attribute's base name, the enclosing class
definition. A class decorator resolves in the scope enclosing the class, while a
method decorator resolves in the class body only, so an enclosing class is not
a lexical scope for a nested class body. Rows without a site and
sites without a row both fail. The row count is the measured census output, not
a declared constant.

The declaration field is reviewed prose, not a derived value. Declarations name
the platform outcome a reader verified when the row was written. One row is
interpreter-gated rather than platform-gated: `not __debug__` skips on every
host under `-O`, and that row declares `platform=environment-gated` with its
`-O` behaviour. A declaration is never derived from the predicate: the refusal
table's POSIX analysis is a narrow syntax table over recognized Windows literals
and does not evaluate arbitrary predicates, so no declaration should be read as
measured predicate behaviour.

To refresh digests after a reviewed source edit, run from `app`:

```text
python -m tests.test_windows_native_ci --repin
python -m tests.test_windows_native_ci --repin --apply
```

The first form is a dry run: it prints every `<key>: <old> -> <new>` digest
change, the change count and the row count, and writes nothing. `--apply`
rewrites only the digest field of the rows whose location and declaration are
unchanged, through a temporary file in the same directory and an atomic
replace, preserving row order and declarations. It refuses instead of writing
when the census reports an unresolved, truncated, refused or ambiguous site,
when a site lacks a row or a row lacks a site, or when a rewritten declaration
would differ from the row read at invocation. It cannot add, remove or reword
a declaration, so a new skip site still needs a hand-written reviewed row, and
it does not establish that any historical declaration was true: it only
recomputes digests from the source present in the working tree. Immediately before
replacement it re-reads the registry and refuses a change since invocation. This
is not atomic compare-and-swap: a concurrent write between that read and replace
can still be overwritten, so do not run concurrent registry writers in one checkout.

A skip site whose condition is a bare name that resolves to no in-repo binding
and is not a builtin remains registered and is also reported as unresolved, so
an unpinned predicate cannot pass as a green row. A dotted predicate such as
`Holder.RUN` is pinned through the class-body binding when one exists and, like
every other dotted predicate, is not seeded as an unknown name; its refusal is
still decided only by the recognized literal comparisons. A binding introduced
by a walrus expression, a `with ... as` target or an `except ... as` target is
recorded as dynamic rather than silently dropped: a skip that depends on one is
reported as a truncated closure and fails even with a matching row, because the
static model does not evaluate those bindings. Recognized skips using unpacked
positional or keyword arguments are ambiguous and fail even with a matching row;
the guard does not evaluate the unpacked values. A helper
that can return two different skip kinds is reported as an ambiguous skip kind:
the site still requires a row and the registry check fails. A whole decorator
that is a blanket `unittest.skip` is refused as an unconditional skip, distinct
from the literal Windows-only refusal.

A whole decorator argument that is an un-negated `sys.platform`, `os.name` or
`platform.system` comparison against a Windows literal (including `==`, `!=`,
literal membership and the recognized `startswith` prefixes `win`/`win32`,
`nt`, and `Win`/`Windows`, respectively) is refused by a syntax table keyed
by the resolved skip kind and comparison polarity: `skipUnless` with a
Windows-true comparison, and `skipIf` with a comparison that is true on Linux,
are refused even when a row exists. The same rule is applied through `and`,
`or`, `not` and conditional expressions without evaluating the predicate:
`skipUnless` is refused when the condition is provably false on Linux (so a
conjunct such as `sys.platform == 'win32' and X` is refused), and `skipIf` is
refused only when the condition is provably true on Linux. An operand that
cannot be proved either way stays unknown, so `skipIf(sys.platform == 'win32'
or sys.platform == 'darwin')` is not a Windows-only refusal while
`skipUnless(sys.platform == 'win32' and X)` is. Linux-true `skipUnless`
conditions and `skipIf(sys.platform == 'win32')` are permitted. No predicate
value is evaluated and no branch is chosen. For `skipUnless`, a leading
Windows-true comparison that the evaluator cannot prove Linux-safe is still
refused, restoring the removed whole-text scan's conservative baseline:
`sys.platform == 'win32' or UNKNOWN_FLAG` and `... or True` are refused even
though the truth result is unknown or true. The refusal reason names this as
`conservative legacy Windows-only refusal`, distinct from
`literal Windows-only refusal` when the condition is proven to skip Linux. The
baseline follows the leading operand of `and`/`or` and the body of a
conditional expression, applies only to `skipUnless`, and evaluates nothing.

Literal membership containing a recognized Windows literal is lowered to the
same ordered `or` of equality comparisons; `not in` adds a surrounding `not`.
Tuple, list and set syntax use their written element order for the conservative
leading-operand rule. Other platform literals remain unknown, exactly as in
the corresponding explicit comparisons. This is syntax normalization, not
predicate execution or an additional platform-coverage claim.

This model targets the Linux release host, not every POSIX platform. A
macOS-only condition remains unknown, not proof of macOS coverage. Only boolean
constants are folded; integers, strings, containers and calls such as
`bool(True)` remain unknown even when a reader can infer their truth value.

Dynamically constructed decorators (`getattr`, `partial`, third-party
factories) and runtime `self.skipTest`/`raise SkipTest` are outside the static
census. They are disclosed residuals, not exemptions: the Linux release shards
fail on every skip they observe, but cannot backstop a runtime skip whose
condition is true only on Windows or macOS. Coverage of those paths requires
execution on the corresponding host; a reviewed static row does not prove it.

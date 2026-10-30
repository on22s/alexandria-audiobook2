"""The unit suite.

THIS FILE IS LOAD-BEARING AND MUST NOT BE DELETED. `verify_release.py` gates
the build with `unittest discover -s . -p 'test_*.py'`, and unittest will not
recurse into a directory that is not an importable package. Without this file
discovery walks past `tests/` and prints:

    Ran 0 tests in 0.000s

    OK

which `validate_unittest_output` accepted as a pass until the floor check was
added alongside this move. A green build that ran nothing is worse than a red
one, so the floor is the real guard and this file is what keeps it unnecessary.

Being a package also means test modules are imported as `tests.test_x`, so a
module that imports a sibling test must say `from tests.test_support import ...`
rather than `from test_support import ...`.
"""

import os
import tempfile

# macOS hands out temporary folders under /var, a link to /private/var, while
# the code under test reports or requires resolved paths. Give every test, and
# every process a test starts, the resolved folder (#1681, #1682). Linux /tmp is
# not a link, so this changes nothing there.
_resolved = os.path.realpath(tempfile.gettempdir())
if os.name != 'nt' and _resolved != tempfile.gettempdir():
    os.environ['TMPDIR'] = _resolved
    tempfile.tempdir = _resolved

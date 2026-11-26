from tests.test_support import get_isolated_git_environment, isolate_git_environment
import ast
import importlib
import inspect
import json
import os
from pathlib import Path
import skip_registry
import subprocess
import sys
import tempfile
import time
import unittest


INVENTORY_PATH = Path(__file__).with_name("unit_test_inventory.json")
EXCLUDED_TEST_MODULES = {"test_api"}  # Script-style live API suite, not unittest.


def open_prior_reader(path):
    """Open a reader whose snapshot survives a concurrent replacement."""
    if os.name != "nt":
        return path.open("rb")
    import ctypes
    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0x1 | 0x2 | 0x4,
                                  None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def get_test_directory(test_directory=None):
    """Return the discovery root, allowing private fixture trees."""
    return Path(test_directory) if test_directory is not None else Path(__file__).parent


def _tracked_test_modules(test_directory=None):
    """Module stems for test files git tracks, or None if git cannot answer.

    This working tree is shared between concurrent sessions on different
    branches, so an untracked test file belonging to someone else's branch sits
    beside ours. Discovering from the filesystem swept those into the checked-in
    inventory, and CI - which only has the committed files - then reported every
    one of them as "no longer discovered". That broke the build three times in
    one day. Only files under version control can be in a checked-in inventory.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "test*.py"],
            cwd=get_test_directory(test_directory), capture_output=True, timeout=10, env=get_isolated_git_environment())
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {Path(name).stem
            for name in result.stdout.decode("utf-8").split("\0") if name}


def _iter_tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _iter_tests(test)
        else:
            yield test


def _module_level_functions(body):
    """Yield function definitions that are not nested in a class or function.

    Includes definitions inside a top-level ``if``/``try``/``with``/loop or
    ``match`` statement, together with their ``else``, ``except`` and
    ``finally`` arms, so a test defined under ``if __name__ == "__main__":``
    or a ``case`` body cannot hide either.
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            yield from _module_level_functions(node.body)
            yield from _module_level_functions(getattr(node, "orelse", []))
            for handler in getattr(node, "handlers", []):
                yield from _module_level_functions(handler.body)
            yield from _module_level_functions(getattr(node, "finalbody", []))
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _module_level_functions(case.body)


def _uncollected_test_classes(module):
    """Classes defined here whose tests unittest will never collect.

    unittest only collects ``TestCase`` subclasses, so a pytest-style plain
    class imports and discovers cleanly while none of its ``test*`` methods
    run. Membership in ``TestCase`` is asked of ``unittest`` itself
    (``issubclass``), which also handles the mixin bases used throughout this
    suite. Imported classes are skipped: their defining module owns them.
    """
    names = []
    for _, value in inspect.getmembers(module, inspect.isclass):
        if value.__module__ != module.__name__:
            continue
        if issubclass(value, unittest.TestCase):
            continue
        methods = sorted(name for name in dir(value) if name.startswith("test"))
        if value.__name__.startswith("Test") or methods:
            detail = f" ({', '.join(methods)})" if methods else ""
            names.append(value.__name__ + detail)
    return names


def _discovery_failures(path, collected, module):
    """Why a tracked test module would silently not run some of its tests.

    unittest collects TestCase methods, or whatever ``load_tests`` returns; it
    never collects module-level functions or methods of a plain class, and it
    does not await an ``async`` test method on a plain TestCase. A pytest-style
    module therefore imports and discovers cleanly while running none of its
    tests. Return those shapes so discovery fails loud instead of recording a
    plausible-looking ``[]``.

    A helper named ``test_*`` that is not a test is rejected too: the naming
    rule is what makes discovery ambiguous, so the message tells the author to
    rename the helper rather than exempting it here.

    This also means a future shared helper module named ``test_*.py`` that
    collects no tests will block ``verify_release.py`` and ``ready.sh``. That
    is intended: name helpers something other than ``test_*.py``, or add the
    module to ``EXCLUDED_TEST_MODULES`` if it is deliberately not a test.
    """
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), str(path))
    functions = list(_module_level_functions(tree.body))
    collected_ids = {test.id() for test in collected}
    # FunctionTestCase.id() is the function's own name, so a function wrapped
    # by load_tests is represented here exactly; a bare test_* function is not.
    problems = []
    uncollected = [node.name for node in functions
                   if node.name.startswith("test")
                   and node.name not in collected_ids]
    if uncollected:
        problems.append(
            "unittest does not collect these module-level test functions "
            "(rename a non-test helper so it does not start with 'test'): "
            + ", ".join(uncollected))
    plain_classes = _uncollected_test_classes(module)
    if plain_classes:
        problems.append(
            "these classes look like tests but do not subclass unittest.TestCase, "
            "so unittest never collects them: " + ", ".join(plain_classes))
    unawaited = [f"{type(test).__name__}.{getattr(test, '_testMethodName', '')}"
                 for test in collected
                 if isinstance(test, unittest.TestCase)
                 and not isinstance(test, unittest.IsolatedAsyncioTestCase)
                 and inspect.iscoroutinefunction(
                     getattr(type(test), getattr(test, "_testMethodName", ""), None))]
    if unawaited:
        problems.append("async test methods on a non-IsolatedAsyncioTestCase are "
                        "never awaited: " + ", ".join(unawaited))
    if not collected_ids and not problems:
        problems.append("module yields no unittest tests")
    return problems


class InventoryUnavailable(RuntimeError):
    """Discovery could not import every tracked test module.

    Raised instead of returning the modules that happened to import: a partial
    inventory is a plausible-looking answer, and comparing one against the
    committed file would report drift where nothing was compared.
    """

    def __init__(self, failures):
        self.failures = tuple(failures)
        super().__init__(
            "cannot verify the unit test inventory: "
            + "; ".join(f"{name}: {reason}" for name, reason in self.failures))


def _import_failure_reason(error):
    """Name the missing module an import failure actually asked for."""
    if isinstance(error, ModuleNotFoundError) and getattr(error, "name", None):
        return f"missing module {error.name!r}"
    return f"{type(error).__name__}: {error}"


def get_unit_test_inventory(test_directory=None, *, module_names=None):
    """Return every discoverable unittest module and its stable test IDs."""
    directory = get_test_directory(test_directory)
    tracked = _tracked_test_modules(directory)
    modules = sorted(
        path.stem for path in directory.glob("test*.py")
        if path.stem not in EXCLUDED_TEST_MODULES
        # Fall back to the filesystem when git cannot answer (a source export,
        # a container without git) rather than reporting an empty inventory.
        and (tracked is None or path.stem in tracked)
    )
    if module_names is not None:
        requested = set(module_names)
        if not requested or any(not isinstance(name, str) or not name for name in requested):
            raise ValueError('Targeted inventory requires at least one nonempty tracked module name')
        unknown = requested - set(modules)
        if unknown:
            raise ValueError('Unknown tracked test modules: ' + ', '.join(repr(name) for name in sorted(unknown)))
        modules = [name for name in modules if name in requested]
    inventory = {}
    failures = []
    for module_name in modules:
        qualified = f"{__package__}.{module_name}"
        try:
            module = importlib.import_module(qualified)
        except Exception as error:
            failures.append((qualified, _import_failure_reason(error)))
            continue
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        collected = list(_iter_tests(suite))
        test_ids = sorted(test.id() for test in collected)
        for reason in _discovery_failures(
                directory / f"{module_name}.py", collected, module):
            failures.append((qualified, reason))
        inventory[module_name] = test_ids
    if failures:
        raise InventoryUnavailable(failures)
    return inventory


class TestInventoryTests(unittest.TestCase):
    def test_targeted_cli_works_without_site_packages_and_full_check_still_refuses(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            # Keep utils' parent-directory imports inside this private fixture,
            # rather than accidentally finding modules in the shared temp root.
            root = Path(directory) / 'app'
            root.mkdir()
            (root / 'tests').mkdir()
            (root / 'tests/__init__.py').touch()
            app = Path(__file__).resolve().parents[1]
            shutil.copyfile(app / 'update_test_inventory.py', root / 'update_test_inventory.py')
            shutil.copyfile(app / 'utils.py', root / 'utils.py')
            shutil.copyfile(app.parent / 'gpu_stats.py', root.parent / 'gpu_stats.py')
            shutil.copyfile(app / 'loaded_module_code.py', root / 'loaded_module_code.py')
            shutil.copyfile(app / 'skip_registry.py', root / 'skip_registry.py')
            shutil.copyfile(Path(__file__), root / 'tests/test_inventory.py')
            shutil.copyfile(Path(__file__).with_name('test_support.py'), root / 'tests/test_support.py')
            (root / 'tests/test_small.py').write_text('import unittest\nclass Small(unittest.TestCase):\n def test_small(self): pass\n')
            (root / 'tests/test_heavy.py').write_text('import torch\n')
            inventory = root / 'tests/unit_test_inventory.json'
            inventory.write_text(json.dumps({'test_small':['before'], 'test_heavy':['preserved']}))
            subprocess.run(['git','init','-q',str(root)], check=True, capture_output=True, env=get_isolated_git_environment())
            subprocess.run(['git','-C',str(root),'add','tests/test_small.py','tests/test_heavy.py'], check=True, capture_output=True, env=get_isolated_git_environment())
            environment = {key:value for key,value in get_isolated_git_environment().items()
                           if not key.startswith('PYTHON')}
            selected = subprocess.run([sys.executable,'-S','update_test_inventory.py','--module','test_small'],
                cwd=root, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(0, selected.returncode, selected.stdout + selected.stderr)
            self.assertIn('TARGETED UPDATE ONLY', selected.stdout)
            self.assertEqual(['preserved'], json.loads(inventory.read_text())['test_heavy'])
            self.assertEqual(['tests.test_small.Small.test_small'], json.loads(inventory.read_text())['test_small'])
            self.assertNotIn(b'\r', inventory.read_bytes())
            before = inventory.read_bytes()
            full = subprocess.run([sys.executable,'-S','update_test_inventory.py','--check'],
                cwd=root, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(2, full.returncode, full.stdout + full.stderr)
            self.assertIn("missing module 'torch'", full.stdout)
            self.assertIn('Nothing was compared', full.stdout)
            self.assertEqual(before, inventory.read_bytes())

    def test_targeted_discovery_does_not_import_unrelated_ml_modules(self):
        import types
        from unittest.mock import patch
        class Example(unittest.TestCase):
            def test_example(self):
                pass
        Example.__module__ = 'tests.test_small'
        module = types.ModuleType('tests.test_small')
        module.Example = Example
        imported = []
        def load(name):
            imported.append(name)
            if name.endswith('test_heavy'):
                raise ModuleNotFoundError("No module named 'torch'", name='torch')
            return module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['test_small.py', 'test_heavy.py']:
                (root / name).touch()
            with patch(__name__ + '._tracked_test_modules', return_value={'test_small', 'test_heavy'}), \
                    patch('importlib.import_module', side_effect=load):
                selected = get_unit_test_inventory(root, module_names=['test_small'])
                self.assertEqual(['test_small'], list(selected))
                self.assertEqual(['tests.test_small'], imported)
                self.assertEqual(1, len(selected['test_small']))
                with self.assertRaises(InventoryUnavailable):
                    get_unit_test_inventory(root)
                with self.assertRaises(ValueError):
                    get_unit_test_inventory(root, module_names=['../untracked'])

    def test_targeted_writer_retains_other_ids_and_lf_bytes(self):
        from unittest.mock import patch
        import update_test_inventory as updater
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.json'
            path.write_text(json.dumps({'test_old':['old'], 'test_new':['before']}))
            with patch.object(updater, 'get_unit_test_inventory', return_value={'test_new':['after']}):
                updater.write_inventory(path, module_names=['test_new'])
            self.assertEqual({'test_old':['old'], 'test_new':['after']}, json.loads(path.read_text()))
            self.assertNotIn(b'\r', path.read_bytes())

    def test_targeted_writer_refuses_unreadable_base_without_replacing_it(self):
        from unittest.mock import patch
        import update_test_inventory as updater
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.json'
            path.write_bytes(b'invalid json')
            with patch.object(updater, 'get_unit_test_inventory', return_value={'test_new':['after']}):
                with self.assertRaises(ValueError):
                    updater.write_inventory(path, module_names=['test_new'])
            self.assertEqual(b'invalid json', path.read_bytes())

    def test_unit_test_inventory_is_stable(self):
        from update_test_inventory import get_inventory_differences

        expected = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        actual = get_unit_test_inventory()

        self.assertNotIn("test_api", actual)
        differences = get_inventory_differences(expected, actual)
        self.assertEqual([], differences, "Unit test inventory drift:\n" + "\n".join(differences))

    def test_inventory_differences_are_compact_and_actionable(self):
        from update_test_inventory import get_inventory_differences

        differences = get_inventory_differences(
            {"test_example": ["test_example.Example.test_removed"]},
            {"test_example": ["test_example.Example.test_added"]},
        )
        self.assertEqual([
            "Missing from inventory: test_example.Example.test_added",
            "No longer discovered: test_example.Example.test_removed",
        ], differences)

    def test_inventory_writer_is_deterministic_and_check_is_read_only(self):
        import tempfile
        from unittest.mock import patch
        import update_test_inventory as updater

        inventory = {"test_z": ["z"], "test_a": ["a"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "inventory.json")
            with patch.object(updater, "get_unit_test_inventory", return_value=inventory):
                updater.write_inventory(path)
                first = path.read_bytes()
                self.assertLess(first.index(b'"test_a"'), first.index(b'"test_z"'))
                updater.write_inventory(path)
                self.assertEqual(first, path.read_bytes())
                self.assertEqual([], updater.check_inventory(path))
                self.assertEqual(first, path.read_bytes())


class InventoryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "tests"
        self.directory.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, env=get_isolated_git_environment())
        (self.directory / "test_fixture.py").write_text(
            "import unittest\nclass Fixture(unittest.TestCase):\n"
            "    def test_tracked(self):\n        pass\n", encoding="utf-8")
        (self.directory / "test_api.py").write_text("raise RuntimeError('excluded')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "tests/test_fixture.py", "tests/test_api.py"], check=True, env=get_isolated_git_environment())

    def start_fixture_reader(self, directory=None, *, wait_file=None, git_available=True):
        directory = self.directory if directory is None else directory
        code = (
            "import json,os,sys,time\nfrom pathlib import Path\n"
            "import tests.test_inventory as inventory\nimport update_test_inventory as updater\n"
            "import tests\ntests.__path__=[sys.argv[1]]\nroot=Path(sys.argv[1])\n"
        )
        if wait_file is not None:
            code += "(root/'ready').touch()\nwhile not Path(sys.argv[2]).exists():time.sleep(0.01)\n"
        code += ("tracked=inventory._tracked_test_modules(root)\n"
                 "print(json.dumps({'inventory':inventory.get_unit_test_inventory(root),"
                 "'untracked':updater.find_untracked_test_modules(root),"
                 "'tracked':None if tracked is None else sorted(tracked)}))\n")
        environment = get_isolated_git_environment()
        environment["PYTHONPATH"] = str(Path(__file__).parent.parent)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if not git_available:
            (self.root / "no-git").mkdir(exist_ok=True)
            environment["PATH"] = str(self.root / "no-git")
        return subprocess.Popen([sys.executable, "-c", code, str(directory), str(wait_file)],
                                cwd=self.root, env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def read_fixture(self, directory=None, *, git_available=True):
        child = self.start_fixture_reader(directory, git_available=git_available)
        return self.read_child(child)

    def read_child(self, child):
        try:
            stdout, stderr = child.communicate(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
        self.assertEqual(0, child.returncode, stderr)
        return json.loads(stdout)


class TrackedOnlyDiscoveryTest(InventoryFixtureTests):
    """The inventory is checked in, so it can only contain tracked files.

    This tree is shared between concurrent sessions on different branches. An
    untracked test file from another branch used to be swept into the inventory
    here and then reported missing by CI, which only sees committed files.
    """

    def test_an_untracked_test_file_is_not_inventoried(self):
        untracked = self.directory / "test_zz_untracked_probe.py"
        untracked.write_text(
            "import unittest\n\n\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_probe(self):\n        pass\n",
            encoding="utf-8")
        self.assertNotIn("test_zz_untracked_probe", self.read_fixture()["inventory"])

    def test_tracked_files_are_still_discovered(self):
        self.assertEqual({"test_fixture": ["tests.test_fixture.Fixture.test_tracked"]},
                         self.read_fixture()["inventory"])

    def test_discovery_falls_back_when_git_is_unavailable(self):
        # A source export without git must still produce an inventory rather
        # than an empty one.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name in ("test_fixture.py", "test_api.py"):
                (directory / name).write_bytes((self.directory / name).read_bytes())
            report = self.read_fixture(directory, git_available=False)
            self.assertIsNone(report["tracked"], "git still answered; the fallback was not exercised")
            self.assertEqual({"test_fixture": ["tests.test_fixture.Fixture.test_tracked"]},
                             report["inventory"])
            self.assertEqual([], report["untracked"])


class UntrackedTestWarningTest(InventoryFixtureTests):
    """Excluding untracked files from the inventory created an ordering trap.

    Write a new test file, regenerate the inventory, then git add and commit:
    the inventory was generated while the file was still untracked, so CI
    discovers tests it does not list and the build goes red. This happened on
    PR #233, one PR after the exclusion was introduced.
    """

    def test_a_new_untracked_test_file_is_reported(self):
        probe = self.directory / "test_zz_untracked_warning_probe.py"
        probe.write_text("import unittest\n", encoding="utf-8")
        self.assertEqual([probe.name], self.read_fixture()["untracked"])

    def test_a_tracked_file_is_not_reported(self):
        # Asserting the whole tree is clean would fail during normal work -
        # a new test file is untracked exactly when you are writing it, which
        # is when you run the suite most. Assert the behaviour instead.
        self.assertEqual([], self.read_fixture()["untracked"])


class InventoryFixtureIsolationTests(InventoryFixtureTests):
    def get_checkout_snapshot(self):
        directory = Path(__file__).parent
        index = subprocess.run(["git", "rev-parse", "--git-path", "index"],
                               cwd=directory, capture_output=True, text=True, check=True).stdout.strip()
        index = Path(index)
        if not index.is_absolute():
            index = directory / index
        return ({path.name: path.read_bytes() for path in directory.glob("test*.py")},
                index.read_bytes())

    def test_hard_exit_after_each_actual_probe_write_leaves_only_private_residue(self):
        before = self.get_checkout_snapshot()
        for class_name, method_name, probe_name in (
            ("TrackedOnlyDiscoveryTest", "test_an_untracked_test_file_is_not_inventoried", "test_zz_untracked_probe.py"),
            ("UntrackedTestWarningTest", "test_a_new_untracked_test_file_is_reported", "test_zz_untracked_warning_probe.py")):
            with self.subTest(class_name=class_name):
                state = self.root / "child-root"
                code = ("import os\nfrom pathlib import Path\nimport tests.test_inventory as module\n"
                        f"case=module.{class_name}({method_name!r})\ncase.setUp()\n"
                        f"Path({str(state)!r}).write_text(str(case.directory))\n"
                        "case.read_fixture=lambda:os._exit(23)\n"
                        f"case.{method_name}()\n")
                environment = get_isolated_git_environment()
                environment.update(PYTHONPATH=str(Path(__file__).parent.parent),
                                   PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.root))
                result = subprocess.run([sys.executable, "-c", code], cwd=self.root, env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(23, result.returncode, result.stderr)
                directory = Path(state.read_text())
                self.assertTrue(directory.is_relative_to(self.root))
                self.assertTrue((directory / probe_name).is_file())
                self.assertEqual(before, self.get_checkout_snapshot())

    def test_overlapping_private_readers_keep_fixed_probe_names_and_indexes_independent(self):
        before = self.get_checkout_snapshot()
        other = InventoryFixtureTests()
        other.setUp()
        self.addCleanup(other.doCleanups)
        probe_name = "test_zz_untracked_probe.py"
        (self.directory / probe_name).write_text("first private probe\n")
        (other.directory / probe_name).write_text("second private probe\n")
        indexes = [(case.root / ".git/index").read_bytes() for case in (self, other)]
        release = self.root / "release"
        children = [case.start_fixture_reader(wait_file=release) for case in (self, other)]
        try:
            deadline = time.monotonic() + 5
            while not all((case.directory / "ready").exists() for case in (self, other)):
                self.assertLess(time.monotonic(), deadline, "private readers did not reach barrier")
                time.sleep(0.01)
            release.touch()
            for child in children:
                result = self.read_child(child)
                self.assertEqual({"test_fixture": ["tests.test_fixture.Fixture.test_tracked"]}, result["inventory"])
                self.assertEqual([probe_name], result["untracked"])
            self.assertEqual("first private probe\n", (self.directory / probe_name).read_text())
            self.assertEqual(indexes[1], (other.root / ".git/index").read_bytes())
            other.doCleanups()
            self.assertTrue((self.directory / probe_name).exists())
            self.assertEqual(indexes[0], (self.root / ".git/index").read_bytes())
            self.assertEqual(before, self.get_checkout_snapshot())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()




class InventoryCheckWarningTests(unittest.TestCase):
    @isolate_git_environment()
    def test_check_warns_about_actual_untracked_tests_without_writing_inventory(self):
        import contextlib
        import io
        import tempfile
        import tests.test_inventory as inventory_module
        import update_test_inventory as updater
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            tests=root/'app/tests'
            tests.mkdir(parents=True)
            subprocess.run(['git','init','-q'],cwd=root,check=True, env=get_isolated_git_environment())
            (tests/'test_tracked.py').write_text('# tracked fixture\n',encoding='utf-8')
            (tests/'test_new.py').write_text('# new local fixture\n',encoding='utf-8')
            (tests/'test_api.py').write_text('# explicitly excluded\n',encoding='utf-8')
            subprocess.run(['git','add','app/tests/test_tracked.py'],cwd=root,check=True, env=get_isolated_git_environment())
            path=tests/'unit_test_inventory.json'
            expected={'test_tracked':['tests.test_tracked.Example.test_one']}
            path.write_text(updater.format_inventory(expected),encoding='utf-8')
            before=path.read_bytes()
            actual_check=updater.check_inventory
            for current,code in ((expected,0),({'test_tracked':[]},1)):
                with self.subTest(code=code), \
                     patch.object(updater,'__file__',str(root/'app/update_test_inventory.py')), \
                     patch.object(inventory_module,'__file__',str(tests/'test_inventory.py')), \
                     patch.object(updater,'get_unit_test_inventory',return_value=current), \
                     patch.object(updater,'check_inventory',side_effect=lambda:actual_check(path)), \
                     patch.object(updater,'write_inventory') as write:
                    output=io.StringIO()
                    with contextlib.redirect_stdout(output):
                        result=updater.main(['--check'])
                    self.assertEqual(code,result)
                    self.assertIn('WARNING:',output.getvalue())
                    self.assertIn('test_new.py',output.getvalue())
                    self.assertNotIn('  test_api.py',output.getvalue())
                    self.assertNotIn('  test_tracked.py',output.getvalue())
                    self.assertEqual(before,path.read_bytes())
                    write.assert_not_called()


class AtomicInventoryWriterTests(unittest.TestCase):
    def test_serialization_has_one_sorted_test_id_per_line(self):
        from update_test_inventory import format_inventory
        inventory = {'test_z': ['z.b', 'z.a'], 'test_a': ['a.b', 'a.a']}
        rendered = format_inventory(inventory)
        lines = rendered.splitlines()
        for test_id in ('a.a', 'a.b', 'z.a', 'z.b'):
            self.assertEqual(1, sum(json.dumps(test_id) in line for line in lines))
        self.assertFalse(any('"a.a"' in line and '"a.b"' in line for line in lines))
        self.assertLess(rendered.index('a.a'), rendered.index('a.b'))
        self.assertLess(rendered.index('test_a'), rendered.index('test_z'))
        self.assertEqual({key: sorted(value) for key, value in inventory.items()}, json.loads(rendered))

    def test_appending_test_id_or_module_preserves_existing_lines(self):
        from update_test_inventory import format_inventory
        before = format_inventory({'test_a': ['a.a', 'a.b']}).splitlines()
        after = format_inventory({'test_a': ['a.a', 'a.b', 'a.z'], 'test_z': ['z.a']}).splitlines()
        self.assertTrue(all(line in after for line in before))

    def test_open_reader_keeps_prior_json_while_new_reader_gets_complete_inventory(self):
        import tempfile
        from unittest.mock import patch
        import update_test_inventory as updater
        inventory = {"test_z": ["unicode_é"], "test_a": ["first"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "inventory.json")
            prior = b'{"prior": ["retained"]}'
            path.write_bytes(prior)
            with open_prior_reader(path) as old_reader, \
                 patch.object(updater, "get_unit_test_inventory", return_value=inventory):
                try:
                    self.assertEqual(path, updater.write_inventory(path))
                except PermissionError:
                    self.assertEqual("nt", os.name,
                                     "POSIX must replace while a reader holds the file")
                    self.assertEqual(prior, path.read_bytes())
                self.assertEqual(prior, old_reader.read())
            with patch.object(updater, "get_unit_test_inventory", return_value=inventory):
                self.assertEqual(path, updater.write_inventory(path))
                actual = path.read_bytes()
                self.assertEqual(inventory, json.loads(actual))
                self.assertLess(actual.index(b'"test_a"'), actual.index(b'"test_z"'))
                self.assertIn("é".encode(), actual)
                self.assertEqual([], updater.check_inventory(path))
                self.assertEqual(actual, path.read_bytes())
            self.assertEqual([path], list(Path(tmp).iterdir()))

    def test_failed_serialization_or_replace_preserves_prior_inventory_and_cleans_temporary_files(self):
        import tempfile
        from unittest.mock import patch
        import update_test_inventory as updater
        import utils
        def broken_dump(data, handle, **kwargs):
            handle.write('{"partial":')
            raise ValueError("serialization interrupted")
        for operation, error in (("serialize", ValueError), ("replace", OSError)):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "inventory.json")
                prior = b'{"prior": ["retained"]}'
                path.write_bytes(prior)
                fault = (patch.object(utils.json, "dump", side_effect=broken_dump)
                         if operation == "serialize" else
                         patch.object(utils.os, "replace", side_effect=OSError("disk failure")))
                with patch.object(updater, "get_unit_test_inventory", return_value={"new": ["complete"]}), fault:
                    with self.assertRaises(error):
                        updater.write_inventory(path)
                self.assertEqual(prior, path.read_bytes())
                self.assertEqual({"prior": ["retained"]}, json.loads(path.read_bytes()))
                self.assertEqual([path], list(Path(tmp).iterdir()))


class CapabilityGateInventoryTests(unittest.TestCase):
    """Native discovery must survive a host without POSIX kernel locks.

    Twenty test modules import fcntl at module scope. Importing them is what
    the inventory does, so one missing capability used to abort the whole
    discovery and read as inventory drift. These controls pin the fix by the
    artifact - the exact inventoried IDs - and by the diagnosis a caller sees
    when discovery genuinely cannot finish.
    """

    MODULES = ("test_app_gpu_lease_inheritance", "test_corpus_owned_summaries",
               "test_enricher_gpu_ownership", "test_everything_reclamation",
               "test_gpu_job", "test_gpu_lock_owner", "test_gpu_owner_death",
               "test_llama_lifecycle_lock", "test_llm_campaign_ownership",
               "test_macos_owner_protocol", "test_macos_probe_lease",
               "test_overnight_cleanup", "test_overnight_llm_campaign",
               "test_owner_disconnect_grace", "test_pdnc_campaign_ownership",
               "test_preparer_restart_lock", "test_reference_spread_reclamation",
               "test_response_log_atomicity", "test_stage_gpu_reclamation",
               "test_voice_library_transactions")

    def gated_classes(self):
        """The reviewed capability rows, read from the registry itself."""
        registry = Path(__file__).with_name("windows_only_skip_baseline.txt")
        gated = {}
        for location, (_digest, declaration) in skip_registry.parse_skip_registry(
                registry.read_text(encoding="utf-8")).items():
            if "predicate=not HAVE_FCNTL" not in declaration:
                continue
            module_file, qualname, _index = location.split(":")
            gated.setdefault(module_file, set()).add(qualname)
        return gated

    def discover_without_fcntl(self, module_name):
        app = Path(__file__).parent.parent
        code = (
            "import importlib,json,sys,unittest\n"
            "sys.modules['fcntl'] = None\n"
            "module = importlib.import_module('tests.' + sys.argv[1])\n"
            "def flatten(suite):\n"
            "    for test in suite:\n"
            "        if isinstance(test, unittest.TestSuite):\n"
            "            yield from flatten(test)\n"
            "        else:\n"
            "            yield test\n"
            "cases = list(flatten(unittest.defaultTestLoader.loadTestsFromModule(module)))\n"
            "print(json.dumps({'have_fcntl': bool(module.HAVE_FCNTL),\n"
            "                  'ids': sorted(case.id() for case in cases),\n"
            "                  'skipped': sorted(case.id() for case in cases\n"
            "                                    if getattr(case.__class__, '__unittest_skip__', False)\n"
            "                                    or getattr(case, '__unittest_skip__', False))}))\n"
        )
        environment = get_isolated_git_environment()
        environment.update(PYTHONPATH=str(app), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-c", code, module_name],
                              cwd=str(app), env=environment, capture_output=True,
                              text=True, timeout=120)

    def test_guarded_modules_keep_every_inventoried_id_without_the_capability(self):
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        gated = self.gated_classes()
        self.assertEqual(26, sum(len(names) for names in gated.values()),
                         "the reviewed capability rows changed; re-read them")
        total = 0
        for module_name in self.MODULES:
            with self.subTest(module=module_name):
                result = self.discover_without_fcntl(module_name)
                self.assertEqual(0, result.returncode, result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report["have_fcntl"],
                                 "the child imported fcntl despite the simulation")
                self.assertEqual(inventory[module_name], report["ids"],
                                 "discovery lost or gained IDs for this module")
                namespace = "tests." + module_name + "."
                for test_id in report["ids"]:
                    qualname = test_id[len(namespace):].rsplit(".", 1)[0]
                    if qualname in gated.get(module_name + ".py", set()):
                        self.assertIn(test_id, report["skipped"],
                                      "a class that needs POSIX locks was not gated")
                total += len(report["ids"])
        expected_total = sum(len(inventory[module_name]) for module_name in self.MODULES)
        self.assertEqual(expected_total, total,
                         "the fcntl modules must account for every inventoried ID")

    def test_discovery_that_cannot_finish_is_exit_two_and_real_drift_is_exit_one(self):
        import contextlib
        import io
        from unittest.mock import patch
        import tests.test_inventory as inventory_module
        import update_test_inventory as updater
        failure = inventory_module.InventoryUnavailable(
            [("tests.test_gpu_job", "missing module 'fcntl'")])
        with patch.object(updater, "find_untracked_test_modules", return_value=[]), \
             patch.object(updater, "get_unit_test_inventory", side_effect=failure):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = updater.main(["--check"])
        self.assertEqual(2, result)
        text = output.getvalue()
        self.assertIn("tests.test_gpu_job", text)
        self.assertIn("missing module 'fcntl'", text)
        self.assertIn("Nothing was compared", text)
        self.assertNotIn("drift", text)
        with patch.object(updater, "find_untracked_test_modules", return_value=[]), \
             patch.object(updater, "check_inventory",
                          return_value=["Missing from inventory: tests.x.T.test_one"]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = updater.main(["--check"])
        self.assertEqual(1, result)
        self.assertIn("drift", output.getvalue())

    def test_write_refuses_without_publishing_a_partial_artifact(self):
        import tempfile
        from unittest.mock import patch
        import tests.test_inventory as inventory_module
        import update_test_inventory as updater
        prior = b'{"prior": ["retained"]}'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "inventory.json")
            path.write_bytes(prior)
            with patch.object(updater, "get_unit_test_inventory",
                              side_effect=inventory_module.InventoryUnavailable(
                                  [("tests.test_gpu_job", "missing module 'fcntl'")])):
                with self.assertRaises(inventory_module.InventoryUnavailable):
                    updater.write_inventory(path)
            self.assertEqual(prior, path.read_bytes())
            self.assertEqual([path], list(Path(tmp).iterdir()))

    def test_writer_pins_lf_and_the_committed_artifact_holds_no_crlf(self):
        import tempfile
        from unittest.mock import patch
        import update_test_inventory as updater
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "inventory.json")
            with patch.object(updater, "get_unit_test_inventory",
                              return_value={"test_a": ["a"]}), \
                 patch.object(updater, "atomic_json_write") as writer:
                updater.write_inventory(path)
            self.assertEqual("\n", writer.call_args.kwargs["newline"])
        self.assertNotIn(b"\r", INVENTORY_PATH.read_bytes())


class UncollectedTestGuardTests(unittest.TestCase):
    """A module unittest runs zero tests from must fail discovery, not read as [].

    Thirteen pytest-style modules once imported and discovered cleanly while
    every one of their tests was silently skipped. These fixtures pin each
    shape that hides tests: a module whose tests are all module-level
    functions, a module that mixes a TestCase class with bare functions so the
    class hides them, a bare function whose name merely matches a collected
    method, a bare function inside an ``if``/``try``/``with``/loop arm or a
    ``match`` case, a bare function a ``load_tests`` wrapper forgot, a
    pytest-style plain class beside a TestCase, a test defined under
    ``if __name__ == "__main__":``, an ``async`` test method on a plain
    TestCase whose coroutine is never awaited, and two such problems in one
    module both being reported.
    """

    def run_discovery(self, name, source):
        import tests
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / f"{name}.py").write_text(source, encoding="utf-8")
            original = tests.__path__
            tests.__path__ = [str(root)]
            try:
                return get_unit_test_inventory(root)
            except InventoryUnavailable as error:
                return error
            finally:
                tests.__path__ = original
                sys.modules.pop(f"tests.{name}", None)

    def failure_reason(self, module_name, result):
        self.assertIsInstance(result, InventoryUnavailable,
                              f"{module_name} was not rejected: {result!r}")
        reasons = [reason for name, reason in result.failures
                   if name == "tests." + module_name]
        self.assertTrue(reasons, f"{module_name} was not rejected: {result!r}")
        return "\n".join(reasons)

    def test_module_with_only_bare_test_functions_is_rejected(self):
        result = self.run_discovery(
            "test_zz_guard_bare_only",
            "def test_alpha():\n    assert True\n\n\ndef test_beta():\n    pass\n")
        reason = self.failure_reason("test_zz_guard_bare_only", result)
        self.assertIn("module-level test functions", reason)
        self.assertIn("test_alpha", reason)
        self.assertIn("test_beta", reason)
        # A helper that is not a test is rejected by naming rule, so the error
        # has to tell the author how to fix it rather than just naming it.
        self.assertIn("rename", reason)

    def test_async_method_on_a_plain_test_case_is_rejected(self):
        result = self.run_discovery(
            "test_zz_guard_async",
            "import unittest\n\n\n"
            "class NotAsync(unittest.TestCase):\n"
            "    async def test_never_awaited(self):\n        pass\n")
        reason = self.failure_reason("test_zz_guard_async", result)
        self.assertIn("never awaited", reason)
        self.assertIn("test_never_awaited", reason)

    def test_async_method_on_isolated_asyncio_test_case_is_accepted(self):
        result = self.run_discovery(
            "test_zz_guard_isolated",
            "import unittest\n\n\n"
            "class Async(unittest.IsolatedAsyncioTestCase):\n"
            "    async def test_awaited(self):\n        pass\n")
        self.assertEqual(
            {"test_zz_guard_isolated":
             ["tests.test_zz_guard_isolated.Async.test_awaited"]}, result)

    def test_bare_function_hidden_under_main_is_rejected(self):
        result = self.run_discovery(
            "test_zz_guard_under_main",
            "import unittest\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    def test_hidden():\n        pass\n")
        reason = self.failure_reason("test_zz_guard_under_main", result)
        self.assertIn("test_hidden", reason)

    def test_load_tests_that_forgets_a_bare_function_is_rejected(self):
        result = self.run_discovery(
            "test_zz_guard_orphan",
            "import unittest\n\n\n"
            "def test_included():\n    pass\n\n\n"
            "def test_orphan():\n    pass\n\n\n"
            "def load_tests(loader, tests, pattern):\n"
            "    suite = unittest.TestSuite()\n"
            "    suite.addTest(unittest.FunctionTestCase(test_included))\n"
            "    return suite\n")
        reason = self.failure_reason("test_zz_guard_orphan", result)
        self.assertIn("test_orphan", reason)
        self.assertNotIn("test_included", reason)

    def test_module_mixing_a_class_and_bare_function_is_rejected(self):
        result = self.run_discovery(
            "test_zz_guard_mixed",
            "import unittest\n\n\n"
            "class Collected(unittest.TestCase):\n"
            "    def test_inside(self):\n        pass\n\n\n"
            "def test_outside():\n    pass\n")
        reason = self.failure_reason("test_zz_guard_mixed", result)
        self.assertIn("test_outside", reason)
        self.assertNotIn("test_inside", reason)

    def test_class_method_does_not_excuse_a_same_named_bare_function(self):
        # Matching only the last id component let T.test_x vouch for a
        # module-level test_x, so unittest ran one and silently dropped the
        # other. A load_tests returning the ordinary suite changes nothing.
        for label, extra in (
                ("plain", ""),
                ("with_load_tests",
                 "\n\ndef load_tests(loader, tests, pattern):\n"
                 "    return tests\n")):
            with self.subTest(label):
                name = f"test_zz_guard_collision_{label}"
                result = self.run_discovery(
                    name,
                    "import unittest\n\n\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_x(self):\n        pass\n\n\n"
                    "def test_x():\n    assert False\n" + extra)
                reason = self.failure_reason(name, result)
                self.assertIn("module-level test functions", reason)
                self.assertIn("test_x", reason)

    def test_bare_function_hidden_in_compound_statements_is_rejected(self):
        cases = {
            "if_body": "if True:\n    def test_hidden(): pass\n",
            "if_else": "if False:\n    pass\nelse:\n    def test_hidden(): pass\n",
            "try_body": "try:\n    def test_hidden(): pass\nexcept Exception:\n    pass\n",
            "try_except": "try:\n    pass\nexcept Exception:\n    def test_hidden(): pass\n",
            "try_else": "try:\n    pass\nexcept Exception:\n    pass\nelse:\n    def test_hidden(): pass\n",
            "try_finally": "try:\n    pass\nfinally:\n    def test_hidden(): pass\n",
            "with": "with open(__file__):\n    def test_hidden(): pass\n",
            "for_body": "for _ in ():\n    def test_hidden(): pass\n",
            "for_else": "for _ in ():\n    pass\nelse:\n    def test_hidden(): pass\n",
            "while_body": "while False:\n    def test_hidden(): pass\n",
            "while_else": "while False:\n    pass\nelse:\n    def test_hidden(): pass\n",
        }
        for label, body in cases.items():
            with self.subTest(label):
                name = f"test_zz_guard_hidden_{label}"
                result = self.run_discovery(name, body)
                reason = self.failure_reason(name, result)
                self.assertIn("test_hidden", reason)

    def test_bare_function_hidden_in_a_match_case_is_rejected(self):
        result = self.run_discovery(
            "test_zz_guard_match",
            "import unittest\n\n\n"
            "class Real(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n\n\n"
            "match 1:\n"
            "    case 1:\n"
            "        def test_hidden(): pass\n")
        reason = self.failure_reason("test_zz_guard_match", result)
        self.assertIn("test_hidden", reason)
        self.assertNotIn("test_ok", reason)

    def test_pytest_style_class_beside_a_test_case_is_rejected(self):
        result = self.run_discovery(
            "test_zz_guard_pytest_class",
            "import unittest\n\n\n"
            "class TestPy:\n"
            "    def test_hidden(self):\n        assert False\n\n\n"
            "class Real(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n")
        reason = self.failure_reason("test_zz_guard_pytest_class", result)
        self.assertIn("subclass unittest.TestCase", reason)
        self.assertIn("TestPy", reason)
        self.assertIn("test_hidden", reason)
        self.assertNotIn("test_ok", reason)

    def test_pytest_style_class_not_named_test_is_rejected(self):
        result = self.run_discovery(
            "test_zz_guard_pytest_named_other",
            "import unittest\n\n\n"
            "class HiddenCases:\n"
            "    def test_hidden(self):\n        assert False\n\n\n"
            "class Real(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n")
        reason = self.failure_reason("test_zz_guard_pytest_named_other", result)
        self.assertIn("HiddenCases", reason)
        self.assertIn("test_hidden", reason)
        self.assertNotIn("test_ok", reason)

    def test_plain_class_named_test_without_test_methods_is_rejected(self):
        result = self.run_discovery(
            "test_zz_guard_test_named_class",
            "import unittest\n\n\n"
            "class TestHelper:\n    VALUE = 1\n\n\n"
            "class Real(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n")
        reason = self.failure_reason("test_zz_guard_test_named_class", result)
        self.assertIn("TestHelper", reason)
        self.assertNotIn("test_ok", reason)

    def test_two_problems_in_one_module_are_both_reported(self):
        result = self.run_discovery(
            "test_zz_guard_two_problems",
            "import unittest\n\n\n"
            "def test_bare():\n    pass\n\n\n"
            "class NotAsync(unittest.TestCase):\n"
            "    async def test_never(self):\n        pass\n")
        reason = self.failure_reason("test_zz_guard_two_problems", result)
        self.assertIn("test_bare", reason)
        self.assertIn("test_never", reason)

    def test_module_that_yields_no_tests_is_rejected(self):
        result = self.run_discovery(
            "test_zz_guard_empty", "VALUE = 1\n\n\ndef helper():\n    return VALUE\n")
        reason = self.failure_reason("test_zz_guard_empty", result)
        self.assertIn("module yields no unittest tests", reason)

    def test_load_tests_wrapping_still_collects_and_is_not_flagged(self):
        # The issue allows unittest.FunctionTestCase via load_tests; those
        # functions ARE collected, so the AST scan must not reject them.
        result = self.run_discovery(
            "test_zz_guard_load_tests",
            "import unittest\n\n\n"
            "def test_wrapped():\n    assert True\n\n\n"
            "def load_tests(loader, tests, pattern):\n"
            "    suite = unittest.TestSuite()\n"
            "    suite.addTest(unittest.FunctionTestCase(test_wrapped))\n"
            "    return suite\n")
        self.assertEqual({"test_zz_guard_load_tests": ["test_wrapped"]}, result)

    def test_excluded_module_is_not_scanned_or_inventoried(self):
        import tests.test_inventory as inventory_module
        from unittest.mock import patch
        with patch.object(inventory_module, "EXCLUDED_TEST_MODULES",
                          {"test_zz_guard_excluded"}):
            result = self.run_discovery(
                "test_zz_guard_excluded", "def test_script_style():\n    pass\n")
        self.assertEqual({}, result)

    def test_update_check_fails_on_a_bare_function_readded_to_a_real_module(self):
        """The end-to-end mutation: a real module gaining a bare test fails."""
        import contextlib
        import io
        import tests as tests_package
        import update_test_inventory as updater
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_zz_guard_real.py").write_text(
                "import unittest\n\n\n"
                "class Real(unittest.TestCase):\n"
                "    def test_kept(self):\n        pass\n\n\n"
                "def test_readded():\n    pass\n",
                encoding="utf-8")
            inventory = root / "unit_test_inventory.json"
            inventory.write_text(
                '{"test_zz_guard_real": ["tests.test_zz_guard_real.Real.test_kept"]}\n',
                encoding="utf-8")
            original = tests_package.__path__
            tests_package.__path__ = [str(root)]
            try:
                def current_differences():
                    return updater.get_inventory_differences(
                        json.loads(inventory.read_text(encoding="utf-8")),
                        get_unit_test_inventory(root))
                with patch.object(updater, "find_untracked_test_modules",
                                  return_value=[]), \
                     patch.object(updater, "check_inventory",
                                  side_effect=current_differences):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        result = updater.main(["--check"])
                self.assertIsInstance(result, int)
                self.assertEqual(2, result, output.getvalue())
                text = output.getvalue()
                self.assertIn("tests.test_zz_guard_real", text)
                self.assertIn("test_readded", text)
                self.assertIn("Nothing was compared", text)
            finally:
                tests_package.__path__ = original
                sys.modules.pop("tests.test_zz_guard_real", None)


if __name__ == "__main__":
    unittest.main()

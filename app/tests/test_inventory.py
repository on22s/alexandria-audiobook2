import importlib
import json
import os
from pathlib import Path
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
            cwd=get_test_directory(test_directory), capture_output=True, timeout=10)
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


def get_unit_test_inventory(test_directory=None):
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
        inventory[module_name] = sorted(test.id() for test in _iter_tests(suite))
    if failures:
        raise InventoryUnavailable(failures)
    return inventory


class TestInventoryTests(unittest.TestCase):
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
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.directory / "test_fixture.py").write_text(
            "import unittest\nclass Fixture(unittest.TestCase):\n"
            "    def test_tracked(self):\n        pass\n", encoding="utf-8")
        (self.directory / "test_api.py").write_text("raise RuntimeError('excluded')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "tests/test_fixture.py", "tests/test_api.py"], check=True)

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
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
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
                environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
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
            subprocess.run(['git','init','-q'],cwd=root,check=True)
            (tests/'test_tracked.py').write_text('# tracked fixture\n',encoding='utf-8')
            (tests/'test_new.py').write_text('# new local fixture\n',encoding='utf-8')
            (tests/'test_api.py').write_text('# explicitly excluded\n',encoding='utf-8')
            subprocess.run(['git','add','app/tests/test_tracked.py'],cwd=root,check=True)
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
        for line in registry.read_text(encoding="utf-8").splitlines():
            location, separator, declaration = line.partition(" = ")
            if not separator or "predicate=not HAVE_FCNTL" not in declaration:
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
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_")}
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


if __name__ == "__main__":
    unittest.main()

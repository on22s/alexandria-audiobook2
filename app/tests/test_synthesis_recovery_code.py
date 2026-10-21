"""Live function changes cannot be mislabeled by rereading a newer disk file."""
from functools import wraps
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import synthesis_recovery_code as code
from synthesis_recovery_identity import RecoveryIdentityUnavailable


def fixture_function(value=1, *, enabled=True):
    return value if enabled else None


class RecoveryCodeIdentityTests(unittest.TestCase):
    def digest(self, callback, **kwargs):
        return code.get_live_callable_sha256({'entry': callback}, code_files=[__file__], **kwargs)

    def test_loaded_code_identity_survives_disk_edit_and_distinguishes_new_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'serving.py'
            header = ('from loaded_module_code import get_current_loaded_module_code_sha256\n'
                      'MODULE_SHA = get_current_loaded_module_code_sha256()\n')
            source.write_text(header + 'def serve():\n return 1\n')
            old = {}
            exec(compile(source.read_text(), str(source), 'exec'), old)
            first = code.get_live_callable_sha256({'serve': old['serve']}, code_files=[source])
            old_module_identity = old['MODULE_SHA']
            source.write_text(header + 'def serve():\n return 2\n')
            self.assertEqual(first, code.get_live_callable_sha256({'serve': old['serve']}, code_files=[source]))
            new = {}
            exec(compile(source.read_text(), str(source), 'exec'), new)
            self.assertEqual(old_module_identity, old['MODULE_SHA'])
            self.assertNotEqual(old_module_identity, new['MODULE_SHA'])
            self.assertNotEqual(first, code.get_live_callable_sha256({'serve': new['serve']}, code_files=[source]))
            self.assertEqual((1, 2), (old['serve'](), new['serve']()))

    def test_actual_defaults_and_wrapped_closure_changes_alter_identity(self):
        def wrapper(function, factor):
            @wraps(function)
            def wrapped(*args, **kwargs):
                return factor * function(*args, **kwargs)
            return wrapped
        first = self.digest(wrapper(fixture_function, 2))
        self.assertEqual(first, self.digest(wrapper(fixture_function, 2)))
        self.assertNotEqual(first, self.digest(wrapper(fixture_function, 3)))
        original = fixture_function.__defaults__
        try:
            before = self.digest(fixture_function)
            fixture_function.__defaults__ = (2,)
            self.assertNotEqual(before, self.digest(fixture_function))
        finally:
            fixture_function.__defaults__ = original

    def test_exact_bound_role_is_required_but_object_id_is_not_semantic(self):
        class Decoder:
            def decode(self, value=1):
                return value
        first, second = Decoder(), Decoder()
        with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'unknown_live_binding'):
            self.digest(first.decode)
        self.assertEqual(self.digest(first.decode, bound_roles=[(first, 'decoder')]),
                         self.digest(second.decode, bound_roles=[(second, 'decoder')]))
        with self.assertRaises(RecoveryIdentityUnavailable):
            self.digest(first.decode, bound_roles=[(second, 'decoder')])

    def test_unknown_objects_callables_code_files_and_unbounded_values_are_refused(self):
        def captured(value):
            def closure():
                return value
            return closure
        for value in (object(), float('nan'), 'x' * (code.MAX_TYPED_STRING + 1)):
            with self.subTest(kind=type(value).__name__), self.assertRaises(RecoveryIdentityUnavailable):
                self.digest(captured(value))
        with self.assertRaises(RecoveryIdentityUnavailable):
            code.get_live_callable_sha256({'entry': fixture_function}, code_files=['unrelated.py'])
        with self.assertRaises(RecoveryIdentityUnavailable):
            self.digest(len)
        with patch.object(code, 'MAX_LIVE_CODE_BYTES', 1):
            with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'live_code_limit'):
                self.digest(fixture_function)

    def test_typed_state_and_manifest_order_are_stable_and_changes_invalidate(self):
        functions = {'second': fixture_function, 'first': fixture_function}
        first = code.get_live_callable_sha256(functions, code_files=[__file__], state={'seed': 42, 'temperature': .9})
        self.assertEqual(first, code.get_live_callable_sha256(dict(reversed(list(functions.items()))),
                         code_files=[__file__], state={'temperature': .9, 'seed': 42}))
        self.assertNotEqual(first, code.get_live_callable_sha256(functions, code_files=[__file__],
                           state={'temperature': .9, 'seed': 43}))

    def test_native_config_integer_keys_are_typed_bounded_and_order_independent(self):
        first = {0: 'LABEL_0', 1: 'LABEL_1', 'policy': {'enabled': True}}
        digest = self.digest(fixture_function, state=first)
        self.assertEqual(digest, self.digest(fixture_function, state=dict(reversed(list(first.items())))))
        self.assertNotEqual(digest, self.digest(fixture_function,
            state={'0': 'LABEL_0', '1': 'LABEL_1', 'policy': {'enabled': True}}))
        self.assertNotEqual(self.digest(fixture_function, state={1: 'value'}),
            self.digest(fixture_function, state={'mapping': [[1, 'value']]}))
        for key in (True, 1.0, 2**129, object(), 'x' * (code.MAX_TYPED_STRING + 1)):
            with self.subTest(key_type=type(key).__name__), self.assertRaises(RecoveryIdentityUnavailable):
                self.digest(fixture_function, state={key: 'value'})

    def test_module_capture_rejects_a_later_nonmodule_call(self):
        from loaded_module_code import get_current_loaded_module_code_sha256
        with self.assertRaisesRegex(ValueError, 'during module execution'):
            get_current_loaded_module_code_sha256()

    def test_declared_super_class_and_enum_defaults_are_typed_and_whitelisted(self):
        from enum import Enum
        class Strategy(str, Enum):
            FIRST = 'first'
            SECOND = 'second'
        class Base:
            def run(self):
                return 1
        class Child(Base):
            def run(self, strategy=Strategy.FIRST):
                return super().run(), strategy
        callbacks = {'base': Base.run, 'child': Child.run}
        with self.assertRaises(RecoveryIdentityUnavailable):
            code.get_live_callable_sha256(callbacks, code_files=[__file__])
        kwargs = dict(code_files=[__file__], class_roles=[(Child, 'child_class')],
                      enum_roles=[(Strategy, 'strategy_enum')])
        first = code.get_live_callable_sha256(callbacks, **kwargs)
        Child.run.__defaults__ = (Strategy.SECOND,)
        self.assertNotEqual(first, code.get_live_callable_sha256(callbacks, **kwargs))

    def test_cumulative_value_bounds_stop_aliased_container_expansion(self):
        state = ['small']
        for _ in range(12):
            state = [state, state]
        with patch.object(code, 'MAX_TYPED_NODES', 100):
            with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'live_value_nodes_limit'):
                self.digest(fixture_function, state=state)
        with patch.object(code, 'MAX_TYPED_BYTES', 100):
            with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'live_value_bytes_limit'):
                self.digest(fixture_function, state=['x' * 50] * 10)

    def test_ambiguous_role_names_or_multiple_names_for_same_object_are_refused(self):
        first, second = object(), object()
        for roles in ([(first, 'same'), (second, 'same')], [(first, 'one'), (first, 'two')]):
            with self.subTest(roles=roles), self.assertRaisesRegex(RecoveryIdentityUnavailable, 'ambiguous_live_roles'):
                self.digest(fixture_function, bound_roles=roles)

    def test_logit_mask_infinity_is_typed_identity_while_nan_stays_unproven(self):
        def masked(value=float('-inf')):
            return value
        first = self.digest(masked)
        masked.__defaults__ = (float('inf'),)
        self.assertNotEqual(first, self.digest(masked))
        masked.__defaults__ = (float('nan'),)
        with self.assertRaises(RecoveryIdentityUnavailable):
            self.digest(masked)

    def test_restart_digest_is_stable_across_import_history_and_hash_seeds(self):
        import json
        import os
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'loaded_serving.py'
            source.write_text('''import json
from loaded_module_code import get_current_loaded_module_code_sha256
from synthesis_recovery_code import get_live_callable_sha256
def serve(value=1):
 return value in {'alpha', 'beta', 'gamma'}
module_sha = get_current_loaded_module_code_sha256()
callable_sha = get_live_callable_sha256({'entry': serve}, code_files=[__file__])
print(json.dumps({'module': module_sha, 'callable': callable_sha}, sort_keys=True))
''')
            results = []
            for seed, history in (('1', ''), ('2', 'import unittest.mock\n'), ('3', 'import pickle, dataclasses\n')):
                program = history + 'import runpy, sys\nrunpy.run_path(sys.argv[1])\n'
                child = subprocess.run([sys.executable, '-c', program, str(source)],
                    env=dict(os.environ, PYTHONHASHSEED=seed), capture_output=True, text=True,
                    check=True, timeout=10)
                results.append(json.loads(child.stdout))
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[0], results[2])
            self.assertTrue(all(len(value) == 64 for value in results[0].values()))

    def test_structural_code_hash_bounds_aliased_constants_and_is_optional_at_import(self):
        from loaded_module_code import get_loaded_code_identity, LoadedCodeIdentityUnavailable
        import loaded_module_code as loaded
        value = ('small',)
        for _ in range(12):
            value = (value, value)
        changed = fixture_function.__code__.replace(co_consts=(value,))
        with patch.object(loaded, 'MAX_LOADED_CODE_NODES', 100):
            with self.assertRaisesRegex(LoadedCodeIdentityUnavailable, 'structure limit'):
                get_loaded_code_identity(changed)
        with patch.object(loaded, 'MAX_LOADED_CODE_BYTES', 1):
            with self.assertRaisesRegex(LoadedCodeIdentityUnavailable, 'byte limit'):
                get_loaded_code_identity(fixture_function.__code__)
            namespace = {}
            exec('from loaded_module_code import get_current_loaded_module_code_sha256\n'
                 'identity = get_current_loaded_module_code_sha256()\n', namespace)
            self.assertIsNone(namespace['identity'])

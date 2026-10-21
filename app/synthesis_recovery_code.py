"""Bounded descriptors of loaded Python serving functions, never fresh disk code.

A native integration supplies its explicit code-file allowlist and object roles.
Unknown callable objects, closure objects and defaults are unproven. The declared
runtime/native build contract is separate from these Python function identities.
"""
import math
import os
from types import FunctionType, MethodType
from enum import Enum
from loaded_module_code import get_loaded_code_identity, LoadedCodeIdentityUnavailable

from synthesis_recovery_identity import RecoveryIdentityUnavailable, get_recovery_digest

MAX_LIVE_FUNCTIONS = 4096
MAX_LIVE_CODE_BYTES = 32 * 1024 * 1024
MAX_TYPED_ITEMS = 2048
MAX_TYPED_STRING = 65536
MAX_DESCRIPTOR_DEPTH = 32
MAX_TYPED_NODES = 16384
MAX_TYPED_BYTES = 4 * 1024 * 1024


class _CallableDescriptor:
    def __init__(self, code_files, bound_roles, class_roles, enum_roles):
        self.code_files = frozenset(os.path.normcase(os.path.abspath(path)) for path in code_files)
        self.bound_roles = tuple(bound_roles)
        self.class_roles = tuple(class_roles)
        self.enum_roles = tuple(enum_roles)
        self.functions = {}
        self.code_bytes = 0
        self.value_nodes = 0
        self.value_bytes = 0

    def _charge_text(self, value):
        self.value_bytes += len(value.encode('utf-8', errors='surrogatepass'))
        if self.value_bytes > MAX_TYPED_BYTES:
            raise RecoveryIdentityUnavailable('live_value_bytes_limit')
        return value

    def encode_value(self, value, depth=0):
        self.value_nodes += 1
        if self.value_nodes > MAX_TYPED_NODES:
            raise RecoveryIdentityUnavailable('live_value_nodes_limit')
        if depth > MAX_DESCRIPTOR_DEPTH:
            raise RecoveryIdentityUnavailable('live_descriptor_depth_limit')
        if value is None or type(value) in (bool, int):
            if type(value) is int and value.bit_length() > 128:
                raise RecoveryIdentityUnavailable('live_integer_limit')
            return value
        # Native logits processors use -infinity as the exact masked-token
        # default. It is typed identity data, never an allowed PCM sample.
        if type(value) is float and not math.isnan(value):
            return {'float': value.hex()}
        if type(value) is str and len(value) <= MAX_TYPED_STRING:
            return self._charge_text(value)
        if type(value) in (tuple, list) and len(value) <= MAX_TYPED_ITEMS:
            return {type(value).__name__: [self.encode_value(item, depth + 1) for item in value]}
        if (type(value) is dict and len(value) <= MAX_TYPED_ITEMS
                and all(type(key) is str and len(key) <= MAX_TYPED_STRING for key in value)):
            return {'dict': {self._charge_text(key): self.encode_value(item, depth + 1)
                             for key, item in sorted(value.items())}}
        if (type(value) is dict and len(value) <= MAX_TYPED_ITEMS
                and all(type(key) is int and key.bit_length() <= 128 or
                        type(key) is str and len(key) <= MAX_TYPED_STRING for key in value)):
            # Native config label/layer maps use integer keys. Keep key types
            # explicit: 1 must never collide with "1" or a user-shaped dict.
            ordered = sorted(value.items(), key=lambda item: (type(item[0]) is int, item[0]))
            return {'mapping': [[self.encode_value(key, depth + 1), self.encode_value(item, depth + 1)]
                                for key, item in ordered]}
        if type(value) in (FunctionType, MethodType):
            return self.encode_callable(value, depth + 1)
        for cls, role in self.class_roles:
            if value is cls:
                # super() captures __class__; native integration supplies only
                # declared classes whose participating methods are bound too.
                return {'declared_class': role}
        for cls, role in self.enum_roles:
            if type(value) is cls:
                return {'declared_enum': role, 'member': value.name,
                        'value': self.encode_value(value.value, depth + 1)}
        raise RecoveryIdentityUnavailable('unsupported_live_value')

    def encode_callable(self, callback, depth=0):
        if depth > MAX_DESCRIPTOR_DEPTH:
            raise RecoveryIdentityUnavailable('live_descriptor_depth_limit')
        role = None
        if type(callback) is MethodType:
            for bound, name in self.bound_roles:
                if callback.__self__ is bound:
                    role = name
                    break
            if role is None:
                # Classmethod binding is structural; arbitrary instance methods
                # require an explicit captured model/context role.
                if type(callback.__self__) is type:
                    role = callback.__self__.__module__ + '.' + callback.__self__.__qualname__
                else:
                    raise RecoveryIdentityUnavailable('unknown_live_binding')
            callback = callback.__func__
        if type(callback) is not FunctionType:
            raise RecoveryIdentityUnavailable('unknown_live_callable')
        code_file = os.path.normcase(os.path.abspath(callback.__code__.co_filename))
        if code_file not in self.code_files:
            raise RecoveryIdentityUnavailable('unqualified_live_code')
        reference = self.functions.get(callback)
        if reference is not None:
            return {'function': reference, 'bound_role': role}
        if len(self.functions) >= MAX_LIVE_FUNCTIONS:
            raise RecoveryIdentityUnavailable('live_function_limit')
        reference = len(self.functions)
        self.functions[callback] = reference
        try:
            code_sha256, code_bytes = get_loaded_code_identity(callback.__code__)
        except LoadedCodeIdentityUnavailable as error:
            raise RecoveryIdentityUnavailable('unsupported_loaded_code') from error
        self.code_bytes += code_bytes
        if self.code_bytes > MAX_LIVE_CODE_BYTES:
            raise RecoveryIdentityUnavailable('live_code_limit')
        cells = callback.__closure__ or ()
        if len(cells) != len(callback.__code__.co_freevars):
            raise RecoveryIdentityUnavailable('invalid_live_closure')
        try:
            closure = {name: self.encode_value(cell.cell_contents, depth + 1)
                       for name, cell in zip(callback.__code__.co_freevars, cells)}
        except ValueError as error:
            raise RecoveryIdentityUnavailable('empty_live_closure') from error
        wrapped = getattr(callback, '__wrapped__', None)
        return {'function': reference, 'bound_role': role, 'module': callback.__module__,
                'qualname': callback.__qualname__, 'code_sha256': code_sha256,
                'defaults': self.encode_value(callback.__defaults__, depth + 1),
                'kwdefaults': self.encode_value(callback.__kwdefaults__, depth + 1),
                'closure': closure,
                'wrapped': self.encode_callable(wrapped, depth + 1) if wrapped is not None else None}


def get_live_callable_sha256(callables, *, code_files, bound_roles=(), class_roles=(), enum_roles=(), state=None):
    """Hash a declared serving graph, with deterministic names and typed state.

    bound_roles is a short sequence of (actual captured object, stable role name).
    Object identities select bindings in memory; they are never persisted keys.
    """
    if (type(callables) is not dict or not callables or len(callables) > MAX_LIVE_FUNCTIONS
            or any(type(name) is not str or len(name) > 1024 for name in callables)
            or not code_files or len(code_files) > MAX_LIVE_FUNCTIONS
            or len(bound_roles) > MAX_LIVE_FUNCTIONS
            or len(class_roles) > MAX_LIVE_FUNCTIONS
            or len(enum_roles) > MAX_LIVE_FUNCTIONS
            or any(type(role) is not str or not role or len(role) > 1024
                   for _, role in (*bound_roles, *class_roles, *enum_roles))
            or any(not isinstance(cls, type) for cls, _ in class_roles)
            or any(not isinstance(cls, type) or not issubclass(cls, Enum) for cls, _ in enum_roles)):
        raise RecoveryIdentityUnavailable('invalid_live_manifest')
    roles = (*bound_roles, *class_roles, *enum_roles)
    if (len({name for _, name in roles}) != len(roles)
            or any(len({id(obj) for obj, _ in family}) != len(family)
                   for family in (bound_roles, class_roles, enum_roles))):
        raise RecoveryIdentityUnavailable('ambiguous_live_roles')
    encoder = _CallableDescriptor(code_files, bound_roles, class_roles, enum_roles)
    functions = {name: encoder.encode_callable(callback) for name, callback in sorted(callables.items())}
    return get_recovery_digest({'version': 1, 'functions': functions, 'state': encoder.encode_value(state)})


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()

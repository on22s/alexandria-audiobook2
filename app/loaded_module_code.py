"""Deterministic identity of code actually loaded, independent of marshal refs."""
import hashlib
import struct
import sys
from types import CodeType

MAX_LOADED_CODE_BYTES = 32 * 1024 * 1024
MAX_LOADED_CODE_NODES = 65536
MAX_LOADED_CODE_DEPTH = 128


class LoadedCodeIdentityUnavailable(ValueError):
    """Unsupported or oversized loaded code; optional recovery must stay off."""


def get_loaded_code_identity(code):
    """Return (digest, encoded bytes) without reading source or object repr.

    marshal's reference flags can depend on import history/refcounts. Encode
    explicit immutable code fields/constants instead, with cumulative bounds.
    Native/Python build compatibility is additionally bound by the caller.
    """
    if type(code) is not CodeType:
        raise LoadedCodeIdentityUnavailable('Expected a loaded code object')
    budget = {'bytes': 0, 'nodes': 0}

    def write(sink, tag, value=b''):
        prefix = tag + str(len(value)).encode('ascii') + b':'
        budget['bytes'] += len(prefix) + len(value)
        if budget['bytes'] > MAX_LOADED_CODE_BYTES:
            raise LoadedCodeIdentityUnavailable('Loaded code byte limit')
        sink.update(prefix)
        sink.update(value)

    def visit(value, sink, depth=0):
        budget['nodes'] += 1
        if budget['nodes'] > MAX_LOADED_CODE_NODES or depth > MAX_LOADED_CODE_DEPTH:
            raise LoadedCodeIdentityUnavailable('Loaded code structure limit')
        kind = type(value)
        if value is None:
            write(sink, b'none')
        elif value is Ellipsis:
            write(sink, b'ellipsis')
        elif kind is bool:
            write(sink, b'bool', b'1' if value else b'0')
        elif kind is int:
            size = (value.bit_length() + 7) // 8
            if size > MAX_LOADED_CODE_BYTES:
                raise LoadedCodeIdentityUnavailable('Loaded code integer limit')
            write(sink, b'int-' if value < 0 else b'int+', abs(value).to_bytes(size, 'big'))
        elif kind is float:
            write(sink, b'float', struct.pack('>d', value))
        elif kind is complex:
            write(sink, b'complex', struct.pack('>dd', value.real, value.imag))
        elif kind is str:
            if len(value) > MAX_LOADED_CODE_BYTES // 4:
                raise LoadedCodeIdentityUnavailable('Loaded code string limit')
            write(sink, b'str', value.encode('utf-8', errors='surrogatepass'))
        elif kind is bytes:
            write(sink, b'bytes', value)
        elif kind in (tuple, frozenset):
            if len(value) > MAX_LOADED_CODE_NODES:
                raise LoadedCodeIdentityUnavailable('Loaded code container limit')
            write(sink, b'tuple' if kind is tuple else b'frozenset', str(len(value)).encode('ascii'))
            if kind is tuple:
                for item in value:
                    visit(item, sink, depth + 1)
            else:
                members = []
                for item in value:
                    member = hashlib.sha256()
                    visit(item, member, depth + 1)  # Same cumulative work budget.
                    members.append(member.digest())
                for member in sorted(members):
                    write(sink, b'member', member)
        elif kind is CodeType:
            write(sink, b'code-v1')
            # Each position has a fixed meaning; missing newer-Python fields
            # are explicit None, not an object-dependent fallback repr.
            fields = (
                value.co_argcount, getattr(value, 'co_posonlyargcount', 0), value.co_kwonlyargcount,
                value.co_nlocals, value.co_stacksize, value.co_flags, value.co_code,
                value.co_consts, value.co_names, value.co_varnames, value.co_freevars,
                value.co_cellvars, value.co_filename, value.co_name, getattr(value, 'co_qualname', None),
                value.co_firstlineno, getattr(value, 'co_linetable', value.co_lnotab),
                getattr(value, 'co_exceptiontable', None),
            )
            visit(fields, sink, depth + 1)
        else:
            raise LoadedCodeIdentityUnavailable('Unsupported loaded code constant')

    digest = hashlib.sha256()
    write(digest, b'aa2-loaded-python-code-v1')
    visit(code, digest)
    return digest.hexdigest(), budget['bytes']


def get_current_loaded_module_code_sha256():
    code = sys._getframe(1).f_code
    if code.co_name != '<module>':
        raise ValueError('Loaded module identity must be captured during module execution')
    try:
        return get_loaded_code_identity(code)[0]
    except LoadedCodeIdentityUnavailable:
        # Optional identity must never stop ordinary application startup.
        return None


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()

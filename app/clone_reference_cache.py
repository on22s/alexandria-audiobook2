"""Bounded process-local clone references; cached tensors own CPU storage."""

from collections import OrderedDict
from dataclasses import fields, is_dataclass
import sys


PREPROCESSING_VERSION = 1
PREPARED_MAX_ENTRIES = 16
PREPARED_MAX_BYTES = 64 * 1024 * 1024
PROMPT_MAX_ENTRIES = 32
PROMPT_MAX_BYTES = 64 * 1024 * 1024
_PROMPT_FIELDS = frozenset(('ref_code', 'ref_spk_embedding', 'x_vector_only_mode',
                            'icl_mode', 'ref_text'))


class ReferenceLRU:
    """Caller-serialized cache with explicit payload charges and entry limits.

    Charges include owned payloads and keys. Entry count separately bounds
    mapping overhead; allocator/interpreter overhead is not a memory reading.
    Values stay private to the owner, which must copy before handing them out.
    """

    def __init__(self, max_entries, max_bytes):
        if type(max_entries) is not int or type(max_bytes) is not int or min(max_entries, max_bytes) < 1:
            raise ValueError('Reference cache limits must be positive integers')
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries = OrderedDict()
        self._bytes = 0
        self._hits = self._misses = self._evictions = self._bypasses = 0
        self._cleared_entries = 0

    def lookup(self, key):
        row = self._entries.get(key)
        if row is None:
            self._misses += 1
            return None
        self._entries.move_to_end(key)
        self._hits += 1
        return row[0]

    def store(self, key, value, payload_bytes):
        if (type(key) is not tuple or any(type(item) not in (str, int) for item in key)
                or type(payload_bytes) is not int or payload_bytes < 0):
            raise ValueError('Invalid reference cache entry')
        charged = payload_bytes + sys.getsizeof(key) + sum(sys.getsizeof(item) for item in key)
        if charged > self.max_bytes:
            self._bypasses += 1
            return False
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= previous[1]
        while self._entries and (len(self._entries) >= self.max_entries
                                 or self._bytes + charged > self.max_bytes):
            _, (_, released) = self._entries.popitem(last=False)
            self._bytes -= released
            self._evictions += 1
        self._entries[key] = (value, charged)
        self._bytes += charged
        return True

    def clear(self):
        self._cleared_entries += len(self._entries)
        self._entries.clear()
        self._bytes = 0

    def record_bypass(self):
        self._bypasses += 1

    def snapshot(self):
        return {'hits': self._hits, 'misses': self._misses,
                'evictions': self._evictions, 'bypasses': self._bypasses,
                'cleared_entries': self._cleared_entries,
                'entries': len(self._entries), 'charged_bytes': self._bytes,
                'max_entries': self.max_entries, 'max_bytes': self.max_bytes}


def copy_clone_prompt(prompt, max_bytes=PROMPT_MAX_BYTES):
    """Return an isolated public prompt and charge, or None if unaccountable.

    Only the public five-field dataclass/list contract is cached. Unknown
    provider representations still work through ordinary uncached generation.
    Preflight bounds logical tensor bytes before copying. CPU contiguous copies
    break tensor views, autograd graphs, caller mutation and GPU retention.
    Torch is inspected only if the local provider already imported it.
    """
    torch = sys.modules.get('torch')
    if torch is None or type(prompt) is not list or not 0 < len(prompt) <= 16:
        return None
    charged = sys.getsizeof(prompt)
    for item in prompt:
        if not is_dataclass(item) or isinstance(item, type):
            return None
        try:
            if {field.name for field in fields(item)} != _PROMPT_FIELDS or set(vars(item)) != _PROMPT_FIELDS:
                return None
        except TypeError:
            return None
        values = vars(item)
        if (type(values['x_vector_only_mode']) is not bool or type(values['icl_mode']) is not bool
                or values['ref_text'] is not None and type(values['ref_text']) is not str):
            return None
        charged += sys.getsizeof(item) + sys.getsizeof(values) + sys.getsizeof(values['ref_text'])
        for name in ('ref_code', 'ref_spk_embedding'):
            tensor = values[name]
            if tensor is None and name == 'ref_code':
                continue
            if type(tensor) is not torch.Tensor or tensor.layout != torch.strided:
                return None
            if (tensor.is_quantized or tensor.is_nested
                    or tensor.device.type not in ('cpu', 'cuda', 'mps')
                    or tensor.dtype not in (torch.uint8, torch.int8, torch.int16,
                        torch.int32, torch.int64, torch.bool, torch.float16,
                        torch.bfloat16, torch.float32, torch.float64)):
                return None
            charged += sys.getsizeof(tensor) + tensor.numel() * tensor.element_size()
        if charged > max_bytes:
            return None
    copied = []
    for item in prompt:
        def clone(tensor):
            return (None if tensor is None else tensor.detach().to(
                device='cpu', copy=True, memory_format=torch.contiguous_format))
        # Copy prepared state without running constructor/post-init hooks again.
        try:
            snapshot = object.__new__(type(item))
            destination = object.__getattribute__(snapshot, '__dict__')
        except (TypeError, AttributeError):
            return None
        if type(destination) is not dict or destination:
            return None
        values = vars(item)
        destination.update(ref_code=clone(values['ref_code']),
                           ref_spk_embedding=clone(values['ref_spk_embedding']),
                           x_vector_only_mode=values['x_vector_only_mode'],
                           icl_mode=values['icl_mode'], ref_text=values['ref_text'])
        copied.append(snapshot)
    return copied, charged


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()

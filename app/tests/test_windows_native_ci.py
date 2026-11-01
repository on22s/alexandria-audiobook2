'''Native checks remain explicit, complete and required on the correct runner.'''
import ast
import builtins
import contextlib
import fnmatch
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'app/tests/native_windows_owner_checks.py'
TRANSACTION_SCRIPT = ROOT / 'app/tests/native_windows_transaction_checks.py'
ALIAS_SCRIPT = ROOT / 'app/tests/native_windows_sound_temp_root_checks.py'
SKIP_REGISTRY = ROOT / 'app/tests/windows_only_skip_baseline.txt'
WORKFLOW = ROOT / '.github/workflows/sound-assets.yml'
TRANSACTION_CLASSES = {'NativeWindowsRecoveryQuotaTests': 11, 'NativeWindowsBookStateTests': 5,
                       'NativeWindowsBookMoveTests': 4, 'NativeWindowsPublicationCancellationTests': 3,
                       'NativeWindowsPathAdmissionTests': 2, 'NativeWindowsUploadStageTests': 3}
ALIAS_STEP = 'Verify aliased temporary roots are canonical before asset admission'
NORMAL_STEP = 'Verify descriptor-owned uploads through real decoding and cancellation'
ALIAS_COMMAND = 'python tests/native_windows_sound_temp_root_checks.py'
CSPR_MAX_DEPTH = 12
CSPR_MAX_DEFINITIONS = 24
CSPR_MAX_NODES = 40000
SOURCE_SCAN_MAX_DIRECTORIES = 10000
SOURCE_SCAN_MAX_ENTRIES = 100000
SKIP_KINDS = {('unittest', 'skip'): 'skip', ('unittest', 'skipIf'): 'skipIf',
              ('unittest', 'skipUnless'): 'skipUnless'}
SKIP_TERMINALS = frozenset(('skip', 'skipIf', 'skipUnless', 'skipTest'))
PLATFORM_LITERALS = {'sys.platform': ('win32', 'win'), 'os.name': ('nt', 'nt'),
                     'platform.system': ('Windows', 'Win')}


def _scope_nodes(body, top=None):
    for node in body:
        current = node if top is None else top
        yield node, current
        if isinstance(node, ast.If):
            yield from _scope_nodes(node.body, current)
            yield from _scope_nodes(node.orelse, current)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            yield from _scope_nodes(node.body, current)
            yield from _scope_nodes(node.orelse, current)
        elif isinstance(node, ast.Try):
            yield from _scope_nodes(node.body, current)
            for handler in node.handlers:
                yield from _scope_nodes(handler.body, current)
            yield from _scope_nodes(node.orelse, current)
            yield from _scope_nodes(node.finalbody, current)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _scope_nodes(node.body, current)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _scope_nodes(case.body, current)


def _module_nodes(tree):
    seen = set()
    stack = list(tree.body)
    def walk(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        yield node
        for child in ast.iter_child_nodes(node):
            yield from walk(child)
    for node in tree.body:
        yield from walk(node)


def _canonical(node):
    if isinstance(node, ast.AST):
        fields = []
        for field in node._fields:
            if field in ('type_comment', 'type_params'):
                continue
            fields.append(field + '=' + _canonical(getattr(node, field, None)))
        return type(node).__name__ + '(' + ','.join(fields) + ')'
    if isinstance(node, list):
        return '[' + ','.join(_canonical(item) for item in node) + ']'
    if isinstance(node, tuple):
        return '(' + ','.join(_canonical(item) for item in node) + ')'
    return repr(node)


def _sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return '.'.join(reversed(parts))
    return ''


def _string_literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_paths(name):
    parts = name.split('.')
    if not parts or not parts[0]:
        return []
    if parts[0] == 'tests':
        base = 'app/tests/' + '/'.join(parts[1:]) if len(parts) > 1 else 'app/tests'
        return [base + '.py', base + '/__init__.py']
    stem = 'app/' + '/'.join(parts)
    return [stem + '.py', stem + '/__init__.py',
            'app/tests/' + '/'.join(parts) + '.py',
            'app/tests/' + '/'.join(parts) + '/__init__.py']


class _Scope:
    def __init__(self):
        self.bindings = {}
        self.stars = []


def _collect_bindings(target, body, relpath):
    for node, top in _scope_nodes(body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target_name = alias.name
                local = alias.asname or target_name.split('.')[0]
                target.bindings.setdefault(local, []).append(('module', target_name, top))
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ''
            if node.level:
                package_parts = relpath.split('/')[:-1]
                if package_parts[:2] == ['app', 'tests']:
                    package = '.'.join(['tests'] + package_parts[2:])
                elif package_parts[:1] == ['app']:
                    package = '.'.join(package_parts[1:])
                else:
                    package = '.'.join(package_parts)
                if node.level > 1 and package:
                    package = '.'.join(package.split('.')[:-(node.level - 1)])
                module_name = package if not module_name else package + '.' + module_name
            for alias in node.names:
                if alias.name == '*':
                    target.stars.append(module_name)
                else:
                    target.bindings.setdefault(alias.asname or alias.name, []).append(
                        ('symbol', module_name, alias.name, top))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            target.bindings.setdefault(node.name, []).append(('definition', node, top))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for statement in targets:
                for name in _target_names(statement):
                    target.bindings.setdefault(name, []).append(('assignment', node, top))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for name in _target_names(node.target):
                target.bindings.setdefault(name, []).append(('assignment', node, top))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                for name in _target_names(item.optional_vars):
                    target.bindings.setdefault(name, []).append(('dynamic', node, top))
        elif isinstance(node, ast.Try):
            for handler in node.handlers:
                if handler.name:
                    target.bindings.setdefault(handler.name, []).append(
                        ('dynamic', node, top))
        for named in _owned_named_exprs(node):
            for name in _target_names(named.target):
                target.bindings.setdefault(name, []).append(('dynamic', named, top))


_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_STATEMENT_LISTS = frozenset(('body', 'orelse', 'finalbody', 'handlers', 'cases'))


def _expression_children(node):
    for field, value in ast.iter_fields(node):
        if field in _STATEMENT_LISTS:
            continue
        if isinstance(value, ast.AST):
            yield value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, ast.AST):
                    yield item


def _owned_named_exprs(statement):
    stack = list(_expression_children(statement))
    seen = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, ast.NamedExpr):
            yield node
        if isinstance(node, _SCOPE_BOUNDARIES):
            continue
        stack.extend(_expression_children(node))


_UNSUPPORTED = object()


def _assigned_value(node, local):
    if local is None:
        return _UNSUPPORTED
    if isinstance(node, ast.Assign):
        for target in node.targets:
            found = _unpacked_value(target, node.value, local)
            if found is not None:
                return found
        return _UNSUPPORTED
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and node.target.id == local \
                and node.value is not None:
            return node.value
        return _UNSUPPORTED
    return _UNSUPPORTED


def _unpacked_value(target, value, local):
    if isinstance(target, ast.Name):
        return value if target.id == local else None
    if isinstance(target, (ast.Tuple, ast.List)):
        if not isinstance(value, (ast.Tuple, ast.List)):
            return _UNSUPPORTED if local in _target_names(target) else None
        starred = (any(isinstance(item, ast.Starred) for item in target.elts)
                   or any(isinstance(item, ast.Starred) for item in value.elts))
        if starred or len(target.elts) != len(value.elts):
            return _UNSUPPORTED if local in _target_names(target) else None
        for target_item, value_item in zip(target.elts, value.elts):
            found = _unpacked_value(target_item, value_item, local)
            if found is not None:
                return found
        return None
    return _UNSUPPORTED if local in _target_names(target) else None


def _class_defs(body, prefix):
    for node, _top in _scope_nodes(body):
        if isinstance(node, ast.ClassDef):
            name = prefix + node.name
            yield name, node
            yield from _class_defs(node.body, name + '.')


class _Module:
    def __init__(self, relpath, source):
        self.relpath = relpath
        self.tree = ast.parse(source)
        self.bindings = {}
        self.stars = []
        _collect_bindings(self, self.tree.body, relpath)
        self.class_bindings = {}
        self.class_names = {}
        for name, node in _class_defs(self.tree.body, ''):
            scope = _Scope()
            _collect_bindings(scope, node.body, relpath)
            self.class_bindings[name] = scope
            self.class_names[id(node)] = name


def _target_names(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for item in target.elts:
            names.extend(_target_names(item))
        return names
    return []


def _argument_names(arguments):
    names = {argument.arg for argument in arguments.posonlyargs + arguments.args
             + arguments.kwonlyargs}
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _entry_top(entry):
    if entry[0] == 'module':
        return entry[2]
    if entry[0] == 'symbol':
        return entry[3]
    return entry[2]


def _entry_line(entry):
    return getattr(_entry_top(entry), 'lineno', 0)


def _entry_conditional(entry):
    if entry[0] == 'module':
        return not isinstance(entry[-1], ast.Import)
    if entry[0] == 'symbol':
        return not isinstance(entry[-1], ast.ImportFrom)
    return entry[-1] is not entry[1]


class _Sources:
    def __init__(self, loader):
        self.loader = loader
        self.cache = {}

    def module(self, relpath):
        if relpath not in self.cache:
            source = self.loader(relpath)
            self.cache[relpath] = None if source is None else _Module(relpath, source)
        return self.cache[relpath]

    def find(self, name):
        for relpath in _module_paths(name):
            module = self.module(relpath)
            if module is not None:
                return module
        return None


def _bindings_of(module, local, scope=None, position=None):
    if scope is not None:
        scoped = module.class_bindings.get(scope)
        if scoped is not None:
            entries = scoped.bindings.get(local, [])
            if entries:
                before = [entry for entry in entries
                          if position is None or _entry_line(entry) < position]
                if before:
                    if any(_entry_conditional(entry) for entry in before):
                        merged = list(before)
                        merged.extend(module.bindings.get(local, []))
                        return merged
                    return before
    return module.bindings.get(local, [])


def _resolve(module, canonical, sources, scope=None, position=None):
    if '.' in canonical:
        root, rest = canonical.split('.', 1)
        entries = _bindings_of(module, root, scope, position)
        if not entries:
            found = sources.find(root)
            if found is not None:
                return _resolve_in_module(found, rest, sources)
            if root in sys.stdlib_module_names:
                return ('external', canonical, None)
            return ('unknown', canonical, None)
        if len(entries) > 1:
            targets = set(_binding_canonical(module, entry, sources) for entry in entries)
            if len(targets) != 1:
                return ('ambiguous', canonical, None)
        entry = entries[0]
        if entry[0] == 'module':
            if entry[1] in sys.stdlib_module_names:
                return ('external', entry[1] + '.' + rest, None)
            found = sources.find(entry[1])
            if found is None:
                return ('external', entry[1] + '.' + rest, None)
            return _resolve_in_module(found, rest, sources)
        if entry[0] == 'symbol':
            if entry[1] and sources.find(entry[1] + '.' + entry[2]) is not None:
                found = sources.find(entry[1] + '.' + entry[2])
                return _resolve_in_module(found, rest, sources)
            target = _symbol_module(entry[1], sources)
            if target is None:
                return ('external', entry[1] + '.' + entry[2] + ('.' + rest if rest else ''), None)
            return _resolve_in_module(target, entry[2] + '.' + rest, sources)
        if entry[0] == 'definition':
            class_scope = module.class_names.get(id(entry[1]))
            if class_scope is not None:
                return _resolve_class_member(module, canonical, class_scope, rest, sources)
        return ('unknown', canonical, None)
    entries = _bindings_of(module, canonical, scope, position)
    if entries:
        if len(entries) > 1:
            targets = set(_binding_canonical(module, entry, sources) for entry in entries)
            if len(targets) != 1:
                return ('ambiguous', canonical, entries)
        entry = entries[0]
        if entry[0] == 'module':
            return ('module', entry[1], entries)
        if entry[0] == 'symbol':
            if entry[1] and sources.find(entry[1] + '.' + entry[2]) is not None:
                return ('module', entry[1] + '.' + entry[2], entries)
            return ('symbol', entry[1], entry[2], entries)
        if entry[0] == 'definition':
            return ('definition', canonical, entries, module)
        if entry[0] == 'dynamic':
            return ('dynamic', canonical, entries, module)
        return ('assignment', canonical, entries, module)
    for star in module.stars:
        if star == 'unittest' and canonical in ('skip', 'skipIf', 'skipUnless', 'skipTest'):
            return ('symbol', 'unittest', canonical, [])
        target = _symbol_module(star, sources)
        if target is not None:
            nested = _resolve(target, canonical, sources)
            if nested[0] in ('definition', 'assignment', 'symbol', 'ambiguous'):
                return nested
    if canonical in sys.stdlib_module_names:
        return ('external', canonical, None)
    return ('unknown', canonical, None)


def _binding_canonical(module, entry, sources):
    if entry[0] == 'module':
        return 'module:' + entry[1]
    if entry[0] == 'symbol':
        return 'symbol:' + entry[1] + ':' + entry[2]
    return entry[0] + ':' + _sha256(_canonical(entry[-1]))


def _symbol_module(name, sources):
    if name == 'unittest':
        return None
    return sources.find(name)


def _resolve_in_module(module, name, sources):
    return _resolve(module, name, sources)


def _class_scope_of(module, resolved):
    if resolved[0] != 'definition':
        return None
    for entry in resolved[2]:
        if entry[0] == 'definition':
            scope = module.class_names.get(id(entry[1]))
            if scope is not None:
                return scope
    return None


def _resolve_class_member(module, canonical, class_scope, rest, sources):
    '''Resolve a class attribute chain in the class-body scope that binds it.

    The attribute is read with the same binding table a method decorator uses,
    so a class-scope alias is censused exactly like a module-scope alias. A
    chain that does not end in a statically bound class attribute returns an
    empty entry list, which the census reports as unresolved.
    '''
    scope = class_scope
    parts = rest.split('.')
    for part in parts[:-1]:
        scope = _class_scope_of(module, _shape(_resolve(module, part, sources, scope, None)))
        if scope is None:
            return ('class_member', canonical, [], module, class_scope)
    return ('class_member', canonical, _bindings_of(module, parts[-1], scope, None),
            module, scope)


def _pin_statements(entries):
    statements = []
    for entry in entries:
        node = entry[-1]
        if node not in statements:
            statements.append(node)
    return statements


def _has_unpacked_arguments(call):
    return (any(isinstance(argument, ast.Starred) for argument in call.args)
            or any(keyword.arg is None for keyword in call.keywords))


def _function_calls(node):
    def walk(current):
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                ast.ListComp, ast.SetComp, ast.DictComp,
                                ast.GeneratorExp)):
            return
        if isinstance(current, ast.Call):
            yield current
        for child in ast.iter_child_nodes(current):
            yield from walk(child)
    for statement in node.body:
        yield from walk(statement)


def _returned_expressions(node):
    def walk(current):
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                ast.ClassDef)):
            return
        if isinstance(current, ast.Return):
            yield current.value
        for child in ast.iter_child_nodes(current):
            yield from walk(child)
    for statement in node.body:
        yield from walk(statement)


def _parameter_defaults(arguments):
    names = arguments.posonlyargs + arguments.args
    defaults = {}
    if arguments.defaults:
        for argument, value in zip(names[-len(arguments.defaults):],
                                   arguments.defaults):
            defaults[argument.arg] = value
    for argument, value in zip(arguments.kwonlyargs, arguments.kw_defaults):
        if value is not None:
            defaults[argument.arg] = value
    return defaults


def _local_binding(module, node, name, sources, seen):
    scope = _Scope()
    _collect_bindings(scope, node.body, module.relpath)
    entries = [entry for entry in scope.bindings.get(name, [])
               if entry[0] in ('assignment', 'symbol')]
    if not entries:
        return None
    if len(entries) > 1:
        return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
    return _skip_binding(module, entries[0], sources, seen, local=name)


def _skip_condition(call):
    if call.args:
        return call.args[0]
    return next((keyword.value for keyword in call.keywords
                 if keyword.arg == 'condition'), None)


def _skip_binding(module, entry, sources, seen, scope=None, position=None, local=None):
    if entry[0] == 'dynamic':
        return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
    if entry[0] == 'assignment':
        value = _assigned_value(entry[1], local)
        if value is _UNSUPPORTED:
            return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
        if isinstance(value, ast.Call):
            found = _skip_expression(module, value.func, sources, seen, scope, position)
            if found is None or found.get('ambiguous'):
                return found
            if _has_unpacked_arguments(value):
                return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
            condition = found.get('condition')
            if condition is not None:
                return {'kind': found['kind'],
                        'condition': condition,
                        'condition_module': found.get('condition_module') or module,
                        'condition_scope': found.get('condition_scope'),
                        'condition_position': found.get('condition_position'),
                        'pins': []}
            condition = _skip_condition(value)
            if condition is not None:
                return {'kind': found['kind'],
                        'condition': condition,
                        'condition_module': module,
                        'condition_scope': scope,
                        'condition_position': position,
                        'pins': []}
            return {'kind': found['kind'], 'condition': None,
                    'condition_module': module, 'condition_scope': scope,
                    'condition_position': position, 'pins': []}
        found = _skip_expression(module, value, sources, seen, scope, position)
        if found is None or found.get('ambiguous'):
            return found
        return {'kind': found['kind'],
                'condition': found.get('condition'),
                'condition_module': found.get('condition_module') or module,
                'condition_scope': found.get('condition_scope'),
                'condition_position': found.get('condition_position'),
                'pins': []}
    if entry[0] == 'definition':
        node = entry[1]
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        bound = _function_locals(node)
        kinds = set()
        conditions = set()
        condition = None
        condition_module = None
        for inner in _function_calls(node):
            found = _skip_expression(module, inner.func, sources, seen)
            if found is None:
                continue
            if found.get('ambiguous') or _has_unpacked_arguments(inner):
                return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
            kinds.add(found['kind'])
            value = None
            if found['kind'] != 'skip':
                value = _skip_condition(inner)
                if value is None:
                    value = found.get('condition')
                if value is not None and not bound.isdisjoint(_references(value)):
                    value = None
            conditions.add(None if value is None else _canonical(value))
            if value is not None and condition is None:
                condition = value
                condition_module = found.get('condition_module') or module
        returns = []
        for value in _returned_expressions(node):
            if value is None:
                returns.append(None)
                continue
            if isinstance(value, ast.Call):
                continue
            if isinstance(value, ast.Name) and value.id in bound:
                found = _local_binding(module, node, value.id, sources, seen)
                if found is None:
                    default = _parameter_defaults(node.args).get(value.id)
                    if default is not None:
                        found = _skip_expression(module, default, sources, seen)
            else:
                found = (None if _returns_function_object(module, value, sources)
                         else _skip_expression(module, value, sources, seen))
            returns.append(found)
            if found is None:
                continue
            if found.get('ambiguous'):
                return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
            kinds.add(found['kind'])
            inner_value = found.get('condition')
            if found['kind'] == 'skip':
                inner_value = None
            elif inner_value is not None and not bound.isdisjoint(_references(inner_value)):
                inner_value = None
            conditions.add(None if inner_value is None else _canonical(inner_value))
            if inner_value is not None and condition is None:
                condition = inner_value
                condition_module = found.get('condition_module') or module
        if kinds and any(found is None for found in returns):
            return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
        if not kinds:
            return None
        if len(kinds) != 1 or len(conditions) > 1:
            return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
        return {'kind': kinds.pop(), 'condition': condition,
                'condition_module': condition_module, 'condition_scope': None,
                'condition_position': None, 'pins': []}
    if entry[0] == 'symbol':
        target = _symbol_module(entry[1], sources)
        if target is None:
            return _skip_expression_in(entry[1], entry[2], sources, seen)
        key = (target.relpath, entry[2])
        if key in seen:
            return None
        seen.add(key)
        return _skip_named(target, entry[2], sources, seen)
    return None


def _returns_function_object(module, node, sources):
    canonical = _dotted(node)
    if not canonical:
        return False
    seen = set()
    while True:
        resolved = _shape(_resolve(module, canonical, sources))
        if resolved[0] == 'definition':
            return True
        if resolved[0] != 'symbol':
            return False
        key = (resolved[1], resolved[2])
        if key in seen:
            return False
        seen.add(key)
        target = _symbol_module(resolved[1], sources)
        if target is None:
            return False
        module = target
        canonical = resolved[2]


def _skip_named(module, name, sources, seen, scope=None, position=None):
    entries = _bindings_of(module, name, scope, position)
    if not entries:
        return None
    kinds = set()
    conditions = set()
    condition = None
    condition_module = None
    condition_scope = None
    condition_position = None
    unresolved = False
    for entry in entries:
        found = _skip_binding(module, entry, sources, seen, scope, position, name)
        if found is None:
            unresolved = True
            continue
        if found.get('ambiguous'):
            return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
        kinds.add(found['kind'])
        conditions.add(None if found.get('condition') is None
                       else _canonical(found['condition']))
        if condition is None and found.get('condition') is not None:
            condition = found['condition']
            condition_module = found.get('condition_module')
            condition_scope = found.get('condition_scope')
            condition_position = found.get('condition_position')
    if not kinds:
        return None
    if unresolved or len(kinds) != 1 or len(conditions) > 1:
        return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
    return {'kind': kinds.pop(), 'condition': condition,
            'condition_module': condition_module,
            'condition_scope': condition_scope,
            'condition_position': condition_position,
            'pins': _pin_statements(entries)}


def _skip_expression_in(module_name, name, sources, seen):
    if (module_name, name) in seen:
        return None
    seen.add((module_name, name))
    target = sources.find(module_name)
    if target is None:
        return {'kind': SKIP_KINDS[(module_name, name)], 'condition': None, 'pins': []} \
            if (module_name, name) in SKIP_KINDS else None
    return _skip_named(target, name, sources, seen)


_RESOLUTION_SHAPES = {'external': 3, 'unknown': 3, 'ambiguous': 3, 'module': 3,
                      'symbol': 4, 'definition': 4, 'assignment': 4, 'dynamic': 4,
                      'class_member': 5}


def _shape(resolved):
    expected = _RESOLUTION_SHAPES.get(resolved[0])
    if expected is None:
        raise AssertionError(f'unknown resolution kind: {resolved[0]!r}')
    if len(resolved) != expected:
        raise AssertionError(f'resolution {resolved[0]!r} carries {len(resolved)} fields, '
                             f'expected {expected}')
    return resolved


def _skip_expression(module, node, sources, seen, scope=None, position=None):
    canonical = _dotted(node)
    if not canonical:
        return None
    if isinstance(node, ast.Call):
        return _skip_expression(module, node.func, sources, seen, scope, position)
    resolved = _shape(_resolve(module, canonical, sources, scope, position))
    if resolved[0] == 'external':
        for key, kind in SKIP_KINDS.items():
            if resolved[1] == '.'.join(key):
                return {'kind': kind, 'condition': None, 'pins': []}
        return None
    if resolved[0] == 'module':
        return None
    if resolved[0] == 'dynamic':
        return {'kind': None, 'ambiguous': True, 'condition': None, 'pins': []}
    if resolved[0] == 'symbol':
        if resolved[1] and (resolved[1], resolved[2]) in SKIP_KINDS:
            return {'kind': SKIP_KINDS[(resolved[1], resolved[2])], 'condition': None, 'pins': []}
        if not resolved[1] and resolved[2] in ('skip', 'skipIf', 'skipUnless'):
            return {'kind': resolved[2], 'condition': None, 'pins': []}
        return _skip_binding(module, ('symbol', resolved[1], resolved[2], resolved[3]), sources, seen)
    if resolved[0] == 'class_member':
        if not resolved[2]:
            return None
        local_name = resolved[1].rsplit('.', 1)[-1]
        key = (resolved[3].relpath, resolved[4], local_name)
        if key in seen:
            return None
        seen.add(key)
        return _skip_named(resolved[3], local_name, sources, seen, resolved[4], None)
    if resolved[0] in ('definition', 'assignment'):
        owner = resolved[3]
        local_name = resolved[1].rsplit('.', 1)[-1]
        key = (owner.relpath, local_name)
        if key in seen:
            return None
        seen.add(key)
        return _skip_named(owner, local_name, sources, seen,
                           scope if owner is module else None,
                           position if owner is module else None)
    if resolved[0] == 'ambiguous':
        if '.' in canonical:
            return None
        key = (module.relpath, canonical)
        if key in seen:
            return None
        seen.add(key)
        return _skip_named(module, canonical, sources, seen, scope, position)
    return None


def _decorator_call(module, decorator, sources, scope=None, position=None):
    if isinstance(decorator, ast.Call):
        found = _skip_expression(module, decorator.func, sources, set(), scope, position)
        if found is None:
            return None
        if found.get('ambiguous') or _has_unpacked_arguments(decorator):
            return {'kind': None, 'condition': None, 'decorator': decorator, 'ambiguous': True}
        condition = _skip_condition(decorator)
        if found.get('condition') is not None:
            return {'kind': found['kind'], 'condition': found['condition'],
                    'decorator': decorator,
                    'condition_module': found.get('condition_module') or module,
                    'condition_scope': found.get('condition_scope'),
                    'condition_position': found.get('condition_position'),
                    'ambiguous': False}
        if condition is not None:
            return {'kind': found['kind'], 'condition': condition, 'decorator': decorator,
                    'condition_module': module, 'condition_scope': scope,
                    'condition_position': position, 'ambiguous': False}
        return {'kind': found['kind'], 'condition': None, 'decorator': decorator,
                'condition_module': module, 'condition_scope': scope,
                'condition_position': position, 'ambiguous': False}
    found = _skip_expression(module, decorator, sources, set(), scope, position)
    if found is None:
        return None
    if found.get('ambiguous'):
        return {'kind': None, 'condition': None, 'decorator': decorator, 'ambiguous': True}
    return {'kind': found['kind'], 'condition': found.get('condition'), 'decorator': decorator,
            'condition_module': found.get('condition_module') or module,
            'condition_scope': found.get('condition_scope'),
            'condition_position': found.get('condition_position'), 'ambiguous': False}


def _terminal_name(node):
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ''


def _unresolved_skip(module, decorator, sources, scope=None, position=None):
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    canonical = _dotted(node)
    if not canonical:
        return None
    resolved = _shape(_resolve(module, canonical, sources, scope, position))
    if resolved[0] == 'class_member':
        return canonical if not resolved[2] else None
    if _terminal_name(decorator) not in SKIP_TERMINALS:
        return None
    if resolved[0] in ('external', 'module', 'symbol', 'definition', 'assignment'):
        return None
    return canonical


def _platform_probe(module, node, scope=None, position=None):
    if isinstance(node, ast.Call):
        if node.args or node.keywords:
            return None
        if module is None:
            dotted = _dotted(node.func)
            return dotted if dotted in PLATFORM_LITERALS else None
        return _platform_probe(module, node.func, scope, position)
    if module is None:
        dotted = _dotted(node)
        return dotted if dotted in PLATFORM_LITERALS else None
    if isinstance(node, ast.Attribute):
        dotted = _dotted(node)
        if dotted in PLATFORM_LITERALS:
            return dotted
        if not isinstance(node.value, ast.Name):
            return None
        for entry in _bindings_of(module, node.value.id, scope, position):
            if entry[0] == 'module':
                candidate = entry[1] + '.' + node.attr
                if candidate in PLATFORM_LITERALS:
                    return candidate
        return None
    if isinstance(node, ast.Name):
        for entry in _bindings_of(module, node.id, scope, position):
            if entry[0] == 'symbol':
                candidate = entry[1] + '.' + entry[2]
                if candidate in PLATFORM_LITERALS:
                    return candidate
        return None
    return None


def _lower_literal_membership(node, module=None, scope=None, position=None):
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return node
    operation = node.ops[0]
    if not isinstance(operation, (ast.In, ast.NotIn)) or len(node.comparators) != 1:
        return node
    comparator = node.comparators[0]
    if not isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
        return node
    probe = _platform_probe(module, node.left, scope, position)
    if probe not in PLATFORM_LITERALS:
        return node
    literals = [_string_literal(item) for item in comparator.elts]
    if any(item is None for item in literals) or PLATFORM_LITERALS[probe][0] not in literals:
        return node
    expanded = ast.BoolOp(op=ast.Or(), values=[
        ast.Compare(left=node.left, ops=[ast.Eq()], comparators=[item])
        for item in comparator.elts])
    if isinstance(operation, ast.NotIn):
        return ast.UnaryOp(op=ast.Not(), operand=expanded)
    return expanded


def _comparison_pair(node, module=None, scope=None, position=None):
    if isinstance(node, (ast.UnaryOp, ast.BoolOp, ast.IfExp)):
        return None
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            return None
        operation = node.ops[0]
        left = node.left
        comparator = node.comparators[0]
        probe = _platform_probe(module, left, scope, position)
        if probe is None:
            probe = _platform_probe(module, comparator, scope, position)
            comparator = left
        if probe is None:
            return None
        windows = PLATFORM_LITERALS[probe][0]
        if isinstance(operation, (ast.In, ast.NotIn)):
            return None
        literal = _string_literal(comparator)
        if literal is None or literal != windows:
            return None
        if isinstance(operation, ast.Eq):
            return (True, False)
        if isinstance(operation, ast.NotEq):
            return (False, True)
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr != 'startswith' or len(node.args) != 1 or node.keywords:
            return None
        probe = _platform_probe(module, node.func.value, scope, position)
        if probe not in PLATFORM_LITERALS:
            return None
        prefix = _string_literal(node.args[0])
        if prefix not in PLATFORM_LITERALS[probe]:
            return None
        return (True, False)
    return None


def _posix_truth(node, module=None, scope=None, position=None):
    '''Conservatively prove the condition on POSIX: 'T', 'F' or 'U' (unknown).'''
    node = _lower_literal_membership(node, module, scope, position)
    if isinstance(node, ast.BoolOp):
        truths = [_posix_truth(value, module, scope, position) for value in node.values]
        if isinstance(node.op, ast.And):
            if 'F' in truths:
                return 'F'
            return 'T' if all(truth == 'T' for truth in truths) else 'U'
        if isinstance(node.op, ast.Or):
            if 'T' in truths:
                return 'T'
            return 'F' if all(truth == 'F' for truth in truths) else 'U'
        return 'U'
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.Not):
            return 'U'
        truth = _posix_truth(node.operand, module, scope, position)
        return {'T': 'F', 'F': 'T', 'U': 'U'}[truth]
    if isinstance(node, ast.IfExp):
        body = _posix_truth(node.body, module, scope, position)
        orelse = _posix_truth(node.orelse, module, scope, position)
        if body == orelse:
            return body
        return 'U'
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return 'T' if node.value else 'F'
    pair = _comparison_pair(node, module, scope, position)
    if pair is None:
        return 'U'
    on_windows, on_posix = pair
    return 'T' if on_posix else 'F'


def _leading_windows_guard(node, module=None, scope=None, position=None):
    node = _lower_literal_membership(node, module, scope, position)
    if isinstance(node, ast.BoolOp):
        return _leading_windows_guard(node.values[0], module, scope, position)
    if isinstance(node, ast.IfExp):
        return _leading_windows_guard(node.body, module, scope, position)
    if isinstance(node, ast.Compare) and len(node.ops) > 1:
        node = ast.Compare(left=node.left, ops=node.ops[:1],
                           comparators=node.comparators[:1])
    pair = _comparison_pair(node, module, scope, position)
    return pair is not None and pair[0] and not pair[1]


def _refusal(kind, condition, module=None, scope=None, position=None):
    if kind == 'skip':
        return 'unconditional skip'
    if kind is None or condition is None:
        return None
    pair = _comparison_pair(condition, module, scope, position)
    if pair is not None:
        on_windows, on_posix = pair
        skips_posix = (not on_posix) if kind == 'skipUnless' else on_posix
        return 'literal Windows-only refusal' if skips_posix else None
    truth = _posix_truth(condition, module, scope, position)
    skips_posix = truth == ('F' if kind == 'skipUnless' else 'T')
    if skips_posix:
        return 'literal Windows-only refusal'
    if kind == 'skipUnless' and _leading_windows_guard(condition, module, scope, position):
        return 'conservative legacy Windows-only refusal'
    return None


def _literal_refusal(kind, condition, module=None, scope=None, position=None):
    return _refusal(kind, condition, module, scope, position) is not None


def _decorators(tree):
    def visit(body, prefix, scope, local):
        for node, _top in _scope_nodes(body):
            if isinstance(node, ast.ClassDef):
                name = prefix + node.name
                for index, decorator in enumerate(node.decorator_list):
                    yield name, index, decorator, scope, node.lineno, local
                yield from visit(node.body, name + '.', name, local)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                for index, decorator in enumerate(node.decorator_list):
                    yield name, index, decorator, scope, node.lineno, local
                yield from visit(node.body, name + '.', None, True)
    yield from visit(tree.body, '', None, False)


def _references(statement, exclude=()):
    excluded = set()
    for node in exclude:
        for child in ast.walk(node):
            excluded.add(id(child))
    for node in ast.walk(statement):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and id(node) not in excluded:
            yield node.id


def _node_defaults(node):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return []
    values = list(node.args.defaults)
    values.extend(value for value in node.args.kw_defaults if value is not None)
    return values


def _function_locals(node):
    bound = _argument_names(node.args)
    declared_global = set()
    declared_nonlocal = set()

    def collect(child):
        for item in ast.iter_child_nodes(child):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(item.name)
                continue
            if isinstance(item, (ast.Lambda, ast.ListComp, ast.SetComp,
                                 ast.DictComp, ast.GeneratorExp)):
                continue
            if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
                bound.add(item.id)
            elif isinstance(item, ast.Import):
                for alias in item.names:
                    bound.add(alias.asname or alias.name.split('.')[0])
            elif isinstance(item, ast.ImportFrom):
                for alias in item.names:
                    if alias.name != '*':
                        bound.add(alias.asname or alias.name)
            elif isinstance(item, ast.ExceptHandler) and item.name:
                bound.add(item.name)
            elif isinstance(item, ast.Global):
                declared_global.update(item.names)
            elif isinstance(item, ast.Nonlocal):
                declared_nonlocal.update(item.names)
            collect(item)

    collect(node)
    bound.difference_update(declared_global)
    bound.update(declared_nonlocal)
    return bound


def _scoped_references(node, bound):
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, ast.Load) and node.id not in bound:
            yield node.id
        return
    if isinstance(node, ast.Lambda):
        for value in _node_defaults(node):
            yield from _scoped_references(value, bound)
        inner = set(bound)
        inner.update(_argument_names(node.args))
        yield from _scoped_references(node.body, inner)
        return
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        inner = set(bound)
        for generator in node.generators:
            inner.update(_target_names(generator.target))
        for index, generator in enumerate(node.generators):
            yield from _scoped_references(generator.iter, bound if index == 0 else inner)
            for condition in generator.ifs:
                yield from _scoped_references(condition, inner)
        if isinstance(node, ast.DictComp):
            yield from _scoped_references(node.key, inner)
            yield from _scoped_references(node.value, inner)
        else:
            yield from _scoped_references(node.elt, inner)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        inner = set(bound)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner.update(_argument_names(node.args))
        for decorator in node.decorator_list:
            yield from _scoped_references(decorator, bound)
        for value in _node_defaults(node):
            yield from _scoped_references(value, bound)
        for child in node.body:
            yield from _scoped_references(child, inner)
        return
    for child in ast.iter_child_nodes(node):
        yield from _scoped_references(child, bound)


def _definition_references(node):
    bound = _function_locals(node)
    for decorator in node.decorator_list:
        yield from _references(decorator)
    arguments = (node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                 + [node.args.vararg, node.args.kwarg])
    for argument in arguments:
        if argument is not None and argument.annotation is not None:
            yield from _references(argument.annotation)
    if node.returns is not None:
        yield from _references(node.returns)
    for statement in node.body:
        yield from _scoped_references(statement, bound)


def _scope_references(node, bound, deferred, attributes):
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, ast.Load) and node.id not in bound:
            yield node.id, deferred
        return
    if attributes and isinstance(node, ast.Attribute):
        text = _dotted(node)
        if text and text.split('.', 1)[0] not in bound:
            yield text, deferred
    if isinstance(node, ast.Lambda):
        for value in _node_defaults(node):
            yield from _scope_references(value, bound, deferred, attributes)
        inner = set(bound)
        inner.update(_argument_names(node.args))
        yield from _scope_references(node.body, inner, True, attributes)
        return
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        inner = set(bound)
        for generator in node.generators:
            inner.update(_target_names(generator.target))
        for index, generator in enumerate(node.generators):
            yield from _scope_references(
                generator.iter, bound if index == 0 else inner,
                deferred if index == 0 else True, attributes)
            for condition in generator.ifs:
                yield from _scope_references(condition, inner, True, attributes)
        if isinstance(node, ast.DictComp):
            yield from _scope_references(node.key, inner, True, attributes)
            yield from _scope_references(node.value, inner, True, attributes)
        else:
            yield from _scope_references(node.elt, inner, True, attributes)
        return
    for child in ast.iter_child_nodes(node):
        yield from _scope_references(child, bound, deferred, attributes)


def _default_references(node, bound=frozenset(), deferred=False):
    for name, nested in _scope_references(node, bound, deferred, False):
        yield name, not nested


def _binding_window(entries):
    if any(_entry_conditional(entry) for entry in entries):
        return None
    lines = [_entry_line(entry) for entry in entries]
    lines = [line for line in lines if line]
    return max(lines) if lines else None


def _scoped_entries(module, scope, name, entries):
    if scope is None:
        return False
    scoped = module.class_bindings.get(scope)
    if scoped is None:
        return False
    class_entries = scoped.bindings.get(name, [])
    return bool(class_entries) and all(
        any(entry is item for item in class_entries) for entry in entries)


def _statement_references(statement, scope, position):
    if isinstance(statement, ast.Assign):
        nodes = [statement.value]
    elif isinstance(statement, ast.AnnAssign):
        nodes = [node for node in (statement.annotation, statement.value)
                 if node is not None]
    elif isinstance(statement, ast.AugAssign):
        nodes = [statement.value]
    else:
        return [(name, scope, position) for name in _references(statement)]
    references = []
    for node in nodes:
        for name, deferred in _expression_references(node):
            references.append((name, None, None) if deferred
                              else (name, scope, position))
    return references


def _closure(module, seeds, sources, scope=None, position=None, deferred=()):
    entries = []
    seen = set()
    queue = [(module, seed, 0, scope, position) for seed in seeds]
    queue.extend((module, seed, 0, None, None) for seed in deferred)
    definitions = 0
    nodes = 0
    max_depth = 0
    truncated = False
    visited_names = set()
    while queue:
        current, name, depth, current_scope, current_position = queue.pop(0)
        max_depth = max(max_depth, depth)
        if depth > CSPR_MAX_DEPTH:
            truncated = True
            break
        resolved = _shape(_resolve(current, name, sources, current_scope, current_position))
        if resolved[0] in ('external', 'unknown'):
            continue
        if resolved[0] == 'module':
            continue
        if resolved[0] == 'ambiguous':
            truncated = True
            continue
        if resolved[0] == 'dynamic':
            truncated = True
        next_scope = None
        next_position = None
        definition_nodes = []
        scoped = False
        if resolved[0] == 'symbol':
            submodule = sources.find(resolved[1] + '.' + resolved[2]) if resolved[1] else None
            target = submodule if submodule is not None else _symbol_module(resolved[1], sources)
            if target is None:
                continue
            owner = target
            name = resolved[2]
            entries_for_name = _bindings_of(owner, name)
            if not entries_for_name:
                continue
            statements = _pin_statements(entries_for_name)
        elif resolved[0] == 'class_member':
            if not resolved[2]:
                continue
            owner = resolved[3]
            statements = _pin_statements(resolved[2])
            name = resolved[1]
            scoped = True
            next_scope = resolved[4]
            next_position = _binding_window(resolved[2])
        elif resolved[0] in ('definition', 'assignment', 'dynamic'):
            owner = resolved[3]
            statements = _pin_statements(resolved[2])
            name = resolved[1]
            scoped = _scoped_entries(current, current_scope, name, resolved[2])
            if resolved[0] in ('assignment', 'dynamic') and scoped:
                next_scope = current_scope
                next_position = _binding_window(resolved[2])
            elif resolved[0] == 'definition' and scoped:
                definition_nodes = [entry[1] for entry in resolved[2]
                                    if entry[0] == 'definition']
        else:
            continue
        current = owner
        key = (current.relpath, name)
        if scoped:
            key += (current_scope,)
        if key in visited_names:
            continue
        visited_names.add(key)
        definitions += 1
        if definitions > CSPR_MAX_DEFINITIONS:
            truncated = True
            break
        for statement in statements:
            count = sum(1 for _ in ast.walk(statement))
            nodes += count
            if nodes > CSPR_MAX_NODES:
                truncated = True
                break
            digest = _sha256(_canonical(statement))
            if (current.relpath, name, digest) not in entries:
                entries.append((current.relpath, name, digest))
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                references = [(reference, next_scope, next_position)
                              for reference in _definition_references(statement)]
                if not scoped:
                    for default in _node_defaults(statement):
                        for reference in _scoped_references(default, set()):
                            queue.append((current, reference, depth + 1, None, None))
            else:
                references = _statement_references(statement, next_scope, next_position)
            for reference, reference_scope, reference_position in references:
                queue.append((current, reference, depth + 1,
                              reference_scope, reference_position))
        for node in definition_nodes:
            for default in _node_defaults(node):
                for reference, immediate in _default_references(default):
                    queue.append((current, reference, depth + 1,
                                  current_scope if immediate else None,
                                  node.lineno if immediate else None))
        if truncated:
            break
    return sorted(entries), max_depth, definitions, nodes, truncated


def _condition_locals(condition):
    names = set()
    for node in ast.walk(condition):
        if isinstance(node, ast.Lambda):
            arguments = node.args.args + node.args.kwonlyargs + node.args.posonlyargs
            names.update(argument.arg for argument in arguments)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for generator in node.generators:
                names.update(_target_names(generator.target))
        elif isinstance(node, ast.NamedExpr):
            names.update(_target_names(node.target))
    return names


def _unknown_seed(module, condition, sources, scope=None, position=None):
    if condition is None:
        return None
    local = _condition_locals(condition)
    for name, deferred in _expression_references(condition):
        if '.' in name:
            continue
        if name in local or name in dir(builtins):
            continue
        reference_scope = None if deferred else scope
        reference_position = None if deferred else position
        if _resolve(module, name, sources, reference_scope,
                    reference_position)[0] == 'unknown':
            return name
    return None


def _expression_references(node, deferred=False):
    yield from _scope_references(node, frozenset(), deferred, True)


def _expressions(node):
    found = []
    seen = set()
    for item in _expression_references(node):
        if item not in seen:
            seen.add(item)
            found.append(item)
    return found


class _Site:
    def __init__(self, key, kind, decorator, condition, entries, declaration, metrics,
                 condition_module=None, condition_scope=None, condition_position=None):
        self.key = key
        self.kind = kind
        self.decorator = decorator
        self.condition = condition
        self.entries = entries
        self.declaration = declaration
        self.metrics = metrics
        self.condition_module = condition_module
        self.condition_scope = condition_scope
        self.condition_position = condition_position

    def digest(self, declaration=None):
        text = declaration if declaration is not None else self.declaration
        if text is None:
            raise ValueError('A declaration is required to compute a registry digest')
        parts = ['decorator=' + _canonical(self.decorator)]
        parts.extend('closure=' + relpath + '|' + qualname + '|' + digest
                     for relpath, qualname, digest in self.entries)
        parts.append('declaration=' + text)
        return _sha256('\n'.join(parts))


class _Census:
    def __init__(self):
        self.sites = {}
        self.unresolved = []
        self.truncated = []
        self.max_depth = 0
        self.max_definitions = 0
        self.max_nodes = 0
        self.refused = []
        self.refusal_reasons = {}
        self.ambiguous = []

    def add(self, site):
        if site.key in self.sites:
            raise AssertionError(f"census repeats location {site.key}")
        self.sites[site.key] = site
        self.max_depth = max(self.max_depth, site.metrics[0])
        self.max_definitions = max(self.max_definitions, site.metrics[1])
        self.max_nodes = max(self.max_nodes, site.metrics[2])
        if site.metrics[3]:
            self.truncated.append(site.key)
        reason = _refusal(site.kind, site.condition, site.condition_module,
                          site.condition_scope, site.condition_position)
        if reason is not None:
            self.refused.append(site.key)
            self.refusal_reasons[site.key] = reason


def _census(files, sources, declarations=None):
    result = _Census()
    for relpath in sorted(files):
        module = sources.module(relpath)
        if module is None:
            continue
        for node in ast.walk(module.tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Call):
                continue
            found = _decorator_call(module, value.func, sources)
            if found is not None:
                result.unresolved.append(
                    f'{relpath}:{node.lineno}:unsupported assignment-applied skip; rewrite as decorator')
        for qualname, index, decorator, scope, position, local in _decorators(module.tree):
            found = _decorator_call(module, decorator, sources, scope, position)
            if found is None:
                unresolved = _unresolved_skip(module, decorator, sources, scope, position)
                if unresolved is not None:
                    result.unresolved.append(f'{relpath}:{qualname}:{index}:{unresolved}')
                continue
            if local:
                result.unresolved.append(
                    f'{relpath}:{qualname}:{index}:function-scoped decorated skip')
            kind = found['kind']
            condition = found['condition']
            raw = found['decorator']
            expressions = _expressions(raw)
            seeds = [text for text, deferred in expressions if not deferred]
            deferred_seeds = [text for text, deferred in expressions if deferred]
            entries, depth, definitions, nodes, truncated = _closure(
                module, seeds, sources, scope, position, deferred_seeds)
            key = f'{Path(relpath).name}:{qualname}:{index}'
            declaration = None
            if declarations is not None:
                declaration = declarations.get(key)
            condition_module = found.get('condition_module') or module
            condition_scope = found.get('condition_scope')
            condition_position = found.get('condition_position')
            if condition_module is not module:
                condition_scope = None
                condition_position = None
            unknown = _unknown_seed(condition_module, condition, sources,
                                    condition_scope, condition_position)
            if unknown is not None:
                result.unresolved.append(f'{relpath}:{qualname}:{index}:{unknown}')
            site = _Site(key, kind, raw, condition, entries, declaration,
                         (depth, definitions, nodes, truncated), condition_module,
                         condition_scope, condition_position)
            result.add(site)
            if found['ambiguous']:
                result.ambiguous.append(key)
    return result


def _source_paths(root):
    pending = [(root / 'app/tests', frozenset())]
    directories = 0
    entries = 0
    while pending:
        directory, ancestors = pending.pop()
        relative = directory.relative_to(root).as_posix()
        directories += 1
        if directories > SOURCE_SCAN_MAX_DIRECTORIES:
            raise AssertionError(f'source scan exceeded directory limit at {relative}')
        try:
            identity_stat = directory.stat()
            identity = ((identity_stat.st_dev, identity_stat.st_ino)
                        if identity_stat.st_ino else
                        os.path.normcase(os.path.realpath(directory)))
            if identity in ancestors:
                raise AssertionError(f'source scan encountered a directory cycle at {relative}')
            descendants = ancestors | {identity}
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > SOURCE_SCAN_MAX_ENTRIES:
                        raise AssertionError(f'source scan exceeded entry limit at {relative}')
                    if fnmatch.fnmatch(entry.name, 'test_*.py'):
                        yield Path(entry.path)
                    if entry.is_dir() and not entry.is_symlink():
                        pending.append((Path(entry.path), descendants))
        except OSError as exc:
            raise AssertionError(f'source scan cannot read directory {relative}: {exc}') from exc


def _scan_sources(root):
    files = {}
    for path in sorted(_source_paths(root)):
        files[path.relative_to(root).as_posix()] = path.read_text(encoding='utf-8')

    def loader(relpath):
        if relpath in files:
            return files[relpath]
        target = root / relpath
        if target.is_file():
            return target.read_text(encoding='utf-8')
        return None

    return _Sources(loader), files


def _parse_registry(text):
    rows = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        location, separator, rest = line.partition(' = ')
        if not separator:
            raise AssertionError(f'Registry line {number} is not "<location> = <digest>  # <declaration>"')
        digest, marker, declaration = rest.partition('  # ')
        if not marker or not declaration.strip():
            raise AssertionError(f'Registry line {number} lacks a reviewed declaration')
        if location in rows:
            raise AssertionError(f'Registry line {number} repeats location {location}')
        rows[location] = (digest.strip(), declaration.strip())
    return rows


def _verify_registry(result, rows):
    problems = []
    if result.unresolved:
        problems.append('unresolved skip decorators: ' + ', '.join(result.unresolved))
    if result.truncated:
        problems.append('truncated census: ' + ', '.join(result.truncated))
    if result.refused:
        groups = {}
        for key in result.refused:
            reason = result.refusal_reasons.get(key, 'literal Windows-only refusal')
            groups.setdefault(reason, []).append(key)
        for reason in sorted(groups):
            problems.append(reason + ': ' + ', '.join(groups[reason]))
    if result.ambiguous:
        problems.append('ambiguous skip kind: ' + ', '.join(result.ambiguous))
    missing = sorted(set(result.sites) - set(rows))
    extra = sorted(set(rows) - set(result.sites))
    if missing:
        problems.append('sites without a registry row: ' + ', '.join(missing))
    if extra:
        problems.append('registry rows without a site: ' + ', '.join(extra))
    for key in sorted(set(result.sites) & set(rows)):
        site = result.sites[key]
        digest, declaration = rows[key]
        if site.declaration is not None and site.declaration != declaration:
            problems.append(f'{key}: declaration differs from census')
        expected = site.digest(declaration)
        if expected != digest:
            problems.append(f'{key}: digest mismatch (registry {digest}, source {expected})')
    return problems


def _repin_problems(result, rows):
    problems = []
    if result.unresolved:
        problems.append('unresolved skip decorators: ' + ', '.join(result.unresolved))
    if result.truncated:
        problems.append('truncated census: ' + ', '.join(result.truncated))
    if result.refused:
        problems.append('refused sites: ' + ', '.join(result.refused))
    if result.ambiguous:
        problems.append('ambiguous skip kinds: ' + ', '.join(result.ambiguous))
    missing = sorted(set(result.sites) - set(rows))
    extra = sorted(set(rows) - set(result.sites))
    if missing:
        problems.append('sites without a registry row: ' + ', '.join(missing))
    if extra:
        problems.append('registry rows without a site: ' + ', '.join(extra))
    for key in sorted(set(result.sites) & set(rows)):
        if result.sites[key].declaration != rows[key][1]:
            problems.append(f'{key}: declaration differs from census')
    return problems


def _repin_updates(result, rows):
    updates = {}
    for key in sorted(rows):
        site = result.sites.get(key)
        if site is None:
            continue
        expected = site.digest(rows[key][1])
        if expected != rows[key][0]:
            updates[key] = expected
    return updates


def _rewrite_registry(text, updates):
    lines = text.split('\n')
    written = []
    for line in lines:
        location, separator, rest = line.partition(' = ')
        if separator and location in updates:
            _digest, marker, declaration = rest.partition('  # ')
            line = location + ' = ' + updates[location]
            if marker:
                line += '  # ' + declaration
        written.append(line)
    return '\n'.join(written)


def _write_registry(text, updates):
    rewritten = _rewrite_registry(text, updates)
    original_rows = _parse_registry(text)
    rewritten_rows = _parse_registry(rewritten)
    if {key: value[1] for key, value in rewritten_rows.items()} != \
            {key: value[1] for key, value in original_rows.items()}:
        raise AssertionError('repin would change a registry declaration')
    handle, name = tempfile.mkstemp(dir=str(SKIP_REGISTRY.parent), prefix='.skip-baseline-')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(rewritten)
        if SKIP_REGISTRY.read_text(encoding='utf-8') != text:
            raise AssertionError('registry changed during repin; refusing overwrite')
        os.replace(name, SKIP_REGISTRY)
    finally:
        if os.path.exists(name):
            os.remove(name)


def _repin(text):
    rows = _parse_registry(text)
    sources, files = _scan_sources(ROOT)
    result = _census(files, sources, {key: value[1] for key, value in rows.items()})
    return rows, result, _repin_problems(result, rows)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != '--repin' or argv[1:] not in ([], ['--apply']):
        print(f'Usage: {Path(__file__).name} --repin [--apply]', file=sys.stderr)
        return 2
    text = SKIP_REGISTRY.read_text(encoding='utf-8')
    rows, result, problems = _repin(text)
    if problems:
        for problem in problems:
            print('repin refused: ' + problem, file=sys.stderr)
        return 1
    updates = _repin_updates(result, rows)
    for key in sorted(updates):
        print(f'{key}: {rows[key][0]} -> {updates[key]}')
    print(f'{len(updates)} digest change(s) across {len(rows)} rows')
    if '--apply' in argv and updates:
        _write_registry(text, updates)
    return 0


def _indent(line):
    return len(line) - len(line.lstrip(' '))


def _strip_comment(value):
    quote = None
    for index, character in enumerate(value):
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in (chr(39), chr(34)):
            quote = character
            continue
        if character == chr(35) and (index == 0 or value[index - 1] in ' \t'):
            return value[:index].rstrip()
    return value.rstrip()


def _unquote(value):
    value = _strip_comment(value).strip()
    if len(value) < 2:
        return value
    quote = value[0]
    if quote in (chr(39), chr(34)) and value[-1] == quote:
        return value[1:-1]
    return value


def _block(lines, indent, key):
    starts = [index for index, line in enumerate(lines)
              if _indent(line) == indent and _strip_comment(line.strip()) == key]
    if len(starts) != 1:
        return None
    start = starts[0]
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and _indent(line) <= indent:
            break
        block.append(line)
    return block


def _job_lines(workflow, job_name):
    block = _block(workflow.splitlines(), 2, f'{job_name}:')
    if block is None:
        raise AssertionError(f'Expected one {job_name} job')
    return block


def _step(job_lines, name):
    starts = [index for index, line in enumerate(job_lines)
              if line.lstrip().startswith('- name:')
              and _unquote(line.lstrip()[len('- name:'):]) == name]
    if len(starts) != 1:
        raise AssertionError(f'Expected one step named {name!r}, found {len(starts)}')
    start = starts[0]
    list_indent = _indent(job_lines[start])
    key_indent = list_indent + 2
    block = [job_lines[start]]
    for line in job_lines[start + 1:]:
        if not line.strip():
            block.append(line)
            continue
        indent = _indent(line)
        if indent < key_indent or (indent == list_indent and line.lstrip().startswith('-')):
            break
        block.append(line)
    normalized = [' ' * key_indent + block[0].lstrip()[2:]] + block[1:]
    values = {}
    for line in normalized:
        if not line.strip() or _indent(line) != key_indent:
            continue
        key, _, value = line.strip().partition(':')
        values[key.strip()] = _unquote(value)
    return values


def _gate(value):
    if value is None or not value.strip():
        raise AssertionError('The alias step must declare a step-level if gate')
    text = _unquote(value)
    if text.startswith('${{') and text.endswith('}}'):
        text = text[3:-2].strip()
    try:
        condition = ast.parse(text, mode='eval').body
    except SyntaxError as error:
        raise AssertionError(f'Step gate is not an expression: {value!r}') from error
    if not isinstance(condition, ast.Compare) or len(condition.ops) != 1:
        return None
    if not isinstance(condition.ops[0], ast.Eq) or _dotted(condition.left) != 'runner.os':
        return None
    comparator = condition.comparators[0]
    if not isinstance(comparator, ast.Constant) or comparator.value != 'Windows':
        return None
    return condition


def _trigger_lines(workflow):
    block = _block(workflow.splitlines(), 0, 'on:')
    if block is None:
        raise AssertionError('Expected one top-level on block')
    return block


def _pull_request(lines):
    return _block(lines, 2, 'pull_request:')


def _paths_ignore(lines):
    return [line.strip() for line in lines
            if _indent(line) == 4 and line.strip().startswith('paths-ignore:')]


def _key_block(lines, indent, key):
    '''The one key: entry at this indent, as (inline value, body lines).'''
    starters = []
    for index, line in enumerate(lines):
        text = _strip_comment(line.strip())
        if (_indent(line) == indent and ':' in text
                and text.partition(':')[0].strip() == key):
            starters.append(index)
    if len(starters) != 1:
        raise AssertionError(
            f'Expected one {key!r} entry at indent {indent}, found {len(starters)}')
    start = starters[0]
    inline = _strip_comment(lines[start].strip()).partition(':')[2].strip()
    body = []
    for line in lines[start + 1:]:
        if line.strip() and _indent(line) <= indent:
            break
        body.append(line)
    return inline, body


def _sequence(lines, indent, key):
    '''A scalar sequence, flow on the key line or a block list beneath it.'''
    inline, body = _key_block(lines, indent, key)
    if inline:
        if not (inline.startswith('[') and inline.endswith(']')):
            raise AssertionError(f'{key!r} must be a flow or block sequence, got {inline!r}')
        items = [item.strip() for item in inline[1:-1].split(',')]
        if any(not item for item in items):
            raise AssertionError(f'{key!r} sequence is malformed: {inline!r}')
        values = [_unquote(item) for item in items]
    else:
        values = []
        for line in body:
            text = line.strip()
            if not text or text.startswith('#'):
                continue
            if _indent(line) != indent + 2 or not text.startswith('- '):
                raise AssertionError(
                    f'{key!r} entry is not a sequence item at indent {indent + 2}: {line!r}')
            values.append(_unquote(text[2:]))
    if not values:
        raise AssertionError(f'{key!r} declares no entries')
    if len(values) != len(set(values)):
        raise AssertionError(f'{key!r} repeats an entry')
    return values


def _declared_paths(workflow):
    trigger = _pull_request(_trigger_lines(workflow))
    if trigger is None:
        raise AssertionError('Expected one pull_request trigger with path filters')
    paths = _sequence(trigger, 4, 'paths')
    negatives = [path for path in paths if path.startswith('!')]
    if negatives:
        raise AssertionError(f'negative path filters are not supported: {negatives}')
    return paths


def _matrix_os(job_lines):
    _inline, strategy = _key_block(job_lines, 4, 'strategy')
    _inline, matrix = _key_block(strategy, 6, 'matrix')
    expansions = []
    for line in matrix:
        text = _strip_comment(line.strip())
        if (_indent(line) == 8 and ':' in text
                and text.partition(':')[0].strip() in ('include', 'exclude')):
            expansions.append(text.partition(':')[0].strip())
    if expansions:
        raise AssertionError(f'matrix {sorted(set(expansions))} expansion is not supported')
    return _sequence(matrix, 8, 'os')


def _repo_path(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def _required_paths():
    paths = {_repo_path(WORKFLOW), _repo_path(__file__), _repo_path(ALIAS_SCRIPT),
             _repo_path(SKIP_REGISTRY), 'app/tests/NATIVE_CHECKS.md',
             'app/routers/sound_design.py', 'app/upload_operation.py'}
    for module in _alias_module_names():
        for candidate in _module_paths(module):
            if (ROOT / candidate).is_file():
                paths.add(candidate)
                break
        else:
            raise AssertionError(f'{module} has no file in this checkout')
    return paths


def _import_roots(module):
    roots = []
    aliases = {}
    for node, _top in _scope_nodes(module.tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split('.')[0]] = alias.name
    for node in _module_nodes(module.tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.append((node.module or '').split('.')[0] if not node.level else '<relative>')
        elif isinstance(node, ast.Call):
            roots.extend(_import_call_roots(node, aliases))
    return roots


def _import_call_roots(node, aliases):
    func = node.func
    if isinstance(func, ast.Call):
        if _dotted(func.func) == 'getattr' and len(func.args) >= 2:
            base = _dotted(func.args[0])
            target = _string_literal(func.args[1])
            if base == 'importlib' and target in ('import_module', '__import__'):
                return _literal_root(node)
        return []
    name = _dotted(func)
    if not name:
        return []
    root = aliases.get(name.split('.')[0], name.split('.')[0])
    canonical = root + ('.' + '.'.join(name.split('.')[1:]) if '.' in name else '')
    if canonical in ('exec', 'eval'):
        if node.args and _string_literal(node.args[0]) is not None:
            return []
        return ['<dynamic>']
    if canonical in ('__import__', 'importlib.import_module') or canonical.endswith('.import_module'):
        return _literal_root(node)
    return []


def _literal_root(node):
    if node.args:
        literal = _string_literal(node.args[0])
        if literal is not None:
            return [literal.split('.')[0]]
    return ['<dynamic>']


def _fixture_census(files):
    return _census(files, _Sources(files.get))


def _single_site(case, files):
    result = _fixture_census(files)
    case.assertEqual(1, len(result.sites), sorted(result.sites))
    return result, result.sites[next(iter(result.sites))]


def _assert_transaction_contract(case):
    source = TRANSACTION_SCRIPT.read_text(encoding='utf-8')
    tree = ast.parse(source)
    counts = {node.name: sum(1 for item in node.body if isinstance(item, ast.FunctionDef)
                             and item.name.startswith('test_'))
              for node in tree.body if isinstance(node, ast.ClassDef)}
    case.assertEqual(TRANSACTION_CLASSES, counts)
    case.assertIn(f'NATIVE_TEST_COUNT = {sum(TRANSACTION_CLASSES.values())}', source)
    case.assertIn('result.testsRun == NATIVE_TEST_COUNT', source)
    case.assertIn('and not result.skipped', source)
    case.assertNotIn('skipUnless', source)
    workflow = (ROOT / '.github/workflows/tests.yml').read_text().split('  windows-owner:', 1)[1]
    case.assertIn('run: python tests/native_windows_transaction_checks.py', workflow)
    install = workflow.index('run: choco install ffmpeg --no-progress -y')
    verify = workflow.index('name: Verify native Windows transaction codec executables')
    transaction = workflow.index('run: python tests/native_windows_transaction_checks.py')
    case.assertLess(workflow.index('windows_import_checks.py'), install)
    case.assertLess(install, verify)
    case.assertLess(verify, transaction)
    case.assertIn('subprocess.run([\'ffmpeg\', \'-version\'], check=True)', workflow)
    case.assertIn('subprocess.run([\'ffprobe\', \'-version\'], check=True)', workflow)


def _assert_stdlib_imports(case):
    alias_source = ALIAS_SCRIPT.read_text(encoding='utf-8')
    roots = _import_roots(_Module('app/tests/native_windows_sound_temp_root_checks.py', alias_source))
    case.assertEqual([], [root for root in roots if root not in sys.stdlib_module_names],
                     f'Alias gate eager imports must be stdlib: {roots}')
    case.assertNotIn('sound_design_assets', roots)
    fixture = ('import json\ntry:\n    import requests\nexcept ImportError:\n    requests = None\n'
               'if True:\n    import yaml\ndef deferred():\n    import torch\n'
               'importlib.import_module(name)\n')
    case.assertEqual(['json', 'requests', 'yaml', '<dynamic>'], _import_roots(_Module('app/tests/x.py', fixture)))
    match_fixture = 'import json\nmatch 1:\n    case 1:\n        import yaml\n'
    case.assertEqual(['json', 'yaml'], _import_roots(_Module('app/tests/x.py', match_fixture)))
    case.assertEqual(['requests'],
                     _import_roots(_Module('app/tests/x.py', "getattr(importlib, 'import_module')('requests')\n")))
    case.assertEqual(['<dynamic>'],
                     _import_roots(_Module('app/tests/x.py', "getattr(importlib, 'import_module')(name)\n")))
    case.assertEqual(['requests'],
                     _import_roots(_Module('app/tests/x.py', "with __import__('requests'):\n    pass\n")))
    case.assertEqual(['<dynamic>'],
                     _import_roots(_Module('app/tests/x.py', '[__import__(item) for item in values]\n')))
    case.assertEqual(['json'],
                     _import_roots(_Module('app/tests/x.py', "importlib.import_module('json')\n")))
    case.assertEqual(['importlib', 'yaml'],
                     _import_roots(_Module('app/tests/x.py', "import importlib as il\nil.import_module('yaml')\n")))
    ordinary = ("sys.exit(0)\nPath('x').read_text()\n"
                "subprocess.run(['ffmpeg', '-version'], check=True)\n")
    case.assertEqual([], _import_roots(_Module('app/tests/x.py', ordinary)))
    case.assertEqual([], _import_roots(_Module('app/tests/x.py', "exec('x = 1')\n")))
    case.assertEqual(['<dynamic>'], _import_roots(_Module('app/tests/x.py', 'exec(source)\n')))


def _assert_skip_registry(case):
    sources, files = _scan_sources(ROOT)
    rows = _parse_registry(SKIP_REGISTRY.read_text(encoding='utf-8'))
    result = _census(files, sources, {key: value[1] for key, value in rows.items()})
    case.assertEqual([], _verify_registry(result, rows))
    case.assertEqual(len(result.sites), len(rows))
    case.assertEqual([], result.refused)
    case.assertEqual([], result.ambiguous)
    case.assertLessEqual(result.max_depth, CSPR_MAX_DEPTH)
    case.assertLessEqual(result.max_definitions, CSPR_MAX_DEFINITIONS)
    case.assertLessEqual(result.max_nodes, CSPR_MAX_NODES)
    case.assertEqual([], result.truncated)
    debug_declaration = rows['test_windows_export_qualification.py'
                             ':WindowsQualificationVerdictTests:1'][1]
    case.assertIn('platform=environment-gated', debug_declaration)
    case.assertIn('predicate=not __debug__', debug_declaration)
    case.assertIn('-O skips on every host', debug_declaration)


def _assert_scan_sources_reuse(case):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'app/tests').mkdir(parents=True)
        fixture = root / 'app/tests/test_fixture.py'
        fixture.write_text('import unittest\n', encoding='utf-8')
        helper = root / 'app/helper.py'
        helper.write_text('HELPER = True\n', encoding='utf-8')
        sources, files = _scan_sources(root)
        case.assertIn('app/tests/test_fixture.py', files)
        case.assertIsNotNone(sources.module('app/helper.py'),
                             'an in-repo helper outside app/tests must load from disk')
        fixture.unlink()
        helper.unlink()
        case.assertIsNotNone(sources.module('app/tests/test_fixture.py'),
                             'a test module already read into files must not be re-read')
        case.assertIsNone(sources.module('app/helper_missing.py'),
                          'an absent helper must not resolve from the files map')
        nested = root / 'app/tests/nested'
        plain = root / 'app/tests/plain'
        nested.mkdir()
        plain.mkdir()
        (nested / '__init__.py').write_text('', encoding='utf-8')
        decorated = ('import sys\nimport unittest\n'
                     'class ProbeCase(unittest.TestCase):\n'
                     "    @unittest.skipIf(sys.platform == 'win32', 'synthetic')\n"
                     '    def test_probe(self): pass\n')
        nested_fixture = nested / 'test_nested_probe.py'
        nested_fixture.write_text(decorated, encoding='utf-8')
        (plain / 'test_plain_probe.py').write_text(decorated, encoding='utf-8')
        sources, files = _scan_sources(root)
        case.assertEqual({'app/tests/nested/test_nested_probe.py',
                          'app/tests/plain/test_plain_probe.py'}, set(files))
        result = _census(files, sources, {})
        case.assertEqual({'test_nested_probe.py:ProbeCase.test_probe:0',
                          'test_plain_probe.py:ProbeCase.test_probe:0'}, set(result.sites))
        case.assertTrue(any('sites without a registry row' in problem
                            for problem in _verify_registry(result, {})),
                        'unstaged source files must not disappear from the census')
        nested_fixture.unlink()
        case.assertIsNotNone(sources.module('app/tests/nested/test_nested_probe.py'),
                             'nested test source must use the canonical cached path')
        for package in ('first', 'second'):
            location = root / 'app/tests' / package
            location.mkdir()
            (location / '__init__.py').write_text('', encoding='utf-8')
            (location / 'test_duplicate.py').write_text(decorated, encoding='utf-8')
        sources, files = _scan_sources(root)
        case.assertIn('app/tests/first/test_duplicate.py', files)
        case.assertIn('app/tests/second/test_duplicate.py', files)
        with case.assertRaisesRegex(AssertionError, 'census repeats location test_duplicate.py:'):
            _census(files, sources, {})
        original_stat = Path.stat
        ancestor_stat = (root / 'app/tests').stat()

        def cycle_stat(path, *args, **kwargs):
            if path == nested:
                return ancestor_stat
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(Path, 'stat', autospec=True, side_effect=cycle_stat):
            with case.assertRaisesRegex(AssertionError,
                                        'source scan encountered a directory cycle at app/tests/nested'):
                _scan_sources(root)
        original_scandir = os.scandir

        def denied_scandir(path):
            if Path(path) == nested:
                raise PermissionError('synthetic unreadable directory')
            return original_scandir(path)

        with mock.patch.object(os, 'scandir', side_effect=denied_scandir):
            with case.assertRaisesRegex(AssertionError,
                                        'source scan cannot read directory app/tests/nested'):
                _scan_sources(root)
        for limit, message in (('SOURCE_SCAN_MAX_DIRECTORIES', 'directory limit'),
                               ('SOURCE_SCAN_MAX_ENTRIES', 'entry limit')):
            with mock.patch.dict(globals(), {limit: 1}):
                with case.assertRaisesRegex(AssertionError, 'source scan exceeded ' + message):
                    _scan_sources(root)


def _assert_alias_gate_contract(case):
    alias_source = ALIAS_SCRIPT.read_text(encoding='utf-8')
    alias_tree = ast.parse(alias_source)
    alias_gate = _load_alias_gate()
    derived = alias_gate.derived_test_ids()
    case.assertEqual(alias_gate.EXPECTED_TEST_COUNT, len(derived))
    case.assertEqual(55, len(derived))
    case.assertEqual(alias_gate.EXPECTED_TEST_IDS_SHA256, alias_gate.identity_digest(derived))
    case.assertTrue(alias_gate._source_identity_matches(derived))
    case.assertFalse(alias_gate._source_identity_matches(derived[:-1]))
    case.assertFalse(alias_gate._source_identity_matches(derived + ['tests.fixture.Case.test_case']))
    case.assertNotEqual(alias_gate.identity_digest(derived), alias_gate.identity_digest(derived[:-1]))
    modules = next(node for node in alias_tree.body if isinstance(node, ast.Assign)
                   and getattr(node.targets[0], 'id', '') == 'MODULES')
    module_names = tuple(item.value for item in modules.value.elts)
    case.assertEqual(('tests.test_sound_design_stage_ownership',
                      'tests.test_sound_design_upload_ownership',
                      'tests.test_source_upload_stage',
                      'tests.test_sound_design_asset_routes'), module_names)
    return alias_gate, module_names


def _assert_native_decisions(case, workflow, required):
    case.assertIn('windows-latest', _matrix_os(_job_lines(workflow, 'native')),
                  'native matrix must run on windows-latest')
    declared = _declared_paths(workflow)
    missing = sorted(set(required) - set(declared))
    case.assertEqual([], missing, f'native path filters must trigger on {missing}')


def _assert_workflow_contract(case):
    sound_assets = WORKFLOW.read_text(encoding='utf-8')
    quote = chr(39)
    required = _required_paths()
    case.assertEqual(11, len(required))
    _assert_native_decisions(case, sound_assets, required)
    install = sound_assets.index('Install upload-route CPU test dependencies')
    modules_step = sound_assets.index(NORMAL_STEP)
    alias_step = sound_assets.index(ALIAS_COMMAND)
    case.assertLess(install, modules_step)
    case.assertLess(modules_step, alias_step)
    native_job_lines = _job_lines(sound_assets, 'native')
    native_job = '\n'.join(native_job_lines)
    case.assertIn('    runs-on: ${{ matrix.os }}', native_job)
    case.assertNotIn('continue-on-error:', native_job)
    trigger = _pull_request(_trigger_lines(sound_assets))
    case.assertIsNotNone(trigger)
    case.assertEqual([], _paths_ignore(trigger))
    case.assertIsNone(_pull_request(['on:', '  push:']))
    case.assertNotEqual([], _paths_ignore(_pull_request(
        ['on:', '  pull_request:', '    paths-ignore:', '      app/tests/**'])))
    normal = _step(native_job_lines, NORMAL_STEP)
    case.assertNotIn('if', normal)
    case.assertEqual('python -m unittest ' + ' '.join(_alias_module_names()) + ' -v', normal['run'])
    alias = _step(native_job_lines, ALIAS_STEP)
    case.assertEqual(ALIAS_COMMAND, alias['run'])
    gate_value = alias.get('if')
    case.assertIsNotNone(gate_value, f'{ALIAS_STEP} must declare a step-level if gate')
    gate = _gate(gate_value)
    case.assertIsNotNone(gate)
    case.assertEqual('Windows', gate.comparators[0].value)
    nested_job = ['  native:', '    steps:', '      - name: ' + ALIAS_STEP, '        env:',
                  '          if: runner.os == ' + quote + 'Windows' + quote,
                  '        run: ' + ALIAS_COMMAND]
    values = _step(nested_job, ALIAS_STEP)
    case.assertNotIn('if', values)
    with case.assertRaisesRegex(AssertionError, 'step-level if'):
        _gate(values.get('if'))
    quoted_job = ['  native:', '    steps:', '      - name: ' + quote + ALIAS_STEP + quote,
                  '        run: ' + ALIAS_COMMAND]
    case.assertEqual(ALIAS_COMMAND, _step(quoted_job, ALIAS_STEP)['run'])
    braced_job = ['  native:', '    steps:', '      - name: ' + ALIAS_STEP,
                  '        if: ${{ runner.os == ' + quote + 'Windows' + quote + ' }}',
                  '        run: ' + ALIAS_COMMAND]
    case.assertIsNotNone(_gate(_step(braced_job, ALIAS_STEP)['if']))
    commented_job = ['  native:', '    steps:', '      - name: ' + ALIAS_STEP,
                     '        if: runner.os == ' + quote + 'Windows' + quote + '  # hosted',
                     '        run: ' + ALIAS_COMMAND + '  # exact command']
    commented = _step(commented_job, ALIAS_STEP)
    case.assertEqual(ALIAS_COMMAND, commented['run'])
    case.assertEqual('Windows', _gate(commented['if']).comparators[0].value)
    hashed = ['  native:', '    steps:', '      - name: ' + ALIAS_STEP,
              '        run: echo ' + quote + '#keep' + quote]
    case.assertEqual('echo ' + quote + '#keep' + quote, _step(hashed, ALIAS_STEP)['run'])
    changed = ['  native:', '    steps:', '      - name: ' + ALIAS_STEP,
               '        run: python tests/another_script.py']
    case.assertNotEqual(ALIAS_COMMAND, _step(changed, ALIAS_STEP)['run'])
    with case.assertRaisesRegex(AssertionError, 'Expected one step'):
        _step(native_job_lines, 'Verify a step that does not exist')

    def _replace(text, old, new):
        changed_text = text.replace(old, new, 1)
        case.assertNotEqual(text, changed_text, f'fixture mutation did not apply: {old!r}')
        return changed_text

    def _refuses(workflow, pattern):
        with case.assertRaisesRegex(AssertionError, pattern) as refusal:
            _assert_native_decisions(case, workflow, required)
        return str(refusal.exception)

    declared = _declared_paths(sound_assets)
    case.assertIn('app/sound_design_*.py', declared)
    case.assertIn('windows-latest', _matrix_os(native_job_lines))
    block_job = ['  native:', '    strategy:', '      matrix:', '        os:',
                 '          - ubuntu-24.04', '          - windows-latest']
    case.assertEqual(['ubuntu-24.04', 'windows-latest'], _matrix_os(block_job))
    flow_trigger = ('on:\n  pull_request:\n'
                    "    paths: ['app/a.py', 'app/b.py']  # inline comment")
    case.assertEqual(['app/a.py', 'app/b.py'], _declared_paths(flow_trigger))
    quoted_trigger = ('on:\n  pull_request:\n    paths:\n'
                      "      - 'app/a.py'  # first\n"
                      '      # interleaved comment\n'
                      '      - app/b.py\n')
    case.assertEqual(['app/a.py', 'app/b.py'], _declared_paths(quoted_trigger))

    matrix_only_in_comment = _replace(
        sound_assets, '[ubuntu-24.04, windows-latest, macos-latest]',
        '[ubuntu-24.04, macos-latest]  # windows-latest')
    message = _refuses(matrix_only_in_comment, 'must run on windows-latest')
    case.assertIn("['ubuntu-24.04', 'macos-latest']", message)
    sibling_key = _replace(
        sound_assets, '[ubuntu-24.04, windows-latest, macos-latest]',
        '[ubuntu-24.04, macos-latest]\n        python: [windows-latest]')
    message = _refuses(sibling_key, 'must run on windows-latest')
    case.assertIn("['ubuntu-24.04', 'macos-latest']", message)
    run_mention = _replace(
        sound_assets, '[ubuntu-24.04, windows-latest, macos-latest]',
        '[ubuntu-24.04, macos-latest]')
    run_mention = _replace(
        run_mention, '        run: ' + ALIAS_COMMAND,
        '        run: echo windows-latest  # ' + ALIAS_COMMAND)
    message = _refuses(run_mention, 'must run on windows-latest')
    case.assertIn("['ubuntu-24.04', 'macos-latest']", message)
    entry = '      - ' + quote + 'app/upload_operation.py' + quote
    case.assertIn(entry, sound_assets)
    commented_path = _replace(sound_assets, entry, '      #' + entry)
    case.assertNotIn('app/upload_operation.py', _declared_paths(commented_path))
    message = _refuses(commented_path, 'must trigger on')
    case.assertIn("'app/upload_operation.py'", message)
    pushed = _replace(sound_assets, entry + '\n', '')
    pushed = _replace(pushed, 'on:\n  pull_request:',
                      'on:\n  push:\n    paths:\n' + entry + '\n  pull_request:')
    case.assertNotIn('app/upload_operation.py', _declared_paths(pushed))
    message = _refuses(pushed, 'must trigger on')
    case.assertIn("'app/upload_operation.py'", message)
    step_named = ('on:\n  pull_request:\n    paths:\n      - ' + quote
                  + 'app/routers/sound_design.py' + quote
                  + '\njobs:\n  native:\n    steps:\n'
                    '      - name: verify app/upload_operation.py\n'
                    '        run: echo ok\n')
    case.assertIn('app/upload_operation.py', step_named)
    case.assertEqual(['app/routers/sound_design.py'], _declared_paths(step_named))

    with case.assertRaisesRegex(AssertionError, "Expected one 'paths' entry") as missing_paths:
        _declared_paths('\n'.join(['on:', '  pull_request:', '    branches:',
                                   '      - main']))
    case.assertIn('found 0', str(missing_paths.exception))
    with case.assertRaisesRegex(AssertionError, "Expected one 'matrix' entry"):
        _matrix_os(['  native:', '    strategy:', '      fail-fast: false'])
    with case.assertRaisesRegex(AssertionError, "Expected one 'paths' entry") as duplicate_key:
        _declared_paths('\n'.join(['on:', '  pull_request:', '    paths:',
                                   "      - 'app/a.py'", '    paths:',
                                   "      - 'app/b.py'"]))
    case.assertIn('found 2', str(duplicate_key.exception))
    with case.assertRaisesRegex(AssertionError, "Expected one 'os' entry") as duplicate_os:
        _matrix_os(['  native:', '    strategy:', '      matrix:', '        os: [a]',
                    '        os: [b]'])
    case.assertIn('found 2', str(duplicate_os.exception))
    with case.assertRaisesRegex(AssertionError, 'repeats an entry'):
        _declared_paths('\n'.join(['on:', '  pull_request:', '    paths:',
                                   "      - 'app/a.py'", '      - app/a.py']))
    with case.assertRaisesRegex(AssertionError, 'flow or block sequence'):
        _declared_paths('\n'.join(['on:', '  pull_request:',
                                   '    paths: ' + quote + 'app/a.py' + quote]))
    with case.assertRaisesRegex(AssertionError, 'is not a sequence item'):
        _declared_paths('\n'.join(['on:', '  pull_request:', '    paths:',
                                   '      dependencies: core']))
    with case.assertRaisesRegex(AssertionError, 'sequence is malformed'):
        _matrix_os(['  native:', '    strategy:', '      matrix:', '        os: []'])
    with case.assertRaisesRegex(AssertionError, 'negative path filters are not supported'):
        _declared_paths('\n'.join(['on:', '  pull_request:', '    paths:',
                                   "      - '!app/a.py'"]))
    with case.assertRaisesRegex(AssertionError, 'include.* expansion is not supported'):
        _matrix_os(['  native:', '    strategy:', '      matrix:', '        os: [ubuntu-24.04]',
                    '        include:', '          - os: windows-latest'])
    with case.assertRaisesRegex(AssertionError, 'exclude.* expansion is not supported'):
        _matrix_os(['  native:', '    strategy:', '      matrix:', '        os: [windows-latest]',
                    '        exclude:', '          - os: windows-latest'])


def _alias_module_names():
    alias_gate = _load_alias_gate()
    return alias_gate.MODULES


def _load_alias_gate():
    specification = importlib.util.spec_from_file_location('alias_gate_fixture', ALIAS_SCRIPT)
    alias_gate = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(alias_gate)
    return alias_gate


def _assert_alias_argv(case):
    alias_gate = _load_alias_gate()
    with mock.patch.object(alias_gate.sys, 'platform', 'linux'), \
            mock.patch.object(alias_gate, '_run_checks') as run_checks, \
            mock.patch.object(alias_gate, '_run_modules_under_alias') as run_worker:
        for arguments in ([], [alias_gate.WORKER_FLAG], [alias_gate.WORKER_FLAG, 'a', 'b'],
                          ['--unexpected']):
            with case.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                case.assertEqual(2, alias_gate.main(arguments))
        run_checks.assert_not_called()
        run_worker.assert_not_called()
    if sys.platform != 'win32':
        completed = subprocess.run([sys.executable, '-B', str(ALIAS_SCRIPT)],
                                   capture_output=True, text=True, timeout=120)
        case.assertEqual(2, completed.returncode, completed.stderr)
        case.assertIn('nothing was verified', completed.stderr)


def _assert_alias_receipt(case):
    alias_gate = _load_alias_gate()
    receipt = {'outcome': 'passed', 'tests': alias_gate.EXPECTED_TEST_COUNT,
               'identity_sha256': alias_gate.EXPECTED_TEST_IDS_SHA256,
               'skips': 0, 'failures': 0, 'errors': 0,
               'success': True, 'store_refused_alias': True}
    with mock.patch.object(alias_gate, '_create_aliased_roots',
                           return_value=(Path('base'), Path('target'), Path('alias'))), \
            mock.patch.object(alias_gate, '_remove_aliased_roots') as cleanup, \
            mock.patch.object(alias_gate, '_verify_hostile_junction'), \
            mock.patch.object(alias_gate.subprocess, 'run',
                              return_value=subprocess.CompletedProcess(
                                  [], 0, json.dumps(receipt), '')), \
            contextlib.redirect_stdout(io.StringIO()):
        case.assertEqual(0, alias_gate._run_checks())
        cleanup.assert_called_once_with(Path('base'), Path('alias'))
    invalid_fields = {'outcome': 'suite-failed', 'tests': 54,
                      'identity_sha256': '0' * 64, 'skips': 1, 'failures': 1,
                      'errors': 1, 'success': False, 'store_refused_alias': False}
    invalid_receipts = [dict(receipt, **{field: value})
                        for field, value in invalid_fields.items()]
    invalid_receipts.extend({field: value for field, value in receipt.items()
                             if field != missing} for missing in invalid_fields)
    passed_line = json.dumps(receipt)
    failed_line = json.dumps(dict(receipt, outcome='suite-failed'))
    trailing_noise = '\nnot JSON\n[]\ntrue\n7\n{"diagnostic": "only"}\n'
    case.assertEqual(receipt, alias_gate._read_receipt(passed_line + trailing_noise))
    case.assertEqual('suite-failed', alias_gate._read_receipt(
        passed_line + '\n' + failed_line + trailing_noise)['outcome'])
    case.assertEqual(receipt, alias_gate._read_receipt(
        failed_line + '\n' + passed_line + trailing_noise))
    case.assertIsNone(alias_gate._read_receipt(trailing_noise))
    completed_cases = [subprocess.CompletedProcess([], 0, json.dumps(invalid), '')
                       for invalid in invalid_receipts]
    completed_cases.extend((
        subprocess.CompletedProcess([], 0, passed_line + '\n' + failed_line, ''),
        subprocess.CompletedProcess([], 1, '', 'fixture failure')))
    for index, completed in enumerate(completed_cases):
        with case.subTest(alias_receipt=index), \
                mock.patch.object(alias_gate, '_create_aliased_roots',
                                  return_value=(Path('base'), Path('target'), Path('alias'))), \
                mock.patch.object(alias_gate, '_remove_aliased_roots') as cleanup, \
                mock.patch.object(alias_gate, '_verify_hostile_junction'), \
                mock.patch.object(alias_gate.subprocess, 'run', return_value=completed), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            case.assertEqual(1, alias_gate._run_checks())
            cleanup.assert_called_once_with(Path('base'), Path('alias'))


def _assert_alias_containment(case):
    alias_gate = _load_alias_gate()
    with tempfile.TemporaryDirectory(prefix=alias_gate.BASE_PREFIX) as directory:
        base = Path(directory)
        marker = base / 'foreign-marker'
        marker.write_bytes(b'preserve')
        for candidate, alias_path in (
                (base, base / 'wrong-alias'),
                (base / '..' / base.name, base / '..' / base.name / 'aliased-root'),
                (base / (alias_gate.BASE_PREFIX + 'nested'),
                 base / (alias_gate.BASE_PREFIX + 'nested') / 'aliased-root')):
            with case.subTest(candidate=candidate), \
                    mock.patch.object(alias_gate.os, 'rmdir') as remove_alias, \
                    mock.patch.object(alias_gate.shutil, 'rmtree') as remove_root:
                with case.assertRaises(RuntimeError):
                    alias_gate._remove_aliased_roots(candidate, alias_path)
                remove_alias.assert_not_called()
                remove_root.assert_not_called()
                case.assertEqual(b'preserve', marker.read_bytes())
    with tempfile.TemporaryDirectory(prefix='foreign-prefix-') as directory:
        base = Path(directory)
        with mock.patch.object(alias_gate.os, 'rmdir') as remove_alias, \
                mock.patch.object(alias_gate.shutil, 'rmtree') as remove_root:
            with case.assertRaises(RuntimeError):
                alias_gate._remove_aliased_roots(base, base / 'aliased-root')
            remove_alias.assert_not_called()
            remove_root.assert_not_called()
    created = []
    real_mkdtemp = tempfile.mkdtemp
    def create_root(*args, **kwargs):
        directory = real_mkdtemp(*args, **kwargs)
        created.append(Path(directory))
        return directory
    with mock.patch.object(alias_gate.tempfile, 'mkdtemp', side_effect=create_root), \
            mock.patch.object(alias_gate, '_create_junction',
                              side_effect=RuntimeError(
                                  'Could not create a real junction: fixture failure')):
        with case.assertRaisesRegex(RuntimeError, 'Could not create a real junction'):
            alias_gate._create_aliased_roots()
    case.assertEqual(1, len(created))
    case.assertFalse(created[0].exists())
    case.assertTrue(alias_gate._store_refusal('root without aliases'))
    case.assertFalse(alias_gate._store_refusal('another failure'))


def _assert_alias_junction_primitive(case):
    alias_source = ALIAS_SCRIPT.read_text(encoding='utf-8')
    tree = ast.parse(alias_source)
    alias_gate = _load_alias_gate()
    legacy = json.dumps(['cmd', '/c', 'mklink', '/J', 'aliased-root', 'canonical-root'])
    case.assertNotIn('mklink', alias_source)
    case.assertNotIn(legacy, alias_source)
    case.assertEqual([], [node.lineno for node in ast.walk(tree)
                          if isinstance(node, ast.Constant) and node.value == 'cmd'])
    case.assertEqual([], [node.lineno for node in ast.walk(tree)
                          if isinstance(node, ast.Call)
                          and _dotted(node.func).split('.')[0] == 'subprocess'
                          and any(keyword.arg == 'shell' for keyword in node.keywords)],
                     'the alias gate must hand argv to the child, never a shell')
    case.assertIn('st_reparse_tag', alias_source)
    case.assertIn('IO_REPARSE_TAG_MOUNT_POINT', alias_source)
    native = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
              and (node.module or '').split('.')[0] == '_winapi']
    case.assertEqual(1, len(native), 'the native junction primitive is imported once')
    junction = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_create_junction')
    case.assertIn(native[0], list(ast.walk(junction)),
                  'the native primitive stays deferred inside its helper')
    target = Path('owned-base') / 'canonical-root'
    alias = Path('owned-base') / 'aliased-root'
    fake = mock.Mock()
    with mock.patch.dict(sys.modules, {'_winapi': fake}):
        alias_gate._create_junction(target, alias)
    fake.CreateJunction.assert_called_once_with(str(target), str(alias))
    with mock.patch.dict(sys.modules, {'_winapi': None}):
        with case.assertRaisesRegex(RuntimeError, '_winapi[.]CreateJunction'):
            alias_gate._create_junction(target, alias)
    with mock.patch.dict(sys.modules, {'_winapi': types.ModuleType('_winapi')}):
        with case.assertRaisesRegex(RuntimeError, '_winapi[.]CreateJunction'):
            alias_gate._create_junction(target, alias)
    failing = mock.Mock()
    failing.CreateJunction.side_effect = OSError(5, 'fixture access denied')
    with mock.patch.dict(sys.modules, {'_winapi': failing}):
        with case.assertRaisesRegex(RuntimeError, 'Could not create a real junction') as caught:
            alias_gate._create_junction(target, alias)
    case.assertIn('fixture access denied', str(caught.exception))


def _conditional_source(platform, wrapper):
    guard = (f"if sys.platform == '{platform}':\n"
             + ' ' * 4 + 'RUN = True\n'
             + 'else:\n'
             + ' ' * 4 + 'RUN = False')
    body = wrapper(guard)
    return ('import contextlib\nimport sys\nimport unittest\n' + body
            + '\nclass ConditionalCase(unittest.TestCase):\n'
            + '    @unittest.skipUnless(RUN, ' + repr('w') + ')\n'
            + '    def test_case(self):\n        pass\n')


WRAPPERS = {
    'plain': lambda guard: guard,
    'for': lambda guard: 'for _ in range(1):\n' + '\n'.join('    ' + line for line in guard.splitlines()),
    'try': lambda guard: ('try:\n' + '\n'.join('    ' + line for line in guard.splitlines())
                          + '\nexcept Exception:\n    RUN = None'),
    'with': lambda guard: ('with contextlib.nullcontext():\n'
                           + '\n'.join('    ' + line for line in guard.splitlines())),
    'match': lambda guard: ('match 1:\n    case 1:\n'
                            + '\n'.join('        ' + line for line in guard.splitlines())),
}


def _assignment_dumps(source):
    return [ast.dump(node) for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Assign)]


def _assert_csp_controls(case):
    registry_row = 'fixture:Case.test:0 = ' + '0' * 64 + '  # source-pinned'
    case.assertEqual(1, len(_parse_registry(registry_row)))
    for duplicate in (registry_row, registry_row.replace('0' * 64, '1' * 64)):
        with case.assertRaisesRegex(AssertionError, 'repeats location'):
            _parse_registry(registry_row + '\n' + duplicate)
    platform_imports = 'import os\nimport platform\nimport sys\nimport unittest\n'
    nested_definition = ('class NestedCase(unittest.TestCase):\n'
                         '    @unittest.skipUnless(True, "control")\n'
                         '    def test_case(self):\n        pass\n')
    containers = {
        'if': ('if True:\n', ''),
        'else': ('if False:\n    pass\nelse:\n', ''),
        'for': ('for item in ():\n', ''),
        'for_else': ('for item in ():\n    pass\nelse:\n', ''),
        'while': ('while False:\n', ''),
        'try': ('try:\n', 'except Exception:\n    pass\n'),
        'except': ('try:\n    pass\nexcept Exception:\n', ''),
        'finally': ('try:\n    pass\nfinally:\n', ''),
        'with': ('with contextlib.nullcontext():\n', ''),
        'match': ('match 1:\n    case 1:\n', ''),
    }
    for container, (opening, closing) in containers.items():
        indent = '        ' if container == 'match' else '    '
        wrapped = opening + ''.join(indent + line + '\n'
            for line in nested_definition.splitlines()) + closing
        for scope in ('module', 'class'):
            prefix = '' if scope == 'module' else 'Outer.'
            body = wrapped if scope == 'module' else ('class Outer:\n' +
                ''.join('    ' + line + '\n' for line in wrapped.splitlines()))
            nested = _fixture_census({'app/tests/test_fixture.py': platform_imports + body})
            case.assertEqual([f'test_fixture.py:{prefix}NestedCase.test_case:0'],
                             list(nested.sites), (container, scope))
    duplicate_site = platform_imports + nested_definition + nested_definition
    with case.assertRaisesRegex(AssertionError, 'census repeats location'):
        _fixture_census({'app/tests/test_fixture.py': duplicate_site})
    alias_source = platform_imports + (
        'import unittest as ut\n'
        'class AliasCase(ut.TestCase):\n'
        "    @ut.skipUnless(sys.platform == 'win32', 'w')\n"
        '    def test_case(self):\n        pass\n')
    alias = _fixture_census({'app/tests/test_fixture.py': alias_source})
    case.assertEqual(['test_fixture.py:AliasCase.test_case:0'], list(alias.sites))
    case.assertEqual(['test_fixture.py:AliasCase.test_case:0'], alias.refused)
    keyword_source = alias_source.replace(
        "sys.platform == 'win32', 'w'", "condition=sys.platform == 'win32', reason='w'")
    keyword = _fixture_census({'app/tests/test_fixture.py': keyword_source})
    case.assertEqual(alias.refused, keyword.refused,
                     'keyword conditions must have the same refusal as positional conditions')
    unknown_keyword = keyword_source.replace("sys.platform == 'win32'", 'UNKNOWN_RUN')
    case.assertTrue(_fixture_census(
        {'app/tests/test_fixture.py': unknown_keyword}).unresolved,
        'a keyword predicate must still check unknown names')
    bound_keyword = platform_imports + (
        "WINDOWS_ONLY = unittest.skipUnless(condition=sys.platform == 'win32', reason='w')\n"
        'class BoundCase(unittest.TestCase):\n'
        '    @WINDOWS_ONLY\n    def test_case(self):\n        pass\n')
    case.assertEqual(['test_fixture.py:BoundCase.test_case:0'],
                     _fixture_census({'app/tests/test_fixture.py': bound_keyword}).refused)
    for arguments in ("**{'condition': sys.platform == 'win32', 'reason': 'w'}",
                      "*(sys.platform == 'win32', 'w')"):
        for bound in (False, True):
            unpacked_source = platform_imports + (
                f'WINDOWS_ONLY = unittest.skipUnless({arguments})\n' if bound else '')
            unpacked_source += (
                'class UnpackedCase(unittest.TestCase):\n'
                + ('    @WINDOWS_ONLY\n' if bound else
                   f'    @unittest.skipUnless({arguments})\n')
                + '    def test_case(self):\n        pass\n')
            unpacked = _fixture_census({'app/tests/test_fixture.py': unpacked_source})
            case.assertEqual(['test_fixture.py:UnpackedCase.test_case:0'],
                             list(unpacked.sites))
            case.assertEqual(list(unpacked.sites), unpacked.ambiguous,
                             'unpacked skip arguments must fail closed without evaluation')
            unpacked_site = next(iter(unpacked.sites.values()))
            unpacked_rows = {unpacked_site.key: (unpacked_site.digest('reviewed'), 'reviewed')}
            case.assertTrue(_verify_registry(unpacked, unpacked_rows),
                            'a matching row cannot authorize opaque skip arguments')
    helper_shapes = (
        ('def W():\n    return unittest.skipUnless({0})\n'
         'class HelperCase(unittest.TestCase):\n'
         '    @W()\n    def test_case(self):\n        pass\n'),
        ('def W():\n    return unittest.skipUnless({0})\n'
         'WINDOWS_ONLY = W()\n'
         'class HelperCase(unittest.TestCase):\n'
         '    @WINDOWS_ONLY\n    def test_case(self):\n        pass\n'),
        ('class HelperCase(unittest.TestCase):\n'
         '    def W():\n        return unittest.skipUnless({0})\n'
         '    @W()\n    def test_case(self):\n        pass\n'),
    )
    for arguments in ("**{'condition': sys.platform == 'win32', 'reason': 'w'}",
                      "*(sys.platform == 'win32', 'w')"):
        for shape in helper_shapes:
            helper_source = platform_imports + shape.format(arguments)
            helper = _fixture_census({'app/tests/test_fixture.py': helper_source})
            case.assertEqual(['test_fixture.py:HelperCase.test_case:0'], list(helper.sites))
            case.assertEqual(list(helper.sites), helper.ambiguous,
                             'recognized helper unpacking must fail closed')
            helper_site = next(iter(helper.sites.values()))
            helper_rows = {helper_site.key: (helper_site.digest('reviewed'), 'reviewed')}
            case.assertTrue(_verify_registry(helper, helper_rows),
                            'a matching row cannot authorize helper unpacking')

    def class_default_source(literal, parameter='flag=FLAG'):
        return platform_imports + (
            'class ClassDefaultCase(unittest.TestCase):\n'
            f'    FLAG = sys.platform == {literal!r}\n'
            f'    def W({parameter}):\n'
            "        return unittest.skipUnless(flag, 'w')\n"
            '    @W()\n    def test_case(self):\n        pass\n')

    for parameter in ('flag=FLAG', '*, flag=FLAG'):
        default_rows = {}
        for literal in ('win32', 'linux'):
            default_source = class_default_source(literal, parameter)
            _default_result, default_site = _single_site(
                case, {'app/tests/test_fixture.py': default_source})
            digest = default_site.digest('class default')
            if not default_rows:
                default_rows[default_site.key] = (digest, 'class default')
                case.assertEqual([], _verify_registry(
                    _census({'app/tests/test_fixture.py': default_source},
                            _Sources({'app/tests/test_fixture.py': default_source}.get),
                            {default_site.key: 'class default'}), default_rows))
                continue
            case.assertNotEqual(list(default_rows.values())[0][0], digest,
                                f'{parameter}: a class-body default binding must be pinned')
            case.assertTrue(_verify_registry(
                _census({'app/tests/test_fixture.py': default_source},
                        _Sources({'app/tests/test_fixture.py': default_source}.get),
                        {default_site.key: 'class default'}), default_rows),
                f'{parameter}: a stale class-default row must fail after the constant changed')
    spaced_source = class_default_source('win32').replace(
        "    def W(flag=FLAG):\n        return unittest.skipUnless(flag, 'w')\n",
        "    # reviewed default\n\n    def W(flag=FLAG):\n"
        "        return unittest.skipUnless( flag, 'w' )\n")
    _spaced_result, spaced_site = _single_site(case, {'app/tests/test_fixture.py': spaced_source})
    _default_result, default_site = _single_site(
        case, {'app/tests/test_fixture.py': class_default_source('win32')})
    case.assertEqual(default_site.digest('class default'), spaced_site.digest('class default'),
                     'whitespace and comments must not move an AST digest')
    default_homonym = platform_imports + (
        "FLAG = sys.platform == 'darwin'\n"
        'class DefaultHomonymCase(unittest.TestCase):\n'
        "    FLAG = sys.platform == 'win32'\n"
        '    def W(flag=FLAG):\n'
        "        return unittest.skipUnless(flag, 'w')\n"
        '    @W()\n    def test_case(self):\n        pass\n')
    _homonym_result, default_homonym_site = _single_site(
        case, {'app/tests/test_fixture.py': default_homonym})
    default_homonym_flip = default_homonym.replace(
        "FLAG = sys.platform == 'darwin'", "FLAG = sys.platform == 'linux'", 1)
    _homonym_flip_result, default_homonym_flip_site = _single_site(
        case, {'app/tests/test_fixture.py': default_homonym_flip})
    case.assertEqual(default_homonym_site.digest('default homonym'),
                     default_homonym_flip_site.digest('default homonym'),
                     'a default argument resolves in the class body, not the module')
    split_scope = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'class SplitScopeCase(unittest.TestCase):\n'
        '    FLAG = True\n'
        '    def W(flag=FLAG):\n'
        "        return unittest.skipUnless(flag and FLAG, 'w')\n"
        '    @W()\n    def test_case(self):\n        pass\n')
    _split_result, split_site = _single_site(case, {'app/tests/test_fixture.py': split_scope})
    split_class_flip = split_scope.replace('    FLAG = True', '    FLAG = False')
    _split_class_result, split_class_site = _single_site(
        case, {'app/tests/test_fixture.py': split_class_flip})
    case.assertNotEqual(split_site.digest('split scope'), split_class_site.digest('split scope'),
                        'the class default binding must be pinned beside a module homonym')
    split_module_flip = split_scope.replace("FLAG = sys.platform == 'win32'",
                                            "FLAG = sys.platform == 'linux'", 1)
    _split_module_result, split_module_site = _single_site(
        case, {'app/tests/test_fixture.py': split_module_flip})
    case.assertNotEqual(split_site.digest('split scope'), split_module_site.digest('split scope'),
                        'a function body must still pin the module binding of the same name')
    body_scope = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'class BodyScopeCase(unittest.TestCase):\n'
        '    FLAG = False\n'
        '    def W():\n'
        "        return unittest.skipUnless(FLAG, 'w')\n"
        '    @W()\n    def test_case(self):\n        pass\n')
    _body_result, body_site = _single_site(case, {'app/tests/test_fixture.py': body_scope})
    body_class_flip = body_scope.replace('    FLAG = False', '    FLAG = True')
    _body_class_result, body_class_site = _single_site(
        case, {'app/tests/test_fixture.py': body_class_flip})
    case.assertEqual(body_site.digest('body scope'), body_class_site.digest('body scope'),
                     'a class attribute is not a lexical binding of a method body')
    body_module_flip = body_scope.replace("FLAG = sys.platform == 'win32'",
                                          "FLAG = sys.platform == 'linux'", 1)
    _body_module_result, body_module_site = _single_site(
        case, {'app/tests/test_fixture.py': body_module_flip})
    case.assertNotEqual(body_site.digest('body scope'), body_module_site.digest('body scope'),
                        'a method body must resolve module globals, not the class body')
    applied_source = platform_imports + (
        'class AppliedCase(unittest.TestCase):\n'
        '    def test_case(self):\n        pass\n'
        "AppliedCase.test_case = unittest.skipUnless(os.name == 'nt', 'w')(AppliedCase.test_case)\n")
    applied = _fixture_census({'app/tests/test_fixture.py': applied_source})
    case.assertEqual([], list(applied.sites))
    case.assertTrue(any('unsupported assignment-applied skip; rewrite as decorator' in item
                        for item in applied.unresolved))
    loop_source = platform_imports + (
        "for WINDOWS_ONLY in (unittest.skipUnless(sys.platform == 'win32', 'w'),):\n    pass\n"
        'class LoopCase(unittest.TestCase):\n'
        '    @WINDOWS_ONLY\n    def test_case(self):\n        pass\n')
    loop = _fixture_census({'app/tests/test_fixture.py': loop_source})
    case.assertEqual(['test_fixture.py:LoopCase.test_case:0'], loop.ambiguous,
                     'a non-value binding must fail with a named ambiguity, not an AST crash')
    star_source = platform_imports + (
        'from unittest import *\n'
        'class StarCase(unittest.TestCase):\n'
        "    @skipUnless(sys.platform == 'win32', 'w')\n"
        '    def test_case(self):\n        pass\n')
    star = _fixture_census({'app/tests/test_fixture.py': star_source})
    case.assertEqual(['test_fixture.py:StarCase.test_case:0'], list(star.sites))
    case.assertEqual(['test_fixture.py:StarCase.test_case:0'], star.refused)
    helper = ('import sys\nimport unittest\n'
              "WINDOWS_ONLY = unittest.skipUnless(sys.platform == 'win32', 'w')\n")
    imported = {'app/tests/test_fixture.py': (
        'import unittest\nfrom tests.fixture_helper import WINDOWS_ONLY\n'
        'class ImportedCase(unittest.TestCase):\n'
        '    @WINDOWS_ONLY\n    def test_case(self):\n        pass\n'),
        'app/tests/fixture_helper.py': helper}
    result = _fixture_census(imported)
    case.assertEqual(['test_fixture.py:ImportedCase.test_case:0'], list(result.sites))
    case.assertEqual(['test_fixture.py:ImportedCase.test_case:0'], result.refused)
    site = result.sites['test_fixture.py:ImportedCase.test_case:0']
    case.assertEqual('app/tests/fixture_helper.py', site.entries[0][0])
    aliased = {'app/tests/test_fixture.py': (
        'import tests.fixture_helper as helper\nimport unittest\n'
        'class AliasedCase(unittest.TestCase):\n'
        '    @helper.WINDOWS_ONLY\n    def test_case(self):\n        pass\n'),
        'app/tests/fixture_helper.py': helper}
    case.assertEqual(['test_fixture.py:AliasedCase.test_case:0'], list(_fixture_census(aliased).refused))

    for name, wrapper in WRAPPERS.items():
        before = _conditional_source('win32', wrapper)
        after = _conditional_source('linux', wrapper)
        case.assertEqual(_assignment_dumps(before), _assignment_dumps(after),
                         f'{name}: only the guard predicate changed')
        _result_before, site_before = _single_site(case, {'app/tests/test_fixture.py': before})
        _result_after, site_after = _single_site(case, {'app/tests/test_fixture.py': after})
        case.assertNotEqual(site_before.digest('declaration ' + name),
                            site_after.digest('declaration ' + name),
                            f'{name}: a predicate-only mutation must change the closure digest')

    helper_linux = ('import sys\ndef supported():\n    return sys.platform == ' + repr('linux') + '\n')
    helper_windows = ('import sys\ndef supported():\n    return sys.platform == ' + repr('win32') + '\n')
    module = ('import unittest\nfrom tests.fixture_helper import supported\n'
              'class HelperCase(unittest.TestCase):\n'
              '    @unittest.skipUnless(supported(), ' + repr('w') + ')\n'
              '    def test_case(self):\n        pass\n')
    _linux_result, linux_site = _single_site(
        case, {'app/tests/test_fixture.py': module, 'app/tests/fixture_helper.py': helper_linux})
    _windows_result, windows_site = _single_site(
        case, {'app/tests/test_fixture.py': module, 'app/tests/fixture_helper.py': helper_windows})
    declaration = 'skipUnless; linux-outcome=environment; condition=supported()'
    case.assertNotEqual(linux_site.digest(declaration), windows_site.digest(declaration))
    linux_rows = {linux_site.key: (linux_site.digest(declaration), declaration)}
    windows_rows = {windows_site.key: (windows_site.digest(declaration), declaration)}
    case.assertEqual([], _verify_registry(
        _census({'app/tests/test_fixture.py': module,
                 'app/tests/fixture_helper.py': helper_linux},
                _Sources({'app/tests/test_fixture.py': module,
                          'app/tests/fixture_helper.py': helper_linux}.get),
                {linux_site.key: declaration}), linux_rows))
    case.assertNotEqual([], _verify_registry(
        _census({'app/tests/test_fixture.py': module,
                 'app/tests/fixture_helper.py': helper_windows},
                _Sources({'app/tests/test_fixture.py': module,
                          'app/tests/fixture_helper.py': helper_windows}.get),
                {linux_site.key: declaration}), linux_rows))

    cycle = ('import sys\nimport unittest\n'
             "RUN = RUN or sys.platform == 'win32'\n"
             'class CycleCase(unittest.TestCase):\n'
             '    @unittest.skipUnless(RUN, ' + repr('w') + ')\n'
             '    def test_case(self):\n        pass\n')
    _cycle_result, cycle_site = _single_site(case, {'app/tests/test_fixture.py': cycle})
    repeated_result, repeated_site = _single_site(case, {'app/tests/test_fixture.py': cycle})
    case.assertEqual(cycle_site.entries, repeated_site.entries)
    case.assertEqual(cycle_site.digest('cycle'), repeated_site.digest('cycle'))
    case.assertTrue(any(qualname == 'RUN' for _path, qualname, _digest in cycle_site.entries))
    mutated_cycle = cycle.replace("sys.platform == 'win32'", "sys.platform == 'linux'", 1)
    case.assertNotEqual(cycle, mutated_cycle)
    mutated_result, mutated_site = _single_site(
        case, {'app/tests/test_fixture.py': mutated_cycle})
    case.assertNotEqual(cycle_site.digest('cycle'), mutated_site.digest('cycle'))
    case.assertEqual([], _cycle_result.truncated)
    case.assertEqual([], repeated_result.truncated)
    case.assertEqual([], mutated_result.truncated)

    duplicate = ('import sys\nimport unittest\nRUN = True\nRUN = False\n'
                 'class DuplicateCase(unittest.TestCase):\n'
                 '    @unittest.skipUnless(RUN, ' + repr('w') + ')\n'
                 '    def test_case(self):\n        pass\n')
    duplicate_files = {'app/tests/test_fixture.py': duplicate}
    duplicate_result, _duplicate_site = _single_site(case, duplicate_files)
    case.assertNotEqual([], duplicate_result.truncated,
                        'two module bindings must not resolve last-wins')
    case.assertNotEqual([], _verify_registry(
        _census(duplicate_files, _Sources(duplicate_files.get), {}), {}))

    shapes = [("sys.platform == 'win32'", False),
              ("sys.platform != 'win32'", True),
              ("sys.platform in ('win32',)", False),
              ("sys.platform not in ('win32',)", True),
              ("sys.platform.startswith('win')", False),
              ("os.name == 'nt'", False),
              ("os.name != 'nt'", True),
              ("os.name.startswith('nt')", False),
              ("platform.system() == 'Windows'", False),
              ("platform.system().startswith('Win')", False),
              ("platform.system().startswith('Windows')", False),
              ("platform.system() != 'Windows'", True)]
    for text, on_posix in shapes:
        condition = ast.parse(text, mode='eval').body
        case.assertEqual(on_posix, _literal_refusal('skipIf', condition), text)
        case.assertEqual(not on_posix, _literal_refusal('skipUnless', condition), text)
    for text in ("sys.platform == 'linux'", "os.name == 'posix'",
                 "sys.platform.startswith('linux')", "platform.system() == 'Linux'",
                 "os.name.startswith('posix')", "platform.system().startswith('Linux')",
                 "platform.system().startswith('win')", "sys.platform.startswith('')"):
        condition = ast.parse(text, mode='eval').body
        case.assertFalse(_literal_refusal('skipIf', condition), text)
        case.assertFalse(_literal_refusal('skipUnless', condition), text)
    for text in ("1", "0", "'yes'", "''", "'win32'", "[]", "[1]",
                 "(1,)", "bool(True)"):
        condition = ast.parse(text, mode='eval').body
        case.assertEqual('U', _posix_truth(condition), text)
        case.assertFalse(_literal_refusal('skipIf', condition), text)
        case.assertFalse(_literal_refusal('skipUnless', condition), text)
    compound_shapes = (
        ("sys.platform == 'win32' and True", False, True),
        ("True and sys.platform == 'win32'", False, True),
        ("os.name == 'nt' and UNKNOWN_FLAG", False, True),
        ("sys.platform == 'win32' and UNKNOWN_FLAG", False, True),
        ("sys.platform != 'win32' and True", True, False),
        ("not (sys.platform == 'win32')", True, False),
        ("not (sys.platform == 'win32' and UNKNOWN_FLAG)", True, False),
        ("not (sys.platform == 'win32' or UNKNOWN_FLAG)", False, False),
        ("sys.platform == 'win32' or True", True, True),
        ("sys.platform == 'win32' or UNKNOWN_FLAG", False, True),
        ("os.name == 'nt' or sys.platform == 'darwin'", False, True),
        ("(sys.platform == 'win32') if UNKNOWN_FLAG else False", False, True),
        ("(sys.platform == 'win32') if UNKNOWN_FLAG else True", False, True),
        ("os.name != 'nt' and hasattr(os, 'fork')", False, False),
        ("not __debug__", False, False),
    )
    for text, skip_if, skip_unless in compound_shapes:
        condition = ast.parse(text, mode='eval').body
        case.assertEqual(skip_if, _literal_refusal('skipIf', condition), text)
        case.assertEqual(skip_unless, _literal_refusal('skipUnless', condition), text)
    membership_shapes = (
        ("sys.platform", ("win32",)),
        ("sys.platform", ("win32", "darwin")),
        ("sys.platform", ("darwin", "win32")),
        ("sys.platform", ("win32", "linux")),
        ("os.name", ("nt", "posix")),
        ("platform.system()", ("Windows", "Darwin")),
        ("platform.system()", ("Windows", "Linux")),
    )
    for probe, literals in membership_shapes:
        disjunction = ' or '.join(f'{probe} == {literal!r}' for literal in literals)
        for container in (repr(literals), repr(list(literals)),
                          '{' + ', '.join(repr(literal) for literal in literals) + '}'):
            for operation, equivalent in (('in', disjunction),
                                          ('not in', f'not ({disjunction})')):
                text = f'{probe} {operation} {container}'
                condition = ast.parse(text, mode='eval').body
                expanded = ast.parse(equivalent, mode='eval').body
                case.assertEqual(_posix_truth(expanded), _posix_truth(condition), text)
                for kind in ('skipIf', 'skipUnless'):
                    case.assertEqual(_refusal(kind, expanded),
                                     _refusal(kind, condition), (kind, text))
    for text in ("sys.platform in ('win32', *UNKNOWN)",
                 "sys.platform in ('win32', UNKNOWN)",
                 "sys.platform in PLATFORMS", "sys.platform in ('darwin',)",
                 "'win32' in sys.platform", "UNKNOWN in ('win32',)"):
        condition = ast.parse(text, mode='eval').body
        case.assertIs(condition, _lower_literal_membership(condition), text)
        case.assertEqual('U', _posix_truth(condition), text)
    case.assertTrue(_literal_refusal('skip', None))
    for text in ("sys.platform == 'win32'", "sys.platform != 'win32'"):
        condition = ast.parse(text, mode='eval').body
        case.assertFalse(_literal_refusal('skipIf', condition) and
                         _literal_refusal('skipUnless', condition))

    legacy_reasons = {
        "sys.platform == 'win32'": 'literal Windows-only refusal',
        "sys.platform == 'win32' and FLAG": 'literal Windows-only refusal',
        "sys.platform == 'win32' or FLAG": 'conservative legacy Windows-only refusal',
        "sys.platform == 'win32' or True": 'conservative legacy Windows-only refusal',
        "os.name == 'nt' or FLAG": 'conservative legacy Windows-only refusal',
        "sys.platform == 'win32' if FLAG else True":
            'conservative legacy Windows-only refusal',
    }
    for expression, reason in legacy_reasons.items():
        legacy_source = platform_imports + (
            'FLAG = bool(sys.maxsize)\n'
            'class LegacyCase(unittest.TestCase):\n'
            f'    @unittest.skipUnless({expression}, "w")\n'
            '    def test_case(self):\n        pass\n')
        legacy_result, legacy_site = _single_site(
            case, {'app/tests/test_fixture.py': legacy_source})
        case.assertEqual([legacy_site.key], legacy_result.refused, expression)
        case.assertEqual(reason, legacy_result.refusal_reasons[legacy_site.key], expression)
        legacy_problems = _verify_registry(legacy_result, {
            legacy_site.key: (legacy_site.digest('legacy baseline'), 'legacy baseline')})
        case.assertTrue(any(problem.startswith(reason + ':')
                            for problem in legacy_problems), expression)
    for expression in ("sys.platform == 'linux'", "sys.platform != 'win32'",
                       "os.name != 'nt' and FLAG",
                       "not (sys.platform == 'win32' or FLAG)", "FLAG"):
        baseline_source = platform_imports + (
            'FLAG = bool(sys.maxsize)\n'
            'class BaselineCase(unittest.TestCase):\n'
            f'    @unittest.skipUnless({expression}, "w")\n'
            '    def test_case(self):\n        pass\n')
        baseline_result, baseline_site = _single_site(
            case, {'app/tests/test_fixture.py': baseline_source})
        case.assertEqual([], baseline_result.refused, expression)
        case.assertEqual([], _verify_registry(baseline_result, {
            baseline_site.key: (baseline_site.digest('baseline'), 'baseline')}), expression)
    skip_if_source = platform_imports + (
        'FLAG = bool(sys.maxsize)\n'
        'class SkipIfBaselineCase(unittest.TestCase):\n'
        "    @unittest.skipIf(sys.platform == 'win32' or FLAG, 'posix')\n"
        '    def test_case(self):\n        pass\n')
    skip_if_result, skip_if_site = _single_site(
        case, {'app/tests/test_fixture.py': skip_if_source})
    case.assertEqual([], skip_if_result.refused)
    case.assertEqual([], _verify_registry(skip_if_result, {
        skip_if_site.key: (skip_if_site.digest('skipIf baseline'), 'skipIf baseline')}))

    prefix_imports = (
        'import os as operating_system\n'
        'import platform as host_platform\n'
        'from os import name as operating_name\n'
        'from platform import system as host_system\n'
        'import unittest\n')
    for predicate in ("operating_system.name.startswith('nt')",
                      "operating_name.startswith('nt')",
                      "host_platform.system().startswith('Win')",
                      "host_system().startswith('Windows')"):
        for kind in ('skipUnless', 'skipIf'):
            prefix_files = {'app/tests/test_fixture.py': prefix_imports + (
                'class PrefixCase(unittest.TestCase):\n'
                '    @unittest.' + kind + '(' + predicate + ", 'w')\n"
                '    def test_case(self):\n        pass\n')}
            prefix_result, prefix_site = _single_site(case, prefix_files)
            prefix_rows = {prefix_site.key: (prefix_site.digest('prefix'), 'prefix')}
            if kind == 'skipUnless':
                case.assertEqual([prefix_site.key], prefix_result.refused, predicate)
                case.assertTrue(any(
                    problem.startswith('literal Windows-only refusal')
                    for problem in _verify_registry(prefix_result, prefix_rows)), predicate)
            else:
                case.assertEqual([], prefix_result.refused, predicate)
                case.assertEqual([], _verify_registry(prefix_result, prefix_rows), predicate)

    single = {'app/tests/test_fixture.py': (
        'import os\nimport unittest\nclass RegistryCase(unittest.TestCase):\n'
        "    @unittest.skipIf(os.name == 'nt', 'posix')\n"
        '    def test_case(self):\n        pass\n')}
    _single_result, single_site = _single_site(case, single)
    declaration = "skipIf; linux-outcome=run; condition=os.name == 'nt'"
    digest = single_site.digest(declaration)
    rows = {single_site.key: (digest, declaration)}
    declarations = {single_site.key: declaration}
    _ok = _verify_registry(_census(single, _Sources(single.get), declarations), rows)
    case.assertEqual([], _ok)
    added = dict(rows)
    added['test_other.py:Other:0'] = (digest, declaration)
    case.assertNotEqual([], _verify_registry(_census(single, _Sources(single.get), declarations), added))
    changed = dict(rows)
    changed[single_site.key] = ('0' * 64, declaration)
    case.assertNotEqual([], _verify_registry(_census(single, _Sources(single.get), declarations), changed))
    reworded = dict(rows)
    reworded[single_site.key] = (digest, declaration + ' changed')
    case.assertNotEqual([], _verify_registry(_census(single, _Sources(single.get), declarations), reworded))
    case.assertNotEqual([], _verify_registry(_census(single, _Sources(single.get), declarations), {}))

    negatives = {'app/tests/test_fixture.py': (
        'import unittest\nfrom unittest import mock\nclass NegativeCase(unittest.TestCase):\n'
        '    @property\n    def value(self):\n        return 1\n'
        '    @staticmethod\n    def helper():\n        return 2\n'
        "    @mock.patch('tests.fixture_helper')\n"
        '    def test_patched(self):\n        pass\n'
        '    @decorator\n    def test_custom(self):\n        pass\n')}
    case.assertEqual(set(), set(_fixture_census(negatives).sites))
    unresolved = _fixture_census({'app/tests/test_fixture.py': (
        'class UnresolvedCase:\n    @skipUnless(True, ' + repr('w') + ')\n'
        '    def test_case(self):\n        pass\n')})
    case.assertEqual(set(), set(unresolved.sites))
    case.assertEqual(1, len(unresolved.unresolved))
    posix = _fixture_census({'app/tests/test_fixture.py': platform_imports + (
        'class PosixCase(unittest.TestCase):\n'
        "    @unittest.skipIf(sys.platform == 'win32', 'posix')\n"
        '    def test_supported(self):\n        pass\n'
        "    @unittest.skipUnless(sys.platform == 'linux', 'linux')\n"
        '    def test_advanced(self):\n        pass\n')})
    case.assertEqual(2, len(posix.sites))
    case.assertEqual([], posix.refused)

    class_scope = platform_imports + (
        'class ClassScopeCase(unittest.TestCase):\n'
        "    RUN = sys.platform == 'linux'\n"
        '    ALIAS = RUN\n'
        '    OTHER = ALIAS\n'
        "    @unittest.skipUnless(OTHER, 'w')\n"
        '    def test_case(self):\n        pass\n')
    _class_result, class_site = _single_site(case, {'app/tests/test_fixture.py': class_scope})
    class_flipped = class_scope.replace("== 'linux'", "== 'win32'")
    _class_flip_result, class_flip_site = _single_site(
        case, {'app/tests/test_fixture.py': class_flipped})
    case.assertNotEqual(class_site.digest('class scope'), class_flip_site.digest('class scope'),
                        'a transitive class alias must pin the assignment it depends on')

    def two_class_source(linux_literal, windows_literal):
        return platform_imports + (
            'class LinuxCase(unittest.TestCase):\n'
            f'    RUN = sys.platform == {linux_literal!r}\n'
            "    @unittest.skipUnless(RUN, 'w')\n"
            '    def test_case(self):\n        pass\n'
            'class WindowsCase(unittest.TestCase):\n'
            f'    RUN = sys.platform == {windows_literal!r}\n'
            "    @unittest.skipUnless(RUN, 'w')\n"
            '    def test_case(self):\n        pass\n')

    two_before = _fixture_census({'app/tests/test_fixture.py': two_class_source('linux', 'win32')})
    two_after = _fixture_census({'app/tests/test_fixture.py': two_class_source('linux', 'darwin')})
    linux_key = 'test_fixture.py:LinuxCase.test_case:0'
    windows_key = 'test_fixture.py:WindowsCase.test_case:0'
    case.assertEqual(2, len(two_before.sites))
    case.assertEqual(two_before.sites[linux_key].digest('two'),
                     two_after.sites[linux_key].digest('two'),
                     'two classes must not share a same-named constant')
    case.assertNotEqual(two_before.sites[windows_key].digest('two'),
                        two_after.sites[windows_key].digest('two'))

    homonym = platform_imports + (
        "RUN = sys.platform == 'win32'\n"
        'class HomonymCase(unittest.TestCase):\n'
        "    RUN = sys.platform == 'linux'\n"
        "    @unittest.skipUnless(RUN, 'w')\n"
        '    def test_case(self):\n        pass\n')
    _homonym_result, homonym_site = _single_site(case, {'app/tests/test_fixture.py': homonym})
    homonym_class_flip = homonym.replace("    RUN = sys.platform == 'linux'",
                                         "    RUN = sys.platform == 'darwin'")
    _homonym_class_result, homonym_class_site = _single_site(
        case, {'app/tests/test_fixture.py': homonym_class_flip})
    homonym_module_flip = homonym.replace("RUN = sys.platform == 'win32'",
                                          "RUN = sys.platform == 'darwin'", 1)
    _homonym_module_result, homonym_module_site = _single_site(
        case, {'app/tests/test_fixture.py': homonym_module_flip})
    case.assertNotEqual(homonym_site.digest('homonym'), homonym_class_site.digest('homonym'))
    case.assertEqual(homonym_site.digest('homonym'), homonym_module_site.digest('homonym'),
                     'a class binding must shadow the module homonym it is named after')

    decorated_class = platform_imports + (
        "MODULE_RUN = sys.platform == 'win32'\n"
        "@unittest.skipUnless(MODULE_RUN, 'w')\n"
        'class DecoratedCase(unittest.TestCase):\n'
        "    MODULE_RUN = sys.platform == 'linux'\n"
        '    def test_case(self):\n        pass\n')
    _decorated_result, decorated_site = _single_site(
        case, {'app/tests/test_fixture.py': decorated_class})
    decorated_body_flip = decorated_class.replace("    MODULE_RUN = sys.platform == 'linux'",
                                                  "    MODULE_RUN = sys.platform == 'darwin'")
    _decorated_body_result, decorated_body_site = _single_site(
        case, {'app/tests/test_fixture.py': decorated_body_flip})
    decorated_module_flip = decorated_class.replace("MODULE_RUN = sys.platform == 'win32'",
                                                    "MODULE_RUN = sys.platform == 'darwin'", 1)
    _decorated_module_result, decorated_module_site = _single_site(
        case, {'app/tests/test_fixture.py': decorated_module_flip})
    case.assertEqual(decorated_site.digest('decorated'),
                     decorated_body_site.digest('decorated'),
                     'a class decorator resolves in its enclosing scope, not its own body')
    case.assertNotEqual(decorated_site.digest('decorated'),
                        decorated_module_site.digest('decorated'))

    nested_class = platform_imports + (
        "RUN = sys.platform == 'win32'\n"
        'class Outer:\n'
        "    RUN = sys.platform == 'linux'\n"
        '    class Inner(unittest.TestCase):\n'
        "        @unittest.skipUnless(RUN, 'w')\n"
        '        def test_case(self):\n            pass\n')
    _nested_result, nested_site = _single_site(
        case, {'app/tests/test_fixture.py': nested_class})
    nested_outer_flip = nested_class.replace("    RUN = sys.platform == 'linux'",
                                            "    RUN = sys.platform == 'darwin'")
    _nested_outer_result, nested_outer_site = _single_site(
        case, {'app/tests/test_fixture.py': nested_outer_flip})
    nested_module_flip = nested_class.replace("RUN = sys.platform == 'win32'",
                                              "RUN = sys.platform == 'darwin'", 1)
    _nested_module_result, nested_module_site = _single_site(
        case, {'app/tests/test_fixture.py': nested_module_flip})
    case.assertEqual(nested_site.digest('nested'), nested_outer_site.digest('nested'),
                     'an enclosing class is not a lexical scope of a nested class body')
    case.assertNotEqual(nested_site.digest('nested'), nested_module_site.digest('nested'))

    unknown_seed = _fixture_census({'app/tests/test_fixture.py': platform_imports + (
        'class UnknownSeedCase(unittest.TestCase):\n'
        "    @unittest.skipUnless(UNDEFINED_FLAG, 'w')\n"
        '    def test_case(self):\n        pass\n')})
    case.assertEqual(['test_fixture.py:UnknownSeedCase.test_case:0'],
                     list(unknown_seed.sites),
                     'an unknown predicate seed must still require a row')
    case.assertEqual(1, len(unknown_seed.unresolved))
    case.assertIn('UNDEFINED_FLAG', unknown_seed.unresolved[0])
    compound_seed = _fixture_census({'app/tests/test_fixture.py': platform_imports + (
        'class CompoundSeedCase(unittest.TestCase):\n'
        "    @unittest.skipUnless(MISSING_FLAG or sys.platform == 'linux', 'w')\n"
        '    def test_case(self):\n        pass\n')})
    case.assertEqual(1, len(compound_seed.unresolved),
                     'a condition that contains an unknown name must fail loud')
    case.assertIn('MISSING_FLAG', compound_seed.unresolved[0])
    comprehension_seed = _fixture_census({'app/tests/test_fixture.py': platform_imports + (
        'FLAGS = (True, False)\n'
        'class ComprehensionSeedCase(unittest.TestCase):\n'
        "    @unittest.skipUnless(any(flag for flag in FLAGS), 'module')\n"
        '    def test_case(self):\n        pass\n')})
    case.assertEqual([], comprehension_seed.unresolved,
                     'a comprehension target is local to the condition, not an unknown seed')
    builtin_seed = _fixture_census({'app/tests/test_fixture.py': platform_imports + (
        'class BuiltinSeedCase(unittest.TestCase):\n'
        "    @unittest.skipUnless(object, 'module')\n"
        '    def test_case(self):\n        pass\n')})
    case.assertEqual([], builtin_seed.unresolved,
                     'a builtin predicate seed is not an unknown seed')
    parameter_source = (
        'import unittest\n'
        'def windows_only(flag):\n'
        "    return unittest.skipUnless(flag, 'w')\n"
        'class ParameterCase(unittest.TestCase):\n'
        '    @windows_only(True)\n'
        '    def test_case(self):\n        pass\n')
    parameter_result = _fixture_census({'app/tests/test_fixture.py': parameter_source})
    case.assertEqual(1, len(parameter_result.sites))
    case.assertEqual([], parameter_result.unresolved,
                     'a helper parameter is not a module-level unknown seed')

    multikind = {'app/tests/test_fixture.py': (
        'import sys\nimport unittest\n'
        'def guarded():\n'
        "    if sys.platform == 'linux':\n"
        "        return unittest.skipIf(sys.platform == 'win32', 'posix')\n"
        "    return unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'class FactoryCase(unittest.TestCase):\n'
        '    @guarded()\n'
        '    def test_case(self):\n        pass\n')}
    multikind_result = _fixture_census(multikind)
    factory_key = 'test_fixture.py:FactoryCase.test_case:0'
    case.assertIn(factory_key, multikind_result.sites,
                  'an ambiguous skip kind must still require a row')
    case.assertEqual([factory_key], multikind_result.ambiguous)
    case.assertEqual([], multikind_result.refused, 'ambiguity is not a literal refusal')
    factory_problems = _verify_registry(multikind_result, {})
    case.assertIn('ambiguous skip kind: ' + factory_key, factory_problems)
    case.assertIn('sites without a registry row: ' + factory_key, factory_problems)

    later_class = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'class LaterBindingCase(unittest.TestCase):\n'
        "    @unittest.skipUnless(FLAG, 'w')\n"
        '    def test_case(self):\n        pass\n'
        '    FLAG = False\n')
    _later_result, later_site = _single_site(
        case, {'app/tests/test_fixture.py': later_class})
    later_module_flip = later_class.replace(
        "FLAG = sys.platform == 'win32'", "FLAG = sys.platform == 'linux'", 1)
    _later_flip_result, later_flip_site = _single_site(
        case, {'app/tests/test_fixture.py': later_module_flip})
    case.assertNotEqual(
        later_site.digest('later binding'), later_flip_site.digest('later binding'),
        'a class binding after the decorator must not hide the module dependency')
    later_body_flip = later_class.replace('    FLAG = False', '    FLAG = True')
    _later_body_result, later_body_site = _single_site(
        case, {'app/tests/test_fixture.py': later_body_flip})
    case.assertEqual(
        later_site.digest('later binding'), later_body_site.digest('later binding'),
        'a class binding after the decorator is not read by it')

    branch_class = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'class BranchBindingCase(unittest.TestCase):\n'
        '    if False:\n        FLAG = False\n'
        "    @unittest.skipUnless(FLAG, 'w')\n"
        '    def test_case(self):\n        pass\n')
    branch_result = _fixture_census({'app/tests/test_fixture.py': branch_class})
    branch_key = 'test_fixture.py:BranchBindingCase.test_case:0'
    case.assertEqual(
        [branch_key], branch_result.truncated,
        'an unexecuted conditional class binding must fail closed, not shadow the module')
    branch_site = branch_result.sites[branch_key]
    branch_rows = {branch_key: (branch_site.digest('branch binding'), 'branch binding')}
    case.assertIn(
        'truncated census', _verify_registry(branch_result, branch_rows)[0],
        'a matching row cannot authorize an unproven class binding')

    for signature in ('flag=FLAG', '*, flag=FLAG', 'flag=(lambda: FLAG)()'):
        default_helper = platform_imports + (
            "FLAG = sys.platform == 'win32'\n"
            'def W(' + signature + '):\n'
            "    return unittest.skipUnless(flag, 'w')\n")
        default_case = (
            'class DefaultBindingCase(unittest.TestCase):\n'
            '    @W()\n    def test_case(self):\n        pass\n')
        for imported in (False, True):
            if imported:
                default_files = {
                    'app/support.py': default_helper,
                    'app/tests/test_fixture.py': (
                        'import unittest\nfrom support import W\n' + default_case)}
            else:
                default_files = {'app/tests/test_fixture.py': default_helper + default_case}
            _default_result, default_site = _single_site(case, default_files)
            changed_files = {
                relpath: text.replace("FLAG = sys.platform == 'win32'",
                                      "FLAG = sys.platform == 'linux'", 1)
                for relpath, text in default_files.items()}
            _changed_result, changed_site = _single_site(case, changed_files)
            case.assertNotEqual(
                default_site.digest('helper default'), changed_site.digest('helper default'),
                'module and imported helper defaults must pin their module dependencies')

    lambda_default = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'class LambdaDefaultCase(unittest.TestCase):\n'
        '    FLAG = False\n'
        '    def W(callback=lambda: FLAG):\n'
        "        return unittest.skipUnless(callback(), 'w')\n"
        '    @W()\n    def test_case(self):\n        pass\n')
    _lambda_result, lambda_site = _single_site(
        case, {'app/tests/test_fixture.py': lambda_default})
    lambda_module_flip = lambda_default.replace(
        "FLAG = sys.platform == 'win32'", "FLAG = sys.platform == 'linux'", 1)
    _lambda_flip_result, lambda_flip_site = _single_site(
        case, {'app/tests/test_fixture.py': lambda_module_flip})
    case.assertNotEqual(
        lambda_site.digest('lambda default'), lambda_flip_site.digest('lambda default'),
        'a lambda body in a class default resolves its free names in module scope')
    lambda_class_flip = lambda_default.replace('    FLAG = False', '    FLAG = True')
    _lambda_class_result, lambda_class_site = _single_site(
        case, {'app/tests/test_fixture.py': lambda_class_flip})
    case.assertEqual(
        lambda_site.digest('lambda default'), lambda_class_site.digest('lambda default'),
        'a module-scope lambda body does not read the class homonym')

    comprehension_default = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'FLAGS = (True,)\n'
        'class ComprehensionDefaultCase(unittest.TestCase):\n'
        '    FLAG = False\n'
        '    def W(flags=(FLAG for _ in FLAGS)):\n'
        "        return unittest.skipUnless(all(flags), 'w')\n"
        '    @W()\n    def test_case(self):\n        pass\n')
    _comprehension_result, comprehension_site = _single_site(
        case, {'app/tests/test_fixture.py': comprehension_default})
    comprehension_module_flip = comprehension_default.replace(
        "FLAG = sys.platform == 'win32'", "FLAG = sys.platform == 'linux'", 1)
    _comprehension_flip_result, comprehension_flip_site = _single_site(
        case, {'app/tests/test_fixture.py': comprehension_module_flip})
    case.assertNotEqual(
        comprehension_site.digest('comprehension default'),
        comprehension_flip_site.digest('comprehension default'),
        'a comprehension element in a class default resolves free names in module scope')
    comprehension_class_flip = comprehension_default.replace(
        '    FLAG = False', '    FLAG = True')
    _comprehension_class_result, comprehension_class_site = _single_site(
        case, {'app/tests/test_fixture.py': comprehension_class_flip})
    case.assertEqual(
        comprehension_site.digest('comprehension default'),
        comprehension_class_site.digest('comprehension default'),
        'a comprehension default does not read the class homonym')

    parameter_homonym = platform_imports + (
        'FLAG = True\n'
        'def W(FLAG):\n'
        "    return unittest.skipUnless(FLAG, 'w')\n"
        'class ParameterHomonymCase(unittest.TestCase):\n'
        '    @W(False)\n    def test_case(self):\n        pass\n')
    _parameter_result, parameter_site = _single_site(
        case, {'app/tests/test_fixture.py': parameter_homonym})
    parameter_module_flip = parameter_homonym.replace('FLAG = True', 'FLAG = False', 1)
    _parameter_flip_result, parameter_flip_site = _single_site(
        case, {'app/tests/test_fixture.py': parameter_module_flip})
    case.assertEqual(
        parameter_site.digest('parameter homonym'),
        parameter_flip_site.digest('parameter homonym'),
        'a helper parameter must not pin an unrelated module homonym')

    local_homonym = platform_imports + (
        'FLAG = True\n'
        'def W():\n'
        '    FLAG = False\n'
        "    return unittest.skipUnless(FLAG, 'w')\n"
        'class LocalHomonymCase(unittest.TestCase):\n'
        '    @W()\n    def test_case(self):\n        pass\n')
    _local_result, local_site = _single_site(
        case, {'app/tests/test_fixture.py': local_homonym})
    local_module_flip = local_homonym.replace('FLAG = True', 'FLAG = False', 1)
    _local_flip_result, local_flip_site = _single_site(
        case, {'app/tests/test_fixture.py': local_module_flip})
    case.assertEqual(
        local_site.digest('local homonym'), local_flip_site.digest('local homonym'),
        'a helper local must not pin an unrelated module homonym')

    module_read = platform_imports + (
        'FLAG = True\n'
        'def W():\n'
        "    return unittest.skipUnless(FLAG, 'w')\n"
        'class ModuleReadCase(unittest.TestCase):\n'
        '    @W()\n    def test_case(self):\n        pass\n')
    _module_read_result, module_read_site = _single_site(
        case, {'app/tests/test_fixture.py': module_read})
    module_read_flip = module_read.replace('FLAG = True', 'FLAG = False', 1)
    _module_read_flip_result, module_read_flip_site = _single_site(
        case, {'app/tests/test_fixture.py': module_read_flip})
    case.assertNotEqual(
        module_read_site.digest('module read'), module_read_flip_site.digest('module read'),
        'a helper body must still pin the module global it actually reads')

    def probe_refusal(imports, expression, kind):
        source = (platform_imports + imports
                  + 'class ProbeCase(unittest.TestCase):\n'
                  + f'    @unittest.{kind}({expression}, "w")\n'
                  + '    def test_case(self):\n        pass\n')
        result = _fixture_census({'app/tests/test_fixture.py': source})
        return result, 'test_fixture.py:ProbeCase.test_case:0'

    windows_probes = (
        ('import sys as s\n', "s.platform == 'win32'"),
        ('from sys import platform as target\n', "target == 'win32'"),
        ('import os as o\n', "o.name == 'nt'"),
        ('from os import name as nt_name\n', "nt_name == 'nt'"),
        ('import platform as p\n', "p.system() == 'Windows'"),
        ('from platform import system as system_probe\n', "system_probe() == 'Windows'"),
        ('', "'win32' == sys.platform"),
        ('', "'nt' == os.name"),
        ('', "'Windows' == platform.system()"),
    )
    for imports, expression in windows_probes:
        for kind, expected in (('skipUnless', True), ('skipIf', False)):
            probe_result, probe_key = probe_refusal(imports, expression, kind)
            case.assertEqual(
                [probe_key] if expected else [], probe_result.refused,
                f'{kind} {expression}: alias/reversed refusal polarity')

    posix_probes = (
        ('import sys as s\n', "s.platform != 'win32'"),
        ('', "'win32' != sys.platform"),
        ('import os as o\n', "o.name != 'nt'"),
    )
    for imports, expression in posix_probes:
        for kind, expected in (('skipIf', True), ('skipUnless', False)):
            probe_result, probe_key = probe_refusal(imports, expression, kind)
            case.assertEqual(
                [probe_key] if expected else [], probe_result.refused,
                f'{kind} {expression}: negated alias/reversed refusal polarity')

    same_kind = {'app/tests/test_fixture.py': platform_imports + (
        "if sys.platform == 'linux':\n"
        "    D = unittest.skipUnless(sys.platform == 'linux', 'linux')\n"
        'else:\n'
        "    D = unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'class SameKindCase(unittest.TestCase):\n'
        '    @D\n    def test_case(self):\n        pass\n')}
    same_kind_result = _fixture_census(same_kind)
    same_kind_key = 'test_fixture.py:SameKindCase.test_case:0'
    case.assertEqual(
        [same_kind_key], same_kind_result.ambiguous,
        'alternate same-kind conditions must be ambiguous, not first-wins')
    case.assertEqual([], same_kind_result.refused)
    same_kind_site = same_kind_result.sites[same_kind_key]
    case.assertIn(
        'ambiguous skip kind',
        _verify_registry(same_kind_result, {
            same_kind_key: (same_kind_site.digest('same kind'), 'same kind')})[0],
        'a matching row cannot authorize alternate same-kind conditions')

    identical_kind = {'app/tests/test_fixture.py': platform_imports + (
        "if sys.platform == 'linux':\n"
        "    D = unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'else:\n'
        "    D = unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'class IdenticalKindCase(unittest.TestCase):\n'
        '    @D\n    def test_case(self):\n        pass\n')}
    identical_result = _fixture_census(identical_kind)
    identical_key = 'test_fixture.py:IdenticalKindCase.test_case:0'
    case.assertEqual(
        [], identical_result.ambiguous,
        'identical same-kind conditions must not be flagged ambiguous')
    case.assertEqual([identical_key], identical_result.refused)

    blanket = _fixture_census({'app/tests/test_fixture.py': (
        'import unittest\nclass BlanketCase(unittest.TestCase):\n'
        "    @unittest.skip('flaky')\n"
        '    def test_case(self):\n        pass\n')})
    blanket_key = 'test_fixture.py:BlanketCase.test_case:0'
    case.assertEqual([blanket_key], blanket.refused)
    case.assertEqual('unconditional skip', blanket.refusal_reasons[blanket_key])
    blanket_problems = _verify_registry(blanket, {})
    case.assertIn('unconditional skip: ' + blanket_key, blanket_problems)
    case.assertFalse(any(problem.startswith('literal Windows-only refusal')
                         for problem in blanket_problems))

    for kind, length in (('module', 3), ('symbol', 4), ('definition', 4),
                         ('assignment', 4), ('dynamic', 4), ('external', 3),
                         ('unknown', 3), ('ambiguous', 3)):
        case.assertEqual(length, len(_shape((kind,) + (None,) * (length - 1))))
    with case.assertRaisesRegex(AssertionError, 'unknown resolution kind'):
        _shape(('class', 'name', None))
    with case.assertRaisesRegex(AssertionError, 'fields'):
        _shape(('symbol', 'module', 'name'))

    repin_files = {'app/tests/test_fixture.py': platform_imports + (
        'class RepinCase(unittest.TestCase):\n'
        "    @unittest.skipIf(sys.platform == 'win32', 'posix')\n"
        '    def test_case(self):\n        pass\n'
        "    @unittest.skipUnless(sys.platform == 'linux', 'linux')\n"
        '    def test_case_other(self):\n        pass\n')}
    repin_result = _fixture_census(repin_files)
    repin_rows = {key: ('0' * 64, 'reviewed declaration for ' + key)
                  for key in repin_result.sites}
    repin_text = ('\n'.join(f'{key} = {digest}  # {declaration}'
                            for key, (digest, declaration) in repin_rows.items()) + '\n')
    repin_updates = _repin_updates(repin_result, repin_rows)
    case.assertEqual(2, len(repin_updates))
    expected_text = repin_text
    for key, digest in repin_updates.items():
        expected_text = expected_text.replace(f'{key} = ' + '0' * 64, f'{key} = ' + digest)
    case.assertEqual(expected_text, _rewrite_registry(repin_text, repin_updates),
                     'repin may rewrite digests only')
    case.assertEqual(repin_text.count('\n'), _rewrite_registry(repin_text, repin_updates).count('\n'))
    case.assertEqual({key: value[1] for key, value in _parse_registry(
        _rewrite_registry(repin_text, repin_updates)).items()},
        {key: value[1] for key, value in repin_rows.items()})
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'registry.txt'
        target.write_text(repin_text, encoding='utf-8')
        module = sys.modules[__name__]
        with mock.patch.object(module, 'SKIP_REGISTRY', target), \
                mock.patch.object(module, '_repin',
                                  return_value=(repin_rows, repin_result, [])):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                case.assertEqual(0, main(['--repin']))
            case.assertEqual(repin_text, target.read_text(encoding='utf-8'),
                             'a dry run must not write')
            case.assertIn(f'{len(repin_updates)} digest change(s)', output.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                case.assertEqual(0, main(['--repin', '--apply']))
            case.assertEqual(expected_text, target.read_text(encoding='utf-8'),
                             'apply writes exactly the recomputed digests')
            case.assertEqual([], sorted(target.parent.glob('.skip-baseline-*')),
                             'the atomic replace must not leave a temporary file')
            target.write_text(repin_text, encoding='utf-8')
            intervening_text = repin_text + '\n'
            target.write_text(intervening_text, encoding='utf-8')
            with case.assertRaisesRegex(AssertionError, 'registry changed during repin'):
                _write_registry(repin_text, repin_updates)
            case.assertEqual(intervening_text, target.read_text(encoding='utf-8'),
                             'a stale repin must preserve the intervening writer')
            case.assertEqual([], sorted(target.parent.glob('.skip-baseline-*')))
            target.write_text(repin_text, encoding='utf-8')
            with mock.patch.object(module, '_repin',
                                   return_value=(repin_rows, repin_result,
                                                 ['sites without a registry row: missing:0'])):
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()) as errors:
                    case.assertEqual(1, main(['--repin', '--apply']))
                case.assertIn('repin refused: sites without a registry row', errors.getvalue())
                case.assertEqual(repin_text, target.read_text(encoding='utf-8'),
                                 'a refused repin must not write')
    for mutate, label in ((lambda census: census.unresolved.append('a'),
                           'unresolved skip decorators'),
                          (lambda census: census.truncated.append('b'), 'truncated census'),
                          (lambda census: census.refused.append('c'), 'refused sites'),
                          (lambda census: census.ambiguous.append('d'),
                           'ambiguous skip kinds')):
        census = _Census()
        mutate(census)
        case.assertIn(label, _repin_problems(census, {})[0])
    case.assertIn('sites without a registry row', _repin_problems(repin_result, {})[0])
    case.assertIn('registry rows without a site',
                  _repin_problems(_Census(), {'unseen:0': ('0' * 64, 'declaration')})[0])
    changed_declaration = _fixture_census(repin_files)
    changed_declaration.sites[next(iter(repin_result.sites))].declaration = 'other declaration'
    case.assertIn('declaration differs from census',
                  _repin_problems(changed_declaration, repin_rows)[0])

    helper_literal = platform_imports + (
        'class HelperLiteralCase(unittest.TestCase):\n'
        '    def W():\n'
        "        return unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        '    @W()\n'
        '    def test_case(self):\n        pass\n')
    helper_literal_key = 'test_fixture.py:HelperLiteralCase.test_case:0'
    helper_literal_result = _fixture_census(
        {'app/tests/test_fixture.py': helper_literal})
    case.assertEqual([helper_literal_key], helper_literal_result.refused,
                     'a helper constructor condition must reach the refusal table')
    helper_literal_site = helper_literal_result.sites[helper_literal_key]
    case.assertIn(
        'literal Windows-only refusal',
        _verify_registry(helper_literal_result, {
            helper_literal_key: (helper_literal_site.digest('helper literal'),
                                 'helper literal')})[0],
        'a matching row cannot authorize a literal Windows-only helper condition')
    helper_literal_posix = helper_literal.replace(
        "sys.platform == 'win32'", "sys.platform == 'linux'")
    case.assertEqual(
        [], _fixture_census({'app/tests/test_fixture.py': helper_literal_posix}).refused,
        'a POSIX helper condition is not a Windows-only refusal')
    helper_reason = platform_imports + (
        'class HelperReasonCase(unittest.TestCase):\n'
        '    def W(reason):\n'
        "        return unittest.skipUnless(sys.platform == 'win32', reason)\n"
        "    @W('explain')\n"
        '    def test_case(self):\n        pass\n')
    helper_reason_key = 'test_fixture.py:HelperReasonCase.test_case:0'
    case.assertEqual(
        [helper_reason_key],
        _fixture_census({'app/tests/test_fixture.py': helper_reason}).refused,
        'a helper reason argument must not replace its closed condition')
    class_helper_literal = platform_imports + (
        'class ClassHelperLiteralCase(unittest.TestCase):\n'
        '    def W():\n'
        "        return unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        '    @W()\n'
        '    def test_case(self):\n        pass\n')
    class_helper_key = 'test_fixture.py:ClassHelperLiteralCase.test_case:0'
    case.assertEqual(
        [class_helper_key],
        _fixture_census({'app/tests/test_fixture.py': class_helper_literal}).refused,
        'a class-defined helper condition resolves its free names at module scope')
    helper_alternate = platform_imports + (
        'class HelperAlternateCase(unittest.TestCase):\n'
        '    def W():\n'
        "        if sys.platform == 'linux':\n"
        "            return unittest.skipUnless(sys.platform == 'linux', 'linux')\n"
        "        return unittest.skipUnless(sys.platform == 'win32', 'win32')\n"
        '    @W()\n'
        '    def test_case(self):\n        pass\n')
    helper_alternate_key = 'test_fixture.py:HelperAlternateCase.test_case:0'
    helper_alternate_result = _fixture_census(
        {'app/tests/test_fixture.py': helper_alternate})
    case.assertEqual(
        [helper_alternate_key], helper_alternate_result.ambiguous,
        'two same-kind helper conditions must be ambiguous, not first-wins')
    case.assertEqual([], helper_alternate_result.refused)
    helper_alternate_site = helper_alternate_result.sites[helper_alternate_key]
    case.assertIn(
        'ambiguous skip kind',
        _verify_registry(helper_alternate_result, {
            helper_alternate_key: (helper_alternate_site.digest('helper alternate'),
                                   'helper alternate')})[0],
        'a matching row cannot authorize alternate same-kind helper conditions')
    helper_identical = platform_imports + (
        'class HelperIdenticalCase(unittest.TestCase):\n'
        '    def W():\n'
        "        if sys.platform == 'linux':\n"
        "            return unittest.skipUnless(sys.platform == 'win32', 'win32')\n"
        "        return unittest.skipUnless(sys.platform == 'win32', 'win32')\n"
        '    @W()\n'
        '    def test_case(self):\n        pass\n')
    helper_identical_key = 'test_fixture.py:HelperIdenticalCase.test_case:0'
    helper_identical_result = _fixture_census(
        {'app/tests/test_fixture.py': helper_identical})
    case.assertEqual([], helper_identical_result.ambiguous)
    case.assertEqual([helper_identical_key], helper_identical_result.refused,
                     'identical same-kind helper conditions remain reviewable')

    helper_alias = platform_imports + (
        "D = unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'def W():\n'
        '    return D\n'
        'class HelperAliasCase(unittest.TestCase):\n'
        '    @W()\n'
        '    def test_case(self):\n        pass\n')
    helper_alias_key = 'test_fixture.py:HelperAliasCase.test_case:0'
    helper_alias_result = _fixture_census({'app/tests/test_fixture.py': helper_alias})
    case.assertEqual([helper_alias_key], list(helper_alias_result.sites),
                     'a helper returning an in-repo skip alias must be censused')
    case.assertEqual([helper_alias_key], helper_alias_result.refused)
    helper_alias_site = helper_alias_result.sites[helper_alias_key]
    case.assertIn(
        'literal Windows-only refusal',
        _verify_registry(helper_alias_result, {
            helper_alias_key: (helper_alias_site.digest('helper alias'),
                               'helper alias')})[0],
        'a matching row cannot authorize a helper-returned Windows-only alias')
    helper_arbitrary = platform_imports + (
        'def W():\n'
        '    return object()\n'
        'class ArbitraryReturnCase(unittest.TestCase):\n'
        '    @W()\n'
        '    def test_case(self):\n        pass\n')
    helper_arbitrary_result = _fixture_census(
        {'app/tests/test_fixture.py': helper_arbitrary})
    case.assertEqual(set(), set(helper_arbitrary_result.sites),
                     'an arbitrary helper return is not a skip')
    case.assertEqual([], helper_arbitrary_result.unresolved)
    helper_mixed = platform_imports + (
        "D = unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'def W():\n'
        "    if sys.platform == 'linux':\n"
        '        return D\n'
        '    return None\n'
        'class MixedReturnCase(unittest.TestCase):\n'
        '    @W()\n'
        '    def test_case(self):\n        pass\n')
    helper_mixed_key = 'test_fixture.py:MixedReturnCase.test_case:0'
    case.assertEqual(
        [helper_mixed_key],
        _fixture_census({'app/tests/test_fixture.py': helper_mixed}).ambiguous,
        'a helper that can return a non-skip value must fail closed')
    helper_bare_return = platform_imports + (
        "D = unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'def W():\n'
        "    if sys.platform == 'linux':\n"
        '        return D\n'
        '    return\n'
        'class BareReturnCase(unittest.TestCase):\n'
        '    @W()\n'
        '    def test_case(self):\n        pass\n')
    helper_bare_key = 'test_fixture.py:BareReturnCase.test_case:0'
    case.assertEqual(
        [helper_bare_key],
        _fixture_census({'app/tests/test_fixture.py': helper_bare_return}).ambiguous,
        'a bare non-skip return must fail closed')
    helper_local_alias = platform_imports + (
        'class HelperLocalAliasCase(unittest.TestCase):\n'
        '    def W():\n'
        "        D = unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        '        return D\n'
        '    @W()\n'
        '    def test_case(self):\n        pass\n')
    helper_local_key = 'test_fixture.py:HelperLocalAliasCase.test_case:0'
    helper_local_result = _fixture_census(
        {'app/tests/test_fixture.py': helper_local_alias})
    case.assertEqual([helper_local_key], list(helper_local_result.sites))
    case.assertEqual([helper_local_key], helper_local_result.refused,
                     'a local skip alias return must keep its constructor condition')
    helper_parameter_default = platform_imports + (
        "D = unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'def W(alias=D):\n'
        '    return alias\n'
        'class ParameterDefaultCase(unittest.TestCase):\n'
        '    @W()\n'
        '    def test_case(self):\n        pass\n')
    helper_parameter_key = 'test_fixture.py:ParameterDefaultCase.test_case:0'
    helper_parameter_result = _fixture_census(
        {'app/tests/test_fixture.py': helper_parameter_default})
    case.assertEqual([helper_parameter_key], list(helper_parameter_result.sites))
    case.assertEqual([helper_parameter_key], helper_parameter_result.refused,
                     'a parameter default skip alias must be resolved')
    helper_parameter_homonym = platform_imports + (
        "alias = unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'def W(alias):\n'
        '    return alias\n'
        'class ParameterHomonymAliasCase(unittest.TestCase):\n'
        '    @W(True)\n'
        '    def test_case(self):\n        pass\n')
    helper_parameter_homonym_result = _fixture_census(
        {'app/tests/test_fixture.py': helper_parameter_homonym})
    case.assertEqual(set(), set(helper_parameter_homonym_result.sites),
                     'a returned parameter must not resolve to a module homonym')

    mixed_binding = platform_imports + (
        'def W():\n'
        '    return None\n'
        'def W():\n'
        "    return unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'class MixedBindingCase(unittest.TestCase):\n'
        '    @W()\n    def test_case(self):\n        pass\n')
    mixed_binding_key = 'test_fixture.py:MixedBindingCase.test_case:0'
    mixed_binding_result = _fixture_census(
        {'app/tests/test_fixture.py': mixed_binding})
    case.assertEqual([mixed_binding_key], list(mixed_binding_result.sites),
                     'a later skip binding must not be hidden by an earlier non-skip binding')
    case.assertEqual([mixed_binding_key], mixed_binding_result.ambiguous,
                     'a skip and a non-skip binding of one name must fail closed')
    case.assertIn('ambiguous skip kind: ' + mixed_binding_key,
                  _verify_registry(mixed_binding_result, {}))
    all_non_skip_binding = platform_imports + (
        'def W():\n'
        '    return None\n'
        'def W():\n'
        '    return None\n'
        'class NonSkipBindingCase(unittest.TestCase):\n'
        '    @W()\n    def test_case(self):\n        pass\n')
    all_non_skip_result = _fixture_census(
        {'app/tests/test_fixture.py': all_non_skip_binding})
    case.assertEqual(set(), set(all_non_skip_result.sites),
                     'two non-skip bindings of one name are not a skip site')
    case.assertEqual([], all_non_skip_result.unresolved)

    returned_function = platform_imports + (
        'def skip_helper():\n'
        "    return unittest.skipUnless(sys.platform == 'win32', 'w')\n"
        'def W():\n'
        '    return skip_helper\n'
        'class ReturnedFunctionCase(unittest.TestCase):\n'
        '    @W()\n    def test_case(self):\n        pass\n')
    returned_function_result = _fixture_census(
        {'app/tests/test_fixture.py': returned_function})
    case.assertEqual(set(), set(returned_function_result.sites),
                     'a helper returning a function object is not a skip')
    case.assertEqual([], returned_function_result.unresolved)

    comprehension_target = platform_imports + (
        'x = 1\n'
        'LIMIT = 0\n'
        'VALUES = (1, 2)\n'
        'class ComprehensionTargetCase(unittest.TestCase):\n'
        "    @unittest.skipUnless(all(x > LIMIT for x in VALUES), 'w')\n"
        '    def test_case(self):\n        pass\n')
    _target_result, target_site = _single_site(
        case, {'app/tests/test_fixture.py': comprehension_target})
    target_homonym = comprehension_target.replace('x = 1', 'x = 2', 1)
    _target_homonym_result, target_homonym_site = _single_site(
        case, {'app/tests/test_fixture.py': target_homonym})
    case.assertEqual(target_site.digest('comprehension target'),
                     target_homonym_site.digest('comprehension target'),
                     'a comprehension target must not pin a module homonym')
    target_iterable = comprehension_target.replace('VALUES = (1, 2)', 'VALUES = (2, 3)', 1)
    _target_iterable_result, target_iterable_site = _single_site(
        case, {'app/tests/test_fixture.py': target_iterable})
    case.assertNotEqual(target_site.digest('comprehension target'),
                        target_iterable_site.digest('comprehension target'),
                        'an eager comprehension iterable must still be pinned')
    target_body = comprehension_target.replace('LIMIT = 0', 'LIMIT = 1', 1)
    _target_body_result, target_body_site = _single_site(
        case, {'app/tests/test_fixture.py': target_body})
    case.assertNotEqual(target_site.digest('comprehension target'),
                        target_body_site.digest('comprehension target'),
                        'a true module load in a comprehension body must still be pinned')

    lambda_parameter = platform_imports + (
        'x = 1\n'
        'FLAG = True\n'
        'class LambdaParameterCase(unittest.TestCase):\n'
        "    @unittest.skipUnless((lambda x: x and FLAG)(False), 'w')\n"
        '    def test_case(self):\n        pass\n')
    _lambda_target_result, lambda_target_site = _single_site(
        case, {'app/tests/test_fixture.py': lambda_parameter})
    lambda_homonym = lambda_parameter.replace('x = 1', 'x = 2', 1)
    _lambda_homonym_result, lambda_homonym_site = _single_site(
        case, {'app/tests/test_fixture.py': lambda_homonym})
    case.assertEqual(lambda_target_site.digest('lambda parameter'),
                     lambda_homonym_site.digest('lambda parameter'),
                     'a lambda parameter must not pin a module homonym')
    lambda_body = lambda_parameter.replace('FLAG = True', 'FLAG = False', 1)
    _lambda_body_result, lambda_body_site = _single_site(
        case, {'app/tests/test_fixture.py': lambda_body})
    case.assertNotEqual(lambda_target_site.digest('lambda parameter'),
                        lambda_body_site.digest('lambda parameter'),
                        'a lambda body must still pin the module global it reads')

    inline_lambda = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'class InlineLambdaCase(unittest.TestCase):\n'
        '    FLAG = False\n'
        "    @unittest.skipUnless((lambda: FLAG)(), 'w')\n"
        '    def test_case(self):\n        pass\n')
    _inline_result, inline_site = _single_site(
        case, {'app/tests/test_fixture.py': inline_lambda})
    inline_module_flip = inline_lambda.replace(
        "FLAG = sys.platform == 'win32'", "FLAG = sys.platform == 'linux'", 1)
    _inline_module_result, inline_module_site = _single_site(
        case, {'app/tests/test_fixture.py': inline_module_flip})
    case.assertNotEqual(
        inline_site.digest('inline lambda'), inline_module_site.digest('inline lambda'),
        'an inline lambda decorator must pin the module name it reads')
    inline_class_flip = inline_lambda.replace('    FLAG = False', '    FLAG = True')
    _inline_class_result, inline_class_site = _single_site(
        case, {'app/tests/test_fixture.py': inline_class_flip})
    case.assertEqual(
        inline_site.digest('inline lambda'), inline_class_site.digest('inline lambda'),
        'an inline lambda decorator must not pin the class homonym')
    inline_comprehension = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'class InlineComprehensionCase(unittest.TestCase):\n'
        '    FLAG = False\n'
        "    @unittest.skipUnless(all(FLAG for _ in (True,)), 'w')\n"
        '    def test_case(self):\n        pass\n')
    _comp_result, comp_site = _single_site(
        case, {'app/tests/test_fixture.py': inline_comprehension})
    comp_module_flip = inline_comprehension.replace(
        "FLAG = sys.platform == 'win32'", "FLAG = sys.platform == 'linux'", 1)
    _comp_module_result, comp_module_site = _single_site(
        case, {'app/tests/test_fixture.py': comp_module_flip})
    case.assertNotEqual(
        comp_site.digest('inline comprehension'),
        comp_module_site.digest('inline comprehension'),
        'an inline comprehension decorator must pin the module name it reads')
    comp_class_flip = inline_comprehension.replace('    FLAG = False', '    FLAG = True')
    _comp_class_result, comp_class_site = _single_site(
        case, {'app/tests/test_fixture.py': comp_class_flip})
    case.assertEqual(
        comp_site.digest('inline comprehension'),
        comp_class_site.digest('inline comprehension'),
        'an inline comprehension decorator must not pin the class homonym')
    assignment_lambda = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'class AssignmentLambdaCase(unittest.TestCase):\n'
        '    FLAG = False\n'
        "    D = unittest.skipUnless((lambda: FLAG)(), 'w')\n"
        '    @D\n'
        '    def test_case(self):\n        pass\n')
    _assigned_result, assigned_site = _single_site(
        case, {'app/tests/test_fixture.py': assignment_lambda})
    assigned_module_flip = assignment_lambda.replace(
        "FLAG = sys.platform == 'win32'", "FLAG = sys.platform == 'linux'", 1)
    _assigned_module_result, assigned_module_site = _single_site(
        case, {'app/tests/test_fixture.py': assigned_module_flip})
    case.assertNotEqual(
        assigned_site.digest('assignment lambda'),
        assigned_module_site.digest('assignment lambda'),
        'a class assignment lambda must pin the module name it reads')
    assigned_class_flip = assignment_lambda.replace('    FLAG = False', '    FLAG = True')
    _assigned_class_result, assigned_class_site = _single_site(
        case, {'app/tests/test_fixture.py': assigned_class_flip})
    case.assertEqual(
        assigned_site.digest('assignment lambda'),
        assigned_class_site.digest('assignment lambda'),
        'a class assignment lambda must not pin the class homonym')
    assignment_comprehension = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'class AssignmentComprehensionCase(unittest.TestCase):\n'
        '    FLAG = False\n'
        "    D = unittest.skipUnless(all(FLAG for _ in (True,)), 'w')\n"
        '    @D\n'
        '    def test_case(self):\n        pass\n')
    _assigned_comp_result, assigned_comp_site = _single_site(
        case, {'app/tests/test_fixture.py': assignment_comprehension})
    assigned_comp_module_flip = assignment_comprehension.replace(
        "FLAG = sys.platform == 'win32'", "FLAG = sys.platform == 'linux'", 1)
    _assigned_comp_module_result, assigned_comp_module_site = _single_site(
        case, {'app/tests/test_fixture.py': assigned_comp_module_flip})
    case.assertNotEqual(
        assigned_comp_site.digest('assignment comprehension'),
        assigned_comp_module_site.digest('assignment comprehension'),
        'a class assignment comprehension must pin the module name it reads')
    assigned_comp_class_flip = assignment_comprehension.replace(
        '    FLAG = False', '    FLAG = True')
    _assigned_comp_class_result, assigned_comp_class_site = _single_site(
        case, {'app/tests/test_fixture.py': assigned_comp_class_flip})
    case.assertEqual(
        assigned_comp_site.digest('assignment comprehension'),
        assigned_comp_class_site.digest('assignment comprehension'),
        'a class assignment comprehension must not pin the class homonym')
    inline_eager = platform_imports + (
        "FLAG = sys.platform == 'win32'\n"
        'class InlineEagerCase(unittest.TestCase):\n'
        '    FLAG = False\n'
        "    @unittest.skipUnless(FLAG, 'w')\n"
        '    def test_case(self):\n        pass\n')
    _eager_result, eager_site = _single_site(
        case, {'app/tests/test_fixture.py': inline_eager})
    eager_class_flip = inline_eager.replace('    FLAG = False', '    FLAG = True')
    _eager_class_result, eager_class_site = _single_site(
        case, {'app/tests/test_fixture.py': eager_class_flip})
    case.assertNotEqual(
        eager_site.digest('inline eager'), eager_class_site.digest('inline eager'),
        'an eager class expression must still pin the class binding')
    eager_module_flip = inline_eager.replace(
        "FLAG = sys.platform == 'win32'", "FLAG = sys.platform == 'linux'", 1)
    _eager_module_result, eager_module_site = _single_site(
        case, {'app/tests/test_fixture.py': eager_module_flip})
    case.assertEqual(
        eager_site.digest('inline eager'), eager_module_site.digest('inline eager'),
        'an eager class expression must not pin the shadowed module name')

    function_nested = platform_imports + (
        'def factory():\n'
        '    class FunctionNestedCase(unittest.TestCase):\n'
        "        @unittest.skipUnless(sys.platform == 'linux', 'linux')\n"
        '        def test_case(self):\n            pass\n'
        "    @unittest.skipUnless(sys.platform == 'linux', 'linux')\n"
        '    def test_helper():\n        pass\n')
    function_nested_result = _fixture_census(
        {'app/tests/test_fixture.py': function_nested})
    function_nested_keys = ['test_fixture.py:factory.FunctionNestedCase.test_case:0',
                            'test_fixture.py:factory.test_helper:0']
    case.assertEqual(function_nested_keys, sorted(function_nested_result.sites),
                     'function-nested skip decorators must still be censused')
    case.assertEqual([], function_nested_result.refused)
    case.assertEqual([], function_nested_result.ambiguous)
    case.assertTrue(all(any(key in item and 'function-scoped decorated skip' in item
                            for item in function_nested_result.unresolved)
                        for key in function_nested_keys),
                    'a function-scoped skip must be recorded as unresolved')
    function_nested_rows = {
        key: (function_nested_result.sites[key].digest('nested function'),
              'nested function') for key in function_nested_keys}
    case.assertIn('unresolved skip decorators',
                  _verify_registry(function_nested_result, function_nested_rows)[0],
                  'a matching row cannot authorize a function-scoped skip')
    function_windows = function_nested.replace(
        "sys.platform == 'linux'", "sys.platform == 'win32'")
    function_windows_result = _fixture_census(
        {'app/tests/test_fixture.py': function_windows})
    case.assertEqual(function_nested_keys, sorted(function_windows_result.refused),
                     'the removed whole-text guard refused function-nested Windows-only skips')
    function_control = platform_imports + (
        'def factory():\n'
        '    class PlainNestedCase(unittest.TestCase):\n'
        '        @staticmethod\n'
        '        def helper():\n            pass\n'
        '        def test_case(self):\n            pass\n')
    case.assertEqual(set(), set(_fixture_census(
        {'app/tests/test_fixture.py': function_control}).sites),
        'entering a function must not census non-skip decorators')

    walrus_source = platform_imports + (
        "if (RUN := sys.platform == 'win32'):\n    pass\n"
        'class WalrusCase(unittest.TestCase):\n'
        "    @unittest.skipUnless(RUN, 'w')\n"
        '    def test_case(self):\n        pass\n')
    walrus_result, walrus_site = _single_site(
        case, {'app/tests/test_fixture.py': walrus_source})
    case.assertEqual([walrus_site.key], walrus_result.truncated,
                     'a walrus-bound skip predicate must fail closed')
    case.assertIn('truncated census', _verify_registry(walrus_result, {
        walrus_site.key: (walrus_site.digest('walrus'), 'walrus')})[0],
        'a matching row cannot authorize a walrus binding')
    walrus_flip = walrus_source.replace(
        "sys.platform == 'win32'", "sys.platform == 'linux'")
    _walrus_flip_result, walrus_flip_site = _single_site(
        case, {'app/tests/test_fixture.py': walrus_flip})
    case.assertNotEqual(walrus_site.digest('walrus'), walrus_flip_site.digest('walrus'),
                        'the walrus binding must be pinned in the closure')
    with_binding = platform_imports + (
        "with contextlib.nullcontext(sys.platform == 'win32') as RUN:\n    pass\n"
        'class WithImportCase(unittest.TestCase):\n'
        "    @unittest.skipUnless(RUN, 'w')\n"
        '    def test_case(self):\n        pass\n')
    with_result, with_site = _single_site(
        case, {'app/tests/test_fixture.py': with_binding})
    case.assertEqual([with_site.key], with_result.truncated,
                     'a with-as skip predicate must fail closed')
    case.assertIn('truncated census', _verify_registry(with_result, {
        with_site.key: (with_site.digest('with as'), 'with as')})[0])
    except_binding = platform_imports + (
        "try:\n    raise RuntimeError('fixture')\n"
        'except RuntimeError as RUN:\n    pass\n'
        'class ExceptImportCase(unittest.TestCase):\n'
        "    @unittest.skipUnless(RUN, 'w')\n"
        '    def test_case(self):\n        pass\n')
    except_result, except_site = _single_site(
        case, {'app/tests/test_fixture.py': except_binding})
    case.assertEqual([except_site.key], except_result.truncated,
                     'an except-as skip predicate must fail closed')
    case.assertIn('truncated census', _verify_registry(except_result, {
        except_site.key: (except_site.digest('except as'), 'except as')})[0])
    walrus_alias = platform_imports + (
        "if (SU := unittest.skipUnless):\n    pass\n"
        'class WalrusAliasCase(unittest.TestCase):\n'
        "    @SU(sys.platform == 'win32', 'w')\n"
        '    def test_case(self):\n        pass\n')
    walrus_alias_result, walrus_alias_site = _single_site(
        case, {'app/tests/test_fixture.py': walrus_alias})
    case.assertEqual([walrus_alias_site.key], walrus_alias_result.ambiguous,
                     'a walrus-bound constructor alias must fail closed')
    case.assertTrue(any(problem.startswith('ambiguous skip kind') for problem in
                        _verify_registry(walrus_alias_result, {
                            walrus_alias_site.key: (walrus_alias_site.digest('walrus alias'),
                                                    'walrus alias')})),
                    'a matching row cannot authorize a dynamic constructor alias')

    constructor_alias = platform_imports + (
        'SU = unittest.skipUnless\n'
        'class ConstructorAliasCase(unittest.TestCase):\n'
        "    @SU(sys.platform == 'win32', 'w')\n"
        '    def test_case(self):\n        pass\n')
    alias_result, alias_site = _single_site(
        case, {'app/tests/test_fixture.py': constructor_alias})
    case.assertEqual([alias_site.key], alias_result.refused,
                     'a constructor alias must keep its Windows-only refusal')
    case.assertIn('literal Windows-only refusal', _verify_registry(alias_result, {
        alias_site.key: (alias_site.digest('alias'), 'alias')})[0],
        'a matching row cannot authorize an aliased constructor')
    alias_flip = constructor_alias.replace(
        'SU = unittest.skipUnless', 'SU = unittest.skipIf')
    _alias_flip_result, alias_flip_site = _single_site(
        case, {'app/tests/test_fixture.py': alias_flip})
    case.assertNotEqual(alias_site.digest('alias'), alias_flip_site.digest('alias'),
                        'the constructor alias binding must be pinned in the closure')
    posix_alias = constructor_alias.replace(
        'SU = unittest.skipUnless', 'SU = unittest.skipIf').replace(
        "sys.platform == 'win32'", "os.name == 'nt'")
    posix_alias_result, posix_alias_site = _single_site(
        case, {'app/tests/test_fixture.py': posix_alias})
    case.assertEqual([], posix_alias_result.refused)
    case.assertEqual([], _verify_registry(posix_alias_result, {
        posix_alias_site.key: (posix_alias_site.digest('posix alias'), 'posix alias')}))
    tuple_alias = platform_imports + (
        'SU, SI = unittest.skipUnless, unittest.skipIf\n'
        'class TupleAliasCase(unittest.TestCase):\n'
        "    @SU(sys.platform == 'win32', 'w')\n"
        '    def test_windows(self):\n        pass\n'
        "    @SI(os.name == 'nt', 'posix')\n"
        '    def test_posix(self):\n        pass\n')
    tuple_alias_result = _fixture_census({'app/tests/test_fixture.py': tuple_alias})
    case.assertEqual(['test_fixture.py:TupleAliasCase.test_posix:0',
                      'test_fixture.py:TupleAliasCase.test_windows:0'],
                     sorted(tuple_alias_result.sites),
                     'tuple-unpacked skip constructors must be censused')
    case.assertEqual(['test_fixture.py:TupleAliasCase.test_windows:0'],
                     tuple_alias_result.refused,
                     'a tuple alias must keep its own constructor polarity')
    swapped_alias = tuple_alias.replace(
        'SU, SI = unittest.skipUnless, unittest.skipIf',
        'SI, SU = unittest.skipUnless, unittest.skipIf')
    case.assertEqual(['test_fixture.py:TupleAliasCase.test_posix:0'],
                     _fixture_census({'app/tests/test_fixture.py': swapped_alias}).refused,
                     'tuple aliases must bind by position, not by name')
    starred_alias = platform_imports + (
        'SU, *REST = unittest.skipUnless, unittest.skipIf\n'
        'class StarredAliasCase(unittest.TestCase):\n'
        "    @SU(sys.platform == 'win32', 'w')\n"
        '    def test_case(self):\n        pass\n')
    starred_result, starred_site = _single_site(
        case, {'app/tests/test_fixture.py': starred_alias})
    case.assertEqual([starred_site.key], starred_result.ambiguous,
                     'a starred constructor alias must fail closed')
    case.assertIn('ambiguous skip kind', _verify_registry(starred_result, {
        starred_site.key: (starred_site.digest('starred alias'), 'starred alias')})[0])

    dynamic = _fixture_census({'app/tests/test_fixture.py': (
        'import unittest\nclass DynamicCase(unittest.TestCase):\n'
        "    @getattr(unittest, 'skipUnless')('w')\n"
        '    def test_case(self):\n        pass\n')})
    case.assertEqual(set(), set(dynamic.sites),
                     'dynamic decorators are a disclosed static residual; Linux shards are the authority')
    runtime = _fixture_census({'app/tests/test_fixture.py': (
        'import unittest\nclass RuntimeCase(unittest.TestCase):\n'
        '    def test_case(self):\n        self.skipTest(' + repr('w') + ')\n')})
    case.assertEqual(set(), set(runtime.sites),
                     'runtime skipTest is a disclosed residual; Linux shards are the authority')
    _assert_class_attribute_alias_controls(case)


def _assert_class_attribute_alias_controls(case):
    '''A skip decorator stored on a class attribute is censused, not skipped.'''
    attribute_imports = 'import os\nimport sys\nimport unittest\n'
    case_body = ('class Case(unittest.TestCase):\n'
                 '    @{0}\n    def test_case(self):\n        pass\n')

    def class_alias(literal):
        condition = 'sys.platform == {0!r}'.format(literal)
        return attribute_imports + (
            'class Holder:\n'
            "    WIN = unittest.skipUnless({0}, 'fixture')\n".format(condition))

    windows_source = class_alias('win32') + case_body.format('Holder.WIN')
    windows_result, windows_site = _single_site(
        case, {'app/tests/test_fixture.py': windows_source})
    case.assertEqual([windows_site.key], windows_result.refused,
                     'a class-attribute skip alias must resolve to its skip kind')
    case.assertEqual('literal Windows-only refusal',
                     windows_result.refusal_reasons[windows_site.key])
    case.assertEqual([], windows_result.unresolved)
    case.assertEqual([], windows_result.ambiguous)
    case.assertEqual([], windows_result.truncated)
    case.assertIn('Holder.WIN',
                  [name for _relpath, name, _digest in windows_site.entries],
                  'the class-body alias binding must be pinned in the closure')
    case.assertIn('literal Windows-only refusal',
                  _verify_registry(windows_result, {
                      windows_site.key: (windows_site.digest('windows alias'),
                                         'windows alias')})[0],
                  'a matching row cannot authorize a class-attribute Windows alias')

    posix_source = class_alias('linux') + case_body.format('Holder.WIN')
    posix_result, posix_site = _single_site(
        case, {'app/tests/test_fixture.py': posix_source})
    case.assertEqual([], posix_result.refused,
                     'a POSIX class-attribute alias must stay permitted')
    case.assertEqual([], _verify_registry(posix_result, {
        posix_site.key: (posix_site.digest('posix alias'), 'posix alias')}),
        'the reviewed POSIX class-attribute row must still verify')
    case.assertNotEqual(windows_site.digest('windows alias'),
                        posix_site.digest('posix alias'),
                        'the class-body alias value must be pinned in the digest')
    case.assertNotEqual([], _verify_registry(posix_result, {
        posix_site.key: (windows_site.digest('windows alias'), 'windows alias')}),
        'a stale Windows declaration cannot authorize the POSIX class attribute')

    nested_source = attribute_imports + (
        'class Outer:\n    class Holder:\n'
        "        WIN = unittest.skipUnless(sys.platform == 'win32', 'fixture')\n"
        + case_body.format('Outer.Holder.WIN'))
    nested_result, nested_site = _single_site(
        case, {'app/tests/test_fixture.py': nested_source})
    case.assertEqual([nested_site.key], nested_result.refused,
                     'a nested class-attribute alias must resolve through both class scopes')

    helper = attribute_imports + (
        'class Holder:\n'
        "    WIN = unittest.skipUnless(sys.platform == 'win32', 'fixture')\n")
    for binding, decorator in (
            ('from tests.fixture_helper import Holder', 'Holder.WIN'),
            ('import tests.fixture_helper as helper', 'helper.Holder.WIN')):
        imported = {'app/tests/test_fixture.py': (
                        attribute_imports + binding + '\n' + case_body.format(decorator)),
                    'app/tests/fixture_helper.py': helper}
        imported_result = _fixture_census(imported)
        key = 'test_fixture.py:Case.test_case:0'
        case.assertEqual([key], list(imported_result.sites), binding)
        case.assertEqual([key], imported_result.refused, binding)
        case.assertIn('app/tests/fixture_helper.py',
                      [relpath for relpath, _name, _digest
                       in imported_result.sites[key].entries],
                      f'{binding}: the module that binds the class must be pinned')

    rebound = attribute_imports + (
        'class Holder:\n'
        "    WIN = unittest.skipUnless(sys.platform == 'win32', 'fixture')\n"
        "    WIN = unittest.skipIf(sys.platform == 'win32', 'fixture')\n"
        + case_body.format('Holder.WIN'))
    conditional = attribute_imports + (
        'class Holder:\n'
        "    if sys.platform == 'win32':\n"
        "        WIN = unittest.skipUnless(True, 'fixture')\n"
        '    else:\n'
        "        WIN = unittest.skipUnless(False, 'fixture')\n"
        + case_body.format('Holder.WIN'))
    for name, fail_closed in (('rebound', rebound), ('conditional', conditional)):
        closed_result, closed_site = _single_site(
            case, {'app/tests/test_fixture.py': fail_closed})
        case.assertEqual([closed_site.key], closed_result.ambiguous,
                         f'{name}: a class attribute without one proven binding fails closed')
        case.assertIn('ambiguous skip kind', _verify_registry(closed_result, {
            closed_site.key: (closed_site.digest(name), name)})[0],
            f'{name}: a matching row cannot authorize the ambiguity')

    missing = _fixture_census({'app/tests/test_fixture.py': attribute_imports + (
        'class Holder:\n    pass\n' + case_body.format('Holder.MISSING'))})
    case.assertEqual([], list(missing.sites),
                     'an unbound class attribute is not itself a skip site')
    case.assertEqual(1, len(missing.unresolved))
    case.assertIn('Holder.MISSING', missing.unresolved[0])
    case.assertIn('unresolved skip decorators', _verify_registry(missing, {})[0],
                  'an unresolvable class-attribute decorator must fail the registry check')
    inherited = attribute_imports + (
        'class Base:\n'
        "    WIN = unittest.skipUnless(sys.platform == 'win32', 'fixture')\n"
        'class Holder(Base): pass\n' + case_body.format('Holder.WIN'))
    scoped_inherited = attribute_imports + (
        'class Outer:\n'
        '    class Base:\n'
        "        WIN = unittest.skipUnless(sys.platform == 'win32', 'fixture')\n"
        '    class Holder(Base): pass\n'
        '    @Holder.WIN\n'
        '    class Case(unittest.TestCase):\n'
        '        def test_case(self): pass\n')
    for name, source in (('module-root inherited', inherited),
                         ('class-scoped inherited', scoped_inherited)):
        inherited_result = _fixture_census({'app/tests/test_fixture.py': source})
        case.assertEqual([], list(inherited_result.sites), name)
        case.assertEqual(1, len(inherited_result.unresolved), name)
        case.assertIn('Holder.WIN', inherited_result.unresolved[0], name)
        case.assertIn('unresolved skip decorators',
                      _verify_registry(inherited_result, {})[0], name)
    post_body = _fixture_census({'app/tests/test_fixture.py': attribute_imports + (
        'class Holder:\n    pass\n'
        "Holder.WIN = unittest.skipUnless(sys.platform == 'win32', 'fixture')\n"
        + case_body.format('Holder.WIN'))})
    case.assertEqual([], list(post_body.sites),
                     'an attribute assigned after the class body has no static binding')
    case.assertEqual(1, len(post_body.unresolved),
                     'a post-body attribute assignment must fail closed, not pass silently')

    constant = _fixture_census({'app/tests/test_fixture.py': attribute_imports + (
        'class Holder:\n    LIMIT = 3\n' + case_body.format('Holder.LIMIT'))})
    case.assertEqual([], list(constant.sites),
                     'a plain class constant must not become a skip site')
    case.assertEqual([], constant.unresolved)

    constructor = {'app/tests/test_fixture.py': attribute_imports + (
        'class Holder:\n    WIN = unittest.skipUnless\n' + case_body.format(
            "Holder.WIN(sys.platform == 'win32', 'fixture')"))}
    constructor_result, constructor_site = _single_site(case, constructor)
    case.assertEqual([constructor_site.key], constructor_result.refused,
                     'a class attribute holding the constructor keeps the call predicate')

    applied = _fixture_census({'app/tests/test_fixture.py': attribute_imports + (
        'class Holder:\n    SW = unittest.skipUnless\n'
        'class Case(unittest.TestCase):\n'
        '    def test_case(self):\n        pass\n'
        "    test_case = Holder.SW(sys.platform == 'win32', 'fixture')(test_case)\n")})
    case.assertEqual([], list(applied.sites))
    case.assertEqual(1, len(applied.unresolved),
                     'an assignment-applied class attribute must stay fail-closed')
    case.assertIn('unsupported assignment-applied skip', applied.unresolved[0])

    predicate = attribute_imports + (
        'class Holder:\n'
        "    RUN = sys.platform == 'linux'\n"
        "    WIN = unittest.skipUnless(RUN, 'fixture')\n"
        + case_body.format('Holder.WIN'))
    predicate_result, predicate_site = _single_site(
        case, {'app/tests/test_fixture.py': predicate})
    flip_source = predicate.replace("== 'linux'", "== 'darwin'")
    flip_result, flip_site = _single_site(
        case, {'app/tests/test_fixture.py': flip_source})
    case.assertEqual([], predicate_result.refused,
                     'a non-literal class predicate is pinned, never proven by the census')
    case.assertNotEqual(predicate_site.digest('class predicate'),
                        flip_site.digest('class predicate'),
                        'the class predicate binding must be pinned in the closure')
    case.assertNotEqual([], _verify_registry(flip_result, {
        predicate_site.key: (predicate_site.digest('class predicate'),
                             'class predicate')}),
        'a stale class predicate row must fail after the constant changed')


class NativeWindowsCiTests(unittest.TestCase):
    def test_invalid_repin_arguments_refuse_before_registry_access(self):
        module = sys.modules[__name__]
        invalid_arguments = (
            [], ['--apply'], ['--unknown'], ['--repin', '--unknown'],
            ['--apply', '--repin'], ['--repin', '--repin'],
            ['--repin', '--apply', '--apply'], ['--repin', '--apply', 'extra'],
            ['--repin', 'registry.txt'],
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                registry = mock.Mock()
                with mock.patch.object(module, 'SKIP_REGISTRY', registry), \
                        mock.patch.object(module, '_repin') as repin, \
                        mock.patch.object(module, '_write_registry') as write, \
                        contextlib.redirect_stdout(io.StringIO()) as output, \
                        contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(2, main(arguments))
                self.assertEqual('', output.getvalue())
                self.assertIn('--repin [--apply]', errors.getvalue())
                registry.read_text.assert_not_called()
                repin.assert_not_called()
                write.assert_not_called()

    def test_ci_runs_all_native_methods_on_windows(self):
        source = SCRIPT.read_text()
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == 'NativeWindowsOwnerTests')
        methods = {node.name for node in cls.body
                   if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')}
        self.assertEqual({'test_inherited_pipe_retains_detached_child_ownership',
                          'test_closed_pipe_retains_detached_child_ownership',
                          'test_current_python_environment_override_and_explicit_handle_are_preserved',
                          'test_inherited_hidden_drive_environment_survives_python_bootstrap',
                          'test_native_high_bit_worker_exit_status_is_preserved',
                          'test_parent_exit_closes_the_only_job_handle_and_stops_its_members'}, methods)
        retirement = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                          and node.name == 'NativeWindowsRetirementTests')
        retirement_methods = {node.name for node in retirement.body
                              if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')}
        self.assertEqual({'test_private_stage_finish_removes_stage_and_slot',
                          'test_hard_exit_recovery_removes_private_slot',
                          'test_live_owner_is_preserved'}, retirement_methods)
        self.assertIn('result.testsRun == 9 and not result.skipped', source)
        workflow = (ROOT / '.github/workflows/tests.yml').read_text().split('  windows-owner:', 1)[1]
        self.assertIn('runs-on: windows-latest', workflow)
        self.assertIn('run: python tests/native_windows_owner_checks.py', workflow)
        self.assertIn('python -m venv --system-site-packages .native-owner-venv', workflow)
        self.assertIn('assert sys.executable != sys._base_executable', workflow)
        self.assertIn('.native-owner-venv/Scripts/python.exe tests/native_windows_owner_checks.py', workflow)
        self.assertIn('run: python -m unittest tests.test_export_staging -v', workflow)
        self.assertIn('PYTHONPATH: .', workflow)
        self.assertFalse((SCRIPT.parent / 'test_windows_owner_native.py').exists())

    def test_wrong_host_is_refused_instead_of_printing_a_successful_skipped_suite(self):
        result = subprocess.run([sys.executable, str(SCRIPT)],
                                env={**os.environ, 'PYTHONPATH': str(ROOT / 'app')},
                                capture_output=True, text=True, timeout=10)
        if sys.platform == 'win32':
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('Ran 9 tests', result.stderr)
            self.assertIn('OK', result.stderr)
        else:
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn('nothing was verified', result.stderr)
            self.assertNotIn('OK', result.stderr)

    def test_ci_runs_every_native_transaction_check_without_skips(self):
        _assert_transaction_contract(self)
        _assert_stdlib_imports(self)
        _assert_skip_registry(self)
        _assert_scan_sources_reuse(self)
        _assert_csp_controls(self)
        _assert_alias_gate_contract(self)
        _assert_workflow_contract(self)
        _assert_alias_argv(self)
        _assert_alias_receipt(self)
        _assert_alias_containment(self)
        _assert_alias_junction_primitive(self)

    def test_native_transaction_checks_refuse_a_non_windows_host(self):
        probe = (
            "import builtins,runpy,sys,unittest\n"
            "from pathlib import Path\n"
            "original_import=builtins.__import__\n"
            "stdlib=set(sys.stdlib_module_names)|set(sys.builtin_module_names)\n"
            "def guarded_import(name,globals=None,locals=None,fromlist=(),level=0):\n"
            "    if level == 0 and name.split('.',1)[0] not in stdlib:\n"
            "        raise AssertionError('application dependency imported before host refusal')\n"
            "    return original_import(name,globals,locals,fromlist,level)\n"
            "builtins.__import__=guarded_import\n"
            "import xml.etree.ElementTree\n"
            "sys.platform='darwin'\n"
            "if len(sys.argv)>2:\n"
            "    source='import '+sys.argv[2]+'\\n'+Path(sys.argv[1]).read_text(encoding='utf-8')\n"
            "    exec(compile(source,sys.argv[1],'exec'),{'__name__':'__main__','__file__':sys.argv[1]})\n"
            "    raise AssertionError('mutated runner returned without refusal')\n"
            "runpy.run_path(sys.argv[1],run_name='__main__')\n")
        refused = subprocess.run([sys.executable, '-B', '-c', probe,
                                  str(TRANSACTION_SCRIPT)],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(2, refused.returncode, refused.stderr)
        self.assertIn('nothing was verified', refused.stderr)
        self.assertNotIn('application dependency imported', refused.stderr)
        for dependency in ('numpy', 'sound_design_assets', 'book_recovery_archive'):
            with self.subTest(hoisted_dependency=dependency):
                hoisted = subprocess.run([sys.executable, '-B', '-c', probe,
                                          str(TRANSACTION_SCRIPT), dependency],
                                         capture_output=True, text=True, timeout=10)
                self.assertEqual(1, hoisted.returncode, hoisted.stderr)
                self.assertIn('application dependency imported before host refusal', hoisted.stderr)
                self.assertNotIn('nothing was verified', hoisted.stderr)
        path = os.pathsep.join(filter(None, (str(ROOT / 'app'), os.environ.get('PYTHONPATH'))))
        result = subprocess.run([sys.executable, str(TRANSACTION_SCRIPT)],
                                env={**os.environ, 'PYTHONPATH': path},
                                capture_output=True, text=True, timeout=120)
        if sys.platform == 'win32':
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(f'Ran {sum(TRANSACTION_CLASSES.values())} tests', result.stderr)
        else:
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn('nothing was verified', result.stderr)
            self.assertNotIn('OK', result.stderr)


if __name__ == '__main__':
    raise SystemExit(main())

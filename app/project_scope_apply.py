"""Compound source-scope publication with one bounded persistent exact undo."""
import base64
import hashlib
import json
from pathlib import Path
from book_state_transaction import apply_book_state_locked
from project_scope_files import capture_scope_file, MAX_SCOPE_METADATA_BYTES
from project_scope_plan import get_scope_preview
from project_scope_admission import is_scope_admitted
from project_scope_assets import get_scope_asset_snapshot
from project_scope_exports import get_scope_export_inventory

UNDO = '.project_scope_undo.json'
FILES = ('state.json', 'annotated_script.json', 'voice_config.json', 'chunks.json',
         'chunks.json.status.jsonl')


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def _digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _get_guards(root, plan):
    keys = ('path','exists','file_version','sha256','size')
    paths = {item['audio']['path']: {key:item['audio'][key] for key in keys}
             for item in plan['audio'] if item['audio'] is not None}
    for item in plan['history']['retained_versions']:
        paths[item['path']] = {'path': item['path'], 'exists': True,
            'file_version': item['file_version'], 'sha256': item['audio']['sha256'],
            'size': item['audio']['size']}
    for key in ('manifest', 'pending'):
        proof = plan['history'][key]; paths[proof['path']] = proof
    for proof in (plan['preserved_files'] + plan['history']['preserved_payloads']
                  + plan['history']['predecessors']):
        if proof['path'] in paths and paths[proof['path']] != proof:
            raise ValueError('Source-scope artifacts changed between preview readers.')
        paths[proof['path']] = proof
    guards = []
    for name in sorted(paths):
        guards.append({'external': False, 'proof': paths[name]})
    source = plan['source_file']
    proof = {key: source[key] for key in ('path','exists','file_version','sha256','size')}
    guards.append({'external': True, 'absolute_path': source['absolute_path'], 'proof': proof})
    _check_guards(root, guards)
    return guards


def _check_guards(root, guards):
    if not isinstance(guards, list) or not guards:
        raise ValueError('Source-scope artifact guards need inspection.')
    for guard in guards:
        if (not isinstance(guard, dict) or type(guard.get('external')) is not bool
                or set(guard) != ({'external','proof','absolute_path'} if guard['external'] else {'external','proof'})
                or not isinstance(guard['proof'], dict)):
            raise ValueError('Invalid source-scope artifact guard.')
        parent = root
        if guard['external']:
            path = guard['absolute_path']
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise ValueError('Invalid source-scope source path.')
            parent = Path(path).parent
            if guard['proof'].get('path') != Path(path).name:
                raise ValueError('Source-scope source binding differs.')
        current, _ = capture_scope_file(parent, guard['proof'].get('path'))
        if current != guard['proof']:
            raise ValueError('A source-scope artifact changed; exact undo was preserved for inspection.')


def _check_assets(assets):
    if not isinstance(assets, list):
        raise ValueError('Invalid source-scope voice artifact guards.')
    for asset in assets:
        if (not isinstance(asset, dict) or not isinstance(asset.get('absolute_path'), str)
                or get_scope_asset_snapshot(asset['absolute_path']) != asset):
            raise ValueError('A retained voice artifact changed; scope operation was refused.')


def apply_source_scope(root, start_entry, expected_revision):
    """Caller excludes all local/foreign tasks and holds portable book admission."""
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission are required to apply source scope.')
    existing, _ = capture_scope_file(root, UNDO)
    if existing['exists']:
        raise ValueError('An existing scope undo must be resolved before another scope change.')
    plan = get_scope_preview(root, start_entry)
    if type(expected_revision) is not str or plan['revision'] != expected_revision:
        raise ValueError('Scope preview changed; preview the selection again.')
    before = {}
    for name in FILES:
        proof, raw = capture_scope_file(root, name, include_bytes=True)
        if proof != plan['files'][name]:
            raise ValueError('Project changed before scope publication.')
        before[name] = raw
    if sum(len(raw) for raw in before.values() if raw is not None) > 32 * 1024**2:
        raise ValueError('Scope undo metadata exceeds its 32 MiB input limit.')
    state = json.loads(before['state.json'])
    state['_source_scope'] = plan['source_scope']
    state['_source_scope_exports'] = plan['export_inventory']
    after = {'state.json': _encode(state), 'annotated_script.json': _encode(plan['selected_script']),
             'voice_config.json': _encode(plan['voices']['voices']),
             'chunks.json': _encode(plan['selected_chunks']), 'chunks.json.status.jsonl': None}
    undo = {'version': 1, 'book_token': plan['book_token'], 'preview_revision': plan['revision'],
            'before': {name: base64.b64encode(raw).decode('ascii') if raw is not None else None
                       for name, raw in before.items()},
            'before_sha256': {name: _digest(raw) for name, raw in before.items()},
            'guards': _get_guards(root, plan),
            'voice_artifacts': plan['voice_artifacts'],
            'exports': plan['export_inventory'],
            'after': {name: _digest(raw) for name, raw in after.items()}}
    undo_bytes = _encode(undo)
    if len(undo_bytes) > MAX_SCOPE_METADATA_BYTES:
        raise ValueError('Scope undo record exceeds its byte limit.')
    replacements = {name: raw for name, raw in after.items() if raw is not None}
    replacements[UNDO] = undo_bytes
    _check_assets(undo['voice_artifacts'])
    if get_scope_export_inventory(root) != undo['exports']:
        raise ValueError('Exports changed before scope publication.')
    apply_book_state_locked(root, replacements, [name for name, raw in after.items() if raw is None])
    return {'revision': plan['revision'], 'retained_uids': plan['retained_uids'],
            'removed_uids': plan['removed_uids'], 'undo_available': True}


def _get_undo(root):
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission are required to undo source scope.')
    proof, raw = capture_scope_file(root, UNDO, include_bytes=True)
    if not proof['exists']:
        raise ValueError('No source-scope undo is available.')
    undo = json.loads(raw)
    if (not isinstance(undo, dict) or set(undo) != {'version','book_token','preview_revision',
                'before','before_sha256','after','guards','voice_artifacts','exports'}
            or type(undo.get('version')) is not int or undo['version'] != 1
            or not isinstance(undo.get('before'), dict) or set(undo['before']) != set(FILES)
            or not isinstance(undo.get('before_sha256'), dict) or set(undo['before_sha256']) != set(FILES)
            or not isinstance(undo.get('after'), dict) or set(undo['after']) != set(FILES)):
        raise ValueError('Source-scope undo record needs inspection.')
    before = {}
    for name in FILES:
        _current, current = capture_scope_file(root, name, include_bytes=True)
        if _digest(current) != undo['after'][name]:
            raise ValueError('Project changed after scope apply; exact undo was preserved for inspection.')
        saved = undo['before'][name]
        before[name] = base64.b64decode(saved, validate=True) if isinstance(saved, str) else None
        if saved is not None and not isinstance(saved, str):
            raise ValueError('Invalid source-scope undo bytes.')
        if _digest(before[name]) != undo['before_sha256'][name]:
            raise ValueError('Saved source-scope undo bytes changed; inspect the undo record.')
    if sum(len(value) for value in before.values() if value is not None) > 32 * 1024**2:
        raise ValueError('Scope undo metadata exceeds its input limit.')
    _check_guards(root, undo['guards'])
    _check_assets(undo['voice_artifacts'])
    if get_scope_export_inventory(root) != undo['exports']:
        raise ValueError('Exports changed after scope apply; exact undo was preserved for inspection.')
    return proof, undo, before


def get_source_scope_undo_preview(root):
    """Bind the exact undo record after checking current metadata/artifacts."""
    proof, undo, _before = _get_undo(root)
    return {'revision': proof['sha256'], 'book_token': undo['book_token'], 'restores': list(FILES)}


def get_source_scope_undo_status(root):
    """Expose the exact discardable record even when later edits prevent undo."""
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission are required to inspect source-scope undo.')
    proof, _ = capture_scope_file(root, UNDO)
    if not proof['exists']:
        return {'available':False, 'can_undo':False, 'revision':None}
    try:
        result = get_source_scope_undo_preview(root)
        return {**result, 'available':True, 'can_undo':True}
    except (ValueError, OSError, RecursionError) as error:
        return {'available':True, 'can_undo':False, 'revision':proof['sha256'], 'reason':str(error)[:240]}


def discard_source_scope_undo(root, expected_revision):
    """Explicitly forget one reviewed undo record, keeping all current content."""
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission are required to discard source-scope undo.')
    proof, _ = capture_scope_file(root, UNDO)
    if not proof['exists'] or type(expected_revision) is not str or proof['sha256'] != expected_revision:
        raise ValueError('Source-scope undo changed; inspect it again before discarding.')
    apply_book_state_locked(root, {}, [UNDO])
    return {'discarded':True}


def apply_source_scope_undo(root, expected_revision):
    """Restore exact prior metadata only for an explicitly reviewed undo record."""
    proof, _undo, before = _get_undo(root)
    if type(expected_revision) is not str or proof['sha256'] != expected_revision:
        raise ValueError('Source-scope undo changed; preview undo again.')
    apply_book_state_locked(root, {name: value for name, value in before.items() if value is not None},
                            [UNDO] + [name for name, value in before.items() if value is None])
    return {'restored': True}

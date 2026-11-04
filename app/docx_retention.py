"""Bounded metadata-only inventory; never admits or modifies a source."""
import os
import stat
from pathlib import Path

from docx_publication import get_docx_group_names


def get_retained_docx_inventory(directory, quota_limit, *, scan_limit=4096, group_limit=100):
    groups = {}
    scanned = counted_bytes = omitted = 0
    complete = True
    unavailable = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if scanned >= scan_limit:
                    complete = False
                    break
                scanned += 1
                size = None
                try:
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
                        raise ValueError('Not a regular file')
                    size = info.st_size
                    counted_bytes += size
                except (OSError, ValueError):
                    unavailable = True
                if '-aa2docx-' not in entry.name:
                    continue
                candidate = entry.name
                for suffix in ('.docx-receipt.json', '.docx', '.txt'):
                    if candidate.endswith(suffix):
                        candidate = candidate[:-len(suffix)] + '.txt'
                        break
                names = get_docx_group_names(candidate)
                valid = names is not None and entry.name in names.values()
                key = candidate if valid else entry.name
                if key not in groups:
                    if len(groups) >= group_limit:
                        omitted += 1
                        continue
                    groups[key] = {'expected_filenames': names if valid else None, 'files': []}
                groups[key]['files'].append({'filename': entry.name, 'bytes': size,
                    'state': 'unverified' if valid and size is not None else 'unavailable'})
    except OSError:
        complete = False
        unavailable = True
    for group in groups.values():
        names = group['expected_filenames']
        present = {item['filename'] for item in group['files']}
        group['state'] = ('unavailable' if any(item['state'] == 'unavailable' for item in group['files'])
            else 'unverified' if names and present == set(names.values()) else 'incomplete/unverified')
    return {'location': str(Path(directory).absolute()), 'groups': list(groups.values()),
        'scanned_entries': scanned, 'scan_limit': scan_limit, 'group_limit': group_limit,
        'scan_complete': complete, 'omitted_marker_entries': omitted,
        'observed_named_bytes': counted_bytes, 'quota_limit_bytes': quota_limit,
        'unavailable_entries': unavailable,
        'notice': 'Metadata only; contents, receipts and digests are not verified. Files may change during inspection. '
            'Observed bytes are a partial quota count, excluding active anonymous uploads and unreadable entries. '
            'Scan/result limits may omit files; unknown names without the reserved marker are not listed. '
            'Stop the app before manual cleanup. Keep full backups. Do not remove currently selected/referenced groups. '
            'Back up all original, text and receipt members together, including partial groups. No automatic deletion.'}

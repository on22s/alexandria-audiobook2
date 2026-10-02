"""Validate artifact membership in the collector's canonical index records."""
import csv
from pathlib import Path
import re


def get_index_artifact_names(path):
    path = Path(path)
    if path.suffix == '.csv':
        with path.open(encoding='utf-8', newline='') as handle:
            rows = csv.DictReader(handle)
            if not rows.fieldnames or 'artifact' not in rows.fieldnames:
                raise RuntimeError(f'{path.name} has no artifact column')
            return {row['artifact'] for row in rows if row.get('artifact')}
    text = re.sub(r'<!--.*?-->', '', path.read_text(encoding='utf-8'), flags=re.DOTALL)
    names = set()
    in_fence = None
    artifact_table = False
    header_pending = False
    for line in text.splitlines():
        stripped = line.strip()
        fence = re.match(r'^(`{3,}|~{3,})', stripped)
        if fence:
            marker = fence.group(1)
            if in_fence is None:
                in_fence = marker
            elif marker[0] == in_fence[0] and len(marker) >= len(in_fence):
                in_fence = None
            artifact_table = header_pending = False
            continue
        if in_fence:
            continue
        if stripped == '| artifact | why |':
            header_pending = True
            artifact_table = False
            continue
        if header_pending:
            artifact_table = bool(re.fullmatch(r'\|\s*:?-{3,}:?\s*\|\s*:?-{3,}:?\s*\|', stripped))
            header_pending = False
            continue
        if artifact_table:
            row = re.fullmatch(r'\|\s*`([^`|]+)`\s*\|\s*([^|]+)\s*\|', stripped)
            if row:
                names.add(row.group(1))
            else:
                artifact_table = False
    return names


def require_results_index_entries(repo, *filenames):
    for index_name in ('RESULTS_INDEX.md', 'results_index.csv'):
        names = get_index_artifact_names(Path(repo) / index_name)
        missing = [name for name in filenames if name not in names]
        if missing:
            raise RuntimeError(f"{index_name} is missing artifact records for {', '.join(missing)}")

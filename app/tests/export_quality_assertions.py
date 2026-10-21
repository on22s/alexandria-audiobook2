"""Compare chapter semantics while excluding per-run receipt timestamps."""
import copy


def stable_chapter_evidence(value):
    result = copy.deepcopy(value)
    def normalize(item):
        if isinstance(item, dict):
            report = item.get('export_quality_report')
            if isinstance(report, dict):
                # The inode/version and scan wall time change on every render.
                # Keep all plan, frame, hash, silence and codec assertions.
                report.pop('file_version', None)
                report.pop('seconds', None)
            for child in item.values():
                normalize(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                normalize(child)
    normalize(result)
    return result

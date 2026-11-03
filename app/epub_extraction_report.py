"""Bounded import snapshots; no source rewriting or publication authority."""
import hashlib
import json


MAX_REPORT_ROWS_BYTES = 128 * 1024
MAX_REPORT_FIELD_CHARS = 512


class EpubExtractionReport:
    def __init__(self, archive_bytes):
        self.source_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        self.rows = []
        self.row_bytes = 0
        self.omitted_rows = 0
        self.omitted_rows_by_kind = {}
        self.row_bytes_by_group = {'identity': 0, 'detail': 0}

    def apply_row(self, kind, **values):
        row = {'kind': kind}
        truncated = []
        for name, value in values.items():
            if isinstance(value, str) and len(value) > MAX_REPORT_FIELD_CHARS:
                value = value[:MAX_REPORT_FIELD_CHARS]
                truncated.append(name)
            row[name] = value
        if truncated:
            row['truncated_fields'] = truncated
        size = len(json.dumps(row, ensure_ascii=False).encode('utf-8')) + 2
        group = 'identity' if kind in {'package', 'spine'} else 'detail'
        if self.row_bytes_by_group[group] + size > MAX_REPORT_ROWS_BYTES // 2:
            self.omitted_rows += 1
            self.omitted_rows_by_kind[kind] = self.omitted_rows_by_kind.get(kind, 0) + 1
        else:
            self.rows.append(row)
            self.row_bytes += size
            self.row_bytes_by_group[group] += size

    def get_report(self, text):
        return {
            'schema_version': 1,
            'source_sha256': self.source_sha256,
            'extracted_text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'extracted_characters': len(text),
            'identity_scope': 'captured_archive_and_import_text_not_current_editor',
            'normalization': 'existing_invisible_character_space_and_blank_line_normalization',
            'title_policy': 'navigation_label_when_not_already_near_target; no_html_title_or_generated_title',
            'content_policy': 'spine_order; style_script_title_not_spoken; no_front_back_matter_exclusion',
            'formatting': 'plain_text_only; emphasis_not_attached; xhtml_uses_tolerant_html_parser',
            'rows': list(self.rows),
            'omitted_rows': self.omitted_rows,
            'omitted_rows_by_kind': dict(self.omitted_rows_by_kind),
            'detail_complete': not self.omitted_rows and not any('truncated_fields' in row for row in self.rows),
            'resource_text_digest_scope': 'after_navigation_title_insertion_strip_and_normalization',
        }

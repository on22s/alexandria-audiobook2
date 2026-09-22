"""Shared rendering for actionable validation failures in generation logs."""

import json


def format_validation_findings(findings):
    """Render findings with source, rejected-value, and expected-value context.

    Findings without entry context retain their existing message verbatim. This
    keeps batch-level failures useful while making entry-level failures identify
    the exact frozen line that was rejected.
    """
    rendered = []
    for finding in findings or []:
        message = finding.get("message")
        if not message:
            continue
        details = []
        if finding.get("entry_number") is not None:
            details.append(f"entry {finding['entry_number']}")
        if "source_line" in finding:
            details.append(f"source line={json.dumps(finding['source_line'], ensure_ascii=False)}")
        if finding.get("value") not in (None, ""):
            details.append(f"rejected={json.dumps(finding['value'], ensure_ascii=False)}")
        expected = finding.get("expected")
        if expected:
            details.append(f"expected={expected}")
        rendered.append(f"{message} ({'; '.join(details)})" if details
                        else message)
    return " ".join(rendered)

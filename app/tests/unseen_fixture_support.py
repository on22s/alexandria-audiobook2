"""Controlled producer fixture with the real completion-evidence schema."""
COMPLETION_FIXTURE_CODE = '''
def save_complete_fixture(output_path, input_path, entries):
    import json
    from pathlib import Path
    from generation_completion import get_file_sha256
    output = Path(output_path)
    output.write_text(json.dumps(entries))
    source_hash = get_file_sha256(input_path)
    manifest = {'status':'complete', 'total_chunks':1, 'accepted_chunk_count':1,
        'fingerprint':{'chunk_sha256':[source_hash]},
        'chunks':[{'chunk_number':1,'source_sha256':source_hash}],
        'completion_artifact':{'version':1,'input_sha256':source_hash,
                               'output_sha256':get_file_sha256(output)}}
    Path(str(output)+'.generation_quality.json').write_text(json.dumps(manifest))
'''

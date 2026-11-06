"""Controlled producer fixture with the real completion-evidence schema."""
from pathlib import Path

# Actual top-level import closure of the completion-verification path
# (generation_completion -> script_selection_binding -> docx_publication
# / project_scope_files -> ... -> utils). Synthetic checkouts need the
# REAL modules, not a reopened copy of the two leaf files: generation
# completion now re-verifies input identity through the confined
# script_selection_binding digest seam, so the sliced copies must carry
# the whole closure (flat modules under app/ or the repo root).
COMPLETION_DEPENDENCY_MODULES = (
    'generation_completion.py', 'source_encoding.py',
    'script_selection_binding.py', 'docx_publication.py',
    'project_scope_files.py', 'docx_preview.py',
    'source_upload_publication.py', 'source_upload_stage.py',
    'publication_source.py', 'adapter_publication.py',
    'audio_take_history.py', 'audio_source_io.py',
    'direct_path_admission.py', 'utils.py',
    'gpu_stats.py', 'loaded_module_code.py', 'audio_generation_owner.py',
    'audio_validation.py', 'take_metadata_pool.py')


def copy_completion_dependencies(app_dir, repo_root):
    """Copy the actual completion-verification modules into a synthetic
    checkout's app dir (fail loudly if any required module is missing)."""
    app_dir, repo_root = Path(app_dir), Path(repo_root)
    for name in COMPLETION_DEPENDENCY_MODULES:
        for base in (repo_root / 'app', repo_root):
            source = base / name
            if source.exists():
                break
        else:
            raise AssertionError(f'required completion dependency missing: {name}')
        (app_dir / name).write_bytes(source.read_bytes())
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

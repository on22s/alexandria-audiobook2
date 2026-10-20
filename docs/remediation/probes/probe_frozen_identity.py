"""Pure temporary-file provenance characterization; no model execution."""
import ast,hashlib,importlib.util,json,subprocess,tempfile
from pathlib import Path
from types import SimpleNamespace
import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[3])
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
repo = args.repo.resolve()
source=repo/'ab_test_runtime/evidence/thunder_results_20260930/verify_identity.py'
raw=source.read_bytes();tree=ast.parse(raw)
main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
sha=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_sha256')
assignments=[n for n in main.body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ('model_root','model_hashes')]
assert len(assignments)==2
code=compile(ast.Module(body=[sha,*assignments],type_ignores=[]),str(source),'exec')
helper_path=repo/'app/experiments/_ecapa_batch.py';spec=importlib.util.spec_from_file_location('archival_ecapa_helper',helper_path);helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
with tempfile.TemporaryDirectory(prefix='aa2-frozen-ecapa-path-') as directory:
 root=Path(directory);current=Path(helper.get_ecapa_model_dir(root));current.mkdir(parents=True)
 legacy=root/'ab_test_runtime/ecapa';legacy.mkdir(parents=True)
 (current/'embedding_model.ckpt').write_bytes(b'current model bytes')
 (legacy/'embedding_model.ckpt').write_bytes(b'different historical model bytes')
 namespace={'Path':Path,'hashlib':hashlib,'args':SimpleNamespace(app=root/'app')};exec(code,namespace)
 current_hash=hashlib.sha256((current/'embedding_model.ckpt').read_bytes()).hexdigest();historical_hash=namespace['model_hashes']['embedding_model.ckpt']
 assert current_hash!=historical_hash and namespace['model_root']==legacy
 (legacy/'embedding_model.ckpt').unlink();legacy.rmdir()
 try:exec(code,namespace)
 except FileNotFoundError:missing_refused=True
 else:raise AssertionError('Expected the frozen consumer to require its original legacy directory')
record={'source_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),'frozen_source':str(source.relative_to(repo)),'frozen_source_sha256':hashlib.sha256(raw).hexdigest(),'current_helper':str(helper_path.relative_to(repo)),'current_helper_sha256':hashlib.sha256(helper_path.read_bytes()).hexdigest(),'instrument':'Execute only the exact frozen get_sha256/model_root/model_hashes AST nodes against synthetic temporary files;invoke the actual current helper path function. No scorer/model/voice data executed.','legacy_directory':'ab_test_runtime/ecapa','actual_current_helper_directory':'cache/ecapa','different_model_bytes':True,'frozen_report_hash':historical_hash,'actual_current_model_hash':current_hash,'mismatch_detected':True,'legacy_directory_missing_raises_FileNotFoundError':missing_refused,'frozen_source_unchanged':source.read_bytes()==raw,'disposition':'Preserve original verifier/evidence as a dated runtime snapshot. Any separately named current consumer must bind actual helper-returned model bytes and reject mismatched legacy/current provenance;this characterization does not revive or recertify the historical verifier.'}
assert record['frozen_source_unchanged']
with args.output.open('x') as output:
 output.write(json.dumps(record,indent=2)+'\n')
print(json.dumps(record))

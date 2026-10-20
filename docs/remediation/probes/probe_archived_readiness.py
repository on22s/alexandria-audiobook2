"""CPU-only characterization of the archived E03 tail; never starts a server."""
import hashlib,json,os,subprocess,tempfile
from pathlib import Path
import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[3])
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
root = args.repo.resolve()
path=root/'ab_test_runtime/experiments/three_pass_grimgar25_20260815/run.sh'
source=path.read_text();tail=source[source.index('for unused in $(seq 1 60);'):]
rows=[]
for app_present in [True,False]:
 with tempfile.TemporaryDirectory(prefix='aa2-archived-probe-') as directory:
  fixture=Path(directory);binary=fixture/'bin';binary.mkdir()
  for name,body in [('curl','exit 1'),('sleep','exit 0')]:
   f=binary/name;f.write_text('#!/bin/sh\n'+body+'\n');f.chmod(0o755)
  interpreter=fixture/('app/env/bin/python' if app_present else 'env/bin/python')
  interpreter.parent.mkdir(parents=True)
  interpreter.write_text('#!/bin/sh\nprintf worker-ran > "$WORKER_MARKER"\nexit 7\n');interpreter.chmod(0o755)
  prefix='set -uo pipefail\nrepo="$FIXTURE_ROOT"\nrun_dir="$FIXTURE_ROOT"\n'
  script=fixture/'archived-tail.sh';script.write_text(prefix+tail)
  marker=fixture/'worker-marker'
  result=subprocess.run(['bash',str(script)],cwd=fixture,env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],'FIXTURE_ROOT':str(fixture),'WORKER_MARKER':str(marker)},capture_output=True,text=True,timeout=5)
  assert marker.read_text()=='worker-ran'
  assert result.returncode==7,result.stdout+result.stderr
  if not app_present:assert 'cd:' in result.stderr
  rows.append({'app_directory_present':app_present,'readiness_probe_results':'all60 fail (no HTTP calls);sleep compressed to zero for CPU fixture','worker_invoked_despite_no_readiness':True,'cd_failure_still_dispatches_relative_worker':not app_present,'worker_nonzero_exit_propagated':True,'exit_code':result.returncode})
record={'source_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),'source_path':str(path.relative_to(root)),'source_sha256':hashlib.sha256(source.encode()).hexdigest(),'instrument':'Original readiness-loop/cd/timeout tail only;private curl/sleep/worker fixtures;native Bash and timeout;no original server-start code executed','observations':rows,'disposition':'Archive original driver unchanged;old missing-readiness/cd boundary characterized. A blanket claim that its worker nonzero exit becomes success is refuted. Any fresh runner needs supervised admission/readiness/cwd/child ownership and fresh evidence.'}
with args.output.open('x') as output:
 output.write(json.dumps(record,indent=2)+'\n')
print(json.dumps(record))

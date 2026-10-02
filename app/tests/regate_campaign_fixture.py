"""Private CPU providers for the real provenance-chain campaign protocol."""
from pathlib import Path
import json
import os

from tests.test_identity_gate_completion import make_inputs, measured_document, gate


ROOT = Path(__file__).resolve().parents[2]


def copy_campaign_cli(root):
    path = root / 'app/gate_campaign.py'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('import sys\nsys.path.insert(0, ' + repr(str(ROOT / 'app')) + ')\n' +
                    'from experiments import verify_adapter_identity as fixture_gate\nfixture_gate.REPO=' + repr(str(root)) + '\n' +
                    (ROOT / 'app/gate_campaign.py').read_text())


def write_campaign_inputs(root, name, score=0.9):
    adapter, dataset = make_inputs(root / 'models' / name)
    doc = measured_document(adapter, dataset, [score] * 6)
    doc['measurement']['requested_lines'] = 6
    doc['measurement']['inputs'] = gate.get_identity_gate_inputs(adapter, dataset, 6)
    doc['adapter'] = os.path.relpath(adapter, root)
    doc['provenance'] = {'fixture': True}
    path = root / f'ab_test_runtime/experiments/gate_promote__{name}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    # Legacy absolute adapter paths are consumed by the queue builder.
    previous = {'adapter': str(adapter), 'passed': doc['passed'], 'median_ecapa': doc['median_ecapa']}
    path.write_text(json.dumps(previous))
    (adapter.parent / 'known_gate.json').write_text(json.dumps(doc))
    return path, previous


def write_campaign_worker(root, prefix=''):
    path = root / 'app/experiments/verify_adapter_identity.py'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('''import json,os,sys,signal,time
from pathlib import Path
out=Path(sys.argv[sys.argv.index('--out')+1])
adapter=Path(sys.argv[sys.argv.index('--adapter')+1])
name=adapter.parent.name
''' + prefix + '''
doc=json.loads((adapter.parent/'known_gate.json').read_text())
out.write_text(json.dumps(doc))
sys.exit(0 if doc['passed'] else 3)
''')

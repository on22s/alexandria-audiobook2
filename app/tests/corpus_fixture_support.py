"""Controlled CPU worker artifacts for the actual corpus CLI tests."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def copy_report_driver(root):
    for name in ('corpus_run_report.py', 'alexandria_run_manifest.py', 'alexandria_file_lock.py'):
        (root / name).write_bytes((ROOT / name).read_bytes())


WORKER = '''import json, os, sys
from pathlib import Path
from alexandria_run_manifest import get_file_identity, write_json_atomic
args=sys.argv[1:]
root=Path(__file__).parent
def value(name):return args[args.index(name)+1]
audio,source=Path(value('--audio')),Path(value('--source'))
output,summary=Path(value('--output')),Path(value('--summary-output'))
with (root/'dispatches.jsonl').open('a') as f:f.write(json.dumps(args)+'\\n')
mode=os.environ.get('CORPUS_FIXTURE_MODE','valid')
failed=audio.stem in os.environ.get('FAILED_PAIRS','').split(',')
if failed:sys.exit(7)
if mode=='missing' and audio.suffix=='.mp3':sys.exit(0)
options={'summary_output':str(summary),'output':str(output),'model':value('--model'),
         'fallback_model':value('--fallback-model'),'chunk_size':float(value('--chunk-size')),'lang':value('--lang')}
identity={'audio':get_file_identity(audio),'source':get_file_identity(source),'options':options}
document={'version':1,'phase':'annotation_complete','counter_scope':'current_attempt',
          'identity':identity,'totals':{'segments_total':3,'segments_this_run':2,'resumed_segments':1,'dataset_seconds':12.0},
          'counters':{'llm_success':2,'llm_fail':0,'sanitize_changed':1,'realign_events':2,'reanchor_events':1,
                      'cut_strategy':{'sentence_end':2},'source_action':{'replace':1,'dropped':1}},
          'source_cursor':{'word':5,'total_words':10}}
if audio.suffix=='.mp3':
    if mode=='stale':identity['audio']['sha256']='0'*64
    if mode=='wrong-settings':options['summary_output']='another run'
    if mode=='nan':document['totals']['dataset_seconds']=float('nan')
    if mode=='invalid':document['counters']['realign_events']=True
    if mode=='bad-resume':document['totals']['resumed_segments']=2
    if mode=='bad-version':document['version']=True
write_json_atomic(document,summary)
if mode=='export-failed' and audio.suffix=='.mp3':sys.exit(8)
output.write_bytes(b'dataset from '+audio.name.encode())
'''

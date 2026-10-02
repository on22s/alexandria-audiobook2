"""Replay frozen development predictions and paired Japanese duration checks.

Raw inputs remain private. Acoustic, accent and ECAPA measurements require their
original scorers; this replay does not independently repeat those instruments.
"""
import json,tarfile,hashlib,statistics,collections,sys,os
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.stats import binomtest
os.umask(0o077)
import argparse
parser=argparse.ArgumentParser(description='Verify private A100 development and Japanese duration evidence.')
parser.add_argument('--repo',required=True)
parser.add_argument('--root',required=True,help='Private thunder_followup_20260930 input directory')
args=parser.parse_args()
r=Path(args.root);sys.path.insert(0,str(Path(args.repo)/'app'))
from experiments.scoring import alias_groups,same_speaker
from experiments.manifest import strict_shared_summary,validate_stored_summary
from experiments.lora_serving_eval import SPECIAL,make_windows,norm
from three_pass_generate import get_deterministic_named_entry
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
a=r/'a100_development/pulled'
with tarfile.open(a/'inputs_private.tar') as t:t.extractall(a,filter='data')
d=json.loads((a/'result_private.json').read_text());plan=json.loads((a.parent/'queue_plan.json').read_text())
checked=0
for path,digest in plan['hashes'].items():
 if '/thunder_a100_development_20260930/' not in path:continue
 relative=path.split('/thunder_a100_development_20260930/',1)[1]
 if relative.startswith('data/'):
  assert sha(a/relative)==digest;checked+=1
assert checked==27
for expected,actual,aliases,answer in [('MR. SMITH','MR SMITH',[],True),('SMITH','JONES',[],False),('SMITH',None,[],False),('李明','李明',[],True),('李明','张伟',[],False),('SMITH','BOB',[{'SMITH','BOB'}],True)]:assert same_speaker(expected,actual,aliases)==answer
assert d['meta']['validation']=='ok' and d['meta']['finished'] and not d['meta']['git']['dirty']
golds={};ids=set()
for book,digest in d['meta']['gold_files'].items():
 p=a/'data/fixtures'/f'attribution_gold_{book}.json';assert sha(p)==digest;gold=json.loads(p.read_text());golds[book]=gold
 seg=json.loads((a/'data/checkpoints'/f'{book}__three_pass.json.threepass_checkpoint.json').read_text())['segmented'];occ=collections.Counter(norm(x.get('text')) for x in seg)
 want={norm(x['line']):x for x in gold['entries'] if occ[norm(x['line'])]==1 and x['expected_speaker'].upper() not in SPECIAL}
 windows=[w for w in make_windows(len(seg),25) if any(norm(seg[i].get('text')) in want for i in w)]
 if len(windows)>40:windows=[windows[int(k*len(windows)/40)] for k in range(40)]
 for w in windows:
  for i in w:
   if get_deterministic_named_entry(seg[i]) is None and norm(seg[i].get('text')) in want:ids.add(book+':'+want[norm(seg[i].get('text'))]['id'])
by={arm:{} for arm in ['base','lora']}
for row in d['rows']:
 book,gid=row['id'].split(':',1);gold=golds[book];g=next(x for x in gold['entries'] if x['id']==gid)
 assert row['line']==g['line'] and row['expected']==g['expected_speaker'].upper()
 assert row['correct']==same_speaker(g['expected_speaker'],row['predicted'],alias_groups(gold))
 assert row['id'] not in by[row['arm']];by[row['arm']][row['id']]=row
assert len(golds)==9 and len(ids)==2655 and all(set(x)==ids for x in by.values())
assert not validate_stored_summary(d)
actual=strict_shared_summary(d['rows']);stored=json.loads(json.dumps(d['strict']))
import math
assert math.isclose(actual['paired'].pop('p'),stored['paired'].pop('p'),rel_tol=1e-12)
assert actual==stored
def summarize(selected):
 arms={arm:{'n':len(selected),'correct':sum(rows[i]['correct'] for i in selected),'unanswered':sum(not rows[i]['predicted'] for i in selected)} for arm,rows in by.items()}
 for v in arms.values():v['accuracy']=v['correct']/v['n']
 improved=sum(not by['base'][i]['correct'] and by['lora'][i]['correct'] for i in selected);regressed=sum(by['base'][i]['correct'] and not by['lora'][i]['correct'] for i in selected)
 return {'arms':arms,'delta_pp':100*(arms['lora']['accuracy']-arms['base']['accuracy']),'improved':improved,'regressed':regressed}
summary={'status':'verified','source_sha256':sha(a/'result_private.json'),'controls_passed':6,'rows':5310,'sampled_ids_per_arm':2655,'overall':summarize(ids),'strict':summarize([i for i in ids if all(by[k][i]['predicted'] for k in by)]),'books':{book:summarize([i for i in ids if i.startswith(book+':')]) for book in golds},'limits':['Development/training books, not held-out.','Saved predictions recomputed; raw responses unavailable so parsing cannot be independently checked.']}
(a/'verified_score_summary.json').write_text(json.dumps(summary,indent=2)+'\n');print('A100',json.dumps(summary['overall']))
j=r/'japanese_scored_20260930'
with tarfile.open(j/'wavs_private.tar') as t:t.extractall(j,filter='data')
d=json.loads((j/'result_private.json').read_text());assert d['status']=='complete' and len(d['cases'])==180
for c in d['cases']:
 p=j/'wavs'/Path(c['audio']['path']).name;assert sha(p)==c['audio']['sha256'];y,sr=sf.read(p);assert sr==24000 and np.isfinite(y).all() and np.any(y);assert abs(len(y)/sr-c['audio']['seconds'])<1/sr;assert abs((len(y)/sr)/c['human_seconds']-c['duration_ratio'])<1e-8;assert c['status']=='complete' and c['duration_ratio']<=3
summary={'status':'duration_scoring_verified','verified_audio':180,'duration_rejected':0,'source_sha256':sha(j/'result_private.json'),'seeds':{},'limits':['Duration only; pronunciation, speaker identity and boundary listening not scored.','Thirty fresh pairs, two fixed seeds; results do not establish population generalization.']}
for seed in [1234,1235]:
 rows=[c for c in d['cases'] if c['seed']==seed];pairs=[]
 for pair in range(30):
  rr={c['arm']:c for c in rows if c['pair']==pair};assert set(rr)=={'left','right','grouped'}
  h=rr['left']['human_seconds']+rr['right']['human_seconds'];assert abs(h-rr['grouped']['human_seconds'])<1e-9
  split=(rr['left']['audio']['seconds']+rr['right']['audio']['seconds'])/h;group=rr['grouped']['duration_ratio'];pairs.append((abs(split-1),abs(group-1),split,group))
 wins=sum(g<s for s,g,_,_ in pairs);loss=sum(g>s for s,g,_,_ in pairs)
 summary['seeds'][str(seed)]={'paired_cases':30,'grouped_closer':wins,'split_closer':loss,'ties':30-wins-loss,'sign_test_two_sided_p':float(binomtest(wins,wins+loss,.5).pvalue) if wins+loss else 1.,'median_split_ratio':statistics.median(x[2] for x in pairs),'median_grouped_ratio':statistics.median(x[3] for x in pairs),'median_split_absolute_error':statistics.median(x[0] for x in pairs),'median_grouped_absolute_error':statistics.median(x[1] for x in pairs)}
(j/'duration_score_summary.json').write_text(json.dumps(summary,indent=2)+'\n');print('JAPANESE',json.dumps(summary['seeds']))

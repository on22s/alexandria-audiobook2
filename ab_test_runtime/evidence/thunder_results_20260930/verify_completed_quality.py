import argparse,json,hashlib,statistics,os,math
from pathlib import Path
os.umask(0o077)
parser=argparse.ArgumentParser(description='Verify downloaded score artifacts and replay aggregate calculations without GPU inference.')
parser.add_argument('--chinese-root',type=Path,required=True)
parser.add_argument('--japanese-root',type=Path,required=True)
parser.add_argument('--out',type=Path,required=True)
args=parser.parse_args()
out={'status':'complete','scope':'Replay of stored scores; no new GPU inference or human ratings'}
roots=[args.chinese_root,args.japanese_root]
for root in roots:
 for name,h in json.loads((root/'local_manifest.json').read_text()).items():
  assert hashlib.sha256((root/name).read_bytes()).hexdigest()==h,name
zh=json.loads((roots[0]/'quality_private.json').read_text());ja=json.loads((roots[1]/'asr_large_private.json').read_text())
assert zh['status']==ja['status']=='complete';assert len(zh['rows'])==300 and len(zh['humans'])==150;assert len(ja['rows'])==180 and len(ja['humans'])==90
assert hashlib.sha256((roots[0]/'score_chinese_v2.py').read_bytes()).hexdigest()==zh['scorer_sha256'];assert hashlib.sha256((roots[1]/'score_controls.py').read_bytes()).hexdigest()==ja['scorer_sha256']
for d in [zh,ja]:
 assert all(math.isfinite(r['cer']) and r['cer']>=0 for r in d['rows'])
zsummary=json.loads((roots[0]/'quality_summary.json').read_text());jsummary=json.loads((roots[1]/'asr_large_full_summary.json').read_text())
z={(r['id'],r['arm']):r for r in zh['rows']};assert len(z)==300
ids=sorted({r['id'] for r in zh['rows']});valid=[i for i in ids if z[i,'long_typical']['duration_status']==z[i,'short_original']['duration_status']=='complete']
# The persisted summary names the common duration-valid subset explicitly.
assert len(valid)==zsummary['common_duration_valid_lines']==101
common={}
for arm in ['long_typical','short_original']:
 rows=[z[i,arm] for i in valid];common[arm]={'n':len(rows),'cer_mean':statistics.mean(r['cer'] for r in rows),'ecapa_median':statistics.median(r['ecapa_human'] for r in rows)}
out['chinese']={'generated':300,'human_controls':150,'metric_controls':zh['controls'],'arms':{a:{'duration_failures':v['duration_rejected'],'n':v['attempted'],'cer_mean_duration_valid':v['cer_mean_duration_valid'],'ecapa_median_all':v['ecapa_median_all']} for a,v in zsummary['arms'].items()},'human_cer_mean':zsummary['human_cer_mean'],'common_valid':common,'common_line_acoustic_comparison':zsummary['common_line_acoustic_comparison'],'short_lower_cer_common_pairs':sum(z[i,'short_original']['cer']<z[i,'long_typical']['cer'] for i in valid),'limits':zsummary['limits']}
for a,v in out['chinese']['arms'].items():
 rr=[r for r in zh['rows'] if r['arm']==a];assert len(rr)==v['n'];assert math.isclose(statistics.median(r['ecapa_human'] for r in rr),v['ecapa_median_all'])
j={(r['pair'],r['seed'],r['arm']):r for r in ja['rows']};assert len(j)==180
out['japanese']={'generated':180,'human_controls':90,'metric_controls':ja['controls'],'arms_by_seed':jsummary['arms_by_seed'],'limits':jsummary['limits']}
for seed in [1234,1235]:
 for arm in ['left','right','grouped']:
  rows=[j[p,seed,arm] for p in range(30)];v=jsummary['arms_by_seed'][f'{seed}:{arm}'];assert math.isclose(statistics.mean(r['cer'] for r in rows),v['cer_mean'])
out['provenance']={'chinese_result_sha256':hashlib.sha256((roots[0]/'quality_private.json').read_bytes()).hexdigest(),'japanese_result_sha256':hashlib.sha256((roots[1]/'asr_large_private.json').read_bytes()).hexdigest(),'chinese_scorer_sha256':zh['scorer_sha256'],'japanese_scorer_sha256':ja['scorer_sha256'],'japanese_model_revision':ja['model'].get('revision'),'chinese_asr_revision':zh['asr_revision']}
out['verified_manifest_files']=sum(len(json.loads((r/'local_manifest.json').read_text())) for r in roots)
p=args.out;p.write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))

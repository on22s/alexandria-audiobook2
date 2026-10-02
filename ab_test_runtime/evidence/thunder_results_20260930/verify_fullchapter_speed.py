from pathlib import Path
import argparse,hashlib,json,math,os,statistics,sys
import numpy as np
import soundfile as sf
parser=argparse.ArgumentParser(description='Verify private full-chapter WAVs and rescore matched saved ASR hypotheses; output aggregates only.')
parser.add_argument('--raw-root',type=Path,required=True,help='Folder containing result.json, quality_summary.json, asr_private.json and wavs/')
parser.add_argument('--baseline-asr',type=Path,required=True)
parser.add_argument('--baseline-summary',type=Path,required=True)
parser.add_argument('--app',type=Path,required=True)
parser.add_argument('--out',type=Path,required=True)
args=parser.parse_args();p=args.raw_root
sys.path.insert(0,str(args.app))
from experiments.asr_backends import word_error_rate
from experiments.thunder_audio_campaign import get_audio_record
sha=lambda f:hashlib.sha256(Path(f).read_bytes()).hexdigest()
q=json.loads((p/'quality_summary.json').read_text());d=json.loads((p/'result.json').read_text());a=json.loads((p/'asr_private.json').read_text())
assert q['status']==d['status']==a['status']=='complete'
assert d['completed']==d['requested']==768 and q['source_hashes']['chapter_result']==sha(p/'result.json')
assert q['source_hashes']==a['source_hashes']
old=json.loads(args.baseline_asr.read_text())
oldq=json.loads(args.baseline_summary.read_text())
assert old['source_hashes']==oldq['source_hashes']
for ref,hyp,expected in [('one two','one two',0),('one two','',1),('one two','three four',1),('one','one two three',2)]:assert word_error_rate(ref,hyp)==expected
assert word_error_rate('...','unexpected words') is None
clips={c['index']:c for b in d['batches'] for c in b['clips']};assert len(clips)==768
paths=set();duration=0.;maxpeak=0.;clipping=0
for c in clips.values():
 remote=Path(c['path']);f=p/remote.relative_to(remote.parents[2]);paths.add(f)
 record=get_audio_record(f);assert record['sha256']==c['sha256'] and math.isclose(record['seconds'],c['seconds'],abs_tol=1e-9)
 x,rate=sf.read(f,dtype='float32',always_2d=True);assert rate==24000 and x.shape[1]==1
 maxpeak=max(maxpeak,float(np.abs(x).max()));clipping+=int(np.any(np.abs(x)>=.9999));duration+=record['seconds']
assert set((p/'wavs').rglob('*.wav'))==paths
assert math.isclose(duration,d['audio_s'],abs_tol=1e-7)
newrows={x['index']:x for x in a['rows']};oldrows={x['index']:x for x in old['rows'] if x['task']=='chapter'}
assert set(newrows)==set(oldrows)==set(clips)
newwer=[];oldwer=[];diff=[];excluded=0
for i,row in newrows.items():
 prior=oldrows[i];assert row['text']==prior['text']==clips[i]['text'];assert row['sha256']==clips[i]['sha256']
 for x in [row,prior]:
  w=word_error_rate(x['text'],x['hypothesis'] or '')
  assert (w is None and x['wer'] is None) or math.isclose(w,x['wer'],abs_tol=1e-12)
 if row['wer'] is None:assert prior['wer'] is None;excluded+=1;continue
 newwer.append(row['wer']);oldwer.append(prior['wer']);diff.append(row['wer']-prior['wer'])
assert len(diff)==764 and excluded==4
assert math.isclose(statistics.mean(newwer),q['summary']['chapter']['asr']['mean_clip_wer'],abs_tol=1e-12)
assert math.isclose(statistics.mean(oldwer),oldq['summary']['chapter']['asr']['mean_clip_wer'],abs_tol=1e-12)
rng=np.random.default_rng(20260930);v=np.array(diff);draws=np.mean(rng.choice(v,size=(10000,len(v)),replace=True),axis=1);ci=np.quantile(draws,[.025,.975])
oldwall=oldq['summary']['chapter']['wall_s'];newwall=d['wall_s']
out={'status':'complete','waveforms_verified':768,'clipping_clips':clipping,'max_peak_abs':maxpeak,'scorer_controls':'identity, deletion, substitutions, insertion, punctuation exclusions passed','asr_scored_per_arm':764,'excluded_punctuation_only':4,'baseline_workers':4,'new_workers':16,'baseline_generation_minutes':oldwall/60,'new_generation_minutes':newwall/60,'generation_wall_reduction_percent':(1-newwall/oldwall)*100,'generation_speedup':oldwall/newwall,'new_audio_minutes':duration/60,'new_generation_over_audio':newwall/duration,'baseline_mean_clip_wer':statistics.mean(oldwer),'new_mean_clip_wer':statistics.mean(newwer),'paired_wer_change_percentage_points':statistics.mean(diff)*100,'paired_clip_bootstrap_change_95ci_points':[float(x)*100 for x in ci],'clips_wer_improved':sum(x<0 for x in diff),'clips_wer_worsened':sum(x>0 for x in diff),'clips_wer_unchanged':sum(x==0 for x in diff),'clips_over_20percent_wer_baseline':sum(x>.2 for x in oldwer),'clips_over_20percent_wer_new':sum(x>.2 for x in newwer),'result_sha256':sha(p/'result.json'),'private_asr_sha256':sha(p/'asr_private.json'),'local_verifier_sha256':sha(__file__),'note':'One chapter, matched clip comparison. Clip bootstrap is descriptive within this chapter, not independent-book generalization. Recomputed saved ASR hypotheses; not a rerun of the recognizer or listening/voice-identity assessment. No production settings changed.'}
args.out.write_text(json.dumps(out,indent=2)+'\n');os.chmod(args.out,0o600)
print(json.dumps(out,indent=2))

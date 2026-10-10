"""Compare the same saved synthetic voices across dialogue contexts, privately."""
import argparse
import html
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
from audio_validation import validate_generated_audio
from experiments.generation import render
from experiments.provenance import file_sha256, provenance
from utils import atomic_json_write

NEUTRAL = 'The red box is beside the blue box. I can see both of them. Please put the small one on the table, and leave the other one by the door.'
LONG = ('We went outside after breakfast and followed the path through the garden. '
        'A small bird was sitting on the gate. It flew away when we came closer. '
        'I stopped to look at the flowers while my friend walked ahead. '
        'Then we heard a noise near the fence. It was only a ball rolling across the grass. '
        'We picked it up and carried it back to the house. Someone had left the door open. '
        'I put the ball beside the shoes and went to find a glass of water. '
        'Before we went outside again, I checked that everyone was ready. This time we remembered to close the door.')


def get_passages(age):
    """Return controlled synthetic passages; target age is not a measured voice age."""
    appropriate = ('Can you help me find my little bear? I put him on the bed, but now he is gone. '
                   'Oh, there he is! He fell behind my pillow. I want to give him a hug.') if age <= 5 else (
                   'I finished my homework before dinner, so I can play for a little while. '
                   'We made up a new game at school today. I want to show you the rules tomorrow.')
    return [('neutral', NEUTRAL), ('age_appropriate', appropriate), ('long', LONG)]


def get_candidates(manifest):
    """Validate reference identities and paths before any model loading."""
    manifest = Path(manifest).resolve()
    data = json.loads(manifest.read_text(encoding='utf-8'))
    rows = data.get('candidates')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Candidate manifest must contain a nonempty candidates list')
    seen, result = set(), []
    reference_text = data.get('reference_text')
    if not isinstance(reference_text, str) or not reference_text.strip():
        raise ValueError('Reference transcript is required')
    for row in rows:
        identity, age = row.get('id'), row.get('age_target')
        if (not isinstance(identity, str) or not identity or identity in seen
                or not isinstance(age, int) or isinstance(age, bool) or not 3 <= age <= 11):
            raise ValueError('Each candidate needs a unique id and requested age 3–11')
        reference = row['clips'][0]
        path = (manifest.parent / reference['path']).resolve()
        if not path.is_relative_to(manifest.parent) or file_sha256(path) != reference['sha256']:
            raise ValueError('Reference path or hash mismatch')
        validate_generated_audio(path, 'saved reference')
        seen.add(identity)
        result.append({'id': identity, 'age_target': age, 'gender_target': row.get('gender_target'),
                       'seed': row['seed'], 'reference': path, 'reference_text': reference_text})
    return result


def write_listening_page(out, rows):
    """Create local listening controls; no network, model calls or catalog writes."""
    from speaker_traits import AGE_GROUPS
    options = '<option value="">Choose…</option>' + ''.join(
        f'<option value="{html.escape(key)}">{html.escape(key.replace("_", " "))} ({html.escape(years or "unsure")})</option>'
        for key, years in AGE_GROUPS)
    scores = '<option value="">Choose…</option>' + ''.join(f'<option>{n}</option>' for n in range(1, 6))
    evidence_sha256 = hashlib.sha256(json.dumps(rows, sort_keys=True).encode('utf-8')).hexdigest()
    cards = []
    import random
    ordered = list(rows)
    random.Random(733).shuffle(ordered)
    for number, row in enumerate(ordered, 1):
        players = ''.join(f'<p>{html.escape(clip["condition"].replace("_", " "))}</p><audio controls preload="metadata" src="{html.escape(clip["path"], quote=True)}"></audio>' for clip in row['clips'])
        ages = ''.join(f'<label>Age heard: {arm.replace("_", " ")}<select data-field="age_{arm}">{options}</select></label>' for arm in ('neutral', 'age_appropriate', 'long'))
        quality = ''.join(f'<label>{key.title()} (1–5)<select data-field="{key}">{scores}</select></label>' for key in ('naturalness', 'consistency', 'clarity'))
        cards.append(f'<article data-id="{html.escape(row["id"],quote=True)}"><h2>Voice {number:02}</h2>{players}{ages}{quality}<label>Notes<textarea data-field="notes"></textarea></label><details><summary>Requested target</summary>{html.escape(str(row["gender_target"]))}, requested age {row["age_target"]}</details></article>')
    page = '''<!doctype html><meta charset="utf-8"><title>Child voice context retest</title>
<style>body{font:17px system-ui;max-width:1000px;margin:30px auto;padding:15px;background:#f4f6fb}article{background:white;padding:20px;margin:20px 0;border-radius:12px}label{display:inline-block;margin:12px}select,textarea{display:block}audio{width:100%}header{position:sticky;top:0;background:#f4f6fb;padding:12px}</style>
<header><button id="download">Download ratings</button> <span id="status"></span></header>
<h1>Child voice context retest</h1><p>The same synthetic references are reused for neutral, age-appropriate and longer unseen passages. Rate age separately for each passage. Wording can bias age judgments; this test does not establish an exact physical age. No voice will be automatically relabeled or published.</p>''' + ''.join(cards) + '''
<script>
const evidenceSha256='EVIDENCE_SHA256';
const key='aa2-child-context-retest-'+evidenceSha256, saved=JSON.parse(localStorage.getItem(key)||'{}');
const cards=[...document.querySelectorAll('article')];
function getRatings(){return cards.map(card=>({id:card.dataset.id,...Object.fromEntries([...card.querySelectorAll('[data-field]')].map(field=>[field.dataset.field,field.value]))}));}
function saveRatings(){localStorage.setItem(key,JSON.stringify(Object.fromEntries(getRatings().map(row=>[row.id,row]))));const done=getRatings().filter(row=>Object.entries(row).every(([name,value])=>name==='notes'||Boolean(value))).length;document.getElementById('status').textContent=done+'/'+cards.length+' rated · saved locally';}
for(const card of cards){for(const field of card.querySelectorAll('[data-field]')){field.value=saved[card.dataset.id]?.[field.dataset.field]||'';field.addEventListener('change',saveRatings);}}
saveRatings();
document.getElementById('download').onclick=()=>{const ratings=getRatings();if(ratings.some(row=>Object.entries(row).some(([name,value])=>name!=='notes'&&!value))){document.getElementById('status').textContent='Complete all age and quality ratings first.';return;}const payload={schema:1,experiment:'aa2-child-voice-context-retest',evidence_sha256:evidenceSha256,completed_at:new Date().toISOString(),ratings};const link=document.createElement('a');link.href=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}));link.download='child_voice_context_ratings_private.json';link.click();URL.revokeObjectURL(link.href);document.getElementById('status').textContent='Ratings downloaded.';};
</script>'''
    (out / 'listen.html').write_text(page.replace('EVIDENCE_SHA256', evidence_sha256), encoding='utf-8')


def run_retest(engine, candidates, out, source):
    """Render new clips through production clone dispatch, checkpointing failures."""
    rows = []
    for number, candidate in enumerate(candidates):
        reference = out / f'candidate_{number:02}_reference.wav'
        shutil.copyfile(candidate['reference'], reference)
        voice = {'type':'clone', 'ref_audio':str(reference), 'ref_text':candidate['reference_text'], 'seed':str(candidate['seed'])}
        row = {key:candidate[key] for key in ('id','age_target','gender_target','seed')}
        row['clips'] = [{'condition':'reference','path':reference.name,'sha256':file_sha256(reference)}]
        rows.append(row)
        for condition, text in get_passages(candidate['age_target']):
            target = out / f'candidate_{number:02}_{condition}.wav'
            start = time.monotonic()
            try:
                render(engine, text, '', 'CANDIDATE', {'CANDIDATE':voice}, voice, str(target))
                row['clips'].append({'condition':condition,'path':target.name,'sha256':file_sha256(target),
                                     'text':text,'elapsed_seconds':round(time.monotonic()-start,3)})
            except Exception as error:
                row['failure'] = {'condition':condition,'error':str(error)}
                atomic_json_write({'provenance':source,'requested':len(candidates),'candidates':rows,'status':'failed'},str(out/'retest.json'))
                raise
            atomic_json_write({'provenance':source,'requested':len(candidates),'completed_clips':sum(len(r['clips'])-1 for r in rows),
                               'candidates':rows,'status':'running','ratings_pending':True},str(out/'retest.json'))
            print(candidate['id'], condition, row['clips'][-1]['elapsed_seconds'], flush=True)
    atomic_json_write({'provenance':source,'requested':len(candidates),'completed_clips':3*len(rows),
                       'candidates':rows,'status':'complete','ratings_pending':True},str(out/'retest.json'))
    write_listening_page(out, rows)
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--config',required=True)
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    os.umask(0o077)
    out=Path(args.out).resolve()
    if out.is_relative_to(APP.parent) or out.exists():
        parser.error('Use a fresh private output directory outside the repository')
    candidates=get_candidates(args.manifest)
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    if (config.get('tts') or {}).get('mode','local')!='local':
        parser.error('This private retest requires local TTS')
    from experiments.gpu_guard import acquire_gpu_lock, release_gpu_lock
    handle=acquire_gpu_lock()
    try:
        out.mkdir(parents=True,mode=0o700)
        os.environ['ALEXANDRIA_DATA_DIR']=str(out)
        source=provenance(__file__,args)
        source['manifest_sha256']=file_sha256(args.manifest)
        source['config_sha256']=file_sha256(args.config)
        from tts import TTSEngine
        run_retest(TTSEngine(config),candidates,out,source)
    finally:
        release_gpu_lock(handle)


if __name__=='__main__':
    main()

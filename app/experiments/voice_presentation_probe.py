"""Small source-grounded probe before enabling voice-presentation annotation."""
import argparse
import json
from pathlib import Path
import sys
import time

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
from utils import atomic_json_write

PROMPT = '''Identify the TARGET speaker's explicitly described vocal presentation independently of their gender. Return ONLY JSON with voice_presentation (masculine, feminine, or unknown) and evidence (an exact quote from this passage, or empty for unknown). Only descriptions of this speaker's audible voice qualify. Clothing, personality, appearance, pronouns, gender and dialogue content are NOT voice evidence. A low/deep/high/soft tone alone does not establish masculine or feminine presentation. Do not borrow another speaker's voice. A woman’s voice or a man’s voice identifies the speaker, not vocal presentation. Womanish or feminine explicitly modifying voice means feminine presentation; masculine explicitly modifying voice means masculine presentation, regardless of identity. Evidence must quote the narrator’s vocal description, never the content of spoken dialogue. For unknown return an empty evidence string. Do not change the character's gender. /no_think'''


def get_probe_verdict(case, answer):
    if not isinstance(answer, dict):
        return False
    presentation = answer.get('voice_presentation')
    quote = answer.get('evidence')
    if not isinstance(quote, str):
        return False
    normal = lambda value: ' '.join(value.split())
    if presentation == 'unknown':
        return case['presentation'] == 'unknown' and quote == ''
    return (presentation == case['presentation'] and presentation in ('masculine', 'feminine')
            and bool(quote.strip()) and normal(quote) in normal(case['text'])
            and normal(case['evidence_anchor']) in normal(quote))


def main():
    from openai import OpenAI
    from experiments.gpu_guard import require_free_gpu
    from experiments.provenance import provenance, file_sha256
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8193/v1')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    require_free_gpu("voice-presentation probe")
    source = provenance(__file__, args, fixture_sha256=file_sha256(APP/"fixtures/voice_presentation_733.json"))
    cases = json.loads((APP/'fixtures/voice_presentation_733.json').read_text())
    client = OpenAI(base_url=args.url, api_key='local', timeout=120, max_retries=0)
    model = client.models.list().data[0].id
    rows = []
    for repeat in range(2):
        for case in cases:
            started = time.monotonic()
            response = client.chat.completions.create(model=model, temperature=0, max_tokens=512,
                response_format={'type':'json_object'},
                messages=[{'role':'system','content':PROMPT}, {'role':'user','content':
                    f"TARGET: {case['speaker']}\nEstablished identity: {case['gender']}\nPASSAGE:\n{case['text']}"}])
            raw = response.choices[0].message.content
            try:
                answer = json.loads(raw)
            except (ValueError, TypeError):
                answer = None
            passed = get_probe_verdict(case, answer)
            rows.append({'case':case['id'],'repeat':repeat,'passed':passed,'answer':answer,
                         'raw':raw,'elapsed_seconds':round(time.monotonic()-started,3),
                         'finish_reason':response.choices[0].finish_reason})
            atomic_json_write({'model':model,'prompt':PROMPT,'provenance':source,'results':rows}, args.out)
            print(case['id'],repeat,passed,flush=True)
    if not all(row['passed'] for row in rows):
        raise SystemExit('Voice-presentation probe failed; do not enable annotation')


if __name__ == '__main__':
    main()

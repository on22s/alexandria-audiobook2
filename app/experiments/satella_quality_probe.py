"""Render a blinded seed-versus-adapter quality probe for Satella."""
import argparse
import html
import json
import os
import random
import shutil
import string
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

from experiments.provenance import file_sha256, provenance  # noqa: E402

TEXTS = (
    "You’re probably right…but…",
    "How did you do that?",
    "You really look like you’re used to this… Subaru, is your profession taming children?",
    "There’s also that my screw-ups are partially at fault, but the thief who stole from you is getting farther away from us.",
)


def build_arms(shipped_adapter, control_adapter, shipped_seed, alternate_seed):
    """Return three arms that isolate seed and adapter changes."""
    return {
        "shipped_adapter_shipped_seed": {
            "adapter_path": shipped_adapter, "seed": str(shipped_seed)},
        "shipped_adapter_alternate_seed": {
            "adapter_path": shipped_adapter, "seed": str(alternate_seed)},
        "control_adapter_shipped_seed": {
            "adapter_path": control_adapter, "seed": str(shipped_seed)},
    }


def make_html(public):
    payload = json.dumps(public, ensure_ascii=False).replace("</", "<\\/")
    return """<!doctype html><meta charset=\"utf-8\"><title>Satella Quality Test</title>
<style>body{font:16px system-ui;max-width:900px;margin:30px auto;padding:0 16px;background:#faf9f6;color:#222}fieldset{margin:24px 0;padding:18px;border:1px solid #bbb;border-radius:10px}audio{width:100%}.sample{background:white;padding:12px;margin:12px 0;border-radius:8px}.scale{display:flex;gap:14px;flex-wrap:wrap}textarea{width:100%;min-height:55px}button{padding:10px 16px;font-size:16px}</style>
<h1>Satella audio quality test</h1>
<p>Each set says the <strong>same sentence</strong> three ways. The labels are shuffled. Judge sound quality—not whether the voices are different.</p>
<p>For every clip: <strong>1 = badly broken or unpleasant</strong>, <strong>3 = usable but flawed</strong>, <strong>5 = clean and natural</strong>. Then choose the best clip in that set.</p>
<div id=sets></div><button id=save>Download results</button>
<script>const data=""" + payload + """;const root=document.querySelector('#sets');
data.sets.forEach((set,si)=>{const f=document.createElement('fieldset');f.innerHTML=`<legend><b>Set ${si+1}</b></legend><p>${set.text}</p>`;set.samples.forEach(s=>{const d=document.createElement('div');d.className='sample';d.innerHTML=`<b>Clip ${s.label}</b><audio controls preload="metadata" src="${s.file}"></audio><div class="scale">${[1,2,3,4,5].map(n=>`<label><input required type="radio" name="q_${set.id}_${s.label}" value="${n}"> ${n}</label>`).join('')}</div>`;f.appendChild(d)});f.insertAdjacentHTML('beforeend',`<p>Best sounding clip: ${set.samples.map(s=>`<label><input required type="radio" name="best_${set.id}" value="${s.label}"> ${s.label}</label>`).join(' &nbsp; ')}</p><label>What sounded wrong, if anything?<textarea data-note="${set.id}"></textarea></label>`);root.appendChild(f)});
document.querySelector('#save').onclick=()=>{const missing=[...document.querySelectorAll('fieldset')].some(f=>f.querySelectorAll('input:checked').length!==4);if(missing){alert('Please rate all three clips and choose one best clip in every set.');return}const answers={};data.sets.forEach(set=>{const quality={};set.samples.forEach(s=>quality[s.label]=+document.querySelector(`[name="q_${set.id}_${s.label}"]:checked`).value);answers[set.id]={quality,best:document.querySelector(`[name="best_${set.id}"]:checked`).value,notes:document.querySelector(`[data-note="${set.id}"]`).value}});const out={what:'Satella blinded seed-versus-adapter quality ratings',rated_at:new Date().toISOString(),source_sha256:data.source_sha256,answers};const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:'application/json'}));a.download='satella-quality-ratings.json';a.click()};</script>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--shipped-adapter", default="lora_models/husky_alto_40s_f")
    ap.add_argument("--control-adapter", default="lora_models/silky_mezzo_30s_f")
    ap.add_argument("--shipped-seed", type=int, default=1453101771)
    ap.add_argument("--alternate-seed", type=int, default=1425066263)
    ap.add_argument("--shuffle-seed", type=int, default=20260822)
    ap.add_argument("--work", default="ab_test_runtime/satella_quality_probe")
    ap.add_argument("--out", default="ab_test_runtime/experiments/satella_quality_probe.json")
    ap.add_argument("--key", default="ab_test_runtime/satella_quality_probe_concealed_key.json")
    ap.add_argument("--html", default="ab_test_runtime/satella_quality_probe/Satella Quality Test.html")
    args = ap.parse_args()
    for path in (args.shipped_adapter, args.control_adapter):
        if not os.path.isfile(os.path.join(path, "adapter_model.safetensors")):
            raise SystemExit(f"adapter is incomplete: {path}")

    from tts import TTSEngine
    from experiments.generation import render, GenerationFailed
    engine = TTSEngine(json.load(open(os.path.join(APP, "config.json"), encoding="utf-8")))
    arms = build_arms(args.shipped_adapter, args.control_adapter,
                      args.shipped_seed, args.alternate_seed)
    os.makedirs(args.work, exist_ok=True)
    rng = random.Random(args.shuffle_seed)
    public_sets, key_sets = [], []
    for line_index, text in enumerate(TEXTS):
        order = list(arms)
        rng.shuffle(order)
        samples, mapping = [], {}
        for sample_index, arm_name in enumerate(order):
            arm = arms[arm_name]
            entry = {"type": "lora", "adapter_path": arm["adapter_path"],
                     "seed": arm["seed"]}
            raw = os.path.join(args.work, f"raw_{line_index}_{arm_name}.wav")
            try:
                render(engine, text, "", "SATELLA", {"SATELLA": entry}, entry, raw)
            except GenerationFailed as exc:
                raise SystemExit(f"render failed for line {line_index} {arm_name}: {exc}")
            label = string.ascii_uppercase[sample_index]
            blind_name = f"set_{line_index}_{label}.wav"
            blind_path = os.path.join(args.work, blind_name)
            shutil.copy2(raw, blind_path)
            samples.append({"label": label, "file": blind_name,
                            "sha256": file_sha256(blind_path)})
            mapping[label] = {"arm": arm_name, "raw_sha256": file_sha256(raw)}
        public_sets.append({"id": f"set_{line_index}", "text": text,
                            "samples": samples})
        key_sets.append({"id": f"set_{line_index}", "mapping": mapping})

    from utils import atomic_json_write
    key = {"status": "complete", "sets": key_sets,
           "arm_configuration": arms}
    atomic_json_write(key, args.key)
    public = {"status": "complete", "sets": public_sets,
              "concealed_key_sha256": file_sha256(args.key),
              "provenance": provenance(__file__, args)}
    public["source_sha256"] = file_sha256(args.key)
    atomic_json_write(public, args.out)
    with open(args.html, "w", encoding="utf-8") as handle:
        handle.write(make_html(public))
    print(f"rendered {len(TEXTS) * len(arms)} clips")
    print(f"open: {os.path.abspath(args.html)}")


if __name__ == "__main__":
    main()

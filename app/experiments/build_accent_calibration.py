"""Build a blinded Japanese accent/delivery calibration page."""
import argparse
import base64
import json
import os
import random
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def encode(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-c:a", "libopus",
                             "-b:a", "32k", "-f", "ogg", "-"], capture_output=True)
    if result.returncode or not result.stdout:
        raise RuntimeError(f"could not encode {path}")
    return "data:audio/ogg;base64," + base64.b64encode(result.stdout).decode()


def get_questions():
    return [
        ("pronunciation", "Are the words pronounced correctly?", ["yes", "no", "cannot tell"]),
        ("accent", "Does the Japanese pitch accent sound natural?", ["yes", "no", "cannot tell"]),
        ("delivery", "How natural is the overall delivery?", ["1", "2", "3", "4", "5"]),
    ]


def render(package):
    questions = json.dumps(get_questions(), ensure_ascii=False)
    data = json.dumps(package, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html><meta charset=utf-8><title>Japanese Accent Calibration</title>
<style>body{{font:16px system-ui;max-width:900px;margin:30px auto;padding:0 18px;background:#f5f6f8;color:#18202a}}.intro,.card{{background:white;border:1px solid #d8dde5;border-radius:10px;padding:16px;margin:14px 0}}.take{{border-top:1px solid #ddd;padding:12px 0}}label{{display:block;margin:5px 0}}select,button,textarea{{font:inherit}}textarea{{width:100%;height:180px}}small{{color:#566}}</style>
<h1>Japanese Accent Calibration</h1><div class=intro><b>These are three different questions.</b><p><b>Pronunciation</b> asks whether the sounds form the intended words. <b>Pitch accent</b> asks whether Japanese high/low pitch placement is natural. <b>Delivery</b> asks about pacing, whispering, volume, and robotic quality.</p><p>If you do not speak Japanese, choose <b>cannot tell</b> for the first two. You may still rate delivery. Do not infer correctness from audio quality.</p></div><div id=items></div><h2>Results</h2><textarea id=out readonly></textarea><button onclick=navigator.clipboard.writeText(out.value)>Copy results</button>
<script>const data={data},questions={questions},answers={{}};const host=document.getElementById('items');data.items.forEach((item,i)=>{{const card=document.createElement('div');card.className='card';card.innerHTML='<h2>Sentence '+(i+1)+'</h2><p lang=ja>'+item.text+'</p>';item.takes.forEach(t=>{{const div=document.createElement('div');div.className='take';div.innerHTML='<b>Take '+t.letter+'</b> ';const audio=document.createElement('audio');audio.controls=true;audio.src=t.audio;div.appendChild(audio);questions.forEach(q=>{{const label=document.createElement('label');label.textContent=q[1]+' ';const select=document.createElement('select');select.innerHTML='<option value="">choose…</option>'+q[2].map(x=>'<option>'+x+'</option>').join('');select.onchange=()=>{{answers[item.id+':'+t.letter+':'+q[0]]=select.value;out.value=JSON.stringify({{what:'japanese accent calibration',answers}},null,2)}};label.appendChild(select);div.appendChild(label)}});card.appendChild(div)}});host.appendChild(card)}});out.value=JSON.stringify({{what:'japanese accent calibration',answers}},null,2)</script>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated", required=True)
    parser.add_argument("--out-html", required=True)
    parser.add_argument("--out-key", required=True)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260823)
    args = parser.parse_args()
    source = json.load(open(args.generated, encoding="utf-8"))
    rng, items, key = random.Random(args.seed), [], []
    for row in source["rows"][:args.count]:
        arms = [("human", row["human_wav"]), ("clone", row["clone_wav"]),
                ("lora", row["lora_wav"])]
        rng.shuffle(arms)
        takes, mapping = [], {}
        for letter, (arm, path) in zip("ABC", arms):
            takes.append({"letter": letter, "audio": encode(os.path.join(REPO, path))})
            mapping[letter] = arm
        items.append({"id": row["id"], "text": row["text"], "takes": takes})
        key.append({"id": row["id"], "letters": mapping})
    os.makedirs(os.path.dirname(args.out_html), exist_ok=True)
    with open(args.out_html, "w", encoding="utf-8") as handle:
        handle.write(render({"seed": args.seed, "items": items}))
    with open(args.out_key, "w", encoding="utf-8") as handle:
        json.dump({"seed": args.seed, "key": key}, handle, indent=2)


if __name__ == "__main__":
    main()

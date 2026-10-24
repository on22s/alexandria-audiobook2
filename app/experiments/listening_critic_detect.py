"""Binary fault-detection page for the #1547 voice/pause supplement.

The 1-5 scale let a noticed fault be rated 5. This page asks the two detection
questions directly: did a character's voice change, and was a pause clearly
too long or too short. Labels stay concealed.
"""

import argparse
import base64
import hashlib
import json
import random
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

REPEATS = ("clean_01", "voice_02")
MIN_GAP = 3
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Listening detection check</title>
<style>
:root { color-scheme: light dark; }
body { font: 16px/1.5 system-ui, sans-serif; max-width: 920px; margin: 0 auto; padding: 24px; }
.card { border: 1px solid #8884; border-radius: 10px; padding: 20px; margin-top: 16px; }
.script { white-space: pre-wrap; background: #8881; padding: 12px; border-radius: 8px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; margin: 6px 0 12px; }
button.answer { flex: 1 1 150px; padding: 12px; border: 1px solid #8886; border-radius: 8px; background: transparent; cursor: pointer; }
button.answer[aria-pressed="true"] { background: #2563eb22; border-color: #2563eb; }
.question { margin-top: 14px; font-weight: 600; }
input[type=text], textarea { width: 100%; margin-top: 6px; padding: 8px; }
textarea { min-height: 60px; }
.bar { display: flex; gap: 10px; align-items: center; justify-content: space-between; margin-top: 16px; }
.pill { padding: 4px 10px; border: 1px solid #8886; border-radius: 999px; font-size: .85rem; }
.hidden { display: none; }
</style></head><body>
<h1>Listening detection check</h1>
<p>Listen to each scene. Answer the two questions, then move on. Nothing is scored while you listen.</p>
<div class="bar"><span id="progress" class="pill"></span><span id="save" class="muted"></span></div>
<div id="app"></div>
<div class="bar"><button id="back" type="button">Back</button><button id="next" type="button">Next</button></div>
<div id="done" class="card hidden"><h2>Finished</h2><button id="download" type="button">Download responses</button></div>
<script>
const AUDIO = __AUDIO__;
const TRIALS = __TRIALS__;
const SEED = __SEED__;
const STORE = 'listening-detect-1547-' + SEED;
let state = { index: 0, responses: {} };
try { const saved = JSON.parse(localStorage.getItem(STORE) || 'null'); if (saved && typeof saved.index === 'number' && saved.responses && saved.index < TRIALS.length) { state = saved; } } catch (e) {}
function save() { localStorage.setItem(STORE, JSON.stringify(state)); document.getElementById('save').textContent = 'Saved'; }
function response(trial) { if (!state.responses[trial.id]) { state.responses[trial.id] = {voice_changed: null, voice_line: '', pause_issue: null, pause_line: '', notes: ''}; } return state.responses[trial.id]; }
function complete(r) { return r.voice_changed !== null && r.pause_issue !== null; }
function render() {
  const trial = TRIALS[state.index]; const r = response(trial); const app = document.getElementById('app');
  let html = '<div class="card"><div class="muted">Trial ' + (state.index + 1) + ' / ' + TRIALS.length + '</div><h2>' + escapeHtml(trial.title) + '</h2>';
  html += '<div class="script">' + trial.scene.map(line => escapeHtml(line.speaker) + ': ' + escapeHtml(line.text)).join('\\n') + '</div>';
  html += '<audio controls preload="auto" src="' + AUDIO[trial.clip] + '"></audio>';
  html += '<div class="question">Did any character’s voice change during this scene?</div><div class="row">';
  [['no','No, same voice throughout',false],['yes','Yes, a character changed',true]].forEach(([value,label,flag]) => {
    html += '<button type="button" class="answer" data-key="voice_changed" data-value="' + flag + '" aria-pressed="' + (r.voice_changed === flag) + '">' + label + '</button>';
  });
  html += '</div><input type="text" id="voice_line" placeholder="Which line? (optional)" value="' + escapeHtml(r.voice_line || '') + '">';
  html += '<div class="question">Was any pause clearly too long or too short?</div><div class="row">';
  [['none','No, pauses were fine'],['long','Yes, too long'],['short','Yes, too short']].forEach(([value,label]) => {
    html += '<button type="button" class="answer" data-key="pause_issue" data-value="' + value + '" aria-pressed="' + (r.pause_issue === value) + '">' + label + '</button>';
  });
  html += '</div><input type="text" id="pause_line" placeholder="Which pause? (optional)" value="' + escapeHtml(r.pause_line || '') + '">';
  html += '<textarea id="notes" placeholder="Notes (optional)">' + escapeHtml(r.notes || '') + '</textarea></div>';
  app.innerHTML = html;
  app.querySelectorAll('button.answer').forEach(button => button.addEventListener('click', () => {
    const key = button.dataset.key; let value = button.dataset.value;
    if (key === 'voice_changed') { value = value === 'true'; }
    r[key] = value;
    app.querySelectorAll('button.answer[data-key="' + key + '"]').forEach(peer => {
      const same = key === 'voice_changed' ? (peer.dataset.value === 'true') === value : peer.dataset.value === value;
      peer.setAttribute('aria-pressed', String(same));
    });
    document.getElementById('next').disabled = !complete(r); save();
  }));
  document.getElementById('voice_line').addEventListener('input', e => { r.voice_line = e.target.value; save(); });
  document.getElementById('pause_line').addEventListener('input', e => { r.pause_line = e.target.value; save(); });
  document.getElementById('notes').addEventListener('input', e => { r.notes = e.target.value; save(); });
  document.getElementById('progress').textContent = 'Trial ' + (state.index + 1) + ' / ' + TRIALS.length;
  document.getElementById('back').disabled = state.index === 0;
  document.getElementById('next').textContent = state.index === TRIALS.length - 1 ? 'Finish' : 'Next';
  document.getElementById('next').disabled = !complete(r);
  document.getElementById('done').classList.add('hidden');
}
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
document.getElementById('next').addEventListener('click', () => {
  if (state.index < TRIALS.length - 1) { state.index += 1; save(); render(); return; }
  document.getElementById('app').classList.add('hidden');
  document.getElementById('done').classList.remove('hidden');
  document.getElementById('next').disabled = true; document.getElementById('back').disabled = true;
});
document.getElementById('back').addEventListener('click', () => { if (state.index > 0) { state.index -= 1; save(); render(); } });
document.getElementById('download').addEventListener('click', () => {
  const payload = { version: 1, seed: SEED, exported_at: new Date().toISOString(), responses: state.responses };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob);
  link.download = 'listening-detect-' + SEED + '.json'; link.click();
});
render();
</script></body></html>"""


def build_detect_page(clips_dir, labels_path, fixture_path, out_html, key_out, seed=1555):
    labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    scenes = {scene["id"]: scene for scene in fixture["scenes"]}
    items = []
    for item_id, label in labels.items():
        path = Path(clips_dir) / f"{item_id}.wav"
        if not path.is_file():
            raise FileNotFoundError(f"Missing supplement clip: {path}")
        scene = scenes[label["scene"]]
        items.append({"item_id": item_id, "title": scene.get("title") or "Scene",
                      "scene": scene["lines"], "path": path})
    schedule = [{"item_id": item["item_id"], "repeat": False} for item in items]
    schedule += [{"item_id": item_id, "repeat": True} for item_id in REPEATS]
    rng = random.Random(seed)
    for _ in range(1000):
        rng.shuffle(schedule)
        positions = {entry["item_id"]: index for index, entry in enumerate(schedule) if not entry["repeat"]}
        if all(not entry["repeat"] or abs(index - positions[entry["item_id"]]) >= MIN_GAP
               for index, entry in enumerate(schedule)):
            break
    else:
        raise RuntimeError("Could not place detection repeats")
    trials = []
    for index, entry in enumerate(schedule):
        item = next(item for item in items if item["item_id"] == entry["item_id"])
        clip = hashlib.sha256(item["item_id"].encode()).hexdigest()[:12]
        trials.append({"id": f"d{index + 1:02d}", "clip": clip, "title": item["title"],
                       "scene": item["scene"], "_item_id": entry["item_id"], "_repeat": entry["repeat"]})
    audio = {hashlib.sha256(item["item_id"].encode()).hexdigest()[:12]:
             "data:audio/wav;base64," + base64.b64encode(item["path"].read_bytes()).decode("ascii")
             for item in items}
    public = [{k: v for k, v in trial.items() if not k.startswith("_")} for trial in trials]
    html = (PAGE.replace("__AUDIO__", json.dumps(audio)).replace("__TRIALS__", json.dumps(public))
                .replace("__SEED__", str(seed)))
    Path(out_html).parent.mkdir(parents=True, exist_ok=True)
    Path(out_html).write_text(html, encoding="utf-8")
    key = {"seed": seed,
           "trials": [{"trial_id": t["id"], "item_id": t["_item_id"], "repeat": t["_repeat"]} for t in trials],
           "repeats": {item_id: [t["id"] for t in trials if t["_item_id"] == item_id] for item_id in REPEATS}}
    Path(key_out).write_text(json.dumps(key, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"trials": len(trials), "html": str(out_html), "key": str(key_out)}


def analyze_detection(responses_path, key_path, labels_path, out_path=None):
    responses = json.loads(Path(responses_path).read_text(encoding="utf-8"))
    key = json.loads(Path(key_path).read_text(encoding="utf-8"))
    labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    answers = responses.get("responses") or {}
    expected = {entry["trial_id"]: entry for entry in key["trials"]}
    if responses.get("seed") != key.get("seed"):
        raise ValueError("Responses belong to a different detection seed")
    missing = sorted(set(expected) - set(answers))
    extra = sorted(set(answers) - set(expected))
    if missing or extra:
        raise ValueError(f"Response set mismatch: missing={missing} extra={extra}")
    for trial_id, answer in answers.items():
        if answer.get("voice_changed") not in (True, False):
            raise ValueError(f"{trial_id}: voice_changed is not boolean")
        if answer.get("pause_issue") not in ("none", "long", "short"):
            raise ValueError(f"{trial_id}: invalid pause_issue {answer.get('pause_issue')!r}")
    per_item = {}
    counts = {"voice": {"tp": 0, "fp": 0, "fn": 0, "tn": 0},
              "pause": {"tp": 0, "fp": 0, "fn": 0, "tn": 0}}
    pause_types = {"correct": 0, "wrong": 0, "missed": 0}
    for trial_id, entry in expected.items():
        item = entry["item_id"]
        if entry["repeat"]:
            continue
        answer = answers[trial_id]
        label = labels[item]
        voice_fault = label["fault"] == "voice"
        pause_fault = label["fault"] in ("pause_long", "pause_short")
        voice_hit = answer["voice_changed"] is True
        pause_hit = answer["pause_issue"] != "none"
        counts["voice"]["tp" if voice_fault and voice_hit else
                         "fn" if voice_fault else
                         "fp" if voice_hit else "tn"] += 1
        counts["pause"]["tp" if pause_fault and pause_hit else
                          "fn" if pause_fault else
                          "fp" if pause_hit else "tn"] += 1
        if pause_fault and pause_hit:
            expected_type = "long" if label["fault"] == "pause_long" else "short"
            pause_types["correct" if answer["pause_issue"] == expected_type else "wrong"] += 1
        elif pause_fault:
            pause_types["missed"] += 1
        per_item[item] = {"expect": label["expect"], "fault": label["fault"],
                          "voice_changed": answer["voice_changed"],
                          "pause_issue": answer["pause_issue"],
                          "voice_line": answer.get("voice_line", ""),
                          "pause_line": answer.get("pause_line", ""),
                          "notes": answer.get("notes", "")}
    for kind, c in counts.items():
        c["precision"] = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else None
        c["recall"] = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else None
    repeat_report = {}
    for item_id, trial_ids in key["repeats"].items():
        repeat_report[item_id] = {
            "voice": [answers[t]["voice_changed"] for t in trial_ids],
            "pause": [answers[t]["pause_issue"] for t in trial_ids],
        }
    summary = {"seed": key["seed"], "counts": counts, "pause_types": pause_types,
               "per_item": per_item, "repeats": repeat_report}
    if out_path:
        Path(out_path).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--clips-dir", required=True)
    build.add_argument("--labels", required=True)
    build.add_argument("--fixture", default=str(APP_DIR / "fixtures" / "alice_pd_listening.json"))
    build.add_argument("--out-html", required=True)
    build.add_argument("--key-out", required=True)
    build.add_argument("--seed", type=int, default=1555)
    analyze = sub.add_parser("analyze")
    analyze.add_argument("--responses", required=True)
    analyze.add_argument("--key", required=True)
    analyze.add_argument("--labels", required=True)
    analyze.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    if args.command == "build":
        print(json.dumps(build_detect_page(args.clips_dir, args.labels, args.fixture,
                                           args.out_html, args.key_out, args.seed), indent=2))
    else:
        summary = analyze_detection(args.responses, args.key, args.labels, args.out)
        print(json.dumps(summary["counts"], indent=2))


if __name__ == "__main__":
    main()

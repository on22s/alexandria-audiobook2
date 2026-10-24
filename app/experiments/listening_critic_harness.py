"""Blind listener round for the #1547 listening critic.

Builds a self-contained rating page from the public-domain Alice fixture and
local clips, with hidden repeats, then scores listener consistency. The page
never shows a critic output or labels which trials are repeats.
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

REPEAT_ITEM_IDS = ("line_10", "scene_03", "line_23")
MIN_REPEAT_GAP = 5
RATING_MIN, RATING_MAX = 1, 5
SCALES = {
    "line": [
        ("naturalness", "Naturalness", ["Robotic", "Mostly unnatural", "Mixed", "Mostly natural", "Natural"]),
        ("instruct_fit", "Follows its instruct", ["Contradicts", "Mostly not", "Partly", "Mostly follows", "Clearly follows"]),
        ("content_correct", "Says the line", ["Wrong or missing", "Mostly wrong", "Partly", "Mostly correct", "Exact"]),
    ],
    "scene": [
        ("voice_consistency", "Same voice per character", ["Characters swap", "Mostly different", "Mixed", "Mostly consistent", "Clearly consistent"]),
        ("pause_fit", "Pause lengths", ["Too long or short", "Mostly wrong", "Noticeable", "Mostly natural", "Natural"]),
        ("emotion_fit", "Emotion matches the scene", ["Mismatched", "Mostly off", "Partly", "Mostly fits", "Clearly fits"]),
    ],
    "clip": [
        ("voice_consistency", "Same voice per character", ["Characters swap", "Mostly different", "Mixed", "Mostly consistent", "Clearly consistent"]),
        ("pause_fit", "Pause lengths", ["Too long or short", "Mostly wrong", "Noticeable", "Mostly natural", "Natural"]),
    ],
}
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Listening check</title>
<style>
:root { color-scheme: light dark; }
body { font: 16px/1.5 system-ui, sans-serif; max-width: 920px; margin: 0 auto; padding: 24px; }
h1 { font-size: 1.4rem; } .muted { opacity: .7; }
.card { border: 1px solid #8884; border-radius: 10px; padding: 20px; margin-top: 16px; }
.script { white-space: pre-wrap; background: #8881; padding: 12px; border-radius: 8px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; margin: 6px 0 16px; }
button.scale { flex: 1 1 120px; padding: 10px 8px; border: 1px solid #8886; border-radius: 8px; background: transparent; cursor: pointer; }
button.scale strong { display: block; font-size: 1.2rem; }
button.scale span { display: block; font-size: .78rem; opacity: .85; }
button.scale[aria-pressed="true"] { background: #2563eb22; border-color: #2563eb; }
.dim { margin-top: 12px; } .dim-label { font-weight: 600; }
textarea { width: 100%; min-height: 70px; margin-top: 12px; }
.bar { display: flex; gap: 10px; align-items: center; justify-content: space-between; margin-top: 16px; }
.pill { padding: 4px 10px; border: 1px solid #8886; border-radius: 999px; font-size: .85rem; }
.hidden { display: none; }
</style></head><body>
<h1>Listening check</h1>
<p class="muted">Rate what you hear. No automatic score is shown. You can go back before submitting. Progress is saved in this browser.</p>
<div class="bar"><span id="progress" class="pill"></span><span id="save" class="muted"></span></div>
<div id="app"></div>
<div class="bar"><button id="back" type="button">Back</button><button id="next" type="button">Next</button></div>
<div id="done" class="card hidden"><h2>Finished</h2><p>Download your responses and send the file back.</p><button id="download" type="button">Download responses</button></div>
<script>
const AUDIO = __AUDIO__;
const TRIALS = __TRIALS__;
const SCALES = __SCALES__;
const SEED = __SEED__;
const STORE = 'listening-check-1547-' + SEED;
let state = { index: 0, responses: {} };
try { const saved = JSON.parse(localStorage.getItem(STORE) || 'null'); if (saved && typeof saved.index === 'number' && saved.responses && saved.index < TRIALS.length) { state = saved; } } catch (e) {}
function save() { localStorage.setItem(STORE, JSON.stringify({ index: state.index, responses: state.responses })); document.getElementById('save').textContent = 'Saved'; }
function render() {
  const trial = TRIALS[state.index];
  const spec = SCALES[trial.kind];
  const response = state.responses[trial.id] || { ratings: {}, notes: '' };
  state.responses[trial.id] = response;
  const app = document.getElementById('app');
  let html = '<div class="card"><div class="muted">Trial ' + (state.index + 1) + ' / ' + TRIALS.length + ' · ' + (trial.kind === 'line' ? 'Line' : (trial.kind === 'scene' ? 'Scene' : 'Clip')) + '</div>';
  if (trial.kind !== 'line') {
    html += '<h2>' + escapeHtml(trial.title || 'Scene') + '</h2><div class="script">' + trial.scene.map(line => escapeHtml(line.speaker) + ': ' + escapeHtml(line.text)).join('\\n') + '</div>';
  } else {
    html += '<h2>' + escapeHtml(trial.speaker) + '</h2><div class="script">' + escapeHtml(trial.text) + '</div>';
    if (trial.instruct) { html += '<p class="muted">Instruct: ' + escapeHtml(trial.instruct) + '</p>'; }
  }
  html += '<audio controls preload="auto" src="' + AUDIO[trial.clip] + '"></audio>';
  for (const [key, label, labels] of spec) {
    if (key === 'instruct_fit' && !trial.instruct) { continue; }
    html += '<div class="dim"><div class="dim-label">' + label + '</div><div class="row">';
    labels.forEach((text, i) => {
      const value = i + 1;
      const selected = response.ratings[key] === value;
      html += '<button type="button" class="scale" data-dim="' + key + '" data-value="' + value + '" aria-pressed="' + selected + '"><strong>' + value + '</strong><span>' + text + '</span></button>';
    });
    html += '</div></div>';
  }
  html += '<textarea id="notes" placeholder="Notes (optional): what did you hear?">' + escapeHtml(response.notes || '') + '</textarea></div>';
  app.innerHTML = html;
  app.querySelectorAll('button.scale').forEach(button => button.addEventListener('click', () => {
    const dim = button.dataset.dim;
    const value = Number(button.dataset.value);
    response.ratings[dim] = value;
    app.querySelectorAll('button.scale[data-dim="' + dim + '"]').forEach(peer => {
      peer.setAttribute('aria-pressed', String(Number(peer.dataset.value) === value));
    });
    document.getElementById('next').disabled = !complete(response.ratings, spec);
    save();
  }));
  document.getElementById('notes').addEventListener('input', event => { response.notes = event.target.value; save(); });
  document.getElementById('progress').textContent = 'Trial ' + (state.index + 1) + ' / ' + TRIALS.length;
  document.getElementById('back').disabled = state.index === 0;
  document.getElementById('next').textContent = state.index === TRIALS.length - 1 ? 'Finish' : 'Next';
  document.getElementById('next').disabled = !complete(response.ratings, spec);
  document.getElementById('done').classList.add('hidden');
}
function complete(ratings, spec) { return spec.every(([key]) => key === 'instruct_fit' && !TRIALS[state.index].instruct ? true : !!ratings[key]); }
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
document.getElementById('next').addEventListener('click', () => {
  if (state.index < TRIALS.length - 1) { state.index += 1; save(); render(); return; }
  document.getElementById('app').classList.add('hidden');
  document.getElementById('done').classList.remove('hidden');
  document.getElementById('back').disabled = true;
  document.getElementById('next').disabled = true;
});
document.getElementById('back').addEventListener('click', () => { if (state.index > 0) { state.index -= 1; save(); render(); } });
document.getElementById('download').addEventListener('click', () => {
  const payload = { version: 1, seed: SEED, exported_at: new Date().toISOString(), responses: state.responses };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'listening-responses-' + SEED + '.json';
  link.click();
});
render();
</script></body></html>"""


def load_items(fixture_path, clips_dir):
    """Load the 35 unique items and verify their audio exists."""
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    if fixture.get("license") != "public-domain":
        raise ValueError("Fixture must be public domain")
    if len(fixture.get("lines") or []) != 30 or len(fixture.get("scenes") or []) != 5:
        raise ValueError("Fixture needs 30 lines and 5 scenes")
    clips = Path(clips_dir)
    items = []
    for line in fixture["lines"]:
        path = clips / f"{line['id']}.wav"
        if not path.is_file():
            raise FileNotFoundError(f"Missing clip for {line['id']}: {path}")
        items.append({"item_id": line["id"], "kind": "line", "speaker": line["speaker"],
                      "text": line["text"], "instruct": line.get("instruct", ""), "path": path})
    for scene in fixture["scenes"]:
        path = clips / f"{scene['id']}.wav"
        if not path.is_file():
            raise FileNotFoundError(f"Missing clip for {scene['id']}: {path}")
        items.append({"item_id": scene["id"], "kind": "scene", "title": scene.get("title", ""),
                      "lines": scene["lines"], "path": path})
    return items


def build_trials(items, seed=1547):
    """Create 35 items plus 3 hidden repeats, shuffled with a minimum gap."""
    by_id = {item["item_id"]: item for item in items}
    missing = [item_id for item_id in REPEAT_ITEM_IDS if item_id not in by_id]
    if missing:
        raise ValueError(f"Repeat items missing from fixture: {missing}")
    schedule = [{"item_id": item["item_id"], "repeat": False} for item in items]
    schedule += [{"item_id": item_id, "repeat": True} for item_id in REPEAT_ITEM_IDS]
    rng = random.Random(seed)
    for _ in range(1000):
        rng.shuffle(schedule)
        positions = {entry["item_id"]: index for index, entry in enumerate(schedule) if not entry["repeat"]}
        ok = True
        for index, entry in enumerate(schedule):
            if entry["repeat"] and abs(index - positions[entry["item_id"]]) < MIN_REPEAT_GAP:
                ok = False
                break
        if ok:
            break
    else:
        raise RuntimeError("Could not place hidden repeats with the required gap")
    trials = []
    for index, entry in enumerate(schedule):
        item = by_id[entry["item_id"]]
        clip = hashlib.sha256(item["item_id"].encode()).hexdigest()[:12]
        trial = {"id": f"t{index + 1:02d}", "clip": clip, "kind": item["kind"]}
        if item["kind"] == "line":
            trial.update(text=item["text"], instruct=item["instruct"], speaker=item["speaker"])
        else:
            trial.update(title=item.get("title", ""), scene=item["lines"])
        trials.append({**trial, "_item_id": entry["item_id"], "_repeat": entry["repeat"]})
    return trials


def key_for(trials, seed, repeat_item_ids):
    return {"seed": seed,
            "trials": [{"trial_id": trial["id"], "item_id": trial["_item_id"],
                        "kind": trial["kind"], "repeat": trial["_repeat"]}
                       for trial in trials],
            "repeats": {item_id: [trial["id"] for trial in trials
                                  if trial["_item_id"] == item_id]
                        for item_id in repeat_item_ids}}


def write_page(items, trials, seed, out_html, key_out, repeat_item_ids):
    """Write a blind page and its concealed key for any item set."""
    audio = {}
    for item in items:
        data = Path(item["path"]).read_bytes()
        audio[hashlib.sha256(item["item_id"].encode()).hexdigest()[:12]] = (
            "data:audio/wav;base64," + base64.b64encode(data).decode("ascii"))
    public_trials = [{key: value for key, value in trial.items() if not key.startswith("_")}
                     for trial in trials]
    html = (PAGE.replace("__AUDIO__", json.dumps(audio))
                .replace("__TRIALS__", json.dumps(public_trials))
                .replace("__SCALES__", json.dumps(SCALES))
                .replace("__SEED__", str(seed)))
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html, encoding="utf-8")
    key_path = Path(key_out)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(json.dumps(key_for(trials, seed, repeat_item_ids),
                                   indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"trials": len(public_trials), "repeats": len(repeat_item_ids),
            "html": str(out_html), "key": str(key_path)}


def build_page(fixture_path, clips_dir, out_html, key_out, seed=1547):
    """Write the blind page and the concealed key for the main fixture."""
    items = load_items(fixture_path, clips_dir)
    trials = build_trials(items, seed)
    return write_page(items, trials, seed, out_html, key_out, REPEAT_ITEM_IDS)


def required_dimensions(key_entry, fixture):
    kind = key_entry.get("kind") or ("line" if key_entry["item_id"].startswith("line_") else "scene")
    spec = SCALES[kind]
    if kind == "line":
        line = next(item for item in fixture["lines"] if item["id"] == key_entry["item_id"])
        if not line.get("instruct"):
            spec = [dim for dim in spec if dim[0] != "instruct_fit"]
    return [dim[0] for dim in spec]


def analyze_responses(responses_path, key_path, fixture_path, out_path=None):
    """Validate completeness and score hidden-repeat consistency."""
    responses = json.loads(Path(responses_path).read_text(encoding="utf-8"))
    key = json.loads(Path(key_path).read_text(encoding="utf-8"))
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    answers = responses.get("responses") or {}
    expected = {entry["trial_id"]: entry for entry in key["trials"]}
    missing = sorted(set(expected) - set(answers))
    extra = sorted(set(answers) - set(expected))
    if missing or extra:
        raise ValueError(f"Response set mismatch: missing={missing} extra={extra}")
    if responses.get("seed") != key.get("seed"):
        raise ValueError("Responses belong to a different study seed")
    problems = []
    for trial_id, entry in expected.items():
        dims = required_dimensions(entry, fixture)
        ratings = (answers[trial_id] or {}).get("ratings") or {}
        for dim in dims:
            value = ratings.get(dim)
            if not isinstance(value, int) or not RATING_MIN <= value <= RATING_MAX:
                problems.append(f"{trial_id}:{dim}={value!r}")
    if problems:
        raise ValueError("Invalid or missing ratings: " + ", ".join(problems))
    repeat_report = {}
    for item_id, trial_ids in key["repeats"].items():
        group = [answers[trial_id]["ratings"] for trial_id in trial_ids]
        entry = {}
        for dim in group[0]:
            values = [ratings.get(dim) for ratings in group]
            if any(value is None for value in values):
                continue
            entry[dim] = {"values": values,
                          "exact": len(set(values)) == 1,
                          "within_one": max(values) - min(values) <= 1,
                          "mean_abs_diff": sum(abs(values[i] - values[j])
                                               for i in range(len(values)) for j in range(i + 1, len(values)))
                                              / max(1, len(values) * (len(values) - 1) / 2)}
        repeat_report[item_id] = entry
    summary = {"trials": len(expected), "repeats": repeat_report,
               "answered_with_notes": sum(1 for answer in answers.values() if (answer.get("notes") or "").strip())}
    if out_path:
        Path(out_path).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--fixture", default=str(APP_DIR / "fixtures" / "alice_pd_listening.json"))
    build.add_argument("--clips-dir", required=True)
    build.add_argument("--out-html", required=True)
    build.add_argument("--key-out", required=True)
    build.add_argument("--seed", type=int, default=1547)
    analyze = sub.add_parser("analyze")
    analyze.add_argument("--responses", required=True)
    analyze.add_argument("--key", required=True)
    analyze.add_argument("--fixture", default=str(APP_DIR / "fixtures" / "alice_pd_listening.json"))
    analyze.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    if args.command == "build":
        print(json.dumps(build_page(args.fixture, args.clips_dir, args.out_html,
                                    args.key_out, args.seed), indent=2))
        return
    summary = analyze_responses(args.responses, args.key, args.fixture, args.out)
    print(json.dumps({"trials": summary["trials"], "repeats": summary["repeats"]}, indent=2))


if __name__ == "__main__":
    main()

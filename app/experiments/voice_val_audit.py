"""Goal 2.7: which shipped voice adapters were trained on their own validation clips.

Every dataset zip holds train/ and val/ (each with metadata.jsonl). An adapter's
training_meta.json records num_samples but not which clips. So, per shipped adapter:
find its dataset zip(s) by the dataset folder named in ref_sample_audio, confirm the
match by the stored reference text appearing in the zip's metadata, count train/ and
val/ clips, and classify:
  - clean            num_samples == train
  - trained on val   num_samples == train + val, val > 0
  - no val split     val == 0 (nothing held out to contaminate, nothing to evaluate on)
  - unmatched        no zip found, or num_samples fits neither
All label-variant zips of one dataset are checked; they must agree.
"""
import argparse, glob, json, os, re, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
import sys
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402


def norm(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def classify(n, train, val):
    if val == 0 and n == train:
        return "no val split"
    if n == train:
        return "clean"
    if n == train + val:
        return "trained on val"
    return "unmatched"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapters", default=os.path.join(REPO, "lora_models"))
    ap.add_argument("--zips", nargs="+", required=True, help="directories holding the dataset zips")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    zips = [z for d in a.zips for z in glob.glob(os.path.join(d, "*.zip"))]
    index = {}                     # zip -> (train, val, metadata text), read once
    for z in zips:
        zf = zipfile.ZipFile(z); nl = zf.namelist()
        index[z] = (sum(1 for x in nl if x.startswith("train/") and x.endswith(".wav")),
                    sum(1 for x in nl if x.startswith("val/") and x.endswith(".wav")),
                    " ".join(zf.read(x).decode("utf-8", "ignore") for x in nl if x.endswith("metadata.jsonl")))
    rows = []
    for meta in sorted(glob.glob(os.path.join(a.adapters, "*", "training_meta.json"))):
        d = json.load(open(meta))
        name = os.path.basename(os.path.dirname(meta)); n = d.get("num_samples")
        base = os.path.basename(os.path.dirname(d.get("ref_sample_audio", "")))
        ref = (d.get("ref_sample_text") or "").replace("...", "").strip()[:30]
        # by dataset name first; when the reference was re-cut elsewhere (a retrain folder),
        # the name says nothing, so fall back to the zips whose metadata holds the reference text
        named = [z for z in zips if norm(os.path.basename(z)).startswith(norm(base))]
        how = "dataset name"
        if not any(ref and ref in index[z][2] for z in named):
            named = [z for z in zips if ref and ref in index[z][2]]; how = "reference text"
        found = [{"zip": os.path.basename(z), "train": index[z][0], "val": index[z][1],
                  "ref_text_in_zip": bool(ref) and ref in index[z][2], "matched_by": how} for z in named]
        verdicts = sorted({classify(n, f["train"], f["val"]) for f in found if f["ref_text_in_zip"]})
        rows.append({"adapter": name, "num_samples": n, "dataset": base, "zips": found,
                     "verdict": verdicts[0] if len(verdicts) == 1 else ("unmatched" if not verdicts else "zips disagree: " + ", ".join(verdicts))})
    summary = {}
    for r in rows:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    json.dump({"summary": summary, "rows": rows, "provenance": provenance(__file__, a)}, open(a.out, "w"), indent=1)
    print(summary)
    for r in rows:
        if r["verdict"] != "clean":
            print(f"  {r['adapter']:34} n={r['num_samples']:4}  {r['verdict']}")


if __name__ == "__main__":
    main()

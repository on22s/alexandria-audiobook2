"""Verify archived CPU evidence without torch, audio, or network access."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(path):
    return json.loads(path.read_text())


def require(condition, message):
    """Keep archive validation active with -O and PYTHONOPTIMIZE as well."""
    if not condition:
        raise ValueError(message)


def main():
    for name, expected in load(ROOT / "SHA256SUMS.json").items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Archive hash mismatch: {name}")
    previous = set()
    for run, runner in (("run2", "run_experiment.py"), ("run3", "run_experiment_4s.py")):
        folder = ROOT / run
        protocol, results = load(folder / "protocol.json"), load(folder / "results.json")
        thresholds = load(folder / "frozen_thresholds.json")
        require(results["status"] == "complete" and results["thresholds"] == thresholds, 'Archive semantic check failed: results["status"] == "complete" and results["thresholds"] == thresholds')
        require(results["protocol_sha256"] == hashlib.sha256((folder / "protocol.json").read_bytes()).hexdigest(), 'Archive semantic check failed: results["protocol_sha256"] == hashlib.sha256((folder / "protocol.json").read_bytes()).hexdigest()')
        require(results["script_sha256"] == hashlib.sha256((ROOT / "frozen" / (runner + ".txt")).read_bytes()).hexdigest(), 'Archive semantic check failed: results["script_sha256"] == hashlib.sha256((ROOT / "frozen" / (runner + ".txt")).read_bytes()).hexdigest()')
        split_speakers = []
        for split in ("calibration", "test"):
            speakers = {s["speaker"] for s in protocol["speakers"][split]}
            split_speakers.append(speakers)
            rows = load(folder / (split + "_rows.json"))
            expected = {(s, d, a) for s in speakers for d in protocol["durations_seconds"] for a in protocol["arms"]}
            require(len(rows) == len(expected) == 96, 'Archive semantic check failed: len(rows) == len(expected) == 96')
            require({(r["speaker"], r["duration"], r["arm"]) for r in rows} == expected, 'Archive semantic check failed: {(r["speaker"], r["duration"], r["arm"]) for r in rows} == expected')
            for row in rows:
                require(row["status"] == "measured" and row["donor"] in speakers, 'Archive semantic check failed: row["status"] == "measured" and row["donor"] in speakers')
                require(row["window_min"] == min(w["score"] for w in row["windows"]), 'Archive semantic check failed: row["window_min"] == min(w["score"] for w in row["windows"])')
            for duration in (24, 48, "all"):
                subset = [r for r in rows if duration == "all" or r["duration"] == duration]
                for metric in ("whole", "window_min"):
                    for arm in protocol["arms"]:
                        cases = [r for r in subset if r["arm"] == arm]
                        reported = results["summaries"][split][str(duration)][metric][arm]
                        require(reported["total"] == len(cases), 'Archive semantic check failed: reported["total"] == len(cases)')
                        require(reported["flagged"] == sum(r[metric] < thresholds[metric]["threshold"] for r in cases), 'Archive semantic check failed: reported["flagged"] == sum(r[metric] < thresholds[metric]["threshold"] for r in cases)')
                        require(reported["rate"] == reported["flagged"] / len(cases), 'Archive semantic check failed: reported["rate"] == reported["flagged"] / len(cases)')
        require(not split_speakers[0] & split_speakers[1], 'Archive semantic check failed: not split_speakers[0] & split_speakers[1]')
        combined = set.union(*split_speakers)
        require(not combined & previous, 'Archive semantic check failed: not combined & previous')
        previous.update(combined)
        require(all(r["status"] == "not_measured" for r in load(folder / "availability_controls.json")), 'Archive semantic check failed: all(r["status"] == "not_measured" for r in load(folder / "availability_controls.json"))')
        test = results["summaries"]["test"]["all"]
        controls = [test["window_min"][a] for a in ("clean", "same_speaker_splice", "gain_change")]
        fpr = sum(c["flagged"] for c in controls) / sum(c["total"] for c in controls)
        require(results["test_window_control_false_positive_rate"] == fpr, 'Archive semantic check failed: results["test_window_control_false_positive_rate"] == fpr')
        require(results["go"] == (test["window_min"]["swap_4s"]["rate"] > test["whole"]["swap_4s"]["rate"] and fpr <= .05), 'Archive semantic check failed: results["go"] == (test["window_min"]["swap_4s"]["rate"] > test["whole"]["swap_4s"]["rate"] and fpr <= .05)')
    print("PASS: archive hashes, 384 cells, split isolation, score summaries, and pilot decisions")


if __name__ == "__main__":
    main()

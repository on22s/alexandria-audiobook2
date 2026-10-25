"""Seed a tiny synthetic book into ALEXANDRIA_DATA_DIR for browser smoke tests.

Run with the app's interpreter from app/. Writes only inside the data dir.
Text is synthetic (Rule 27); audio is short sine tones.
"""
import json
import os
import sys

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "app")
sys.path.insert(0, APP)

ROWS = [  # (speaker, text, pause_after ms or None, tone Hz, seconds)
    ("NARRATOR", "The lamp was lit.", 600, 440, 0.35),
    ("ANNA", "Who is there?", 0, 550, 0.30),
    ("NARRATOR", "Nobody answered.", None, 660, 0.30),
]


def main(data_dir):
    if (not os.path.isabs(data_dir) or os.path.islink(data_dir)
            or not os.path.isdir(data_dir) or os.listdir(data_dir)):
        raise ValueError("E2E seeding requires an existing empty absolute data directory")
    import numpy as np
    import soundfile as sf
    from project import ProjectManager
    entries, chunks = [], []
    os.makedirs(os.path.join(data_dir, "voicelines"), exist_ok=True)
    for index, (speaker, text, pause, tone, seconds) in enumerate(ROWS):
        name = f"voicelines/e2e_{index}.wav"
        rate = 24000
        t = np.arange(int(rate * seconds)) / rate
        sf.write(os.path.join(data_dir, name), (0.2 * np.sin(2 * np.pi * tone * t)).astype("float32"),
                 rate, subtype="PCM_16")
        entries.append({"speaker": speaker, "text": text, "instruct": ""})
        chunk = {"id": index, "uid": f"e2e{index}", "speaker": speaker, "text": text, "instruct": "",
                 "status": "done", "audio_path": name}
        if pause is not None:
            chunk["pause_after"] = pause
        chunks.append(chunk)
    with open(os.path.join(data_dir, "annotated_script.json"), "w") as handle:
        json.dump(entries, handle)
    with open(os.path.join(data_dir, "state.json"), "w") as handle:
        json.dump({"active_book_id": "e2e-smoke"}, handle)
    ProjectManager(data_dir).save_chunks(chunks)
    print(json.dumps({"rows": len(chunks), "pauses": [row[2] for row in ROWS]}))


if __name__ == "__main__":
    main(os.environ["ALEXANDRIA_DATA_DIR"])

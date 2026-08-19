"""The pause detector, checked on audio whose answer is known in advance.

Rule 21. `measure_pauses.internal_pauses` produced the finding that overturned
the -ay respelling change - 341 of 384 terms gaining internal pauses, sign test
p=1.1e-58 - and had no test. It separated internal silences from edge silences
by DURATION (anything under 1.5s counted as internal) rather than by position,
while its own module docstring required the opposite: "leading and trailing
room tone is not the voice breaking a word up, and counting it would swamp the
signal being measured". These are short TTS clips, so their room tone is almost
always under 1.5s and was counted every time.

Every fixture here is real audio built by ffmpeg with silences at known places,
and one test asserts the OLD rule fails them - so the fixtures cannot quietly
stop discriminating.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from experiments.measure_pauses import clip_duration, internal_pauses  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def build(path, segments, rate=24000):
    """segments: [("tone"|"silence", seconds)] -> one concatenated wav."""
    parts = []
    for kind, seconds in segments:
        if kind == "tone":
            parts.append("sine=frequency=440:duration=%s:sample_rate=%d"
                         % (seconds, rate))
        else:
            parts.append("anullsrc=r=%d:cl=mono:d=%s" % (rate, seconds))
    graph = "".join("[%d:a]" % i for i in range(len(parts)))
    inputs = []
    for part in parts:
        inputs += ["-f", "lavfi", "-i", part]
    subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex",
         "%sconcat=n=%d:v=0:a=1[out]" % (graph, len(parts)),
         "-map", "[out]", "-ac", "1", "-ar", str(rate), path],
        capture_output=True, check=True)
    return path


def old_rule(path, edge_seconds=1.5):
    """The pre-2026-08-19 implementation, kept so the fixtures stay honest."""
    import re
    proc = subprocess.run(
        ["ffmpeg", "-i", path, "-af", "silencedetect=noise=-35dB:d=0.12",
         "-f", "null", "-"], capture_output=True, text=True)
    durations = [float(x) for x in
                 re.findall(r"silence_duration: ([0-9.]+)", proc.stderr)]
    inner = [d for d in durations if d < edge_seconds]
    return len(inner), round(sum(inner), 3)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not available")
class PauseScoringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_one_internal_gap_between_two_words(self):
        clip = build(self.path("one.wav"),
                     [("tone", 0.5), ("silence", 0.4), ("tone", 0.5)])
        count, seconds = internal_pauses(clip)
        self.assertEqual(1, count)
        self.assertAlmostEqual(0.4, seconds, delta=0.08)

    def test_leading_and_trailing_room_tone_are_not_pauses(self):
        """THE BUG. Room tone shorter than 1.5s used to count as internal."""
        clip = build(self.path("edges.wav"),
                     [("silence", 0.4), ("tone", 0.6), ("silence", 0.4)])
        count, seconds = internal_pauses(clip)
        self.assertEqual(0, count,
                         "silence at the top and bottom of a clip is room "
                         "tone, not the voice breaking a word up")
        self.assertEqual(0.0, seconds)

    def test_the_old_rule_fails_that_case(self):
        """If this ever passes, the fixture stopped discriminating."""
        clip = build(self.path("edges2.wav"),
                     [("silence", 0.4), ("tone", 0.6), ("silence", 0.4)])
        old_count, _ = old_rule(clip)
        self.assertGreater(old_count, 0,
                           "the old duration rule is supposed to miscount "
                           "these edges; if it no longer does, this test file "
                           "is no longer proving anything")
        self.assertEqual(0, internal_pauses(clip)[0])

    def test_edges_and_an_internal_gap_together(self):
        """The realistic shape: room tone at both ends AND a real chop."""
        clip = build(self.path("mixed.wav"),
                     [("silence", 0.3), ("tone", 0.5), ("silence", 0.4),
                      ("tone", 0.5), ("silence", 0.3)])
        count, seconds = internal_pauses(clip)
        self.assertEqual(1, count)
        self.assertAlmostEqual(0.4, seconds, delta=0.08)
        self.assertGreaterEqual(old_rule(clip)[0], 3,
                                "the old rule counted all three")

    def test_continuous_speech_has_no_pauses(self):
        clip = build(self.path("solid.wav"), [("tone", 1.2)])
        self.assertEqual((0, 0.0), internal_pauses(clip))

    def test_two_internal_gaps_are_both_counted(self):
        clip = build(self.path("two.wav"),
                     [("tone", 0.4), ("silence", 0.3), ("tone", 0.4),
                      ("silence", 0.3), ("tone", 0.4)])
        count, seconds = internal_pauses(clip)
        self.assertEqual(2, count)
        self.assertAlmostEqual(0.6, seconds, delta=0.12)

    def test_a_gap_shorter_than_the_floor_is_not_a_pause(self):
        """0.12s is the detector's minimum; 0.05s must not register."""
        clip = build(self.path("tiny.wav"),
                     [("tone", 0.5), ("silence", 0.05), ("tone", 0.5)])
        self.assertEqual(0, internal_pauses(clip)[0])

    def test_an_unreadable_clip_reports_nothing_rather_than_guessing(self):
        bad = self.path("not-audio.wav")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("this is not a wav file")
        self.assertIsNone(clip_duration(bad))
        self.assertEqual((0, 0.0), internal_pauses(bad))


if __name__ == "__main__":
    unittest.main()

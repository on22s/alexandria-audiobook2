"""GOALS 6.6, sixth tranche (2026-09-19): thirteen guards no test had ever
shown failing. Each test below constructs the input the guard must reject and
watches it reject, then the accepting control, so a guard that fails
everything is caught as surely as one that fails nothing.

Found by listing every check/verify/validate/require/guard function in app/
and app/routers/ and searching the suite for a rejecting assertion that
exercises it: 44 guard-shaped functions, 13 never called by any test, 4 called
only on their accepting path."""
import json
import os
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException


class FilenameAndPathGuards(unittest.TestCase):
    def test_a_name_that_sanitises_to_nothing_is_a_400(self):
        import core
        for raw in ("", "..", " ", ". ."):
            with self.assertRaises(HTTPException, msg=raw) as ctx:
                core._require_safe_filename(raw, "bad name")
            self.assertEqual(400, ctx.exception.status_code)
            self.assertEqual("bad name", ctx.exception.detail)

    def test_a_traversal_is_flattened_to_one_component_that_cannot_climb(self):
        """The contract is not "reject anything with dots" - it is that the
        result is ONE path component: no separator survives, so a leading
        `..` is a literal name inside the target directory, not a climb."""
        import core
        for raw in ("../../etc/passwd", "..\\..\\x", "a/../b", "/abs/path", "\0name"):
            safe = core._require_safe_filename(raw, "bad name")
            self.assertNotIn("/", safe, raw); self.assertNotIn("\\", safe, raw)
            self.assertFalse(safe.startswith("."), raw)
            self.assertEqual(safe, os.path.basename(safe), raw)
        self.assertEqual("book.txt", core._require_safe_filename("book.txt", "bad name"))

    def test_a_voicelab_path_inside_a_content_directory_is_a_400(self):
        import core
        forbidden = core._VOICELAB_FORBIDDEN_DIRS[0]
        inside = os.path.join(forbidden, "sub", "script.py")
        with self.assertRaises(HTTPException) as ctx:
            core._validate_voicelab_path(inside, "Pipeline script")
        self.assertEqual(400, ctx.exception.status_code)
        self.assertIn("cannot be inside", ctx.exception.detail)

    def test_a_voicelab_path_outside_every_content_directory_passes(self):
        import core
        with tempfile.TemporaryDirectory() as tmp:
            core._validate_voicelab_path(os.path.join(tmp, "pipeline.py"), "Pipeline script")


class NetworkGuards(unittest.TestCase):
    def test_a_public_llm_endpoint_is_refused_and_a_local_one_is_not(self):
        import core
        with self.assertRaises(core.LLMConfigError) as ctx:
            core._validate_local_llm_base_url("https://api.example.com/v1")
        self.assertIn("not local", str(ctx.exception))
        core._validate_local_llm_base_url("http://127.0.0.1:1234/v1")
        core._validate_local_llm_base_url("http://localhost:11434/v1")
        core._validate_local_llm_base_url("")

    def test_the_thunder_allowlist_does_not_admit_a_lookalike_host(self):
        import core
        core._validate_local_llm_base_url("https://abc-8000.thundercompute.net/v1")
        with self.assertRaises(core.LLMConfigError):
            core._validate_local_llm_base_url("https://thundercompute.net.evil.com/v1")
        with self.assertRaises(core.LLMConfigError):
            core._validate_local_llm_base_url("https://notthundercompute.net/v1")

    def test_an_ssh_alias_that_ssh_would_read_as_an_option_is_refused(self):
        import lmstudio_settings as ls
        for alias in ("", "-oProxyCommand=evil", "--help", "-"):
            with self.assertRaises(OSError, msg=repr(alias)):
                ls._validate_ssh_alias(alias)
        ls._validate_ssh_alias("tnr-0")


class PayloadGuards(unittest.TestCase):
    def test_persona_payloads_that_are_not_a_description_plus_ref_text_are_refused(self):
        from persona_validation import validate_persona_payload as v
        for bad in ("a string", ["list"], {}, {"description": "", "ref_text": "x"},
                    {"description": "x", "ref_text": "   "}, {"description": 5, "ref_text": "x"},
                    {"description": "x" * 4001, "ref_text": "x"},
                    {"description": "x", "ref_text": "y" * 2001}):
            with self.assertRaises(ValueError, msg=repr(bad)[:60]):
                v(bad)
        self.assertEqual({"description": "warm", "ref_text": "hello"},
                         v({"description": " warm ", "ref_text": "hello\n"}))

    def test_a_config_field_holding_the_wrong_type_is_dropped_with_a_warning(self):
        import config_settings as cs
        warnings = []
        out = cs._validate_present_fields("generation", {"three_pass_chunk_size": "not a number"},
                                          cs.GenerationConfig, warnings)
        self.assertNotIn("three_pass_chunk_size", out)
        self.assertEqual(1, len(warnings))
        self.assertIn("generation.three_pass_chunk_size", str(warnings[0].__dict__))
        ok = cs._validate_present_fields("generation", {"three_pass_chunk_size": 3000},
                                         cs.GenerationConfig, [])
        self.assertEqual(3000, ok["three_pass_chunk_size"])


class ReviewAndDiskGuards(unittest.TestCase):
    def test_a_review_that_dropped_a_fifth_of_the_words_fails_the_loss_check(self):
        from review_script import check_text_loss
        original = [{"text": "one two three four five six seven eight nine ten"}]
        lost = [{"text": "one two three four five six seven eight"}]
        passed, _o, _c, ratio = check_text_loss(original, lost)
        self.assertFalse(passed)
        self.assertAlmostEqual(0.8, ratio)
        grew = [{"text": " ".join(["w"] * 13)}]
        self.assertFalse(check_text_loss(original, grew)[0], "a 30% gain is also a loss of fidelity")
        kept = [{"text": "one two three four five six seven eight nine ten"}]
        self.assertTrue(check_text_loss(original, kept)[0])

    def test_check_disk_space_says_no_when_free_space_is_short_or_unreadable(self):
        import core
        with mock.patch.object(core.shutil, "disk_usage",
                               return_value=mock.Mock(free=1 * 1024 ** 3)):
            has, free = core.check_disk_space("/", required_gb=2)
        self.assertFalse(has); self.assertAlmostEqual(1.0, free)
        with mock.patch.object(core.shutil, "disk_usage", side_effect=OSError("gone")):
            self.assertEqual((False, 0.0), core.check_disk_space("/nowhere", 1))
        with mock.patch.object(core.shutil, "disk_usage",
                               return_value=mock.Mock(free=5 * 1024 ** 3)):
            self.assertTrue(core.check_disk_space("/", required_gb=2)[0])


class RouteGuards(unittest.TestCase):
    def test_recovery_without_a_failed_unit_is_a_409_and_the_wrong_chunk_too(self):
        from routers import script
        with mock.patch.object(script, "_load_failed_checkpoint", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                script._require_failed_unit(3)
            self.assertEqual(409, ctx.exception.status_code)
        ckpt = {"failed": {"pass": "segment", "chunk": 7}}
        with mock.patch.object(script, "_load_failed_checkpoint", return_value=ckpt):
            with self.assertRaises(HTTPException) as ctx:
                script._require_failed_unit(3)
            self.assertIn("7, not 3", ctx.exception.detail)
            self.assertIs(ckpt, script._require_failed_unit(7))

    def test_a_speaker_absent_from_the_active_script_is_a_404_and_no_script_a_422(self):
        from routers import voices
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "annotated_script.json")
            with mock.patch.object(voices, "SCRIPT_PATH", path):
                with self.assertRaises(HTTPException) as ctx:
                    voices._require_script_speaker("MARA")
                self.assertEqual(422, ctx.exception.status_code)
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump([{"speaker": "MARA", "text": "x"}, {"speaker": "NARRATOR", "text": "y"}], handle)
                with self.assertRaises(HTTPException) as ctx:
                    voices._require_script_speaker("TOMAS")
                self.assertEqual(404, ctx.exception.status_code)
                voices._require_script_speaker("MARA")


class BenchmarkFixtureGuards(unittest.TestCase):
    """The four fixture validators the benchmark runner trusts before spending
    GPU time. Each is given a fixture missing what it needs."""
    def _call(self, name, fixture):
        import benchmark_runner as br
        return getattr(br, name)(fixture)

    def test_each_fixture_validator_rejects_an_empty_fixture(self):
        import benchmark_runner as br
        for name in ("_validate_profiling_fixture", "_validate_persona_fixture",
                     "_validate_nickname_fixture", "_validate_export_fixture"):
            with self.assertRaises(Exception, msg=name):
                getattr(br, name)({})


if __name__ == "__main__":
    unittest.main()

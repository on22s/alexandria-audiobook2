"""Sound inventory admission with portable registry fixtures and real consumers."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from export_staging_retirement import DIRECTORY as REGISTRY
from sound_design_candidates import list_sound_candidates
from sound_design_rendition_cleanup import _stage_names
import sound_design_staging as staging


class SoundInventoryRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / staging.DIRECTORY
        self.directory.mkdir()
        self.candidate = self.directory / "render-retained"
        self.candidate.mkdir()
        (self.candidate / "partial.pcm").write_bytes(b"retained")
        self.registry = self.directory / REGISTRY
        self.registry.mkdir(mode=0o700)

    @contextmanager
    def admitted_registry(self, slots=()):
        registry = MagicMock()
        registry.__enter__.return_value = registry
        registry.__exit__.return_value = False
        registry.slot_names.return_value = list(slots)
        with patch("export_staging_retirement.PosixRetirement", return_value=registry) as admission:
            yield admission

    def test_listing_keeps_candidates_when_empty_registry_is_admitted(self):
        with self.admitted_registry() as admission:
            inventory = list_sound_candidates(self.root)
        self.assertEqual([self.candidate.name], [row["stage"] for row in inventory["candidates"]])
        self.assertEqual(8, inventory["charged_bytes"])
        self.assertTrue(admission.call_count)
        self.assertEqual(b"retained", (self.candidate / "partial.pcm").read_bytes())
        self.assertTrue(self.registry.is_dir())

    def test_cleanup_names_accept_only_validated_empty_registry(self):
        with self.admitted_registry() as admission:
            self.assertEqual([self.candidate.name], _stage_names(self.directory))
        admission.assert_called_once_with(parent=self.directory, create=False)

    def test_cleanup_refuses_retained_private_work_listing_still_charges_it(self):
        slot = self.registry / ("1" * 32)
        slot.mkdir(mode=0o700)
        (slot / "retained.bin").write_bytes(b"private")
        with self.admitted_registry([slot.name]):
            inventory = list_sound_candidates(self.root)
            self.assertEqual(15, inventory["charged_bytes"])
            self.assertEqual([self.candidate.name], [row["stage"] for row in inventory["candidates"]])
            with self.assertRaisesRegex(ValueError, "[Rr]etained.*export"):
                _stage_names(self.directory)
        self.assertEqual(b"private", (slot / "retained.bin").read_bytes())

    def test_registry_refusal_and_unknown_entries_are_not_hidden(self):
        consumers = (lambda: list_sound_candidates(self.root), lambda: _stage_names(self.directory))
        for consume in consumers:
            with self.subTest(consumer=consume), patch(
                    "export_staging_retirement.PosixRetirement",
                    side_effect=ValueError("Invalid registry owner")):
                with self.assertRaisesRegex(ValueError, "Invalid registry owner"):
                    consume()
        unknown = self.directory / ".unknown"
        unknown.mkdir()
        for consume in consumers:
            with self.subTest(consumer=consume), self.admitted_registry():
                with self.assertRaises(ValueError):
                    consume()
        self.assertTrue(unknown.is_dir())

    def test_private_slots_keep_shared_quota_and_count_limits(self):
        slot = self.registry / ("2" * 32)
        slot.mkdir(mode=0o700)
        (slot / "retained.bin").write_bytes(b"private")
        for consume in (lambda: list_sound_candidates(self.root), lambda: _stage_names(self.directory)):
            with self.subTest(consumer=consume), self.admitted_registry([slot.name]), \
                    patch.object(staging, "MAX_STAGES", 1):
                with self.assertRaisesRegex(ValueError, "full"):
                    consume()
        with self.admitted_registry([slot.name]):
            with self.assertRaisesRegex(ValueError, "full"):
                with staging.ensure_sound_render_quota(self.root, max_bytes=14):
                    self.fail("Private export bytes escaped quota")

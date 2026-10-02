"""Native immutable prefix publication, process death and content verification."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import generation_checkpoint_shards as shards


class GenerationCheckpointShardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "book.generation_checkpoint.json"
        self.fingerprint = {"chunk_sha256": [str(i) for i in range(64)]}
        self.chunks = [{"entries": [{"text": "chunk " + str(i) + " words " * 100}],
                        "source_sha256": str(i), "quality": {"passed": True}} for i in range(64)]

    def test_append_serializes_each_chunk_once_and_loads_exact_prefix_without_mutation(self):
        writer = shards.GenerationCheckpointShards(self.path, self.fingerprint)
        written = []
        original = shards.atomic_json_write
        def measure(data, path):
            written.append(len(json.dumps(data, indent=2, ensure_ascii=False).encode()))
            original(data, path)
        original_chunks = copy.deepcopy(self.chunks)
        with patch.object(shards, "atomic_json_write", side_effect=measure):
            for end in range(1, 65):
                writer.save_chunks(self.chunks[:end])
        self.assertEqual(len(written), 65)
        full_bytes = len(json.dumps({"fingerprint": self.fingerprint,
                                   "accepted_chunks": self.chunks}, indent=2).encode())
        self.assertLess(sum(written), full_bytes * 1.5)
        self.assertEqual(shards.load_generation_shard_checkpoint(self.path)["accepted_chunks"], self.chunks)
        self.assertEqual(original_chunks, self.chunks)
        loaded = shards.load_generation_shard_checkpoint(self.path)
        loaded["accepted_chunks"][0]["entries"][0]["text"] = "caller mutation"
        self.assertEqual(writer.chunks[0], self.chunks[0])

    def test_legacy_header_migration_and_changed_prefix_publish_new_epoch(self):
        legacy = {"fingerprint": self.fingerprint, "accepted_chunks": self.chunks[:1]}
        self.path.write_text(json.dumps(legacy))
        self.assertEqual(shards.load_generation_shard_checkpoint(self.path), legacy)
        writer = shards.GenerationCheckpointShards(self.path, self.fingerprint)
        writer.save_chunks(self.chunks[:2])
        old_directory = writer.directory
        changed = copy.deepcopy(self.chunks[:2])
        changed[0]["entries"][0]["text"] = "corrected accepted text"
        writer.save_chunks(changed)
        self.assertNotEqual(writer.directory, old_directory)
        self.assertTrue(old_directory.is_dir())  # recovery evidence is not silently discarded
        self.assertEqual(shards.load_generation_shard_checkpoint(self.path)["accepted_chunks"], changed)

    def test_failed_new_header_keeps_original_checkpoint_and_failed_append_keeps_prior_prefix(self):
        legacy = {"fingerprint": self.fingerprint, "accepted_chunks": self.chunks[:1]}
        self.path.write_text(json.dumps(legacy))
        previous = self.path.read_bytes()
        original = shards.atomic_json_write
        def fail_header(data, path):
            if Path(path) == self.path:
                raise OSError("header publication denied")
            return original(data, path)
        writer = shards.GenerationCheckpointShards(self.path, self.fingerprint)
        with patch.object(shards, "atomic_json_write", side_effect=fail_header):
            with self.assertRaisesRegex(OSError, "header publication"):
                writer.save_chunks(self.chunks[:2])
        self.assertEqual(self.path.read_bytes(), previous)
        writer.save_chunks(self.chunks[:1])
        with patch.object(shards, "atomic_json_write", side_effect=OSError("append denied")):
            with self.assertRaisesRegex(OSError, "append denied"):
                writer.save_chunks(self.chunks[:2])
        self.assertEqual(shards.load_generation_shard_checkpoint(self.path)["accepted_chunks"], self.chunks[:1])

    def test_gap_corrupted_digest_and_unsafe_directory_are_refused_preserving_evidence(self):
        writer = shards.GenerationCheckpointShards(self.path, self.fingerprint)
        writer.save_chunks(self.chunks[:2])
        first = writer.directory / "00000000.json"
        first.unlink()
        with self.assertRaisesRegex(ValueError, "contiguous"):
            shards.load_generation_shard_checkpoint(self.path)
        shards.atomic_json_write({"chunk": self.chunks[0], "sha256": "wrong"}, str(first))
        evidence = first.read_bytes()
        with self.assertRaisesRegex(ValueError, "digest"):
            writer.save_chunks(self.chunks[:3])
        self.assertEqual(first.read_bytes(), evidence)
        self.path.write_text(json.dumps({"storage": shards.STORAGE, "directory": "../escape"}))
        with self.assertRaisesRegex(ValueError, "ownership"):
            shards.load_generation_shard_checkpoint(self.path)

    def test_native_process_exit_after_committed_append_recovers_without_header_rewrite(self):
        writer = shards.GenerationCheckpointShards(self.path, self.fingerprint)
        writer.save_chunks(self.chunks[:1])
        header = self.path.read_bytes()
        code = """import json,os,sys
from generation_checkpoint_shards import GenerationCheckpointShards
w=GenerationCheckpointShards(sys.argv[1],json.loads(sys.argv[2]))
w.save_chunks(json.loads(sys.argv[3]));os._exit(93)
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.path), json.dumps(self.fingerprint),
                                json.dumps(self.chunks[:2])], capture_output=True, text=True)
        self.assertEqual(child.returncode, 93, child.stderr)
        self.assertEqual(self.path.read_bytes(), header)
        self.assertEqual(shards.load_generation_shard_checkpoint(self.path)["accepted_chunks"], self.chunks[:2])

    def test_clear_retires_owned_epochs_and_rejects_foreign_or_linked_directories(self):
        writer = shards.GenerationCheckpointShards(self.path, self.fingerprint)
        writer.save_chunks(self.chunks[:1])
        first = writer.directory
        writer.save_chunks([])
        second = writer.directory
        other = self.path.with_name(self.path.name + ".parts-keep")
        other.mkdir()
        (other / "keep").write_text("foreign")
        shards.remove_generation_shard_checkpoint(self.path)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertFalse(self.path.exists())
        self.assertEqual((other / "keep").read_text(), "foreign")
        linked = self.path.with_name(self.path.name + ".parts-" + "f" * 32)
        linked.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ownership"):
            shards.remove_generation_shard_checkpoint(self.path)
        self.assertEqual((other / "keep").read_text(), "foreign")

    def test_production_save_rejects_writer_bound_to_other_fingerprint_or_path(self):
        import generate_script as generation
        output = str(self.path.parent / "book")
        writer = shards.GenerationCheckpointShards(generation.get_generation_checkpoint_path(output), self.fingerprint)
        for path, fingerprint in ((output + "other", self.fingerprint), (output, {"other": True})):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "different run"):
                generation.save_generation_checkpoint(path, fingerprint, self.chunks[:1], writer=writer)
        self.assertFalse(Path(generation.get_generation_checkpoint_path(output)).exists())


if __name__ == "__main__":
    unittest.main()

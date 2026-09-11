import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from routers.script import build_generate_script_command, get_script_recovery_manifest
from three_pass_generate import get_output_paths, three_pass_manifest_path


class GenerationDispatchTests(unittest.TestCase):
    def test_single_generation_uses_production_three_pass_fallback(self):
        command = build_generate_script_command("book.txt")

        self.assertEqual(sys.executable, command[0])
        self.assertEqual("-u", command[1])
        self.assertEqual("three_pass_generate.py", os.path.basename(command[2]))
        self.assertEqual("book.txt", command[3])
        self.assertEqual(
            ["--pass2-on-exhaustion", "fallback"], command[4:])

    def test_reasoning_effort_is_passed_to_three_pass_when_set(self):
        with_effort = build_generate_script_command("book.txt", reasoning_effort="low")
        self.assertEqual(["--reasoning-effort", "low"],
                         with_effort[with_effort.index("--reasoning-effort"):][:2])
        self.assertNotIn("--reasoning-effort", build_generate_script_command("book.txt"))
        self.assertNotIn("--reasoning-effort",
                         build_generate_script_command("book.txt", reasoning_effort=None))

    def test_batch_generation_uses_same_dispatch_with_output(self):
        command = build_generate_script_command(
            "book.txt", output_path="scripts/book.json",
            strip_front_matter=False)

        self.assertEqual("three_pass_generate.py", os.path.basename(command[2]))
        self.assertEqual(
            ["book.txt", "--pass2-on-exhaustion", "fallback",
             "--output", "scripts/book.json", "--no-strip-front-matter"],
            command[3:])

    def test_narrator_metadata_uses_same_dispatch_and_is_normalized(self):
        command = build_generate_script_command(
            "book.txt", first_person_narrator="  Alexis   Ivanovitch ")

        self.assertEqual(
            ["--first-person-narrator", "ALEXIS IVANOVITCH"], command[-2:])

    def test_placeholder_narrator_is_rejected_before_dispatch(self):
        with self.assertRaisesRegex(ValueError, "exact character name"):
            build_generate_script_command(
                "book.txt", first_person_narrator="NARRATOR")

    def test_default_output_and_stale_chunks_share_runtime_data_directory(self):
        output, chunks = get_output_paths("/runtime/data")

        self.assertEqual("/runtime/data/annotated_script.json", output)
        self.assertEqual("/runtime/data/chunks.json", chunks)

    def test_explicit_batch_output_does_not_clear_active_book_chunks(self):
        output, chunks = get_output_paths(
            "/runtime/data", "/runtime/data/scripts/book.json")

        self.assertEqual("/runtime/data/scripts/book.json", output)
        self.assertIsNone(chunks)

    def test_only_failed_or_incomplete_manifest_is_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "annotated_script.json")
            manifest_path = three_pass_manifest_path(output)
            state_path = os.path.join(directory, "state.json")
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write('{"input_file_path":"book.txt",'
                             '"script_generation_input_file":"book.txt"}')
            with patch("routers.script.SCRIPT_PATH", output), \
                 patch("routers.script.DATA_DIR", directory):
                with open(manifest_path, "w", encoding="utf-8") as handle:
                    handle.write('{"status":"complete"}')
                self.assertIsNone(get_script_recovery_manifest())

                with open(manifest_path, "w", encoding="utf-8") as handle:
                    handle.write('{"status":"failed","failed_pass":"attribute"}')
                self.assertEqual(
                    "attribute", get_script_recovery_manifest()["failed_pass"])

                with open(state_path, "w", encoding="utf-8") as handle:
                    handle.write('{"input_file_path":"new-book.txt",'
                                 '"script_generation_input_file":"book.txt"}')
                self.assertIsNone(get_script_recovery_manifest())


if __name__ == "__main__":
    unittest.main()

"""Reserved edges cannot launder a proposal into a protected terminal alias."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import find_nicknames
from speaker_identity import (get_resolved_speaker_merge_map, get_safe_alias_proposals,
                              get_validated_alias_graph)
from tests.test_support import assert_file_lock_released


class ReservedEdgeDedupeTests(unittest.TestCase):
    def test_proposal_roots_stop_at_reserved_edges(self):
        terminal = {'OLD': 'UNKNOWN', 'UNKNOWN': 'MARA'}
        for middle in ('UNKNOWN', 'NARRATOR', 'CROWD'):
            human = {'OLD': middle, middle: 'MARA'}
            for flatten in (True, False):
                with self.subTest(middle=middle, flatten=flatten):
                    safe = get_safe_alias_proposals(
                        [{'NEW': 'OLD'}], ['MARA', 'BOB'], human, flatten=flatten)
                    self.assertEqual({}, safe)
                    self.assertEqual(human, get_validated_alias_graph(human))
                    self.assertEqual({}, get_resolved_speaker_merge_map(human))

    def test_valid_unprotected_chain_is_still_accepted(self):
        human = {'OLD': 'ALICE'}
        for flatten, expected in ((True, {'NEW': 'ALICE'}), (False, {'NEW': 'OLD'})):
            with self.subTest(flatten=flatten):
                self.assertEqual(expected, get_safe_alias_proposals(
                    [{'NEW': 'OLD'}], ['MARA', 'ALICE'], human, flatten=flatten))

    def test_bulk_dedupe_writes_no_protected_terminal_alias_or_script_edit(self):
        entries = [{'speaker': name, 'text': 'Line ' + name}
                   for name in ('NEW', 'OLD', 'MARA', 'BOB')]
        for middle in ('UNKNOWN', 'NARRATOR', 'CROWD'):
            with self.subTest(middle=middle), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script = root / 'script.json'
                registry = root / 'aliases.json'
                script.write_text(json.dumps(entries))
                source_before = script.read_bytes()
                human = {'OLD': middle, middle: 'MARA'}
                registry.write_text(json.dumps(human))
                registry_before = registry.read_bytes()
                with patch.object(find_nicknames, '_chunk_evidence', return_value=[[]]), \
                     patch.object(find_nicknames, 'call_llm_for_object',
                                  return_value={'aliases': {'NEW': 'OLD'}}):
                    aliases, _ = find_nicknames.find_nicknames(
                        object(), 'fixture-model', entries, existing_aliases=human)
                self.assertEqual({}, aliases)
                saved = find_nicknames.save_discovered_aliases(
                    str(registry), aliases, roster=['NEW', 'OLD', 'MARA', 'BOB'])
                self.assertEqual(human, saved)
                self.assertEqual(registry_before, registry.read_bytes())
                self.assertEqual(source_before, script.read_bytes())
                assert_file_lock_released(str(registry))

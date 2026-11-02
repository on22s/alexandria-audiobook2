"""Casting library catalogue and marks (#1532), on a temporary designed_voices tree."""
from pathlib import Path
import json
import os
import tempfile
import threading
import unittest

import casting_catalog as cc
from utils import character_voice_seed


class CastingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'designed_voices'
        self.dir.mkdir()

    def persona(self, book_key, folder, meta=None, audio=True, preview=None):
        voice = self.dir / 'persona' / book_key / folder
        voice.mkdir(parents=True)
        if audio:
            (voice / 'preview.wav').write_bytes(b'RIFF')
        meta = {'book_id': 'DH', 'speaker': 'ABEL CARNEDAS', 'description': 'Young male; calm and academic.',
                'ref_text': 'Being able to use 1-star magic already is amazing!',
                'preview': preview or f'designed_voices/persona/{book_key}/{folder}/preview.wav', **(meta or {})}
        (voice / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')
        return voice

    def catalog(self):
        return cc.get_casting_catalog(str(self.dir))

    def test_persona_voice_has_id_url_tags_and_audio(self):
        self.persona('dh_1', 'abel_1')
        voice = self.catalog()['voices'][0]
        self.assertEqual('persona/dh_1/abel_1', voice['id'])
        self.assertEqual(('persona', 'DH', 'ABEL CARNEDAS'), (voice['source'], voice['book_id'], voice['name']))
        self.assertEqual('/designed_voices/persona/dh_1/abel_1/preview.wav', voice['preview_url'])
        self.assertTrue(voice['audio_present'])
        self.assertEqual('Male', voice['tags']['gender'])
        self.assertEqual(['calm', 'academic'], voice['tags']['tones'])

    def test_designer_voices_are_listed_from_the_manifest(self):
        (self.dir / 'v1.wav').write_bytes(b'RIFF')
        (self.dir / 'manifest.json').write_text(json.dumps([
            {'id': 'v1', 'name': 'Lady Narrator', 'description': 'A warm, mature female voice.',
             'sample_text': 'The library stood at the crossroads.', 'filename': 'v1.wav'},
            {'id': 'v2', 'name': 'Missing', 'description': '', 'sample_text': '', 'filename': '../escape.wav'},
            'not a voice']), encoding='utf-8')
        catalog = self.catalog()
        by_id = {v['id']: v for v in catalog['voices']}
        self.assertTrue(by_id['designer/v1']['audio_present'])
        self.assertEqual('/designed_voices/v1.wav', by_id['designer/v1']['preview_url'])
        self.assertEqual('Female', by_id['designer/v1']['tags']['gender'])
        self.assertFalse(by_id['designer/v2']['audio_present'])  # an escaping filename is never served
        self.assertIsNone(by_id['designer/v2']['preview_url'])
        self.assertEqual(1, len(catalog['skipped']))

    def test_unreadable_entries_are_reported_not_invented(self):
        self.persona('dh_1', 'good')
        bad = self.dir / 'persona' / 'dh_1' / 'broken'
        bad.mkdir()
        (bad / 'meta.json').write_text('{not json', encoding='utf-8')
        big = self.dir / 'persona' / 'dh_1' / 'huge'
        big.mkdir()
        (big / 'meta.json').write_text(json.dumps({'description': 'x' * (cc.MAX_META_BYTES + 10)}), encoding='utf-8')
        listed = self.dir / 'persona' / 'dh_1' / 'list'
        listed.mkdir()
        (listed / 'meta.json').write_text('[1, 2]', encoding='utf-8')
        catalog = self.catalog()
        self.assertEqual(['persona/dh_1/good'], [v['id'] for v in catalog['voices']])
        self.assertEqual({'persona/dh_1/broken', 'persona/dh_1/huge', 'persona/dh_1/list'},
                         {s['id'] for s in catalog['skipped']})

    def test_audio_is_only_trusted_at_its_own_folder(self):
        self.persona('dh_1', 'no_audio', audio=False)
        self.persona('dh_1', 'other')  # real audio another meta.json could point at
        self.persona('dh_1', 'elsewhere', audio=False, preview='designed_voices/persona/dh_1/other/preview.wav')
        (Path(self.tmp.name) / 'secret.wav').write_bytes(b'RIFF')
        self.persona('dh_1', 'outside', audio=False, preview='designed_voices/../secret.wav')
        voices = {v['id']: v for v in self.catalog()['voices']}
        self.assertTrue(voices['persona/dh_1/other']['audio_present'])
        for voice_id in ('persona/dh_1/no_audio', 'persona/dh_1/elsewhere', 'persona/dh_1/outside'):
            with self.subTest(voice=voice_id):
                self.assertFalse(voices[voice_id]['audio_present'])
                self.assertIsNone(voices[voice_id]['preview_url'])
                self.assertIsNone(cc.get_voice_clone_config(voices[voice_id], 'ADAM'))

    @unittest.skipIf(os.name == 'nt', 'symlink creation needs privileges on Windows')
    def test_symlinked_voice_folder_outside_the_tree_is_skipped(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (outside / 'meta.json').write_text(json.dumps({'speaker': 'X', 'description': 'male'}), encoding='utf-8')
        (self.dir / 'persona' / 'dh_1').mkdir(parents=True)
        os.symlink(outside, self.dir / 'persona' / 'dh_1' / 'alias', target_is_directory=True)
        catalog = self.catalog()
        self.assertEqual([], catalog['voices'])
        self.assertEqual(['persona/dh_1/alias'], [s['id'] for s in catalog['skipped']])

    def test_clone_config_is_the_same_entry_persona_generation_writes(self):
        self.persona('dh_1', 'abel_1')
        voice = self.catalog()['voices'][0]
        config = cc.get_voice_clone_config(voice, 'ADAM')
        expected = {'type': 'clone', 'persona_status': 'generated',
                    'ref_audio': 'designed_voices/persona/dh_1/abel_1/preview.wav',
                    'ref_text': voice['ref_text'], 'description': voice['description'],
                    'character_style': voice['description'], 'seed': character_voice_seed('ADAM')}
        self.assertEqual(expected, config)
        self.assertEqual(expected, cc.get_persona_clone_entry(expected['ref_audio'], voice['ref_text'],
                                                              voice['description'], 'ADAM'))
        source = (Path(cc.__file__).parent / 'generate_personas.py').read_text(encoding='utf-8')
        self.assertIn('get_persona_clone_entry(relative_path, ref_text, description, speaker)', source)

    def test_catalog_voice_lookup_rejects_malformed_ids(self):
        self.persona('dh_1', 'abel_1')
        self.assertIsNotNone(cc.get_catalog_voice(str(self.dir), 'persona/dh_1/abel_1'))
        for bad in ('persona/dh_1', 'persona/../x/y', '../persona/dh_1/abel_1', 'designer/a/b', '', None):
            with self.subTest(bad=bad):
                self.assertIsNone(cc.get_catalog_voice(str(self.dir), bad))


class CastingMarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'casting_marks.json')

    def test_marks_merge_and_persist(self):
        cc.save_casting_mark(self.path, 'persona/a/b', {'status': 'shortlist'})
        mark = cc.save_casting_mark(self.path, 'persona/a/b', {'notes': 'Great for a mentor.', 'tags': ['mentor', 'mentor ']})
        self.assertEqual({'status': 'shortlist', 'notes': 'Great for a mentor.', 'tags': ['mentor']}, mark)
        self.assertEqual(mark, cc.get_casting_marks(self.path)['persona/a/b'])

    def test_invalid_patches_name_the_field_and_change_nothing(self):
        cc.save_casting_mark(self.path, 'persona/a/b', {'status': 'callback'})
        before = Path(self.path).read_bytes()
        for patch, field in (({'status': 'maybe'}, 'status'), ({'priority': 'urgent'}, 'priority'),
                             ({'notes': 'x' * (cc.MAX_NOTES + 1)}, 'notes'), ({'notes': 5}, 'notes'),
                             ({'tags': ['']}, 'tags'), ({'tags': ['x' * 41]}, 'tags'),
                             ({'tags': [str(i) for i in range(21)]}, 'tags'), ({'colour': 'red'}, 'status, priority'),
                             ({}, 'status, priority')):
            with self.subTest(patch=str(patch)[:40]), self.assertRaisesRegex(ValueError, field):
                cc.save_casting_mark(self.path, 'persona/a/b', patch)
        self.assertEqual(before, Path(self.path).read_bytes())

    def test_concurrent_writers_lose_no_mark(self):
        ids = [f'persona/book/v{i}' for i in range(12)]
        threads = [threading.Thread(target=cc.save_casting_mark, args=(self.path, i, {'status': 'shortlist'})) for i in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(set(ids), set(cc.get_casting_marks(self.path)))


if __name__ == '__main__':
    unittest.main()

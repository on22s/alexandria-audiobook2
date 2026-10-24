"""Shared M4B metadata preserves source pause ownership and UTF-8 bounds."""
import unittest
from m4b_metadata import get_m4b_source_chapters, get_m4b_tags, get_m4b_metadata_text
from project import ProjectManager


class M4BMetadataTests(unittest.TestCase):
    def test_chapters_assign_intervening_pause_on_exact_source_clock(self):
        plan = dict(frames=96000,frame_rate=24000,source_frame_ranges=[[0,24000],[36000,48000],[72000,96000]])
        self.assertEqual([('One',0,3),('Two',3,4)],
            get_m4b_source_chapters(plan,[('One',0,1),('Two',2,2)],3))
        with self.assertRaisesRegex(ValueError,'source-frame ranges'):
            get_m4b_source_chapters(plan,[('One',0,1)],2)

    def test_metadata_cap_counts_escaped_utf8_without_truncation(self):
        tags=get_m4b_tags({'title':'雪=title;#\\\nline','author':'Author'})
        chapters=[('雪=chapter',0,1.2345)]
        text=get_m4b_metadata_text(tags,chapters,ProjectManager._escape_ffmeta)
        size=len(text.encode('utf-8'))
        self.assertEqual(text,get_m4b_metadata_text(tags,chapters,ProjectManager._escape_ffmeta,max_bytes=size))
        with self.assertRaisesRegex(ValueError,'allowance'):
            get_m4b_metadata_text(tags,chapters,ProjectManager._escape_ffmeta,max_bytes=size-1)
        self.assertIn('END=1234',text)

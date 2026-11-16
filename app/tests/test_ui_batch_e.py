"""Batch E regression tests."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from project import ProjectManager
from routers import editor

STATIC = Path(__file__).resolve().parent.parent / 'static'


class BatchEUndoTests(unittest.TestCase):
    def test_changed_revision_refuses_restore_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = ProjectManager(folder)
            manager.save_chunks([{'id': index, 'speaker': 'SYNTHETIC', 'text': f'Synthetic sentence {index}.', 'status': 'pending'} for index in range(5)])
            with patch.object(editor, 'project_manager', manager), patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), patch.object(editor, '_deleted_chunk_receipts', {}):
                deleted, _, token = editor.apply_chunk_deletion(1)
                rows = manager.load_chunks()
                rows[0]['text'] = 'SYNTHETIC incompatible edit.'
                manager.save_chunks(rows)
                before = (root / 'chunks.json').read_bytes()
                with self.assertRaises(HTTPException) as raised:
                    editor.apply_chunk_restore(editor.ChunkRestoreRequest(chunk=deleted, at_index=1, undo_token=token))
                self.assertEqual(409, raised.exception.status_code)
                self.assertEqual(before, (root / 'chunks.json').read_bytes())

    def test_multiple_deletes_restore_lifo_after_eight_seconds(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = ProjectManager(folder)
            manager.save_chunks([{'id': index, 'speaker': 'SYNTHETIC', 'text': f'Synthetic sentence {index}.', 'status': 'pending'} for index in range(5)])
            original = manager.load_chunks()
            with patch.object(editor, 'project_manager', manager), patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), patch.object(editor, '_deleted_chunk_receipts', {}):
                first, _, first_token = editor.apply_chunk_deletion(1)
                second, _, second_token = editor.apply_chunk_deletion(2)
                with patch.object(editor.time, 'monotonic', return_value=editor._deleted_chunk_receipts[first_token]['created'] + 120):
                    self.assertEqual([first, second], editor._get_undo_take_rows())
                    editor.apply_chunk_restore(editor.ChunkRestoreRequest(chunk=second, at_index=2, undo_token=second_token))
                    editor.apply_chunk_restore(editor.ChunkRestoreRequest(chunk=first, at_index=1, undo_token=first_token))
                self.assertEqual([row['text'] for row in original], [row['text'] for row in manager.load_chunks()])
                self.assertEqual([row['uid'] for row in original], [row['uid'] for row in manager.load_chunks()])


    def test_book_generation_and_expired_receipts_refuse_without_writing(self):
        for change in ('book', 'generation', 'expiry'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                manager = ProjectManager(folder)
                manager.save_chunks([{'id': index, 'speaker': 'SYNTHETIC', 'text': f'Synthetic {index}.'} for index in range(3)])
                state = {'active_book_id': 'synthetic-a', 'book_generation': 'generation-a'}
                (root / 'state.json').write_text(json.dumps(state))
                with patch.object(editor, 'project_manager', manager), patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), patch.object(editor, '_deleted_chunk_receipts', {}):
                    deleted, _, token = editor.apply_chunk_deletion(1)
                    if change == 'expiry':
                        editor._deleted_chunk_receipts[token]['created'] -= 1801
                    else:
                        state['active_book_id' if change == 'book' else 'book_generation'] = 'synthetic-changed'
                        (root / 'state.json').write_text(json.dumps(state))
                    before = (root / 'chunks.json').read_bytes()
                    with self.assertRaises(HTTPException) as raised:
                        editor.apply_chunk_restore(editor.ChunkRestoreRequest(chunk=deleted, at_index=1, undo_token=token))
                    self.assertEqual(409, raised.exception.status_code)
                    self.assertEqual(before, (root / 'chunks.json').read_bytes())

    def test_out_of_order_restore_refuses_then_latest_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = ProjectManager(folder)
            manager.save_chunks([{'id': index, 'speaker': 'SYNTHETIC', 'text': f'Synthetic {index}.'} for index in range(5)])
            with patch.object(editor, 'project_manager', manager), patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), patch.object(editor, '_deleted_chunk_receipts', {}):
                first, _, first_token = editor.apply_chunk_deletion(1)
                second, _, second_token = editor.apply_chunk_deletion(2)
                before = (root / 'chunks.json').read_bytes()
                with self.assertRaises(HTTPException) as raised:
                    editor.apply_chunk_restore(editor.ChunkRestoreRequest(chunk=first, at_index=1, undo_token=first_token))
                self.assertEqual(409, raised.exception.status_code)
                self.assertEqual(before, (root / 'chunks.json').read_bytes())
                self.assertEqual(4, len(editor.apply_chunk_restore(editor.ChunkRestoreRequest(chunk=second, at_index=2, undo_token=second_token))))


class BatchEMarkupTests(unittest.TestCase):
    def test_voices_have_essential_first_disclosures(self):
        html = (STATIC / 'index.html').read_text(encoding='utf8')
        self.assertIn('id="voices-advanced"', html)
        self.assertLess(html.index('id="voices-list"'), html.index('id="voices-advanced"'))
        core = (STATIC / 'js/app-core.js').read_text(encoding='utf8')
        self.assertIn('voice-card-advanced', core)
        self.assertIn('getStateVoiceCardsMarkup(voices) + getVoiceSeedRepairMarkup(ownSnapshot)', core)

    def test_required_feedback_is_associated_before_work(self):
        core = (STATIC / 'js/app-core.js').read_text(encoding='utf8')
        self.assertIn('function setFieldFeedback(', core)
        self.assertIn("setFieldFeedback('design-description'", (STATIC / 'js/app-scripts.js').read_text(encoding='utf8'))
        self.assertIn("setFieldFeedback('prep-audio-file'", (STATIC / 'js/app-workbench.js').read_text(encoding='utf8'))
        self.assertIn('new URL(baseUrl)', core)

    def test_real_field_feedback_preserves_descriptions_and_clears_on_edit(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const nodes=new Map(),listeners=new Map();let focused=null;
const field={attributes:{'aria-describedby':'existing-help'},getAttribute(name){return this.attributes[name]||null;},setAttribute(name,value){this.attributes[name]=value;},removeAttribute(name){delete this.attributes[name];},addEventListener(name,callback){listeners.set(name,callback);},insertAdjacentElement(position,node){assert.equal(position,'afterend');nodes.set(node.id,node);},focus(){focused=this;}};
nodes.set('required',field);
const document={getElementById:id=>nodes.get(id)||null,createElement:()=>({hidden:false,setAttribute(name,value){this[name]=value;},replaceChildren(message){this.message=message;}})};
const start=source.indexOf('function setFieldFeedback(');assert(start>=0);
const context={document,Set};vm.createContext(context);vm.runInContext(source.slice(start,source.indexOf('function showConfirm(',start)),context);
const message={textContent:'SYNTHETIC required feedback'};context.setFieldFeedback('required',message);
const error=nodes.get('required-error');assert.equal(error.role,'alert');assert.strictEqual(error.message,message);assert.strictEqual(focused,field);assert.equal(field.attributes['aria-invalid'],'true');assert.equal(field.attributes['aria-describedby'],'existing-help required-error');assert.equal(error.hidden,false);
listeners.get('input')();assert.equal(field.getAttribute('aria-invalid'),null);assert.equal(error.hidden,true);assert.equal(field.attributes['aria-describedby'],'existing-help required-error');
context.setFieldFeedback('required',message);listeners.get('change')();assert.equal(error.hidden,true);assert.equal(nodes.size,2);
'''
        result = subprocess.run(['node', '-e', script, str(STATIC / 'js/app-core.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_editor_yields_between_bounded_batches(self):
        core = (STATIC / 'js/app-core.js').read_text(encoding='utf8')
        self.assertIn('const renderBatchSize = 20;', core)
        self.assertIn('await new Promise(resolve => setTimeout(resolve, 0))', core)

    def test_durable_undo_is_outside_editor_drawer(self):
        html = (STATIC / 'index.html').read_text(encoding='utf8')
        self.assertLess(html.index('id="editor-undo-delete"'), html.index('id="editor-tool-drawer"'))

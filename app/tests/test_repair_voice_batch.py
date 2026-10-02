import builtins
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import repair_voice_config as repair
import speaker_identity as identities


class RepairVoiceBatchTests(unittest.TestCase):
    def test_apply_reports_only_actual_voice_changes_and_keeps_ambiguity(self):
        for mixed, force, expected in ((False,False,0),(True,False,3),(True,True,203)):
            with self.subTest(mixed=mixed,force=force), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);config=root/'voices.json';aliases=root/'aliases.json';script=root/'script.json'
                voices={'ANNA':{'type':'lora','adapter_id':'first'}, 'Anna':{'type':'lora','adapter_id':'second'}}
                entries=[{'speaker':'ANNA'}]*201+[{'speaker':'Anna'}]*200
                if mixed:
                    voices.update({'BOB':{'type':'lora','adapter_id':'bob'},'Bob':{'type':'custom','voice':'Ryan'}})
                    entries += [{'speaker':'Bob'}]*3
                original={'characters':voices,'keep':17};config.write_text(json.dumps(original));before=config.read_bytes()
                aliases.write_text('{}');script.write_text(json.dumps(entries))
                argv=['repair_voice_config.py','--config',str(config),'--aliases',str(aliases),'--script',str(script),'--apply']
                if force:argv.append('--force-ambiguous')
                output=io.StringIO()
                with patch('sys.argv',argv),contextlib.redirect_stdout(output):repair.main()
                self.assertIn(f'merged {expected} lines',output.getvalue())
                result=json.loads(config.read_text());self.assertEqual(17,result['keep'])
                if not force:self.assertEqual(voices['Anna'],result['characters']['Anna'])
                else:self.assertEqual(voices['ANNA'],result['characters']['Anna'])
                if mixed:self.assertEqual(voices['BOB'],result['characters']['Bob'])
                backups=list(root.glob('voices.json.bak-*'));self.assertEqual(1,len(backups));self.assertEqual(before,backups[0].read_bytes())

    def test_alias_validation_and_label_index_prepared_once_for_whole_config(self):
        aliases={f'Alias{i}':f'Person{i}' for i in range(300)}
        aliases.update({'Bri-chan':'BRI','Bri chan':'Bri'})
        config={f'Alias{i}':{'type':'custom','voice':'Ryan'} for i in range(300)}
        config.update({'BRI':{'type':'lora','adapter_id':'chosen'},'Bri chan':{'type':'custom','voice':'Ryan'}})
        before=copy.deepcopy((config,aliases))
        with patch.object(identities,'get_validated_alias_map',wraps=identities.get_validated_alias_map) as validate:
            splits=repair.find_splits(config,aliases,{})
        self.assertEqual(1,validate.call_count)
        self.assertEqual(['BRI'],[s['canonical'] for s in splits])
        self.assertEqual(before,(config,aliases))
        self.assertEqual('BRI',repair.canonical('Bri chan',aliases))
        self.assertEqual('!',repair.canonical('!',{'?':'OTHER'}))

    def test_alias_and_script_handles_close_on_success_and_json_failure(self):
        for malformed in (None,'aliases','script'):
            with self.subTest(malformed=malformed),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);config=root/'voices.json';aliases=root/'aliases.json';script=root/'script.json'
                config.write_text('{}');aliases.write_text('{' if malformed=='aliases' else '{}');script.write_text('{' if malformed=='script' else '[]')
                handles=[];real_open=builtins.open
                def open_file(path,*args,**kwargs):
                    handle=real_open(path,*args,**kwargs)
                    if Path(path) in (config,aliases,script):handles.append(handle)
                    return handle
                with patch('sys.argv',['repair_voice_config.py','--config',str(config),'--aliases',str(aliases),'--script',str(script)]),patch('builtins.open',side_effect=open_file),contextlib.redirect_stdout(io.StringIO()):
                    if malformed:
                        with self.assertRaises(json.JSONDecodeError):repair.main()
                    else:repair.main()
                self.assertTrue(handles)
                try:self.assertTrue(all(h.closed for h in handles),[(h.name,h.closed) for h in handles])
                finally:
                    for h in handles:h.close()

    def test_prepared_index_has_same_sorted_unicode_and_empty_matching_policy(self):
        labels=['Bri-chan','Bri chan','JOSE\u0301','JOSÉ','Straße','STRASSE','!','?','Bri_chan']
        index=identities.get_speaker_label_index(reversed(labels))
        for query in ['bri chan','Bri-chan','Jose\u0301','José','straße','STRASSE','!','?','Bri_chan','Other','']:
            with self.subTest(query=query):
                key=identities._identity_key(query)
                expected=next((label for label in sorted(labels) if key and identities._identity_key(label)==key),None)
                self.assertEqual(expected,index.get(key))
                self.assertEqual(expected,identities.resolve_speaker_label(query,labels))

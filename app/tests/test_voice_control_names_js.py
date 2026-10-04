"""Validate native voice-card labels, including attribute-escaping rejects."""
from html.parser import HTMLParser
from pathlib import Path
import json
import os
import subprocess
import unittest

SOURCE = Path(os.environ.get('VOICE_NAMES_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-core.js'))

class Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = []
        self.labels = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('input', 'select', 'button'):
            self.controls.append((tag, attrs))
        if tag == 'label':
            self.labels.append(attrs.get('for'))

class VoiceControlNamesJsTests(unittest.TestCase):
    def test_card_names_and_radio_labels_preserve_exact_character(self):
        name = 'RUDY "< & 日本語'
        script = r'''const fs=require('fs'),vm=require('vm');const s=fs.readFileSync(process.argv[1],'utf8');
const c={window:{_voicesNames:['OTHER']},AVAILABLE_VOICES:['Ryan'],BUILTIN_LORAS:[],
 getLibraryVoiceReference:()=>null,getTraitBadgeHtml:()=>'',getVoiceCandidateMarkup:()=>'',renderStyleTimeline:()=>'',ensembleMembersMarkup:()=>''};
vm.createContext(c);let a=s.indexOf('function escapeHtml(');vm.runInContext(s.slice(a,s.indexOf('// Parse a numeric input',a)),c);
a=s.indexOf('function createVoiceCard(');vm.runInContext(s.slice(a,s.indexOf('// Suggest members',a)),c);
console.log(JSON.stringify(c.createVoiceCard({name:JSON.parse(process.argv[2]),config:{type:'clone',ref_audio:'clip.wav',voice:'Ryan'},traits:{states:[{},{}]}},4)));'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), json.dumps(name)], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        parser = Controls()
        parser.feed(json.loads(result.stdout))
        for tag, attrs in parser.controls:
            if tag in ('input', 'select'):
                self.assertTrue(attrs.get('aria-label'), attrs)
                self.assertIn(name, attrs['aria-label'])
        radios = [attrs for tag, attrs in parser.controls if attrs.get('type') == 'radio']
        self.assertEqual(len(radios), 6)
        self.assertEqual(len({a['id'] for a in radios}), 6)
        for attrs in radios:
            self.assertIn(attrs['id'], parser.labels)
        names = [attrs.get('aria-label') for _, attrs in parser.controls]
        self.assertIn('Play reference audio for ' + name, names)
        self.assertIn('Delete uploaded reference voice for ' + name, names)
        self.assertIn('Load voice changes for ' + name, names)

    def test_cast_and_ensemble_names_preserve_exact_character(self):
        name = 'RUDY "< & 日本語'
        script = r'''const fs=require('fs'),vm=require('vm');const s=fs.readFileSync(process.argv[1],'utf8');
const name=JSON.parse(process.argv[2]),panel={innerHTML:''};const c={window:{_selectedCast:'CAST',_voiceLibrary:{current_characters:[{name:'NARRATOR',line_count:2},{name,line_count:30}]},_voicesNames:[name,'GROUP']},document:{getElementById:()=>panel},_castMatchBadge:()=>'',suggestEnsembleMembers:()=>[]};
vm.createContext(c);function load(a,b){a=s.indexOf(a);vm.runInContext(s.slice(a,s.indexOf(b,a)),c);}
load('function escapeHtml(','// Parse a numeric input');load('function openCastSave()','function reapplyCastSaveThreshold()');
load('function _renderCastMatchRows(','// Apply a cast to the current book');load('function ensembleMembersMarkup(','window.toggleVoiceType =');
c.openCastSave();const save=panel.innerHTML;const apply=c._renderCastMatchRows([{character:name,line_count:30,match:{key:'member',exact:true}}],[{key:'member',name,source:'cast'}]);
console.log(JSON.stringify(save+'<table>'+apply+'</table>'+c.ensembleMembersMarkup('GROUP',[name])));'''
        result = subprocess.run(['node','-e',script,str(SOURCE),json.dumps(name)],text=True,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        parser = Controls()
        parser.feed(json.loads(result.stdout))
        attrs = [a for tag,a in parser.controls if tag in ('input','select')]
        self.assertEqual(len(attrs), 7)
        self.assertTrue(all(a.get('aria-label') for a in attrs))
        names = [a['aria-label'] for a in attrs]
        self.assertIn('Save voice for ' + name + ' to cast', names)
        self.assertIn('Cast voice for ' + name, names)
        self.assertIn('Apply cast voice to ' + name, names)
        self.assertIn('Narrator scope for NARRATOR', names)
        self.assertIn('Include ' + name + ' in voices together for GROUP', names)

    def test_designed_voice_actions_name_exact_resource_and_preserve_arguments(self):
        name = 'RUDY "< & 日本語\'s'
        script = r'''const fs=require('fs'),vm=require('vm');const core=fs.readFileSync(process.argv[1],'utf8'),source=fs.readFileSync(process.argv[2],'utf8'),voice=JSON.parse(process.argv[3]);const elements={};const c={window:{},console,document:{getElementById:id=>elements[id]||(elements[id]={})},API:{get:async()=>[voice]}};vm.createContext(c);let a=core.indexOf('function escapeHtml(');vm.runInContext(core.slice(a,core.indexOf('// Parse a numeric input',a)),c);a=source.indexOf('async function loadDesignedVoices()');vm.runInContext(source.slice(a,source.indexOf('function getDesignerFormSnapshot()',a)),c);c.loadDesignedVoices().then(()=>console.log(JSON.stringify(elements['designed-voices-list'].innerHTML))).catch(e=>{console.error(e);process.exitCode=1;});'''
        voice = {'id':name, 'name':name, 'filename':name+'.wav', 'description':'description'}
        result = subprocess.run(['node','-e',script,str(SOURCE),str(SOURCE.with_name('app-scripts.js')),json.dumps(voice)],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
        parser = Controls()
        parser.feed(json.loads(result.stdout))
        actions = [a for tag,a in parser.controls if tag == 'button']
        self.assertEqual(3,len(actions))
        for action, verb, value in zip(actions, ['Play','Edit','Delete'], [voice['filename'],name,name]):
            self.assertEqual(verb+' designed voice '+name,action.get('aria-label'))
            args = action['onclick'].split('(',1)[1].rsplit(')',1)[0]
            if verb == 'Delete':
                args = args.rsplit(', this',1)[0]
            self.assertEqual(value,json.loads(args))

    def test_stop_preview_control_dispatches_native_pause_without_deleting_media(self):
        parser = Controls()
        parser.feed((SOURCE.parent.parent / 'index.html').read_text())
        actions = [a for tag,a in parser.controls if tag=='button' and a.get('onclick')=='stopDesignedVoicePlayback()']
        self.assertEqual(1,len(actions))
        script = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const saved={src:'saved.wav',pause(){this.pauses=(this.pauses||0)+1;}},preview={src:'preview.wav',pause(){this.pauses=(this.pauses||0)+1;}};const c={window:{_designedPlaybackAudio:saved},document:{getElementById:()=>preview}};vm.createContext(c);let a=s.indexOf('function stopDesignedVoicePlayback(');vm.runInContext(s.slice(a,s.indexOf('function invalidateDesignerWork()',a)),c);vm.runInContext(process.argv[2],c);assert.strictEqual(saved.pauses,1);assert.strictEqual(preview.pauses,1);assert.strictEqual(saved.src,'saved.wav');assert.strictEqual(preview.src,'preview.wav');'''
        result=subprocess.run(['node','-e',script,str(SOURCE.with_name('app-scripts.js')),actions[0]['onclick']],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)

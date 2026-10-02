"""The AppConfig schema alone determines validation of present stored fields."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from typing import Optional
from pydantic import BaseModel, Field, create_model
import config_settings as config


class ConfigSchemaBoundaryTests(unittest.TestCase):
    def load(self, value, model=None):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json';path.write_text(json.dumps(value));before=path.read_bytes()
            with patch.object(config,'AppConfig',model or config.AppConfig):result=config.load_app_config_result(str(path))
            self.assertEqual(before,path.read_bytes())
            self.assertEqual(['config.json'],[p.name for p in path.parent.iterdir()])
            return result

    def test_new_schema_scalar_is_validated_without_adding_a_second_dispatch_list(self):
        model=create_model('SchemaScalarFixture',__base__=config.AppConfig,
            boundary_count=(int,Field(default=7,ge=1,le=10)))
        valid=self.load({'boundary_count':'3','unknown':{'keep':1}},model)
        self.assertEqual({'boundary_count':3,'unknown':{'keep':1}},valid.data)
        invalid=self.load({'boundary_count':0,'llm_failover':'false'},model)
        self.assertEqual({'llm_failover':False},invalid.data)
        self.assertEqual(['boundary_count'],[w.field for w in invalid.warnings])
        self.assertFalse(invalid.needs_backup)
        self.assertEqual({},self.load({},model).data)

    def test_new_schema_sections_preserve_partial_unknowns_and_existing_shape_policies(self):
        class Section(BaseModel):
            count:int=Field(default=4,ge=1)
        model=create_model('SchemaSectionsFixture',__base__=config.AppConfig,
            required_section=(Section,...),optional_section=(Optional[Section],None))
        loaded=self.load({'required_section':{'count':'2','unknown':'kept'},
                          'optional_section':{'count':0,'unknown':'kept'}},model)
        self.assertEqual({'required_section':{'count':2,'unknown':'kept'},
                          'optional_section':{'unknown':'kept'}},loaded.data)
        self.assertEqual(['optional_section.count'],[w.field for w in loaded.warnings])
        bad=self.load({'required_section':[], 'optional_section':[]},model)
        self.assertEqual({'optional_section':None},bad.data)
        self.assertTrue(bad.needs_backup)
        self.assertEqual({'optional_section':None},self.load({'optional_section':None},model).data)
        self.assertEqual({},self.load({},model).data)

    def test_current_schema_keeps_json_native_presets_and_partial_profile_semantics(self):
        original={'llm':{'model_name':'fixture','extra':'retained'},'tts':{'parallel_workers':0},
            'llm_remote':None,'prompt_presets':[{'name':'saved','system_prompt':'body'}],
            'prompts':{'pass1_prompt_presets':[{'name':'pass1','system_prompt':'body'}]},
            'llm_mode':'wrong','generation':{},'unknown':'retained'}
        result=self.load(original)
        self.assertEqual({'model_name':'fixture','extra':'retained'},result.data['llm'])
        self.assertEqual({},result.data['tts']);self.assertNotIn('llm_mode',result.data)
        self.assertEqual({'name':'saved','description':'','variant':'default','system_prompt':'body',
            'user_prompt':'','example':'','builtin':False},result.data['prompt_presets'][0])
        self.assertEqual({},result.data['generation']);self.assertIsNone(result.data['llm_remote'])
        self.assertEqual('retained',result.data['unknown'])

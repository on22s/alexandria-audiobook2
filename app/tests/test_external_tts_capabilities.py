"""Synthetic external schema discovery and validated adapter tests."""
import unittest
from unittest.mock import patch

import external_tts_capabilities as capabilities
import external_tts_client as transport


def parameter(name, kind, default=None, use_default=False, enum=None):
    entry = {"parameter_name": name, "type": {"type": kind}, "python_type": {"type": kind}}
    if use_default:
        entry["parameter_has_default"] = True
        entry["parameter_default"] = default
    if enum is not None:
        entry["type"]["enum"] = enum
    return entry


def custom_parameters(seed_default=False, seed_kind="int", model_name="model_size", speaker_enum=None, instruct_name="instruct"):
    entries = [parameter("text", "str"), parameter("language", "str"), parameter("speaker", "str", enum=speaker_enum), parameter(instruct_name, "str"), parameter(model_name, "str")]
    seed_entry = parameter("seed", seed_kind)
    if seed_default:
        seed_entry["parameter_has_default"] = True
        seed_entry["parameter_default"] = -1
    entries.append(seed_entry)
    return entries


def clone_parameters(audio_name="ref_audio", model_name="model_size"):
    return [parameter(audio_name, "file"), parameter("ref_text", "str"), parameter("text", "str"), parameter("language", "str"), parameter("use_xvector_only", "bool"), parameter(model_name, "str"), parameter("max_chunk_chars", "int"), parameter("chunk_gap", "int"), parameter("seed", "int")]


def schema(custom_endpoint="/generate_custom_voice", clone_endpoint="/generate_voice_clone", custom_entries=None, clone_entries=None):
    return {"named_endpoints": {custom_endpoint: {"parameters": custom_entries if custom_entries is not None else custom_parameters()}, clone_endpoint: {"parameters": clone_entries if clone_entries is not None else clone_parameters()}}, "unnamed_endpoints": {}}


class CapabilityReportTests(unittest.TestCase):
    def test_known_model_and_utf8_inspection_limits_are_honest(self):
        entries = custom_parameters()
        entries[4]['type']['enum'] = ['0.6B']
        report = capabilities.build_capability_report('http://fixture', schema(custom_entries=entries))
        self.assertEqual('unsupported', report['operations']['custom']['status'])
        self.assertIn('1.7B', report['operations']['custom']['error'])
        metadata = schema()
        metadata['description'] = '\u58f0' * 40000
        self.assertEqual('supported', capabilities.build_capability_report('http://fixture', metadata)['operations']['custom']['status'])
        metadata['description'] = '\u58f0' * 50000
        limited = capabilities.build_capability_report('http://fixture', metadata)
        self.assertEqual('unknown', limited['operations']['custom']['status'])
        self.assertEqual(capabilities.LIMIT_ERROR, limited['operations']['custom']['error'])

    def test_bad_operation_does_not_poison_valid_sibling_and_optional_unknown_defaults_are_omitted(self):
        entries = custom_parameters()
        entries[0]['python_type']['type'] = 'unknown'
        entries[0]['type'] = {}
        report = capabilities.build_capability_report('http://fixture', schema(custom_entries=entries))
        self.assertEqual('unknown', report['operations']['custom']['status'])
        self.assertEqual('supported', report['operations']['clone']['status'])
        extra = parameter('advanced_options', 'list[str]', default=[], use_default=True)
        values = {'text': 'hello', 'language': 'English', 'speaker': 'Ryan', 'instruct': '', 'model_size': '1.7B', 'seed': 0, 'api_name': '/generate_custom_voice'}
        call = capabilities.build_compatible_call('http://fixture', schema(custom_entries=custom_parameters()+[extra]), (), values)
        self.assertNotIn('advanced_options', call[1])

    def test_parameter_names_require_full_match_and_filedata_meta_can_carry_extra_keys(self):
        entries = custom_parameters()+[parameter('extra\n', 'str', default='', use_default=True)]
        self.assertEqual('unknown', capabilities.build_capability_report('http://fixture', schema(custom_entries=entries))['operations']['custom']['status'])
        reference = {'path': 'synthetic.wav', 'meta': {'_type': 'gradio.FileData', 'future_key': True}}
        call = capabilities.build_compatible_call('http://fixture', schema(), (reference,'reference','target','English',False,'1.7B',200,0,0), {'api_name':'/generate_voice_clone'})
        self.assertEqual(reference, call[0][0])

    def test_canonical_schema_reports_supported_operations_and_unsupported_categories(self):
        report = capabilities.build_capability_report("http://fixture", schema())
        self.assertEqual("supported", report["operations"]["custom"]["status"])
        self.assertEqual("supported", report["operations"]["clone"]["status"])
        self.assertEqual("/generate_custom_voice", report["operations"]["custom"]["endpoint"])
        self.assertEqual(["text", "language", "speaker", "instruct", "model_size", "seed"], report["operations"]["custom"]["parameters"])
        self.assertEqual(["design", "ensemble"], report["unsupported_operations"])
        self.assertLessEqual(len(__import__("json").dumps(report).encode("utf-8")), 16384)

    def test_alias_endpoints_and_parameter_aliases_are_supported(self):
        report = capabilities.build_capability_report("http://fixture", schema("/custom_voice", "/voice_clone", custom_parameters(instruct_name="instruct_text"), clone_parameters(audio_name="audio")))
        self.assertEqual("supported", report["operations"]["custom"]["status"])
        self.assertEqual("supported", report["operations"]["clone"]["status"])
        self.assertIn("instruct", report["operations"]["custom"]["parameters"])
        self.assertIn("ref_audio", report["operations"]["clone"]["parameters"])

    def test_missing_operation_and_missing_semantics_are_unsupported(self):
        entries = [entry for entry in custom_parameters() if entry["parameter_name"] != "seed"]
        report = capabilities.build_capability_report("http://fixture", schema(custom_entries=entries))
        self.assertEqual("unsupported", report["operations"]["custom"]["status"])
        self.assertIn("seed", report["operations"]["custom"]["missing"])
        missing_clone = {"named_endpoints": {"/generate_custom_voice": {"parameters": custom_parameters()}}, "unnamed_endpoints": {}}
        clone_report = capabilities.build_capability_report("http://fixture", missing_clone)
        self.assertEqual("unsupported", clone_report["operations"]["clone"]["status"])
        self.assertIsNone(clone_report["operations"]["clone"]["endpoint"])

    def test_malformed_and_unavailable_metadata_report_unknown_with_safe_error(self):
        report = capabilities.build_capability_report("http://fixture", {"named_endpoints": []})
        self.assertEqual("unknown", report["operations"]["custom"]["status"])
        self.assertEqual(capabilities.MALFORMED_ERROR, report["operations"]["custom"]["error"])
        unavailable = capabilities.make_unknown_capability_report("http://fixture", capabilities.UNAVAILABLE_ERROR)
        self.assertEqual("unknown", unavailable["operations"]["clone"]["status"])
        self.assertEqual(capabilities.UNAVAILABLE_ERROR, unavailable["operations"]["clone"]["error"])

    def test_fingerprint_changes_with_schema_and_url(self):
        first = capabilities.build_capability_report("http://fixture-a", schema())
        second = capabilities.build_capability_report("http://fixture-a", schema(custom_entries=custom_parameters(seed_default=True)))
        third = capabilities.build_capability_report("http://fixture-b", schema())
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["fingerprint"], third["fingerprint"])

    def test_extra_required_unknown_parameter_is_unsupported(self):
        entries = custom_parameters() + [parameter("mystery", "str")]
        report = capabilities.build_capability_report("http://fixture", schema(custom_entries=entries))
        self.assertEqual("unsupported", report["operations"]["custom"]["status"])
        self.assertIn("mystery", report["operations"]["custom"]["missing"])


class CompatibleCallTests(unittest.TestCase):
    def test_schema_file_type_and_choice_labels_preserve_semantics(self):
        entries = clone_parameters()
        entries[0]['type'] = {'type': 'string'}
        report = capabilities.build_capability_report('http://fixture', schema(clone_entries=entries))
        self.assertEqual('supported', report['operations']['clone']['status'])
        custom = custom_parameters()
        custom[2]['choices'] = [['Friendly label', 'Ryan']]
        values = {'text': 'hello', 'language': 'English', 'speaker': 'Ryan', 'instruct': '', 'model_size': '1.7B', 'seed': 0, 'api_name': '/generate_custom_voice'}
        self.assertEqual('Ryan', capabilities.build_compatible_call('http://fixture', schema(custom_entries=custom), (), values)[1]['speaker'])

    def test_model_id_short_enum_is_an_explicit_accepted_identifier(self):
        entries = custom_parameters(model_name='model_id')
        entries[4]['type']['enum'] = ['1.7B']
        values = {'text': 'hello', 'language': 'English', 'speaker': 'Ryan', 'instruct': '', 'model_size': '1.7B', 'seed': 0, 'api_name': '/generate_custom_voice'}
        self.assertEqual('1.7B', capabilities.build_compatible_call('http://fixture', schema(custom_entries=entries), (), values)[1]['model_id'])

    def test_missing_remote_default_or_falsy_parameter_list_is_unknown(self):
        for malformed in (False, 0, '', {}):
            metadata = schema()
            metadata['named_endpoints']['/generate_custom_voice']['parameters'] = malformed
            self.assertEqual('unknown', capabilities.build_capability_report('http://fixture', metadata)['operations']['custom']['status'])
        entries = custom_parameters(seed_default=True)
        entries[-1].pop('parameter_default')
        self.assertEqual('unknown', capabilities.build_capability_report('http://fixture', schema(custom_entries=entries))['operations']['custom']['status'])

    def test_remote_audio_default_cannot_choose_an_upload(self):
        entries = clone_parameters()
        entries[0].update(parameter_has_default=True, parameter_default={'path': 'server-chosen.wav', 'meta': {'_type': 'gradio.FileData'}})
        values = {'ref_text': 'reference', 'text': 'target', 'language': 'English', 'use_xvector_only': False, 'model_size': '1.7B', 'max_chunk_chars': 200, 'chunk_gap': 0, 'seed': 0, 'api_name': '/generate_voice_clone'}
        with self.assertRaisesRegex(ValueError, 'missing'):
            capabilities.build_compatible_call('http://fixture', schema(clone_entries=entries), (), values)

    def test_unowned_audio_none_and_nonfinite_values_refuse(self):
        custom = custom_parameters()
        custom[3].update(parameter_has_default=True, parameter_default='Calm')
        custom[-1]['python_type']['type'] = 'int | float'
        custom[-1]['type'] = {'type': 'number'}
        values = {'text': 'hello', 'language': 'English', 'speaker': 'Ryan', 'instruct': '', 'model_size': '1.7B', 'seed': 0, 'api_name': '/generate_custom_voice'}
        for changed in ({'instruct': None}, {'seed': float('nan')}, {'seed': float('inf')}):
            with self.assertRaises(ValueError):
                capabilities.build_compatible_call('http://fixture', schema(custom_entries=custom), (), dict(values, **changed))
        for audio in ('unowned.wav', {'path': 'unowned.wav'}, {'path': 'https://invalid.example/reference.wav', 'meta': {'_type': 'gradio.FileData'}}):
            with self.assertRaises(ValueError):
                capabilities.build_compatible_call('http://fixture', schema(), (audio, 'reference', 'target', 'English', False, '1.7B', 200, 0, 0), {'api_name': '/generate_voice_clone'})

    def test_endpoint_alias_selected_from_actual_server_schema(self):
        call_args, call_kwargs = capabilities.build_compatible_call("http://fixture", schema(custom_endpoint="/custom_voice"), (), {"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "model_size": "1.7B", "seed": 0, "api_name": "/generate_custom_voice"})
        self.assertEqual((), call_args)
        self.assertEqual("/custom_voice", call_kwargs["api_name"])
        self.assertEqual(0, call_kwargs["seed"])

    def test_number_union_and_json_schema_enum_preserve_requested_values(self):
        entries = custom_parameters(speaker_enum=["Ryan"])
        entries[-1]["python_type"]["type"] = "int | float"
        entries[-1]["type"] = {"type": "number"}
        values = {"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "model_size": "1.7B", "seed": 0, "api_name": "/generate_custom_voice"}
        self.assertEqual(0, capabilities.build_compatible_call("http://fixture", schema(custom_entries=entries), (), values)[1]["seed"])
        with self.assertRaisesRegex(ValueError, "allowed value"):
            capabilities.build_compatible_call("http://fixture", schema(custom_entries=entries), (), dict(values, speaker="Other"))
        with self.assertRaisesRegex(ValueError, "incompatible type"):
            capabilities.build_compatible_call("http://fixture", schema(custom_entries=entries), (), dict(values, seed=True))

    def test_unknown_callable_name_or_type_is_unknown_not_compatible(self):
        for missing_key in ("parameter_name", "python_type"):
            entries = custom_parameters()
            entries[0].pop(missing_key)
            entries[0]["label"] = "text"
            report = capabilities.build_capability_report("http://fixture", schema(custom_entries=entries))
            self.assertEqual("unknown", report["operations"]["custom"]["status"])

    def test_optional_unknown_fields_are_not_submitted_and_required_ones_refuse(self):
        values = {"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "model_size": "1.7B", "seed": 7, "api_name": "/generate_custom_voice"}
        entries = custom_parameters() + [parameter("unrelated", "str", default="server", use_default=True)]
        call = capabilities.build_compatible_call("http://fixture", schema(custom_entries=entries), (), values)
        self.assertNotIn("unrelated", call[1])
        entries[-1]["parameter_has_default"] = False
        with self.assertRaisesRegex(ValueError, "required unknown"):
            capabilities.build_compatible_call("http://fixture", schema(custom_entries=entries), (), values)

    def test_clone_order_follows_schema_and_unnamed_endpoints_do_not_poison_it(self):
        entries = clone_parameters(model_name="model_id")
        entries = entries[-1:] + entries[:-1]
        metadata = schema(clone_endpoint="/voice_clone", clone_entries=entries)
        metadata["unnamed_endpoints"] = {0: {"parameters": []}}
        reference = {"path": "synthetic.wav", "meta": {"_type": "gradio.FileData"}}
        call_args, call_kwargs = capabilities.build_compatible_call("http://fixture", metadata, (reference, "reference", "target", "English", False, "1.7B", 200, 0, 3), {"api_name": "/generate_voice_clone"})
        self.assertEqual((3, reference, "reference", "target", "English", False, capabilities.MODEL_IDS["clone"], 200, 0), call_args)
        self.assertEqual({"api_name": "/voice_clone"}, call_kwargs)

    def test_competing_endpoint_or_model_aliases_refuse_ambiguity(self):
        metadata = schema()
        metadata["named_endpoints"]["/custom_voice"] = {"parameters": custom_parameters()}
        self.assertEqual("unsupported", capabilities.build_capability_report("http://fixture", metadata)["operations"]["custom"]["status"])
        metadata = schema(custom_entries=custom_parameters() + [parameter("model_id", "str")])
        self.assertEqual("unsupported", capabilities.build_capability_report("http://fixture", metadata)["operations"]["custom"]["status"])

    def test_exact_custom_payload_uses_canonical_kwargs_and_passes_optional_seed(self):
        entries = custom_parameters(seed_default=True)
        call_args, call_kwargs = capabilities.build_compatible_call("http://fixture", schema(custom_entries=entries), (), {"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "model_size": "1.7B", "seed": 7, "api_name": "/custom_voice"})
        self.assertEqual((), call_args)
        self.assertEqual({"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "model_size": "1.7B", "seed": 7, "api_name": "/generate_custom_voice"}, call_kwargs)

    def test_exact_clone_payload_uses_canonical_positional_order(self):
        reference = {"path": "/tmp/reference.wav", "meta": {"_type": "gradio.FileData"}}
        call_args, call_kwargs = capabilities.build_compatible_call("http://fixture", schema(), (reference, "reference words", "target", "English", False, "1.7B", 200, 0, 3), {"api_name": "/generate_voice_clone"})
        self.assertEqual((reference, "reference words", "target", "English", False, "1.7B", 200, 0, 3), call_args)
        self.assertEqual({"api_name": "/generate_voice_clone"}, call_kwargs)

    def test_clone_alias_audio_maps_to_ref_audio_position(self):
        reference = {"path": "/tmp/reference.wav", "meta": {"_type": "gradio.FileData"}}
        call_args, call_kwargs = capabilities.build_compatible_call("http://fixture", schema(clone_entries=clone_parameters(audio_name="audio")), (), {"audio": reference, "ref_text": "reference", "text": "target", "language": "English", "use_xvector_only": True, "model_size": "1.7B", "max_chunk_chars": 10, "chunk_gap": 1, "seed": 2, "api_name": "/voice_clone"})
        self.assertEqual((reference, "reference", "target", "English", True, "1.7B", 10, 1, 2), call_args)
        self.assertEqual("/generate_voice_clone", call_kwargs["api_name"])

    def test_model_id_mapping_for_custom_and_clone_never_passes_size_blindly(self):
        custom_call = capabilities.build_compatible_call("http://fixture", schema(custom_entries=custom_parameters(model_name="model_id")), (), {"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "model_size": "1.7B", "seed": 1, "api_name": "/generate_custom_voice"})
        self.assertEqual("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", custom_call[1]["model_id"])
        self.assertNotIn("model_size", custom_call[1])
        reference = {"path": "/tmp/reference.wav", "meta": {"_type": "gradio.FileData"}}
        clone_call = capabilities.build_compatible_call("http://fixture", schema(clone_entries=clone_parameters(model_name="model_id")), (reference, "reference", "target", "English", False, "1.7B", 200, 0, 1), {"api_name": "/generate_voice_clone"})
        self.assertEqual("Qwen/Qwen3-TTS-12Hz-1.7B-Base", clone_call[0][5])

    def test_type_and_enum_mismatch_are_refused_before_predict(self):
        entries = custom_parameters(speaker_enum=["Ryan"])
        with self.assertRaisesRegex(ValueError, "incompatible type"):
            capabilities.build_compatible_call("http://fixture", schema(custom_entries=entries), (), {"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "model_size": "1.7B", "seed": "bad", "api_name": "/generate_custom_voice"})
        with self.assertRaisesRegex(ValueError, "allowed value"):
            capabilities.build_compatible_call("http://fixture", schema(custom_entries=entries), (), {"text": "hello", "language": "English", "speaker": "Other", "instruct": "", "model_size": "1.7B", "seed": 1, "api_name": "/generate_custom_voice"})

    def test_unknown_metadata_and_ambiguous_alias_refuse_compatible_generation(self):
        with self.assertRaisesRegex(ValueError, "malformed"):
            capabilities.build_compatible_call("http://fixture", {"broken": True}, (), {"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "model_size": "1.7B", "seed": 1, "api_name": "/generate_custom_voice"})
        entries = custom_parameters() + [parameter("instruct_text", "str")]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            capabilities.build_compatible_call("http://fixture", schema(custom_entries=entries), (), {"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "model_size": "1.7B", "seed": 1, "api_name": "/generate_custom_voice"})

    def test_ambiguous_submitted_alias_and_missing_semantic_refuse_generation(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            capabilities.build_compatible_call("http://fixture", schema(), (), {"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "instruct_text": "", "model_size": "1.7B", "seed": 1, "api_name": "/generate_custom_voice"})
        with self.assertRaisesRegex(ValueError, "missing"):
            capabilities.build_compatible_call("http://fixture", schema(), (), {"text": "hello", "language": "English", "speaker": "Ryan", "instruct": "", "model_size": "1.7B", "api_name": "/generate_custom_voice"})


class ClientDispatchTests(unittest.TestCase):
    def test_new_methods_retire_worker_before_request_and_generic_predict_does_not(self):
        client = transport.ExternalTTSClient("http://fixture", timeout=3)
        calls = []
        def dispatch(operation, args, kwargs, retire_worker):
            calls.append((operation, retire_worker))
            if operation == "capabilities":
                return {"capabilities": {"fingerprint": "fixture", "operations": {}, "unsupported_operations": []}}, None
            return {"audio": None}, None
        client._dispatch = dispatch
        client._prediction_from_response = lambda response, directory: ("prediction", response["audio"])
        self.assertEqual("fixture", client.get_capabilities()["fingerprint"])
        self.assertEqual(("prediction", None), client.predict_compatible(text="hello"))
        self.assertEqual(("prediction", None), client.predict(text="hello"))
        self.assertEqual([("capabilities", True), ("compatible_predict", True), ("predict", False)], calls)


if __name__ == "__main__":
    unittest.main()

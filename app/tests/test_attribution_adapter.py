"""A configured adapter that is not actually serving must be noticed.

The measured stake: the distilled adapter is +9.8 points cross-book and +14.6
through the served path. A server started without it, or with its scale at
zero, answers every request happily at base quality - nothing fails, the book
generates, it is simply much worse and no artifact says why.

That is the same shape as the seed bug: a configured field silently ignored,
which cost six contaminated comparisons before anyone noticed by ear. These
tests cover the ways it could go unnoticed again.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from attribution_adapter import (AdapterError, adapter_config, check_adapter,
                                 describe)

CFG = {"llm_mode": "local",
       "llm_local": {"base_url": "http://x/v1",
                     "attribution_adapter": {"path": "/m/adapter_mixed.gguf",
                                             "scale": 1.0}}}


def served(entries):
    return patch("attribution_adapter.served_adapters", return_value=entries)


class ConfigTest(unittest.TestCase):

    def test_it_reads_from_the_active_mode_block(self):
        """The adapter travels with the endpoint it belongs to. A second
        location could disagree with base_url about which server is meant."""
        spec = adapter_config(CFG)
        self.assertEqual(spec["path"], "/m/adapter_mixed.gguf")
        self.assertEqual(spec["scale"], 1.0)

    def test_remote_mode_reads_the_remote_block(self):
        cfg = {"llm_mode": "remote",
               "llm_local": {"attribution_adapter": {"path": "/local.gguf"}},
               "llm_remote": {"attribution_adapter": {"path": "/remote.gguf"}}}
        self.assertEqual(adapter_config(cfg)["path"], "/remote.gguf")

    def test_no_adapter_configured_is_valid(self):
        self.assertIsNone(adapter_config({"llm_mode": "local",
                                          "llm_local": {}}))
        ok, msg = check_adapter({"llm_mode": "local", "llm_local": {}}, "http://x")
        self.assertTrue(ok)


class ServingTest(unittest.TestCase):

    def test_nonfinite_served_scale_never_verifies(self):
        for value in (float('nan'),float('inf')):
            with self.subTest(value=value),served([{'path':'/m/adapter_mixed.gguf','scale':value}]):
                self.assertFalse(check_adapter(CFG,'http://x/v1')[0])

    def test_invalid_configured_and_served_scales_warn_or_refuse_without_crashing(self):
        import copy
        for value in ('bad',None,True,{},float('nan'),float('inf'),-float('inf')):
            with self.subTest(value=value):
                cfg=copy.deepcopy(CFG);cfg['llm_local']['attribution_adapter']['scale']=value
                with served([{'path':'/m/adapter_mixed.gguf','scale':1}]):
                    self.assertFalse(check_adapter(cfg,'http://x/v1')[0])
                    with self.assertRaises(AdapterError):check_adapter(cfg,'http://x/v1',require=True)
                with served([{'path':'/m/adapter_mixed.gguf','scale':value}]):
                    self.assertFalse(check_adapter(CFG,'http://x/v1')[0])
                    with self.assertRaises(AdapterError):check_adapter(CFG,'http://x/v1',require=True)
        for value in (-1,0,'0'):
            cfg=copy.deepcopy(CFG);cfg['llm_local']['attribution_adapter']['scale']=value
            self.assertFalse(check_adapter(cfg,'http://x/v1')[0])

    def test_the_configured_adapter_serving_is_ok(self):
        with served([{"path": "/m/adapter_mixed.gguf", "scale": 1.0}]):
            ok, msg = check_adapter(CFG, "http://x/v1")
        self.assertTrue(ok)
        self.assertIn("adapter_mixed.gguf", msg)

    def test_a_missing_adapter_is_reported(self):
        with served([{"path": "/srv/something_else.gguf", "scale": 1.0}]):
            ok, msg = check_adapter(CFG, "http://x/v1")
        self.assertFalse(ok)
        self.assertIn("NOT loaded", msg)
        self.assertIn("something_else.gguf", msg,
                      "the message should say what IS loaded")

    def test_no_adapters_at_all_is_reported(self):
        with served([]):
            ok, msg = check_adapter(CFG, "http://x/v1")
        self.assertFalse(ok)

    def test_scale_zero_is_reported(self):
        """THE QUIET ONE. The adapter is loaded, the name matches, and it
        contributes nothing - generation runs at base quality while every
        surface check passes."""
        with served([{"path": "/m/adapter_mixed.gguf", "scale": 0.0}]):
            ok, msg = check_adapter(CFG, "http://x/v1")
        self.assertFalse(ok)
        self.assertIn("scale", msg)
        self.assertIn("BASE quality", msg)

    def test_distinct_full_paths_with_same_basename_are_not_verified(self):
        """Configure the active endpoint's reported path, not a local copy."""
        with served([{"path": "/opt/models/adapter_mixed.gguf", "scale": 1.0}]):
            ok, message = check_adapter(CFG, "http://x/v1")
        self.assertFalse(ok)
        self.assertIn('/m/adapter_mixed.gguf',message)
        self.assertIn('/opt/models/adapter_mixed.gguf',message)

    def test_remote_endpoint_uses_its_configured_server_path_without_local_alias_resolution(self):
        import tempfile
        from pathlib import Path
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);expected=root/'voice.gguf';expected.write_bytes(b'local fixture')
            different=root/'other.gguf';different.symlink_to(expected)
            cfg={'llm_mode':'remote','llm_remote':{'attribution_adapter':{'path':str(expected)}}}
            before=copy.deepcopy(cfg)
            with served([{'path':str(different),'scale':1}]):
                self.assertFalse(check_adapter(cfg,'http://x/v1')[0])
            with served([{'path':str(expected),'scale':1}]):
                self.assertTrue(check_adapter(cfg,'http://x/v1')[0])
            self.assertEqual(before,cfg)

    def test_id_only_response_does_not_prove_a_path_and_mismatch_cannot_mask_exact_entry(self):
        with served([{'id':'/m/adapter_mixed.gguf','scale':1}]):
            self.assertFalse(check_adapter(CFG,'http://x/v1')[0])
        with served([{'path':'/other/adapter_mixed.gguf','scale':0},
                     {'path':'/m/adapter_mixed.gguf','scale':1}]):
            self.assertTrue(check_adapter(CFG,'http://x/v1')[0])

    def test_an_endpoint_that_cannot_be_asked_is_UNKNOWN_not_absent(self):
        """LM Studio and Ollama do not implement /lora-adapters. Treating
        silence as 'missing' would cry wolf on every non-llama.cpp setup, and a
        warning that always fires is one nobody reads."""
        with served(None):
            ok, msg = check_adapter(CFG, "http://x/v1")
        self.assertFalse(ok)
        self.assertIn("cannot verify", msg)


class RequireTest(unittest.TestCase):

    def test_same_filename_wrong_path_refuses_required_adapter(self):
        with served([{'path':'/wrong/adapter_mixed.gguf','scale':1}]),self.assertRaises(AdapterError):
            check_adapter(CFG,'http://x/v1',require=True)

    def test_require_raises(self):
        with served([]):
            with self.assertRaises(AdapterError):
                check_adapter(CFG, "http://x/v1", require=True)

    def test_the_default_warns_rather_than_dying(self):
        """A book half-generated overnight should not die because a server was
        restarted without its adapter - but the run must carry the fact."""
        with served([]):
            ok, msg = check_adapter(CFG, "http://x/v1")
        self.assertFalse(ok)
        self.assertTrue(msg)

    def test_require_can_be_set_in_config(self):
        cfg = {"llm_mode": "local",
               "llm_local": {"attribution_adapter":
                             {"path": "/a.gguf", "require": True}}}
        with served([]):
            with self.assertRaises(AdapterError):
                check_adapter(cfg, "http://x/v1")


class DescribeTest(unittest.TestCase):

    def test_it_records_what_was_expected(self):
        self.assertIn("/m/adapter_mixed.gguf", describe(CFG))
        self.assertEqual(describe({}), "attribution_adapter=none")


class WiringTest(unittest.TestCase):

    def test_generate_script_performs_the_check(self):
        """A correct module nothing calls is the state this replaces."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "generate_script.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("check_adapter", src)
        self.assertIn("attribution_adapter", src)


class AdapterTransportTest(unittest.TestCase):
    def start_server(self,body,required_key=None,redirect=None):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path,self.headers.get('Authorization')))
                if required_key and self.headers.get('Authorization')!='Bearer '+required_key:
                    self.send_response(401);self.end_headers();return
                self.send_response(302 if redirect else 200)
                if redirect:self.send_header('Location',redirect)
                else:self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                if not redirect:
                    try:self.wfile.write(body)
                    except (BrokenPipeError,ConnectionResetError):pass
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def close():server.shutdown();server.server_close();thread.join(2)
        self.addCleanup(close)
        return f'http://127.0.0.1:{server.server_port}',requests

    def test_actual_authenticated_http_uses_active_profile_and_env_key_without_exposing_it(self):
        import copy,json
        from attribution_adapter import served_adapters
        key='fixture-private-key'
        entries=[{'path':'/m/adapter_mixed.gguf','scale':1}]
        url,requests=self.start_server(json.dumps(entries).encode(),required_key=key)
        self.assertIsNone(served_adapters(url+'/v1'))
        cfg=copy.deepcopy(CFG);cfg['llm_local']['api_key']='wrong-local-key';cfg['llm_mode']='remote'
        cfg['llm_remote']={'api_key':'env:ALEX_ADAPTER_FIXTURE_KEY','attribution_adapter':{'path':'/m/adapter_mixed.gguf','require':True}}
        before=copy.deepcopy(cfg)
        with patch.dict(os.environ,{'ALEX_ADAPTER_FIXTURE_KEY':key}):
            try:ok,message=check_adapter(cfg,url+'/v1')
            except AdapterError as error:self.fail(f'Authenticated fixture adapter was refused: {error}')
        self.assertTrue(ok);self.assertNotIn(key,message);self.assertNotIn(key,describe(cfg));self.assertEqual(before,cfg)
        self.assertEqual([('/lora-adapters',None),('/lora-adapters','Bearer '+key)],requests)

    def test_actual_http_rejects_oversized_valid_json_and_accepts_normal_response(self):
        import json
        from attribution_adapter import served_adapters,MAX_ADAPTER_RESPONSE_BYTES
        entries=[{'path':'/m/adapter_mixed.gguf','scale':1}]
        body=json.dumps(entries).encode()+b' '*(MAX_ADAPTER_RESPONSE_BYTES+1)
        self.assertEqual(entries,json.loads(body))
        oversized,_=self.start_server(body)
        self.assertIsNone(served_adapters(oversized))
        normal,_=self.start_server(json.dumps(entries).encode())
        self.assertEqual(entries,served_adapters(normal))

    def test_authenticated_redirect_cannot_forward_key_to_other_origin(self):
        from attribution_adapter import served_adapters
        target,target_requests=self.start_server(b'[]')
        source,source_requests=self.start_server(b'',redirect=target+'/lora-adapters')
        self.assertIsNone(served_adapters(source,api_key='fixture-private-key'))
        self.assertEqual([('/lora-adapters','Bearer fixture-private-key')],source_requests)
        self.assertEqual([],target_requests)


if __name__ == "__main__":
    unittest.main()

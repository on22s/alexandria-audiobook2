import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import external_tts_capabilities as capabilities
import external_tts_client as transport
from tests.test_external_tts_client_process import is_alive
FAKE_SDK = """
from concurrent.futures import ThreadPoolExecutor
import json,os,threading,wave
from pathlib import Path
ROOT=Path(__file__).parent
def record(**values):
 with (ROOT/'events.jsonl').open('a') as stream:
  stream.write(json.dumps(dict(pid=os.getpid(),**values))+'\\n')
class Client:
 def __init__(self,url):
  self.url=url;self.count=0;self.executor=ThreadPoolExecutor(2)
  record(event='init',url=url,telemetry=os.environ.get('HF_HUB_DISABLE_TELEMETRY'))
 def view_api(self,return_format=None,print_info=None):
  record(event='view',url=self.url)
  if self.url.endswith('view-hang'):
   record(event='blocked-view');threading.Event().wait()
  schema=ROOT/'schema.json'
  if schema.exists(): return json.loads(schema.read_text())
  raise RuntimeError('no schema fixture')
 def predict(self,*args,**kwargs):
  self.count+=1;text=kwargs.get('text','')
  reference=None;reference_hex=None
  for value in list(args)+list(kwargs.values()):
   if isinstance(value,dict) and isinstance(value.get('path'),str):
    reference=value['path']
    try: reference_hex=Path(reference).read_bytes().hex()
    except Exception: reference_hex=None
  if reference: record(event='upload',reference_path=reference,reference=reference_hex)
  record(event='call',count=self.count,args=args,kwargs=kwargs,text=text,reference=reference_hex,reference_path=reference)
  def work():
   output=ROOT/('provider-'+str(os.getpid())+'.wav')
   with wave.open(str(output),'wb') as wav:
    wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(self.count.to_bytes(2,'little')*2400)
   return str(output) if self.url.endswith('single-output') else (str(output),)
  return self.executor.submit(work).result()
"""
CUSTOM_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
CLONE_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
def make_param(name, kind, has_default=False, default=None, enum=None):
 item = {"parameter_name": name, "python_type": {"type": kind}, "parameter_has_default": has_default}
 if has_default: item["parameter_default"] = default
 spec = {}
 types = {"str": "string", "int": "integer", "bool": "boolean"}
 if kind in types: spec["type"] = types[kind]
 if enum is not None: spec["enum"] = enum
 if spec: item["type"] = spec
 return item
def custom_schema(endpoint="/custom_voice", speaker_enum=None, extra=False, seed_default=0):
 params = [make_param("text", "str"), make_param("language", "str"), make_param("speaker", "str", enum=speaker_enum), make_param("instruct", "str", True, "Speak clearly"), make_param("model_id", "str"), make_param("seed", "int", True, seed_default)]
 if extra: params.append(make_param("extra_required", "str"))
 return {"named_endpoints": {endpoint: {"parameters": params}}}
def clone_schema():
 params = [make_param("seed", "int"), make_param("model_id", "str"), make_param("chunk_gap", "int"), make_param("max_chunk_chars", "int"), make_param("use_xvector_only", "bool"), make_param("language", "str"), make_param("text", "str"), make_param("ref_text", "str"), make_param("ref_audio", "file")]
 return {"named_endpoints": {"/voice_clone": {"parameters": params}}}
class CapabilityProcessTests(unittest.TestCase):
 def test_single_output_sdk_and_telemetry_disabled_worker(self):
  self.write_schema(custom_schema());client=self.make_client('single-output')
  with client.predict_compatible(text='synthetic',language='English',speaker='Alice',instruct='',model_size='1.7B',seed=0,api_name='/custom_voice') as prediction:
   self.assertTrue(Path(prediction[0]).is_file())
  self.assert_stopped()
  self.assertTrue(all(row['telemetry']=='1' for row in self.events() if row['event']=='init'))
 def setUp(self):
  self.temporary = tempfile.TemporaryDirectory();self.root = Path(self.temporary.name)
  (self.root / "gradio_client.py").write_text(FAKE_SDK)
  self.environment = dict(os.environ, PYTHONPATH=str(self.root), TMPDIR=str(self.root), TEMP=str(self.root), TMP=str(self.root))
  self.clients = []
 def tearDown(self):
  for client in self.clients: client.close()
  self.temporary.cleanup()
 def make_client(self, suffix="ok", timeout=5):
  client = transport.ExternalTTSClient("http://fixture/" + suffix, timeout, environment=self.environment);self.clients.append(client);return client
 def events(self):
  path = self.root / "events.jsonl"
  return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
 def write_schema(self, value): (self.root / "schema.json").write_text(json.dumps(value))
 def wait_for(self, predicate):
  deadline = time.monotonic() + 3
  while time.monotonic() < deadline:
   if predicate(): return True
   time.sleep(.01)
  return False
 def assert_stopped(self):
  pids = {row["pid"] for row in self.events()};deadline = time.monotonic() + 3
  while any(is_alive(pid) for pid in pids) and time.monotonic() < deadline: time.sleep(.02)
  self.assertTrue(all(not is_alive(pid) for pid in pids), pids)
 def test_generic_cache_fresh_metadata_fingerprint_and_alias_dispatch(self):
  client = self.make_client()
  with client.predict(text="first"): pass
  with client.predict(text="second"): pass
  self.write_schema(custom_schema())
  report = client.get_capabilities()
  self.assertEqual("supported", report["operations"]["custom"]["status"]);self.assertEqual("/custom_voice", report["operations"]["custom"]["endpoint"])
  with client.predict_compatible(api_name="/custom_voice", text="hello", language="English", speaker="Alice", instruct="Calm", model_size="1.7B", seed=42) as result: self.assertTrue(Path(result[0]).is_file())
  calls = [row for row in self.events() if row["event"] == "call"]
  self.assertEqual([1, 2], [row["count"] for row in calls[:2]]);self.assertEqual([], calls[-1]["args"])
  self.assertEqual({"api_name": "/custom_voice", "text": "hello", "language": "English", "speaker": "Alice", "instruct": "Calm", "model_id": CUSTOM_ID, "seed": 42}, calls[-1]["kwargs"])
  again = client.get_capabilities();self.assertEqual(report["fingerprint"], again["fingerprint"])
  self.write_schema(custom_schema(seed_default=7));changed = client.get_capabilities();self.assertNotEqual(report["fingerprint"], changed["fingerprint"])
  events = self.events();self.assertEqual(5, sum(row["event"] == "init" for row in events));self.assertEqual(4, sum(row["event"] == "view" for row in events))
 def test_returned_audio_survives_next_worker_retirement_until_owner_closes(self):
  client = self.make_client();self.write_schema(custom_schema())
  with client.predict(text='retained') as retained:
   path = Path(retained[0]);original = path.read_bytes()
   client.get_capabilities()
   self.assertEqual(original, path.read_bytes())
   with client.predict_compatible(api_name='/custom_voice', text='next', language='English', speaker='Alice', instruct='', model_size='1.7B', seed=0):
    self.assertEqual(original, path.read_bytes())
   client.close()
   self.assertEqual(original, path.read_bytes())
  self.assertFalse(path.exists())
 def test_clone_reordering_model_id_mapping_and_upload_preservation(self):
  client = self.make_client();self.write_schema(clone_schema())
  source = self.root / "reference.wav";source.write_bytes(b"synthetic reference bytes")
  submitted = {"api_name": "/voice_clone", "ref_audio": {"path": str(source), "meta": {"_type": "gradio.FileData"}}, "ref_text": "reference words", "text": "clone words", "language": "English", "use_xvector_only": True, "model_size": "1.7B", "max_chunk_chars": 80, "chunk_gap": 3, "seed": 9}
  with client.predict_compatible(**submitted): pass
  call = [row for row in self.events() if row["event"] == "call"][-1]
  self.assertEqual({"api_name": "/voice_clone"}, call["kwargs"]);self.assertEqual(9, len(call["args"]));self.assertEqual(9, call["args"][0]);self.assertEqual(CLONE_ID, call["args"][1]);self.assertEqual(3, call["args"][2]);self.assertEqual(80, call["args"][3]);self.assertEqual(True, call["args"][4]);self.assertEqual("English", call["args"][5]);self.assertEqual("clone words", call["args"][6]);self.assertEqual("reference words", call["args"][7])
  self.assertEqual(b"synthetic reference bytes".hex(), call["reference"]);self.assertNotEqual(str(source), call["args"][8]["path"])
 def test_extra_enum_and_unknown_refuse_before_predict_or_upload(self):
  client = self.make_client()
  self.write_schema(custom_schema(extra=True));self.assertEqual("unsupported", client.get_capabilities()["operations"]["custom"]["status"])
  before = [row for row in self.events() if row["event"] in ("call", "upload")]
  with self.assertRaises(RuntimeError): client.predict_compatible(api_name="/custom_voice", text="x", language="English", speaker="Alice", instruct="", model_size="1.7B", seed=1)
  self.write_schema(custom_schema(speaker_enum=["Alice"]))
  with self.assertRaisesRegex(RuntimeError, "allowed value"): client.predict_compatible(api_name="/custom_voice", text="x", language="English", speaker="Mallory", instruct="", model_size="1.7B", seed=1)
  self.assertEqual(before, [row for row in self.events() if row["event"] in ("call", "upload")])
  (self.root / "schema.json").unlink();unknown = client.get_capabilities();self.assertEqual("unknown", unknown["operations"]["custom"]["status"]);self.assertEqual(capabilities.UNAVAILABLE_ERROR, unknown["operations"]["custom"]["error"])
  source = self.root / "reference.wav";source.write_bytes(b"must not upload")
  with self.assertRaises(RuntimeError): client.predict_compatible(api_name="/voice_clone", ref_audio={"path": str(source), "meta": {"_type": "gradio.FileData"}}, ref_text="r", text="t", language="English", use_xvector_only=False, model_size="1.7B", max_chunk_chars=10, chunk_gap=1, seed=1)
  self.assertEqual(before, [row for row in self.events() if row["event"] in ("call", "upload")])
 def test_view_timeout_and_cancellation_block_then_kill_and_reap(self):
  for mode in ("timeout", "cancel"):
   with self.subTest(mode=mode):
    client = self.make_client("view-hang", timeout=1 if mode == "timeout" else 30);prior = sum(row["event"] == "blocked-view" for row in self.events())
    if mode == "timeout":
     started = time.monotonic()
     with self.assertRaises(TimeoutError): client.get_capabilities()
     self.assertLess(time.monotonic() - started, 4)
    else:
     cancelled = threading.Event()
     with concurrent.futures.ThreadPoolExecutor(1) as pool:
      def call_with_control():
       with transport.ensure_external_request_control(time.monotonic() + 30, cancelled):
        return client.get_capabilities()
      future = pool.submit(call_with_control)
      self.assertTrue(self.wait_for(lambda: sum(row["event"] == "blocked-view" for row in self.events()) > prior));cancelled.set()
      with self.assertRaises(TimeoutError): future.result(3)
    self.assertTrue(sum(row["event"] == "blocked-view" for row in self.events()) > prior);self.assert_stopped();self.assertIsNone(client._resources.process);self.assertIsNone(client._resources.directory)
 def test_cleanup_failure_is_not_silent_and_closed_client_never_starts(self):
  client = self.make_client()
  with client.predict(text="warm"): pass
  directory = Path(client._resources.directory.name)
  with patch.object(transport, "stop_external_client_worker", side_effect=OSError("fixture termination failure")):
   with self.assertRaisesRegex(OSError, "fixture termination failure"): client.close()
  self.assertIsNotNone(client._resources.process);self.assertTrue(directory.exists());self.assertTrue(client._resources.pending_cleanup)
  client.close();self.assertIsNone(client._resources.process);self.assertFalse(directory.exists());self.assert_stopped()
  before = len(self.events())
  with self.assertRaisesRegex(RuntimeError, "closed"): client.get_capabilities()
  with self.assertRaisesRegex(RuntimeError, "closed"): client.predict(text="closed")
  with self.assertRaisesRegex(RuntimeError, "closed"): client.predict_compatible(text="closed")
  self.assertEqual(before, len(self.events()));self.assertIsNone(client._resources.process)
if __name__ == "__main__": unittest.main()
